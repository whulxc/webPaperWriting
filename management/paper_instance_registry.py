"""Read-only instance facts and rebind preflight over the existing Tunnel registry.

Configuration/source observations do not establish ChatGPT acceptance. No helper
in this module starts a process, switches a project or writes a credential.
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'paper-studio'))
from paper_studio.security.policy import builds_root


ACTIVE_BUILD_STATES = {'queued', 'running', 'recovery_required'}
ACTIVE_SYNC_STATES = {'prepared', 'syncing', 'downloading', 'creating', 'patching'}


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def load_metadata(path, limit=2 * 1024 * 1024):
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path:
        raise ValueError('Canonical metadata path required')
    info = path.stat()
    if not path.is_file() or info.st_nlink != 1 or info.st_size > limit:
        raise ValueError('Bounded ordinary metadata file required')
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError('Object metadata required')
    return value


def attached_config(entry):
    """Recognize a literal shared adapter config without importing the wrapper."""
    tree = ast.parse(Path(entry).read_text(encoding='utf-8'))
    paths = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == 'PAPER_STUDIO_CONFIG'
                        for t in node.targets)):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                paths.add(node.value.value)
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == 'attach' and len(node.args) == 2
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)):
            paths.add(node.args[1].value)
    if len(paths) > 1:
        raise ValueError('Wrapper has conflicting Paper Studio configurations')
    return Path(next(iter(paths))) if paths else None


def runner_state(config):
    """Locate the matching existing Runner; state_root may hold only job state."""
    candidates = []
    if isinstance(config.get('runner_config'), str):
        candidates.append(Path(config['runner_config']).parent)
    reader = config.get('credentials', {}).get('reader')
    if isinstance(reader, str):
        candidates.append(Path(reader).parent)
    if isinstance(config.get('state_root'), str):
        candidates.append(Path(config['state_root']))
    import tomllib
    for candidate in candidates:
        path = candidate / 'runner.toml'
        if not path.is_file() or path.resolve() != path:
            continue
        raw = path.read_bytes()
        if len(raw) > 2 * 1024 * 1024:
            continue
        value = tomllib.loads(raw.decode())
        if value.get('client_id') == config.get('client_id'):
            return candidate
    return Path(config['state_root'])


def _release_candidates(entry, config):
    candidates = []
    for field in ('release_path', 'release_root'):
        if isinstance(config.get(field), str):
            candidates.append(Path(config[field]))
    release = config.get('release')
    if isinstance(release, dict) and isinstance(release.get('path'), str):
        candidates.append(Path(release['path']))
    if isinstance(release, dict) and isinstance(release.get('manifest_path'), str):
        candidates.append(Path(release['manifest_path']).parent)
    paths = [Path(entry)]
    tree = ast.parse(Path(entry).read_text(encoding='utf-8'))
    # A private wrapper can import the shared release using a literal sys.path
    # entry. Inspect only absolute strings, never execute their expressions.
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value.startswith('/') and len(node.value) <= 2048:
                paths.append(Path(node.value))
    for path in paths:
        candidates.extend(list(path.parents)[:6])
        if path.is_dir():
            candidates.append(path)
    seen = set()
    for path in candidates:
        if path not in seen:
            seen.add(path)
            yield path


def release_identity(entry, config):
    from management.paper_release import verify_release
    invalid = False
    io_errors = []
    for directory in _release_candidates(entry, config):
        manifest_path = directory / 'manifest.json'
        for attempt in range(3):
            try:
                if not manifest_path.is_file():
                    break
                value = load_metadata(manifest_path)
                if value.get('schema') != 'paper-studio.source-release.v1':
                    break
                manifest = verify_release(directory)
                claim = config.get('release', {})
                if isinstance(claim, dict):
                    expected = {'manifest_sha256': digest(manifest_path.read_bytes()),
                                'source_sha256': manifest['source_sha256'],
                                'release_id': manifest['release_id']}
                    if any(claim.get(k, v) != v for k, v in expected.items()):
                        raise ValueError('Configured release identity differs from verified installed source')
                git = manifest.get('git', {})
                return {'status': 'verified_source_release', 'path': str(directory),
                        'version': manifest['version'], 'release_id': manifest['release_id'],
                        'release_sha256': manifest['release_sha256'],
                        'source_sha256': manifest['source_sha256'],
                        'manifest_sha256': digest(manifest_path.read_bytes()),
                        'git_commit': git.get('commit'), 'git_state': git.get('state'),
                        'source_dirty': git.get('source_dirty'), 'verification_io_errors': io_errors,
                        'basis': 'Every installed release member was hash-verified; not a version-string comparison'}
            except OSError as error:
                # DrvFS can report temporary ENOMEM during directory scans.
                # Bound retries and retain uncertainty instead of calling it
                # a source mismatch or altering the immutable release.
                io_errors.append({'code': 'SOURCE_RELEASE_IO_ERROR', 'errno': error.errno,
                                  'attempt': attempt + 1})
            except (ValueError, RuntimeError, KeyError):
                invalid = True
                break
    return {'status': 'invalid_release' if invalid else 'unable_to_verify' if io_errors else 'not_release_bound',
            'git_commit': None,
            'verification_io_errors': io_errors,
            'basis': 'No verified immutable source release was observed'}


def activity_snapshot(config):
    """Include provider jobs and long syncs, absent from Native Job counters."""
    projects = [config, *config.get('projects', [])]
    active, unknown = [], 0
    for project in projects:
        if not isinstance(project, dict):
            unknown += 1
            continue
        locations = []
        from paper_studio.provider import job_index, jobs
        if job_index.enabled(project):
            try:
                for status in jobs.active_jobs(project):
                    active.append({'project_id':project.get('project_id'),'kind':'build',
                                   'status':status['status'],'operation_id':status['build_id']})
                active.extend(job_index.sync_activity(project))
                from paper_studio.drive.sync_tasks import active_tasks
                active.extend({'project_id':project.get('project_id'),'kind':'source_sync',
                               'status':row['status'],'operation_id':row['request_id']}
                              for row in active_tasks(project))
            except Exception:
                unknown += 1
        elif isinstance(project.get('artifact_root'), str):
            locations.append((builds_root(project), 'rb_*/status.json', ACTIVE_BUILD_STATES, 'build'))
        if isinstance(project.get('state_root'), str) and not job_index.enabled(project):
            state = Path(project['state_root'])
            locations.extend([(state / 'drive-source-sync/requests', '*.json', ACTIVE_SYNC_STATES, 'source_sync'),
                              (state / 'drive-source-sync/reviews', '*.json', ACTIVE_SYNC_STATES, 'review_import')])
        for directory, pattern, states, kind in locations:
            if not directory.exists():
                continue
            if directory.resolve() != directory:
                unknown += 1
                continue
            paths = sorted(directory.glob(pattern))
            if len(paths) > 4096:
                unknown += 1
                continue
            for path in paths:
                try:
                    value = load_metadata(path)
                    if value.get('status') in states:
                        active.append({'project_id': project.get('project_id'), 'kind': kind,
                                       'status': value['status'],
                                       'operation_id': value.get('build_id', value.get('request_id'))})
                except (OSError, ValueError, UnicodeError):
                    unknown += 1
    return {'active_count': len(active), 'active_operations': active,
            'unknown_records': unknown, 'basis': 'Read-only local durable provider/sync status'}


def project_state_observation(project):
    root = project.get('project_root')
    relative = project.get('research_state_path', 'PROJECT_STATE.json')
    if not isinstance(root, str) or not isinstance(relative, str):
        return {'status': 'not_configured'}
    directory = Path(root)
    path = directory / relative
    if Path(relative).is_absolute() or '..' in Path(relative).parts or not path.is_relative_to(directory):
        return {'status': 'invalid_state_reference'}
    if not path.exists():
        return {'status': 'not_present'}
    try:
        value = load_metadata(path)
        acceptance = value.get('last_acceptance', {})
        if not isinstance(acceptance, dict):
            acceptance = {}
        safe = {k: v for k, v in acceptance.items()
                if k in {'date', 'scope', 'status', 'report', 'build', 'visual_review', 'chatgpt_independent_review', 'author_accepted'}
                and (type(v) in (str, bool, int) or v is None)}
        task = value.get('current_task')
        return {'status': 'observed', 'path': str(path),
                'sha256': digest(path.read_bytes()),
                'current_task': task[:1000] if isinstance(task, str) else None,
                'last_acceptance': safe,
                'basis': 'Declared local project state; not inferred from health or upload success'}
    except (OSError, ValueError, UnicodeError):
        return {'status': 'unreadable_state'}


def enrich_binding(binding, config, entry):
    """Append safe configuration facts; never return credential values/paths."""
    projects = [config, *config.get('projects', [])]
    summaries = []
    for item in projects:
        scopes = item.get('zotero_write', {})
        read = item.get('zotero_readonly', {})
        sync = item.get('drive_source_sync', {})
        execution = item.get('execution_policy', {})
        references = scopes.get('references', {})
        summaries.append({'project_id': item.get('project_id'),
                          'native_project_id': item.get('native_project'),
                          'native_artifact_project_id': item.get('native_artifact_project'),
                          'project_root': item.get('project_root'),
                          'writable_roots': item.get('writable_roots', []),
                          'readonly_roots': item.get('readonly_roots', []),
                          'build_targets': sorted(execution.get('tex_targets', {})),
                          'source_sync_enabled': sync.get('enabled', False),
                          'drive_project_folder_id': sync.get('project_folder_id'),
                          'version_publish_folder_id': item.get('drive_folder_id', config.get('drive_folder_id')),
                          'current_project_state': project_state_observation(item),
                          'zotero': {'read_configured': bool(read), 'write_configured': bool(scopes),
                                     'citation_keys': sorted(read.get('items', {})),
                                     'read_collection_keys': read.get('collection_keys', []),
                                     'write_citation_keys': sorted(references),
                                     'write_dois': sorted({r.get('doi') for r in references.values() if isinstance(r.get('doi'), str)}),
                                     'write_collection_keys': sorted({r.get('collection_key') for r in references.values() if isinstance(r.get('collection_key'), str)}),
                                     'write_collection_names': sorted({r.get('collection_name') for r in references.values() if isinstance(r.get('collection_name'), str)}),
                                     'bib_sync_enabled': scopes.get('bib_sync', {}).get('enabled', False),
                                     'bib_sync_paths': scopes.get('bib_sync', {}).get('paths', []),
                                     'cross_project_inheritance': False}})
    references = {role: 'secure-ref:' + digest(str(path).encode())[:16]
                  for role, path in config.get('credentials', {}).items() if isinstance(path, str)}
    for name in ('drive_oauth_file',):
        if isinstance(config.get(name), str):
            references[name] = 'secure-ref:' + digest(config[name].encode())[:16]
    binding.update(instance_id=config.get('instance_id'), extension_version=config.get('extension_version'),
                   configured_provider_id=config.get('provider_id', 'research-build'),
                   source_release=release_identity(entry, config), project_permissions=summaries,
                   credential_references=references, local_activity=activity_snapshot(config))
    return binding


def instance_registry(inventory):
    """Stable public view; alias labels never rename a Runner or remote Tunnel."""
    records = []
    for record in inventory['tunnels']:
        aliases = record.get('aliases', [])
        active_aliases = [alias for alias in aliases if alias.get('process_running') is True]
        current_aliases = active_aliases or aliases
        inactive_aliases = [alias for alias in aliases if alias not in current_aliases]
        bindings = [item.get('binding', {}) for item in current_aliases]
        busy = any(binding.get('runtime', {}).get('active_jobs', 0) not in (None, 0)
                   or binding.get('runtime', {}).get('pending_requests', 0) not in (None, 0)
                   or binding.get('local_activity', {}).get('active_count', 0) > 0 for binding in bindings)
        errors = (record.get('state') == 'error'
                  or any(binding.get('status') == 'UNKNOWN'
                         or binding.get('source_release', {}).get('status') in {'invalid_release', 'unable_to_verify'}
                         or binding.get('local_activity', {}).get('unknown_records', 0) > 0
                         for binding in bindings))
        if errors:
            state = 'config_error'
        elif busy:
            state = 'active_jobs'
        elif record.get('binding_state') == 'unbound' and record.get('unbound_verified') is True:
            state = 'unbound'
        elif record.get('binding_state') == 'bound':
            online = record.get('state') == 'online' and all(
                b.get('runtime', {}).get('runner_online') is True for b in bindings)
            state = 'bound_online' if online else 'bound_offline'
        else:
            state = 'unknown'
        permission = record.get('shared_key_permission', {})
        use_verified = (permission.get('use_permission') == 'VERIFIED_READONLY_INTROSPECTION'
                        or permission.get('use_evidence', {}).get('status') == 'VERIFIED_EXISTING_CONNECTION')
        idle_verified = bool(bindings) and all(
            binding.get('runtime', {}).get('active_jobs') == 0
            and binding.get('runtime', {}).get('pending_requests') == 0
            and binding.get('local_activity', {}).get('active_count', 0) == 0
            and binding.get('local_activity', {}).get('unknown_records', 0) == 0 for binding in bindings)
        preflight = (not errors and not busy and idle_verified and use_verified
                     and len(aliases) <= 1 and all(b.get('gateway_kind') == 'paper_studio' for b in bindings))
        name = record.get('platform_name')
        records.append({'tunnel_id': record['tunnel_id'], 'platform_name': name,
                        'management_display_alias': 'Paper Editor · ' + name if name else None,
                        'display_name_source': record.get('display_mapping_status'),
                        'status': state, 'binding_state': record.get('binding_state'),
                        'occupied': record.get('binding_state') == 'bound',
                        'offline_is_unbound': False, 'active_tasks': busy,
                        'profiles': [{key: alias.get(key) for key in ['alias', 'profile', 'profile_sha256', 'credential_ref', 'process_running', 'runtime_state', 'healthz', 'readyz']}
                                     for alias in aliases],
                        'instances': [{key: binding.get(key) for key in [
                            'instance_id', 'runner', 'client_id', 'gateway_kind', 'gateway_mode', 'gateway_entrypoint',
                            'gateway_config', 'gateway_sha256', 'config_sha256', 'extension_version',
                            'configured_provider_id', 'source_release', 'project_bindings', 'legacy_project_bindings',
                            'legacy_gateway_config', 'legacy_gateway_config_sha256',
                            'project_permissions', 'native_registration', 'credential_references',
                            'local_activity', 'runtime']} for binding in bindings],
                        'historical_or_inactive_instances': [{
                            'alias': alias.get('alias'), 'process_running': alias.get('process_running'),
                            'runner': alias.get('binding', {}).get('runner'),
                            'gateway_kind': alias.get('binding', {}).get('gateway_kind'),
                            'project_bindings': alias.get('binding', {}).get('project_bindings', []),
                            'selected_as_current_binding': False} for alias in inactive_aliases],
                        'credential_use_verification': {'read_permission': permission.get('read_permission'),
                                                        'use_permission': permission.get('use_permission'),
                                                        'existing_connection_evidence': permission.get('use_evidence'),
                                                        'organization_ids': permission.get('metadata', {}).get('organization_ids')},
                        'rebind': {'preflight_ready': preflight, 'safe_to_rebind': False,
                                   'requires_explicit_user_selection': True,
                                   'live_apply_enabled': False,
                                   'basis': 'Generate an exact binding_plan and recheck identities/jobs before an authorized lifecycle change'},
                        'last_health_verification': record.get('last_successful_verification'),
                        'last_acceptance': [p.get('current_project_state') for b in bindings
                                            for p in b.get('project_permissions', [])],
                        'chatgpt_independent_acceptance': 'not_established_by_registry'})
    return {'schema': 'paper-editor.instance-registry.v1', 'observed_at': inventory['observed_at'],
            'instances': records, 'source': 'Actual local profile/config/Native runtime and verified release files',
            'mutation_actions': 0, 'stdio_clients_started': 0,
            'unknown_profiles': inventory.get('unknown_profiles', [])}
