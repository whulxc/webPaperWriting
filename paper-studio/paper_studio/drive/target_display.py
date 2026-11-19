"""Local exact-ID preview delivery, separate from official archive publication.

The caller supplies a trusted project folder, an existing exact parent, an
indexed source identity and an explicit checksum. This module never discovers
files or folders by name, lists a collection, edits a remote object, or grants
sharing. It is a local maintenance API, not an arbitrary-file Chat tool.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import time
import urllib.parse

from paper_studio.drive import publisher
from paper_studio.security.policy import StudioError, atomic_json, encoded, fail, no_links, project, safe_read, sha

SCHEMA = 'paper-studio.target-display-receipt.v1'
MAX_BYTES = 64 * 1024 * 1024
DISPLAY_ROLES = {'manuscript_pdf','figure_pdf','table_pdf','figure_source','table_source','named_source','review','provenance'}
LANGUAGES = {'en', 'zh', 'shared'}
FOLDER = 'application/vnd.google-apps.folder'
PROGRESS_FIELDS = {'id', 'name', 'status', 'bytes', 'sha256', 'confirmed_bytes', 'requests',
                   'wire_bytes', 'session_transport_recoveries', 'resumed_existing_session',
                   'new_session_started', 'network_events', 'error_code', 'error_details'}


def _need(value, code, message):
    if not value:
        fail(code, message)


class ExactIdDrive(publisher.RecoveryDrive):
    """Enforce the no-list/no-search boundary at the actual HTTP exit."""

    def __init__(self, *args, **kwargs):
        self.trace = []
        self.active_file_id = None
        self.active_media_id = None
        super().__init__(*args, **kwargs)

    def refresh(self):
        self.trace.append({'method': 'POST', 'operation': 'oauth_refresh', 'target_id': 'oauth2.googleapis.com'})
        return super().refresh()

    def exchange(self, method, url, payload=None, headers=None, allowed=(200, 201)):
        parsed = urllib.parse.urlsplit(url)
        _need(parsed.scheme == 'https' and parsed.netloc == 'www.googleapis.com'
              and not parsed.fragment, 'TARGET_API_DENIED', 'Only the fixed Google Drive origin is allowed')
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        match = re.fullmatch(r'/drive/v3/files/([A-Za-z0-9_-]{1,256})', parsed.path)
        if method == 'GET' and parsed.path == '/drive/v3/files/generateIds':
            _need(query == {'count': ['1'], 'space': ['drive'], 'type': ['files']},
                  'TARGET_API_DENIED', 'Only one durable file ID may be reserved')
            operation, target = 'reserve_file_id', 'generateIds'
        elif method == 'GET' and match:
            target = match.group(1)
            if query == {'alt': ['media']}:
                ranges = [value for key, value in (headers or {}).items() if key.lower() == 'range']
                bound = re.fullmatch(r'bytes=(\d+)-(\d+)', ranges[0]) if len(ranges) == 1 else None
                _need(getattr(self, 'active_media_id', None) == target and bound
                      and 0 <= int(bound[1]) <= int(bound[2])
                      and int(bound[2]) - int(bound[1]) < publisher.CHUNK_BYTES,
                      'TARGET_API_DENIED', 'Media reads require an admitted exact ID and bounded byte range')
                operation = 'file_media_range'
            else:
                _need(set(query) <= {'fields'}, 'TARGET_API_DENIED', 'Only exact-ID metadata is allowed')
                operation = 'file_metadata'
        elif method == 'POST' and parsed.path == '/upload/drive/v3/files':
            _need(self.active_file_id and query.get('uploadType') in (['multipart'], ['resumable'])
                  and set(query) <= {'uploadType', 'fields'},
                  'TARGET_API_DENIED', 'Upload needs one active preallocated file identity')
            operation, target = 'upload_create', self.active_file_id
        elif method == 'PUT' and parsed.path == '/upload/drive/v3/files':
            publisher.session_url(url)
            _need(self.active_file_id, 'TARGET_API_DENIED', 'Upload needs one active file identity')
            content_range = next((value for key, value in (headers or {}).items()
                                  if key.lower() == 'content-range'), '')
            operation = 'upload_status' if content_range.startswith('bytes */') else 'upload_chunk'
            target = self.active_file_id
        else:
            fail('TARGET_API_DENIED', 'Collection listing/search and remote edits are not allowed')
        event = {'method': method, 'operation': operation, 'target_id': target}
        if operation == 'file_media_range':
            event.update(byte_start=int(bound[1]), byte_end=int(bound[2]))
        self.trace.append(event)
        try:
            status, response_headers, raw = super().exchange(method, url, payload, headers, allowed)
            event['http_status'] = status
            return status, response_headers, raw
        except StudioError as error:
            event['error_code'] = error.code
            if type(error.details.get('http_status')) is int:
                event['http_status'] = error.details['http_status']
            raise


def client(cfg):
    _need(cfg.get('drive_oauth_file'), 'OAUTH_REQUIRED', 'The project needs its existing shared OAuth reference')
    return ExactIdDrive(cfg['drive_oauth_file'], persist_access_token=False, deadline=time.monotonic() + 600)


def _identity(project_id, parent_id, name, digest, size, role, language):
    publisher._drive_id(parent_id)
    _need(isinstance(name, str) and 0 < len(name) <= 200 and '/' not in name and '\\' not in name
          and not any(ord(character) < 32 for character in name), 'INVALID_ARGUMENT', 'A bounded basename is required')
    _need(isinstance(digest, str) and re.fullmatch('[0-9a-f]{64}', digest)
          and type(size) is int and 0 < size <= MAX_BYTES,
          'INVALID_ARGUMENT', 'An exact bounded SHA256 and byte count are required')
    _need(role in DISPLAY_ROLES and language in LANGUAGES, 'INVALID_ARGUMENT', 'Explicit figure/paper role and language are required')
    mime = mimetypes.guess_type(name)[0]
    _need(mime in {'application/pdf', 'image/png', 'image/svg+xml'}
          and (role not in {'manuscript_pdf', 'figure_pdf', 'table_pdf'} or mime == 'application/pdf'),
          'INVALID_ARGUMENT', 'File type does not match its indexed display role')
    return {'project_id': project_id, 'parent_id': parent_id, 'name': name, 'sha256': digest,
            'bytes': size, 'role': role, 'language': language, 'mime_type': mime}


def _parent(cfg, drive, project_folder_id, parent_id):
    root = publisher._drive_id(cfg.get('drive_folder_id'))
    publisher._drive_id(project_folder_id)
    configured = drive.private(root)
    folder = drive.private(project_folder_id)
    _need(configured.get('id') == root and configured.get('mimeType') == FOLDER
          and folder.get('id') == project_folder_id
          and folder.get('name') == cfg['project_id'] and folder.get('mimeType') == FOLDER
          and folder.get('parents') == [root],
          'TARGET_PROJECT_DENIED', 'Exact project folder differs from the configured review-root identity')
    seen = set()
    current = parent_id
    for _ in range(8):
        if current == project_folder_id:
            return
        _need(current not in seen, 'TARGET_PROJECT_DENIED', 'Target folder ancestry is cyclic')
        seen.add(current)
        meta = drive.private(publisher._drive_id(current))
        parents = meta.get('parents', [])
        _need(meta.get('id') == current and meta.get('mimeType') == FOLDER and len(parents) == 1,
              'TARGET_PROJECT_DENIED', 'Target is not a private single-parent project folder')
        current = parents[0]
    fail('TARGET_PROJECT_DENIED', 'Target is outside the bounded exact project ancestry')


def _trace(drive, start):
    """Copy this operation's evidence without truncating the total audit."""
    return [dict(event) for event in getattr(drive, 'trace', [])[start:]]


