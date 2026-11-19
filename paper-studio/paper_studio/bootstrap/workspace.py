"""Project-owned indexes with hash-guarded incremental updates.

No source is copied, executed, accepted as a scientific fact or sent to a cloud.
Existing PROJECT/HANDOFF/STATE and manually changed indexes are retained.
The only mutable namespace in sync/manifest.json is ``bootstrap``. Service
registration, actual permissions, Zotero inspection and Drive sync are separate.
"""
from __future__ import annotations

import contextlib
import csv
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tempfile

CONTRACT = 'paper-editor.project.v1'
MANIFEST_SCHEMA = 'paper-editor.sync-manifest.v1'
SKIP_DIRS = {'.git', '.hg', '.svn', '.ssh', '.config', '.venv', 'venv', '__pycache__',
             'node_modules', '.mypy_cache', '.pytest_cache', 'checkpoints', 'credentials',
             'secrets', '.secrets', 'cache', '.cache'}
SKIP_SUFFIXES = {'.pem', '.key', '.p12', '.pfx', '.sqlite', '.sqlite3', '.db', '.pyc'}
TEXT_LIMIT = 2 * 1024 * 1024
METADATA_LIMIT = 32 * 1024 * 1024
FIELDS = ['evidence_id', 'source_root', 'source_path', 'bytes', 'sha256', 'role',
          'validation_status', 'scientific_status', 'claim', 'statistics_scope', 'source_version']


class BootstrapError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def fail(code, message):
    raise BootstrapError(code, message)


def encoded(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def no_links(value):
    path = Path(value)
    if not path.is_absolute() or path != Path(os.path.abspath(path)):
        fail('BOOTSTRAP_PATH_INVALID', 'Use canonical absolute paths without parent traversal')
    for part in [*reversed(path.parents), path]:
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            fail('BOOTSTRAP_LINK_DENIED', 'Project and output paths cannot traverse links or reparse points')
    return path


def relative(value):
    if not isinstance(value, str) or not value or '\\' in value or '\x00' in value:
        fail('BOOTSTRAP_PATH_INVALID', 'A safe project-relative path is required')
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in {'', '.', '..'} for p in value.split('/')) or ':' in value:
        fail('BOOTSTRAP_PATH_INVALID', 'Relative paths cannot escape their root')
    return path.as_posix()


def _read(path, limit=METADATA_LIMIT):
    no_links(path)
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0))
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            fail('BOOTSTRAP_FILE_INVALID', 'Only regular files with one link are supported')
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        fail('BOOTSTRAP_FILE_LIMIT', 'A bounded metadata file exceeds its size limit')
    return raw


def _json(path):
    try:
        value = json.loads(_read(path))
    except (ValueError, UnicodeError):
        fail('BOOTSTRAP_METADATA_INVALID', 'An existing metadata file is not valid UTF-8 JSON')
    if not isinstance(value, dict):
        fail('BOOTSTRAP_METADATA_INVALID', 'An existing metadata file must contain an object')
    return value


