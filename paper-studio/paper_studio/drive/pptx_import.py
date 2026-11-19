"""Exact-ID Drive intake for editable figure sources in the bound project."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import time
import uuid

from paper_studio.capabilities import require_edit_kind
from paper_studio.drive import publisher, review_import, target_display
from paper_studio.pptx.document import MAX_PACKAGE, _unzip, _validate_parts
from paper_studio.security.policy import (
    atomic_json, encoded, fail, locked, no_links, path_for, safe_read,
    relpath, sha, _rename_noreplace,
)

MIME = 'application/vnd.openxmlformats-officedocument.presentationml.presentation'
SCHEMA = 'paper-studio.pptx-drive-import.v1'


def _save_new(root, relative, raw):
    """Publish complete bytes without exposing a partial target or replacing it."""
    parts = relpath(relative).parts
    fds = []
    temporary = '.pptx-import-' + uuid.uuid4().hex
    created = False
    try:
        directory = os.open(no_links(Path(root)), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        fds.append(directory)
        for component in parts[:-1]:
            try:
                os.mkdir(component, mode=0o755, dir_fd=directory)
            except FileExistsError:
                pass
            directory = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            fds.append(directory)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        created = True
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        base = '/proc/self/fd/' + str(directory) + '/'
        if not _rename_noreplace(base + temporary, base + parts[-1]):
            os.link(temporary, parts[-1], src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
            os.unlink(temporary, dir_fd=directory)
        created = False
    finally:
        if created:
            os.unlink(temporary, dir_fd=directory)
        for fd in reversed(fds):
            os.close(fd)
    return {'path': relative, 'bytes': len(raw), 'sha256': sha(raw)}


def _destination(cfg, destination):
    path = path_for(cfg, destination, True)
    figures = PurePosixPath(cfg.get('execution_policy', {}).get('figure_root', 'candidates'))
    if path.suffix.lower() != '.pptx' or not PurePosixPath(destination).is_relative_to(figures):
        fail('WRITE_DENIED', 'PPTX intake requires a writable figure candidate path')
    return path


def _project_folder(cfg, drive, parent):
    root = publisher._drive_id(cfg.get('drive_folder_id'))
    seen = set()
    for _ in range(8):
        if parent in seen or parent == root:
            break
        seen.add(parent)
        folder = drive.private(publisher._drive_id(parent))
        if folder.get('mimeType') != target_display.FOLDER or len(folder.get('parents', [])) != 1:
            break
        if folder['parents'] == [root] and folder.get('name') == cfg['project_id']:
            return parent
        parent = folder['parents'][0]
    fail('PROJECT_DENIED', 'PPTX must be inside the existing project Publisher folder')


def _remote(cfg, drive, source):
    file_id = publisher._drive_id(source['file_id'])
    meta = drive.private(file_id)
    extra = drive.request('GET', 'drive/v3/files/' + file_id + '?fields=id,trashed,version')
    if extra.get('id') != file_id or extra.get('trashed') is not False:
        fail('DRIVE_SOURCE_INVALID', 'PPTX source is missing or trashed')
    meta.update(extra)
    try:
        size = int(meta.get('size', ''))
    except (ValueError, TypeError):
        size = 0
    if (meta.get('id') != file_id or meta.get('mimeType') != MIME
            or not isinstance(meta.get('name'), str) or not meta['name'].lower().endswith('.pptx')
            or not 0 < size <= MAX_PACKAGE
            or not re.fullmatch('[0-9a-f]{64}', str(meta.get('sha256Checksum', '')))
            or not re.fullmatch('[0-9a-f]{32}', str(meta.get('md5Checksum', '')))
            or not isinstance(meta.get('parents'), list) or len(meta['parents']) != 1
            or not isinstance(meta.get('version'), str)):
        fail('DRIVE_SOURCE_INVALID', 'Select one stored PPTX with bounded size and server checksums')
    if (source.get('parent_id', meta['parents'][0]) != meta['parents'][0]
            or source.get('expected_sha256', meta['sha256Checksum']) != meta['sha256Checksum']
            or source.get('expected_bytes', size) != size):
        fail('REVISION_CONFLICT', 'PPTX source differs from its expected Drive identity')
    project_folder = _project_folder(cfg, drive, meta['parents'][0])
    if source.get('project_folder_id', project_folder) != project_folder:
        fail('PROJECT_DENIED', 'PPTX differs from the expected project Publisher folder')
    target_display._parent(cfg, drive, project_folder, meta['parents'][0])
    return {key: meta[key] for key in (
        'id', 'name', 'mimeType', 'size', 'md5Checksum', 'sha256Checksum', 'parents', 'version'
    )}, project_folder


def import_pptx(cfg, args):
    """Plan or create one candidate, preserving bytes and retry provenance."""
    allowed = {'action', 'drive_source', 'destination', 'request_id', 'dry_run', 'authorized'}
    if set(args) - allowed or args.get('action') != 'import_drive':
        fail('INVALID_ARGUMENT', 'Drive intake accepts only source, destination and request identity')
    if (not isinstance(args.get('drive_source'), dict) or not args['drive_source'].get('file_id')
            or not isinstance(args.get('destination'), str) or not isinstance(args.get('request_id'), str)):
        fail('INVALID_ARGUMENT', 'Drive intake requires a source file ID, destination and request_id')
    require_edit_kind(cfg, 'create')
    if not cfg.get('drive_folder_id') or not cfg.get('drive_oauth_file'):
        fail('DRIVE_NOT_CONFIGURED', 'PPTX intake needs this project Publisher folder and OAuth reference')
    destination = args['destination']
    target = _destination(cfg, destination)
    source = args['drive_source']
    drive = target_display.client(cfg)
    meta, project_folder = _remote(cfg, drive, source)
    identity = {'project_id': cfg['project_id'], 'project_root': cfg['project_root'],
                'drive_root_id': cfg['drive_folder_id'], 'file_id': meta['id'],
                'project_folder_id': project_folder,
                'destination': destination, 'sha256': meta['sha256Checksum'], 'bytes': int(meta['size'])}
    anchor = sha(encoded(identity))
    private = no_links(Path(cfg['state_root']) / 'pptx-drive-import')
    object_path = private / 'objects' / (sha(encoded([cfg['project_root'], destination])) + '.json')
    request_id = args['request_id']
    if not re.fullmatch('[A-Za-z0-9_-]{1,64}', request_id):
        fail('INVALID_ARGUMENT', 'A stable 1..64 character request_id is required')
    request_path = private / 'requests' / (sha(request_id.encode()) + '.json')
    preview = {'schema': SCHEMA, 'project_id': cfg['project_id'], 'action': 'import_drive',
               'request_id': request_id, 'path': destination, 'sha256': identity['sha256'],
               'bytes': identity['bytes'], 'source': meta,
               'project_folder_id': project_folder, 'project_ancestry_verified': True,
               'official_publication': False}
    dry_run = args.get('dry_run', True)
    if not dry_run and args.get('authorized') is not True:
        fail('AUTHORIZATION_REQUIRED', 'PPTX intake apply requires authorized=true')
    with locked(cfg, 'pptx-drive-import'):
        previous = None
        for path in (request_path, object_path):
            if path.exists():
                record = json.loads(safe_read(path.parent, path.name, 64 * 1024))
                if (record.get('identity_sha256') != anchor or record.get('identity') != identity
                        or record.get('creation_conflict')):
                    fail('IDEMPOTENCY_CONFLICT', 'Request or candidate already belongs to another PPTX identity')
                previous = record
        if target.exists():
            if previous is None or previous.get('creation_attempted') is not True:
                fail('REVISION_CONFLICT', 'Candidate destination already exists independently')
            raw = safe_read(cfg['project_root'], destination, MAX_PACKAGE)
            if len(raw) != identity['bytes'] or sha(raw) != identity['sha256']:
                fail('REVISION_CONFLICT', 'Imported candidate has been edited locally')
            if not dry_run and not request_path.exists():
                publisher._private_state_directory(request_path.parent)
                atomic_json(request_path, {**previous, 'request_id': request_id}, True)
            return {**preview, 'dry_run': dry_run, 'reused': True,
                    'import_complete': True, 'local_readback_verified': True}
        if previous and previous.get('import_complete'):
            fail('REVISION_CONFLICT', 'Previously imported candidate was removed locally')
        if dry_run:
            return {**preview, 'dry_run': True, 'would_create': True, 'import_complete': False}
        drive.active_media_id = meta['id']
        try:
            raw, digest = review_import._download(drive, meta['id'], meta, identity['bytes'])
        finally:
            drive.active_media_id = None
        if digest != identity['sha256'] or hashlib.md5(raw).hexdigest() != meta['md5Checksum']:
            fail('DRIVE_READBACK_MISMATCH', 'Actual PPTX bytes differ from the selected Drive source')
        after, after_project = _remote(cfg, drive, source)
        if after != meta or after_project != project_folder:
            fail('REVISION_CONFLICT', 'PPTX source changed during download')
        budget = {'entries': 0, 'expanded': 0}
        try:
            _validate_parts(_unzip(raw, budget), budget)
        except (ValueError, UnicodeError) as error:
            fail('PPTX_SOURCE_INVALID', 'Drive PPTX failed the existing package validator', reason=str(error)[:300])
        receipt = {'schema': SCHEMA, 'identity': identity, 'identity_sha256': anchor,
                   'creation_attempted': True, 'import_complete': False, 'request_id': request_id,
                   'created_at': time.time()}
        for directory in (private, request_path.parent, object_path.parent):
            publisher._private_state_directory(directory)
        atomic_json(request_path, receipt)
        atomic_json(object_path, receipt)
        with locked(cfg):
            _destination(cfg, destination)
            try:
                copied = _save_new(cfg['project_root'], destination, raw)
            except FileExistsError:
                receipt['creation_conflict'] = True
                atomic_json(object_path, receipt)
                atomic_json(request_path, receipt)
                fail('REVISION_CONFLICT', 'Candidate destination already exists', path=destination)
            saved = safe_read(cfg['project_root'], destination, MAX_PACKAGE)
            if len(saved) != identity['bytes'] or sha(saved) != digest:
                fail('LOCAL_READBACK_MISMATCH', 'Saved candidate differs from downloaded PPTX')
        receipt.update(import_complete=True, local_readback_verified=True)
        atomic_json(object_path, receipt)
        atomic_json(request_path, receipt)
        return {**preview, **copied, 'dry_run': False, 'reused': False,
                'import_complete': True, 'local_readback_verified': True,
                'api_trace': target_display._trace(drive, 0)}
