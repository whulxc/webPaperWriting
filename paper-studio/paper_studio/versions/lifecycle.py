"""Opt-in paper versions over the existing immutable history and build engine.

Only small editorial sources are copied. Dependency references always identify
an immutable version member or an immutable candidate captured by this module.
Support files (.bib, .sty, \\input sections) change through dependency
candidates; selected figure identities remain explicit while current source
provides the figure/table counts on revise. Every change lands in a new frozen
revision, so history stays immutable and content-addressed.
"""
from __future__ import annotations
import copy
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
import time
import uuid
from paper_studio.security.policy import (artifact_project_relative, atomic_json, encoded, fail,
    file_sha, locked, no_links, relpath, safe_read, sha, safe_write_new)
from paper_studio.versions import history

LANGUAGES = ('en', 'zh')
# Text inputs a revision may override or add. Figures keep their own slots.
DEPENDENCY_SUFFIXES = frozenset({'.bib', '.bst', '.tex', '.sty', '.cls', '.cfg', '.def', '.clo',
                                 '.fd', '.ldf', '.csv', '.dat', '.txt'})
MAX_DEPENDENCY_BYTES = 16 * 1024**2
MAX_STRUCTURE_COUNT = 100


def config(cfg):
    value = cfg.get('version_management')
    if not isinstance(value, dict) or set(value) != {'root', 'targets'}:
        fail('VERSION_MANAGEMENT_DISABLED', 'Explicit project version management is required')
    relpath(value['root'])
    if set(value['targets']) != set(LANGUAGES):
        fail('CONFIG_INVALID', 'English and Chinese trusted target mappings are required')
    return value


def root(cfg):
    return no_links(Path(cfg['project_root']) / config(cfg)['root'])