def _receipt(drive, expected, file_id, *, source_identity=None, trace_start=0):
    publisher._drive_id(file_id)
    meta = drive.private(file_id)
    _need(meta.get('id') == file_id and meta.get('name') == expected['name']
          and meta.get('mimeType') == expected['mime_type']
          and meta.get('sha256Checksum') == expected['sha256']
          and str(meta.get('size')) == str(expected['bytes'])
          and meta.get('parents') == [expected['parent_id']],
          'TARGET_IDENTITY_CHANGED', 'Exact Drive object does not match the expected file identity')
    source_identity = _source_identity(expected, source_identity)
    return {'schema': SCHEMA, **expected, 'file_id': file_id,
            'metadata_verified': True, 'server_sha256_verified': True,
            'expected_identity_verified': True, 'owner_only_verified': True,
            'verified_at': time.time(), 'url': meta.get('webViewLink'),
            'source_identity': source_identity,
            **{key: source_identity[key] for key in ('figure_number', 'target_key')
               if source_identity is not None and key in source_identity},
            'api_trace': _trace(drive, trace_start),
            'official_publication': False, 'chat_readback_verified': False}


def _source_identity(expected, source_identity):
    if source_identity is None:
        return None
    _need(isinstance(source_identity, dict) and type(source_identity.get('bytes')) is int
          and all(source_identity.get(key) == expected[key]
          for key in ('role', 'language', 'sha256', 'bytes')),
          'TARGET_SOURCE_MISMATCH', 'Indexed source identity differs from the exact display identity')
    from paper_studio.versions.target_index import _selector
    try:
        _selector(expected['role'], expected['language'],
                  source_identity.get('figure_number'), source_identity.get('target_key'))
    except StudioError:
        fail('TARGET_SOURCE_MISMATCH', 'Display identity must match its exact indexed selector')
    return json.loads(json.dumps(source_identity))


