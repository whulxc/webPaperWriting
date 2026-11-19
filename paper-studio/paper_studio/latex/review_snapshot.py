"""Compile a writable marked TeX against one explicitly pinned frozen baseline."""
from pathlib import Path
import json
import re
import tempfile

from paper_studio.security.policy import (
    encoded, fail, file_sha, no_links, path_for, relpath, safe_read, safe_write_new, sha,
)


def validate_targets(cfg):
    targets = cfg['execution_policy'].get('text_review_targets')
    if targets is None:
        return
    if not isinstance(targets, dict) or not targets:
        fail('CONFIG_INVALID', 'Review targets require an explicit nonempty path mapping')
    for path, value in targets.items():
        relative = relpath(path)
        if (relative.parts[0] != 'reviews' or not path.endswith('.review.tex')
                or not isinstance(value, dict) or set(value) != {'target', 'language', 'baseline'}
                or value['target'] not in cfg['execution_policy']['tex_targets']
                or value['language'] not in {'en', 'zh'}):
            fail('CONFIG_INVALID', 'Review targets require an exact reviews path, trusted target and language')
        baseline = value['baseline']
        fields = {'kind', 'manifest_sha256'}
        if isinstance(baseline, dict):
            fields |= {'version', 'revision'} if baseline.get('kind') == 'managed_revision' else {'build_id'}
        if (not isinstance(baseline, dict) or set(baseline) != fields
                or baseline.get('kind') not in {'managed_revision', 'build_snapshot'}
                or not isinstance(baseline.get('manifest_sha256'), str)
                or not re.fullmatch('[0-9a-f]{64}', baseline['manifest_sha256'])):
            fail('CONFIG_INVALID', 'Review baseline requires its immutable manifest SHA256')
        if baseline['kind'] == 'managed_revision':
            if (not cfg.get('version_management')
                    or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9._-]{0,63}', baseline.get('version', ''))
                    or not re.fullmatch('rev-[0-9]{3,6}', baseline.get('revision', ''))
                    or cfg['version_management']['targets'].get(value['language']) != value['target']):
                fail('CONFIG_INVALID', 'Managed review must name its registered language and frozen revision')
        elif not re.fullmatch('rb_[0-9a-f]{24}', baseline.get('build_id', '')):
            fail('CONFIG_INVALID', 'Build review requires an exact native build ID')
        path_for(cfg, path, True)


def selected(cfg, tex_path):
    value = cfg.get('execution_policy', {}).get('text_review_targets', {}).get(tex_path)
    if value is None:
        return None
    if (cfg.get('admission_profile') != 'paper-editor.v4'
            or cfg['execution_policy'].get('allow_text_reviews') is not True):
        fail('BUILD_TARGET_DENIED', 'Frozen-baseline text reviews are not enabled')
    path_for(cfg, tex_path, True)
    return value


