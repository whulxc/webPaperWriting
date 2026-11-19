"""Collect Drive comment threads for hash-bound, actually published project PDFs.

Drive comments are external review data, not embedded PDF annotations or edit
instructions. Callers select a local immutable version, never an arbitrary ID.
Collection creates content-addressed review records; it never edits a PDF,
manuscript, remote comment, sharing rule, or OAuth file.
"""
from __future__ import annotations

import json
from datetime import datetime
import hashlib
import contextlib
from pathlib import Path
import re
import time
import tempfile
import urllib.parse
import uuid

from paper_studio.drive.publisher import Drive, verified_manifest
from paper_studio.provider import jobs
from paper_studio.security.policy import (
    StudioError, atomic_json, builds_root, encoded, fail, locked, no_links,
    path_for, project, safe_read, safe_write_new, sha,
)
from paper_studio.versions.history import version_path

SCHEMA = 'paper-studio.drive-review-comments.v1'
MAX_THREADS = 2000
MAX_PAGES = 20
MAX_BYTES = 2 * 1024 * 1024
FIELDS = ('nextPageToken,comments(id,content,createdTime,modifiedTime,resolved,deleted,'
          'author(me),anchor,quotedFileContent(value,mimeType),'
          'replies(id,content,createdTime,modifiedTime,action,deleted,author(me)))')


def _need(value, code, message):
    if not value:
        fail(code, message)


def publication_context(cfg, version):
    """Resolve only the main PDFs in a completed, locally verified publication."""
    archive, manifest, identity = verified_manifest(cfg, version)
    from paper_studio.provider import job_index
    if job_index.enabled(cfg):
        build_id = job_index.publication(cfg, version, identity['sha256'])
    else:
        candidates = []
        paths = sorted(builds_root(cfg).glob('rb_*/status.json'))
        _need(len(paths) <= 4096, 'REVIEW_HISTORY_LIMIT', 'Publication history exceeds its bounded inventory')
        for path in paths:
            status = json.loads(safe_read(path.parent, path.name, MAX_BYTES))
            result = status.get('result', {})
            if (status.get('project_id') == cfg['project_id'] and status.get('kind') == 'drive'
                    and status.get('status') == 'completed' and result.get('version') == version
                    and result.get('version_manifest_sha256') == identity['sha256']
                    and result.get('upload_complete') is True):
                candidates.append((status.get('finished_at', 0), path.parent.name))
        _need(candidates, 'REVIEW_PUBLICATION_REQUIRED', 'Version has no completed hash-bound Drive publication')
        build_id = max(candidates)[1]
    status = jobs.read_status(cfg, build_id)
    result = status.get('result', {})
    _need(status.get('project_id') == cfg['project_id'] and status.get('kind') == 'drive'
          and status.get('status') == 'completed' and result.get('version') == version
          and result.get('version_manifest_sha256') == identity['sha256']
          and result.get('upload_complete') is True,
          'REVIEW_RECEIPT_CHANGED', 'Selected publication differs from the indexed version identity')
    job = jobs.job_path(cfg, build_id)
    receipt_raw = safe_read(job / 'output', 'receipt.json', MAX_BYTES)
    receipt = json.loads(receipt_raw)
    receipt_entry = next((row for row in status.get('artifacts', []) if row.get('path') == 'receipt.json'), None)
    _need(receipt_entry and receipt_entry.get('sha256') == sha(receipt_raw)
          and receipt_entry.get('bytes') == len(receipt_raw),
          'REVIEW_RECEIPT_CHANGED', 'Publication receipt is not a verified build artifact')
    _need(receipt.get('project_id') == cfg['project_id'] and receipt.get('version') == version
          and receipt.get('version_manifest_sha256') == identity['sha256']
          and receipt.get('upload_complete') is True,
          'REVIEW_RECEIPT_CHANGED', 'Publication receipt does not identify the selected version')
    members = {row['path']: row for row in manifest['files']}
    pdfs = []
    for bid in manifest['build_ids']:
        prefix = 'builds/' + bid + '/'
        build = json.loads(safe_read(archive, prefix + 'manifest.json', MAX_BYTES))
        request = json.loads(safe_read(archive, prefix + 'request.json', MAX_BYTES))
        result = build.get('result', {})
        pdf_path = result.get('pdf_path')
        if not isinstance(pdf_path, str) or not pdf_path.lower().endswith('.pdf'):
            continue
        relative = prefix + 'output/' + pdf_path
        member = members.get(relative)
        rows = [row for row in receipt.get('files', []) if row.get('version_path') == relative]
        _need(member and len(rows) == 1, 'REVIEW_RECEIPT_CHANGED', 'Main PDF is not uniquely covered by the publication')
        remote = rows[0]
        _need(remote.get('sha256') == member['sha256'] and remote.get('bytes') == member['bytes']
              and all(remote.get(key) is True for key in
                      ('metadata_verified', 'server_sha256_verified', 'expected_identity_verified')),
              'REVIEW_RECEIPT_CHANGED', 'Main PDF publication identity is incomplete')
        body = request.get('request', request)
        language = body.get('_version_binding', {}).get('language')
        if language not in {'en', 'zh'}:
            match = re.search(r'(?:^|[/_.-])(en|zh)(?=[/_.-]|$)', body.get('tex_path', ''))
            language = match.group(1) if match else 'unspecified'
        _need(type(result.get('pages')) is int and result['pages'] > 0,
              'REVIEW_PDF_IDENTITY', 'Published main PDF has no verified page count')
        pdfs.append({'file_id': remote['id'], 'name': remote['name'], 'language': language,
                     'build_id': bid, 'pdf_sha256': member['sha256'], 'bytes': member['bytes'],
                     'pages': result['pages'], 'version_path': relative})
    _need(pdfs and len(pdfs) <= 16 and len({row['file_id'] for row in pdfs}) == len(pdfs),
          'REVIEW_PDF_IDENTITY', 'Version needs one to sixteen distinct published main PDFs')
    return {'project_id': cfg['project_id'], 'version': version,
            'version_manifest_sha256': identity['sha256'], 'publication_build_id': build_id,
            'folder_id': receipt['folder_id'], 'pdfs': pdfs}