def verify_exact(cfg, *, project_id, project_folder_id, parent_id, file_id, name,
                 sha256, bytes, role, language, source_identity=None, drive=None):
    """GET only: verify a previously known ID; no lookup by name or checksum."""
    project(cfg, project_id)
    expected = _identity(project_id, parent_id, name, sha256, bytes, role, language)
    expected['project_folder_id'] = publisher._drive_id(project_folder_id)
    source_identity = _source_identity(expected, source_identity)
    publisher._drive_id(file_id)
    trace_start = len(getattr(drive, 'trace', [])) if drive is not None else 0
    drive = drive or client(cfg)
    _parent(cfg, drive, project_folder_id, parent_id)
    return _receipt(drive, expected, file_id, source_identity=source_identity, trace_start=trace_start)


def download_exact(cfg, *, project_id, project_folder_id, parent_id, file_id, name,
                   sha256, bytes, role, language, source_identity=None, drive=None):
    """Hash actual bounded media bytes from one verified ID; never list/search.

    Raw bytes remain in bounded memory, not tool output. Each range has at most
    three transient read attempts under the same existing client deadline.
    """
    project(cfg, project_id)
    _source_identity(_identity(project_id, parent_id, name, sha256, bytes, role, language), source_identity)
    publisher._drive_id(file_id)
    publisher._drive_id(project_folder_id)
    trace_start = len(getattr(drive, 'trace', [])) if drive is not None else 0
    drive = drive or client(cfg)
    arguments = dict(project_id=project_id, project_folder_id=project_folder_id,
                     parent_id=parent_id, file_id=file_id, name=name, sha256=sha256,
                     bytes=bytes, role=role, language=language, source_identity=source_identity, drive=drive)
    before = verify_exact(cfg, **arguments)
    digest, count, chunks, retries = hashlib.sha256(), 0, 0, 0
    drive.active_media_id = file_id
    try:
        for offset in range(0, bytes, publisher.CHUNK_BYTES):
            end = min(bytes, offset + publisher.CHUNK_BYTES) - 1
            for attempt in range(3):
                try:
                    status, headers, raw = drive.exchange('GET', publisher.API_ROOT + 'drive/v3/files/'
                        + publisher._drive_id(file_id) + '?alt=media',
                        headers={'Range': f'bytes={offset}-{end}'}, allowed=(200, 206))
                    break
                except StudioError as error:
                    code = error.details.get('http_status')
                    transient = error.code == 'DRIVE_INTERRUPTED' or (error.code == 'DRIVE_HTTP_ERROR'
                        and type(code) is int and (code == 429 or 500 <= code <= 599))
                    if not transient or attempt == 2:
                        raise
                    delay = 0.25 * (2 ** attempt)
                    drive.timeout(delay)
                    time.sleep(delay)
                    retries += 1
            content_range = next((value for key, value in headers.items() if key.lower() == 'content-range'), None)
            _need(len(raw) == end - offset + 1 and ((status == 206
                  and content_range == f'bytes {offset}-{end}/{bytes}')
                  or (status == 200 and offset == 0 and end + 1 == bytes)),
                  'TARGET_MEDIA_RANGE', 'Drive returned a different media range or byte count')
            digest.update(raw)
            count += len(raw)
            chunks += 1
    finally:
        drive.active_media_id = None
    _need(count == bytes and digest.hexdigest() == sha256, 'TARGET_MEDIA_CHANGED',
          'Actual media bytes differ from the approved source identity')
    after = verify_exact(cfg, **arguments)
    _need(all(before[key] == after[key] for key in ('file_id', 'sha256', 'bytes', 'parent_id', 'mime_type')),
          'TARGET_MEDIA_CHANGED', 'Exact media identity changed during download')
    return {**after, 'media_readback': {'actual_sha256': digest.hexdigest(), 'actual_bytes': count,
            'media_bytes_verified': True, 'chunks': chunks, 'transient_read_retries': retries,
            'raw_media_in_tool_output': False}, 'api_trace': _trace(drive, trace_start)}


