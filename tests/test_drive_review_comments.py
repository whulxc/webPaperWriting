"""Real local version/receipt identities with simulated read-only Drive comments."""
from copy import deepcopy
import json
from pathlib import Path
import urllib.parse

import pytest

from paper_studio.drive import review_comments as review
from paper_studio.security.policy import StudioError, encoded, sha


@pytest.fixture
def published(tmp_path):
    root = tmp_path / 'paper'
    (root / 'reviews').mkdir(parents=True)
    artifact = tmp_path / 'artifacts'
    cfg = {'project_id': 'example', 'project_root': str(root),
           'artifact_root': str(artifact), 'state_root': str(tmp_path / 'state'),
           'writable_roots': ['reviews'], 'readonly_roots': ['materials'],
           'drive_oauth_file': '/private/not-read'}
    version = 'demo-v1'
    archive = artifact / 'versions' / version
    archive.mkdir(parents=True)
    files, uploads, bids, metadata = [], [], [], {}
    for number, language in enumerate(('en', 'zh'), 1):
        bid = 'rb_' + str(number).zfill(24)
        bids.append(bid)
        prefix = 'builds/' + bid + '/'
        tex = 'manuscript/main_' + language + '.tex'
        pdf = 'main_' + language + '.pdf'
        raw_pdf = b'%PDF synthetic ' + language.encode()
        members = {prefix + 'manifest.json': encoded({'result': {'pdf_path': pdf, 'pages': 2}}),
                   prefix + 'request.json': encoded({'request': {'tex_path': tex}}),
                   prefix + 'output/' + pdf: raw_pdf}
        for relative, raw in members.items():
            path = archive / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            files.append({'path': relative, 'bytes': len(raw), 'sha256': sha(raw)})
        remote = {'id': 'pdf_' + language, 'name': pdf, 'version_path': prefix + 'output/' + pdf,
                  'sha256': sha(raw_pdf), 'bytes': len(raw_pdf), 'metadata_verified': True,
                  'server_sha256_verified': True, 'expected_identity_verified': True}
        uploads.append(remote)
        metadata[remote['id']] = {'id': remote['id'], 'mimeType': 'application/pdf',
            'sha256Checksum': remote['sha256'], 'size': str(remote['bytes']), 'parents': ['version_folder']}
    manifest = {'schema': 'paper-studio.version.v1', 'project_id': cfg['project_id'],
                'version': version, 'build_ids': bids, 'files': files}
    (archive / 'manifest.json').write_bytes(encoded(manifest))
    manifest_sha = sha(encoded(manifest))
    drive_id = 'rb_' + '9' * 24
    job = artifact / 'builds' / drive_id
    (job / 'output').mkdir(parents=True)
    receipt = {'project_id': cfg['project_id'], 'version': version,
               'version_manifest_sha256': manifest_sha, 'upload_complete': True,
               'folder_id': 'version_folder', 'files': uploads}
    raw_receipt = encoded(receipt)
    (job / 'output/receipt.json').write_bytes(raw_receipt)
    status = {'project_id': cfg['project_id'], 'build_id': drive_id, 'kind': 'drive',
              'status': 'completed', 'finished_at': 1, 'result': receipt,
              'artifacts': [{'path': 'receipt.json', 'bytes': len(raw_receipt), 'sha256': sha(raw_receipt)}]}
    (job / 'status.json').write_bytes(encoded(status))
    metadata['version_folder'] = {'id': 'version_folder', 'mimeType': 'application/vnd.google-apps.folder'}

    class Cloud:
        def __init__(self):
            self.comments = {'pdf_' + lang: [{'id': 'c1', 'content': lang + ' page 1: check the example sentence.',
                'quotedFileContent': {'value': lang + ' example sentence'},
                'modifiedTime': '2026-11-10T00:00:00Z', 'resolved': False,
                'replies': [{'id': 'reply1', 'content': 'Example reply.', 'action': 'reply'}]}]
                for lang in ('en', 'zh')}
            self.calls = []
            self.metadata = metadata
            self.pages = None

        def private(self, ident):
            self.calls.append(('metadata', ident))
            return deepcopy(self.metadata[ident])

        def request(self, method, path):
            assert method == 'GET', 'Comment collection must never write to Drive'
            self.calls.append((method, path))
            fid = path.split('/files/', 1)[1].split('/', 1)[0]
            if self.pages:
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
                return deepcopy(self.pages[query.get('pageToken', ['first'])[0]])
            return {'comments': deepcopy(self.comments[fid])}

    return cfg, version, Cloud(), archive, job