def _baseline(cfg, selection):
    """Return only verified source references, never live manuscript dependencies."""
    from paper_studio.execution import target_recipe
    target = cfg['execution_policy']['tex_targets'][selection['target']]
    recipe = target_recipe(target)
    recipe.update(working_directory=target['working_directory'],
                  tex_search_paths=target['tex_search_paths'],
                  compatibility_hooks=target.get('compatibility_hooks', []))
    baseline = selection['baseline']
    if baseline['kind'] == 'managed_revision':
        from paper_studio.versions import lifecycle
        dest, _, revision = lifecycle._revision(cfg, baseline['version'], baseline['revision'])
        manifest = dest / 'revisions' / baseline['revision'] / 'manifest.json'
        if file_sha(manifest) != baseline['manifest_sha256']:
            fail('REVISION_CONFLICT', 'Selected review revision manifest changed')
        language = revision['languages'][selection['language']]
        plan = lifecycle.build_plan(cfg, language['entry_path'])
        rows = plan['files']
        def read(path):
            return lifecycle._bytes(cfg, language['inputs'][path])
        return recipe, plan['tex_path'], rows, read

    from paper_studio.provider import jobs
    from paper_studio.drive.target_display import review_build_pdf
    job = jobs.job_path(cfg, baseline['build_id'])
    raw = safe_read(job, 'manifest.json', 8 * 1024**2)
    if sha(raw) != baseline['manifest_sha256']:
        fail('REVISION_CONFLICT', 'Selected baseline build manifest changed')
    manifest = json.loads(raw)
    record = json.loads(safe_read(job, 'request.json', 8 * 1024**2))
    status = jobs._lifecycle_status(cfg, baseline['build_id'])
    request = record.get('request', {})
    snapshot = record.get('snapshot', {})
    if (status.get('status') != 'completed' or status.get('kind') != 'tex'
            or status.get('manifest_sha256') != baseline['manifest_sha256']
            or record.get('schema') != 'paper-studio.build-request.v1'
            or record.get('project_id') != cfg['project_id'] or record.get('build_id') != job.name
            or record.get('kind') != 'tex' or request.get('_text_review')
            or request.get('tex_path') != target['tex_path']
            or record.get('prepared_request_sha256') != sha(encoded(request))
            or record.get('snapshot_complete') is not True
            or manifest.get('input') != snapshot
            or snapshot.get('sha256') != sha(encoded(snapshot.get('files')))
            or any(manifest.get(key) != record.get(key) for key in
                   ('project_id', 'build_id', 'kind', 'request_sha256', 'config_revision', 'code_sha256'))):
        fail('REVIEW_BASELINE_INVALID', 'Review baseline is not a verified successful TeX build')
    recorded = {
        'engine': request.get('build_profile', 'xelatex'),
        'bibliography_engine': request.get('bibliography_engine', 'bibtex'),
        'use_latexmk': request.get('use_latexmk', False),
        'working_directory': request.get('working_directory', '.'),
        'tex_search_paths': request.get('tex_search_paths', []),
        'compatibility_hooks': request.get('compatibility_hooks', []),
    }
    if recorded != recipe:
        fail('BUILD_PROFILE_DENIED', 'Baseline build recipe differs from the registered trusted target')
    jobs.verify_snapshot(job, record)
    pdf_path, pdf = review_build_pdf(cfg, job.name)
    pdf_bytes = safe_read(pdf_path.parent, pdf_path.name, 128 * 1024**2)
    if len(pdf_bytes) != pdf['bytes'] or sha(pdf_bytes) != pdf['sha256']:
        fail('ARTIFACT_TAMPERED', 'Baseline primary PDF differs from its frozen build')
    def read(path):
        return safe_read(job / 'source', path, 64 * 1024**2)
    return recipe, request['tex_path'], snapshot['files'], read


def _inputs(cfg, tex_path, selection):
    recipe, base_tex_path, rows, read = _baseline(cfg, selection)
    budget = cfg.get('limits', {}).get('max_snapshot_bytes', 32 * 1024**2)
    if not isinstance(rows, list) or not rows or len(rows) + 1 > cfg.get('limits', {}).get('max_files', 2048):
        fail('INPUT_LIMIT', 'Frozen review inputs exceed the configured file budget')
    inputs = {}
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {'path', 'bytes', 'sha256'}
                or row['path'] in inputs or row['path'] == tex_path):
            fail('REVIEW_BASELINE_INVALID', 'Frozen review input inventory is invalid')
        relpath(row['path'])
        raw = read(row['path'])
        if len(raw) != row['bytes'] or sha(raw) != row['sha256']:
            fail('SNAPSHOT_TAMPERED', 'A frozen review input changed', path=row['path'])
        inputs[row['path']] = raw
    if base_tex_path not in inputs:
        fail('REVIEW_BASELINE_INVALID', 'Frozen review inventory has no selected main TeX')
    overlay = safe_read(cfg['project_root'], tex_path, 4 * 1024**2)
    inputs[tex_path] = overlay
    if sum(map(len, inputs.values())) > budget:
        fail('INPUT_LIMIT', 'Frozen review inputs exceed the configured byte budget')
    files = [{'path': path, 'bytes': len(raw), 'sha256': sha(raw)} for path, raw in sorted(inputs.items())]
    binding = {'source_kind': 'trusted_snapshot', 'language': selection['language'],
               'base_tex_path': base_tex_path, 'baseline': dict(selection['baseline']),
               'target': selection['target'], 'baseline_input_sha256': sha(encoded(rows)),
               'overlay_tex_path': tex_path, 'overlay_sha256': sha(overlay)}
    return recipe, inputs, files, binding