class _ReadOnlyDrive(Drive):
    """Retry interrupted GETs only, within the unchanged collection deadline."""

    def request(self, method, path, body=None, content_type='application/json'):
        _need(method == 'GET' and body is None, 'REVIEW_READ_ONLY',
              'Comment collection permits only read-only Drive GET requests')
        for attempt in range(3):
            try:
                return super().request(method, path, content_type=content_type)
            except StudioError as error:
                if error.code != 'DRIVE_INTERRUPTED' or attempt == 2:
                    raise
                self.timeout(1)  # Preserve the original total deadline.

    def metadata(self, file_id):
        fields = 'id,name,mimeType,size,md5Checksum,sha256Checksum,parents,permissions(type,role),headRevisionId,version,modifiedTime'
        return self.request('GET', 'drive/v3/files/' + urllib.parse.quote(file_id, safe='')
                            + '?fields=' + fields)

    def read_media(self, file_id, size):
        parts = []
        for start in range(0, size, 1024 * 1024):
            end = min(size, start + 1024 * 1024) - 1
            for attempt in range(3):
                try:
                    _, _, raw = self.exchange('GET', 'https://www.googleapis.com/drive/v3/files/'
                        + urllib.parse.quote(file_id, safe='') + '?alt=media',
                        headers={'Range':f'bytes={start}-{end}'}, allowed=(206,))
                    break
                except StudioError as error:
                    if error.code != 'DRIVE_INTERRUPTED' or attempt == 2:
                        raise
            _need(len(raw) == end - start + 1, 'REVIEW_REMOTE_CHANGED', 'Review media range length changed')
            parts.append(raw)
        return b''.join(parts)


def _client(cfg):
    _need(bool(cfg.get('drive_oauth_file')), 'OAUTH_REQUIRED', 'Shared local Drive OAuth reference is required')
    return _ReadOnlyDrive(cfg['drive_oauth_file'], persist_access_token=False, deadline=time.monotonic() + 600)


def _read_threads(drive, file_id):
    result, tokens, token = [], set(), None
    for _ in range(MAX_PAGES):
        query = {'fields': FIELDS, 'pageSize': 100, 'includeDeleted': 'true'}
        if token:
            query['pageToken'] = token
        response = drive.request('GET', 'drive/v3/files/' + urllib.parse.quote(file_id, safe='')
                                 + '/comments?' + urllib.parse.urlencode(query))
        rows = response.get('comments', [])
        _need(isinstance(rows, list), 'REVIEW_COMMENT_PROTOCOL', 'Drive returned an invalid comment list')
        result.extend(rows)
        _need(len(result) <= MAX_THREADS and len(encoded(result)) <= MAX_BYTES,
              'REVIEW_COMMENT_LIMIT', 'Comment thread collection exceeds its bounded size')
        token = response.get('nextPageToken')
        if not token:
            return result
        _need(isinstance(token, str) and len(token) <= 4096 and token not in tokens,
              'REVIEW_COMMENT_PROTOCOL', 'Comment continuation token is invalid or repeated')
        tokens.add(token)
    fail('REVIEW_COMMENT_LIMIT', 'Comment pagination exceeds the bounded page count')


def _text(value, maximum=16384):
    _need(isinstance(value, str) and len(value) <= maximum,
          'REVIEW_COMMENT_PROTOCOL', 'Comment text is missing, invalid or exceeds its bound')
    return value


def _normalize(context, pdf, rows):
    result, seen = [], {}
    for raw in rows:
        _need(isinstance(raw, dict), 'REVIEW_COMMENT_PROTOCOL', 'Drive comment must be an object')
        comment_id = _text(raw.get('id'), 256)
        _need(comment_id, 'REVIEW_COMMENT_PROTOCOL', 'Drive comment ID is missing')
        replies = raw.get('replies', [])
        _need(isinstance(replies, list) and len(replies) <= 2000,
              'REVIEW_COMMENT_LIMIT', 'Comment reply list is invalid or excessive')
        selected = {key: raw[key] for key in ('content', 'createdTime', 'modifiedTime',
                     'resolved', 'deleted', 'anchor', 'quotedFileContent') if key in raw}
        selected['author_is_current_user'] = isinstance(raw.get('author'), dict) and raw['author'].get('me') is True
        for key in ('content', 'anchor'):
            if key in selected:
                _text(selected[key])
        selected['replies'] = [{key: reply[key] for key in
            ('id', 'content', 'createdTime', 'modifiedTime', 'action', 'deleted') if key in reply}
            for reply in replies if isinstance(reply, dict)]
        _need(len(selected['replies']) == len(replies), 'REVIEW_COMMENT_PROTOCOL', 'Reply must be an object')
        for reply, original in zip(selected['replies'], replies):
            reply['author_is_current_user'] = isinstance(original.get('author'), dict) and original['author'].get('me') is True
            _text(reply.get('id'), 256)
            if 'content' in reply:
                _text(reply['content'])
        record = {'schema': 'paper-studio.review-issue.v1', 'channel': 'drive_comment',
                  'untrusted_review_content': True, 'project_id': context['project_id'],
                  'version': context['version'], 'version_manifest_sha256': context['version_manifest_sha256'],
                  'file_id': pdf['file_id'], 'file_name': pdf['name'], 'language': pdf['language'],
                  'pdf_sha256': pdf['pdf_sha256'], 'pdf_bytes': pdf['bytes'], 'pdf_pages': pdf['pages'],
                  'comment_id': comment_id, **selected, 'source_binding': 'file_identity_verified',
                  'page_source_binding': 'unbound', 'resolution_status': 'unprocessed'}
        if context.get('review_receipt_id'):
            record['review_receipt_id'] = context['review_receipt_id']
        record['review_id'] = 'dr_' + sha(encoded([context['project_id'], context['version'], pdf['file_id'], comment_id]))[:24]
        fingerprint = sha(encoded(record))
        if comment_id in seen:
            _need(seen[comment_id] == fingerprint, 'REVIEW_COMMENT_CHANGED', 'Duplicate thread changed during pagination')
            continue
        seen[comment_id] = fingerprint
        result.append(record)
    return result