def call(published, action='collect', request='first', **extra):
    cfg, version, cloud, *_ = published
    return review.dispatch(cfg, {'project_id': cfg['project_id'], 'version': version,
        'action': action, 'request_id': request, **extra}, drive=cloud)


def test_resolve_completed_receipt_and_plan_language_identity_without_writes(published):
    cfg, version, cloud, archive, _ = published
    before = {p: p.read_bytes() for p in Path(cfg['project_root']).rglob('*') if p.is_file()}
    context = review.publication_context(cfg, version)
    assert {row['language'] for row in context['pdfs']} == {'en', 'zh'}
    result = call(published, 'plan')
    assert result['comment_count'] == result['reply_count'] == 2
    assert result['dry_run'] and result['remote_revalidated']
    assert not result['embedded_pdf_annotations_included']
    assert all(row['page_source_binding'] == 'unbound' for row in result['comments_preview'])
    assert not Path(cfg['state_root']).exists()
    assert {p: p.read_bytes() for p in Path(cfg['project_root']).rglob('*') if p.is_file()} == before


def test_apply_replay_and_other_request_reuse_issue_revisions(published):
    cfg, *_ = published
    first = call(published, dry_run=False, authorized=True)
    assert first['new_issue_revisions'] == 2 and first['reused_issue_revisions'] == 0
    replay = call(published, dry_run=False, authorized=True)
    assert replay['idempotent_replay'] and replay['snapshot_path'] == first['snapshot_path']
    second = call(published, request='again', dry_run=False, authorized=True)
    assert second['new_issue_revisions'] == 0 and second['reused_issue_revisions'] == 2
    assert len(list((Path(cfg['project_root']) / 'reviews').rglob('dr_*/*.json'))) == 2
    saved = json.loads((Path(cfg['project_root']) / first['snapshot_path']).read_text())
    assert {row['language'] for row in saved['comments']} == {'en', 'zh'}
    assert len({row['review_id'] for row in saved['comments']}) == 2  # Same comment ID on two PDFs is distinct.
    historical = call(published, 'status')
    assert historical['receipt_only'] and historical['remote_revalidated'] is False


def test_updated_comment_new_request_preserves_old_revision(published):
    cfg, _, cloud, *_ = published
    first = call(published, dry_run=False, authorized=True)
    cloud.comments['pdf_en'][0]['content'] = 'en page 1: revised external review text.'
    with pytest.raises(StudioError) as error:
        call(published, dry_run=False, authorized=True)
    assert error.value.code == 'IDEMPOTENCY_CONFLICT'
    changed = call(published, request='changed', dry_run=False, authorized=True)
    assert changed['new_issue_revisions'] == changed['reused_issue_revisions'] == 1
    assert changed['snapshot_path'] != first['snapshot_path']
    assert len(list((Path(cfg['project_root']) / 'reviews').rglob('dr_*/*.json'))) == 3


@pytest.mark.parametrize('field,value', [('sha256Checksum', '0' * 64), ('size', '9999'), ('parents', ['other'])])
def test_remote_pdf_change_or_other_folder_refuses_collection(published, field, value):
    _, _, cloud, *_ = published
    cloud.metadata['pdf_en'][field] = value
    with pytest.raises(StudioError) as error:
        call(published, 'plan')
    assert error.value.code == 'REVIEW_REMOTE_CHANGED'


def test_no_completed_publication_and_corrupt_receipt_refuse(published):
    cfg, version, _, _, job = published
    status = json.loads((job / 'status.json').read_text())
    status['status'] = 'failed'
    (job / 'status.json').write_bytes(encoded(status))
    with pytest.raises(StudioError) as error:
        review.publication_context(cfg, version)
    assert error.value.code == 'REVIEW_PUBLICATION_REQUIRED'
    status['status'] = 'completed'
    (job / 'status.json').write_bytes(encoded(status))
    (job / 'output/receipt.json').write_bytes(b'{}')
    with pytest.raises(StudioError) as error:
        review.publication_context(cfg, version)
    assert error.value.code == 'ARTIFACT_TAMPERED'


