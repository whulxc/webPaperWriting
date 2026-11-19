"""Generate sections only through two project-selected, SHA-pinned templates."""
import difflib
import html
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile

from paper_studio.security.policy import (atomic_json, encoded, fail,
    locked, no_links, relpath, safe_read, sha)

LANGUAGES = ('en', 'zh')
TOKEN = '@@SECTION_BODY@@'


def label(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', value):
        fail('INVALID_ARGUMENT', 'Section and revision identifiers require bounded simple names')
    return value


def structure(source):
    text = re.sub(r'(?<!\\)%[^\n]*', '', source.decode('utf-8'))
    counts = {kind: len(re.findall(r'\\begin\s*\{' + kind + r'\*?\}', text))
        for kind in ['figure', 'table']}
    if any(count > 100 for count in counts.values()):
        fail('INPUT_LIMIT', 'Section structure exceeds the fixed figure/table bound')
    return counts


def policy(cfg, section_id=None):
    value = cfg.get('chapter_workflow')
    if (not isinstance(value, dict) or not {'root','templates'}<=set(value)
            or set(value)-{'root','templates','section_templates'}):
        fail('SECTION_NOT_CONFIGURED', 'Explicit project section templates are required')
    relpath(value['root'])
    overrides=value.get('section_templates',{})
    if not isinstance(overrides,dict) or len(overrides)>16:
        fail('CONFIG_INVALID','Bounded per-section template overrides required')
    for name in overrides:label(name)
    for templates in [value['templates'],*overrides.values()]:
        if not isinstance(templates, dict) or set(templates) != set(LANGUAGES):
            fail('CONFIG_INVALID', 'Both English and Chinese section templates are required')
        for item in templates.values():
            if not isinstance(item, dict) or set(item) != {'path', 'sha256'}:
                fail('CONFIG_INVALID', 'Section templates require path and SHA256')
            raw = safe_read(cfg['project_root'], item['path'], 256 * 1024)
            if sha(raw) != item['sha256'] or raw.decode('utf-8').count(TOKEN) != 1:
                fail('SECTION_TEMPLATE_CHANGED', 'Section template identity or body placeholder differs')
    return {**value,'templates':overrides.get(section_id,value['templates'])}


def read_revision(cfg, section_id, revision):
    value = policy(cfg, section_id)
    relative = '/'.join([value['root'], label(section_id), label(revision)])
    root = no_links(Path(cfg['project_root']) / relative)
    raw = safe_read(root, 'manifest.json', 128 * 1024)
    manifest = json.loads(raw)
    if (manifest.get('project_id') != cfg['project_id'] or manifest.get('section_id') != section_id
            or manifest.get('revision') != revision or manifest.get('schema') != 'paper-studio.section.v1'):
        fail('PROJECT_DENIED', 'Section manifest belongs to a different project or revision')
    for lang in LANGUAGES:
        entry = manifest['languages'][lang]
        for key in ['source', 'body']:
            expected_name = ('main_' if key == 'source' else 'body_') + lang + '.tex'
            if entry[key] != expected_name:
                fail('SECTION_TAMPERED', 'Unexpected section entry point')
            data = safe_read(root, entry[key], 512 * 1024)
            if sha(data) != entry[key + '_sha256']:
                fail('SECTION_TAMPERED', 'Section source no longer matches its immutable manifest')
        template = value['templates'][lang]
        raw_template = safe_read(cfg['project_root'], template['path'], 256 * 1024)
        raw_body = safe_read(root, entry['body'], 256 * 1024)
        generated = raw_template.decode().replace(TOKEN, raw_body.decode()).encode()
        if sha(generated) != entry['source_sha256'] or entry['template_sha256'] != template['sha256']:
            fail('SECTION_TAMPERED', 'Section source differs from its trusted template and body')
        if entry.get('structure', structure(generated)) != structure(generated):
            fail('SECTION_TAMPERED', 'Section structural manifest differs from the generated source')
    return root, manifest, sha(raw)


def validate_config(cfg):
    if cfg.get('chapter_workflow') is not None:
        policy(cfg)


def guard_write(cfg, relative):
    value = cfg.get('chapter_workflow')
    if value and PurePosixPath(relative).is_relative_to(value['root']):
        fail('WRITE_DENIED', 'Section revisions are immutable; stage a new revision')


def trusted_target(cfg, tex_path):
    value = cfg.get('chapter_workflow')
    if not value or not PurePosixPath(tex_path).is_relative_to(value['root']):
        return None
    return target_for(cfg, tex_path)


def _selected_material(cfg, path, expected_sha256, *, new_revision=False):
    from paper_studio.bootstrap.handoff import BASE, INDEX_PATH, MANIFEST_SCHEMA, verify_indexed_handoff
    from paper_studio.bootstrap.workspace import BootstrapError

    root = Path(cfg['project_root'])
    raw = safe_read(root, path, 2 * 1024**2)
    if sha(raw) != expected_sha256:
        fail('EVIDENCE_STALE', 'Section material differs from the requested SHA256')
    try:
        material = json.loads(raw)
    except (ValueError, UnicodeError):
        fail('EVIDENCE_STALE', 'Section material must be a JSON object')
    if not isinstance(material, dict):
        fail('EVIDENCE_STALE', 'Section material must be a JSON object')
    if material.get('project_id') != cfg['project_id']:
        fail('PROJECT_DENIED', 'Section material belongs to another project')
    # Handwritten legacy projects may have only a project-local material manifest.
    # Once Codex records an index, sections must use a verified entry from it.
    if (material.get('schema') == MANIFEST_SCHEMA or PurePosixPath(path).is_relative_to(BASE)
            or (new_revision and (root / INDEX_PATH).exists())):
        try:
            verify_indexed_handoff(root, cfg['project_id'], path, expected_sha256)
        except BootstrapError as error:
            fail(error.code, str(error))
    return material


def provenance_paths(cfg, tex_path):
    """Selected section and handoff snapshots, with all archived identities checked."""
    value = cfg.get('chapter_workflow')
    if not value or not PurePosixPath(tex_path).is_relative_to(value['root']):return []
    parts = PurePosixPath(tex_path).parts
    root, manifest, _ = read_revision(cfg, parts[-3], parts[-2])
    relative = root.relative_to(cfg['project_root']).as_posix()
    paths = [relative + '/manifest.json']
    paths.extend(relative + '/' + item['body'] for item in manifest['languages'].values())
    material_path = manifest['material_manifest']
    material = _selected_material(cfg, material_path, manifest['material_revision'])
    paths.append(material_path)
    if material.get('schema') == 'paper-studio.material-handoff.v1':
        parent = PurePosixPath(material_path).parent
        handoff_path = (parent / 'HANDOFF.md').as_posix()
        if sha(safe_read(cfg['project_root'], handoff_path, 2 * 1024**2)) != material['handoff_sha256']:
            fail('EVIDENCE_STALE', 'Chapter handoff summary changed')
        paths.append(handoff_path)
        for source in material['sources']:
            relpath(source['snapshot_path'])
            source_path = (parent / source['snapshot_path']).as_posix()
            source_raw = safe_read(cfg['project_root'], source_path, 16 * 1024**2)
            if len(source_raw) != source['bytes'] or sha(source_raw) != source['sha256']:
                fail('EVIDENCE_STALE', 'Chapter handoff source snapshot changed')
            paths.append(source_path)
    return sorted(set(paths))


def target_for(cfg, tex_path):
    """Called by the ordinary execution planner after its registered targets."""
    value = policy(cfg)
    parts = PurePosixPath(tex_path).parts
    prefix = PurePosixPath(value['root']).parts
    if parts[:len(prefix)] != prefix or len(parts) != len(prefix) + 3:
        fail('BUILD_TARGET_DENIED', 'Section entry is outside the configured chapter root')
    section_id, revision, name = parts[-3:]
    if name not in {'main_en.tex', 'main_zh.tex'}:
        fail('BUILD_TARGET_DENIED', 'Only verified bilingual section entry points may compile')
    root, manifest, _ = read_revision(cfg, section_id, revision)
    entry = manifest['languages']['en' if name == 'main_en.tex' else 'zh']
    counts = entry.get('structure') or structure(safe_read(root, entry['source'], 512 * 1024))
    return 'section_' + section_id, {'tex_path': tex_path, 'working_directory': '.',
        'tex_search_paths': [], 'build_profile': 'xelatex', 'bibliography_engine': 'bibtex',
        'use_latexmk': False, 'expected_figures': counts['figure'], 'expected_tables': counts['table']}


def stage(cfg, args):
    section_id, revision = label(args.get('section_id')), label(args.get('revision'))
    value = policy(cfg, section_id)
    bodies = args.get('bodies')
    if not isinstance(bodies, dict) or set(bodies) != set(LANGUAGES):
        fail('INVALID_ARGUMENT', 'A section revision requires both languages of the same scope')
    if any(not isinstance(s, str) or not s.strip() or len(s.encode()) > 256 * 1024 for s in bodies.values()):
        fail('INPUT_LIMIT', 'Section bodies must be nonempty bounded UTF-8 text')
    expected = args.get('expected_previous_sha256')
    if not isinstance(expected, str) or (expected != 'absent' and not re.fullmatch('[a-f0-9]{64}', expected)):
        fail('REVISION_REQUIRED', 'Expected previous section manifest SHA256 or absent is required')
    scope = args.get('scope', section_id)
    if not isinstance(scope, str) or not scope.strip() or len(scope.encode()) > 1024:
        fail('INVALID_ARGUMENT', 'Section scope requires a bounded nonempty label')
    material_path = args.get('material_manifest')
    material_revision = args.get('material_revision')
    base_relative = value['root'] + '/' + section_id
    dest_relative = base_relative + '/' + revision
    if not any(PurePosixPath(dest_relative).is_relative_to(root) for root in cfg.get('writable_roots', [])):
        fail('WRITE_DENIED', 'Section archive root requires explicit project write scope')
    base = no_links(Path(cfg['project_root']) / base_relative)
    dest = base / revision
    languages = {}; files = {}
    for lang in LANGUAGES:
        template = value['templates'][lang]
        raw = safe_read(cfg['project_root'], template['path'], 256 * 1024)
        source = raw.decode('utf-8').replace(TOKEN, bodies[lang]).encode('utf-8')
        entry = {'source': 'main_' + lang + '.tex', 'source_sha256': sha(source),
            'body': 'body_' + lang + '.tex', 'body_sha256': sha(bodies[lang].encode()),
            'template_sha256': template['sha256'], 'structure': structure(source)}
        languages[lang] = entry
        files[entry['source']] = source
        files[entry['body']] = bodies[lang].encode()
    manifest = {'schema': 'paper-studio.section.v1', 'project_id': cfg['project_id'],
        'section_id': section_id, 'revision': revision, 'scope': scope,
        'previous_manifest_sha256': expected, 'material_manifest': material_path,
        'material_revision': material_revision, 'languages': languages}
    identity = sha(encoded(manifest))
    result = {**manifest, 'manifest_sha256': identity,
        'tex_paths': {lang: dest_relative + '/' + entry['source'] for lang, entry in languages.items()},
        'dry_run': args.get('dry_run', True)}
    if type(result['dry_run']) is not bool:
        fail('INVALID_ARGUMENT', 'dry_run must be boolean')
    with locked(cfg, 'section'):
        _selected_material(cfg, material_path, material_revision, new_revision=not dest.exists())
        latest = base / 'LATEST.json'
        pointer = json.loads(safe_read(base, 'LATEST.json', 16384)) if latest.exists() else None
        current = pointer['manifest_sha256'] if pointer else 'absent'
        if dest.exists():
            _, previous, old_sha = read_revision(cfg, section_id, revision)
            if old_sha != identity:
                fail('IDEMPOTENCY_CONFLICT', 'Existing section revision has different content')
            if current == identity:
                return {**result, 'reused': True}
        if current != expected:
            fail('REVISION_CONFLICT', 'Section baseline changed', actual_revision=current)
        if not result['dry_run']:
            base.mkdir(parents=True, exist_ok=True)
            if not dest.exists():
                temporary = Path(tempfile.mkdtemp(prefix='.section-', dir=base))
                try:
                    for name, raw in files.items():
                        (temporary / name).write_bytes(raw)
                    atomic_json(temporary / 'manifest.json', manifest, True)
                    temporary.rename(dest)
                finally:
                    if temporary.exists():shutil.rmtree(temporary)
            atomic_json(latest, {'revision': revision, 'manifest_sha256': identity})
    return result


def compare(cfg, args):
    section_id = label(args.get('section_id'))
    left, lm, ls = read_revision(cfg, section_id, args.get('left_revision'))
    right, rm, rs = read_revision(cfg, section_id, args.get('right_revision'))
    if lm['scope'] != rm['scope']:
        fail('SECTION_SCOPE_CONFLICT', 'Compared sections must cover the same scope')
    changes = {}
    tables = []
    for lang in LANGUAGES:
        before = safe_read(left, lm['languages'][lang]['body'], 256 * 1024).decode().splitlines()
        after = safe_read(right, rm['languages'][lang]['body'], 256 * 1024).decode().splitlines()
        changes[lang] = '\n'.join(difflib.unified_diff(before, after,
            fromfile=args['left_revision'] + '/' + lang, tofile=args['right_revision'] + '/' + lang))
        tables.append('<h2>' + lang.upper() + '</h2>' + difflib.HtmlDiff(wrapcolumn=90).make_table(
            before, after, fromdesc=html.escape(args['left_revision']), todesc=html.escape(args['right_revision'])))
    document = ('<!doctype html><html lang="zh"><meta charset="utf-8"><title>章节修订对照</title>'
        '<style>body{font-family:system-ui;margin:2rem}table{width:100%;border-collapse:collapse}'
        'td{padding:.2rem;white-space:pre-wrap}.diff_add{background:#d7f3da}'
        '.diff_sub{background:#ffe0e0}.diff_chg{background:#fff1ad}</style>'
        '<h1>' + html.escape(lm['scope']) + '</h1>' + ''.join(tables) + '</html>')
    return {'project_id': cfg['project_id'], 'section_id': section_id, 'scope': lm['scope'],
        'left_manifest_sha256': ls, 'right_manifest_sha256': rs,
        'material_changed': lm['material_revision'] != rm['material_revision'],
        'diffs': changes, 'html': document, 'html_sha256': sha(document.encode())}


def dispatch(cfg, args):
    if args.get('project_id', cfg['project_id']) != cfg['project_id']:
        fail('PROJECT_DENIED', 'Section request project differs from selected binding')
    if args.get('action') == 'stage':return stage(cfg, args)
    if args.get('action') == 'compare':return compare(cfg, args)
    fail('INVALID_ARGUMENT', 'Section actions are stage or compare')