def _revision_identity(metadata):
    # Drive's file version also ticks for comment/metadata changes. The head
    # revision and byte checksums identify the downloaded PDF content.
    return {key:metadata.get(key) for key in ('id','mimeType','size','md5Checksum',
            'parents','headRevisionId')}


def _check_server_sha(metadata, expected):
    _need(not metadata.get('sha256Checksum') or metadata['sha256Checksum'] == expected,
          'REVIEW_REMOTE_CHANGED', 'Drive server checksum conflicts with the verified PDF bytes')


def verified_page_equivalent(cfg, context, pdf, metadata, drive, *, downloaded=None, persist=False):
    """Keep baseline bytes distinct while proving an exact-ID Drive copy has identical pages."""
    from paper_studio.drive.target_display import review_build_pdf
    _need(context.get('context_kind') == 'lightweight_review' and metadata.get('headRevisionId')
          and metadata.get('md5Checksum'),
          'REVIEW_REMOTE_CHANGED', 'A rewritten review needs its exact Drive revision identity')
    _need(metadata.get('id') == pdf['file_id'] and metadata.get('mimeType') == 'application/pdf'
          and metadata.get('parents') == [context['folder_id']],
          'REVIEW_REMOTE_CHANGED', 'The reviewed file left its bound project folder')
    try:
        size = int(metadata['size'])
    except (KeyError, TypeError, ValueError):
        fail('REVIEW_REMOTE_CHANGED', 'The rewritten review size is missing')
    _need(0 < size <= 64 * 1024**2, 'REVIEW_REMOTE_CHANGED', 'The rewritten review exceeds its bounded size')
    identity = {'project_id':cfg['project_id'], 'review_receipt_id':context['review_receipt_id'],
                'baseline_sha256':pdf['pdf_sha256'], 'remote':_revision_identity(metadata),
                'verification_rules':'content-resources-text-geometry-render-links-no-active-v1'}
    digest = sha(encoded(identity))
    private = Path(cfg['state_root'])/'drive-review-comments'/('review-'+context['review_receipt_id'])/'remote-identities'
    cache = private/(digest+'.json')
    with locked(cfg, 'drive-review-identity'):
        if cache.exists():
            result = json.loads(safe_read(private, cache.name, MAX_BYTES))
            _need(result.get('identity') == identity and result.get('mode') in
                  {'verified_page_equivalent','download_verified_original_bytes'},
                  'REVIEW_LOCAL_CHANGED', 'Stored rewritten review identity changed')
            _check_server_sha(metadata, result.get('reviewed_sha256'))
            if result['mode'] == 'download_verified_original_bytes':
                _need(result.get('reviewed_sha256') == pdf['pdf_sha256']
                      and result.get('reviewed_bytes') == pdf['bytes']
                      and result.get('unchanged_original_pdf_bytes') is True,
                      'REVIEW_LOCAL_CHANGED', 'Stored exact download identity changed')
                return result
            evidence = no_links(Path(result['evidence_root']))
            _need(evidence.is_relative_to(Path(cfg['artifact_root'])/'review-identities'/context['review_receipt_id']),
                  'REVIEW_LOCAL_CHANGED', 'Stored page evidence left the protected artifact store')
            proof = safe_read(evidence, 'annotation-identity.json', MAX_BYTES)
            _need(sha(proof) == result.get('proof_sha256'), 'REVIEW_LOCAL_CHANGED', 'Stored page-identity evidence changed')
            document = json.loads(proof)
            _need(document.get('baseline_sha256') == pdf['pdf_sha256']
                  and document.get('reviewed_sha256') == result.get('reviewed_sha256')
                  and document.get('reviewed_bytes') == result.get('reviewed_bytes')
                  and document.get('page_content_identity_verified') is True
                  and document.get('active_content_rejected') is True,
                  'REVIEW_LOCAL_CHANGED', 'Stored page-identity result changed')
            return result
        media = downloaded if downloaded is not None else drive.read_media(pdf['file_id'], size)
        _need(isinstance(media, bytes) and len(media) == size
              and hashlib.md5(media).hexdigest() == metadata['md5Checksum']
              and (not metadata.get('sha256Checksum') or sha(media) == metadata['sha256Checksum']),
              'REVIEW_REMOTE_CHANGED', 'The reviewed Drive bytes differ from their revision metadata')
        after = drive.private(pdf['file_id'])
        _need(_revision_identity(after) == identity['remote'],
              'REVIEW_REMOTE_CHANGED', 'Drive revision changed while its exact bytes were received')
        _check_server_sha(after, sha(media))
        original, _ = review_build_pdf(cfg, pdf['build_id'])
        baseline = safe_read(original.parent, original.name, 64 * 1024**2)
        _need(sha(baseline) == pdf['pdf_sha256'], 'REVIEW_PDF_IDENTITY', 'Original review PDF changed locally')
        if media == baseline:
            result = {'mode':'download_verified_original_bytes','identity':identity,
                      'baseline_sha256':pdf['pdf_sha256'],'reviewed_sha256':sha(media),'reviewed_bytes':len(media),
                      'unchanged_original_pdf_bytes':True,'download_sha256_verified':True,
                      'server_sha256_available':bool(metadata.get('sha256Checksum'))}
            if persist:
                atomic_json(cache,result,exclusive=True)
            return result
        return _verify_review_copy(cfg, context, pdf, baseline, media, identity, cache, persist)