def label(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', value):
        fail('INVALID_ARGUMENT', 'A bounded version label is required')
    return value


def directory(cfg, version):
    return no_links(root(cfg) / label(version))


def _json(path):
    path = no_links(Path(path))
    return json.loads(safe_read(path.parent, path.name, 8 * 1024**2))


def _identity(path):
    return file_sha(path) if path.exists() else 'absent'


def _expect(path, expected):
    if not isinstance(expected, str):
        fail('REVISION_REQUIRED', 'Expected SHA256 or absent is required', path=path.name)
    actual = _identity(path)
    if actual != expected:
        fail('REVISION_CONFLICT', 'Management identity changed', path=path.name, actual_revision=actual)


def read_version(cfg, version):
    dest = directory(cfg, version)
    m = _json(dest / 'VERSION.json')
    if m.get('schema') != 'paper-studio.working-version.v1' or m.get('project_id') != cfg['project_id'] or m.get('version') != version:
        fail('PROJECT_DENIED', 'Working version belongs to another project')
    return dest, m


def current(cfg):
    p = root(cfg) / 'CURRENT.json'
    value = _json(p) if p.exists() else {'schema': 'paper-studio.current-versions.v1',
        'project_id': cfg['project_id'], 'active_working_version': None,
        'latest_published_version': None, 'author_approved_version': None,
        'checkout_baseline': None}
    if value.get('project_id') != cfg['project_id']:
        fail('PROJECT_DENIED', 'Current version pointers belong to another project')
    return value


def status(cfg, version=None, revision=None):
    result = {'project_id': cfg['project_id'], 'current': current(cfg),
        'current_sha256': _identity(root(cfg) / 'CURRENT.json')}
    if version is not None:
        dest, m = read_version(cfg, version)
        result.update(version=m, version_sha256=file_sha(dest / 'VERSION.json'))
        if revision is not None:
            _revision(cfg, version, revision)
            from paper_studio.versions.review_deliveries import lookup
            result['review_deliveries'] = lookup(cfg, version, revision)
    elif revision is not None:
        fail('INVALID_ARGUMENT', 'Select the parent version for an exact review revision')
    return result


def guard_write(cfg, relative):
    if not cfg.get('version_management'):
        return
    p = relpath(relative); base = PurePosixPath(config(cfg)['root'])
    if not p.is_relative_to(base):
        return
    parts = p.relative_to(base).parts
    main = len(parts) == 3 and parts[1] == 'working' and parts[2] in {'main_en.tex', 'main_zh.tex'}
    staged = (len(parts) >= 4 and parts[1:3] == ('working', 'deps')
              and PurePosixPath(*parts[3:]).suffix.lower() in DEPENDENCY_SUFFIXES)
    if not (main or staged):
        fail('WRITE_DENIED', 'Version metadata, revisions and releases are managed immutable files; '
             'only draft working/main_en.tex, working/main_zh.tex and working/deps/<support file> are editable')
    _, m = read_version(cfg, parts[0])
    if m['status'] != 'draft':
        fail('WRITE_DENIED', 'Published versions are frozen; restore into a new version')


def is_managed(cfg, tex_path):
    if not cfg.get('version_management') or not isinstance(tex_path, str):
        return False
    return PurePosixPath(tex_path).is_relative_to(config(cfg)['root'])


def _revision(cfg, version, revision):
    if not re.fullmatch(r'rev-\d{3,6}', revision or ''):
        fail('INVALID_ARGUMENT', 'Revision must use rev-NNN')
    dest, m = read_version(cfg, version)
    path = dest / 'revisions' / revision / 'manifest.json'
    r = _json(path)
    if r.get('project_id') != cfg['project_id'] or r.get('version') != version or r.get('revision') != revision:
        fail('PROJECT_DENIED', 'Revision identity mismatch')
    registered = m.get('revisions', {}).get(revision)
    if registered != file_sha(path):
        fail('VERSION_TAMPERED', 'Revision manifest identity changed')
    return dest, m, r


def _history_manifest(cfg, version):
    """Bound one archive manifest to this project without rehashing every member.

    Whole-archive verification stays where a whole archive is consumed: the
    restore/rollback source, version list/compare, activate and Drive
    publication. A dependency read checks its own manifest row and then the
    member bytes against the immutable reference.
    """
    root = history.version_path(cfg, version); no_links(root)
    manifest = json.loads(safe_read(root, 'manifest.json', 8 * 1024**2))
    if (manifest.get('schema') != 'paper-studio.version.v1' or manifest.get('project_id') != cfg['project_id']
            or manifest.get('version') != version or not isinstance(manifest.get('files'), list)):
        fail('PROJECT_DENIED', 'Version identity does not belong to current project')
    return root, manifest


def _bytes(cfg, ref):
    kind = ref.get('kind')
    if kind == 'history':
        dest, m = _history_manifest(cfg, ref['version'])
        row = next((x for x in m['files'] if isinstance(x, dict) and x.get('path') == ref['member']), None)
        if row is None or any(row.get(k) != ref[k] for k in ('bytes', 'sha256')):
            fail('VERSION_TAMPERED', 'Immutable source reference differs from history manifest')
        raw = safe_read(dest, ref['member'], 128 * 1024**2)
    elif kind == 'version':
        dest, _ = read_version(cfg, ref['version'])
        raw = safe_read(dest, ref['member'], 128 * 1024**2)
    else:
        fail('VERSION_TAMPERED', 'Unrecognized immutable dependency reference')
    if len(raw) != ref['bytes'] or sha(raw) != ref['sha256']:
        fail('VERSION_TAMPERED', 'Immutable dependency bytes changed', member=ref.get('member'))
    return raw


def _reference(kind, version, member, raw):
    relpath(member)
    return {'kind': kind, 'version': version, 'member': member, 'bytes': len(raw), 'sha256': sha(raw)}


def _target(cfg, lang):
    name = config(cfg)['targets'][lang]
    target = cfg.get('execution_policy', {}).get('tex_targets', {}).get(name)
    if not isinstance(target, dict):
        fail('BUILD_TARGET_DENIED', 'Version language has no trusted recipe')
    return name, target


def _recipe(cfg, lang, old_request):
    _, trusted = _target(cfg, lang)
    from paper_studio.execution import target_recipe
    engines=target_recipe(trusted)
    recipe = {key: trusted.get(key, [] if key != 'working_directory' else '.')
        for key in ('working_directory', 'tex_search_paths', 'compatibility_hooks')}
    for key in recipe:
        if old_request.get(key, [] if key != 'working_directory' else '.') != recipe[key]:
            fail('BUILD_PROFILE_DENIED', 'Historical recipe differs from the current trusted language recipe', field=key)
    recipe.update(build_profile=engines['engine'],bibliography_engine=engines['bibliography_engine'],
                  use_latexmk=engines['use_latexmk'])
    for key,default in [('build_profile','xelatex'),('bibliography_engine','bibtex'),('use_latexmk',False)]:
        if old_request.get(key,default)!=recipe[key]:
            fail('BUILD_PROFILE_DENIED','Historical engine recipe differs from the current trusted language recipe',field=key)
    return recipe


def _figures(refs, main_bytes):
    # Figure numbers follow the manuscript, never lexicographic filesystem order.
    text = re.sub(r'(?<!\\)%[^\n]*', '', main_bytes.decode('utf-8'))
    declared = re.findall(r'\\includegraphics(?:\[[^\]]*\])?\s*\{([^}]+)\}', text)
    figures = {}; used = set()
    for index, literal in enumerate(declared, 1):
        if any(char in literal for char in r'\\$#{}'):
            fail('DEPENDENCY_UNRESOLVED', 'Dynamic figure paths need an explicit verified selection')
        candidates = [path for path in refs if Path(path).suffix.lower() in {'.pdf', '.png', '.jpg', '.jpeg'}
            and (path == literal or path.endswith('/' + literal) or str(Path(path).with_suffix('')) == literal
                or str(Path(path).with_suffix('')).endswith('/' + literal))]
        if len(candidates) != 1 or candidates[0] in used:
            fail('FIGURE_IDENTITY_CONFLICT', 'Each manuscript figure needs one unique immutable dependency', value=literal)
        path = candidates[0]; used.add(path)
        figures[f'fig{index:02d}'] = {'candidate': 'v001', 'output_path': path, 'output': refs[path],
            'editorial_sources': 'not_present_in_historical_build_snapshot'}
    return figures


def _baseline_structure(cfg):
    policy = cfg.get('execution_policy', {})
    return {'figures': policy.get('expected_figures'), 'tables': policy.get('expected_tables')}


def _structure(cfg, r):
    """A revision's declared contract; old revisions inherit the trusted baseline."""
    return dict(r['structure']) if isinstance(r.get('structure'), dict) else _baseline_structure(cfg)


def _declared_structure(value):
    if (not isinstance(value, dict) or set(value) != {'figures', 'tables'}
            or any(type(value[key]) is not int or not 0 <= value[key] <= MAX_STRUCTURE_COUNT for key in value)):
        fail('INVALID_ARGUMENT', 'structure needs integer figures and tables counts')
    return {'figures': value['figures'], 'tables': value['tables']}


def _counts(main_bytes):
    text = re.sub(r'(?<!\\)%[^\n]*', '', main_bytes.decode('utf-8'))
    return {kind + 's': len(re.findall(r'\\begin\s*\{' + kind + r'\*?\}', text)) for kind in ('figure', 'table')}


def _logical_path(cfg, value):
    """A snapshot-relative input identity outside the managed and build trees."""
    path = relpath(value)
    managed = PurePosixPath(config(cfg)['root']); artifacts = artifact_project_relative(cfg)
    if path.is_relative_to(managed) or (artifacts and path.is_relative_to(artifacts)):
        fail('INVALID_ARGUMENT', 'Managed version and build trees are not logical input paths', path=value)
    return path.as_posix()


def _dependency_path(cfg, value):
    path = _logical_path(cfg, value)
    if PurePosixPath(path).suffix.lower() not in DEPENDENCY_SUFFIXES:
        fail('INVALID_ARGUMENT', 'Dependency candidates are TeX support, bibliography or data text files; '
             'figures use action=figure', path=value)
    return path


def _new_slot(dest, m, r, fig, output):
    """A figure slot absent from the current revision keeps one logical PDF output."""
    if PurePosixPath(output).suffix.lower() != '.pdf':
        fail('FIGURE_IDENTITY_CONFLICT', 'A new figure slot needs a PDF output path', output_path=output)
    earlier = sorted(m.get('figure_candidates', {}).get(fig, {}))
    for candidate in earlier:
        saved = _json(dest / 'figures' / fig / candidate / 'manifest.json')
        if saved.get('output_path') != output:
            fail('FIGURE_IDENTITY_CONFLICT', 'All candidates of one figure slot share its logical output',
                 figure_id=fig, output_path=saved.get('output_path'))
    if not earlier and (output in _union(r) or output in _protected(r)[1]):
        fail('FIGURE_IDENTITY_CONFLICT', 'A new figure slot needs an output path that is not already an input',
             output_path=output)


def _union(r):
    merged = {}
    for lang in LANGUAGES:
        for path, ref in r['languages'][lang]['inputs'].items():
            merged.setdefault(path, ref)
    return merged


def _protected(r):
    return ({r['languages'][lang]['entry_path'] for lang in LANGUAGES},
            {figure['output_path'] for figure in r['figures'].values()})


def _input_changes(first, r):
    """Logical input differences against the restored rev-001 baseline."""
    before, after = _union(first), _union(r)
    entries = _protected(first)[0] | _protected(r)[0]
    return {'added': sorted(p for p in after if p not in before and p not in entries),
            'removed': sorted(p for p in before if p not in after and p not in entries),
            'modified': sorted(p for p in after if p in before and p not in entries
                               and after[p]['sha256'] != before[p]['sha256'])}


def _candidate_dir(target, files, manifest):
    """Publish one immutable candidate directory with a single rename."""
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = target.parent / ('.stage-' + uuid.uuid4().hex); stage.mkdir()
    try:
        for name, raw in files.items():
            safe_write_new(stage, name, raw)
        atomic_json(stage / 'manifest.json', manifest, True); stage.rename(target)
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def _restore_plan(cfg, args):
    version = label(args['version']); source = label(args.get('source_version'))
    old, old_manifest = history.verify_version(cfg, source)
    if file_sha(old / 'manifest.json') != args.get('expected_source_manifest_sha256'):
        fail('REVISION_CONFLICT', 'Restoration requires the verified historical manifest SHA256')
    builds = args.get('source_builds')
    if not isinstance(builds, dict) or set(builds) != set(LANGUAGES) or len(set(builds.values())) != 2:
        fail('INVALID_ARGUMENT', 'Distinct explicit EN and ZH historical build IDs are required')
    revision = 'rev-001'; languages = {}; main_bytes = {}; common = {}
    for lang in LANGUAGES:
        bid = builds[lang]
        if bid not in old_manifest['build_ids']:
            fail('BUILD_IDENTITY_CONFLICT', 'Historical build is not in the selected version')
        req = _json(old / 'builds' / bid / 'request.json')
        request = req['request']; main = request['tex_path']
        # The original trusted language target is the authority, not a filename guess.
        _, target = _target(cfg, lang)
        if main != target['tex_path']:
            # New-style snapshots carry an explicit language binding instead.
            if request.get('_version_binding', {}).get('language') != lang:
                fail('LANGUAGE_IDENTITY_CONFLICT', 'Historical build does not match its explicit language')
        recipe = _recipe(cfg, lang, request)
        prefix = 'builds/' + bid + '/source/'
        refs = {}
        for row in old_manifest['files']:
            if not row['path'].startswith(prefix):
                continue
            relative = row['path'][len(prefix):]
            if relative == main:
                main_bytes[lang] = safe_read(old, row['path'], 4 * 1024**2)
                continue
            ref = {'kind': 'history', 'version': source, 'member': row['path'],
                'bytes': row['bytes'], 'sha256': row['sha256']}
            if relative in common and common[relative]['sha256'] != ref['sha256']:
                fail('DEPENDENCY_CONFLICT', 'EN and ZH historical dependencies have conflicting identities', path=relative)
            common[relative] = ref; refs[relative] = ref
        if lang not in main_bytes:
            fail('NOT_FOUND', 'Historical main TeX is missing')
        entry = config(cfg)['root'] + '/' + version + '/revisions/' + revision + '/main_' + lang + '.tex'
        member = 'revisions/' + revision + '/main_' + lang + '.tex'
        refs[entry] = _reference('version', version, member, main_bytes[lang])
        languages[lang] = {'entry_path': entry, 'historical_entry_path': main,
            'historical_build_id': bid, 'recipe': recipe, 'inputs': refs}
    figures = _figures(common, main_bytes['en'])
    zh_figures = _figures(common, main_bytes['zh'])
    if [(k, v['output_path']) for k, v in figures.items()] != [(k, v['output_path']) for k, v in zh_figures.items()]:
        fail('FIGURE_IDENTITY_CONFLICT', 'Bilingual manuscript figure order differs')
    r = {'schema': 'paper-studio.revision.v1', 'project_id': cfg['project_id'],
        'version': version, 'revision': revision, 'languages': languages, 'figures': figures,
        'structure': _source_structure(cfg, old, old_manifest)}
    return r, main_bytes, old_manifest


def _source_structure(cfg, archive, manifest):
    """A managed release keeps its own declared contract; legacy history uses the baseline."""
    members = {row['path'] for row in manifest['files']}
    if 'VERSION.json' in members:
        release = json.loads(safe_read(archive, 'VERSION.json', 8 * 1024**2)).get('release') or {}
        path = 'revisions/' + str(release.get('revision')) + '/manifest.json'
        if path in members:
            value = json.loads(safe_read(archive, path, 8 * 1024**2)).get('structure')
            if value is not None:
                return _declared_structure(value)
    return _baseline_structure(cfg)


def _materialize(cfg, dest, language):
    records = []; total = 0
    budget = cfg.get('limits', {}).get('max_snapshot_bytes', 32 * 1024**2)
    if len(language['inputs']) > cfg.get('limits', {}).get('max_files', 2048):
        fail('INPUT_LIMIT', 'Revision exceeds the configured file budget')
    for path, ref in sorted(language['inputs'].items()):
        relpath(path); raw = _bytes(cfg, ref); total += len(raw)
        if total > budget:
            fail('INPUT_LIMIT', 'Revision exceeds configured snapshot budget')
        safe_write_new(dest, path, raw)
        records.append({'path': path, 'bytes': len(raw), 'sha256': sha(raw)})
    return {'files': records, 'bytes': total, 'sha256': sha(encoded(records))}


def build_plan(cfg, tex_path):
    relative = relpath(tex_path).relative_to(config(cfg)['root']).parts
    if len(relative) != 4 or relative[1] != 'revisions' or relative[3] not in {'main_en.tex', 'main_zh.tex'}:
        fail('BUILD_TARGET_DENIED', 'Only frozen version revision main files may be built')
    version, _, revision, filename = relative; lang = filename[5:7]
    dest, m, r = _revision(cfg, version, revision)
    language = r['languages'][lang]
    if tex_path != language['entry_path']:
        fail('BUILD_TARGET_DENIED', 'Revision entry mapping mismatch')
    _, trusted = _target(cfg, lang)
    recipe = dict(language['recipe'])
    for key in ('working_directory', 'tex_search_paths', 'compatibility_hooks'):
        if recipe[key] != trusted.get(key, [] if key != 'working_directory' else '.'):
            fail('BUILD_PROFILE_DENIED', 'Frozen revision recipe differs from trusted runtime recipe')
    from paper_studio.execution import target_recipe
    engines=target_recipe(trusted)
    expected={'build_profile':engines['engine'],'bibliography_engine':engines['bibliography_engine'],
              'use_latexmk':engines['use_latexmk']}
    for key,default in [('build_profile','xelatex'),('bibliography_engine','bibtex'),('use_latexmk',False)]:
        recipe.setdefault(key,default)
        if recipe[key]!=expected[key]:
            fail('BUILD_PROFILE_DENIED','Frozen revision engine differs from trusted runtime recipe',field=key)
    # Resolve against a private materialization, never against mutable working files.
    state = no_links(Path(cfg['state_root'])); state.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='version-plan-', dir=state) as temp:
        snapshot = Path(temp); snap = _materialize(cfg, snapshot, language)
        from paper_studio.latex.dependencies import plan_dependencies
        plan = plan_dependencies(snapshot, tex_path, working_directory=recipe['working_directory'],
            search_paths=recipe['tex_search_paths'], texlive_root=cfg['toolchain']['texlive_root'],
            windows_fonts_root=cfg['toolchain'].get('windows_fonts_root'),build_profile=recipe['build_profile'])
        if any(x.get('required', True) for x in plan.get('missing', []) + plan.get('unresolved', [])):
            fail('DEPENDENCY_UNRESOLVED', 'Revision contains a missing or unrecorded dependency',
                missing=plan.get('missing'), unresolved=plan.get('unresolved'))
        needed = {x['path'] for x in plan['files']}
        if not needed.issubset(language['inputs']):
            fail('DEPENDENCY_UNRESOLVED', 'Resolver selected unrecorded project inputs')
        main = safe_read(snapshot, tex_path, 4 * 1024**2)
        from paper_studio.latex.structure import manuscript_structure
        declared_structure = _structure(cfg, r)
        structure_report = manuscript_structure(snapshot, tex_path, plan,
            {'figure': declared_structure['figures'], 'table': declared_structure['tables']},
            baseline_source='frozen_revision_manifest' if 'structure' in r else 'legacy_configuration_counts')
        def logical(value):
            if isinstance(value, dict):return {key: logical(item) for key, item in value.items()}
            if isinstance(value, list):return [logical(item) for item in value]
            if isinstance(value, str) and value.startswith(str(snapshot) + '/'):
                return 'project:' + value[len(str(snapshot)) + 1:]
            return value
        plan = logical(plan)
        plan['project_dependency_paths'] = 'project: denotes logical snapshot-relative identity; materialization was temporary'
    found = _counts(main); declared = _structure(cfg, r)
    counts = {'figure': found['figures'], 'table': found['tables']}
    expected = {'figure': declared['figures'], 'table': declared['tables']}
    if counts != expected:
        fail('MANUSCRIPT_STRUCTURE_CONFLICT', 'Frozen revision differs from its declared structure', actual=counts,
             expected=expected, resolution='Revise the draft with an explicit structure declaration and matching figure slots')
    binding = {'version': version, 'revision': revision, 'language': lang,
        'manifest_sha256': file_sha(dest / 'revisions' / revision / 'manifest.json')}
    return {**plan, 'project_id': cfg['project_id'], 'target': config(cfg)['targets'][lang],
        'tex_path': tex_path, 'expected_revision': sha(main), **recipe,
        'files': snap['files'], 'input_paths': [x['path'] for x in snap['files']],
        'input_sha256': snap['sha256'], '_version_binding': binding, 'counts': structure_report['counts'], 'structure': declared,
        'manuscript_structure': structure_report, 'structure_scope':'reachable_tex',
        'structure_files':[row['path'] for row in structure_report['source_files']],
        'engine':recipe['build_profile'], 'bibliography_engine':recipe['bibliography_engine'],
        'use_latexmk':recipe['use_latexmk'],
        'build_executed': False, 'visual_review': 'not_performed',
        'dependency_storage': 'SHA256-verified immutable history members and version candidates'}


