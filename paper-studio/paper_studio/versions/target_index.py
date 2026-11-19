"""Project-bound incremental revisions and exact, protected artifact lookup.

This index is independent of legacy builds, versions and CURRENT. It consumes
only explicitly configured baseline members or files in configured intake roots.
Objects are immutable copies; the public resolver never returns filesystem paths.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import time
import uuid

from paper_studio.security.policy import StudioError, encoded, fail, no_links, relpath, safe_read, sha
from paper_studio.versions.target_index_tree import (
    SelectionTree, SelectionForest, FORMAT as SELECTION_FORMAT, FOREST_FORMAT,
    NAMED_FORMAT,
)

SCHEMA = 'paper-studio.target-index.v1'
ROLES = frozenset({'manuscript_pdf', 'manuscript_tex', 'figure_pdf', 'figure_source', 'review', 'provenance',
                   'table_pdf', 'table_source', 'section_tex', 'bibliography', 'named_source'})
FIGURE_ROLES = frozenset({'figure_pdf', 'figure_source', 'table_pdf', 'table_source'})
LANGUAGES = frozenset({'en', 'zh', 'shared'})
MAX_BYTES = 64 * 1024**2
LEGACY_MAX_MEMBERS = 32
MAX_CHANGES_PER_REQUEST = 1024
MAX_MANIFEST_BYTES = 128 * 1024
CHUNK_BYTES = 262144
LABEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z')
HASH = re.compile(r'[0-9a-f]{64}\Z')
TARGET_KEY = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,95}\Z')


def _label(value):
    if not isinstance(value, str) or not LABEL.fullmatch(value) or value in {'.', '..', 'current', 'HEAD'}:
        fail('INDEX_ARGUMENT', 'A bounded explicit version/revision label is required')
    return value


def _hash(value):
    if not isinstance(value, str) or not HASH.fullmatch(value):
        fail('INDEX_ARGUMENT', 'An exact SHA256 is required')
    return value


def _selector(role, language, figure_number=None, target_key=None):
    if not isinstance(role, str) or not isinstance(language, str) or role not in ROLES or language not in LANGUAGES:
        fail('INDEX_ARGUMENT', 'Unknown target role or language')
    if target_key is not None:
        if (not isinstance(target_key, str) or not TARGET_KEY.fullmatch(target_key)
                or figure_number is not None or role.startswith('manuscript_')
                or role in FIGURE_ROLES | {'section_tex'} and language not in {'en', 'zh'}):
            fail('INDEX_ARGUMENT', 'Named targets require a canonical key, compatible language and no numeric selector')
        return role, language, '@' + target_key
    if role in {'table_pdf', 'table_source', 'section_tex', 'bibliography', 'named_source'}:
        fail('INDEX_ARGUMENT', 'This target role requires target_key')
    if role in FIGURE_ROLES:
        if language not in {'en', 'zh'} or type(figure_number) is not int or not 1 <= figure_number <= 999:
            fail('INDEX_ARGUMENT', 'A figure requires EN/ZH and integer figure_number 1..999')
    else:
        if figure_number is not None or role.startswith('manuscript_') and language not in {'en', 'zh'}:
            fail('INDEX_ARGUMENT', 'Manuscripts require EN/ZH; non-figure targets have no figure number')
    return role, language, figure_number or 0


def _safe_intake_root(cfg, value):
    path = relpath(value)
    if not path.parts or any(part.lower() in {'builds', 'versions'} for part in path.parts):
        fail('CONFIG_INVALID', 'Build/archive trees cannot be general index intake roots')
    for field in ('chapter_workflow', 'version_management'):
        protected = cfg.get(field, {}).get('root')
        if protected and (path.is_relative_to(protected) or PurePosixPath(protected).is_relative_to(path)):
            fail('CONFIG_INVALID', 'Index intake cannot overlap managed immutable trees')
    return path


def validate_config(cfg):
    value = cfg.get('target_index')
    if value is None:
        return
    try:
        if (not isinstance(value, dict) or set(value) - {'enabled', 'ingest_roots', 'seed_members', 'max_file_bytes'}
                or type(value.get('enabled')) is not bool
                or not isinstance(value.get('ingest_roots'), list) or not 1 <= len(value['ingest_roots']) <= 16
                or len(set(value['ingest_roots'])) != len(value['ingest_roots'])
                or not isinstance(value.get('seed_members'), dict) or len(value['seed_members']) > 64
                or type(value.get('max_file_bytes', MAX_BYTES)) is not int
                or not 1 <= value.get('max_file_bytes', MAX_BYTES) <= MAX_BYTES):
            fail('CONFIG_INVALID', 'Invalid explicit target_index configuration')
        for path in value['ingest_roots']:
            _safe_intake_root(cfg, path)
        for key, item in value['seed_members'].items():
            _label(key)
            required = {'archive_version', 'member', 'sha256', 'bytes', 'role', 'language'}
            if not isinstance(item, dict) or not required <= set(item) or set(item) - required - {'figure_number', 'figure_version', 'target_key'}:
                fail('CONFIG_INVALID', 'Seed member must specify its archive, exact member, SHA, bytes and role')
            _label(item['archive_version']); relpath(item['member']); _hash(item['sha256'])
            if type(item['bytes']) is not int or not 1 <= item['bytes'] <= value.get('max_file_bytes', MAX_BYTES):
                fail('CONFIG_INVALID', 'Seed byte identity is invalid')
            _selector(item['role'], item['language'], item.get('figure_number'), item.get('target_key'))
            if 'figure_version' in item:
                if item['role'] not in FIGURE_ROLES:
                    fail('CONFIG_INVALID', 'Only figure targets have figure_version')
                _label(item['figure_version'])
    except (StudioError, TypeError, KeyError) as error:
        if isinstance(error, StudioError) and error.code == 'CONFIG_INVALID':
            raise
        fail('CONFIG_INVALID', 'Invalid target index roots, selectors or seed identities')


def _policy(cfg):
    validate_config(cfg)
    value = cfg.get('target_index')
    if not value or value['enabled'] is not True:
        fail('INDEX_DISABLED', 'This project has not enabled its target index')
    for key in ('project_id', 'project_root', 'state_root'):
        if not isinstance(cfg.get(key), str) or not cfg[key]:
            fail('CONFIG_INVALID', 'Target index requires the trusted current project binding')
    no_links(Path(cfg['project_root'])); no_links(Path(cfg['state_root']))
    if Path(cfg['state_root']).is_relative_to(cfg['project_root']):
        fail('CONFIG_INVALID', 'Protected index state must be outside the Chat-writable project tree')
    return value


def _private(path, create=False):
    no_links(path)
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.exists():
        fail('INDEX_NOT_FOUND', 'This project has no initialized target index')
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        fail('INDEX_PERMISSIONS', 'Target index requires an owned private native state directory')


def _connection(cfg, write=False):
    _policy(cfg)
    state = Path(cfg['state_root']); _private(state, write)
    root = state / 'target-index'; _private(root, write)
    path = root / 'index.sqlite3'; no_links(path)
    if not write and not path.is_file():
        fail('INDEX_NOT_FOUND', 'This project has no initialized target index')
    if path.exists():
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or info.st_mode & 0o077:
            fail('INDEX_PERMISSIONS', 'Index database must be a private single-link file')
    # SQLite journal files live inside the same protected directory.
    old_umask = os.umask(0o077)
    try:
        connection = sqlite3.connect(path if write else path.as_uri() + '?mode=ro', uri=not write, timeout=10)
    finally:
        os.umask(old_umask)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys=ON')
    if write:
        connection.executescript('''
          CREATE TABLE IF NOT EXISTS binding(id INTEGER PRIMARY KEY CHECK(id=1), identity TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS objects(sha256 TEXT PRIMARY KEY, bytes INTEGER NOT NULL, path TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS revisions(version TEXT, revision TEXT, digest TEXT NOT NULL, document TEXT NOT NULL,
            created REAL NOT NULL, PRIMARY KEY(version,revision));
          CREATE TABLE IF NOT EXISTS heads(version TEXT PRIMARY KEY, revision TEXT NOT NULL, digest TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS figure_catalog(role TEXT, number INTEGER, language TEXT, label TEXT,
            sha256 TEXT NOT NULL, document TEXT NOT NULL, PRIMARY KEY(role,number,language,label));
          CREATE TABLE IF NOT EXISTS object_chunks(sha256 TEXT, number INTEGER, digest TEXT NOT NULL,
            PRIMARY KEY(sha256,number));
          CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY, action TEXT NOT NULL, digest TEXT NOT NULL, result TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS releases(name TEXT PRIMARY KEY, version TEXT NOT NULL, revision TEXT NOT NULL,
            digest TEXT NOT NULL, reason TEXT NOT NULL, created REAL NOT NULL);
          CREATE INDEX IF NOT EXISTS release_source ON releases(version,revision);
          CREATE TABLE IF NOT EXISTS drive_receipts(version TEXT, revision TEXT, selector TEXT, sha256 TEXT NOT NULL,
            document TEXT NOT NULL, PRIMARY KEY(version,revision,selector));
          CREATE TABLE IF NOT EXISTS selection_nodes(digest TEXT PRIMARY KEY, document TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS named_catalog(role TEXT, target_key TEXT, language TEXT, label TEXT,
            sha256 TEXT NOT NULL, document TEXT NOT NULL, PRIMARY KEY(role,target_key,language,label));
          CREATE TABLE IF NOT EXISTS build_proofs(version TEXT, revision TEXT, language TEXT,
            digest TEXT NOT NULL, document TEXT NOT NULL, PRIMARY KEY(version,revision,language));
          CREATE TABLE IF NOT EXISTS revision_artifacts(version TEXT, revision TEXT, language TEXT,
            digest TEXT NOT NULL, document TEXT NOT NULL, PRIMARY KEY(version,revision,language));
        ''')
        connection.execute('BEGIN IMMEDIATE')
    identity = encoded({'schema': SCHEMA, 'project_id': cfg['project_id'],
                        'project_root': str(Path(cfg['project_root']))}).decode()
    saved = connection.execute('SELECT identity FROM binding WHERE id=1').fetchone()
    if saved is None and write:
        connection.execute('INSERT INTO binding VALUES(1,?)', (identity,))
    elif saved is None or saved['identity'] != identity:
        connection.close()
        fail('PROJECT_DENIED', 'Target index belongs to a different project binding')
    return connection, root


@contextlib.contextmanager
def _db(cfg, write=False):
    connection, root = _connection(cfg, write)
    try:
        yield connection, root
        if write:
            connection.commit()
    except Exception:
        if write:
            connection.rollback()
        raise
    finally:
        connection.close()


def _head(connection, version):
    row = connection.execute('SELECT revision,digest FROM heads WHERE version=?', (version,)).fetchone()
    return {'version': version, 'head_revision': row['revision'] if row else None,
            'head_sha256': row['digest'] if row else 'absent'}


def get_head(cfg, version):
    _label(version)
    try:
        with _db(cfg) as (connection, _):
            return {'project_id': cfg['project_id'], **_head(connection, version)}
    except StudioError as error:
        if error.code != 'INDEX_NOT_FOUND':
            raise
        return {'project_id': cfg['project_id'], 'version': version, 'head_revision': None, 'head_sha256': 'absent'}


def _expect_head(connection, version, expected):
    actual = _head(connection, version)
    if expected != actual['head_sha256']:
        fail('INDEX_HEAD_CONFLICT', 'Index head changed; read its exact SHA before writing', **actual)


def _revision(connection, version, revision):
    row = connection.execute('SELECT digest,document FROM revisions WHERE version=? AND revision=?',
                             (_label(version), _label(revision))).fetchone()
    if row is None:
        fail('INDEX_TARGET_NOT_FOUND', 'The named index revision does not exist')
    if len(row['document'].encode()) > MAX_MANIFEST_BYTES or sha(row['document'].encode()) != row['digest']:
        fail('INDEX_TAMPERED', 'Revision document differs from its recorded SHA')
    try:
        document = json.loads(row['document'])
    except (ValueError, RecursionError):
        fail('INDEX_TAMPERED', 'Invalid revision JSON')
    if not isinstance(document, dict) or document.get('version') != version or document.get('revision') != revision:
        fail('INDEX_TAMPERED', 'Revision identity changed')
    if document.get('schema') == SCHEMA + '.revision':
        if not isinstance(document.get('entries'), dict) or len(document['entries']) > LEGACY_MAX_MEMBERS:
            fail('INDEX_TAMPERED', 'Legacy revision exceeds its bounded snapshot format')
    elif document.get('schema') == 'paper-studio.target-index.v2.revision':
        if document.get('selection_format') != SELECTION_FORMAT:
            fail('INDEX_TAMPERED', 'Unknown selection storage format')
        SelectionTree(connection, document.get('selection_root'), document.get('selection_count'))
    elif document.get('schema') == 'paper-studio.target-index.v3.revision':
        if document.get('selection_format') != FOREST_FORMAT:
            fail('INDEX_TAMPERED', 'Unknown selection forest format')
        forest = SelectionForest(connection, document.get('selection_roots'))
        if document.get('selection_count') != len(forest):
            fail('INDEX_TAMPERED', 'Selection forest count differs from the revision')
    else:
        fail('INDEX_TAMPERED', 'Unsupported revision format')
    return document, row['digest']


def _selections(connection, document, *, writable=False):
    if document['schema'] == 'paper-studio.target-index.v3.revision':
        return SelectionForest(connection, document['selection_roots'])
    if document['schema'] == 'paper-studio.target-index.v2.revision':
        if not writable:
            return SelectionTree(connection, document['selection_root'], document['selection_count'])
        return SelectionForest(connection, {'numeric':{'root':document['selection_root'],'count':document['selection_count']},
                                            'named':{'root':None,'count':0}})
    if not writable:
        return document['entries']
    # Only a specifically named small legacy baseline is converted, inside the
    # new revision transaction. Its original row, SHA and receipts stay intact.
    tree = SelectionForest(connection)
    for key, entry in document['entries'].items():
        if _key(entry) != key:
            fail('INDEX_TAMPERED', 'Legacy selection identity changed')
        tree.set(key, entry)
    return tree


def _request(connection, request_id, action, body):
    _label(request_id); raw = encoded(body)
    if len(raw) > 256 * 1024:
        fail('INDEX_LIMIT', 'An index request exceeds256KiB')
    digest = sha(raw)
    prior = connection.execute('SELECT * FROM requests WHERE id=?', (request_id,)).fetchone()
    if prior:
        if prior['action'] != action or prior['digest'] != digest:
            fail('IDEMPOTENCY_CONFLICT', 'Request ID is already bound to another index operation')
        return digest, {**json.loads(prior['result']), 'idempotent_replay': True}
    return digest, None


def _receipt(connection, request_id, action, digest, result):
    connection.execute('INSERT INTO requests VALUES(?,?,?,?)', (request_id, action, digest, encoded(result).decode()))
    return result


def _key(item):
    return '|'.join(map(str, _selector(item['role'], item['language'], item.get('figure_number'), item.get('target_key'))))


def _catalog(connection, item, label):
    if item.get('target_key') is not None:
        return connection.execute('SELECT sha256,document FROM named_catalog WHERE role=? AND target_key=? AND language=? AND label=?',
            (item['role'], item['target_key'], item['language'], label)).fetchone()
    return connection.execute('SELECT sha256,document FROM figure_catalog WHERE role=? AND number=? AND language=? AND label=?',
        (item['role'], item['figure_number'], item['language'], label)).fetchone()


def _paired(item, role):
    return {'role':role, 'language':item['language'], 'figure_number':item.get('figure_number'),
            'target_key':item.get('target_key')}


def _object_stat(connection, root, digest):
    row = connection.execute('SELECT * FROM objects WHERE sha256=?', (digest,)).fetchone()
    if row is None:
        fail('INDEX_TAMPERED', 'Registered object is missing from the object index')
    if row['path'] != f'objects/{digest[:2]}/{digest}' or not 1 <= row['bytes'] <= MAX_BYTES:
        fail('INDEX_TAMPERED', 'Registered object locator differs from its content identity')
    path = root / row['path']; no_links(path)
    try: info = path.stat()
    except FileNotFoundError: fail('INDEX_TAMPERED', 'Registered immutable object is missing')
    if (not stat.S_ISREG(info.st_mode) or info.st_size != row['bytes'] or info.st_nlink != 1
            or info.st_uid != os.getuid() or info.st_mode & 0o077):
        fail('INDEX_TAMPERED', 'Registered immutable object metadata changed')
    return path, row['bytes']


def _object_range(connection, root, digest, offset, limit):
    path, total = _object_stat(connection, root, digest)
    if offset > total: fail('INDEX_ARGUMENT', 'Content offset exceeds object size')
    end = min(total, offset + limit)
    if offset == end: return b'', total
    first = offset // CHUNK_BYTES; last = (end - 1) // CHUNK_BYTES
    parts = []
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if info.st_size != total or info.st_nlink != 1:
            fail('INDEX_TAMPERED', 'Registered immutable object changed during range read')
        for number in range(first, last + 1):
            row = connection.execute('SELECT digest FROM object_chunks WHERE sha256=? AND number=?',
                                     (digest, number)).fetchone()
            stream.seek(number * CHUNK_BYTES)
            data = stream.read(min(CHUNK_BYTES, total - number * CHUNK_BYTES))
            if row is None or sha(data) != row['digest']:
                fail('INDEX_TAMPERED', 'Registered immutable object chunk changed')
            parts.append(data)
    return b''.join(parts)[offset - first * CHUNK_BYTES:end - first * CHUNK_BYTES], total


def _object_bytes(connection, root, digest):
    # Full reads are limited to explicit local publish verification, never lookup.
    _, total = _object_stat(connection, root, digest)
    return b''.join(_object_range(connection, root, digest, offset, CHUNK_BYTES)[0]
                    for offset in range(0, total, CHUNK_BYTES))


def _store(connection, root, raw):
    digest = sha(raw); relative = f'objects/{digest[:2]}/{digest}'
    target = root / relative; _private(target.parent, True)
    if target.exists():
        saved = safe_read(root, relative, MAX_BYTES)
        if saved != raw:
            fail('INDEX_TAMPERED', 'Content-addressed object has different bytes')
    else:
        temporary = target.parent / ('.stage-' + uuid.uuid4().hex)
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw); stream.flush(); os.fsync(stream.fileno())
            try:
                os.link(temporary, target)
            except FileExistsError:
                if safe_read(root, relative, MAX_BYTES) != raw:
                    fail('INDEX_TAMPERED', 'Concurrent object differs')
        finally:
            temporary.unlink(missing_ok=True)
    connection.execute('INSERT OR IGNORE INTO objects VALUES(?,?,?)', (digest, len(raw), relative))
    connection.executemany('INSERT OR IGNORE INTO object_chunks VALUES(?,?,?)',
                          [(digest, number, sha(raw[offset:offset + CHUNK_BYTES]))
                           for number, offset in enumerate(range(0, len(raw), CHUNK_BYTES))])
    return digest


def _mime(role, name, raw):
    suffix = PurePosixPath(name).suffix.lower()
    if role.endswith('_pdf'):
        if suffix != '.pdf' or not raw.startswith(b'%PDF-'):
            fail('INDEX_ARGUMENT', 'PDF targets require actual PDF bytes in a .pdf file')
        return 'application/pdf'
    if role in {'manuscript_tex', 'section_tex', 'bibliography'}:
        extension = '.bib' if role == 'bibliography' else '.tex'
        if suffix != extension:
            fail('INDEX_ARGUMENT', 'Manuscript text target must be a TeX file')
        try: raw.decode('utf-8')
        except UnicodeError: fail('INDEX_ARGUMENT', 'Manuscript source must be UTF-8')
        return 'text/plain' if role == 'bibliography' else 'text/x-tex'
    allowed = {'.py': 'text/x-python', '.pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
               '.zip': 'application/zip', '.json': 'application/json', '.csv': 'text/csv', '.md': 'text/markdown',
               '.txt': 'text/plain', '.svg': 'image/svg+xml', '.png': 'image/png', '.tex': 'text/x-tex',
               '.pdf': 'application/pdf', '.bib': 'text/plain', '.cls':'text/plain', '.sty':'text/plain',
               '.bst':'text/plain', '.bbx':'text/plain', '.cbx':'text/plain', '.def':'text/plain',
               '.jpg':'image/jpeg', '.jpeg':'image/jpeg', '.eps':'application/postscript', '.yaml':'text/plain', '.yml':'text/plain'}
    if suffix not in allowed:
        fail('INDEX_ARGUMENT', 'Target file type is outside the bounded index formats')
    return allowed[suffix]


def _entry(connection, root, item, raw, version, revision):
    role, language, selector = _selector(item['role'], item['language'], item.get('figure_number'), item.get('target_key'))
    if role not in FIGURE_ROLES and 'figure_version' in item:
        fail('INDEX_ARGUMENT', 'Only figure or table assets accept figure_version')
    mime = _mime(role, item.get('path', item.get('member', '')), raw)
    digest = _store(connection, root, raw)
    result = {'role': role, 'language': language, 'figure_number': item.get('figure_number'),
              'target_key':item.get('target_key'), 'project_path':item.get('path'),
              'object_id': digest, 'sha256': digest, 'bytes': len(raw),
              'mime_type': mime,
              'effective_version': version, 'effective_revision': revision,
              'current_for_revision': True, 'generation_verified': False,
              'registration': 'explicit_bytes'}
    if role in FIGURE_ROLES:
        label = item.get('figure_version') or f'asset-{language}-{digest[:16]}'
        _label(label)
        # The same human figure version cannot silently identify different bytes.
        old = _catalog(connection, item, label)
        if old and old['sha256'] != digest:
            fail('FIGURE_VERSION_CONFLICT', 'Figure version label already identifies different bytes')
        result['figure_version'] = label
        table = 'named_catalog' if item.get('target_key') is not None else 'figure_catalog'
        identity = item['target_key'] if table == 'named_catalog' else item['figure_number']
        connection.execute(f'INSERT OR IGNORE INTO {table} VALUES(?,?,?,?,?,?)',
                           (role, identity, language, label, digest, encoded(result).decode()))
    else:
        result['figure_version'] = None
    return result


def _commit_revision(connection, cfg, version, revision, entries, baseline, request_id, action, digest, changed,
                     invalidated_languages=()):
    if connection.execute('SELECT 1 FROM revisions WHERE version=? AND revision=?', (version, revision)).fetchone():
        fail('INDEX_REVISION_EXISTS', 'Index revisions are immutable; choose a new revision label')
    if connection.execute('SELECT 1 FROM releases WHERE name=?', (version,)).fetchone():
        fail('INDEX_RELEASE_EXISTS', 'A working namespace cannot shadow a formal release')
    document = {
        'schema': 'paper-studio.target-index.v3.revision',
        'project_id': cfg['project_id'],
        'version': version,
        'revision': revision,
        'kind': 'working',
        'baseline': baseline,
        'selection_format': FOREST_FORMAT,
        'selection_roots': entries.roots,
        'selection_count': len(entries),
    }
    raw = encoded(document).decode()
    identity = sha(raw.encode())
    if len(raw.encode()) > MAX_MANIFEST_BYTES:
        fail('INDEX_LIMIT', 'A revision manifest exceeds128KiB')
    entries.flush()
    connection.execute('INSERT INTO revisions VALUES(?,?,?,?,?)', (version, revision, identity, raw, time.time()))
    connection.execute('INSERT INTO heads VALUES(?,?,?) ON CONFLICT(version) DO UPDATE SET revision=excluded.revision,digest=excluded.digest',
                       (version, revision, identity))
    if isinstance(baseline,dict) and {'version','revision'} <= set(baseline):
        # A direct candidate reference, never an ancestry walk. Publication
        # rechecks all current selections and live dependencies before reuse.
        connection.execute('INSERT INTO build_proofs SELECT ?,?,language,digest,document FROM build_proofs WHERE version=? AND revision=?',
                           (version,revision,baseline['version'],baseline['revision']))
        for language in ('en','zh'):
            if language not in invalidated_languages:
                connection.execute('INSERT INTO revision_artifacts SELECT ?,?,language,digest,document FROM revision_artifacts WHERE version=? AND revision=? AND language=?',
                    (version,revision,baseline['version'],baseline['revision'],language))
    stale_entries = []
    for language in ('en', 'zh'):
        key = _key({'role': 'manuscript_pdf', 'language': language})
        item = entries.get(key)
        if item and not item['current_for_revision']:
            stale_entries.append(key)
    result = {'project_id': cfg['project_id'], 'version': version, 'revision': revision,
              'revision_sha256': identity, 'head_revision': revision, 'head_sha256': identity,
              'kind': 'working', 'formal_published': False, 'entry_count': len(entries),
              'changed_entries': changed, 'stale_entries': stale_entries,
              'idempotent_replay': False}
    return _receipt(connection, request_id, action, digest, result)


def seed_revision(cfg, *, version, revision, seed_ids, request_id, expected_head='absent'):
    """Register only exact SHA/role-whitelisted members of a named old archive."""
    _label(version); _label(revision); policy = _policy(cfg)
    if not isinstance(seed_ids, list) or not 1 <= len(seed_ids) <= MAX_CHANGES_PER_REQUEST or len(set(seed_ids)) != len(seed_ids):
        fail('INDEX_ARGUMENT', 'Choose a bounded unique list of configured seed IDs')
    body = {'version': version, 'revision': revision, 'seed_ids': seed_ids, 'expected_head': expected_head}
    with _db(cfg, True) as (connection, root):
        digest, prior = _request(connection, request_id, 'seed', body)
        if prior: return prior
        _expect_head(connection, version, expected_head)
        entries = SelectionForest(connection); sources = []
        from paper_studio.versions.history import _read_version_manifest
        for key in seed_ids:
            item = policy['seed_members'].get(key)
            if item is None: fail('INDEX_SEED_DENIED', 'Baseline member is not explicitly configured', seed_id=key)
            archive, manifest, identity = _read_version_manifest(cfg, item['archive_version'])
            row = next((row for row in manifest['files'] if row['path'] == item['member']), None)
            if not row or any(row.get(field) != item[field] for field in ('sha256', 'bytes')):
                fail('INDEX_SEED_CHANGED', 'Exact baseline member differs from the configured identity')
            raw = safe_read(archive, item['member'], policy.get('max_file_bytes', MAX_BYTES))
            if sha(raw) != item['sha256'] or len(raw) != item['bytes']:
                fail('INDEX_SEED_CHANGED', 'Baseline member bytes differ from the configured identity')
            selector = _key(item)
            if entries.get(selector): fail('INDEX_ARGUMENT', 'Seed selectors must be unique')
            source = {'seed_id': key, 'archive_version': item['archive_version'], 'manifest_sha256': identity,
                      'member': item['member'], 'sha256': item['sha256']}
            entry = _entry(connection, root, item, raw, version, revision)
            if '/source/' in item['member']:
                entry['project_path'] = item['member'].split('/source/',1)[1]
            entry['imported_source'] = source
            entries.set(selector, entry); sources.append(source)
        return _commit_revision(connection, cfg, version, revision, entries,
                                {'imported_member_count':len(sources),'imported_members_sha256':sha(encoded(sources))},
                                request_id, 'seed', digest, sorted(_key(policy['seed_members'][key]) for key in seed_ids))


def create_revision(cfg, *, version, revision, baseline, members=None, figure_selections=None,
                    expected_head, request_id, kind='working'):
    if any(isinstance(value,str) and re.fullmatch(r'[0-9]+(?:\.[0-9]+)*', value) for value in (version, revision)):
        fail('REVIEW_APPLY_REQUIRED', 'Numeric manuscript revisions require an approved review application')
    with _db(cfg, True) as (connection, root):
        return create_revision_in_transaction(cfg, connection, root, version=version, revision=revision,
            baseline=baseline, members=members, figure_selections=figure_selections,
            expected_head=expected_head, request_id=request_id, kind=kind)


def create_revision_in_transaction(cfg, connection, root, *, version, revision, baseline,
                                   members=None, figure_selections=None, expected_head,
                                   request_id, kind='working', captured_bytes=None):
    """Trusted review application only; caller owns and commits this transaction."""
    _label(version); _label(revision); policy = _policy(cfg)
    expected_root = Path(cfg['state_root'])/'target-index'
    if Path(root) != expected_root or not connection.in_transaction:
        fail('INDEX_TRANSACTION_REQUIRED', 'Review application requires the bound active index transaction')
    database = next((row[2] for row in connection.execute('PRAGMA database_list') if row[1] == 'main'),None)
    binding = connection.execute('SELECT identity FROM binding WHERE id=1').fetchone()
    expected_binding = encoded({'schema':SCHEMA,'project_id':cfg['project_id'],
                                'project_root':str(Path(cfg['project_root']))}).decode()
    if database != str(expected_root/'index.sqlite3') or binding is None or binding['identity'] != expected_binding:
        fail('PROJECT_DENIED', 'Review transaction belongs to another project binding')
    members = [] if members is None else members
    selections = [] if figure_selections is None else figure_selections
    if kind != 'working' or not isinstance(baseline, dict) or set(baseline) != {'version', 'revision'}:
        fail('INDEX_ARGUMENT', 'Every child is working and names an explicit baseline version/revision')
    _label(baseline['version']); _label(baseline['revision'])
    if (not isinstance(members, list) or not isinstance(selections, list)
            or len(members) + len(selections) > MAX_CHANGES_PER_REQUEST):
        fail('INDEX_ARGUMENT', 'Revision changes exceed the bounded member/figure count')
    if captured_bytes is not None and (not isinstance(captured_bytes,dict)
            or set(captured_bytes) != {item.get('path') for item in members if isinstance(item,dict)}):
        fail('INDEX_ARGUMENT', 'Captured review bytes must exactly match the approved member paths')
    body = {'version': version, 'revision': revision, 'baseline': baseline, 'members': members,
            'figure_selections': selections, 'expected_head': expected_head, 'kind': kind}
    with contextlib.nullcontext((connection, root)):
        digest, prior = _request(connection, request_id, 'revise', body)
        if prior: return prior
        _expect_head(connection, version, expected_head)
        previous, base_sha = _revision(connection, **baseline)
        entries = _selections(connection, previous, writable=True); changed = []; dirty = set(); supplied = set()
        artifact_dirty = set()
        for item in members:
            needed = {'path', 'sha256', 'role', 'language'}
            if not isinstance(item, dict) or not needed <= set(item) or set(item) - needed - {'figure_number', 'figure_version', 'target_key'}:
                fail('INDEX_ARGUMENT', 'Each intake member requires path, exact SHA, role and language')
            path = relpath(item['path']); _hash(item['sha256'])
            if not any(path.is_relative_to(_safe_intake_root(cfg, value)) for value in policy['ingest_roots']):
                fail('INDEX_INTAKE_DENIED', 'Only explicitly configured project intake files may be captured')
            raw = (captured_bytes[item['path']] if captured_bytes is not None
                   else safe_read(cfg['project_root'], item['path'], policy.get('max_file_bytes', MAX_BYTES)))
            if not isinstance(raw,bytes) or len(raw) > policy.get('max_file_bytes', MAX_BYTES):
                fail('INDEX_LIMIT', 'Captured review member exceeds its file budget')
            if not raw or sha(raw) != item['sha256']:
                fail('REVISION_CONFLICT', 'Intake bytes changed after the supplied read identity')
            key = _key(item)
            if key in supplied: fail('INDEX_ARGUMENT', 'A revision cannot provide duplicate selectors')
            supplied.add(key); old = entries.get(key)
            entry = _entry(connection, root, item, raw, version, revision)
            entries.set(key, entry); changed.append(key)
            if item['role'] in {'manuscript_tex', 'figure_pdf', 'table_pdf', 'section_tex', 'bibliography', 'named_source'} and (old is None or old['sha256'] != entry['sha256'] or old.get('project_path') != entry.get('project_path')):
                dirty.update({'en', 'zh'} if item['language'] == 'shared' else {item['language']})
            if item['role'] == 'manuscript_pdf' and (old is None or old['sha256'] != entry['sha256']):
                artifact_dirty.add(item['language'])
        for choice in selections:
            if (not isinstance(choice, dict) or not {'language','figure_version'} <= set(choice)
                    or set(choice) - {'role','figure_number','target_key','language','figure_version'}):
                fail('INDEX_ARGUMENT', 'Figure selections need number, language and figure_version')
            language = choice['language']; label = _label(choice['figure_version'])
            role = choice.get('role','figure_pdf')
            if role not in {'figure_pdf','table_pdf'}:
                fail('INDEX_ARGUMENT', 'Select a registered figure or table PDF')
            selected_item = {**choice, 'role':role}
            key = _key(selected_item)
            if key in supplied: fail('INDEX_ARGUMENT', 'Choose either a new figure file or an existing figure version')
            row = _catalog(connection, selected_item, label)
            if row is None: fail('INDEX_TARGET_NOT_FOUND', 'The selected language figure version is not registered')
            found = json.loads(row['document'])
            origin_document,_ = _revision(connection,found['effective_version'],found['effective_revision'])
            origin = _selections(connection,origin_document).get(key)
            if origin is None or origin['sha256'] != row['sha256'] or origin.get('figure_version') != label:
                fail('INDEX_TAMPERED', 'Asset catalog differs from its immutable source revision')
            found = origin
            old = entries.get(key,{})
            if old.get('sha256') != found['sha256'] or old.get('project_path') != found.get('project_path'):
                dirty.add(language)
            _object_stat(connection, root, found['sha256']); entries.set(key, found); changed.append(key); supplied.add(key)
            # Editable source follows the same selected language/version. An old
            # source must never masquerade as the source of a reselected PDF.
            source_item = _paired(selected_item, role.replace('_pdf','_source'))
            source_key = _key(source_item)
            source = _catalog(connection, source_item, label)
            if source_key in supplied:
                if entries.get(source_key).get('figure_version') != label:
                    fail('INDEX_ARGUMENT', 'Explicit figure source differs from the selected PDF version')
            elif source:
                entries.set(source_key, json.loads(source['document'])); changed.append(source_key)
            else:
                entries.delete(source_key)
                found['source_available'] = False
                entries.set(key, found)
        for key in list(supplied):
            item = entries.get(key)
            if item['role'] in {'figure_pdf','table_pdf'}:
                source_key = _key(_paired(item, item['role'].replace('_pdf','_source')))
                source = entries.get(source_key)
                if source and source.get('figure_version') != item['figure_version']:
                    if source_key in supplied:
                        fail('INDEX_ARGUMENT', 'A figure PDF and its editable source must use the same figure_version')
                    entries.delete(source_key)
                item['source_available'] = entries.get(source_key) is not None
                entries.set(key, item)
            elif item['role'] in {'figure_source','table_source'}:
                pdf_key = _key(_paired(item, item['role'].replace('_source','_pdf')))
                pdf = entries.get(pdf_key)
                if pdf and pdf['figure_version'] != item['figure_version']:
                    fail('INDEX_ARGUMENT', 'A selected editable source must match the selected PDF figure_version')
                if pdf:
                    pdf['source_available'] = True; entries.set(pdf_key, pdf)
        for language in dirty:
            key = _key({'role':'manuscript_pdf','language':language})
            entry = entries.get(key)
            if entry and key not in supplied:
                entry['current_for_revision'] = False
                entry['stale_reason'] = 'manuscript_or_selected_figure_changed_without_a_new_explicit_pdf'
                entries.set(key, entry)
        baseline_identity = {**baseline, 'revision_sha256': base_sha}
        return _commit_revision(connection, cfg, version, revision, entries, baseline_identity,
                                request_id, 'revise', digest, changed, invalidated_languages=dirty | artifact_dirty)


def _resolve_revision(connection, version, revision):
    _label(version)
    if revision is not None:
        return version, _label(revision), 'explicit_revision'
    formal = connection.execute('SELECT version,revision FROM releases WHERE name=?', (version,)).fetchone()
    if formal: return formal['version'], formal['revision'], 'formal_release'
    head = _head(connection, version)
    if head['head_revision'] is None: fail('INDEX_TARGET_NOT_FOUND', 'No named formal release or working head exists')
    return version, head['head_revision'], 'head'


def _drive_reference(row, version, revision):
    """Project one selected receipt; full immutable audit evidence stays in SQLite.

    In particular, transport traces contain historic operations and other IDs.
    They are not evidence of a new operation during this read-only lookup and
    must not be embedded in either metadata or individual content responses.
    An explicit allowlist applies equally to old and newly registered rows.
    """
    if row is None:
        return None
    receipt = json.loads(row['document'])
    fields = ('file_id', 'url', 'name', 'bytes', 'sha256', 'mime_type', 'verified_at',
              'metadata_verified', 'server_sha256_verified', 'expected_identity_verified',
              'owner_only_verified', 'official_publication', 'chat_readback_verified')
    result = {key: receipt[key] for key in fields if key in receipt}
    result.update(projection='file_reference', audit_record_included=False,
                  inherited=(row['version'], row['revision']) != (version, revision),
                  from_version=row['version'], from_revision=row['revision'])
    return result


def _revision_artifact(connection, version, revision, language):
    table = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='revision_artifacts'").fetchone()
    if not table:
        return None
    row = connection.execute('SELECT digest,document FROM revision_artifacts WHERE version=? AND revision=? AND language=?',
                             (version,revision,language)).fetchone()
    if row is None:
        return None
    if sha(row['document'].encode()) != row['digest']:
        fail('INDEX_TAMPERED', 'Verified revision artifact metadata changed')
    return json.loads(row['document'])


def resolve(cfg, *, version, revision=None, role, language, figure_number=None, part='metadata',
            offset=0, limit=262144, expected_revision_sha256=None, target_key=None):
    """Resolve one exact registered object. Content is bounded and contains no paths."""
    key = '|'.join(map(str, _selector(role, language, figure_number, target_key)))
    if part not in {'metadata', 'content'} or type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 262144:
        fail('INDEX_ARGUMENT', 'Use metadata or a bounded content range of at most256KiB')
    with _db(cfg) as (connection, root):
        actual_version, actual_revision, how = _resolve_revision(connection, version, revision)
        document, identity = _revision(connection, actual_version, actual_revision)
        if expected_revision_sha256 is not None and expected_revision_sha256 != identity:
            fail('INDEX_REVISION_CONFLICT', 'Resolved revision changed; use its exact recorded SHA')
        selections = _selections(connection, document)
        entry = selections.get(key)
        if role == 'manuscript_pdf':
            entry = _revision_artifact(connection,actual_version,actual_revision,language) or entry
        if isinstance(selections, (SelectionTree,SelectionForest)):
            storage = {'format': NAMED_FORMAT if target_key else SELECTION_FORMAT, 'nodes_read': len(selections._cache)}
        else:
            storage = {'format': 'legacy_snapshot', 'members_read': len(selections)}
        if entry is None:
            fail('INDEX_TARGET_NOT_FOUND', 'The exact requested target is not registered', selection_storage=storage)
        _, total = _object_stat(connection, root, entry['object_id'])
        drive = connection.execute('SELECT version,revision,document FROM drive_receipts WHERE version=? AND revision=? AND selector=? AND sha256=?',
                                   (actual_version, actual_revision, key, entry['sha256'])).fetchone()
        if drive is None:
            drive = connection.execute('SELECT version,revision,document FROM drive_receipts WHERE version=? AND revision=? AND selector=? AND sha256=?',
                                       (entry['effective_version'], entry['effective_revision'], key, entry['sha256'])).fetchone()
        result = {'schema': SCHEMA + '.target', 'project_id': cfg['project_id'],
                  'config_revision': cfg.get('_config_revision'), 'requested_version': version, 'requested_revision': revision,
                  'resolved_version': actual_version, 'resolved_revision': actual_revision, 'resolution_kind': how,
                  'revision_sha256': identity, **entry, 'local_available': True,
                  'selection_storage':storage,
                  'inherited': (entry['effective_version'], entry['effective_revision']) != (actual_version, actual_revision),
                  'drive_receipt': _drive_reference(drive, actual_version, actual_revision)
                                   if entry['current_for_revision'] else None}
        if part == 'content':
            if not entry['current_for_revision']:
                fail('INDEX_TARGET_STALE', 'Inherited manuscript PDF predates this revision; register its exact new PDF',
                     effective_version=entry['effective_version'], effective_revision=entry['effective_revision'])
            data, total = _object_range(connection, root, entry['object_id'], offset, limit)
            result.update(offset=offset, total=total, chunk_bytes=len(data), chunk_sha256=sha(data),
                          data_base64=base64.b64encode(data).decode(),
                          next_offset=offset + len(data) if offset + len(data) < total else None)
        return result


def publish(cfg, *, version, revision, release_name, expected_head, expected_revision_sha256,
            request_id, reason, authorized=False, user_requested=False):
    """Management-only explicit user release; never called by automatic revisions."""
    if authorized is not True or user_requested is not True or not isinstance(reason, str) or not reason.strip():
        fail('AUTHORIZATION_REQUIRED', 'A formal release needs an explicit user publication request and reason')
    _label(release_name)
    body = dict(version=version, revision=revision, release_name=release_name, expected_head=expected_head,
                expected_revision_sha256=expected_revision_sha256, reason=reason)
    with _db(cfg, True) as (connection, root):
        digest, prior = _request(connection, request_id, 'publish', body)
        if prior: return {**prior,'receipt_only':True,'current_inputs_revalidated':False}
        _expect_head(connection, version, expected_head)
        document, identity = _revision(connection, version, revision)
        if identity != expected_revision_sha256: fail('INDEX_REVISION_CONFLICT', 'Formal release source revision changed')
        verified = _verify_release(connection, cfg, document)
        if connection.execute('SELECT 1 FROM releases WHERE name=?', (release_name,)).fetchone():
            fail('INDEX_RELEASE_EXISTS', 'Formal release names are immutable')
        if connection.execute('SELECT 1 FROM heads WHERE version=?', (release_name,)).fetchone():
            fail('INDEX_RELEASE_EXISTS', 'A formal release cannot shadow an existing working namespace')
        connection.execute('INSERT INTO releases VALUES(?,?,?,?,?,?)', (release_name, version, revision, identity, reason, time.time()))
        return _receipt(connection, request_id, 'publish', digest, {'project_id': cfg['project_id'],
            'release_name': release_name, 'version': version, 'revision': revision, 'revision_sha256': identity,
            'formal_published': True, 'drive_published': False, 'legacy_current_changed': False,
            'receipt_only':False,'current_inputs_revalidated':True,
            'build_proofs':verified['build_proofs']})


def _verify_release(connection, cfg, document):
    from paper_studio.versions.index_proof import attest
    selections = _selections(connection,document)
    verified = {}
    for language in ('en','zh'):
        pdf = _revision_artifact(connection,document['version'],document['revision'],language)
        table = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='build_proofs'").fetchone()
        row = (connection.execute('SELECT digest,document FROM build_proofs WHERE version=? AND revision=? AND language=?',
            (document['version'],document['revision'],language)).fetchone() if table else None)
        if row is None:
            fail('BUILD_PROOF_REQUIRED', 'Formal release requires verified native build evidence', language=language)
        if sha(row['document'].encode()) != row['digest']:
            fail('INDEX_TAMPERED', 'Registered build evidence changed')
        proof = json.loads(row['document'])
        fresh = attest(cfg,selections,language,proof['build_id'])
        if not pdf or not pdf['current_for_revision']:
            fail('BUILD_PROOF_REQUIRED', 'Formal release needs attached verified EN/ZH native PDFs')
        if pdf.get('sha256') != fresh['pdf_sha256'] or pdf.get('bytes') != fresh['pdf_bytes']:
            fail('BUILD_PROOF_PDF_MISMATCH', 'Verified revision artifact differs from its native build')
        private_root = Path(cfg['state_root'])/'target-index'
        _, total = _object_stat(connection,private_root,pdf['object_id'])
        if total != pdf['bytes']:
            fail('INDEX_TAMPERED', 'Verified revision PDF size differs from its native build')
        whole = hashlib.sha256()
        for offset in range(0,total,CHUNK_BYTES):
            data,_ = _object_range(connection,private_root,pdf['object_id'],offset,CHUNK_BYTES)
            whole.update(data)
        if whole.hexdigest() != fresh['pdf_sha256']:
            fail('INDEX_TAMPERED', 'Verified revision PDF bytes differ from its native build')
        for field in ('project_id','language','build_manifest_sha256','request_document_sha256',
                      'snapshot_sha256','pdf_sha256','pdf_bytes','selection_sha256'):
            if fresh[field] != proof[field]:
                fail('BUILD_PROOF_SELECTION_CHANGED', 'Current release source selection differs from its native build proof', field=field)
        verified[language] = {'build_id':proof['build_id'],'proof_sha256':row['digest'],
                              'generation_verified':True,'dependency_count':fresh['dependency_count']}
    return {'project_id':cfg['project_id'],'version':document['version'],'revision':document['revision'],
            'release_ready':True,'build_proofs':verified,'formal_published':False}


def verify_release(cfg, *, version, revision, expected_head, expected_revision_sha256):
    """Read-only publication preflight. Declared languages come from policy."""
    with _db(cfg) as (connection,_):
        _expect_head(connection,version,expected_head)
        document,identity = _revision(connection,version,revision)
        if identity != expected_revision_sha256:
            fail('INDEX_REVISION_CONFLICT', 'Release source revision changed')
        return {**_verify_release(connection,cfg,document),'revision_sha256':identity}


def register_build_proof(cfg, *, version, revision, language, build_id, expected_head, request_id):
    from paper_studio.versions.index_proof import attest
    body = dict(version=version,revision=revision,language=language,build_id=build_id,expected_head=expected_head)
    with _db(cfg,True) as (connection,root):
        digest,prior = _request(connection,request_id,'build-proof',body)
        if prior:
            return prior
        _expect_head(connection,version,expected_head)
        if connection.execute('SELECT 1 FROM releases WHERE version=? AND revision=? LIMIT 1',
                              (version,revision)).fetchone():
            fail('INDEX_RELEASE_FROZEN', 'Published revision artifacts and build evidence are immutable')
        document,identity = _revision(connection,version,revision)
        proof = attest(cfg,_selections(connection,document),language,build_id)
        proof.update(registered_version=version,registered_revision=revision)
        raw = encoded(proof).decode()
        if len(raw.encode()) > 8*1024*1024:
            fail('INDEX_LIMIT', 'Native build evidence exceeds8MiB')
        old = connection.execute('SELECT digest,document FROM build_proofs WHERE version=? AND revision=? AND language=?',
            (version,revision,language)).fetchone()
        inherited = False
        if old:
            saved = json.loads(old['document'])
            inherited = (saved.get('registered_version'),saved.get('registered_revision')) != (version,revision)
            stable = ('build_id','build_manifest_sha256','request_document_sha256','snapshot_sha256',
                      'pdf_sha256','pdf_bytes','selection_sha256')
            if not inherited and any(saved.get(field) != proof[field] for field in stable):
                fail('BUILD_PROOF_CONFLICT', 'Registered native build evidence is immutable; create a new revision')
            if not inherited:
                proof_digest = old['digest']
        if old is None or inherited:
            proof_digest = sha(raw.encode())
            connection.execute('INSERT INTO build_proofs VALUES(?,?,?,?,?) ON CONFLICT(version,revision,language) DO UPDATE SET digest=excluded.digest,document=excluded.document',
                               (version,revision,language,proof_digest,raw))
        from paper_studio.provider.jobs import job_path
        native_pdf = safe_read(job_path(cfg,build_id)/'output',proof['pdf_artifact_path'],MAX_BYTES)
        if sha(native_pdf) != proof['pdf_sha256'] or len(native_pdf) != proof['pdf_bytes']:
            fail('BUILD_PROOF_PDF_MISMATCH', 'Native PDF changed during artifact attachment')
        object_id = _store(connection,root,native_pdf)
        artifact = {'role':'manuscript_pdf','language':language,'figure_number':None,'target_key':None,
            'object_id':object_id,'sha256':object_id,'bytes':len(native_pdf),'mime_type':'application/pdf',
            'effective_version':version,'effective_revision':revision,'current_for_revision':True,
            'generation_verified':True,'registration':'verified_native_build','figure_version':None,
            'build_id':build_id,'build_proof_sha256':proof_digest,'source_revalidation':'required_at_release'}
        previous_artifact = _revision_artifact(connection,version,revision,language)
        if previous_artifact and not inherited:
            if any(previous_artifact.get(field) != artifact[field] for field in ('sha256','bytes','build_id','build_proof_sha256')):
                fail('BUILD_PROOF_CONFLICT', 'A verified revision artifact cannot be replaced')
        else:
            artifact_raw = encoded(artifact).decode()
            connection.execute('INSERT INTO revision_artifacts VALUES(?,?,?,?,?) ON CONFLICT(version,revision,language) DO UPDATE SET digest=excluded.digest,document=excluded.document',
                               (version,revision,language,sha(artifact_raw.encode()),artifact_raw))
        return _receipt(connection,request_id,'build-proof',digest,{'project_id':cfg['project_id'],
            'version':version,'revision':revision,'revision_sha256':identity,'language':language,
            'build_id':build_id,'proof_sha256':proof_digest,'generation_verified':True,
            'dependency_count':proof['dependency_count'],'formal_published':False,'idempotent_replay':False})


attach_build = register_build_proof


def source_identity(metadata):
    """Canonical identity to pass unchanged to exact-ID Drive delivery."""
    return {**{key: metadata[key] for key in ('revision_sha256', 'object_id', 'sha256', 'bytes',
                                            'role', 'language', 'figure_number')},
            'version': metadata['resolved_version'], 'revision': metadata['resolved_revision'],
            **({'target_key':metadata['target_key']} if metadata.get('target_key') is not None else {})}


def register_drive_receipt(cfg, *, version, revision, role, language, figure_number=None,
                           receipt, expected_head, request_id, drive=None, target_key=None):
    """Revalidate a role/byte/selector-bound exact-ID receipt before storing it."""
    body = dict(version=version, revision=revision, role=role, language=language, figure_number=figure_number,
                receipt=receipt, expected_head=expected_head)
    if target_key is not None:
        body['target_key'] = target_key
    # Replay has no new external effect and returns its original evidence.
    with _db(cfg) as (connection, _):
        _, prior = _request(connection, request_id, 'drive', body)
        if prior: return prior
    selected = resolve(cfg, version=version, revision=revision, role=role, language=language, figure_number=figure_number,target_key=target_key)
    if not selected['current_for_revision']: fail('INDEX_TARGET_STALE', 'Cannot attach a current delivery to a stale PDF')
    if not isinstance(receipt, dict) or receipt.get('schema') != 'paper-studio.target-display-receipt.v1':
        fail('INDEX_RECEIPT_CONFLICT', 'An exact target display receipt is required')
    for key, expected in [('project_id', cfg['project_id']), ('role', role), ('language', language),
                          ('sha256', selected['sha256']), ('bytes', selected['bytes']), ('figure_number', figure_number),('target_key',target_key)]:
        if receipt.get(key) != expected: fail('INDEX_RECEIPT_CONFLICT', 'Receipt role, language, selector or bytes differ', field=key)
    source = receipt.get('source_identity')
    identity = source_identity(selected)
    if not isinstance(source, dict) or any(source.get(key) != value for key, value in identity.items()):
        fail('INDEX_RECEIPT_CONFLICT', 'Receipt must retain the complete resolved index source identity')
    for key in ('metadata_verified', 'server_sha256_verified', 'expected_identity_verified', 'owner_only_verified'):
        if receipt.get(key) is not True: fail('INDEX_RECEIPT_CONFLICT', 'Receipt lacks verified identity', field=key)
    # Reuse the exact-ID transport and its project-folder authorization. It does
    # not list/search Drive, mutate objects, or trust a caller's verified flags.
    from paper_studio.drive.target_display import verify_exact
    if any(not isinstance(receipt.get(key), str) or not receipt[key]
           for key in ('project_folder_id', 'parent_id', 'file_id', 'name', 'mime_type')):
        fail('INDEX_RECEIPT_CONFLICT', 'Receipt requires exact Drive folder, file, name and MIME identities')
    verified = verify_exact(cfg, project_id=cfg['project_id'], project_folder_id=receipt['project_folder_id'],
        parent_id=receipt['parent_id'], file_id=receipt['file_id'], name=receipt['name'],
        sha256=selected['sha256'], bytes=selected['bytes'], role=role, language=language,
        source_identity=source, drive=drive)
    # Transport may return its receipt together with a separate trace envelope.
    verified = verified.get('receipt', verified)
    for key in ('file_id', 'sha256', 'bytes', 'parent_id', 'mime_type', 'project_id', 'project_folder_id',
                'name', 'role', 'language', 'figure_number', 'source_identity'):
        if verified.get(key) != receipt.get(key): fail('INDEX_RECEIPT_CONFLICT', 'Drive readback differs from the submitted receipt', field=key)
    with _db(cfg, True) as (connection, _):
        digest, prior = _request(connection, request_id, 'drive', body)
        if prior: return prior
        _expect_head(connection, version, expected_head)
        _, current_identity = _revision(connection, version, revision)
        if current_identity != selected['revision_sha256']: fail('INDEX_REVISION_CONFLICT', 'Drive registration revision changed')
        key = _key({'role': role, 'language': language, 'figure_number': figure_number,'target_key':target_key})
        old = connection.execute('SELECT document FROM drive_receipts WHERE version=? AND revision=? AND selector=?', (version, revision, key)).fetchone()
        stored = json.loads(old['document']) if old else verified
        immutable = ('file_id', 'sha256', 'bytes', 'parent_id', 'mime_type', 'project_id', 'project_folder_id',
                     'name', 'role', 'language', 'figure_number', 'source_identity')
        if old and any(stored.get(key) != verified.get(key) for key in immutable):
            fail('INDEX_RECEIPT_CONFLICT', 'Registered delivery identity is immutable')
        connection.execute('INSERT OR IGNORE INTO drive_receipts VALUES(?,?,?,?,?)',
                           (version, revision, key, selected['sha256'], encoded(verified).decode()))
        return _receipt(connection, request_id, 'drive', digest, {'project_id': cfg['project_id'], 'version': version,
            'revision': revision, 'object_id': selected['object_id'], 'drive_receipt': stored,
            'fresh_verification': verified, 'idempotent_replay': False})
