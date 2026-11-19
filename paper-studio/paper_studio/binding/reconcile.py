"""Reviewed hot repair of a bound project's publisher folder.

The full candidate is compared with the current JSON: only drive_folder_id may
change. Connection, project identity, scopes, authentication, runtime and service
files stay byte-identical. No service is stopped and no upload is performed.
"""
from __future__ import annotations

import json
from pathlib import Path
import stat

from management import paper_deploy as dep
from paper_studio.binding import transaction as tx
from paper_studio.binding.compatibility import check as check_compatibility
from paper_studio.binding.configuration import capture, preflight
from paper_studio.security.policy import encoded, no_links, sha

PLAN = 'paper-studio.reconcile-plan.v1'
RECORD = 'paper-studio.reconcile-record.v1'


def _check_guards(guards):
    for path, digest in guards.items():
        tx.require(tx.digest(tx.read(path, True)) == digest,
                   'RECONCILE_GUARD_CHANGED', 'A reviewed connection or project input changed')


def compute(p, candidate_path, operation_id):
    """Return a read-only plan plus exact before/after bytes and target checks."""
    tx.require(isinstance(operation_id, str) and tx.LABEL.fullmatch(operation_id),
               'INVALID_ARGUMENT', 'Bounded operation ID required')
    source = no_links(Path(p['config_path']))
    candidate_path = no_links(Path(candidate_path))
    tx.require(candidate_path != source, 'RECONCILE_CANDIDATE_INVALID',
               'Review a separate complete candidate file')
    before, proposed = tx.read(source), tx.read(candidate_path)
    old, new = json.loads(before), json.loads(proposed)
    tx.require(isinstance(old, dict) and isinstance(new, dict) and not old.get('projects'),
               'RECONCILE_CANDIDATE_INVALID', 'A single currently bound project is required')
    tx.require(old.get('instance_id') == p['instance_id'] and old.get('client_id') == p['client_id'],
               'IDENTITY_CONFLICT', 'Profile does not select this current connection')
    changed = {key for key in old.keys() | new.keys()
               if (key in old) != (key in new) or old.get(key) != new.get(key)}
    tx.require(changed == {'drive_folder_id'}, 'RECONCILE_SCOPE_DENIED',
               'Hot reconciliation permits only the bound project drive_folder_id field')
    tx.require(isinstance(new.get('drive_folder_id'), str) and bool(new['drive_folder_id']),
               'RECONCILE_CANDIDATE_INVALID', 'An explicit publisher folder ID is required')
    manifest = capture(new)
    original_manifest = capture(old)
    local = preflight(new, manifest)
    compatibility = check_compatibility(new, fixture_only=p['fixture_only'])
    if compatibility['status'] != 'skipped_fixture':
        tx.require(all(compatibility['capabilities'].get(name) == 'available'
                       for name in manifest['required_capabilities']),
                   'RUNTIME_CAPABILITY_MISSING', 'Installed runtime does not admit the repaired publisher')
    guards = {p['_path']: p['_sha256'],
              str(Path(new['project_root']) / 'PROJECT.yaml'): manifest['contract_sha256'],
              p['runner_config']: sha(tx.read(p['runner_config'])),
              p['tunnel']['profile_path']: sha(tx.read(p['tunnel']['profile_path'])),
              p['dropin_path']: tx.digest(tx.read(p['dropin_path'], True)),
              str(Path(__file__)): sha(tx.read(__file__))}
    guards.update(compatibility.get('guard_files', {}))
    for peer in p.get('peer_config_paths', []):
        guards[peer] = sha(tx.read(peer))
    tx.require(str(source) not in guards and str(candidate_path) not in guards,
               'RECONCILE_CANDIDATE_INVALID', 'Candidate and active configuration cannot alias guarded inputs')
    after = encoded(new)
    plan = {'schema': PLAN, 'operation_id': operation_id, 'instance_id': old['instance_id'],
            'client_id': old['client_id'], 'project_id': old['project_id'],
            'project_root': old['project_root'], 'state_root': old['state_root'],
            'profile_path': p['_path'], 'profile_sha256': p['_sha256'],
            'candidate_path': str(candidate_path), 'candidate_sha256': sha(proposed),
            'before_sha256': sha(before), 'after_sha256': sha(after),
            'mode': stat.S_IMODE(source.stat().st_mode), 'connection_guards': guards,
            'contract_sha256': manifest['contract_sha256'],
            'required_capabilities': manifest['required_capabilities'],
            'before_required_capabilities': original_manifest['required_capabilities'],
            'change': {'field': 'drive_folder_id', 'before': old.get('drive_folder_id'),
                       'after': new['drive_folder_id']},
            'local_preflight': local, 'runtime_compatibility': compatibility,
            'binding_changed': False, 'service_restart': False, 'upload_performed': False,
            'shared_auth_preserved': True}
    plan['plan_sha256'] = sha(encoded(plan))
    return plan, before, after, new, manifest