def capture_snapshot(cfg, dest, request):
    binding = request.get('_version_binding', {})
    _, _, r = _revision(cfg, binding.get('version'), binding.get('revision'))
    language = binding.get('language')
    if language not in LANGUAGES or r['languages'][language]['entry_path'] != request.get('tex_path'):
        fail('LANGUAGE_IDENTITY_CONFLICT', 'Build request and revision language differ')
    manifest = directory(cfg, binding['version']) / 'revisions' / binding['revision'] / 'manifest.json'
    if file_sha(manifest) != binding.get('manifest_sha256'):
        fail('REVISION_CONFLICT', 'Build revision changed before snapshot capture')
    dest = Path(dest); dest.mkdir(parents=True, exist_ok=False)
    snap = _materialize(cfg, dest, r['languages'][language])
    if request.get('_input_manifest_sha256') != snap['sha256']:
        fail('REVISION_CONFLICT', 'Build input selection differs from frozen revision')
    return snap


def _require_draft(m):
    if m['status'] != 'draft':
        fail('VERSION_FROZEN', 'Published version cannot accept further revisions')


def _mark(value, args, outcome=None):
    value.setdefault('committed_operations', {})[args['request_id']] = {
        'request_sha256': args['_operation_sha256'], 'action': args['action'],
        'outcome': outcome or {key: args[key] for key in
            ('version', 'revision', 'figure_id', 'candidate_id', 'drive_build_id') if key in args}}
    return value