def _verify_review_copy(cfg, context, pdf, baseline, media, identity, cache, persist):
    from paper_studio.security.sandbox import run_sandbox
    with contextlib.ExitStack() as temporary:
        if persist:
            root = no_links(Path(cfg['artifact_root'])/'review-identities'/context['review_receipt_id']
                            /sha(encoded(identity))/('attempt-'+uuid.uuid4().hex))
        else:
            folder = no_links(Path(cfg['state_root'])/'review-identity-temporary')
            folder.mkdir(mode=0o700,parents=True,exist_ok=True)
            root = Path(temporary.enter_context(tempfile.TemporaryDirectory(prefix='read-',dir=folder)))
        snapshot, output = root/'input', root/'output'
        snapshot.mkdir(parents=True, exist_ok=True)
        for name, raw in (('baseline.pdf',baseline),('reviewed.pdf',media)):
            target = snapshot/name
            if target.exists():
                _need(safe_read(snapshot,name,64*1024**2) == raw, 'REVIEW_LOCAL_CHANGED', 'A retained identity input changed')
            else:
                safe_write_new(snapshot,name,raw);target.chmod(0o400)
        bounded = {**cfg,'limits':{**cfg.get('limits',{}),'timeout_seconds':90,'max_files':32,
                   'max_output_bytes':max(64*1024**2,len(baseline)+len(media)+MAX_BYTES)}}
        execution = run_sandbox(snapshot,output,'pdf',{'_annotation_identity':True,
                                'baseline_pdf':'baseline.pdf','reviewed_pdf':'reviewed.pdf'},bounded)
        raw_proof = safe_read(output,'annotation-identity.json',MAX_BYTES)
        proof = json.loads(raw_proof)
        _need(execution.get('returncode') == 0 and proof.get('baseline_sha256') == pdf['pdf_sha256']
              and proof.get('reviewed_sha256') == sha(media) and proof.get('page_content_identity_verified') is True
              and proof.get('active_content_rejected') is True,
              'REVIEW_REMOTE_CHANGED', 'The rewritten PDF did not prove identical safe pages')
        result = {'mode':'verified_page_equivalent','identity':identity,
                  'baseline_sha256':pdf['pdf_sha256'],'reviewed_sha256':sha(media),'reviewed_bytes':len(media),
                  'unchanged_original_pdf_bytes':False,'page_content_identity_verified':True,
                  'proof_sha256':sha(raw_proof),'verified_pages':len(proof['pages']),
                  'evidence_root':str(output) if persist else None,'temporary_evidence_released':not persist}
        if persist:
            atomic_json(cache,result,exclusive=True)
        return result


def collect_snapshot(cfg, context, *, drive=None, persist_identity=False):
    drive = drive or _client(cfg)
    folder = drive.private(context['folder_id'])
    _need(folder.get('id') == context['folder_id'] and folder.get('mimeType') == 'application/vnd.google-apps.folder',
          'REVIEW_REMOTE_CHANGED', 'Published version folder is no longer the expected private folder')
    records, remote_identities = [], []
    for pdf in context['pdfs']:
        before = drive.private(pdf['file_id'])
        expected = (before.get('id') == pdf['file_id'] and before.get('mimeType') == 'application/pdf'
                    and before.get('parents') == [context['folder_id']])
        _need(expected, 'REVIEW_REMOTE_CHANGED', 'Published PDF bytes or parent differ from the verified publication')
        if before.get('sha256Checksum') == pdf['pdf_sha256'] and str(before.get('size')) == str(pdf['bytes']):
            remote_identities.append({'file_id':pdf['file_id'],'mode':'original_bytes',
                                      'baseline_sha256':pdf['pdf_sha256'],'reviewed_sha256':pdf['pdf_sha256'],
                                      'reviewed_bytes':pdf['bytes'],'unchanged_original_pdf_bytes':True})
        else:
            remote_identities.append({'file_id':pdf['file_id'],
                **verified_page_equivalent(cfg,context,pdf,before,drive,persist=persist_identity)})
        rows = _read_threads(drive, pdf['file_id'])
        after = drive.private(pdf['file_id'])
        _need(_revision_identity(after) == _revision_identity(before),
              'REVIEW_REMOTE_CHANGED', 'Published PDF identity changed while comments were collected')
        _check_server_sha(after, remote_identities[-1]['reviewed_sha256'])
        records.extend(_normalize(context, pdf, rows))
    records.sort(key=lambda row: (row['file_id'], row['comment_id']))
    snapshot = {'schema': SCHEMA, **context, 'comments': records,
                'remote_pdf_identities':remote_identities,
                'channel': 'drive_comments', 'embedded_pdf_annotations_included': False,
                'scientific_changes_applied': False, 'remote_comments_modified': False}
    _need(len(records) <= MAX_THREADS and len(encoded(snapshot)) <= MAX_BYTES,
          'REVIEW_COMMENT_LIMIT', 'Combined comment collection exceeds its bounded size')
    return snapshot