def _host(p, supplied):
    tx.require(not p['fixture_only'] or supplied is not None, 'FIXTURE_ONLY',
               'A fixture cannot use live services')
    if supplied is None:
        from paper_studio.binding.host import BindingHost
        supplied = BindingHost(p)
    return supplied


def _restore(p, host, record, backup, *, automatic):
    plan = record['plan']
    _check_guards(plan['connection_guards'])
    saved = no_links(backup / 'config.before')
    raw = tx.read(saved)
    tx.require(sha(raw) == plan['before_sha256'], 'BACKUP_HASH_MISMATCH',
               'Original configuration backup changed')
    current = tx.read(p['config_path'])
    allowed = {plan['before_sha256'], plan['after_sha256']} if automatic else {plan['after_sha256']}
    tx.require(sha(current) in allowed, 'ROLLBACK_CONCURRENT_CHANGE',
               'Current configuration is not a transaction-owned revision')
    old = json.loads(raw)
    host.singleton()
    host.idle(json.loads(current), old)
    tx.require(tx.read(p['config_path']) == current, 'ROLLBACK_CONCURRENT_CHANGE',
               'Configuration changed during recovery preflight')
    dep._write(Path(p['config_path']), raw, plan['mode'])
    host.readback(old, plan['contract_sha256'], plan['before_required_capabilities'])
    _check_guards(plan['connection_guards'])
    tx.require(tx.read(p['config_path']) == raw, 'ROLLBACK_READBACK_FAILED',
               'Original configuration bytes were not restored')


def apply(profile_path, plan_path, *, authorized=False, host=None):
    tx.require(authorized, 'AUTHORIZATION_REQUIRED', 'Apply requires the reviewed plan and authorization')
    p = tx.load_profile(profile_path)
    host = _host(p, host)
    reviewed = json.loads(tx.read(plan_path))
    tx.require(reviewed.get('schema') == PLAN, 'PLAN_INVALID', 'Expected a reconciliation plan')
    actual, before, after, cfg, manifest = compute(p, reviewed['candidate_path'], reviewed['operation_id'])
    tx.require(actual == reviewed, 'PLAN_STALE', 'Candidate or reviewed plan inputs changed')
    old = json.loads(before)
    host.singleton()
    host.idle(old, cfg)
    live = host.verify_target(cfg, manifest)
    backup = no_links(Path(p['backup_root']) / ('reconcile-' + reviewed['operation_id']))
    record_path = backup / 'reconcile.json'
    marker = Path(cfg['state_root']) / 'routing-unavailable.json'
    identity = {'schema': RECORD, 'operation_id': reviewed['operation_id'], 'plan_sha256': reviewed['plan_sha256']}
    with tx.state_locks([cfg['state_root']]):
        tx.require(not marker.exists(), 'BINDING_UNAVAILABLE', 'An existing routing marker requires recovery')
        host.idle(old, cfg)
        _check_guards(reviewed['connection_guards'])
        tx.require(tx.read(p['config_path']) == before and sha(tx.read(reviewed['candidate_path'])) == reviewed['candidate_sha256'],
                   'PLAN_STALE', 'Configuration or candidate changed before mutation')
        fresh = preflight(cfg, manifest)
        tx.require(fresh['build_targets'] == reviewed['local_preflight']['build_targets'],
                   'PLAN_STALE', 'Build inputs changed before reconciliation')
        backup.mkdir(parents=True, mode=0o700, exist_ok=False)
        tx.require(not backup.stat().st_mode & 0o077, 'BACKUP_PERMISSIONS', 'Private backup directory required')
        dep._private_write(backup / 'config.before', before)
        record = {'schema': RECORD, 'status': 'prepared', 'profile_path': p['_path'],
                  'plan': reviewed, 'marker': str(marker), 'marker_identity': identity,
                  'live_preflight': live, 'secrets_in_record': False}
        dep._record(record_path, record)
        owned = False
        try:
            dep._private_write(marker, encoded(identity)); owned = True
            record['status'] = 'applying'; dep._record(record_path, record)
            tx.require(tx.read(p['config_path']) == before, 'CONCURRENT_CHANGE',
                       'Configuration changed before the atomic repair write')
            dep._write(Path(p['config_path']), after, reviewed['mode'])
            readback = host.readback(cfg, manifest['contract_sha256'], manifest['required_capabilities'])
            _check_guards(reviewed['connection_guards'])
            tx.require(tx.read(p['config_path']) == after, 'READBACK_CHANGED', 'Configuration changed during verification')
            record.update(status='applied', readback=readback)
            dep._record(record_path, record)
            tx._clear_markers([marker], identity)
        except Exception as error:
            record['error_code'] = getattr(error, 'code', type(error).__name__)
            try:
                if owned:
                    _restore(p, host, record, backup, automatic=True)
                record['status'] = 'rolled_back' if owned else 'failed_before_change'
                dep._record(record_path, record)
                if owned:
                    tx._clear_markers([marker], identity)
            except Exception as recovery_error:
                record.update(status='rollback_failed', recovery_error_code=getattr(recovery_error, 'code', type(recovery_error).__name__))
                dep._record(record_path, record)
            raise
    return {'status': 'applied', 'record': str(record_path), 'project_id': cfg['project_id'],
            'binding_changed': False, 'service_restart': False, 'readback': readback}