def _write_meta(dest, m, args=None, outcome=None):
    if args is not None:_mark(m, args, outcome)
    atomic_json(dest / 'VERSION.json', m)



def _initial_current(cfg, args):
    value = current(cfg)
    if _identity(root(cfg) / 'CURRENT.json') == 'absent':
        initial = args.get('initial_published_version')
        if initial:
            history.verify_version(cfg, initial)
            value['latest_published_version'] = initial
    return value


def _restore(cfg, args):
    dest = directory(cfg, args['version']); r, sources, _ = _restore_plan(cfg, args)
    m = {'schema': 'paper-studio.working-version.v1', 'project_id': cfg['project_id'],
        'version': args['version'], 'status': 'draft', 'parent_version': args.get('rollback_from') or args['source_version'],
        'restored_from': args['source_version'], 'restored_manifest_sha256': args['expected_source_manifest_sha256'],
        'rollback_from': args.get('rollback_from') if args['action'] == 'rollback' else None,
        'creation_reason': args.get('reason') or args['action'], 'creation_request_id': args['request_id'],
        'created_at': time.time(), 'current_revision': 'rev-001',
        'revisions': {'rev-001': sha(encoded(r))}, 'selected_figures': {k: v['candidate'] for k, v in r['figures'].items()},
        'figure_candidates': {k: {'v001': sha(encoded({'project_id': cfg['project_id'], 'version': args['version'], 'figure_id': k, **v}))} for k, v in r['figures'].items()}}
    _mark(m, args, {'version': args['version'], 'revision': 'rev-001'})
    if args.get('rollback_from'):
        history.verify_version(cfg, args['rollback_from'])
    result = {'version': args['version'], 'revision': 'rev-001', 'restored_from': args['source_version'],
        'rollback_from': m['rollback_from'], 'source_manifest_sha256': args['expected_source_manifest_sha256'],
        'sources': {lang: sha(raw) for lang, raw in sources.items()}, 'dependency_count': len(r['languages']['en']['inputs']),
        'figures': len(r['figures']), 'structure': r['structure'], 'dry_run': args.get('dry_run', True)}
    if result['dry_run']:
        return result
    if dest.exists():
        fail('VERSION_EXISTS', 'Restoration target already exists')
    parent = root(cfg); parent.mkdir(parents=True, exist_ok=True)
    stage = parent / ('.stage-' + uuid.uuid4().hex); stage.mkdir()
    try:
        for lang, raw in sources.items():
            safe_write_new(stage, 'working/main_' + lang + '.tex', raw)
            safe_write_new(stage, 'revisions/rev-001/main_' + lang + '.tex', raw)
        atomic_json(stage / 'revisions/rev-001/manifest.json', r, True)
        for fig, candidate in r['figures'].items():
            atomic_json(stage / 'figures' / fig / 'v001' / 'manifest.json',
                {'project_id': cfg['project_id'], 'version': args['version'], 'figure_id': fig, **candidate}, True)
        atomic_json(stage / 'VERSION.json', m, True)
        stage.rename(dest)
        if not (parent / 'CURRENT.json').exists():
            atomic_json(parent / 'CURRENT.json', _mark(_initial_current(cfg, args), args, {'version': args['version'], 'initialized_only': True}), True)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return {**result, **status(cfg, args['version'])}


def _figure(cfg, args, dest, m):
    _require_draft(m)
    fig = args.get('figure_id'); candidate = args.get('candidate_id')
    if not re.fullmatch(r'fig\d{2,3}', fig or '') or not re.fullmatch(r'v\d{3,6}', candidate or ''):
        fail('INVALID_ARGUMENT', 'Use figNN and vNNN figure identities')
    output = _logical_path(cfg, args.get('output_path'))
    _, _, previous = _revision(cfg, m['version'], m['current_revision'])
    if fig in previous['figures']:
        if output != previous['figures'][fig]['output_path']:
            fail('FIGURE_IDENTITY_CONFLICT', 'Figure candidate must replace the registered logical output',
                 figure_id=fig, output_path=previous['figures'][fig]['output_path'])
    else:
        _new_slot(dest, m, previous, fig, output)
    paths = args.get('source_paths')
    if not isinstance(paths, list) or not 1 <= len(paths) <= 32:
        fail('INVALID_ARGUMENT', 'A bounded candidate source list is required')
    captured = {}; output_ref = None
    for row in paths:
        relative = row['path']; relpath(relative)
        # New generation remains a separate, explicitly admitted action.
        raw = safe_read(cfg['project_root'], relative, 64 * 1024**2)
        if sha(raw) != row.get('sha256'):
            fail('REVISION_CONFLICT', 'Figure source changed before capture', path=relative)
        name = Path(relative).name
        if name in captured:
            fail('FIGURE_IDENTITY_CONFLICT', 'Candidate source basenames must be unique')
        captured[name] = raw
        if row.get('role') == 'output':
            if output_ref is not None or Path(name).suffix.lower() != '.pdf':
                fail('FIGURE_IDENTITY_CONFLICT', 'Exactly one PDF output must be selected')
            output_ref = _reference('version', m['version'], f'figures/{fig}/{candidate}/{name}', raw)
    if output_ref is None:
        fail('INVALID_ARGUMENT', 'A PDF source with role=output is required')
    manifest = {'project_id': cfg['project_id'], 'version': m['version'], 'figure_id': fig,
        'candidate': candidate, 'output_path': output, 'output': output_ref,
        'sources': [{'path': name, 'bytes': len(raw), 'sha256': sha(raw)} for name, raw in captured.items()]}
    manifest['operation'] = {'request_id': args['request_id'], 'request_sha256': args['_operation_sha256']}
    target = dest / 'figures' / fig / candidate
    if target.exists():
        saved = _json(target / 'manifest.json')
        if saved != manifest or saved.get('operation') != {'request_id': args['request_id'], 'request_sha256': args['_operation_sha256']}:
            fail('VERSION_EXISTS', 'Figure candidate is immutable and already exists')
        for name, raw in captured.items():
            if safe_read(target, name, 64 * 1024**2) != raw:fail('VERSION_TAMPERED', 'Interrupted candidate differs from request')
    if args.get('dry_run', True):
        return {'dry_run': True, 'candidate': manifest}
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        stage = target.parent / ('.stage-' + uuid.uuid4().hex); stage.mkdir()
        try:
            for name, raw in captured.items():
                safe_write_new(stage, name, raw)
            atomic_json(stage / 'manifest.json', manifest, True); stage.rename(target)
        finally:
            if stage.exists():shutil.rmtree(stage)
    m.setdefault('figure_candidates', {}).setdefault(fig, {})[candidate] = file_sha(target / 'manifest.json')
    _write_meta(dest, m, args)
    return {'candidate': manifest, **status(cfg, m['version'])}


def _stage(cfg, args, dest, m):
    """Copy one frozen support input into working/deps for revision-checked editing."""
    _require_draft(m)
    path = _dependency_path(cfg, args.get('path'))
    _, _, r = _revision(cfg, m['version'], m['current_revision'])
    if path in _protected(r)[0]:
        fail('DEPENDENCY_CONFLICT', 'Entry mains are edited at working/main_<lang>.tex', path=path)
    frozen = _union(r).get(path)
    if frozen is None:
        fail('NOT_FOUND', 'Only an existing frozen input can be staged; create a new support file under '
             'working/deps/<path> with paper_edit_files', path=path)
    raw = _bytes(cfg, frozen); working = 'working/deps/' + path
    result = {'dry_run': args.get('dry_run', True), 'version': m['version'], 'revision': m['current_revision'],
              'path': path, 'working_path': config(cfg)['root'] + '/' + m['version'] + '/' + working,
              'bytes': len(raw), 'sha256': sha(raw)}
    if (dest / working).exists():
        actual = safe_read(dest, working, MAX_DEPENDENCY_BYTES)
        if actual != raw:
            fail('REVISION_CONFLICT', 'The working copy already differs from the frozen input; read and edit it instead',
                 path=result['working_path'], actual_sha256=sha(actual))
        return {**result, 'already_staged': True}
    if not result['dry_run']:
        safe_write_new(dest, working, raw)
    return {**result, 'already_staged': False}


