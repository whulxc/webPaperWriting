"""Project-private job lookup, explicitly imported once before indexed operation.

Job files remain the evidence. This catalogue locates request IDs, blocking jobs
and completed Drive publications without walking retained build directories.
"""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid

from paper_studio.security.policy import builds_root, encoded, fail, no_links, safe_read, sha

SCHEMA = 'paper-studio.job-index.v1'
BUILD_ID = re.compile(r'rb_[a-f0-9]{24}\Z')
STATES = {'queued', 'running', 'completed', 'failed', 'recovery_required'}
ACTIVE = {'preparing', 'queued', 'running', 'recovery_required'}
OP_ACTIVE = ACTIVE | {'prepared','syncing','downloading','creating','patching'}
OP_TERMINAL = {'completed','failed','partial_sync_retained'}
OP_DIRS = {'sync_engine':'requests','review_import':'imports','legacy_review':'reviews'}


def enabled(cfg):
    return cfg.get('job_index', {}).get('enabled') is True


def validate_config(cfg):
    value = cfg.get('job_index')
    if not isinstance(value, dict) or set(value) != {'enabled'} or type(value['enabled']) is not bool:
        fail('CONFIG_INVALID', 'Job index requires an explicit enabled boolean')
    if Path(cfg['state_root']).is_relative_to(Path(cfg['project_root'])):
        fail('CONFIG_INVALID', 'Job index state must be outside the writable project')


def _identity(cfg):
    return encoded({'schema': SCHEMA, 'project_id': cfg['project_id'],
                    'project_root': str(no_links(Path(cfg['project_root']))),
                    'builds_root': str(no_links(builds_root(cfg))),
                    'state_root': str(no_links(Path(cfg['state_root'])))}).decode()


def path(cfg):
    return no_links(Path(cfg['state_root']) / 'job-index' / 'index.sqlite3')


def recovery_config(cfg, proofs=()):
    """Recover an omitted old-plan flag only from hash-pinned bound configs."""
    if 'job_index' not in cfg and path(cfg).exists():
        fields=('project_id','project_root','artifact_root','state_root','artifact_layout')
        if any(enabled(proof) and all(proof.get(key)==cfg.get(key) for key in fields) for proof in proofs):
            return {**cfg,'job_index':{'enabled':True}}
        fail('ROLLBACK_INDEX_POLICY_REQUIRED','Older recovery record has no pinned indexed-writer policy; reconcile the stopped project before recovery')
    return cfg


def _open(cfg, *, create=False):
    if not enabled(cfg):
        fail('JOB_INDEX_DISABLED', 'This project has not enabled indexed job discovery')
    validate_config(cfg)
    p = path(cfg)
    if create:
        p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not p.is_file() and not create:
        fail('JOB_INDEX_NOT_READY', 'Explicitly import the project job history before indexed operation')
    if p.exists():
        info = p.stat()
        if info.st_nlink != 1 or info.st_mode & 0o077:
            fail('JOB_INDEX_UNSAFE', 'Job index must be a private regular single-link file')
    connection = None
    try:
        connection = sqlite3.connect(p, timeout=10)
        connection.row_factory = sqlite3.Row
        if create:
            os.chmod(p, 0o600)
            connection.executescript('''
              CREATE TABLE IF NOT EXISTS metadata(singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                identity TEXT NOT NULL, ready INTEGER NOT NULL, imported_at REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS jobs(build_id TEXT PRIMARY KEY, request_id TEXT UNIQUE,
                request_sha256 TEXT, kind TEXT NOT NULL, status TEXT NOT NULL,
                blocking INTEGER NOT NULL, created_at REAL NOT NULL, finished_at REAL NOT NULL,
                publication_version TEXT, manifest_sha256 TEXT, upload_complete INTEGER NOT NULL,
                status_sha256 TEXT);
              CREATE INDEX IF NOT EXISTS jobs_active ON jobs(blocking, created_at, build_id);
              CREATE INDEX IF NOT EXISTS jobs_publication ON jobs(kind, publication_version,
                manifest_sha256, upload_complete, finished_at DESC, build_id DESC);
              CREATE TABLE IF NOT EXISTS operations(namespace TEXT NOT NULL, operation_id TEXT NOT NULL,
                record_name TEXT NOT NULL, status TEXT NOT NULL, blocking INTEGER NOT NULL,
                PRIMARY KEY(namespace,operation_id));
              CREATE INDEX IF NOT EXISTS operations_active ON operations(namespace,blocking,operation_id);
            ''')
            connection.execute('INSERT OR IGNORE INTO metadata VALUES(1,?,0,?)', (_identity(cfg), time.time()))
            connection.commit()
        meta = connection.execute('SELECT identity,ready FROM metadata WHERE singleton=1').fetchone()
        if meta is None or meta['identity'] != _identity(cfg):
            fail('PROJECT_DENIED', 'Job index belongs to another project or physical job store')
        if not create and meta['ready'] != 1:
            fail('JOB_INDEX_NOT_READY', 'The explicit history import is incomplete')
        return connection
    except (sqlite3.Error, OSError):
        if connection is not None:
            connection.close()
        fail('JOB_INDEX_UNAVAILABLE', 'The project job index cannot be read safely')
    except BaseException:
        if connection is not None:
            connection.close()
        raise