def _atomic(path, raw, exclusive=False):
    no_links(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    no_links(path.parent)
    fd, temp_name = tempfile.mkstemp(prefix='.paper-editor-', dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        no_links(path)
        if exclusive:
            if os.name == 'nt':
                os.rename(temp, path)
            else:
                os.link(temp, path)
                temp.unlink()
        else:
            os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


@contextlib.contextmanager
def _lock(path):
    no_links(path)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        fail('BOOTSTRAP_BUSY', 'An initialization lock exists; inspect any interrupted operation before retrying')
    try:
        os.close(fd)
        yield
    finally:
        path.unlink()


def _project_metadata(path):
    """Read only explicit project mappings; never execute YAML constructors."""
    if not path.exists():
        return {}
    raw = _read(path)
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        try:
            import yaml
            value = yaml.safe_load(raw)
        except ImportError:
            # Dependency-free fallback for the ordinary scalar mappings used by
            # existing PaperWrite projects; complex YAML requires explicit args.
            text = raw.decode('utf-8')
            value = {}
            for name in ['project_id', 'workspace_version', 'current_manuscript_version']:
                match = re.search(r'^' + name + r':\s*([A-Za-z0-9_.-]+)\s*$', text, re.M)
                if match:
                    value[name] = match.group(1)
            match = re.search(r'^  manuscript:\s*([A-Za-z0-9_./-]+)\s*$', text, re.M)
            if match:
                value['paths'] = {'manuscript': match.group(1)}
        except Exception:
            fail('BOOTSTRAP_PROJECT_INVALID', 'Existing PROJECT.yaml cannot be safely interpreted; supply an explicit mapping')
    if not isinstance(value, dict):
        fail('BOOTSTRAP_PROJECT_INVALID', 'PROJECT.yaml must describe a project object')
    return value


def _git(root):
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_TERMINAL_PROMPT='0', GIT_OPTIONAL_LOCKS='0', GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)
    def run(*args):
        try:
            result = subprocess.run(['git', '-C', str(root), *args], capture_output=True, timeout=5, env=env, shell=False)
            return result.stdout if result.returncode == 0 and len(result.stdout) < 1024 * 1024 else None
        except (OSError, subprocess.TimeoutExpired):
            return None
    head = run('rev-parse', '--verify', 'HEAD')
    changes = run('status', '--porcelain=v1', '-z', '--untracked-files=no')
    return {'head': head.decode('ascii').strip() if head and re.fullmatch(rb'[0-9a-f]{40,64}\s*', head) else None,
            'tracked_worktree_dirty': bool(changes) if changes is not None else None,
            'tracked_status_sha256': digest(changes) if changes is not None else None,
            'untracked_sources': 'identified_by_material_inventory', 'scientific_acceptance': 'not_assessed'}


def _private_name(name):
    lower = name.casefold()
    return (lower in SKIP_DIRS or lower.startswith(('.env', '.paper-editor-'))
            or any(token in lower for token in ['credential', 'secret', 'cookie', 'oauth', 'access_token', 'refresh_token'])
            or Path(lower).suffix in SKIP_SUFFIXES)


def _role(path):
    parts = set(Path(path).parts)
    lower = path.casefold()
    if 'fixture' in lower or 'synthetic' in lower:
        return 'synthetic_fixture_candidate'
    if Path(path).suffix.lower() == '.bib':
        return 'bibliography_source'
    if Path(path).suffix.lower() in {'.tex', '.sty', '.cls'}:
        return 'manuscript_source'
    if Path(path).suffix.lower() in {'.svg', '.png', '.jpg', '.pdf', '.pptx', '.tikz'}:
        return 'figure_or_document_source'
    if any(token in lower for token in ['ablation', 'baseline', 'result', 'experiment', 'metric']):
        return 'experimental_material_candidate'
    if parts & {'data', 'dataset', 'datasets', 'raw'}:
        return 'dataset_material_candidate'
    if Path(path).suffix.lower() in {'.py', '.m', '.r', '.cpp', '.java', '.kt'}:
        return 'method_implementation_candidate'
    return 'project_material'


def _file_record(root, rel, root_name, max_hash_bytes):
    path = root / rel
    no_links(path)
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0))
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            fail('BOOTSTRAP_FILE_INVALID', 'Only regular files with one link are inventoried')
        value = None
        status = 'not_hashed_size_limit'
        if before.st_size <= max_hash_bytes:
            hasher = hashlib.sha256()
            while True:
                block = stream.read(1024 * 1024)
                if not block:
                    break
                hasher.update(block)
            after = os.fstat(stream.fileno())
            attrs = ['st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns']
            if any(getattr(before, k) != getattr(after, k) for k in attrs):
                fail('BOOTSTRAP_SOURCE_CHANGED', 'A material changed during hashing; retry once the source is stable')
            value, status = hasher.hexdigest(), 'verified_inventory'
    return {'source_root': root_name, 'source_path': rel, 'bytes': before.st_size, 'sha256': value,
            'role': _role(rel), 'validation_status': status, 'scientific_status': 'unverified',
            'claim': None, 'statistics_scope': None}