def _summary(snapshot):
    comments = snapshot['comments']
    result = {key: snapshot[key] for key in ('project_id', 'version', 'version_manifest_sha256',
            'publication_build_id', 'channel', 'embedded_pdf_annotations_included')} | {
            'pdfs': snapshot['pdfs'], 'comment_count': len(comments),
            'reply_count': sum(len(row['replies']) for row in comments),
            'snapshot_sha256': sha(encoded(snapshot)), 'remote_revalidated': True,
            'scientific_changes_applied': False, 'remote_comments_modified': False}
    for key in ('review_receipt_id', 'review_build_id', 'context_kind'):
        if key in snapshot:
            result[key] = snapshot[key]
    result['remote_pdf_identities'] = snapshot['remote_pdf_identities']
    return result


def _preview(records):
    # Keep public tool results small even when a single thread has many replies.
    result = []
    for row in records[:5]:
        item = {key: row[key] for key in ('review_id', 'file_id', 'language', 'comment_id',
                'source_binding', 'page_source_binding')}
        for key in ('content', 'createdTime', 'modifiedTime', 'resolved', 'deleted'):
            if key in row:
                item[key] = row[key][:2048] if isinstance(row[key], str) else row[key]
        if isinstance(row.get('quotedFileContent'), dict):
            item['quoted_text'] = str(row['quotedFileContent'].get('value', ''))[:2048]
        item['reply_count'] = len(row['replies'])
        item['replies_preview'] = [{key: (value[:1024] if isinstance(value, str) else value)
                                   for key, value in reply.items()}
                                  for reply in row['replies'][:3]]
        item['replies_preview_truncated'] = len(row['replies']) > 3
        if 'source_change' in row:
            item['source_change'] = row['source_change']
        result.append(item)
        if len(encoded(result)) > 32768:
            result.pop()
            break
    return result


def _selection(cfg, args):
    version, receipt_id = args.get('version'), args.get('review_receipt_id')
    _need(not (version is not None and receipt_id is not None),
          'INVALID_ARGUMENT', 'Choose a formal version or one lightweight receipt')
    if isinstance(version, str) and version.startswith('review:'):
        receipt_id, version = version[7:], None
    if receipt_id is not None:
        _need(isinstance(receipt_id, str) and re.fullmatch('[0-9a-f]{64}', receipt_id),
              'INVALID_ARGUMENT', 'An exact lightweight review receipt SHA is required')
        return None, receipt_id, 'review-' + receipt_id
    version_path(cfg, version)
    return version, None, version


def lightweight_context(cfg, receipt_id):
    """Read one existing display journal and its exact native PDF, with no listing."""
    from paper_studio.drive import target_display
    _need(cfg.get('admission_profile') == 'paper-editor.v4'
          and cfg.get('execution_policy', {}).get('allow_text_reviews') is True,
          'REVIEW_NOT_CONFIGURED', 'Lightweight reviews are not enabled in this project')
    raw = safe_read(Path(cfg['state_root']) / 'review-display', receipt_id + '.json', MAX_BYTES)
    journal = json.loads(raw)
    plan, receipt = journal.get('plan', {}), journal.get('receipt', {})
    expected = plan.get('expected', {})
    _need(journal.get('schema') == 'paper-studio.target-display-journal.v1'
          and journal.get('status') == 'completed' and journal.get('plan_sha256') == sha(encoded(plan))
          and expected.get('project_id') == cfg['project_id'] and expected.get('role') == 'review'
          and receipt.get('file_id') == journal.get('file_id')
          and all(receipt.get(key) == value for key, value in expected.items())
          and all(receipt.get(key) is True for key in
                  ('metadata_verified', 'server_sha256_verified', 'expected_identity_verified', 'owner_only_verified')),
          'REVIEW_RECEIPT_CHANGED', 'The exact lightweight display receipt is incomplete or changed')
    identity = plan.get('source_identity', {})
    _need(receipt.get('source_identity') == identity and identity.get('build_id')
          and all(identity.get(key) == expected.get(key) for key in ('role', 'language', 'sha256', 'bytes')),
          'REVIEW_RECEIPT_CHANGED', 'The display is not tied to one native review build')
    lookup = {'source':plan.get('source_path'), **{key:expected[key] for key in
              ('project_id','project_folder_id','parent_id','name','sha256','bytes','role','language')},
              'source_identity':identity}
    _need(sha(encoded(lookup)) == receipt_id, 'REVIEW_RECEIPT_CHANGED', 'The display receipt key changed')
    path, artifact = target_display.review_build_pdf(cfg, identity['build_id'])
    _need(str(path) == plan.get('source_path') and artifact.get('sha256') == expected.get('sha256')
          and artifact.get('bytes') == expected.get('bytes'),
          'REVIEW_RECEIPT_CHANGED', 'The display PDF differs from its native build')
    source = target_display.review_build_source(cfg, identity['build_id'], expected['language'],
                                              historical_source=identity.get('review_source'))
    if source['kind'] == 'controlled_tex_review':
        _need(identity.get('review_source') == source, 'REVIEW_RECEIPT_CHANGED',
              'Clean review receipt differs from its frozen source identity')
    manifest = json.loads(safe_read(jobs.job_path(cfg, identity['build_id']), 'manifest.json', 4 * MAX_BYTES))
    pages = manifest.get('result', {}).get('pages')
    _need(type(pages) is int and pages > 0, 'REVIEW_PDF_IDENTITY', 'Native review page count is missing')
    return {'project_id':cfg['project_id'], 'version':'review:' + receipt_id,
            'version_manifest_sha256':None, 'publication_build_id':None,
            'review_receipt_id':receipt_id, 'review_build_id':identity['build_id'],
            'context_kind':'lightweight_review', 'folder_id':expected['parent_id'],
            'pdfs':[{'file_id':receipt['file_id'], 'name':expected['name'], 'language':expected['language'],
                     'build_id':identity['build_id'], 'pdf_sha256':expected['sha256'],
                     'bytes':expected['bytes'], 'pages':pages, 'version_path':None}]}


