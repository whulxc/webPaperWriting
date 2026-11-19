"""Local, evidence-bound adoption of an existing empty version folder.

Legacy publishers created the folder before saving its ID. This administrative
repair never uploads, creates a remote object, rewrites a failed job, or labels a
transport error as OAuth failure. Cloud resumes the original failed ancestry.
"""
from datetime import datetime
import json
from pathlib import Path

from paper_studio.drive import publisher as p
from paper_studio.provider import jobs
from paper_studio.security.policy import (atomic_json, encoded, fail, locked,
                                         no_links, safe_read, sha)

SCHEMA = 'paper-studio.drive-empty-folder-adoption.v1'


def _require(value, message):
    if not value:
        fail('DRIVE_FOLDER_ADOPTION_DENIED', message)


def _paths(cfg, version, root_id):
    # Reuse label and rb identity validation without accepting caller paths.
    p.recovery_receipt_path(cfg, version, root_id)
    state = no_links(Path(cfg['state_root']))
    return (state / 'drive-folder-adoptions' / version / (root_id + '.json'),
            state / 'drive-publications' / (version + '.json'))


def prepare(cfg, *, version, resume_build_id, folder_id, expected_manifest_sha256, drive=None):
    """Return a hash-fenced plan after full local and remote read-only checks."""
    folder_id = p._drive_id(folder_id)
    chain = jobs.drive_resume_chain(cfg, resume_build_id, version, expected_manifest_sha256)
    root_id = chain[-1]
    proof_path, checkpoint_path = _paths(cfg, version, root_id)
    _require(not jobs.active_jobs(cfg), 'Wait until project jobs are inactive before local folder adoption')
    _, manifest, identity = p.verified_manifest(cfg, version)
    _require(identity['sha256'] == expected_manifest_sha256, 'Selected immutable manifest changed')
    records = []
    for bid in chain:
        job = jobs.job_path(cfg, bid)
        status_raw = safe_read(job, 'status.json', p.RESPONSE_LIMIT)
        request_raw = safe_read(job, 'request.json', p.RESPONSE_LIMIT)
        status = json.loads(status_raw); request = json.loads(request_raw)
        receipt_raw = safe_read(job / 'output', 'receipt.json', p.RESPONSE_LIMIT)
        receipt = json.loads(receipt_raw)
        artifacts = [x for x in status.get('artifacts', []) if x.get('path') == 'receipt.json']
        result = status.get('result', {})
        _require(status.get('status') == 'failed' and len(artifacts) == 1
                 and artifacts[0].get('sha256') == sha(receipt_raw)
                 and artifacts[0].get('bytes') == len(receipt_raw), 'Failed job receipt identity changed')
        _require(status.get('error', {}).get('code') == ('DRIVE_INTERRUPTED' if bid == root_id else 'DRIVE_RECOVERY_NOT_READY'),
                 'Only the specific missing-checkpoint transport failure ancestry can be adopted')
        for value in (receipt, result):
            _require(value.get('status') == 'failed_before_upload' and value.get('upload_complete') is False
                     and value.get('version') == version and value.get('version_manifest_sha256') == expected_manifest_sha256
                     and not value.get('files') and 'folder_id' not in value and 'current_upload' not in value,
                     'Failure does not prove the legacy pre-file-upload checkpoint gap')
        _require(result.get('file_count') == 0 and result.get('files_truncated') is False,
                 'Failed job reports possible file uploads')
        _require(request.get('project_id') == cfg['project_id'] and request.get('kind') == 'drive'
                 and request.get('request', {}).get('expected_manifest_sha256') == expected_manifest_sha256,
                 'Failed request belongs to another publication')
        sidecar = p.recovery_receipt_path(cfg, version, bid)
        _require(not sidecar.exists() and not (sidecar.parent / 'progress.json').exists(),
                 'Existing recovery progress must be reconciled instead of adopted')
        records.append({'build_id': bid, 'status_sha256': sha(status_raw),
                        'request_sha256': sha(request_raw), 'receipt_sha256': sha(receipt_raw)})
    root_status = jobs.read_status(cfg, root_id)
    start = root_status.get('started_at'); finish = root_status.get('finished_at')
    _require(type(start) in (int, float) and type(finish) in (int, float) and start <= finish,
             'Original failed job needs its actual execution window')
    if drive is None:
        import time
        drive = p.RecoveryDrive(cfg['drive_oauth_file'], persist_access_token=False, deadline=time.monotonic() + 120)
    parent = p._drive_id(cfg.get('drive_folder_id'))
    p._verify_recovery_folders(drive, cfg, version, folder_id, parent)
    folder = drive.private(folder_id); project_folder = folder['parents'][0]
    # Exact IDs alone do not excuse ambiguous copied names in this hierarchy.
    for container, name, expected in [(parent, cfg['project_id'], project_folder), (project_folder, version, folder_id)]:
        rows = p._folder_index(drive, container)['names'].get(name, [])
        _require(len(rows) == 1 and rows[0].get('id') == expected
                 and rows[0].get('mimeType') == 'application/vnd.google-apps.folder',
                 'Project/version folder identity is ambiguous')
    metadata = drive.request('GET', 'drive/v3/files/' + folder_id
                             + '?fields=id,name,mimeType,parents,createdTime,trashed')
    _require(metadata.get('id') == folder_id and metadata.get('name') == version
             and metadata.get('parents') == [project_folder] and metadata.get('trashed') is False
             and metadata.get('mimeType') == 'application/vnd.google-apps.folder', 'Version folder metadata changed')
    try:
        created = datetime.fromisoformat(metadata['createdTime'].replace('Z', '+00:00')).timestamp()
    except (KeyError, TypeError, ValueError):
        fail('DRIVE_FOLDER_ADOPTION_DENIED', 'Folder creation time is missing or invalid')
    _require(start - 5 <= created <= finish + 5, 'Folder creation time is outside the failed worker execution window')
    inventory = p._folder_index(drive, folder_id)
    _require(inventory['rows'] == 0 and not inventory['names'],
             'This narrow repair accepts only a fully enumerated empty folder; preserve any existing objects')
    proof = {'schema': SCHEMA, 'project_id': cfg['project_id'], 'project_root': cfg['project_root'],
             'state_root': cfg['state_root'], 'version': version, 'folder_id': folder_id,
             'drive_parent_id': parent, 'project_folder_id': project_folder,
             'version_manifest_sha256': expected_manifest_sha256, 'resume_build_id': resume_build_id,
             'original_build_id': root_id, 'failed_jobs': records,
             'folder_created_time': metadata['createdTime'], 'job_execution_window': [start, finish],
             'owner_only_verified': True, 'complete_remote_child_count': 0,
             'pdfs_observed': 0, 'remote_writes': 0,
             'checkpoint_origin': 'explicit_local_adoption_after_legacy_folder_creation_failure'}
    if proof_path.exists():
        _require(safe_read(proof_path.parent, proof_path.name, p.RESPONSE_LIMIT) == encoded(proof),
                 'Existing adoption evidence differs from this exact failure and folder')
    checkpoint = {'schema': 'paper-studio.drive-publication.v1', 'project_id': cfg['project_id'],
                  'version': version, 'folder_id': folder_id, 'version_manifest_sha256': expected_manifest_sha256,
                  'files': [], 'upload_complete': False, 'status': 'failed',
                  'chatgpt_fetched': False, 'pages_seen': False, 'human_accepted': False,
                  'checkpoint_origin': proof['checkpoint_origin'], 'adoption_evidence_sha256': sha(encoded(proof)),
                  'adopted_from_failed_build_id': root_id, 'original_failed_history_preserved': True}
    if checkpoint_path.exists():
        _require(safe_read(checkpoint_path.parent, checkpoint_path.name, p.RESPONSE_LIMIT) == encoded(checkpoint),
                 'Publication state already exists and must not be replaced')
    return {'proof': proof, 'checkpoint': checkpoint,
            'plan_sha256': sha(encoded({'proof': proof, 'checkpoint': checkpoint})),
            'writes_drive': False, 'would_write_local_checkpoint': not checkpoint_path.exists()}