def _inventory(root, root_name, *, excluded, selections, max_files, max_hash_bytes):
    records, gaps = [], []
    selected = [root / relative(item) for item in selections] if selections else [root]
    seen = set()
    for start in selected:
        no_links(start)
        if any(_private_name(part) for part in start.relative_to(root).parts):
            gaps.append({'code': 'SOURCE_PRIVATE_PATH_EXCLUDED', 'source_root': root_name})
            continue
        if not start.exists():
            gaps.append({'code': 'SOURCE_MISSING', 'source_root': root_name, 'source_path': start.relative_to(root).as_posix()})
            continue
        candidates = []
        if start.is_file():
            candidates = [start]
        else:
            for parent, dirs, files in os.walk(start, followlinks=False):
                base = Path(parent)
                kept = []
                for name in sorted(dirs):
                    directory = base / name
                    if _private_name(name) or directory in excluded:
                        continue
                    try:
                        no_links(directory)
                    except (BootstrapError, OSError):
                        gaps.append({'code': 'SOURCE_DIRECTORY_LINK_OR_UNREADABLE', 'source_root': root_name,
                                     'source_path': directory.relative_to(root).as_posix()})
                        continue
                    kept.append(name)
                dirs[:] = kept
                candidates.extend(base / name for name in sorted(files) if not _private_name(name))
                if len(candidates) > max_files:
                    break
        for path in candidates:
            if path in seen or any(_private_name(part) for part in path.relative_to(root).parts) or any(path == e or e in path.parents for e in excluded):
                continue
            seen.add(path)
            if len(records) >= max_files:
                gaps.append({'code': 'INVENTORY_FILE_LIMIT', 'source_root': root_name})
                return records, gaps
            rel = path.relative_to(root).as_posix()
            try:
                record = _file_record(root, rel, root_name, max_hash_bytes)
            except (BootstrapError, OSError):
                gaps.append({'code': 'SOURCE_UNREADABLE_OR_UNSTABLE', 'source_root': root_name, 'source_path': rel})
                continue
            records.append(record)
            if record['sha256'] is None:
                gaps.append({'code': 'SOURCE_HASH_SIZE_LIMIT', 'source_root': root_name, 'source_path': rel})
    return sorted(records, key=lambda r: r['source_path']), gaps