@contextlib.contextmanager
def _db(cfg, *, create=False):
    db = _open(cfg, create=create)
    try:
        yield db
        db.commit()
    except sqlite3.Error:
        db.rollback()
        fail('JOB_INDEX_UNAVAILABLE', 'The project job index transaction failed')
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def reserve(cfg, build_id, request_id, request_sha256, kind, created_at):
    if not enabled(cfg):
        return
    with _db(cfg) as db:
        db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                   (build_id, request_id, request_sha256, kind, 'preparing', 1, created_at,
                    0, None, None, 0, None))


def _record(cfg, build_id, value):
    if (not BUILD_ID.fullmatch(build_id) or not isinstance(value, dict)
            or value.get('build_id', build_id) != build_id or value.get('project_id') != cfg['project_id']
            or value.get('status') not in STATES):
        fail('BUILD_RECORD_CORRUPT', 'Job index received an invalid project job state', build_id=build_id)
    result = value.get('result', {})
    if not isinstance(result, dict):
        fail('BUILD_RECORD_CORRUPT', 'Job result must be an object', build_id=build_id)
    blocking = value['status'] in ACTIVE or value.get('recovery_required') is True
    return (value.get('kind', 'unknown'), value['status'], int(blocking),
            value.get('created_at', 0), value.get('finished_at', 0),
            result.get('version'), result.get('version_manifest_sha256'),
            int(result.get('upload_complete') is True), sha(encoded(value)))


def observe(cfg, build_id, value):
    """Record a status after its atomic file write; stale active rows stay safe."""
    if not enabled(cfg):
        return
    row = _record(cfg, build_id, value)
    with _db(cfg) as db:
        known = db.execute('SELECT 1 FROM jobs WHERE build_id=?', (build_id,)).fetchone()
        if known is None:
            fail('JOB_INDEX_INCOMPLETE', 'The selected job is absent from the imported index', build_id=build_id)
        db.execute('UPDATE jobs SET kind=?,status=?,blocking=?,created_at=?,finished_at=?,publication_version=?, '
                   'manifest_sha256=?,upload_complete=?,status_sha256=? WHERE build_id=?', (*row, build_id))


def find_request(cfg, request_id):
    with _db(cfg) as db:
        row = db.execute('SELECT build_id,request_sha256 FROM jobs WHERE request_id=?', (request_id,)).fetchone()
        return dict(row) if row else None


def active_ids(cfg):
    with _db(cfg) as db:
        return [row['build_id'] for row in db.execute(
            'SELECT build_id FROM jobs WHERE blocking=1 ORDER BY created_at,build_id')]


def publication(cfg, version, manifest_sha256):
    """Locate one publication; its original status, receipt and PDFs remain verified."""
    with _db(cfg) as db:
        row = db.execute('SELECT build_id FROM jobs WHERE kind=? AND publication_version=? '
                         'AND manifest_sha256=? AND upload_complete=1 AND status=? '
                         'ORDER BY finished_at DESC,build_id DESC LIMIT 1',
                         ('drive', version, manifest_sha256, 'completed')).fetchone()
        if row is None:
            fail('REVIEW_PUBLICATION_REQUIRED', 'Version has no completed hash-bound Drive publication')
        return row['build_id']


def _operation(namespace, operation_id, record_name, state):
    if namespace not in set(OP_DIRS) | {'sync_task'} or not isinstance(operation_id,str) or not operation_id:
        fail('JOB_INDEX_ARGUMENT','Invalid indexed operation identity')
    if state not in OP_ACTIVE | OP_TERMINAL:
        fail('JOB_INDEX_INCOMPLETE','Operation has an unclassified lifecycle state')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}(?:\.json)?',record_name):
        fail('JOB_INDEX_ARGUMENT','Invalid indexed operation record name')
    return (namespace,operation_id,record_name,state,int(state in OP_ACTIVE))


def track_operation(cfg, namespace, operation_id, record_name, state):
    if not enabled(cfg):
        return
    row = _operation(namespace,operation_id,record_name,state)
    with _db(cfg) as db:
        db.execute('INSERT INTO operations VALUES(?,?,?,?,?) ON CONFLICT(namespace,operation_id) '
                   'DO UPDATE SET record_name=excluded.record_name,status=excluded.status,blocking=excluded.blocking',row)


def operation_ids(cfg,namespace):
    with _db(cfg) as db:
        return [dict(row) for row in db.execute('SELECT operation_id,record_name FROM operations '
                'WHERE namespace=? AND blocking=1 ORDER BY operation_id',(namespace,))]


