"""Reviewed, repeatable single-connection project rebinding.

Management only: preserves the installed runtime and Tunnel command. Project
configuration is selected from a catalog; no paper or credential is migrated.
"""
from __future__ import annotations

import contextlib
import copy
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import tomllib

import yaml
from management import paper_deploy as dep
from paper_studio.binding.toml import dump_toml
from paper_studio.security.policy import encoded, fail, load_config, no_links, sha
from paper_studio.binding.configuration import capture, compose, load_manifest, preflight, dump_manifest
from paper_studio.binding.compatibility import check as check_compatibility

PROFILE = 'paper-studio.rebind-instance.v1'
PLAN = 'paper-studio.rebind-plan.v1'
RECORD = 'paper-studio.rebind-record.v1'
LABEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}')


def require(condition, code, message):
    if not condition:
        fail(code, message)


def read(path, missing=False):
    return dep.read_file(no_links(Path(path)), missing_ok=missing)


def digest(raw):
    return sha(raw) if raw is not None else None


def load_profile(path):
    raw = read(path)
    p = json.loads(raw)
    required = {'schema', 'instance_id', 'client_id', 'config_path', 'runner_config',
                'runner_service', 'tunnel', 'dropin_path', 'backup_root', 'project_catalog',
                'readiness', 'legacy_job_roots', 'fixture_only'}
    require(required <= set(p) and not set(p) - required - {'peer_config_paths'} and p['schema'] == PROFILE,
            'PROFILE_INVALID', 'Exact rebind instance profile required')
    for key in ('instance_id', 'client_id'):
        require(isinstance(p[key], str) and LABEL.fullmatch(p[key]), 'PROFILE_INVALID', 'Invalid instance identity')
    require(re.fullmatch(r'[A-Za-z0-9_.@-]+\.service', p['runner_service']) is not None,
            'PROFILE_INVALID', 'Explicit Runner service required')
    for key in ('config_path', 'runner_config', 'dropin_path', 'backup_root', 'project_catalog'):
        no_links(Path(p[key]))
    t = p['tunnel']
    require(set(t) == {'alias', 'tunnel_id', 'profile_path'} and LABEL.fullmatch(t['alias'])
            and re.fullmatch(r'tunnel_[0-9a-f]{32}', t['tunnel_id']), 'PROFILE_INVALID', 'Explicit Tunnel identity required')
    no_links(Path(t['profile_path']))
    require(Path(t['profile_path']).name == t['alias'] + '.yaml', 'PROFILE_INVALID', 'Tunnel profile must match alias')
    r = p['readiness']
    require(set(r) == {'attempts', 'interval_seconds'} and type(r['attempts']) is int
            and 1 <= r['attempts'] <= 60 and type(r['interval_seconds']) in (int, float)
            and 0 <= r['interval_seconds'] <= 2, 'PROFILE_INVALID', 'Bounded readiness required')
    require(type(p['fixture_only']) is bool and isinstance(p['legacy_job_roots'], list), 'PROFILE_INVALID', 'Explicit fixture and legacy scope required')
    for root in p['legacy_job_roots']:
        no_links(Path(root))
    peers = p.get('peer_config_paths', [])
    require(isinstance(peers, list) and len(peers) <= 32 and len(set(peers)) == len(peers),
            'PROFILE_INVALID', 'Explicit bounded peer instance inventory required')
    for peer_path in peers:
        no_links(Path(peer_path))
    p['_path'], p['_sha256'] = str(Path(path)), sha(raw)
    return p


def catalog_path(p, project_id):
    require(isinstance(project_id, str) and LABEL.fullmatch(project_id), 'PROJECT_DENIED', 'Invalid catalog project ID')
    return no_links(Path(p['project_catalog']) / (project_id + '.json'))


def _path_unit(path):
    # systemd specifier expansion and list splitting must never reinterpret a
    # path. Quotes support spaces; reject controls, percent and escaping syntax.
    text = str(no_links(Path(path)))
    require(not any(ord(c) < 32 or c in '%"\\' for c in text), 'PATH_DENIED', 'Unsupported systemd path characters')
    return '"' + text + '"'