def _dependency(cfg, args, dest, m):
    """Capture one immutable support-file candidate; revise selects it explicitly."""
    _require_draft(m)
    path = _dependency_path(cfg, args.get('path')); candidate = args.get('candidate_id')
    if not re.fullmatch(r'dep-\d{3,6}', candidate or ''):
        fail('INVALID_ARGUMENT', 'Use dep-NNN dependency candidate identities')
    _, _, r = _revision(cfg, m['version'], m['current_revision'])
    entries, outputs = _protected(r)
    if path in entries or path in outputs:
        fail('DEPENDENCY_CONFLICT', 'Entry mains and figure outputs are not dependency candidates', path=path)
    source = args.get('source_path') or config(cfg)['root'] + '/' + m['version'] + '/working/deps/' + path
    relpath(source)
    raw = safe_read(cfg['project_root'], source, MAX_DEPENDENCY_BYTES)
    if sha(raw) != args.get('source_sha256'):
        fail('REVISION_CONFLICT', 'Dependency source changed before capture', path=source, actual_sha256=sha(raw))
    base = _union(r).get(path)
    if base is not None and base['sha256'] == sha(raw):
        fail('INVALID_ARGUMENT', 'Candidate is identical to the current frozen input', path=path, unchanged=True)
    name = PurePosixPath(path).name
    manifest = {'schema': 'paper-studio.dependency-candidate.v1', 'project_id': cfg['project_id'],
        'version': m['version'], 'candidate': candidate, 'path': path,
        'output': _reference('version', m['version'], f'dependencies/{candidate}/{name}', raw),
        'source': {'path': source, 'bytes': len(raw), 'sha256': sha(raw)},
        'base': {'revision': m['current_revision'], 'sha256': base['sha256'] if base else None,
                 'change': 'modify' if base else 'add'},
        'reason': args.get('reason'),
        'operation': {'request_id': args['request_id'], 'request_sha256': args['_operation_sha256']}}
    target = dest / 'dependencies' / candidate
    if target.exists():
        if _json(target / 'manifest.json') != manifest:
            fail('VERSION_EXISTS', 'Dependency candidate is immutable and already exists', candidate_id=candidate)
        if safe_read(target, name, MAX_DEPENDENCY_BYTES) != raw:
            fail('VERSION_TAMPERED', 'Interrupted dependency candidate differs from request')
    if args.get('dry_run', True):
        return {'dry_run': True, 'candidate': manifest}
    if not target.exists():
        _candidate_dir(target, {name: raw}, manifest)
    m.setdefault('dependency_candidates', {})[candidate] = file_sha(target / 'manifest.json')
    _write_meta(dest, m, args)
    return {'candidate': manifest, **status(cfg, m['version'])}


def _select_dependencies(cfg, dest, m, r, selections):
    """Apply explicit dep-NNN / base / absent choices on top of the copied revision."""
    if not isinstance(selections, dict) or len(selections) > 64:
        fail('INVALID_ARGUMENT', 'dependency_selections maps at most 64 logical paths')
    first = None; chosen = dict(r.get('dependency_selections', {}))
    entries, outputs = _protected(r)
    for raw_path, choice in sorted(selections.items()):
        path = _dependency_path(cfg, raw_path)
        if path in entries or path in outputs:
            fail('DEPENDENCY_CONFLICT', 'Entry mains and figure outputs are not dependency selections', path=path)
        present = [lang for lang in LANGUAGES if path in r['languages'][lang]['inputs']]
        if choice == 'absent':
            if not present:
                fail('NOT_FOUND', 'Dependency is not an input of the current revision', path=path)
            for lang in present:
                del r['languages'][lang]['inputs'][path]
            chosen.pop(path, None)
        elif choice == 'base':
            if first is None:
                _, _, first = _revision(cfg, m['version'], 'rev-001')
            for lang in LANGUAGES:
                original = first['languages'][lang]['inputs'].get(path)
                if original is None:
                    r['languages'][lang]['inputs'].pop(path, None)
                else:
                    r['languages'][lang]['inputs'][path] = original
            chosen.pop(path, None)
        else:
            if not re.fullmatch(r'dep-\d{3,6}', choice or ''):
                fail('INVALID_ARGUMENT', 'Use dep-NNN, base or absent', path=path)
            manifest_path = dest / 'dependencies' / choice / 'manifest.json'
            selected = _json(manifest_path)
            if m.get('dependency_candidates', {}).get(choice) != file_sha(manifest_path):
                fail('VERSION_TAMPERED', 'Dependency candidate manifest changed', candidate_id=choice)
            if (selected.get('project_id') != cfg['project_id'] or selected.get('version') != m['version']
                    or selected.get('candidate') != choice):
                fail('PROJECT_DENIED', 'Dependency candidate belongs to another version')
            if selected.get('path') != path:
                fail('DEPENDENCY_CONFLICT', 'Dependency candidate was captured for another path',
                     path=path, candidate_path=selected.get('path'))
            _bytes(cfg, selected['output'])
            for lang in present or LANGUAGES:
                r['languages'][lang]['inputs'][path] = selected['output']
            chosen[path] = choice
    r['dependency_selections'] = chosen


def _select_figures(cfg, dest, m, r, selections, structure):
    """Keep, replace, add or remove explicitly selected immutable figure slots."""
    if not isinstance(selections, dict) or len(selections) > MAX_STRUCTURE_COUNT:
        fail('INVALID_ARGUMENT', 'figure_selections maps figNN to vNNN')
    added = sorted(set(selections) - set(r['figures'])); removed = sorted(set(r['figures']) - set(selections))
    for fig in removed:
        output = r['figures'].pop(fig)['output_path']
        for lang in LANGUAGES:
            r['languages'][lang]['inputs'].pop(output, None)
    for fig, candidate in sorted(selections.items()):
        if not re.fullmatch(r'fig\d{2,3}', fig) or not re.fullmatch(r'v\d{3,6}', candidate or ''):
            fail('INVALID_ARGUMENT', 'Use an immutable vNNN figure candidate for each figNN slot')
        selected_path = dest / 'figures' / fig / candidate / 'manifest.json'
        selected = _json(selected_path)
        if m.get('figure_candidates', {}).get(fig, {}).get(candidate) != file_sha(selected_path):
            fail('VERSION_TAMPERED', 'Figure candidate manifest changed')
        if selected.get('project_id') != cfg['project_id'] or selected.get('version') != m['version'] or selected.get('figure_id') != fig:
            fail('PROJECT_DENIED', 'Figure candidate belongs to another version')
        _bytes(cfg, selected['output'])
        output = selected['output_path']
        if fig in added:
            taken = {item['output_path'] for item in r['figures'].values()}
            if output in _union(r) or output in taken:
                fail('FIGURE_IDENTITY_CONFLICT', 'A new figure slot needs an output path that is not already an input',
                     figure_id=fig, output_path=output)
        elif output != r['figures'][fig]['output_path'] or any(
                output not in r['languages'][lang]['inputs'] for lang in LANGUAGES):
            fail('FIGURE_IDENTITY_CONFLICT', 'Figure dependency absent from language input map', figure_id=fig)
        r['figures'][fig] = {key: selected[key] for key in ('candidate', 'output_path', 'output')}
        for lang in LANGUAGES:
            r['languages'][lang]['inputs'][output] = selected['output']
    return added, removed


def _check_manuscripts(r, sources, structure):
    """Reject a revision whose working mains disagree with its slots or structure."""
    order = {}
    for lang in LANGUAGES:
        counts = _counts(sources[lang])
        if counts != structure:
            fail('MANUSCRIPT_STRUCTURE_CONFLICT', 'Working manuscript differs from the declared structure',
                 language=lang, actual=counts, expected=structure,
                 resolution='Declare the intended figure/table counts with structure, together with matching figure slots')
        order[lang] = [item['output_path'] for _, item in sorted(_figures(r['languages'][lang]['inputs'], sources[lang]).items())]
    if order['en'] != order['zh']:
        fail('FIGURE_IDENTITY_CONFLICT', 'Bilingual manuscript figure order differs', en=order['en'], zh=order['zh'])
    slots = sorted(item['output_path'] for item in r['figures'].values())
    if sorted(order['en']) != slots:
        fail('FIGURE_IDENTITY_CONFLICT', 'Manuscript graphics must match the selected figure slots',
             manuscript=order['en'], slots=slots)


