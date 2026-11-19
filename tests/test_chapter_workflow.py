from pathlib import Path
import pytest
from paper_studio.chapters.workflow import dispatch, read_revision, trusted_target, guard_write
from paper_studio.chapters.delivery import receive
from paper_studio.security.policy import StudioError, atomic_json, file_sha


@pytest.fixture
def section(tmp_path):
    project = tmp_path / 'paper'; project.mkdir()
    (project / 'templates').mkdir()
    templates = {}
    for lang in ['en', 'zh']:
        path = project / 'templates' / (lang + '.tex')
        path.write_text('\\documentclass{article}\n\\begin{document}\n@@SECTION_BODY@@\n\\end{document}')
        templates[lang] = {'path': 'templates/' + path.name, 'sha256': file_sha(path)}
    atomic_json(project / 'material.json', {'project_id': 'demo'})
    cfg = {'project_id': 'demo', 'project_root': str(project), 'state_root': str(tmp_path / 'state'),
        'artifact_root': str(tmp_path / 'artifacts'), 'writable_roots': ['chapters'],
        'readonly_roots': ['templates'], 'chapter_workflow': {'root': 'chapters', 'templates': templates}}
    args = {'project_id': 'demo', 'action': 'stage', 'section_id': 'methods', 'revision': 'r01',
        'expected_previous_sha256': 'absent', 'scope': 'Methods', 'bodies': {'en': 'Old result.', 'zh': '旧结果。'},
        'material_manifest': 'material.json', 'material_revision': file_sha(project / 'material.json'), 'dry_run': False}
    return cfg, args


def test_section_idempotency_conflicts_and_immutable_sources(section):
    cfg, args = section
    result = dispatch(cfg, args)
    assert dispatch(cfg, args)['reused']
    with pytest.raises(StudioError):dispatch(cfg, {**args, 'bodies': {'en': 'Changed', 'zh': '变了'}})
    assert trusted_target(cfg, result['tex_paths']['en'])[1]['build_profile'] == 'xelatex'
    with pytest.raises(StudioError):guard_write(cfg, result['tex_paths']['en'])
    Path(cfg['project_root'], result['tex_paths']['en']).write_text('tampered')
    with pytest.raises(StudioError):trusted_target(cfg, result['tex_paths']['en'])


def test_section_compare_and_material_identity(section):
    cfg, args = section
    old = dispatch(cfg, args)
    new = {**args, 'revision': 'r02', 'expected_previous_sha256': old['manifest_sha256'],
        'bodies': {'en': 'New result <script>bad()</script>', 'zh': '新结果。'}}
    dispatch(cfg, new)
    compared = dispatch(cfg, {'action': 'compare', 'section_id': 'methods', 'left_revision': 'r01', 'right_revision': 'r02'})
    assert 'diff_add' in compared['html'] and '<script>' not in compared['html']
    with pytest.raises(StudioError):dispatch(cfg, {**new, 'revision': '../escape'})
    with pytest.raises(StudioError):dispatch(cfg, {**new, 'revision': 'r03', 'material_revision': '0' * 64})


def test_explicit_section_templates_keep_existing_section_bytes_and_validation(section):
    cfg,args=section
    old=dispatch(cfg,args)
    paths={}
    for lang in ['en','zh']:
        relative='templates/preview_'+lang+'.tex'
        p=Path(cfg['project_root'])/relative
        p.write_text('\\documentclass{article}\n\\begin{document}\nNew preview wrapper.\n@@SECTION_BODY@@\n\\end{document}')
        paths[lang]={'path':relative,'sha256':file_sha(p)}
    cfg['chapter_workflow']['section_templates']={'shortpaper-preview':paths}
    _,_,digest=read_revision(cfg,'methods','r01')
    assert digest==old['manifest_sha256']
    new=dispatch(cfg,{**args,'section_id':'shortpaper-preview'})
    source=Path(cfg['project_root'])/new['tex_paths']['en']
    assert 'New preview wrapper.' in source.read_text()
    assert read_revision(cfg,'shortpaper-preview','r01')[2]==new['manifest_sha256']
    assert trusted_target(cfg,new['tex_paths']['en'])[1]['build_profile']=='xelatex'
    source.write_text(source.read_text().replace('New preview wrapper.','Tampered.'))
    with pytest.raises(StudioError) as changed:read_revision(cfg,'shortpaper-preview','r01')
    assert changed.value.code=='SECTION_TAMPERED'


