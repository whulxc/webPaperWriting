"""Private, project-bound material metadata. Lookups never build the catalog.

The legacy JSON is consumed only by the explicit local enable operation. Packs
remain immutable in their original locations. The database is a trusted local
registry, not a claim that all historical payloads were reverified on each read.
"""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile

from .workspace import _project_metadata, digest, encoded, fail, no_links

SCHEMA = 'paper-studio.material-handoff-catalog.v1'
STATE_ENV = 'PAPER_STUDIO_HANDOFF_STATE_ROOT'
FIELDS = {'change_id', 'sequence', 'manifest_path', 'manifest_sha256', 'previous_manifest_sha256'}
LABEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z')
HASH = re.compile(r'[a-f0-9]{64}\Z')


def path_for(paper):
    paper = no_links(Path(paper))
    # Drive-letter/DrvFS workspaces need not support Unix private modes. Keep
    # the catalog and SQLite sidecars outside all paper editor writable roots.
    # This is an administrator environment setting, never a tool argument.
    base = Path(os.environ.get(STATE_ENV, str(Path.home() / '.local/state/webcodex-paper-studio/material-handoffs')))
    key = digest(encoded({'paper_root': str(paper)}))
    path = no_links(base / key / 'index.sqlite3')
    if path.is_relative_to(paper):
        fail('HANDOFF_CATALOG_PERMISSIONS', 'Material state cannot be located inside the paper editor workspace')
    return path


def exists(paper):
    return path_for(paper).exists()


def identity(paper, project_id):
    paper = no_links(Path(paper))
    contract = _project_metadata(paper / 'PROJECT.yaml')
    if (contract.get('project_id') != project_id
            or contract.get('paper_writing_root', str(paper)) != str(paper)):
        fail('HANDOFF_PROJECT_MISMATCH', 'Material catalog requires this physical project contract')
    return {'schema': SCHEMA, 'project_id': project_id, 'paper_root': str(paper)}


def _private(path, directory=False):
    no_links(path)
    info = path.stat()
    if ((directory and not stat.S_ISDIR(info.st_mode))
            or (not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1))
            or info.st_uid != os.getuid() or info.st_mode & 0o077):
        fail('HANDOFF_CATALOG_PERMISSIONS', 'Material catalog requires owned private native files and directories')


def _validate(row):
    if (not isinstance(row, dict) or set(row) != FIELDS
            or not isinstance(row['change_id'], str) or not LABEL.fullmatch(row['change_id'])
            or type(row['sequence']) is not int or row['sequence'] < 1
            or row['manifest_path'] != 'materials/handoffs/' + row['change_id'] + '/manifest.json'
            or not isinstance(row['manifest_sha256'], str) or not HASH.fullmatch(row['manifest_sha256'])
            or (row['previous_manifest_sha256'] is not None
                and (not isinstance(row['previous_manifest_sha256'], str)
                     or not HASH.fullmatch(row['previous_manifest_sha256'])))):
        fail('HANDOFF_CATALOG_INVALID', 'Indexed material row identity is invalid')
    return row


class Catalog:
    def __init__(self, connection):
        self.connection = connection

    def _row(self, saved):
        if saved is None:
            return None
        try:
            raw = saved['document'].encode()
            row = _validate(json.loads(raw))
            if digest(raw) != saved['digest']:
                raise ValueError('row digest')
            if any(row[k] != saved[k] for k in ('change_id', 'sequence', 'manifest_sha256')):
                raise ValueError('row key')
        except (ValueError, TypeError, KeyError):
            fail('HANDOFF_CATALOG_INVALID', 'Indexed material metadata changed')
        return row

    def by_id(self, change_id):
        if not isinstance(change_id, str) or not LABEL.fullmatch(change_id):
            fail('HANDOFF_INPUT_INVALID', 'A bounded exact material change_id is required')
        return self._row(self.connection.execute('SELECT * FROM entries WHERE change_id=?', (change_id,)).fetchone())

    def by_sha(self, manifest_sha256):
        if not isinstance(manifest_sha256, str) or not HASH.fullmatch(manifest_sha256):
            fail('HANDOFF_INPUT_INVALID', 'An exact material manifest SHA256 is required')
        return self._row(self.connection.execute('SELECT * FROM entries WHERE manifest_sha256=?', (manifest_sha256,)).fetchone())

    def latest(self):
        saved = self.connection.execute("SELECT value FROM meta WHERE key='latest'").fetchone()
        if saved is None:
            fail('HANDOFF_CATALOG_INVALID', 'Material catalog has no latest pointer')
        if saved['value'] == 'absent':
            return None
        row = self.by_sha(saved['value'])
        if row is None:
            fail('HANDOFF_CATALOG_INVALID', 'Material latest pointer has no indexed row')
        return row

    def predecessor(self, row):
        if row['sequence'] == 1:
            if row['previous_manifest_sha256'] is not None:
                fail('HANDOFF_CATALOG_INVALID', 'First material row has a predecessor')
            return None
        previous = self.by_sha(row['previous_manifest_sha256'])
        if previous is None or previous['sequence'] != row['sequence'] - 1:
            fail('HANDOFF_CATALOG_INVALID', 'Material direct predecessor identity is unavailable')
        return previous

    def append(self, row):
        row = _validate(row)
        previous = self.latest()
        if (row['sequence'] != (previous['sequence'] + 1 if previous else 1)
                or row['previous_manifest_sha256'] != (previous['manifest_sha256'] if previous else None)):
            fail('HANDOFF_PREDECESSOR_CONFLICT', 'Material predecessor changed before catalog commit')
        raw = encoded(row).decode()
        try:
            self.connection.execute('INSERT INTO entries VALUES(?,?,?,?,?)',
                (row['change_id'], row['sequence'], row['manifest_sha256'], raw, digest(raw.encode())))
            self.connection.execute("UPDATE meta SET value=? WHERE key='latest'", (row['manifest_sha256'],))
        except sqlite3.IntegrityError:
            fail('HANDOFF_IDEMPOTENCY_CONFLICT', 'Material catalog identity is already allocated')