def _revise(cfg, args, dest, m):
    _require_draft(m)
    _, _, old = _revision(cfg, m['version'], m['current_revision'])
    index = max(int(x[4:]) for x in m['revisions']) + 1; revision = f'rev-{index:03d}'
    r = copy.deepcopy(old); r.pop('operation', None); r.pop('structure_change', None); r['revision'] = revision
    expected = args.get('expected_sources')
    if not isinstance(expected, dict) or set(expected) != set(LANGUAGES):
        fail('REVISION_REQUIRED', 'Both working source SHA256 values are required')
    sources = {}
    for lang in LANGUAGES:
        raw = safe_read(dest, f'working/main_{lang}.tex', 4 * 1024**2)
        if sha(raw) != expected[lang]:
            fail('REVISION_CONFLICT', 'Working source differs from expected SHA256', language=lang)
        sources[lang] = raw; record = r['languages'][lang]
        del record['inputs'][record['entry_path']]
        entry = config(cfg)['root'] + '/' + m['version'] + '/revisions/' + revision + '/main_' + lang + '.tex'
        record['entry_path'] = entry
        record['inputs'][entry] = _reference('version', m['version'], f'revisions/{revision}/main_{lang}.tex', raw)
    before = _structure(cfg, old)
    declared = args.get('structure')
    structure = _counts(sources['en']) if declared is None else _declared_structure(declared)
    _select_dependencies(cfg, dest, m, r, args.get('dependency_selections', {}))
    selections = args.get('figure_selections', m['selected_figures'])
    added, removed = _select_figures(cfg, dest, m, r, selections, declared)
    r['structure'] = structure
    if structure != before or added or removed:
        r['structure_change'] = {'from': before, 'to': structure, 'added_figures': added,
                                 'removed_figures': removed,
                                 'reason': args.get('reason') or 'Derived from the current manuscript source',
                                 'source_sha256': {lang:sha(raw) for lang,raw in sources.items()}}
    _check_manuscripts(r, sources, structure)
    r['operation'] = {'request_id': args['request_id'], 'request_sha256': args['_operation_sha256']}
    if args.get('dry_run', True):
        _, _, first = _revision(cfg, m['version'], 'rev-001')
        return {'dry_run': True, 'version': m['version'], 'revision': revision,
            'sources': {k: sha(v) for k, v in sources.items()}, 'figure_selections': selections,
            'dependency_selections': r['dependency_selections'], 'structure': structure,
            'structure_change': r.get('structure_change'), 'input_changes': _input_changes(first, r)}
    target = dest / 'revisions' / revision
    if target.exists():
        if _json(target / 'manifest.json') != r:fail('VERSION_EXISTS', 'Revision directory is already present')
        for lang, raw in sources.items():
            if safe_read(target, f'main_{lang}.tex', 4 * 1024**2) != raw:fail('VERSION_TAMPERED', 'Interrupted revision source differs from request')
    else:
        stage = target.parent / ('.stage-' + uuid.uuid4().hex); stage.mkdir()
        try:
            for lang, raw in sources.items():safe_write_new(stage, f'main_{lang}.tex', raw)
            atomic_json(stage / 'manifest.json', r, True); stage.rename(target)
        finally:
            if stage.exists():shutil.rmtree(stage)
    m['revisions'][revision] = file_sha(target / 'manifest.json')
    m['current_revision'] = revision; m['selected_figures'] = selections
    m['selected_dependencies'] = r['dependency_selections']
    _write_meta(dest, m, args, {'version': m['version'], 'revision': revision})
    return {'revision': revision, **status(cfg, m['version'])}


def _promote(cfg, args, dest, m):
    _require_draft(m)
    revision = args.get('revision', m['current_revision'])
    _, _, r = _revision(cfg, m['version'], revision)
    builds = args.get('build_ids')
    if not isinstance(builds, dict) or set(builds) != set(LANGUAGES) or len(set(builds.values())) != 2:
        fail('INVALID_ARGUMENT', 'Promotion requires distinct EN and ZH builds')
    from paper_studio.provider import jobs
    results = {}; plans = {}
    for lang in LANGUAGES:
        bid = builds[lang]; state = jobs.read_status(cfg, bid)
        if state['status'] != 'completed' or state.get('kind') != 'tex':
            fail('BUILD_NOT_COMPLETE', 'Promotion requires completed bilingual TeX builds', build_id=bid)
        job = jobs.job_path(cfg, bid); record = _json(job / 'request.json')
        plan = build_plan(cfg, r['languages'][lang]['entry_path']); plans[lang] = plan
        if record.get('request', {}).get('_version_binding') != plan['_version_binding'] or record.get('snapshot', {}).get('sha256') != plan['input_sha256']:
            fail('BUILD_IDENTITY_CONFLICT', 'Build does not belong to selected revision and language', build_id=bid)
        from paper_studio.versions.current_inputs import verify_build_current
        verify_build_current(cfg,bid)
        manifest = _json(job / 'manifest.json'); results[lang] = manifest['result']
        if not results[lang].get('pdf_path') or not isinstance(results[lang].get('pages'), int):
            fail('BUILD_NOT_COMPLETE', 'Completed build has no verified PDF identity')
    visual = args.get('visual_review')
    if not isinstance(visual, dict) or set(visual) != {'path', 'sha256', 'pages_seen'}:
        fail('VISUAL_REVIEW_REQUIRED', 'Actual bilingual page review and report identity are required')
    report = safe_read(cfg['project_root'], visual['path'], 2 * 1024**2)
    if sha(report) != visual['sha256']:
        fail('REVISION_CONFLICT', 'Visual review report identity changed')
    for lang in LANGUAGES:
        if visual['pages_seen'].get(lang) != list(range(1, results[lang]['pages'] + 1)):
            fail('VISUAL_REVIEW_REQUIRED', 'Every newly built PDF page must be reviewed', language=lang)
    release = {'schema': 'paper-studio.release-selection.v1', 'project_id': cfg['project_id'],
        'version': m['version'], 'revision': revision, 'revision_manifest_sha256': m['revisions'][revision],
        'build_ids': builds, 'figures': r['figures'], 'languages': {}, 'visual_review': visual,
        'system_environment': 'Build recorder and trusted toolchain identity retained; bit-for-bit OS/font environment recreation is not claimed'}
    if 'structure' in r or r.get('dependency_selections'):
        # Older releases keep their exact bytes; new ones record the contract they were built under.
        _, _, first = _revision(cfg, m['version'], 'rev-001')
        release.update(structure=_structure(cfg, r), input_changes=_input_changes(first, r),
            dependencies={path: {'candidate': candidate, 'sha256': _union(r)[path]['sha256']}
                          for path, candidate in sorted(r.get('dependency_selections', {}).items())})
    for lang in LANGUAGES:
        result = results[lang]
        release['languages'][lang] = {'tex_path': r['languages'][lang]['entry_path'],
            'tex_sha256': plans[lang]['expected_revision'], 'input_sha256': plans[lang]['input_sha256'],
            'build_id': builds[lang], 'pdf_path': 'builds/' + builds[lang] + '/output/' + result['pdf_path'],
            'pdf_sha256': result['pdf_sha256'], 'pages': result['pages'], 'delivery_path': f'release/main_{lang}.pdf'}
    frozen = {**m, 'status': 'published', 'current_revision': revision,
        'selected_figures': {k: v['candidate'] for k, v in r['figures'].items()},
        'release': release, 'promotion_request_id': args['request_id']}
    _mark(frozen, args, {'version': m['version'], 'revision': revision})
    extra = {'VERSION.json': encoded(frozen), 'release/manifest.json': encoded(release),
        'release/visual_review.md': report}
    for lang in LANGUAGES:
        raw = _bytes(cfg, r['languages'][lang]['inputs'][r['languages'][lang]['entry_path']])
        extra[f'release/main_{lang}.tex'] = raw
        pdf = safe_read(jobs.job_path(cfg, builds[lang]) / 'output', results[lang]['pdf_path'], 64 * 1024**2)
        if sha(pdf) != results[lang]['pdf_sha256']:
            fail('ARTIFACT_TAMPERED', 'Release PDF differs from the verified build identity')
        extra[f'release/main_{lang}.pdf'] = pdf
    for base in ('revisions', 'figures', 'dependencies'):
        for path in sorted((dest / base).rglob('*')):
            if path.is_file():
                relative = path.relative_to(dest).as_posix()
                extra[relative] = safe_read(dest, relative, 64 * 1024**2)
    if args.get('dry_run', True):
        return {'dry_run': True, 'release': release, 'extra_file_count': len(extra)}
    published_path = history.version_path(cfg, m['version'])
    if published_path.exists():
        # A process may have stopped after immutable save but before freezing work metadata.
        _, immutable = history.verify_version(cfg, m['version'])
        saved = safe_read(published_path, 'VERSION.json', 8 * 1024**2)
        if saved != encoded(frozen) or immutable['build_ids'] != [builds['en'], builds['zh']]:
            fail('VERSION_EXISTS', 'Existing immutable publication differs from this promotion')
        saved_result = {'version': m['version'], 'manifest_sha256': file_sha(published_path / 'manifest.json'), 'immutable': True}
    else:
        saved_result = history.save_version(cfg, m['version'], [builds['en'], builds['zh']],
            args.get('changelog') or m['creation_reason'], extra_files=extra)
    # release is recoverable from the immutable archive if this final local commit is interrupted.
    (dest / 'release').mkdir(exist_ok=True)
    for relative, raw in extra.items():
        if relative.startswith('release/'):
            p = dest / relative
            if p.exists() and safe_read(dest, relative, 64 * 1024**2) != raw:
                fail('VERSION_TAMPERED', 'Release file changed during promotion')
            if not p.exists():safe_write_new(dest, relative, raw)
    _write_meta(dest, frozen)
    return {**saved_result, 'release': release, **status(cfg, m['version'])}