def apply(cfg, *, authorized=False, expected_plan_sha256, **arguments):
    if authorized is not True:
        fail('AUTHORIZATION_REQUIRED', 'Explicit local approval is required to adopt a remote folder')
    with locked(cfg, 'drive-version-' + sha(arguments['version'].encode())[:24]):
        plan = prepare(cfg, **arguments)
        _require(plan['plan_sha256'] == expected_plan_sha256, 'Reviewed folder adoption plan changed')
        # Verify the entire remote emptiness/hierarchy and local history again.
        _require(prepare(cfg, **arguments)['plan_sha256'] == plan['plan_sha256'],
                 'Folder or failed history changed before local checkpoint creation')
        proof = plan['proof']; path, checkpoint = _paths(cfg, arguments['version'], proof['original_build_id'])
        for directory in [path.parent.parent, path.parent, checkpoint.parent]:
            p._private_state_directory(directory)
        replayed = checkpoint.exists()
        if not path.exists(): atomic_json(path, proof, exclusive=True)
        if not replayed: atomic_json(checkpoint, plan['checkpoint'], exclusive=True)
        return {'status': 'adopted', 'replayed': replayed, 'folder_id': proof['folder_id'],
                'version': proof['version'], 'plan_sha256': plan['plan_sha256'],
                'adoption_evidence_sha256': sha(encoded(proof)), 'remote_writes': 0,
                'resume_build_id': proof['resume_build_id'], 'original_failed_history_preserved': True}