def _references(records, roots):
    entries, citations, gaps = [], [], []
    for row in records:
        rel, namespace = row['source_path'], row['source_root']
        if Path(rel).suffix.lower() not in {'.bib', '.tex'} or not row['sha256']:
            continue
        if row['bytes'] > TEXT_LIMIT:
            gaps.append({'code': 'REFERENCE_PARSE_SIZE_LIMIT', 'source_root': namespace, 'source_path': rel})
            continue
        raw = _read(roots[namespace] / rel, TEXT_LIMIT)
        if digest(raw) != row['sha256']:
            fail('BOOTSTRAP_SOURCE_CHANGED', 'A citation source changed after inventory; nothing is accepted')
        try:
            text = raw.decode('utf-8')
        except UnicodeError:
            gaps.append({'code': 'REFERENCE_ENCODING_UNSUPPORTED', 'source_root': namespace, 'source_path': rel})
            continue
        if rel.lower().endswith('.tex'):
            # Static literal calls only; no macro/dependency expansion or execution.
            # This tiny lexical operation stays portable on native Windows,
            # where the Gateway's existing fcntl-dependent module is unavailable.
            def uncomment(value):
                lines = []
                for line in value.splitlines():
                    cut = len(line)
                    for position, char in enumerate(line):
                        if char != '%':
                            continue
                        previous = position - 1
                        while previous >= 0 and line[previous] == '\\':
                            previous -= 1
                        if (position - 1 - previous) % 2 == 0:
                            cut = position
                            break
                    lines.append(line[:cut])
                return '\n'.join(lines)
            for match in re.finditer(r'\\(?:cite\w*|nocite)\*?(?:\[[^\]]*\])*\{([^{}]+)\}', uncomment(text)):
                for key in match.group(1).split(','):
                    if key.strip() != '*':
                        citations.append({'project_citation_key': key.strip(), 'source_root': namespace,
                                          'source_path': rel, 'source_sha256': row['sha256']})
        else:
            try:
                from paper_studio.references.zotero_bibtex import parse_bibtex, doi_value
                parsed = parse_bibtex(text)
            except Exception:
                gaps.append({'code': 'BIBTEX_PARSE_UNSUPPORTED', 'source_root': namespace, 'source_path': rel})
                continue
            for entry in parsed:
                doi = doi_value(entry['fields'].get('doi', '')) or None
                metadata = {key: value for key, value in entry['fields'].items()
                            if key in {'title', 'author', 'year', 'journal', 'volume', 'number', 'pages'}}
                entries.append({'project_citation_key': entry['key'], 'doi': doi, 'zotero_item_key': None,
                                'zotero_collection_key': None, 'library_citation_keys': [],
                                'metadata': metadata,
                                'source_root': namespace, 'source_path': rel, 'source_sha256': row['sha256'],
                                'metadata_status': 'parsed_local_unverified', 'attachment_available': None,
                                'zotero_verified': False, 'claim_support': 'unverified'})
    by_key, by_doi = {}, {}
    for row in entries:
        by_key.setdefault(row['project_citation_key'], []).append(row)
        if row['doi']:
            by_doi.setdefault(row['doi'], []).append(row['project_citation_key'])
    for key, rows in by_key.items():
        if len(rows) > 1:
            gaps.append({'code': 'CITATION_KEY_DUPLICATE', 'project_citation_key': key,
                         'metadata_conflict': len({digest(encoded([row['doi'], row['metadata']])) for row in rows}) > 1})
    for doi, keys in by_doi.items():
        if len(keys) > 1:
            gaps.append({'code': 'DOI_DUPLICATE', 'doi': doi, 'project_citation_keys': sorted(set(keys))})
    for row in citations:
        if row['project_citation_key'] not in by_key:
            gaps.append({'code': 'CITATION_NOT_INDEXED', **row})
    return {'schema': 'paper-editor.reference-index.v1', 'entries': entries, 'citations': citations,
            'verification': 'local_parse_only_no_zotero_or_fulltext_access'}, gaps


def _registry(path, project_id):
    if path is None:
        return {'status': 'not_supplied', 'unbound_available': [], 'bound': [], 'offline_bound': [], 'current_project': [], 'unknown': []}
    value = _json(no_links(Path(path)))
    rows = value.get('tunnels', [])
    if not isinstance(rows, list) or len(rows) > 64:
        fail('BOOTSTRAP_REGISTRY_INVALID', 'Tunnel registry must contain a bounded tunnel list')
    output = {'status': 'local_registry_only_not_live_verified', 'unbound_available': [], 'bound': [], 'offline_bound': [], 'current_project': [], 'unknown': []}
    for row in rows:
        if not isinstance(row, dict):
            continue
        registrations = row.get('registered_projects', [])
        projects = [p['project_id'] for p in registrations if isinstance(p, dict) and isinstance(p.get('project_id'), str)] if isinstance(registrations, list) else []
        aliases = row.get('aliases', [])
        alias_names = [a['alias'] for a in aliases if isinstance(a, dict) and isinstance(a.get('alias'), str)] if isinstance(aliases, list) else []
        clean = {'tunnel_id': row.get('tunnel_id'), 'platform_name': row.get('platform_name'),
                 'aliases': alias_names, 'state': row.get('state'), 'project_ids': projects,
                 'binding_state': row.get('binding_state')}
        if row.get('binding_state') == 'bound' or projects:
            output['bound'].append(clean)
            if row.get('state') != 'online':
                output['offline_bound'].append(clean)
        elif (row.get('binding_state') == 'unbound' and row.get('unbound_verified') is True
              and row.get('state') == 'offline'
              and row.get('shared_key_permission', {}).get('use_permission') == 'VERIFIED_READONLY_INTROSPECTION'):
            output['unbound_available'].append(clean)
        else:
            output['unknown'].append(clean)
        if project_id in projects:
            output['current_project'].append(clean)
    return output


