"""Compose one project with a connection without inheriting another paper's scope.

A manifest contains a complete project configuration and pins its PROJECT.yaml.
Connection credentials, OAuth references and admission stay on the connection.
These functions perform bounded local reads only; they never open credentials,
execute TeX, call a service or change a permission.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from paper_studio.capabilities import describe_capabilities
from paper_studio.contracts import BY_NAME
from paper_studio.security.policy import (
    OPTIONAL_PROJECT_FIELDS, PROJECT_FIELDS, StudioError, encoded, fail, no_links,
    path_for, resolve_projects, validate_project_config,
)


SCHEMA = 'paper-studio.project-binding.v1'
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MANIFEST_FIELDS = frozenset({
    'schema', 'project', 'contract_sha256', 'required_capabilities',
    'required_build_targets',
})
NATIVE_FIELDS = frozenset({'native_project', 'native_artifact_project'})
PROJECT_METADATA_FIELDS = frozenset({
    'project_name', 'paper_title', 'display_name', 'name', 'description',
    'legacy_provider_only',
})
# drive_folder_id is project scoped even on runtimes which historically inherit
# that field on an additional-project declaration. We never use that inheritance.
BINDING_PROJECT_FIELDS = frozenset(
    (PROJECT_FIELDS - NATIVE_FIELDS) | OPTIONAL_PROJECT_FIELDS
    | PROJECT_METADATA_FIELDS | {'drive_folder_id'}
)
REQUIRED_PROJECT_FIELDS = frozenset(PROJECT_FIELDS - NATIVE_FIELDS)
_ID = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}\Z')
_SHA = re.compile(r'[0-9a-f]{64}\Z')


def _bounded_file(path: Path, limit: int = MAX_MANIFEST_BYTES) -> bytes:
    path = no_links(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
        fail('BINDING_FILE_INVALID', 'Binding input must be a bounded regular single-link file')
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            fail('BINDING_INPUT_CHANGED', 'Binding input changed during open')
        raw = stream.read(limit + 1)
    no_links(path)
    after = path.stat()
    if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns
    ) or len(raw) != info.st_size:
        fail('BINDING_INPUT_CHANGED', 'Binding input changed during capture')
    return raw


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            fail('BINDING_MANIFEST_INVALID', 'Duplicate JSON field in project manifest')
        result[key] = value
    return result


def _contract(project):
    root = no_links(Path(project['project_root']))
    if not root.is_dir():
        fail('BINDING_CONTRACT_INVALID', 'Target project directory is missing')
    raw = _bounded_file(root / 'PROJECT.yaml')
    try:
        # PROJECT.yaml is a project contract and can be either JSON or YAML.
        import yaml
        value = yaml.safe_load(raw)
    except Exception as error:
        if isinstance(error, StudioError):
            raise
        fail('BINDING_CONTRACT_INVALID', 'Cannot parse the target project contract')
    if not isinstance(value, dict) or value.get('project_id') != project['project_id']:
        fail('BINDING_CONTRACT_INVALID', 'Target contract project identity does not match')
    declared_roots = [value[key] for key in ('paper_writing_root', 'project_root') if key in value]
    if not declared_roots or any(item != str(root) for item in declared_roots):
        fail('BINDING_CONTRACT_INVALID', 'Target contract must declare the exact writing root')
    return hashlib.sha256(raw).hexdigest()


def validate_manifest(manifest):
    """Return a checked independent JSON value; unknown/shared fields are denied."""
    if not isinstance(manifest, dict) or set(manifest) != MANIFEST_FIELDS or manifest.get('schema') != SCHEMA:
        fail('BINDING_MANIFEST_INVALID', 'Exact project binding manifest fields are required')
    project = manifest['project']
    if (not isinstance(project, dict) or not REQUIRED_PROJECT_FIELDS <= set(project)
            or set(project) - BINDING_PROJECT_FIELDS):
        fail('BINDING_MANIFEST_INVALID', 'Manifest must contain complete project-only fields')
    if not isinstance(project['project_id'], str) or not _ID.fullmatch(project['project_id']):
        fail('BINDING_MANIFEST_INVALID', 'Invalid target project identity')
    if not isinstance(manifest['contract_sha256'], str) or not _SHA.fullmatch(manifest['contract_sha256']):
        fail('BINDING_MANIFEST_INVALID', 'Target project contract SHA256 is required')
    for field in ('project_root', 'artifact_root', 'state_root'):
        if not isinstance(project[field], str):
            fail('BINDING_MANIFEST_INVALID', 'Project roots must be canonical absolute paths')
        no_links(Path(project[field]))
    for field in ('required_capabilities', 'required_build_targets'):
        values = manifest[field]
        if (not isinstance(values, list) or len(values) > (len(BY_NAME) if field == 'required_capabilities' else 8)
                or any(not isinstance(item, str) for item in values) or len(set(values)) != len(values)):
            fail('BINDING_MANIFEST_INVALID', 'Unique bounded capability and build target lists are required')
    if any(name not in BY_NAME for name in manifest['required_capabilities']):
        fail('BINDING_MANIFEST_INVALID', 'Unknown required capability')
    if any(not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', name) for name in manifest['required_build_targets']):
        fail('BINDING_MANIFEST_INVALID', 'Invalid required build target name')
    # Policy validation below checks nested fields. Serialization also rejects
    # non-JSON objects and nonfinite numbers before anything can be written.
    try:
        raw = json.dumps(manifest, ensure_ascii=False, allow_nan=False).encode()
    except (TypeError, ValueError):
        fail('BINDING_MANIFEST_INVALID', 'Manifest must contain finite JSON values')
    if len(raw) > MAX_MANIFEST_BYTES:
        fail('BINDING_MANIFEST_INVALID', 'Project manifest exceeds its byte limit')
    validate_project_config(deepcopy(project))
    if project.get('drive_folder_id') is not None:
        from paper_studio.drive.publisher import _drive_id
        _drive_id(project['drive_folder_id'])
    for field in PROJECT_METADATA_FIELDS - {'legacy_provider_only'}:
        if field in project and (not isinstance(project[field], str) or len(project[field]) > 4096):
            fail('BINDING_MANIFEST_INVALID', 'Project display metadata must be bounded text')
    if 'legacy_provider_only' in project and type(project['legacy_provider_only']) is not bool:
        fail('BINDING_MANIFEST_INVALID', 'legacy_provider_only must be an explicit boolean')
    return deepcopy(manifest)


def dump_manifest(manifest):
    return encoded(validate_manifest(manifest))


def load_manifest(path):
    """Read one strict JSON manifest; credentials are never opened."""
    raw = _bounded_file(Path(path))
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object,
                           parse_constant=lambda _: fail('BINDING_MANIFEST_INVALID', 'Nonfinite JSON number'))
    except (ValueError, UnicodeError):
        fail('BINDING_MANIFEST_INVALID', 'Cannot parse project binding manifest JSON')
    return validate_manifest(value)


def _single(cfg):
    if cfg.get('projects') or cfg.get('_projects'):
        fail('BINDING_MULTI_PROJECT', 'Atomic single-project binding cannot drop additional projects')


def _resolved_single(cfg):
    _single(cfg)
    clean = {key: deepcopy(value) for key, value in cfg.items() if not key.startswith('_')}
    resolved = resolve_projects(clean)
    return {key: value for key, value in resolved.items() if not key.startswith('_')}


def capture(cfg):
    """Capture the complete current project, excluding connection authentication."""
    current = _resolved_single(cfg)
    project = {key: deepcopy(current[key]) for key in sorted(BINDING_PROJECT_FIELDS) if key in current}
    manifest = {
        'schema': SCHEMA,
        'project': project,
        'contract_sha256': _contract(project),
        'required_capabilities': sorted(describe_capabilities(current)['available_tools']),
        'required_build_targets': sorted(current.get('execution_policy', {}).get('tex_targets', {})),
    }
    return validate_manifest(manifest)


def _separate_projects(current, target):
    changed = (current['project_id'], current['project_root']) != (target['project_id'], target['project_root'])
    if not changed:
        return
    # A receipt directory is per project: retaining it can confuse same-named
    # immutable Drive publications. No source/target tree may alias another.
    for old_key in ('project_root', 'artifact_root', 'state_root'):
        old = Path(current[old_key])
        for new_key in ('project_root', 'artifact_root', 'state_root'):
            new = Path(target[new_key])
            if old.is_relative_to(new) or new.is_relative_to(old):
                fail('BINDING_PROJECT_OVERLAP', 'Source and target project trees must be separate',
                     source_field=old_key, target_field=new_key)


def compose(current_cfg, manifest):
    """Replace all project fields and retain the connection's current ceiling."""
    manifest = validate_manifest(manifest)
    current = _resolved_single(current_cfg)
    target = manifest['project']
    if _contract(target) != manifest['contract_sha256']:
        fail('BINDING_CONTRACT_CHANGED', 'Target project contract changed after manifest capture')
    _separate_projects(current, target)
    candidate = {key: deepcopy(value) for key, value in current.items()
                 if key not in BINDING_PROJECT_FIELDS | NATIVE_FIELDS | {'projects'}}
    candidate.update(deepcopy(target))
    candidate['native_project'] = 'agent:' + current['client_id'] + ':' + target['project_id']
    candidate['native_artifact_project'] = candidate['native_project'] + '-artifacts'
    candidate['projects'] = []
    return _resolved_single(candidate)