def build_plan(cfg, tex_path):
    selection = selected(cfg, tex_path)
    if selection is None:
        fail('BUILD_TARGET_DENIED', 'Review target is not registered')
    recipe, inputs, files, binding = _inputs(cfg, tex_path, selection)
    state = no_links(Path(cfg['state_root'])); state.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='text-review-plan-', dir=state) as temp:
        root = Path(temp)
        for path, raw in inputs.items():
            safe_write_new(root, path, raw)
        from paper_studio.latex.dependencies import plan_dependencies
        dependencies = plan_dependencies(root, tex_path,
            working_directory=recipe['working_directory'], search_paths=recipe['tex_search_paths'],
            texlive_root=cfg['toolchain']['texlive_root'],
            windows_fonts_root=cfg['toolchain'].get('windows_fonts_root'), build_profile=recipe['engine'])
        if any(row.get('required', True) for row in dependencies.get('missing', []) + dependencies.get('unresolved', [])):
            fail('DEPENDENCY_UNRESOLVED', 'Review uses an input absent from its pinned baseline')
        if any(row['path'] not in inputs for row in dependencies['files']):
            fail('INPUT_DENIED', 'Review dependency is outside its frozen input selection')
        from paper_studio.latex.structure import manuscript_structure
        target = cfg['execution_policy']['tex_targets'][selection['target']]
        policy = cfg['execution_policy']
        baseline = {kind:target.get(field,policy[field])
                    for kind,field in [('figure','expected_figures'),('table','expected_tables')]}
        structure = manuscript_structure(root, tex_path, dependencies, baseline)
    target = cfg['execution_policy']['tex_targets'][selection['target']]
    return {'project_id': cfg['project_id'], 'target': selection['target'] + '-text-review',
            'tex_path': tex_path, 'expected_revision': binding['overlay_sha256'],
            'input_paths': [row['path'] for row in files], 'input_sha256': sha(encoded(files)),
            'files': files, **recipe, '_text_review': binding, 'review_only': True,
            'counts': structure['counts'], 'manuscript_structure': structure,
            'structure_scope': 'reachable_tex',
            'structure_files': [row['path'] for row in structure['source_files']],
            'preview_selection': {key.removeprefix('preview_'): target[key]
                                  for key in ('preview_pages', 'preview_page_ranges') if key in target},
            'build_executed': False, 'visual_review': 'not_performed',
            'dependency_storage': 'Pinned frozen baseline and one writable marked TeX'}


def capture_snapshot(cfg, dest, request):
    tex_path = request['tex_path']
    selection = selected(cfg, tex_path)
    if selection is None:
        fail('REVIEW_BASELINE_INVALID', 'Review target is no longer registered')
    _, inputs, files, binding = _inputs(cfg, tex_path, selection)
    if (request.get('_text_review') != binding
            or request.get('_input_manifest_sha256') != sha(encoded(files))
            or request.get('expected_revision') != binding['overlay_sha256']):
        fail('REVISION_CONFLICT', 'Review source or frozen baseline changed before capture')
    dest = Path(dest); dest.mkdir(parents=True, exist_ok=False)
    for path, raw in inputs.items():
        safe_write_new(dest, path, raw)
    return {'files': files, 'bytes': sum(row['bytes'] for row in files), 'sha256': sha(encoded(files))}