def initialize(project_root, project_id, paper_root=None, project_name=None, manuscript_dir=None,
               sources=None, tunnel_registry=None, target_venue=None, max_files=10000,
               max_hash_bytes=256 * 1024 * 1024, gateway_config=None):
    """Initialize/update only hash-owned indexes; existing human files stay intact."""
    if not isinstance(project_id, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,95}', project_id):
        fail('BOOTSTRAP_ID_INVALID', 'A bounded lowercase project_id is required')
    if type(max_files) is not int or not 1 <= max_files <= 100000 or type(max_hash_bytes) is not int or max_hash_bytes < 1:
        fail('BOOTSTRAP_LIMIT_INVALID', 'Positive bounded file and hash limits are required')
    root = no_links(Path(project_root))
    if not root.is_dir():
        fail('BOOTSTRAP_PROJECT_MISSING', 'The original project directory does not exist')
    if paper_root is None:
        writing, legacy = root / 'PaperWriting', root / 'PaperWrite'
        if writing.exists() and legacy.exists():
            fail('BOOTSTRAP_MAPPING_REQUIRED', 'Both PaperWriting and PaperWrite exist; specify --paper-root')
        paper = legacy if legacy.exists() else writing
    else:
        paper = no_links(Path(paper_root))
    no_links(paper)
    if paper == root or paper in root.parents:
        fail('BOOTSTRAP_PATH_INVALID', 'Paper workspace must be distinct from and not contain the research root')
    metadata = _project_metadata(paper / 'PROJECT.yaml')
    if metadata.get('project_id') not in {None, project_id}:
        fail('BOOTSTRAP_PROJECT_MISMATCH', 'Existing PROJECT.yaml belongs to a different project_id')
    previous_manifest_path = paper / 'sync/manifest.json'
    old_manifest = _json(previous_manifest_path) if previous_manifest_path.exists() else {}
    old = old_manifest.get('bootstrap', {})
    if not isinstance(old, dict):
        fail('BOOTSTRAP_METADATA_INVALID', 'Existing bootstrap manifest namespace must be an object')
    if old and (old.get('project_id') != project_id or old.get('original_project_root') != str(root) or old.get('paper_writing_root') != str(paper)):
        fail('BOOTSTRAP_PROJECT_MISMATCH', 'Existing bootstrap manifest belongs to a different project or path mapping')
    existing_paths = metadata.get('logical_paths') or metadata.get('paths') or {}
    if not isinstance(existing_paths, dict):
        fail('BOOTSTRAP_PROJECT_INVALID', 'Project path mapping must be an object')
    existing_work = existing_paths.get('manuscript')
    version = metadata.get('current_manuscript_version') or metadata.get('workspace_version')
    work = manuscript_dir or existing_work or old.get('manuscript_dir') or version or 'manuscript'
    work = relative(work)
    if existing_work and manuscript_dir and manuscript_dir != existing_work:
        fail('BOOTSTRAP_MAPPING_CONFLICT', 'Retain the declared current manuscript path; version derivation is a separate workflow')
    if work in {'materials', 'builds', 'sync', 'references', 'figures', 'reviews'} or work.startswith(('materials/', 'builds/', 'sync/')):
        fail('BOOTSTRAP_MAPPING_INVALID', 'Manuscript cannot map into protected or management directories')
    if old.get('manuscript_dir') and work != old['manuscript_dir']:
        fail('BOOTSTRAP_MAPPING_CONFLICT', 'Existing manuscript mapping is retained; version changes require a separate explicit workflow')
    if metadata.get('project_id') and not existing_work and not version and not manuscript_dir:
        fail('BOOTSTRAP_MAPPING_REQUIRED', 'Existing project needs an explicit current manuscript mapping')
    is_legacy = bool(metadata and metadata.get('schema') != CONTRACT)
    logical = {'materials': 'materials', 'manuscript': work, 'references': 'references',
               'figures': 'figures', 'reviews': 'reviews', 'builds': 'builds'}
    for key in ['materials', 'references', 'figures', 'reviews', 'builds']:
        if key in existing_paths:
            logical[key] = relative(existing_paths[key])
    if logical['materials'] != 'materials':
        fail('BOOTSTRAP_MAPPING_INVALID', 'A custom materials root requires an explicit migration rather than inferred writes')
    logical.update(evidence_registry='materials/evidence_registry.csv',
                   reference_index=logical['references'] + '/reference_index.json', manifest='sync/manifest.json')
    protected_builds = logical['builds']
    if not protected_builds.endswith('/builds') and protected_builds != 'builds':
        fail('BOOTSTRAP_MAPPING_INVALID', 'Existing build mapping must identify a dedicated builds directory')
    for key in ['references', 'figures', 'reviews']:
        value = logical[key]
        if any(value == p or value.startswith(p + '/') for p in ['materials', 'sync', 'builds', protected_builds, work + '/builds']):
            fail('BOOTSTRAP_MAPPING_INVALID', 'Editable logical paths cannot map into protected materials or build history')
    state_path = paper / 'PROJECT_STATE.json'
    if state_path.exists() and _json(state_path).get('project_id') != project_id:
        fail('BOOTSTRAP_PROJECT_MISMATCH', 'Existing PROJECT_STATE.json belongs to a different project_id')
    for item in sources or []:
        relative(item)
    if project_name is not None and (not isinstance(project_name, str) or not 1 <= len(project_name) <= 200 or any(ord(c) < 32 for c in project_name)):
        fail('BOOTSTRAP_NAME_INVALID', 'Use a bounded printable project name')
    if target_venue is not None and (not isinstance(target_venue,str) or not 1<=len(target_venue)<=300 or any(ord(c)<32 for c in target_venue)):
        fail('BOOTSTRAP_VENUE_INVALID','Use a bounded printable target venue')
    registry = _registry(tunnel_registry, project_id)
    from .zotero_index import prepare, enrich
    zotero_reader = prepare(gateway_config, project_id, paper) if gateway_config is not None else None
    paper.mkdir(parents=True, exist_ok=True)
    with _lock(paper / '.paper-editor-bootstrap.lock'):
        # Revalidate under the output lock; a concurrent first invocation cannot
        # replace the other invocation's newly established ownership manifest.
        if previous_manifest_path.exists() and _json(previous_manifest_path) != old_manifest:
            fail('BOOTSTRAP_CONCURRENT_CHANGE', 'Bootstrap metadata changed while waiting for initialization')
        dirs = ['materials/methods', 'materials/experiments', 'materials/figures', work,
                logical['references'], logical['figures'], logical['reviews'], logical['builds'], 'sync']
        for rel in dirs:
            directory = no_links(paper / relative(rel))
            directory.mkdir(parents=True, exist_ok=True)
        managed = dict(old.get('managed_files', {}))
        excluded = {paper, root / '.paper-editor-bootstrap.lock'}
        records, gaps = _inventory(root, 'original_project', excluded=excluded, selections=sources,
                                  max_files=max_files, max_hash_bytes=max_hash_bytes)
        generated = {'PROJECT.yaml', 'HANDOFF.md', 'PROJECT_STATE.json', 'materials/evidence_registry.csv',
                     logical['reference_index'], '.paper-editor-bootstrap.lock'}
        generated.update(managed)
        paper_excluded = {paper / rel for rel in generated} | {paper / 'sync', paper / 'builds', paper / logical['builds'], paper / work / 'builds'}
        paper_records, paper_gaps = _inventory(paper, 'paper_writing', excluded=paper_excluded, selections=None,
                                             max_files=max_files, max_hash_bytes=max_hash_bytes)
        records += paper_records
        gaps += paper_gaps
        git = _git(root)
        for row in records:
            identity = row['source_root'] + ':' + row['source_path']
            row['evidence_id'] = 'SRC-' + digest(identity.encode())[:16]
            row['source_version'] = git['head'] if row['source_root'] == 'original_project' else None
        reference_index, ref_gaps = _references(records, {'original_project': root, 'paper_writing': paper})
        gaps += ref_gaps
        gaps += enrich(reference_index, zotero_reader)
        if not any(row['role'] == 'experimental_material_candidate' for row in records):
            gaps.append({'code': 'EXPERIMENTAL_EVIDENCE_NOT_IDENTIFIED'})
        gaps += [{'code': 'SCIENTIFIC_CLAIMS_REQUIRE_HUMAN_VERIFICATION'},
                 {'code': 'TUNNEL_REGISTRATION_NOT_PERFORMED'}, {'code': 'DRIVE_BINDING_NOT_VERIFIED'}]
        contract = {'schema': CONTRACT, 'project_name': project_name or metadata.get('project_name') or root.name,
                    'project_slug': project_id, 'project_id': project_id, 'original_project_root': str(root),
                    'paper_writing_root': str(paper), 'tunnel_id': None, 'tunnel_alias': None,
                    'gateway_profile': None, 'runner': None, 'target_venue': target_venue if target_venue is not None else metadata.get('target_venue'),
                    'current_manuscript_version': version or 'v0.01',
                    'writable_scopes': list(dict.fromkeys([work, logical['references'], logical['figures'], logical['reviews']])),
                    'readonly_scopes': list(dict.fromkeys(['PROJECT.yaml', 'HANDOFF.md', 'materials', 'builds', logical['builds'], work + '/builds'])),
                    'zotero_library_id': None, 'zotero_collection_key': None,
                    'drive_project_folder_id': None, 'drive_knowledge_root_id': None,
                    'skill_contract_version': CONTRACT, 'policy_enforcement_status': 'not_registered',
                    'logical_paths': logical}
        # Retain direct, manually declared service identifiers without claiming
        # that an old PROJECT file proves a live connection or current grant.
        for key in ['tunnel_id', 'tunnel_alias', 'gateway_profile', 'runner', 'zotero_library_id',
                    'zotero_collection_key', 'drive_project_folder_id', 'drive_knowledge_root_id']:
            if key in metadata:
                contract[key] = metadata[key]
        if is_legacy:
            contract['policy_enforcement_status'] = 'not_verified_by_bootstrap'
        contract['binding_verification'] = 'not_performed'
        created, updated, preserved = [], [], []

        def write(rel, raw, human=False):
            destination = no_links(paper / relative(rel))
            if destination.exists():
                current = _read(destination)
                if current == raw:
                    if not human:
                        managed[rel] = digest(raw)
                    return
                if human or managed.get(rel) != digest(current):
                    preserved.append(rel)
                    if not human:
                        gaps.append({'code': 'MANUAL_INDEX_PRESERVED', 'path': rel})
                    return
                _atomic(destination, raw)
                updated.append(rel)
            else:
                _atomic(destination, raw, exclusive=True)
                created.append(rel)
            if not human:
                managed[rel] = digest(raw)

        # PROJECT.yaml uses JSON syntax, a valid YAML subset; no YAML dependency
        # is needed to consume newly initialized contracts.
        write('PROJECT.yaml', encoded(contract), human=True)
        if is_legacy:
            write('sync/paper-editor-map.json', encoded(contract))
        state = {'schema': 'paper-studio.research-state.v1', 'project_id': project_id, 'workdir': work,
                 'current_task': 'Review the real material inventory and resolve missing evidence before paper writing',
                 'accepted_fact_sources': [], 'frozen_result_sources': [], 'completed_builds': [],
                 'issues': ['Material inventory is not scientific acceptance', 'Service identity and write scopes require actual registration'],
                 'deliverables': [], 'last_acceptance': None}
        write('PROJECT_STATE.json', encoded(state), human=True)
        handoff = ('# Paper Editor 项目交接\n\n'
                   f'- project_id: `{project_id}`\n- original_project_root: `{root}`\n'
                   f'- paper_writing_root: `{paper}`\n- 当前编辑路径: `{work}`\n\n'
                   '真实来源、SHA256、Git 状态和缺口见 `sync/manifest.json` 的 `bootstrap` 字段；'
                   f'书目索引见 `{logical["reference_index"]}`，材料索引见 `materials/evidence_registry.csv`。\n\n'
                   '后续实验以材料版本增量交接：先读 `sync/handoff-index.json`（存在时）及其 latest 指向的'
                   '`materials/handoffs/<change_id>/HANDOFF.md`，再按受影响主张、图件和章节接续；不要覆盖人工状态。\n\n'
                   '本次只验证文件清单与哈希。代码、结果候选文件、fixture 标签均不构成已验证科研结论；'
                   '每条结论仍须人工核对原始来源、统计口径及冻结协议。大文件只引用，未复制任何原材料。\n\n'
                   'Tunnel、Runner、Drive、Zotero 未注册或授权，不据此声称权限已经生效。'
                   '写作前用实际 MCP 返回核对项目身份。不得覆盖历史 builds 或派生新论文版本。\n')
        write('HANDOFF.md', handoff.encode('utf-8'), human=True)
        output = io.StringIO(newline='')
        writer = csv.DictWriter(output, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows({key: row.get(key) for key in FIELDS} for row in records)
        write('materials/evidence_registry.csv', output.getvalue().encode('utf-8'))
        write(logical['reference_index'], encoded(reference_index))
        bootstrap = {'schema': 'paper-editor.bootstrap.v1', 'project_id': project_id,
                     'original_project_root': str(root), 'paper_writing_root': str(paper), 'manuscript_dir': work,
                     'git': git, 'sources': records, 'gaps': gaps, 'managed_files': managed,
                     'source_selection': sources or [], 'scan_revision': digest(encoded(records)),
                     'contract_path': 'sync/paper-editor-map.json' if is_legacy else 'PROJECT.yaml',
                     'zotero': reference_index['zotero'],
                     'source_copy_count': 0, 'scientific_acceptance': 'not_assessed'}
        manifest = dict(old_manifest)
        manifest.setdefault('schema', MANIFEST_SCHEMA)
        manifest['bootstrap'] = bootstrap
        raw = encoded(manifest)
        if not previous_manifest_path.exists():
            _atomic(previous_manifest_path, raw, exclusive=True)
            created.append('sync/manifest.json')
        elif _read(previous_manifest_path) != raw:
            _atomic(previous_manifest_path, raw)
            updated.append('sync/manifest.json')
        return {'project_id': project_id, 'paper_writing_root': str(paper), 'manuscript_dir': work,
                'contract_path': bootstrap['contract_path'], 'manifest_path': str(previous_manifest_path),
                'created': created, 'updated': updated, 'preserved': preserved, 'source_count': len(records),
                'reference_count': len(reference_index['entries']), 'gaps': gaps, 'tunnels': registry,
                'zotero': reference_index['zotero'],
                'registration_performed': False, 'cloud_operations': 0, 'source_copy_count': 0,
                'scientific_acceptance': 'not_assessed'}