def _page_text(cfg, pdf, page):
    from paper_studio.drive.target_display import review_build_pdf
    from paper_studio.versions.review_proposals import _engine
    path, _ = review_build_pdf(cfg, pdf['build_id'])
    raw = safe_read(path.parent, path.name, 64 * 1024**2)
    _need(sha(raw) == pdf['pdf_sha256'], 'REVIEW_PDF_IDENTITY', 'The local review PDF changed')
    with _engine(cfg).open(stream=raw, filetype='pdf') as document:
        _need(len(document) == pdf['pages'] and 1 <= page <= len(document),
              'REVIEW_PAGE_RANGE', 'The selected review page is outside this PDF')
        return document[page - 1].get_text()


def _bind_sources(cfg, snapshot, bindings):
    _need(snapshot.get('context_kind') == 'lightweight_review',
          'REVIEW_SOURCE_REQUIRED', 'Source binding requires a lightweight native review')
    _need(isinstance(bindings, list) and len(bindings) <= 16,
          'INVALID_ARGUMENT', 'Select at most sixteen explicit source bindings')
    comments = {(row['file_id'], row['comment_id']):row for row in snapshot['comments']}
    pdfs = {row['file_id']:row for row in snapshot['pdfs']}
    seen = set()
    for binding in bindings:
        key = (binding['file_id'], binding['comment_id'])
        _need(key not in seen and key in comments, 'REVIEW_SOURCE_REQUIRED', 'Select one exact collected thread')
        seen.add(key)
        row, pdf = comments[key], pdfs[key[0]]
        path_for(cfg, binding['path'], True)
        raw = safe_read(cfg['project_root'], binding['path'], MAX_BYTES)
        text = raw.decode('utf-8')
        _need(sha(raw) == binding['sha256'] and text.count(binding['quote']) == 1,
              'REVIEW_SOURCE_CHANGED', 'The quoted source must match its pinned SHA and occur exactly once')
        baseline_path = binding.get('baseline_path', binding['path'])
        path_for(cfg, baseline_path)
        _need(sha(safe_read(cfg['project_root'], baseline_path, MAX_BYTES)) == binding['sha256'],
              'REVIEW_SOURCE_CHANGED', 'The working copy must retain the exact original baseline bytes')
        quoted = row.get('quotedFileContent', {}).get('value', '')
        compact = lambda text: ''.join(text.split())
        _need(quoted and compact(quoted) == compact(binding['quote'])
              and compact(binding['quote']) in compact(_page_text(cfg, pdf, binding['page'])),
              'REVIEW_QUOTE_MISMATCH', 'The source quote must also be present on the selected original PDF page')
        _need(binding['quote'] != binding['replacement'], 'INVALID_ARGUMENT', 'A source change must change the selected text')
        identity = {'file_id':pdf['file_id'], 'pdf_sha256':pdf['pdf_sha256'],
                    **{key:binding[key] for key in ('path','sha256','quote','replacement','page')}}
        identity['path'] = baseline_path
        change = {**binding, 'confirmation_text':'接受修改 ' + sha(encoded(identity))[:12],
                  'expected_after_sha256':sha(text.replace(binding['quote'], binding['replacement'], 1).encode())}
        row.update(source_change=change, page_source_binding='quoted_page_and_source_verified',
                   resolution_status='awaiting_author_confirmation')


def _request_id(value):
    _need(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', value),
          'INVALID_ARGUMENT', 'A stable bounded request_id is required')
    return value


def _latest_author_reply(replies):
    candidates = [row for row in replies if row.get('author_is_current_user') is True
                  and not row.get('deleted') and row.get('action', 'reply') == 'reply'
                  and row.get('content', '').strip()]
    def stamp(row):
        value = row.get('createdTime')
        _need(isinstance(value, str), 'REVIEW_CONFIRMATION_REQUIRED', 'Author reply time is required')
        try:
            result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            fail('REVIEW_CONFIRMATION_REQUIRED', 'Author reply time is invalid')
        _need(result.tzinfo is not None, 'REVIEW_CONFIRMATION_REQUIRED', 'Author reply time needs a timezone')
        return result.timestamp()
    ordered = sorted((stamp(row), row['id']) for row in candidates)
    _need(ordered and (len(ordered) == 1 or ordered[-1][0] > ordered[-2][0]),
          'REVIEW_CONFIRMATION_REQUIRED', 'An unambiguous latest author reply is required')
    return ordered[-1][1]


def _application_identity(intent):
    """Compare approved edits and PDF content, leaving cache diagnostics out."""
    result = dict(intent)
    proofs = []
    for record in intent.get('reviewed_pdf_identities', []):
        identity = record.get('identity', {})
        original = record.get('unchanged_original_pdf_bytes') is True
        proofs.append({'file_id':record.get('file_id'),
                       'project_id':identity.get('project_id', intent.get('project_id')),
                       'review_receipt_id':identity.get('review_receipt_id', intent.get('review_receipt_id')),
                       'baseline_sha256':record.get('baseline_sha256'),
                       'reviewed_sha256':record.get('reviewed_sha256'),
                       'reviewed_bytes':record.get('reviewed_bytes'),
                       'unchanged_original_pdf_bytes':original,
                       'page_content_identity_verified':record.get('page_content_identity_verified') is True or original,
                       'verification_rules':identity.get('verification_rules') if not original else 'exact_sha256_bytes'})
    result['reviewed_pdf_identities'] = sorted(proofs, key=lambda proof:str(proof['file_id']))
    return result