def _local_path(path, *, directory=False, regular=False, required=False):
    path = no_links(Path(path))
    exists = path.exists()
    if required and not exists:
        fail('BINDING_LOCAL_INPUT_MISSING', 'Configured local input is missing', path=str(path))
    if exists and directory and not path.is_dir():
        fail('BINDING_LOCAL_INPUT_INVALID', 'Configured directory is not a directory', path=str(path))
    if exists and regular and not path.is_file():
        fail('BINDING_LOCAL_INPUT_INVALID', 'Configured input is not a regular file', path=str(path))
    return {'path': str(path), 'exists': exists}


def preflight(candidate, manifest):
    """Check configuration, target dependencies and pinned helper; no services."""
    manifest = validate_manifest(manifest)
    cfg = _resolved_single(candidate)
    actual = {key: deepcopy(cfg[key]) for key in sorted(BINDING_PROJECT_FIELDS) if key in cfg}
    if actual != manifest['project']:
        fail('BINDING_TARGET_MISMATCH', 'Candidate does not contain the exact target project configuration')
    if _contract(actual) != manifest['contract_sha256']:
        fail('BINDING_CONTRACT_CHANGED', 'Target project contract changed after manifest capture')
    capabilities = describe_capabilities(cfg)
    by_name = {row['name']: row for row in capabilities['tools']}
    missing = [{key: by_name[name][key] for key in ('name', 'status', 'reason')}
               for name in manifest['required_capabilities'] if by_name[name]['status'] != 'available']
    if missing:
        fail('BINDING_CAPABILITY_MISSING', 'Required target capabilities are not configured or admitted', missing=missing)
    targets = cfg.get('execution_policy', {}).get('tex_targets', {})
    absent = [name for name in manifest['required_build_targets'] if name not in targets]
    if absent:
        fail('BINDING_BUILD_TARGET_MISSING', 'Required trusted build targets are missing', targets=absent)
    build_results = []
    for name in manifest['required_build_targets']:
        from paper_studio.execution import build_plan
        plan = build_plan(cfg, targets[name]['tex_path'])
        build_results.append({key: plan[key] for key in (
            'target', 'tex_path', 'engine', 'bibliography_engine', 'input_sha256', 'expected_revision'
        )})
    checks = [_local_path(cfg['project_root'], directory=True, required=True)]
    for field in ('artifact_root', 'state_root'):
        checks.append(_local_path(cfg[field], directory=True))
    for relative in cfg['writable_roots']:
        # A nonexistent exact file cannot become a systemd writable mount.
        # Fail before stopping the old instance instead of guessing its type
        # or widening the permission to the containing directory.
        checks.append(_local_path(Path(cfg['project_root']) / relative, required=True))
    for relative in cfg['readonly_roots']:
        checks.append(_local_path(Path(cfg['project_root']) / relative))
    if cfg.get('zotero_readonly'):
        scope = cfg['zotero_readonly']
        helper = no_links(Path(scope['helper_path']))
        raw = _bounded_file(helper, 256 * 1024)
        if hashlib.sha256(raw).hexdigest() != scope['helper_sha256']:
            fail('BINDING_ZOTERO_HELPER_CHANGED', 'Installed Zotero helper differs from its pinned hash')
        checks.append({'path': str(helper), 'exists': True, 'sha256_verified': True})
    if cfg.get('zotero_backend'):
        checks.append(_local_path(cfg['zotero_backend']['sdk_path'], directory=True, required=True))
    if cfg.get('research_state_path'):
        checks.append(_local_path(path_for(cfg, cfg['research_state_path']), regular=True, required=True))
    return {
        'schema': 'paper-studio.binding-preflight.v1',
        'project_id': cfg['project_id'], 'project_root': cfg['project_root'],
        'status': 'local_preflight_passed', 'contract_sha256': manifest['contract_sha256'],
        'capabilities': [{key: by_name[name][key] for key in ('name', 'status', 'reason')}
                         for name in manifest['required_capabilities']],
        'unavailable_capabilities': [{key: row[key] for key in ('name', 'status', 'reason')}
                                     for row in capabilities['tools'] if row['status'] != 'available'],
        'build_targets': build_results, 'path_checks': checks,
        'pending_directories': [row['path'] for row in checks[:3] if not row['exists']],
        'credentials_read': False, 'services_probed': False, 'build_executed': False,
        'verification': 'local_configuration_and_inputs_only',
    }