@contextlib.contextmanager
def connect(paper, project_id, *, write=False):
    path = path_for(paper)
    if not path.is_file():
        fail('HANDOFF_CATALOG_REQUIRED', 'Enable the material metadata catalog explicitly before exact lookup')
    _private(path.parent, True); _private(path)
    expected = encoded(identity(paper, project_id)).decode()
    old_umask = os.umask(0o077)
    try:
        connection = sqlite3.connect(path if write else path.as_uri() + '?mode=ro', uri=not write, timeout=10)
    finally:
        os.umask(old_umask)
    connection.row_factory = sqlite3.Row
    try:
        saved = connection.execute("SELECT value FROM meta WHERE key='binding'").fetchone()
        if saved is None or saved['value'] != expected:
            fail('HANDOFF_PROJECT_MISMATCH', 'Material catalog belongs to another physical project')
        if write:
            connection.execute('BEGIN IMMEDIATE')
        yield Catalog(connection)
        if write:
            connection.commit()
    except sqlite3.DatabaseError:
        if write:
            connection.rollback()
        fail('HANDOFF_CATALOG_INVALID', 'Material metadata catalog could not be read safely')
    except BaseException:
        if write:
            connection.rollback()
        raise
    finally:
        connection.close()


@contextlib.contextmanager
def append_session(paper, project_id):
    if exists(paper):
        with connect(paper, project_id, write=True) as catalog:
            yield catalog
    else:
        yield None


def create(paper, project_id, rows, source_index_sha256, *, initial_plan_sha256):
    """Called only under the existing handoff lock by explicit initialization."""
    path = path_for(paper)
    if path.exists():
        fail('HANDOFF_CATALOG_EXISTS', 'A material catalog already exists; never replace its newer state')
    binding = encoded(identity(paper, project_id)).decode()
    # Native state uses private modes; no SQLite or sidecar is created in the
    # manuscript tree. A copied registry still fails its physical binding.
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, name = tempfile.mkstemp(prefix='.catalog-', suffix='.sqlite3', dir=path.parent)
    os.close(fd); stage = Path(name)
    connection = sqlite3.connect(stage)
    try:
        connection.executescript('''
          CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE entries(change_id TEXT PRIMARY KEY, sequence INTEGER UNIQUE NOT NULL,
            manifest_sha256 TEXT UNIQUE NOT NULL, document TEXT NOT NULL, digest TEXT NOT NULL);
        ''')
        connection.execute('INSERT INTO meta VALUES(?,?)', ('binding', binding))
        connection.execute('INSERT INTO meta VALUES(?,?)', ('legacy_index_sha256', source_index_sha256))
        connection.execute('INSERT INTO meta VALUES(?,?)', ('initial_plan_sha256', initial_plan_sha256))
        connection.execute('INSERT INTO meta VALUES(?,?)', ('latest', 'absent'))
        connection.row_factory = sqlite3.Row
        catalog = Catalog(connection)
        for row in rows:
            catalog.append(row)
        connection.commit(); connection.close(); connection = None
        os.chmod(stage, 0o600)
        with stage.open('rb') as stream:
            os.fsync(stream.fileno())
        if path.exists():
            fail('HANDOFF_CATALOG_EXISTS', 'Material catalog appeared while initializing')
        os.rename(stage, path)
    finally:
        if connection is not None:
            connection.close()
        if stage.exists():
            stage.unlink()