def _apply(cfg, args, context, private, *, drive=None, edit_sources=None):
    _need(context.get('context_kind') == 'lightweight_review',
          'REVIEW_SOURCE_REQUIRED', 'Direct comment application requires a lightweight review')
    request_id = _request_id(args.get('request_id'))
    collection_id = _request_id(args.get('collection_request_id'))
    collection = json.loads(safe_read(private / 'requests', collection_id + '.json', MAX_BYTES))
    raw = safe_read(cfg['project_root'], collection['snapshot_path'], MAX_BYTES)
    _need(collection.get('project_id') == cfg['project_id']
          and collection.get('review_receipt_id') == context['review_receipt_id']
          and sha(raw) == collection.get('snapshot_sha256') == args.get('expected_snapshot_sha256'),
          'REVIEW_LOCAL_CHANGED', 'The collected source proposal changed')
    snapshot = json.loads(raw)
    selected = args.get('confirmations')
    _need(isinstance(selected, list) and 1 <= len(selected) <= 16,
          'INVALID_ARGUMENT', 'Select the explicit acceptance replies to apply')
    stored = {(row['file_id'],row['comment_id']):row for row in snapshot['comments']}
    current = collect_snapshot(cfg, context, drive=drive,
                               persist_identity=args.get('dry_run',True) is False and args.get('authorized') is True)
    live = {(row['file_id'],row['comment_id']):row for row in current['comments']}
    changes, source_states, decisions, seen = [], [], [], set()
    for selection in selected:
        key = (selection['file_id'], selection['comment_id'])
        _need(key not in seen and key in stored and key in live,
              'REVIEW_CONFIRMATION_REQUIRED', 'Select one collected source proposal per thread')
        seen.add(key)
        before, now = stored[key], live[key]
        source = before.get('source_change')
        _need(source and all(now.get(k) == before.get(k) for k in ('content','quotedFileContent','anchor','deleted'))
              and not now.get('deleted'), 'REVIEW_COMMENT_CHANGED', 'The source proposal or original comment changed')
        reply = next((r for r in now['replies'] if r['id'] == selection['reply_id']), None)
        approved = {source['confirmation_text'], source['confirmation_text']
                    + '\n\n由 Codex 按作者在本次会话中的明确授权代为回复。'}
        _need(reply and _latest_author_reply(now['replies']) == selection['reply_id']
              and not reply.get('deleted') and reply.get('author_is_current_user') is True
              and reply.get('action', 'reply') == 'reply'
              and reply.get('content', '').strip() in approved,
              'REVIEW_CONFIRMATION_REQUIRED', 'The current Drive user must explicitly accept this change in the same thread')
        path_for(cfg, source['path'], True)
        baseline_path = source.get('baseline_path', source['path'])
        if baseline_path != source['path']:
            path_for(cfg, baseline_path)
            _need(sha(safe_read(cfg['project_root'], baseline_path, MAX_BYTES)) == source['sha256'],
                  'REVIEW_SOURCE_CHANGED', 'The original baseline changed after collection')
        _need(source['path'] not in {row['path'] for row in source_states},
              'REVIEW_SOURCE_OVERLAP', 'Apply one collected change per source file in a single transaction')
        source_states.append({'path':source['path'], 'before_sha256':source['sha256'],
                              'after_sha256':source['expected_after_sha256']})
        decisions.append({**selection, 'confirmation_text':source['confirmation_text'],
                          'acceptance_reply_content':reply['content'], 'author_is_current_user':True})
        changes.append({'kind':'edit', 'path':source['path'], 'expected_read_revision':source['read_revision'],
                        'edits':[{'kind':'replace_exact','old_text':source['quote'],
                                  'new_text':source['replacement'],'expected_match_count':1}]})
    intent = {'project_id':cfg['project_id'], 'review_receipt_id':context['review_receipt_id'],
              'snapshot_sha256':collection['snapshot_sha256'], 'decisions':decisions, 'sources':source_states,
              'reviewed_pdf_identities':current['remote_pdf_identities']}
    identities = [sha(safe_read(cfg['project_root'], row['path'], MAX_BYTES)) for row in source_states]
    apply_path = private / 'applies' / (request_id + '.json')
    is_dry_run = args.get('dry_run', True) is True
    with locked(cfg, 'drive-review-comments'):
        prior = json.loads(safe_read(apply_path.parent, apply_path.name, MAX_BYTES)) if apply_path.exists() else None
        if prior:
            _need(isinstance(prior.get('intent'), dict)
                  and _application_identity(prior['intent']) == _application_identity(intent),
                  'IDEMPOTENCY_CONFLICT', 'The apply request belongs to another decision')
            _need(all(digest == row['after_sha256'] for digest, row in zip(identities, source_states))
                  or all(digest == row['before_sha256'] for digest, row in zip(identities, source_states)),
                  'REVIEW_SOURCE_CHANGED', 'A source changed outside this confirmed application')
            if all(digest == row['after_sha256'] for digest, row in zip(identities, source_states)):
                result = {**prior, 'status':'completed', 'scientific_changes_applied':True,
                          'manuscript_version_created':False, 'idempotent_replay':True,
                          'author_confirmation_verified':True,'dry_run':is_dry_run}
                if not is_dry_run:
                    atomic_json(apply_path, result)
                return result
            _need(prior.get('status') == 'prepared', 'REVIEW_SOURCE_CHANGED',
                  'An applied change was reverted; collect and confirm a new decision before editing again')
        _need(all(digest == row['before_sha256'] for digest, row in zip(identities, source_states)),
              'REVIEW_SOURCE_CHANGED', 'The confirmed source differs from its collected baseline')
        result = {'project_id':cfg['project_id'], 'request_id':request_id, 'intent':intent,
                  'dry_run':is_dry_run, 'scientific_changes_applied':False,
                  'manuscript_version_created':False, 'remote_comments_modified':False}
        if is_dry_run:
            return {**result, 'status':'ready', 'author_confirmation_verified':True}
        _need(args.get('authorized') is True and callable(edit_sources),
              'AUTHORIZATION_REQUIRED', 'Applying confirmed edits needs authorization and the native writer')
        if not prior:
            atomic_json(apply_path, {**result,'status':'prepared'}, exclusive=True)
        edit_sources(changes)
        _need(all(sha(safe_read(cfg['project_root'], row['path'], MAX_BYTES)) == row['after_sha256']
                  for row in source_states), 'REVIEW_WRITE_INCOMPLETE', 'Native edit did not produce the confirmed bytes')
        result.update(status='completed', scientific_changes_applied=True, author_confirmation_verified=True)
        atomic_json(apply_path, result)
        return result