def _activate(cfg, args, dest, m):
    if m['status'] != 'published':
        fail('VERSION_NOT_PUBLISHED', 'Only a frozen, built version may become current')
    archive, archive_manifest = history.verify_version(cfg, m['version'])
    from paper_studio.versions.current_inputs import verify_version_current
    verify_version_current(cfg,m['version'])
    manifest_sha = file_sha(archive / 'manifest.json')
    mode = args.get('delivery_mode', 'drive')
    if mode not in {'local', 'drive'}:
        fail('INVALID_ARGUMENT', 'Activation delivery_mode must be local or drive')
    if mode == 'local':
        if args.get('drive_build_id') is not None:
            fail('INVALID_ARGUMENT', 'Local activation cannot consume a Drive receipt')
        if args.get('expected_source_manifest_sha256') != manifest_sha:
            fail('VERSION_TAMPERED', 'Local activation requires the selected immutable archive SHA256')
        before = current(cfg)
        after = {**before, 'active_working_version': m['version'],
            'latest_local_version': m['version'], 'activation': {'delivery_mode': 'local',
                'version_manifest_sha256': manifest_sha, 'request_id': args['request_id']}}
        if args.get('dry_run', True):
            return {'dry_run': True, 'current_before': before, 'current_after': after}
        atomic_json(root(cfg) / 'CURRENT.json', _mark(after, args))
        return status(cfg, m['version'])
    from paper_studio.provider import jobs
    bid = args.get('drive_build_id'); state = jobs.read_status(cfg, bid)
    if state.get('kind') != 'drive' or state['status'] != 'completed':
        fail('DRIVE_INCOMPLETE', 'Current pointers require a completed publication')
    job = jobs.job_path(cfg, bid); receipt = _json(job / 'output' / 'receipt.json')
    if receipt.get('project_id') != cfg['project_id'] or receipt.get('version') != m['version'] or receipt.get('version_manifest_sha256') != manifest_sha or receipt.get('upload_complete') is not True:
        fail('DRIVE_IDENTITY_CONFLICT', 'Drive receipt does not identify this immutable version')
    files = receipt.get('files', [])
    if not files or any(x.get('metadata_verified') is not True or x.get('server_sha256_verified') is not True for x in files):
        fail('DRIVE_INCOMPLETE', 'Every published object requires server SHA256 verification')
    from paper_studio.drive.publisher import publication_uploads
    uploads = publication_uploads(archive, archive_manifest,
        {'sha256': manifest_sha, 'bytes': (archive / 'manifest.json').stat().st_size})
    expected = {name: {'version_path': path.relative_to(archive).as_posix(), **identity}
        for path, name, identity in uploads}
    remote = {row.get('name'): row for row in files}
    if len(remote) != len(files) or set(remote) != set(expected):
        fail('DRIVE_INCOMPLETE', 'Receipt must cover every selected publication member exactly once')
    for name, identity in expected.items():
        row = remote[name]
        if not row.get('id') or any(row.get(key) != value for key, value in identity.items()):
            fail('DRIVE_IDENTITY_CONFLICT', 'Remote publication identity differs from immutable member', name=name)
    before = current(cfg); after = {**before, 'active_working_version': m['version'],
        'latest_published_version': m['version'], 'activation': {'drive_build_id': bid,
            'version_manifest_sha256': manifest_sha, 'request_id': args['request_id']}}
    if args.get('dry_run', True):return {'dry_run': True, 'current_before': before, 'current_after': after}
    atomic_json(root(cfg) / 'CURRENT.json', _mark(after, args))
    return status(cfg, m['version'])


def _recover_committed(cfg, args, intent):
    """Reconstruct a lost response only from a request-bound atomic commit.

    A pending intent is not proof of completion. The final metadata commit must
    carry the exact request digest, and all immutable outputs are checked again.
    No newer pointer or metadata state is replaced during reconciliation.
    """
    action = args['action']; request_id = args['request_id']; digest = args['_operation_sha256']
    def marker(value):
        row = value.get('committed_operations', {}).get(request_id)
        if row is None:return None
        if row.get('request_sha256') != digest or row.get('action') != action:
            fail('IDEMPOTENCY_CONFLICT', 'Atomic commit belongs to another request body')
        return row
    if action in {'activate', 'checkout'}:
        committed = marker(current(cfg))
        if committed is None:return None
        if action == 'checkout':
            archived, _ = history.verify_version(cfg, args['version'])
            if file_sha(archived / 'manifest.json') != args['expected_source_manifest_sha256']:
                fail('VERSION_TAMPERED', 'Recovered checkout source manifest changed')
            return {'dry_run': False, 'checkout_baseline': {'version': args['version'],
                'manifest_sha256': args['expected_source_manifest_sha256'], 'readonly': True},
                'recovered_from_commit': True, **status(cfg)}
        dest, m = read_version(cfg, args['version'])
        _activate(cfg, {**args, 'dry_run': True}, dest, m)
        return {'recovered_from_commit': True, **status(cfg, args['version'])}
    dest = directory(cfg, args['version'])
    if not (dest / 'VERSION.json').exists():return None
    dest, m = read_version(cfg, args['version']); committed = marker(m)
    if committed is None:return None
    outcome = committed.get('outcome', {})
    if action in {'restore', 'rollback', 'revise', 'select', 'promote'}:
        revision = outcome.get('revision')
        _, _, r = _revision(cfg, args['version'], revision)
        for language in r['languages'].values():
            for ref in language['inputs'].values():_bytes(cfg, ref)
    if action in {'restore', 'rollback'}:
        old, _ = history.verify_version(cfg, args['source_version'])
        if file_sha(old / 'manifest.json') != args['expected_source_manifest_sha256'] or m.get('restored_manifest_sha256') != args['expected_source_manifest_sha256']:
            fail('VERSION_TAMPERED', 'Recovered restoration source identity changed')
        pointer = root(cfg) / 'CURRENT.json'
        # The directory rename is a commit, but initial CURRENT creation follows
        # it. Fill that one missing file only under the original absent guard.
        if not pointer.exists() and args['expected_current_sha256'] == 'absent':
            atomic_json(pointer, _mark(_initial_current(cfg, args), args,
                {'version': args['version'], 'initialized_only': True}), True)
        return {'dry_run': False, 'version_id': args['version'], 'revision': revision,
            'restored_from': args['source_version'], 'rollback_from': m.get('rollback_from'),
            'source_manifest_sha256': args['expected_source_manifest_sha256'],
            'sources': {lang: r['languages'][lang]['inputs'][r['languages'][lang]['entry_path']]['sha256'] for lang in LANGUAGES},
            'recovered_from_commit': True, **status(cfg, args['version'])}
    if action == 'figure':
        fig, candidate = args['figure_id'], args['candidate_id']
        p = dest / 'figures' / fig / candidate / 'manifest.json'; selected = _json(p)
        if m.get('figure_candidates', {}).get(fig, {}).get(candidate) != file_sha(p):
            fail('VERSION_TAMPERED', 'Recovered figure commit changed')
        if selected.get('operation') != {'request_id': request_id, 'request_sha256': digest}:
            fail('IDEMPOTENCY_CONFLICT', 'Figure candidate was not created by this request')
        for row in selected['sources']:
            raw = safe_read(p.parent, row['path'], 64 * 1024**2)
            if len(raw) != row['bytes'] or sha(raw) != row['sha256']:
                fail('VERSION_TAMPERED', 'Recovered figure source changed')
        _bytes(cfg, selected['output'])
        return {'candidate': selected, 'recovered_from_commit': True, **status(cfg, args['version'])}
    if action == 'dependency':
        p = dest / 'dependencies' / args['candidate_id'] / 'manifest.json'; selected = _json(p)
        if m.get('dependency_candidates', {}).get(args['candidate_id']) != file_sha(p):
            fail('VERSION_TAMPERED', 'Recovered dependency commit changed')
        if selected.get('operation') != {'request_id': request_id, 'request_sha256': digest}:
            fail('IDEMPOTENCY_CONFLICT', 'Dependency candidate was not created by this request')
        _bytes(cfg, selected['output'])
        return {'candidate': selected, 'recovered_from_commit': True, **status(cfg, args['version'])}
    if action == 'promote':
        archive, manifest = history.verify_version(cfg, args['version'])
        if safe_read(archive, 'VERSION.json', 8 * 1024**2) != encoded(m):
            fail('VERSION_TAMPERED', 'Recovered frozen metadata differs from immutable archive')
        return {'immutable': True, 'manifest_sha256': file_sha(archive / 'manifest.json'),
            'release': m['release'], 'recovered_from_commit': True, **status(cfg, args['version'])}
    return {'revision': outcome.get('revision'), 'recovered_from_commit': True,
        **status(cfg, args['version'])}