def rollback(record_path, *, authorized=False, host=None, recover=False):
    tx.require(authorized, 'AUTHORIZATION_REQUIRED', 'Rollback requires authorization')
    rp = no_links(Path(record_path))
    record = json.loads(tx.read(rp))
    tx.require(record.get('schema') == RECORD, 'RECORD_INVALID', 'Expected a reconciliation record')
    allowed = {'prepared', 'applying', 'applied', 'rollback_failed', 'rolled_back'} if recover else {'applied'}
    tx.require(record.get('status') in allowed, 'RECORD_STATE_INVALID', 'Record cannot be rolled back in this state')
    p = tx.load_profile(record['profile_path'])
    host = _host(p, host)
    plan = record['plan']
    tx.require(p['_sha256'] == plan['profile_sha256'], 'PROFILE_CHANGED', 'Original connection profile required')
    backup = no_links(Path(p['backup_root']) / ('reconcile-' + plan['operation_id']))
    tx.require(rp == backup / 'reconcile.json', 'RECORD_PATH_INVALID', 'Record is outside its private transaction directory')
    marker = Path(plan['state_root']) / 'routing-unavailable.json'
    tx.require(record['marker'] == str(marker), 'RECORD_INVALID', 'Unexpected routing marker')
    identity = record['marker_identity']
    with tx.state_locks([plan['state_root']]):
        _check_guards(plan['connection_guards'])
        # An active Native job must be rejected before closing a healthy route.
        current = json.loads(tx.read(p['config_path']))
        original = tx.read(backup / 'config.before')
        tx.require(sha(original) == plan['before_sha256'], 'BACKUP_HASH_MISMATCH', 'Original backup changed')
        host.singleton(); host.idle(current, json.loads(original))
        if marker.exists():
            tx.require(recover and json.loads(tx.read(marker)) == identity,
                       'BINDING_UNAVAILABLE', 'Existing marker belongs to another operation')
        else:
            tx.require(record['status'] != 'rolled_back', 'RECORD_STATE_INVALID', 'Completed rollback has no pending recovery')
            dep._private_write(marker, encoded(identity))
        try:
            _restore(p, host, record, backup, automatic=recover)
            record['status'] = 'rolled_back'; dep._record(rp, record)
            tx._clear_markers([marker], identity)
        except Exception as error:
            record.update(status='rollback_failed', recovery_error_code=getattr(error, 'code', type(error).__name__))
            dep._record(rp, record)
            raise
    return {'status': 'rolled_back', 'record': str(rp), 'binding_changed': False, 'service_restart': False}