@contextlib.contextmanager
def _locked_journal(path):
    path = no_links(Path(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = no_links(path.with_name(path.name + '.lock'))
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fail('TARGET_BUSY', 'This exact delivery journal is already in use')
        yield path
    finally:
        os.close(fd)


def publish_exact(cfg, *, project_id, project_folder_id, parent_id, source_path, name,
                  sha256, bytes, role, language, journal_path, source_identity=None,
                  authorized=False, drive=None):
    """Create at most one exact-ID display object, never a formal paper version.

    Journal contains no transport capability. A crash with an unknown incomplete
    attempt remains blocked; a completed object can be safely adopted by its own
    preallocated ID. In-process interruptions use existing same-session recovery.
    """
    _need(authorized is True, 'AUTHORIZATION_REQUIRED', 'Exact display upload requires explicit local authorization')
    project(cfg, project_id)
    expected = _identity(project_id, parent_id, name, sha256, bytes, role, language)
    expected['project_folder_id'] = publisher._drive_id(project_folder_id)
    source_identity = _source_identity(expected, source_identity)
    path = no_links(Path(source_path))
    root = no_links(Path(cfg['project_root']))
    if not path.is_relative_to(root):
        # Older projects store native outputs in their own configured artifact root.
        # Only the completed job's declared primary PDF can be read there.
        _need(role == 'review' and source_identity and source_identity.get('build_id'),
              'PATH_DENIED', 'Display source must belong to this project')
        primary, artifact = review_build_pdf(cfg, source_identity['build_id'])
        _need(path == primary and artifact['sha256'] == sha256 and artifact['bytes'] == bytes,
              'PATH_DENIED', 'External review source must be the selected native main PDF')
        root = primary.parent
    raw = safe_read(root, path.relative_to(root).as_posix(), MAX_BYTES)
    _need(len(raw) == bytes and sha(raw) == sha256, 'REVISION_CONFLICT', 'Display source differs from the approved identity')
    plan = {'expected': expected, 'source_path': str(path), 'source_identity': source_identity,
            'project_folder_id': project_folder_id}
    plan_sha = sha(encoded(plan))
    with _locked_journal(journal_path) as journal_path:
        if journal_path.exists():
            journal = json.loads(safe_read(journal_path.parent, journal_path.name, 2 * 1024 * 1024))
            _need(journal.get('plan_sha256') == plan_sha, 'IDEMPOTENCY_CONFLICT', 'Journal belongs to another exact display identity')
        else:
            journal = {'schema': 'paper-studio.target-display-journal.v1', 'plan_sha256': plan_sha,
                       'plan': plan, 'attempted': False}
            atomic_json(journal_path, journal, exclusive=True)
        def persist():
            atomic_json(journal_path, journal)
        trace_start = len(getattr(drive, 'trace', [])) if drive is not None else 0
        drive = drive or client(cfg)
        _parent(cfg, drive, project_folder_id, parent_id)
        if not journal.get('file_id'):
            ids = drive.request('GET', 'drive/v3/files/generateIds?count=1&space=drive&type=files').get('ids')
            _need(isinstance(ids, list) and len(ids) == 1, 'DRIVE_PROTOCOL', 'One exact generated file ID is required')
            journal['file_id'] = publisher._drive_id(ids[0])
            persist()
        file_id = publisher._drive_id(journal['file_id'])
        try:
            receipt = _receipt(drive, expected, file_id, source_identity=source_identity, trace_start=trace_start)
        except StudioError as error:
            if error.code != 'DRIVE_HTTP_ERROR' or error.details.get('http_status') != 404:
                raise
            _need(not journal['attempted'], 'TARGET_UPLOAD_UNCERTAIN', 'Reserved ID is absent after an attempted upload; no new session was created')
            journal['attempted'] = True
            persist()
            def progress(value):
                journal['progress'] = {key: value[key] for key in PROGRESS_FIELDS if key in value}
                persist()
            drive.active_file_id = file_id
            try:
                result = drive.upload(path, name, parent_id, progress=progress, expected_sha256=sha256,
                                      expected_bytes=bytes, preallocated_file_id=file_id)
                _need(result.get('id') == file_id, 'TARGET_IDENTITY_CHANGED', 'Upload returned another file identity')
            finally:
                drive.active_file_id = None
            receipt = _receipt(drive, expected, file_id, source_identity=source_identity, trace_start=trace_start)
        journal.update(status='completed', receipt=receipt)
        persist()
        return receipt


def review_build_pdf(cfg, build_id):
    """Resolve one completed build's primary PDF using this project's job store."""
    from paper_studio.provider.jobs import job_path
    job = job_path(cfg, build_id)
    status = json.loads(safe_read(job, 'status.json', 8 * 1024**2))
    _need(status.get('project_id') == cfg['project_id'] and status.get('build_id') == build_id
          and status.get('status') == 'completed' and status.get('kind') == 'tex',
          'BUILD_NOT_COMPLETE', 'Select a completed TeX build in the bound project')
    manifest = safe_read(job, 'manifest.json', 8 * 1024**2)
    _need(sha(manifest) == status.get('manifest_sha256'), 'ARTIFACT_TAMPERED', 'Build manifest changed')
    record = json.loads(manifest)
    _need(record.get('project_id') == cfg['project_id'] and record.get('build_id') == build_id
          and record.get('kind') == 'tex', 'ARTIFACT_TAMPERED', 'Build manifest identity changed')
    result = record.get('result', {})
    name = result.get('pdf_path')
    from paper_studio.security.policy import relpath
    relpath(name)
    match = next((item for item in record.get('artifacts', []) if item['path'] == name), {})
    _need(match.get('mime_type') == 'application/pdf' and name.lower().endswith('.pdf')
          and match.get('sha256') == result.get('pdf_sha256'),
          'ARTIFACT_TAMPERED', 'The build did not declare this primary PDF')
    return no_links(job / 'output' / name), match


def _review_language(cfg, request):
    """Use server-controlled bindings and names, never the display argument."""
    tex_path = request['tex_path']
    managed = request.get('_version_binding', {}).get('language')
    candidates = {managed} if managed is not None else set()
    targets = cfg.get('execution_policy', {}).get('tex_targets', {})
    mapping = cfg.get('version_management', {}).get('targets', {})
    names = {name for name, target in targets.items() if target.get('tex_path') == tex_path}
    if managed in mapping:
        names.add(mapping[managed])
    candidates.update(lang for lang, name in mapping.items() if name in names)
    for name in names:
        candidates.update(re.findall(r'(?:^|_)(en|zh)(?=_|$)', name))
        if targets[name].get('language') is not None:
            candidates.add(targets[name]['language'])
    if request.get('_review_binding'):
        from paper_studio.versions.review_proposals import _target_record
        selected = _target_record(cfg, tex_path)
        if selected is not None:
            candidates.add(selected[2])
    if cfg.get('chapter_workflow'):
        from pathlib import PurePosixPath
        from paper_studio.chapters.workflow import read_revision
        path, root = PurePosixPath(tex_path), PurePosixPath(cfg['chapter_workflow']['root'])
        if path.is_relative_to(root) and len(path.relative_to(root).parts) == 3:
            section, revision, filename = path.relative_to(root).parts
            _, document, _ = read_revision(cfg, section, revision)
            candidates.update(lang for lang, item in document['languages'].items() if item['source'] == filename)
    _need(len(candidates) <= 1, 'LANGUAGE_IDENTITY_CONFLICT', 'Trusted review language bindings disagree')
    _need(len(candidates) == 1 and candidates <= {'en', 'zh'}, 'LANGUAGE_IDENTITY_REQUIRED',
          'Clean review needs one consistent trusted language binding')
    return candidates.pop()


def review_build_source(cfg, build_id, language, *, current=False, historical_source=None):
    """Bind one marked or controlled clean TeX PDF to its source and language."""
    from paper_studio.provider import jobs
    from paper_studio.versions import current_inputs
    job = jobs.job_path(cfg, build_id)
    record = json.loads(safe_read(job, 'request.json', 4 * 1024**2))
    request = record.get('request', {})
    _need(isinstance(request, dict) and record.get('project_id') == cfg['project_id']
          and record.get('build_id') == build_id and record.get('kind') == 'tex'
          and record.get('prepared_request_sha256') == sha(encoded(request)),
          'REVIEW_BUILD_REQUIRED', 'Select a source-bound TeX build of the bound project')
    review_build_pdf(cfg, build_id)
    tex_path = request.get('tex_path', '')
    marked = bool(request.get('_text_review')) or str(tex_path).endswith('.review.tex')
    if marked:
        _need(isinstance(request.get('_text_review'), dict) and request['_text_review']
              and isinstance(tex_path, str) and tex_path.endswith('.review.tex'),
              'REVIEW_BUILD_REQUIRED', 'Marked reviews require their registered review entry')
        status = jobs.read_status(cfg, build_id)
        manifest = json.loads(safe_read(job, 'manifest.json', 8 * 1024**2))
        current_inputs._verify_record_identities(cfg, record, manifest, status)
        jobs.verify_snapshot(job, record)
        snapshot = record.get('snapshot', {})
        _need(isinstance(snapshot.get('files'), list)
              and snapshot.get('sha256') == sha(encoded(snapshot['files'])),
              'SNAPSHOT_TAMPERED', 'Review input inventory changed')
        pinned_language = request['_text_review'].get('language')
        if pinned_language is None:
            base = request['_text_review'].get('base_tex_path')
            _need(isinstance(base, str), 'LANGUAGE_IDENTITY_REQUIRED', 'Marked review has no trusted language binding')
            pinned_language = _review_language(cfg, {**request, 'tex_path': base})
        source_kind = 'marked_text_review'
    else:
        (current_inputs.verify_build_current if current else current_inputs.verify_build_frozen)(cfg, build_id)
        if not current and historical_source is not None:
            fields = {'kind', 'tex_path', 'language', 'source_sha256', 'input_sha256'}
            _need(isinstance(historical_source, dict) and set(historical_source) == fields
                  and historical_source.get('kind') == 'controlled_tex_review'
                  and historical_source.get('tex_path') == tex_path
                  and historical_source.get('source_sha256') == request.get('expected_revision')
                  and historical_source.get('input_sha256') == record.get('snapshot', {}).get('sha256')
                  and historical_source.get('language') in ('en', 'zh'),
                  'REVIEW_RECEIPT_CHANGED', 'Saved review source differs from its original frozen build')
            pinned_language = historical_source['language']
            bound = request.get('_version_binding', {}).get('language')
            _need(bound is None or bound == pinned_language, 'LANGUAGE_IDENTITY_CONFLICT',
                  'Saved review language differs from the frozen managed revision')
        else:
            pinned_language = _review_language(cfg, request)
        source_kind = 'controlled_tex_review'
    _need(pinned_language == language,
          'LANGUAGE_IDENTITY_CONFLICT', 'Review language differs from its controlled source')
    if current:
        from paper_studio.execution import build_plan
        plan = build_plan(cfg, tex_path)
        _need(record.get('snapshot_complete') is True
              and plan.get('expected_revision') == request.get('expected_revision')
              and plan.get('input_sha256') == record.get('snapshot', {}).get('sha256')
              and plan.get('_version_binding') == request.get('_version_binding')
              and plan.get('_review_binding') == request.get('_review_binding')
              and plan.get('_text_review') == request.get('_text_review'),
              'BUILD_PROOF_SOURCE_CHANGED', 'Review PDF no longer matches the selected source and dependencies')
        for key, expected in (
                ('build_profile', plan.get('engine', 'xelatex')),
                ('bibliography_engine', plan.get('bibliography_engine', 'bibtex')),
                ('use_latexmk', plan.get('use_latexmk', False)),
                ('working_directory', plan.get('working_directory', '.')),
                ('tex_search_paths', plan.get('tex_search_paths', [])),
                ('compatibility_hooks', plan.get('compatibility_hooks', []))):
            _need(request.get(key, [] if key == 'compatibility_hooks' else None) == expected,
                  'BUILD_PROFILE_DENIED', 'Review build recipe differs from its trusted current entry')
    return {'kind': source_kind, 'tex_path': tex_path, 'language': pinned_language,
            'source_sha256': request.get('expected_revision'),
            'input_sha256': record.get('snapshot', {}).get('sha256')}