def _dispatch(cfg, args):
    """The optional action branch of paper_version_derive; no free filesystem API."""
    config(cfg); args = dict(args); action = args.get('action', 'status')
    if action == 'status':return status(cfg, args.get('version'), args.get('revision'))
    allowed = {'checkout', 'restore', 'rollback', 'stage', 'dependency', 'revise', 'figure', 'select', 'promote', 'activate'}
    if action not in allowed:fail('INVALID_ARGUMENT', 'Unsupported version management action')
    if type(args.get('dry_run', True)) is not bool:fail('INVALID_ARGUMENT', 'dry_run must be boolean')
    request_id = args.get('request_id')
    if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', request_id):
        fail('INVALID_ARGUMENT', 'A stable bounded request_id is required')
    version = label(args.get('version')); base = root(cfg); dest = directory(cfg, version)
    body = {k: v for k, v in args.items() if k != 'project_id'}; digest = sha(encoded(body))
    args['_operation_sha256'] = digest
    journal = Path(cfg['state_root']) / 'version-operations' / (request_id + '.json')
    intent = Path(cfg['state_root']) / 'version-operation-intents' / (request_id + '.json')
    with locked(cfg, 'lifecycle'):
        if not args.get('dry_run', True) and journal.exists():
            previous = _json(journal)
            if previous.get('project_id') != cfg['project_id'] or previous.get('request_sha256') != digest:
                fail('IDEMPOTENCY_CONFLICT', 'Request identity is bound to another version action')
            return {**previous['result'], 'idempotent_replay': True}
        if not args.get('dry_run', True) and intent.exists():
            pending = _json(intent)
            if pending.get('project_id') != cfg['project_id'] or pending.get('request_sha256') != digest:
                fail('IDEMPOTENCY_CONFLICT', 'Pending request identity is bound to another version action')
            recovered = _recover_committed(cfg, args, pending)
            if recovered is not None:
                atomic_json(journal, {'schema': 'paper-studio.version-operation.v1', 'project_id': cfg['project_id'],
                    'request_id': request_id, 'request_sha256': digest, 'action': action, 'result': recovered}, True)
                return {**recovered, 'idempotent_replay': True}
        _expect(base / 'CURRENT.json', args.get('expected_current_sha256'))
        if not args.get('dry_run', True) and not intent.exists():
            if action != 'checkout':_expect(dest / 'VERSION.json', args.get('expected_version_sha256'))
            atomic_json(intent, {'schema': 'paper-studio.version-operation-intent.v1', 'project_id': cfg['project_id'],
                'request_id': request_id, 'request_sha256': digest, 'request': body}, True)
        if action == 'checkout':
            archived, _ = history.verify_version(cfg, version)
            if args.get('expected_source_manifest_sha256') != file_sha(archived / 'manifest.json'):
                fail('REVISION_CONFLICT', 'Checkout requires the immutable manifest identity')
            value = current(cfg); value['checkout_baseline'] = {'version': version,
                'manifest_sha256': args['expected_source_manifest_sha256'], 'readonly': True}
            result = {'dry_run': args.get('dry_run', True), 'checkout_baseline': value['checkout_baseline']}
            if not result['dry_run']:atomic_json(base / 'CURRENT.json', _mark(value, args))
        else:
            _expect(dest / 'VERSION.json', args.get('expected_version_sha256'))
            if action in {'restore', 'rollback'}:
                result = _restore(cfg, args)
            else:
                dest, m = read_version(cfg, version)
                if action == 'figure':result = _figure(cfg, args, dest, m)
                elif action == 'stage':result = _stage(cfg, args, dest, m)
                elif action == 'dependency':result = _dependency(cfg, args, dest, m)
                elif action == 'revise':result = _revise(cfg, args, dest, m)
                elif action == 'promote':result = _promote(cfg, args, dest, m)
                elif action == 'activate':result = _activate(cfg, args, dest, m)
                elif action == 'select':
                    _require_draft(m); revision = args.get('revision')
                    _, _, r = _revision(cfg, version, revision)
                    result = {'dry_run': args.get('dry_run', True), 'version': version, 'revision': revision}
                    if not result['dry_run']:
                        m['current_revision'] = revision
                        m['selected_figures'] = {k: v['candidate'] for k, v in r['figures'].items()}
                        m['selected_dependencies'] = r.get('dependency_selections', {})
                        _write_meta(dest, m, args); result.update(status(cfg, version))
        if not args.get('dry_run', True):
            atomic_json(journal, {'schema': 'paper-studio.version-operation.v1', 'project_id': cfg['project_id'],
                'request_id': request_id, 'request_sha256': digest, 'action': action, 'result': result}, True)
        return result


def dispatch(cfg, args):
    """Keep failed operation evidence without changing any success receipt."""
    try:
        return _dispatch(cfg, args)
    except Exception as exc:
        from paper_studio.security.policy import StudioError
        request_id = args.get('request_id') if isinstance(args, dict) else None
        if isinstance(request_id, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', request_id):
            # Never replace the success journal. Distinct failed bodies keep distinct audits.
            body_sha = sha(encoded(args))
            audit = Path(cfg['state_root']) / 'version-operation-errors' / (request_id + '-' + body_sha[:16] + '.json')
            try:
                with locked(cfg, 'lifecycle-error-audit'):
                    if not audit.exists():
                        atomic_json(audit, {'schema': 'paper-studio.version-operation-error.v1',
                            'project_id': cfg['project_id'], 'request_id': request_id,
                            'request_sha256': body_sha, 'action': args.get('action'),
                            'version': args.get('version'), 'dry_run': args.get('dry_run', True),
                            'error': exc.as_dict() if isinstance(exc, StudioError) else
                                {'code': 'VERSION_OPERATION_FAILED', 'message': str(exc)[:1000]}}, True)
            except Exception:
                # Reporting the original error takes priority over a secondary audit I/O failure.
                pass
        raise