def sync_activity(cfg):
    """Reconcile only active engine/intake receipts at their exact saved paths."""
    out=[]
    for namespace,directory in OP_DIRS.items():
        for row in operation_ids(cfg,namespace):
            p=no_links(Path(cfg['state_root'])/'drive-source-sync'/directory/row['record_name'])
            value=json.loads(safe_read(p.parent,p.name,8*1024**2))
            if value.get('project_id')!=cfg['project_id'] or value.get('request_id',row['operation_id'])!=row['operation_id']:
                fail('PROJECT_DENIED','Indexed operation belongs to another project or request')
            track_operation(cfg,namespace,row['operation_id'],row['record_name'],value.get('status'))
            if value['status'] in OP_ACTIVE:
                out.append({'project_id':cfg['project_id'],'kind':namespace,'status':value['status'],
                            'operation_id':row['operation_id']})
    return out


def import_history(cfg, *, authorized=False, reconcile=False):
    """One explicit management scan under routing/build locks, safe to rerun.

    The complete import commits as one transaction. A first-import crash leaves
    ready=false. Reconciliation keeps a backup and rolls back to the prior
    catalogue on failure. The caller must hold the routing lock and stop any
    old/unindexed writers until the indexed runtime is active. A ready catalogue
    may be reused only when every intervening writer maintained this index.
    """
    if authorized is not True:
        fail('AUTHORIZATION_REQUIRED', 'History indexing requires an explicit local management action')
    from paper_studio.security.policy import locked
    with locked(cfg, 'build'):
        with _db(cfg, create=True) as db:
            ready=db.execute('SELECT ready FROM metadata WHERE singleton=1').fetchone()[0] == 1
            if ready and not reconcile:
                return {'project_id': cfg['project_id'], 'status': 'ready', 'reused': True,
                        'jobs': db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 'directories_enumerated': 0}
            archived=None
            if ready:
                archives=path(cfg).parent/'archives'
                archives.mkdir(mode=0o700,exist_ok=True)
                archived=no_links(archives/('before-reconcile-'+uuid.uuid4().hex+'.sqlite3'))
                with sqlite3.connect(archived) as previous:
                    db.backup(previous)
                os.chmod(archived,0o600)
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM jobs')
            db.execute('DELETE FROM operations')
            count = 0
            for job in sorted(builds_root(cfg).glob('rb_*')):
                no_links(job)
                if not job.is_dir() or not BUILD_ID.fullmatch(job.name):
                    fail('BUILD_RECORD_CORRUPT', 'History contains an unclassified build directory')
                raw = safe_read(job, 'status.json', 8 * 1024**2)
                try:
                    value = json.loads(raw)
                except ValueError:
                    fail('BUILD_RECORD_CORRUPT', 'History status requires local evidence review', build_id=job.name)
                fields = _record(cfg, job.name, value)
                request_id, request_sha256 = None, None
                if (job/'request.json').exists():
                    try:
                        request = json.loads(safe_read(job, 'request.json', 8 * 1024**2))
                    except ValueError:
                        fail('BUILD_RECORD_CORRUPT', 'History request requires local evidence review', build_id=job.name)
                    if (not isinstance(request, dict) or request.get('project_id', cfg['project_id']) != cfg['project_id']
                            or request.get('build_id', job.name) != job.name or not isinstance(request.get('request'), dict)):
                        fail('BUILD_RECORD_CORRUPT', 'Historical request identity differs', build_id=job.name)
                    request_id = request['request'].get('request_id')
                    request_sha256 = request.get('request_sha256')
                db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                           (job.name, request_id, request_sha256, *fields))
                count += 1
            operations = 0
            private = no_links(Path(cfg['state_root'])/'drive-source-sync')
            for namespace,directory in OP_DIRS.items():
                for p in sorted((private/directory).glob('*.json')):
                    value=json.loads(safe_read(p.parent,p.name,8*1024**2))
                    if value.get('project_id')!=cfg['project_id']:
                        fail('PROJECT_DENIED','Imported operation belongs to another project')
                    operation_id=value.get('request_id',p.stem)
                    db.execute('INSERT INTO operations VALUES(?,?,?,?,?)',
                               _operation(namespace,operation_id,p.name,value.get('status')))
                    operations += 1
            from paper_studio.drive import sync_tasks
            for p in sorted((private/'tasks').glob('*/request.json')):
                record=sync_tasks._task_record(cfg,p.parent)
                value=sync_tasks.read_task(cfg,record['request_id'])
                db.execute('INSERT INTO operations VALUES(?,?,?,?,?)',
                           _operation('sync_task',record['request_id'],p.parent.name,value['status']))
                operations += 1
            db.execute('UPDATE metadata SET ready=1,imported_at=? WHERE singleton=1', (time.time(),))
            return {'project_id': cfg['project_id'], 'status': 'ready', 'reused': False,
                    'jobs': count, 'operations':operations,'directories_enumerated': 5,
                    'reconciled':ready,'previous_index_backup':str(archived) if archived else None}
