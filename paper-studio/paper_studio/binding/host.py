"""Read-only live checks and selected-instance lifecycle adapter for rebinds.

The transaction owns writes, locking and rollback. This adapter never uploads,
creates a Zotero object, grants access, or starts a second stdio client.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import time
import tomllib

from management import paper_deploy as dep
from paper_studio.security.policy import StudioError, fail, no_links, path_for, sha


def _need(value, code, message):
    if not value:
        fail(code, message)


class BindingHost(dep.SystemHost):
    def __init__(self, profile):
        self.p = profile

    def runtime_status(self, config_path):
        # Public requests remain blocked by transaction markers. Only this
        # bounded management discovery call bypasses the public routing guard.
        from paper_studio.gateway.main import Gateway
        gateway = Gateway(config_path)
        return gateway._call_locked(gateway.cfg(), 'paper_runtime_status', {})

    def singleton(self):
        from management.paper_tunnels import runtime_list, runtime_status
        selected = self.p['tunnel']
        inventory = runtime_list()
        _need(isinstance(inventory, dict) and '_error' not in inventory
              and isinstance(inventory.get('aliases'), list),
              'REBIND_TUNNEL_UNKNOWN', 'Cannot establish the Tunnel alias inventory')
        aliases = {selected['alias']}
        for row in inventory['aliases']:
            _need(isinstance(row, dict), 'REBIND_TUNNEL_UNKNOWN', 'Invalid Tunnel alias inventory entry')
            tunnel_id = row.get('tunnel_id')
            _need(tunnel_id is not None or row.get('process_running') is False,
                  'REBIND_TUNNEL_UNKNOWN', 'An unclassified alias may still be running')
            if tunnel_id == selected['tunnel_id']:
                _need(isinstance(row.get('alias'), str) and bool(row['alias']),
                      'REBIND_TUNNEL_UNKNOWN', 'Same-Tunnel alias identity is missing')
                aliases.add(row['alias'])
        active = []
        for alias in sorted(aliases):
            observed = runtime_status(alias)
            _need(isinstance(observed, dict) and '_error' not in observed
                  and observed.get('alias') == alias
                  and observed.get('tunnel_id') == selected['tunnel_id']
                  and type(observed.get('process_running')) is bool,
                  'REBIND_TUNNEL_UNKNOWN', 'Same-Tunnel process state or identity is unknown')
            if observed['process_running']:
                active.append(alias)
        _need(all(alias == selected['alias'] for alias in active),
              'REBIND_SECOND_CLIENT', 'Another active alias uses the selected Tunnel')
        return {'status': 'verified', 'selected_alias': selected['alias'],
                'same_tunnel_active_aliases': active}

    def idle(self, source_cfg, target_cfg):
        from management.paper_tunnels import native_probe
        from management.paper_instance_registry import activity_snapshot
        from paper_studio.drive.sync_tasks import active_tasks
        from paper_studio.security.policy import builds_root
        observed = native_probe({'_reader_token_ref': 'file:' + source_cfg['credentials']['reader'],
                                 'server_url': source_cfg['server_url'],
                                 'runner': source_cfg['client_id']})
        _need(observed.get('runner_online') is True
              and observed.get('active_jobs') == 0 and observed.get('pending_requests') == 0,
              'REBIND_BUSY', 'Native Runner has active or unknown work')
        projects = {}
        for cfg in (source_cfg, target_cfg):
            key = (cfg['project_id'], cfg['artifact_root'], cfg['state_root'])
            if key in projects:
                continue
            snapshot = activity_snapshot(cfg)
            _need(snapshot.get('active_count') == 0 and snapshot.get('unknown_records') == 0,
                  'REBIND_BUSY', 'Project build or synchronization work is active or unknown')
            # The durable engine receipts above predate the asynchronous task
            # index. Check that index too, including lost-worker reconciliation.
            _need(not active_tasks(cfg), 'REBIND_BUSY',
                  'A background source-sync task is active or requires recovery')
            from paper_studio.provider import job_index, jobs
            if job_index.enabled(cfg):
                _need(not jobs.active_jobs(cfg), 'REBIND_BUSY', 'An indexed build is active or requires recovery')
            else:
                paths = sorted(builds_root(cfg).glob('rb_*/status.json'))
                _need(len(paths) <= 4096, 'REBIND_BUSY', 'Build state exceeds its bounded inventory')
                for path in paths:
                    status = json.loads(dep.read_file(no_links(path)))
                    _need(isinstance(status, dict) and status.get('project_id') == cfg['project_id']
                          and status.get('status') in {'completed', 'failed'},
                          'REBIND_BUSY', 'A build record is active or has an unknown project or state')
            projects[key] = {'project_id': cfg['project_id'], 'active_count': 0, 'unknown_records': 0}
        self.legacy_idle()
        return {'native_idle': True, 'projects': list(projects.values()), 'legacy_idle': True}

    def legacy_idle(self):
        for raw in self.p.get('legacy_job_roots', []):
            directory = no_links(Path(raw))
            _need(directory.is_dir(), 'REBIND_LEGACY_UNKNOWN', 'Legacy job root is unavailable')
            list(directory.iterdir())  # An unreadable directory is not an empty job list.
            paths = sorted(directory.glob('*/status.json'))
            _need(len(paths) <= 4096, 'REBIND_LEGACY_UNKNOWN', 'Legacy job inventory exceeds its bound')
            for path in paths:
                status = json.loads(dep.read_file(no_links(path)))
                _need(isinstance(status, dict) and status.get('status') in {'completed', 'failed'}
                      and not status.get('recovery_required'),
                      'REBIND_BUSY', 'Legacy job is active or has unknown terminal status')

    def service_stopped(self):
        result = subprocess.run(['systemctl', '--user', 'show', self.p['runner_service'],
                                 '--property=ActiveState,MainPID'],
                                capture_output=True, text=True, timeout=15)
        _need(result.returncode == 0, 'REBIND_SERVICE_UNKNOWN', 'Runner service state is unavailable')
        fields = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        return fields.get('ActiveState') == 'inactive' and fields.get('MainPID') == '0'

    def verify_target(self, cfg, manifest):
        """Perform bounded read-only service checks using the target's scope.

        Returned evidence is allowlisted. In particular, OAuth dictionaries,
        permission rows, Zotero helper responses and account data never escape.
        """
        required = set(manifest.get('required_capabilities', []))
        result = {'project_id': cfg['project_id'], 'read_only': True,
                  'drive': {'status': 'not_configured'},
                  'zotero': {'status': 'not_requested'},
                  'upload_performed': False, 'zotero_write_performed': False}
        sync = cfg.get('drive_source_sync', {})
        folders = []
        if cfg.get('drive_folder_id'):
            folders.append(cfg['drive_folder_id'])
        inspect_sync = bool(sync) and (sync.get('enabled') or 'paper_source_sync' in required)
        if inspect_sync:
            folders.append(sync['root_folder_id'])
            if sync.get('project_folder_id'):
                folders.append(sync['project_folder_id'])
        try:
            if folders or 'paper_drive_verify' in required:
                from paper_studio.drive.publisher import Drive
                _need(bool(cfg.get('drive_oauth_file')), 'OAUTH_REQUIRED',
                      'Target Drive operations require the shared local OAuth reference')
                drive = Drive(cfg['drive_oauth_file'], persist_access_token=False,
                              deadline=time.monotonic() + 60)
                drive.request('GET', 'drive/v3/about?fields=kind')
                observed = {}
                for ident in dict.fromkeys(folders):
                    row = drive.private(ident)
                    _need(row.get('id') == ident and row.get('mimeType') == 'application/vnd.google-apps.folder',
                          'REBIND_DRIVE_IDENTITY', 'Configured Drive target is not the expected folder')
                    observed[ident] = {'id': ident, 'parents': row.get('parents', []), 'owner_only': True}
                if inspect_sync and sync.get('project_folder_id'):
                    _need(sync['root_folder_id'] in observed[sync['project_folder_id']]['parents'],
                          'REBIND_DRIVE_PARENT', 'Source-sync project folder has a different parent')
                result['drive'] = {'status': 'verified', 'authorization': 'verified',
                                   'folders': list(observed.values())}
            zotero_tools = {name for name in required if name.startswith('paper_zotero_')}
            if zotero_tools and cfg.get('zotero_backend'):
                from paper_studio.references.zotero_local import dispatch
                row = dispatch(cfg, 'paper_zotero_manage',
                               {'project_id': cfg['project_id'], 'action': 'status',
                                'dry_run': True, 'authorized': False})
                _need(row.get('status') == 'ready' and row.get('read_ready') is True
                      and not row.get('blocked_version'),
                      'REBIND_ZOTERO_UNAVAILABLE', 'Target Zotero SDK is unavailable or unsupported')
                if ('paper_zotero_add_reference' in required
                        and cfg.get('zotero_write', {}).get('references')):
                    _need(row.get('write_ready') is True, 'REBIND_ZOTERO_GRANT_UNAVAILABLE',
                          'The required target Zotero import grant is not ready')
                result['zotero'] = {'status': 'verified', 'backend': 'pyzotero_local',
                                    'read_ready': True, 'write_grant_configured': row.get('write_ready') is True,
                                    'write_operation_verified': False}
            elif zotero_tools and cfg.get('zotero_readonly'):
                from paper_studio.references.zotero_readonly import dispatch
                row = dispatch(cfg, {'project_id': cfg['project_id'], 'action': 'status'})
                _need(row.get('status') == 'ready' and row.get('local_api_http_status') == 200,
                      'REBIND_ZOTERO_UNAVAILABLE', 'Target scoped Zotero reader is unavailable')
                result['zotero'] = {'status': 'verified', 'backend': 'readonly_helper',
                                    'read_ready': True, 'write_operation_verified': False}
        except StudioError:
            raise
        except Exception:
            fail('REBIND_TARGET_VERIFY_FAILED', 'Target service verification failed; no changes were made')
        return result

    def readback(self, cfg, contract_sha256, required_capabilities):
        from paper_studio.gateway.main import Gateway
        gateway = Gateway(self.p['config_path'])
        actual = gateway.cfg()
        for key in ('instance_id', 'client_id', 'project_id', 'project_root', 'artifact_root',
                    'state_root', 'native_project', 'native_artifact_project',
                    'writable_roots', 'readonly_roots'):
            _need(actual.get(key) == cfg.get(key), 'REBIND_READBACK_MISMATCH',
                  'Active configuration differs from the reviewed target')
        contract = no_links(Path(cfg['project_root']) / 'PROJECT.yaml')
        raw = dep.read_file(contract)
        _need(sha(raw) == contract_sha256, 'REBIND_CONTRACT_CHANGED',
              'Target project contract changed during the transaction')
        native = gateway.client(actual, 'reader').call('read_files',
                {'project': cfg['native_project'], 'items': [{'path': 'PROJECT.yaml'}],
                 'with_line_numbers': False, 'max_result_bytes': 262144})
        _need(not native.get('isError'), 'REBIND_NATIVE_READ_FAILED', 'Native target readback was rejected')
        value = gateway.unwrap(native)
        returned = next((row.get('output', {}).get('text') for row in value.get('items', [])
                         if row.get('path') == 'PROJECT.yaml'), None)
        _need(isinstance(returned, str) and returned.splitlines() == raw.decode('utf-8').splitlines(),
              'REBIND_NATIVE_READ_FAILED', 'Native target contract content does not match the configured root')
        registry = Path(self.p['runner_config']).parent / 'project-registry'
        expected = [(cfg['native_project'], cfg['project_root'], bool(cfg['writable_roots']))]
        if cfg.get('native_artifact_project'):
            expected.append((cfg['native_artifact_project'], cfg['artifact_root'], False))
        entries = []
        paths = sorted(registry.glob('*.toml'))
        _need(len(paths) <= 4096, 'REBIND_REGISTRY_MISMATCH', 'Native registry exceeds its bounded inventory')
        for path in paths:
            entries.append(tomllib.loads(dep.read_file(no_links(path)).decode('utf-8')))
        for alias, expected_path, writable in expected:
            prefix = 'agent:' + cfg['client_id'] + ':'
            _need(alias.startswith(prefix), 'REBIND_NATIVE_IDENTITY', 'Native alias belongs to another Runner')
            ident = alias[len(prefix):]
            matched = [entry for entry in entries if entry.get('id') == ident]
            _need(len(matched) == 1, 'REBIND_REGISTRY_MISMATCH', 'Native registration identity is missing or ambiguous')
            row = matched[0]
            _need(row.get('id') == ident and row.get('path') == expected_path
                  and row.get('allow_patch') is writable,
                  'REBIND_REGISTRY_MISMATCH', 'Native registration does not match the target boundary')
        capabilities = gateway._call_locked(actual, 'paper_capabilities', {'project_id': cfg['project_id']})
        tools = {tool['name']: tool['status'] for tool in capabilities['tools']}
        _need(all(tools.get(name) == 'available' for name in required_capabilities),
              'REBIND_CAPABILITY_MISSING', 'A required target capability is unavailable after the switch')
        for relative in cfg['readonly_roots']:
            try:
                path_for(actual, relative, write=True)
            except StudioError as error:
                _need(error.code in {'WRITE_DENIED', 'VERSION_WRITE_DENIED', 'PATH_DENIED'},
                      'REBIND_SCOPE_UNKNOWN', 'Readonly boundary check returned an unexpected failure')
            else:
                fail('REBIND_SCOPE_MISMATCH', 'A configured readonly path allows ordinary editing')
        return {'project_id': cfg['project_id'], 'project_root': cfg['project_root'],
                'native_project_id': cfg['native_project'], 'native_artifact_project_id': cfg.get('native_artifact_project'),
                'native_contract_read_verified': True, 'native_registry_verified': True,
                'required_capabilities_verified': sorted(required_capabilities),
                'readonly_logical_scopes_verified': True, 'scientific_probe_writes': 0,
                'chatgpt_independent_acceptance': False}