def _dropin(cfg, historical_roots, runner_state=None):
    root = Path(cfg['project_root'])
    writable = {str(root / rel) for rel in cfg['writable_roots']}
    writable.update([cfg['artifact_root'], cfg['state_root']])
    if runner_state is not None:
        writable.add(str(runner_state))
    # Provider lifecycle writes immutable artifacts; public editing remains
    # constrained by Gateway readonly_roots. More specific readonly exceptions
    # inside manuscript are maintained, except the explicit artifact store.
    readonly = {str(root), *historical_roots}
    readonly.update(str(root / rel) for rel in cfg['readonly_roots']
                    if not (root / rel).is_relative_to(Path(cfg['artifact_root'])))
    readonly.update(value for value in cfg.get('credentials', {}).values()
                    if isinstance(value, str) and value.startswith('/') and Path(value).exists())
    lines = ['[Service]', 'ReadWritePaths=', 'ReadOnlyPaths=',
             'ReadOnlyPaths=' + ' '.join(('-' if not Path(p).exists() else '') + _path_unit(p) for p in sorted(readonly)),
             'ReadWritePaths=' + ' '.join(_path_unit(p) for p in sorted(writable))]
    return ('\n'.join(lines) + '\n').encode()


def compute(p, project_id, operation_id):
    require(LABEL.fullmatch(operation_id), 'INVALID_ARGUMENT', 'Bounded operation ID required')
    raw_cfg = read(p['config_path'])
    old = json.loads(raw_cfg)
    require(old['instance_id'] == p['instance_id'] and old['client_id'] == p['client_id'], 'IDENTITY_CONFLICT', 'Profile does not select the current instance')
    manifest_path = catalog_path(p, project_id)
    manifest = load_manifest(manifest_path)
    require(manifest['project']['project_id'] == project_id, 'IDENTITY_CONFLICT', 'Catalog filename and project disagree')
    new = compose(old, manifest)
    local = preflight(new, manifest)
    compatibility = check_compatibility(new, fixture_only=p['fixture_only'])
    if compatibility['status'] != 'skipped_fixture':
        require(all(compatibility['capabilities'].get(name) == 'available'
                    for name in manifest['required_capabilities']), 'RUNTIME_CAPABILITY_MISSING',
                'Installed runtime does not provide every required target capability')
    require(not old.get('projects'), 'MULTI_PROJECT_UNSUPPORTED', 'Select a single-project connection')
    require(old['project_id'] == new['project_id'] or all(
        not Path(new[a]).is_relative_to(Path(old[b])) and not Path(old[b]).is_relative_to(Path(new[a]))
        for a in ('project_root', 'artifact_root', 'state_root')
        for b in ('project_root', 'artifact_root', 'state_root')),
        'PROJECT_BOUNDARY_OVERLAP', 'Different projects must use disjoint source, artifact and state roots')
    for key in ('project_root', 'artifact_root', 'state_root'):
        require(old['project_id'] != new['project_id'] or old[key] == new[key],
                'PROJECT_IDENTITY_CHANGED', 'Moving an existing project requires a separate migration')

    changes = {}
    guards = {str(manifest_path): sha(read(manifest_path)), str(Path(p['_path'])): p['_sha256']}
    guards.update(compatibility.get('guard_files', {}))
    def add(path, after):
        path = no_links(Path(path))
        require(path.parent.is_dir() or str(path) == p['dropin_path'], 'PARENT_MISSING', 'Managed file parent must already exist')
        before = read(path, True)
        if before != after:
            changes[path] = (before, after, stat.S_IMODE(path.stat().st_mode) if before is not None else 0o600)
        else:
            guards[str(path)] = digest(before)

    add(p['config_path'], encoded(new))
    runner_path = Path(p['runner_config'])
    runner = tomllib.loads(read(runner_path).decode())
    require(runner['client_id'] == p['client_id'] and type(runner['policy']['allow_raw_shell']) is bool
            and type(runner['policy']['allow_cwd_anywhere']) is bool,
            'RUNNER_POLICY_INVALID', 'Unexpected Runner identity or unclassified policy')
    providers = runner['plugins']['providers']
    require(sum(row.get('id') == old.get('provider_id', 'research-build') for row in providers) == 1,
            'PROVIDER_CONFLICT', 'Expected exactly one selected shared Provider')
    require(p['fixture_only'] or len(providers) == 1 or bool(p['legacy_job_roots']),
            'LEGACY_ACTIVITY_SCOPE_REQUIRED', 'Other Providers require explicit background job roots before live switching')
    # No release upgrade is hidden in rebinding. Bind all executable/config
    # command bytes as guards; actual readiness is checked on the same service.
    tunnel_raw = read(p['tunnel']['profile_path'])
    tunnel = yaml.safe_load(tunnel_raw)
    require(tunnel['control_plane']['tunnel_id'] == p['tunnel']['tunnel_id']
            and len(tunnel['mcp']['commands']) == 1, 'TUNNEL_IDENTITY_CONFLICT', 'Expected one command on the selected Tunnel')
    guards[p['tunnel']['profile_path']] = sha(tunnel_raw)
    registry = runner_path.parent / 'project-registry'
    require(registry.is_dir(), 'REGISTRY_MISSING', 'Existing Native project registry required')
    entries = {}
    for path in sorted(registry.glob('*.toml')):
        item = tomllib.loads(read(path).decode())
        require(isinstance(item.get('id'), str) and LABEL.fullmatch(item['id']) and item['id'] not in entries,
                'REGISTRY_INVALID', 'Unique bounded Native registration IDs required')
        no_links(Path(item['path']))
        entries[item['id']] = (path, item)
    expected = [(new['project_id'], new['project_root'], bool(new['writable_roots'])),
                (new['project_id'] + '-artifacts', new['artifact_root'], False)]
    historical = set()
    for ident, (path, item) in entries.items():
        if ident not in {x[0] for x in expected}:
            # An older registration may use a different alias for this exact
            # returning project. Keep it readonly, but do not mount the active
            # artifact directory read-only through that historical alias.
            if item['path'] not in {new['project_root'], new['artifact_root']}:
                historical.add(item['path'])
            add(path, dump_toml(dict(item, allow_patch=False)))
    for ident, root, editable in expected:
        require(LABEL.fullmatch(ident), 'PROJECT_ID_TOO_LONG', 'Project ID must leave room for artifact registration suffix')
        if ident in entries:
            path, item = entries[ident]
            require(item['path'] == root, 'REGISTRY_COLLISION', 'Existing target ID points at a different root')
        else:
            path, item = registry / (ident + '.toml'), {'id': ident, 'path': root}
            if path.exists():
                # Old registries may name files after a paper while using a
                # different internal ID. Preserve that historical record.
                path = registry / ('paper-studio-binding-' + ident + '.toml')
            require(not path.exists(), 'REGISTRY_COLLISION', 'Registration filename collision')
        add(path, dump_toml(dict(item, allow_patch=editable)))
    require(all(not Path(new['project_root']).is_relative_to(Path(h)) and not Path(h).is_relative_to(Path(new['project_root']))
                for h in historical), 'REGISTRY_OVERLAP', 'Target cannot overlap a retained historical registration')
    runner['policy']['allowed_roots'] = sorted({new['project_root'], new['artifact_root']})
    add(runner_path, dump_toml(runner))
    add(p['dropin_path'], _dropin(new, historical, runner_path.parent))
    dropin = Path(p['dropin_path'])
    require(dropin.name.endswith('.conf'), 'PROFILE_INVALID', 'Managed systemd drop-in must end in .conf')
    for other_dropin in sorted(dropin.parent.glob('*.conf')):
        if other_dropin == dropin:
            continue
        raw = read(other_dropin)
        require(other_dropin.name < dropin.name or not re.search(rb'^\s*Read(?:Write|Only)Paths\s*=', raw, re.M),
                'SYSTEMD_OVERRIDE_CONFLICT', 'A later drop-in would override the reviewed filesystem scope')
        guards[str(other_dropin)] = sha(raw)
    for peer in p.get('peer_config_paths', []):
        if peer == p['config_path']:
            continue
        raw = read(peer)
        other = load_config(peer)
        for bound in [other, *other.get('_projects', [])]:
            require(all(not Path(new[a]).is_relative_to(Path(bound[b]))
                        and not Path(bound[b]).is_relative_to(Path(new[a]))
                        for a in ('project_root', 'artifact_root', 'state_root')
                        for b in ('project_root', 'artifact_root', 'state_root')),
                    'PROJECT_ALREADY_BOUND', 'Target overlaps a project registered on another connection')
        guards[peer] = sha(raw)
    # Refresh the outgoing project profile from its current live configuration.
    # This captures later targets (e.g. method_zh_r05) and directory repairs.
    if old['project_id'] != new['project_id']:
        outgoing = capture(old)
        outgoing_path = catalog_path(p, old['project_id'])
        if outgoing_path.exists():
            previous = load_manifest(outgoing_path)
            require(previous['project']['project_root'] == old['project_root'], 'CATALOG_COLLISION', 'Outgoing project identity differs from its catalog entry')
            outgoing['required_capabilities'] = sorted(set(outgoing['required_capabilities']) | set(previous['required_capabilities']))
            outgoing['required_build_targets'] = sorted(set(outgoing['required_build_targets']) | set(previous['required_build_targets']))
        from paper_studio.capabilities import describe_capabilities
        available = {row['name']: row for row in describe_capabilities(old)['tools']}
        missing = [{key: available[name][key] for key in ('name', 'status', 'reason')}
                   for name in outgoing['required_capabilities'] if available[name]['status'] != 'available']
        targets = old.get('execution_policy', {}).get('tex_targets', {})
        missing_targets = [name for name in outgoing['required_build_targets'] if name not in targets]
        if missing or missing_targets:
            fail('SOURCE_PROFILE_STALE',
                 'Outgoing project requirements differ from its active configuration; recapture and review its project profile before switching',
                 project_id=old['project_id'], missing_capabilities=missing,
                 missing_build_targets=missing_targets)
        add(outgoing_path, dump_manifest(outgoing))
    old_contract = Path(old['project_root']) / 'PROJECT.yaml'
    guards[str(old_contract)] = sha(read(old_contract))
    guards[str(Path(new['project_root']) / 'PROJECT.yaml')] = manifest['contract_sha256']
    for filename in ('configuration.py', 'host.py', 'transaction.py', 'compatibility.py', 'toml.py'):
        path = Path(__file__).with_name(filename)
        guards[str(path)] = sha(read(path))
    management_root = Path(dep.__file__).parent
    for filename in ('paper_deploy.py', 'paper_release.py', 'paper_build_recovery.py',
                     'paper_tunnels.py', 'paper_instance_registry.py', 'native_client.py'):
        path = management_root / filename
        guards[str(path)] = sha(read(path))
    # Catalog inputs can be an outgoing write only on an invalid same-ID alias.
    require(not set(guards) & {str(x) for x in changes}, 'PLAN_ALIAS', 'A guarded input is also a transaction output')
    plan = {'schema': PLAN, 'operation_id': operation_id, 'instance_id': p['instance_id'],
            'project_id': project_id, 'before_project_id': old['project_id'],
            'profile_path': p['_path'], 'profile_sha256': p['_sha256'],
            'manifest_path': str(manifest_path), 'manifest_sha256': sha(read(manifest_path)),
            'manager_sha256': sha(read(__file__)), 'guards': guards,
            'source_state_root': old['state_root'], 'target_state_root': new['state_root'],
            'project_boundaries': [{k: cfg[k] for k in ('project_id', 'project_root', 'artifact_root', 'state_root')}
                                   | {'artifact_layout': cfg.get('artifact_layout'),
                                      'job_index': cfg.get('job_index',{'enabled':False})}
                                   for cfg in (old, new)],
            'registry_files_before': sorted(str(x) for x in registry.glob('*.toml')),
            'dropin_files_before': sorted(str(x) for x in dropin.parent.glob('*.conf')),
            'source_contract_sha256': guards[str(old_contract)], 'target_contract_sha256': manifest['contract_sha256'],
            'required_capabilities': manifest['required_capabilities'],
            'local_preflight': local, 'shared_auth_preserved': True,
            'installed_runtime_compatibility': {k: v for k, v in compatibility.items() if k != 'guard_files'},
            'runtime_release_changed': False, 'upload_performed': False,
            'files': [{'path': str(path), 'before_sha256': digest(before), 'after_sha256': sha(after), 'mode': mode}
                      for path, (before, after, mode) in sorted(changes.items())]}
    plan['plan_sha256'] = sha(encoded(plan))
    return plan, changes, old, new, manifest