def dispatch(cfg, args, *, drive=None, edit_sources=None):
    project(cfg, args['project_id'])
    version, receipt_id, storage_label = _selection(cfg, args)
    action = args.get('action', 'plan')
    _need(action in {'plan', 'collect', 'status', 'apply'}, 'INVALID_ARGUMENT', 'Unsupported review comment action')
    request_id = args.get('request_id')
    if action in {'collect', 'status'}:
        _need(isinstance(request_id, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', request_id),
              'INVALID_ARGUMENT', 'Collection/status needs a stable bounded request_id')
    private = no_links(Path(cfg['state_root']) / 'drive-review-comments' / storage_label)
    receipt_path = private / 'requests' / ((request_id or '') + '.json')
    if action == 'status':
        receipt = json.loads(safe_read(receipt_path.parent, receipt_path.name, MAX_BYTES))
        _need(receipt.get('project_id') == cfg['project_id'] and receipt.get('version') == (version or 'review:' + receipt_id),
              'PROJECT_DENIED', 'Stored collection belongs to another project/version')
        return {**receipt, 'receipt_only': True, 'remote_revalidated': False}
    context = lightweight_context(cfg, receipt_id) if receipt_id else publication_context(cfg, version)
    if action == 'apply':
        return _apply(cfg, args, context, private, drive=drive, edit_sources=edit_sources)
    snapshot = collect_snapshot(cfg, context, drive=drive,
                                persist_identity=action=='collect' and args.get('dry_run',True) is False
                                and args.get('authorized') is True)
    if args.get('source_bindings'):
        _bind_sources(cfg, snapshot, args['source_bindings'])
    summary = _summary(snapshot)
    if action == 'plan' or args.get('dry_run', True) is True:
        preview = _preview(snapshot['comments'])
        return {**summary, 'dry_run': True, 'would_save': action == 'collect',
                'comments_preview': preview,
                'comments_preview_truncated': len(preview) < len(snapshot['comments'])}
    _need(args.get('dry_run') is False and args.get('authorized') is True,
          'AUTHORIZATION_REQUIRED', 'Saving review data requires dry_run=false and authorized=true')
    reviews = cfg.get('drive_source_sync', {}).get('reviews_dir', 'reviews')
    base = reviews + '/drive-comments/' + storage_label
    snapshot_path = base + '/snapshots/' + summary['snapshot_sha256'] + '.json'
    path_for(cfg, snapshot_path, True)
    with locked(cfg, 'drive-review-comments'):
        if receipt_path.exists():
            prior = json.loads(safe_read(receipt_path.parent, receipt_path.name, MAX_BYTES))
            _need(prior.get('snapshot_sha256') == summary['snapshot_sha256'],
                  'IDEMPOTENCY_CONFLICT', 'Request already belongs to a different comment snapshot; use a new request_id')
            raw = safe_read(cfg['project_root'], prior['snapshot_path'], MAX_BYTES)
            _need(sha(raw) == prior['snapshot_sha256'], 'REVIEW_LOCAL_CHANGED', 'Saved review snapshot changed')
            return {**prior, 'idempotent_replay': True, 'remote_revalidated': True}
        created = reused = 0
        for issue in snapshot['comments']:
            raw = encoded(issue)
            relative = base + '/issues/' + issue['review_id'] + '/' + sha(raw) + '.json'
            target = path_for(cfg, relative, True)
            if target.exists():
                _need(safe_read(cfg['project_root'], relative, MAX_BYTES) == raw,
                      'REVIEW_LOCAL_CHANGED', 'Saved review issue identity changed')
                reused += 1
            else:
                safe_write_new(cfg['project_root'], relative, raw)
                created += 1
        raw = encoded(snapshot)
        if path_for(cfg, snapshot_path, True).exists():
            _need(safe_read(cfg['project_root'], snapshot_path, MAX_BYTES) == raw,
                  'REVIEW_LOCAL_CHANGED', 'Saved review snapshot identity changed')
        else:
            safe_write_new(cfg['project_root'], snapshot_path, raw)
        result = {**summary, 'request_id': request_id, 'status': 'completed',
                  'snapshot_path': snapshot_path, 'new_issue_revisions': created,
                  'reused_issue_revisions': reused, 'dry_run': False}
        atomic_json(receipt_path, result, exclusive=True)
        return result