def test_section_stage_and_build_verify_indexed_material(section):
    from paper_studio.bootstrap.handoff import create_handoff, INPUT_SCHEMA
    from paper_studio.chapters.workflow import provenance_paths

    cfg, args = section
    paper = Path(cfg['project_root'])
    research = paper.parent
    atomic_json(paper / 'PROJECT.yaml', {'project_id': 'demo', 'paper_writing_root': str(paper),
                                        'original_project_root': str(research)})
    atomic_json(research / 'result.json', {'n': 100})
    spec = {'schema': INPUT_SCHEMA, 'project_id': 'demo', 'change_id': 'record',
            'title': 'Sample size', 'summary': 'Synthetic fixture',
            'sources': [{'path': 'result.json', 'sha256': file_sha(research / 'result.json'), 'role': 'result'}],
            'evidence': [{'id': 'count', 'kind': 'measured_fact', 'statement': '100 cases',
                          'sources': [{'path': 'result.json', 'locator': {'json_pointer': '/n'}}]}],
            'impacts': {'claims': [], 'figures': [], 'sections': []}}
    handoff = create_handoff(project_root=research, paper_root=paper, project_id='demo',
                             spec=spec, expected_previous='absent')
    with pytest.raises(StudioError) as unindexed:
        dispatch(cfg, args)
    assert unindexed.value.code == 'HANDOFF_NOT_INDEXED'
    staged = {**args, 'material_manifest': handoff['manifest_relative_path'],
              'material_revision': handoff['material_revision']}
    saved = dispatch(cfg, staged)
    copied = Path(handoff['manifest_path']).parent / 'sources/result.json'
    assert copied.relative_to(paper).as_posix() in provenance_paths(cfg, saved['tex_paths']['en'])
    copied.write_text('{}')
    next_revision = {**staged, 'revision': 'r02', 'expected_previous_sha256': saved['manifest_sha256']}
    with pytest.raises(StudioError) as changed:
        dispatch(cfg, next_revision)
    assert changed.value.code == 'HANDOFF_TAMPERED'
    with pytest.raises(StudioError) as build:
        provenance_paths(cfg, saved['tex_paths']['en'])
    assert build.value.code == 'HANDOFF_TAMPERED'
    assert not (paper / 'chapters/methods/r02').exists()


def test_chapter_plan_keeps_main_identity_when_provenance_is_added(section, monkeypatch):
    from paper_studio.execution import build_plan
    cfg, args = section
    result = dispatch(cfg, args)
    cfg['execution_policy'] = {'tex_targets': {}, 'expected_figures': 0, 'expected_tables': 0}
    cfg['toolchain'] = {'texlive_root': '/usr'}
    source = result['tex_paths']['en']
    path = Path(cfg['project_root']) / source
    def dependency_plan(*a, **kw):
        return {'files': [{'path': source, 'bytes': path.stat().st_size, 'sha256': file_sha(path)}],
            'missing': [], 'unresolved': []}
    monkeypatch.setattr('paper_studio.latex.dependencies.plan_dependencies', dependency_plan)
    plan = build_plan(cfg, source)
    assert plan['expected_revision'] == file_sha(path)
    assert 'material.json' in plan['input_paths']
    assert 'chapters/methods/r01/manifest.json' in plan['input_paths']


def test_receiver_rejects_reordered_or_corrupt_chunks_without_final_file(tmp_path):
    import base64
    import hashlib
    data = b'pdf-test' * 50000
    identity = hashlib.sha256(data).hexdigest()
    def call(name, args):
        metadata = {'bytes': len(data), 'sha256': identity}
        if args['action'] == 'metadata':return metadata
        offset = args['offset']; chunk = data[offset:offset + args['length']]; end = offset + len(chunk)
        return {**metadata, 'offset': offset, 'chunk_sha256': hashlib.sha256(chunk).hexdigest(),
            'data_base64': base64.b64encode(chunk).decode(), 'next_offset': end if end < len(data) else None}
    destination = tmp_path / 'received.pdf'
    result = receive(call, project_id='demo', build_id='b', path='main.pdf', destination=destination)
    assert destination.read_bytes() == data and result['chunks'] == 2
    def corrupt(name, args):
        value = call(name, args)
        if args['action'] == 'chunk':value['offset'] += 1
        return value
    with pytest.raises(StudioError):receive(corrupt, project_id='demo', build_id='b', path='main.pdf', destination=tmp_path / 'bad.pdf')
    assert not (tmp_path / 'bad.pdf').exists()


def test_local_activation_uses_verified_archive_and_keeps_published_pointer(section):
    from paper_studio.versions import lifecycle
    from test_version_history_bilingual import build
    cfg, _ = section
    cfg['_config_revision'] = 'fixture'
    cfg['version_management'] = {'root': 'versions', 'targets': {'en': 'en', 'zh': 'zh'}}
    build_id, _, _ = build(cfg, 1, 'en')
    lifecycle.history.save_version(cfg, 'r1', [build_id], 'Synthetic local activation')
    archive, _ = lifecycle.history.verify_version(cfg, 'r1')
    dest = Path(cfg['project_root']) / 'versions' / 'r1'; dest.mkdir(parents=True)
    meta = {'schema': 'paper-studio.working-version.v1', 'project_id': 'demo', 'version': 'r1', 'status': 'published'}
    atomic_json(dest / 'VERSION.json', meta)
    args = {'action': 'activate', 'version': 'r1', 'delivery_mode': 'local', 'dry_run': False, 'request_id': 'local',
        'expected_source_manifest_sha256': file_sha(archive / 'manifest.json'), '_operation_sha256': 'd' * 64}
    result = lifecycle._activate(cfg, args, dest, meta)
    assert result['current']['active_working_version'] == 'r1'
    assert result['current']['latest_published_version'] is None
    with pytest.raises(StudioError):lifecycle._activate(cfg, {**args, 'expected_source_manifest_sha256': 'bad'}, dest, meta)