def test_project_identity_authorization_and_output_scope_are_enforced(published):
    cfg, version, cloud, *_ = published
    with pytest.raises(StudioError) as error:
        review.dispatch(cfg, {'project_id': 'other', 'version': version}, drive=cloud)
    assert error.value.code == 'PROJECT_DENIED'
    with pytest.raises(StudioError) as error:
        call(published, dry_run=False)
    assert error.value.code == 'AUTHORIZATION_REQUIRED'
    cfg['writable_roots'] = ['other']
    with pytest.raises(StudioError) as error:
        call(published, dry_run=False, authorized=True)
    assert error.value.code == 'WRITE_DENIED'


def test_paginated_duplicate_threads_deduplicate_and_conflicts_refuse(published):
    _, _, cloud, *_ = published
    row = cloud.comments['pdf_en'][0]
    cloud.pages = {'first': {'comments': [row], 'nextPageToken': 'second'}, 'second': {'comments': [row]}}
    result = call(published, 'plan')
    assert result['comment_count'] == 2  # One per PDF despite duplicate provider pages.
    cloud.pages['second']['comments'] = [{**row, 'content': 'Changed while reading.'}]
    with pytest.raises(StudioError) as error:
        call(published, 'plan')
    assert error.value.code == 'REVIEW_COMMENT_CHANGED'


def test_existing_saved_issue_or_snapshot_is_never_silently_overwritten(published):
    cfg, *_ = published
    first = call(published, dry_run=False, authorized=True)
    path = Path(cfg['project_root']) / first['snapshot_path']
    path.write_bytes(b'owner-edited local review')
    with pytest.raises(StudioError) as error:
        call(published, dry_run=False, authorized=True)
    assert error.value.code == 'REVIEW_LOCAL_CHANGED'
    assert path.read_bytes() == b'owner-edited local review'


def test_public_gateway_uses_version_receipt_and_rejects_arbitrary_file_id(published, monkeypatch, tmp_path):
    from paper_studio.gateway.main import Gateway
    cfg, version, cloud, *_ = published
    current = {**cfg, 'schema_version': 1, 'software_id': 'webcodex-paper-studio',
               'instance_id': 'review-fixture', 'client_id': 'runner',
               'native_project': 'agent:runner:example',
               'native_artifact_project': 'agent:runner:example-artifacts',
               'admission_profile': 'paper-editor.v2'}
    config = tmp_path / 'gateway.json'
    config.write_bytes(encoded(current))
    monkeypatch.setattr(review, '_client', lambda _: cloud)
    gateway = Gateway(str(config))
    result = gateway.call('paper_review_comments', {'project_id': cfg['project_id'],
        'version': version, 'action': 'plan'})
    assert result['comment_count'] == 2 and result['remote_revalidated']
    with pytest.raises(StudioError):
        gateway.call('paper_review_comments', {'project_id': cfg['project_id'],
            'version': version, 'action': 'plan', 'file_id': 'an-unpublished-pdf'})


def test_read_only_client_retries_only_interrupted_gets_and_keeps_deadline(monkeypatch):
    calls = []
    client = object.__new__(review._ReadOnlyDrive)
    client.deadline = review.time.monotonic() + 1

    def request(self, method, path, body=None, content_type='application/json'):
        calls.append((method, path))
        if len(calls) < 3:
            raise StudioError('DRIVE_INTERRUPTED', 'test transport interruption')
        return {'comments': []}

    monkeypatch.setattr(review.Drive, 'request', request)
    assert client.request('GET', 'drive/v3/files/test/comments') == {'comments': []}
    assert len(calls) == 3
    with pytest.raises(StudioError) as error:
        client.request('POST', 'drive/v3/files/test/comments', {'content': 'forbidden'})
    assert error.value.code == 'REVIEW_READ_ONLY' and len(calls) == 3
    client.deadline = review.time.monotonic() - 1
    calls.clear()
    with pytest.raises(StudioError) as error:
        client.request('GET', 'drive/v3/files/test/comments')
    assert error.value.code == 'DRIVE_DEADLINE' and len(calls) == 1


def test_read_only_client_does_not_retry_drive_rejection(monkeypatch):
    calls = []
    client = object.__new__(review._ReadOnlyDrive)

    def request(*args, **kwargs):
        calls.append(True)
        raise StudioError('DRIVE_HTTP_ERROR', 'test API rejection')

    monkeypatch.setattr(review.Drive, 'request', request)
    with pytest.raises(StudioError) as error:
        client.request('GET', 'drive/v3/files/test/comments')
    assert error.value.code == 'DRIVE_HTTP_ERROR' and len(calls) == 1