@contextlib.contextmanager
def state_locks(roots):
    handles = []
    try:
        for root in sorted(set(roots)):
            directory = no_links(Path(root))
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            for name in ('routing', 'build'):
                fd = os.open(directory / (name + '.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                handles.append(fd)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        for fd in reversed(handles):
            os.close(fd)


def _guards(plan):
    for path, expected in plan['guards'].items():
        require(digest(read(path, True)) == expected, 'PLAN_STALE', 'Guarded input changed')


def _stop(p, host):
    host.singleton()
    alias = p['tunnel']['alias']
    host.tunnel('stop', alias, '--json')
    require(host.tunnel_status(alias).get('process_running') is False, 'STOP_UNCONFIRMED', 'Tunnel stop must be confirmed')
    host.systemctl('stop', p['runner_service'])
    require(host.service_stopped(), 'STOP_UNCONFIRMED', 'Runner stop must be confirmed')


def _start(p, host, tunnel_bytes):
    host.systemctl('daemon-reload')
    host.systemctl('start', p['runner_service'])
    dep.wait_ready(p, host, tunnel=False)
    host.singleton()
    require(host.tunnel_status(p['tunnel']['alias']).get('process_running') is False,
            'STDIO_NOT_STOPPED', 'Refuse a second or unclassified stdio connection')
    result = dep._connect(p, host, dep._command(tunnel_bytes), dep._tunnel_secret(p), None)
    dep._restore_profile_bytes(p, tunnel_bytes)
    return result


def _markers(roots):
    return [Path(r) / 'routing-unavailable.json' for r in sorted(set(roots))]


def _clear_markers(paths, identity):
    for path in paths:
        require(json.loads(read(path)) == identity, 'MARKER_CONFLICT', 'Routing marker belongs to another transaction')
    for path in paths:
        path.unlink()


def apply(profile_path, plan_path, *, authorized=False, host=None):
    require(authorized, 'AUTHORIZATION_REQUIRED', 'Apply requires the reviewed plan and explicit authorization')
    p = load_profile(profile_path)
    require(not p['fixture_only'] or host is not None, 'FIXTURE_ONLY', 'Fixture profile cannot operate live services')
    reviewed = json.loads(read(plan_path))
    current, changes, old, new, manifest = compute(p, reviewed['project_id'], reviewed['operation_id'])
    require(current == reviewed, 'PLAN_STALE', 'Plan or its inputs changed; prepare a new plan')
    if host is None:
        from paper_studio.binding.host import BindingHost
        host = BindingHost(p)
    host.singleton()
    host.idle(old, new)
    live_preflight = host.verify_target(new, manifest)
    roots = [old['state_root'], new['state_root']]
    markers = _markers(roots)
    identity = {'schema': RECORD, 'operation_id': current['operation_id'], 'plan_sha256': current['plan_sha256']}
    backup = no_links(Path(p['backup_root']) / current['operation_id'])
    record_path = backup / 'binding.json'
    with state_locks(roots):
        require(not any(path.exists() for path in markers), 'BINDING_UNAVAILABLE', 'Existing routing marker requires recovery')
        host.idle(old, new)
        _guards(reviewed)
        require(sorted(str(x) for x in (Path(p['runner_config']).parent / 'project-registry').glob('*.toml'))
                == reviewed['registry_files_before'], 'PLAN_STALE', 'Native registry inventory changed')
        require(sorted(str(x) for x in Path(p['dropin_path']).parent.glob('*.conf')) == reviewed['dropin_files_before'],
                'PLAN_STALE', 'Runner drop-in inventory changed')
        for path, (before, _, _) in changes.items():
            require(read(path, True) == before, 'PLAN_STALE', 'Managed file changed before locking')
        # Lock acquisition may create the target state directory. Revalidate
        # target inputs without comparing that harmless existence observation.
        fresh_local = preflight(new, manifest)
        require(fresh_local['build_targets'] == reviewed['local_preflight']['build_targets'],
                'PLAN_STALE', 'Build inputs changed before switching')
        backup.mkdir(parents=True, mode=0o700, exist_ok=False)
        require(not backup.stat().st_mode & 0o077, 'BACKUP_PERMISSIONS', 'Private transaction directory required')
        record = {'schema': RECORD, 'status': 'prepared', 'plan': reviewed,
                  'profile_path': p['_path'], 'files': [], 'live_preflight': live_preflight,
                  'markers': [str(x) for x in markers], 'marker_identity': identity, 'secrets_in_record': False}
        for index, (path, (before, after, mode)) in enumerate(changes.items()):
            saved = backup / f'{index}.before'
            if before is not None:
                dep._private_write(saved, before)
            record['files'].append({'path': str(path), 'backup': str(saved) if before is not None else None,
                                    'before_sha256': digest(before), 'after_sha256': sha(after), 'mode': mode})
        dep._record(record_path, record)
        owned = []
        transition = False
        try:
            for marker in markers:
                dep._private_write(marker, encoded(identity))
                owned.append(marker)
            record['status'] = 'switching'; dep._record(record_path, record)
            transition = True
            _stop(p, host)
            _guards(reviewed)
            for path, (before, _, _) in changes.items():
                require(read(path, True) == before, 'CONCURRENT_CHANGE', 'Managed file changed before switching')
            for path, (_, after, mode) in changes.items():
                if str(path) == p['dropin_path']:
                    no_links(path.parent).mkdir(parents=True, exist_ok=True, mode=0o700)
                dep._write(path, after, mode)
            for directory in reviewed['local_preflight'].get('pending_directories', []):
                no_links(Path(directory)).mkdir(parents=True, exist_ok=True, mode=0o700)
            tunnel_bytes = read(p['tunnel']['profile_path'])
            observed = _start(p, host, tunnel_bytes)
            verified = host.readback(new, current['target_contract_sha256'], current['required_capabilities'])
            require(preflight(new, manifest)['build_targets'] == reviewed['local_preflight']['build_targets'],
                    'READBACK_CHANGED', 'Build inputs changed during switching')
            _guards(reviewed)
            for row in record['files']:
                require(digest(read(row['path'], True)) == row['after_sha256'], 'READBACK_CHANGED', 'Managed file changed during verification')
            record.update(status='applied', observations=observed, readback=verified)
            dep._record(record_path, record)
            _clear_markers(markers, identity)
        except Exception as error:
            record['error_code'] = getattr(error, 'code', type(error).__name__)
            try:
                if transition:
                    _restore(p, host, record, backup, automatic=True)
                record['status'] = 'rolled_back' if transition else 'failed_before_change'
                dep._record(record_path, record)
                _clear_markers(owned, identity)
            except Exception as recovery_error:
                record.update(status='rollback_failed', recovery_error_code=getattr(recovery_error, 'code', type(recovery_error).__name__))
                dep._record(record_path, record)
            raise
    return {'status': 'applied', 'record': str(record_path), 'project_id': new['project_id'], 'readback': verified}


def _restore(p, host, record, backup, *, automatic=False):
    # Validate every backup and every current file BEFORE restoring any file.
    # A changed Tunnel profile must never be used to reconnect while recovering.
    # Target contract edits do not prevent returning to the old project, but
    # the selected instance profile, Tunnel and old contract remain pinned.
    for path in (p['_path'], p['tunnel']['profile_path']):
        require(digest(read(path)) == record['plan']['guards'][path],
                'ROLLBACK_GUARD_CHANGED', 'Connection guard changed; recovery remains closed')
    from management.paper_instance_registry import activity_snapshot
    from paper_studio.drive.sync_tasks import active_tasks
    from paper_studio.security.policy import builds_root
    from paper_studio.provider import job_index
    proofs=[]
    config_row=next((row for row in record['files'] if row['path']==p['config_path']),None)
    if config_row:
        current=read(p['config_path'],True)
        if current is not None and digest(current) in {config_row['before_sha256'],config_row['after_sha256']}:
            proofs.append(json.loads(current))
        if config_row.get('backup'):
            prior_path=no_links(Path(config_row['backup']))
            require(prior_path.parent==backup,'BACKUP_PATH_INVALID','Config proof must belong to the reviewed backup')
            prior=read(prior_path)
            require(digest(prior)==config_row['before_sha256'],'BACKUP_HASH_MISMATCH','Recovery config proof changed')
            proofs.append(json.loads(prior))
    for boundary in record['plan']['project_boundaries']:
        cfg = job_index.recovery_config(boundary,proofs)
        activity = activity_snapshot(cfg)
        require(activity['active_count'] == activity['unknown_records'] == 0 and not active_tasks(cfg),
                'ROLLBACK_BUSY', 'Source or target has active or unknown local work')
        status_paths = [] if job_index.enabled(cfg) else builds_root(cfg).glob('rb_*/status.json')
        for status_path in status_paths:
            value = json.loads(read(status_path))
            terminal=value.get('status') in {'completed', 'failed'}
            if terminal and value.get('recovery_required'):
                from paper_studio.provider.operator_recovery import verified_operator_closure
                terminal=verified_operator_closure(cfg,status_path.parent.name,value)
            require(terminal,
                    'ROLLBACK_BUSY', 'Unclassified build prevents recovery')
    restored = []
    for row in record['files']:
        path = no_links(Path(row['path']))
        actual = digest(read(path, True))
        allowed = {row['after_sha256'], row['before_sha256']} if automatic else {row['after_sha256']}
        require(actual in allowed, 'ROLLBACK_CONCURRENT_CHANGE', 'Current file is not a transaction-owned revision')
        before = None
        if row['backup'] is not None:
            saved = no_links(Path(row['backup']))
            require(saved.parent == backup, 'BACKUP_PATH_INVALID', 'Backup must belong to this transaction')
            before = read(saved)
            require(sha(before) == row['before_sha256'], 'BACKUP_HASH_MISMATCH', 'Backup identity changed')
        else:
            require(row['before_sha256'] is None, 'BACKUP_MISSING', 'Existing file needs an original backup')
        restored.append((path, before, row['mode']))
    _stop(p, host)
    # Recheck after service stop before touching files; do not overwrite edits.
    for row in record['files']:
        allowed = {row['after_sha256'], row['before_sha256']} if automatic else {row['after_sha256']}
        require(digest(read(row['path'], True)) in allowed, 'ROLLBACK_CONCURRENT_CHANGE', 'Concurrent rollback edit')
    for path, before, mode in reversed(restored):
        if before is None:
            if path.exists():
                path.unlink()
        else:
            dep._write(path, before, mode)
    _start(p, host, read(p['tunnel']['profile_path']))
    old = load_config(p['config_path'])
    host.readback(old, record['plan']['source_contract_sha256'], [])
    for row in record['files']:
        require(digest(read(row['path'], True)) == row['before_sha256'], 'ROLLBACK_READBACK_FAILED', 'Original bytes not restored')


def rollback(record_path, *, authorized=False, host=None, recover=False):
    require(authorized, 'AUTHORIZATION_REQUIRED', 'Rollback requires explicit authorization')
    rp = no_links(Path(record_path))
    record = json.loads(read(rp))
    require(record.get('schema') == RECORD, 'RECORD_INVALID', 'Expected rebind transaction record')
    states = {'prepared', 'switching', 'rollback_failed', 'applied', 'rolled_back'} if recover else {'applied'}
    require(record['status'] in states, 'RECORD_STATE_INVALID', 'Transaction cannot be rolled back in this state')
    p = load_profile(record['profile_path'])
    require(p['_sha256'] == record['plan']['profile_sha256'], 'PROFILE_CHANGED', 'Original instance profile required')
    backup = Path(p['backup_root']) / record['plan']['operation_id']
    require(rp == backup / 'binding.json', 'RECORD_PATH_INVALID', 'Record must belong to selected private backup root')
    require(not p['fixture_only'] or host is not None, 'FIXTURE_ONLY', 'Fixture profile cannot operate live services')
    if host is None:
        from paper_studio.binding.host import BindingHost
        host = BindingHost(p)
    markers = _markers([record['plan']['source_state_root'], record['plan']['target_state_root']])
    require(record['markers'] == [str(x) for x in markers], 'RECORD_INVALID', 'Unexpected routing markers')
    if record['status'] == 'rolled_back':
        require(recover and any(path.exists() for path in markers), 'RECORD_STATE_INVALID',
                'Completed rollback has no pending routing marker to recover')
    with state_locks([str(x.parent) for x in markers]):
        # Recovery of an otherwise live instance must not bypass the ordinary
        # Native-job gate. A stopped owned service has no Native work to stop;
        # _restore still checks both projects' durable background job records.
        host.singleton()
        if not recover or not host.service_stopped():
            current = load_config(p['config_path'])
            original_row = next(row for row in record['files'] if row['path'] == p['config_path'])
            original_bytes = read(original_row['backup'])
            require(sha(original_bytes) == original_row['before_sha256'], 'BACKUP_HASH_MISMATCH', 'Original instance backup changed')
            host.idle(current, json.loads(original_bytes))
        identity = record['marker_identity']
        owned = []
        for marker in markers:
            if marker.exists():
                require(recover and json.loads(read(marker)) == identity, 'BINDING_UNAVAILABLE', 'Existing marker requires matching recovery')
            else:
                dep._private_write(marker, encoded(identity))
            owned.append(marker)
        try:
            _restore(p, host, record, backup, automatic=recover)
            record['status'] = 'rolled_back'; dep._record(rp, record)
            _clear_markers(owned, identity)
        except Exception as error:
            record.update(status='rollback_failed', recovery_error_code=getattr(error, 'code', type(error).__name__))
            dep._record(rp, record)
            raise
    return {'status': 'rolled_back', 'record': str(rp)}
