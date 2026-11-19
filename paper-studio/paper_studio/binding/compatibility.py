"""Validate a candidate with the selected connection's installed source release.

This is a configuration-only subprocess, not an execution or service health
probe. It never invokes TeX, a Provider tool, OAuth or a Zotero transport.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

from management.paper_release import verify_release
from paper_studio.contracts import BY_NAME
from paper_studio.security.policy import fail, no_links, sha


MAX_INPUT = 2 * 1024 * 1024
MAX_OUTPUT = 1024 * 1024
STATUSES = frozenset({'available', 'not_configured', 'permission_denied', 'unsupported'})

# The program is literal trusted code. Candidate bytes only enter json.loads;
# they cannot supply commands, Python fragments, a working directory or imports.
PROGRAM = r'''
import json, sys
try:
    sys.path.insert(0, sys.argv[1])
    from paper_studio.security.policy import resolve_projects
    from paper_studio.capabilities import describe_capabilities
    raw = sys.stdin.buffer.read(2097153)
    if len(raw) > 2097152:
        raise ValueError('input limit')
    config = resolve_projects(json.loads(raw))
    tools = describe_capabilities(config)['tools']
    result = {'ok': True, 'tools': [{'name': row['name'], 'status': row['status']} for row in tools]}
except BaseException:
    result = {'ok': False}
sys.stdout.write(json.dumps(result, separators=(',', ':')))
'''


def _installed(cfg):
    claim = cfg.get('release')
    keys = ('version', 'release_id', 'source_sha256', 'manifest_sha256')
    if (not isinstance(claim, dict) or not isinstance(claim.get('manifest_path'), str)
            or any(not isinstance(claim.get(key), str) or not claim[key] for key in keys)):
        fail('REBIND_RELEASE_REQUIRED', 'A complete verified installed release identity is required')
    path = no_links(Path(claim['manifest_path']))
    if path.name != 'manifest.json':
        fail('REBIND_RELEASE_REQUIRED', 'Installed release must identify its exact source manifest')
    try:
        manifest = verify_release(path.parent)
        manifest_sha = sha(path.read_bytes())
    except Exception:
        fail('REBIND_RELEASE_INVALID', 'Installed source release integrity could not be verified')
    expected = {key: manifest[key] for key in ('version', 'release_id', 'source_sha256')}
    expected['manifest_sha256'] = manifest_sha
    if any(claim[key] != expected[key] for key in keys):
        fail('REBIND_RELEASE_MISMATCH', 'Configured and installed source release identities differ')
    guards = {str(path): manifest_sha}
    for row in manifest['files']:
        guards[str(no_links(path.parent / row['path']))] = row['sha256']
    return path, expected, guards


def check(cfg, *, fixture_only=False):
    if type(fixture_only) is not bool:
        fail('INVALID_ARGUMENT', 'fixture_only must be an explicit boolean')
    if fixture_only:
        return {'status': 'skipped_fixture', 'configuration_only': True,
                'guard_files': {}, 'capabilities': {}, 'services_probed': False,
                'credentials_read': False, 'build_executed': False}
    path, identity, guards = _installed(cfg)
    candidate = {key: value for key, value in cfg.items() if not key.startswith('_')}
    try:
        payload = json.dumps(candidate, ensure_ascii=False, allow_nan=False).encode('utf-8')
    except (TypeError, ValueError):
        fail('REBIND_RUNTIME_INCOMPATIBLE', 'Candidate configuration cannot be serialized for the installed runtime')
    if len(payload) > MAX_INPUT:
        fail('REBIND_RUNTIME_INCOMPATIBLE', 'Candidate configuration exceeds the compatibility input bound')
    try:
        process = subprocess.run([sys.executable, '-I', '-B', '-c', PROGRAM,
                                  str(path.parent / 'paper-studio')],
                                 input=payload, capture_output=True, timeout=20, check=False,
                                 cwd=str(path.parent),
                                 env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8',
                                      'PYTHONDONTWRITEBYTECODE': '1'})
    except (OSError, subprocess.TimeoutExpired):
        fail('REBIND_RUNTIME_INCOMPATIBLE', 'Installed runtime compatibility probe could not finish')
    if (process.returncode != 0 or len(process.stdout) > MAX_OUTPUT
            or len(process.stderr) > MAX_OUTPUT):
        fail('REBIND_RUNTIME_INCOMPATIBLE', 'Installed runtime rejected the candidate configuration')
    try:
        result = json.loads(process.stdout)
        if not isinstance(result, dict) or result.get('ok') is not True:
            raise ValueError('probe failed')
        rows = result['tools']
        if not isinstance(rows, list) or len(rows) > 256:
            raise ValueError('invalid tool states')
        capabilities = {}
        for row in rows:
            if (not isinstance(row, dict) or set(row) != {'name', 'status'}
                    or row['name'] not in BY_NAME or row['status'] not in STATUSES
                    or row['name'] in capabilities):
                raise ValueError('invalid tool state')
            capabilities[row['name']] = row['status']
    except (ValueError, KeyError, TypeError, UnicodeError):
        fail('REBIND_RUNTIME_INCOMPATIBLE', 'Installed runtime could not validate the candidate configuration')
    # Imports must not race an edited release. Revalidate every member after
    # the child exits and bind all bytes as transaction guards, not just version.
    after_path, after_identity, after_guards = _installed(cfg)
    if after_path != path or after_identity != identity or after_guards != guards:
        fail('REBIND_RELEASE_CHANGED', 'Installed runtime changed during compatibility validation')
    return {'status': 'verified', 'release': identity, 'capabilities': capabilities,
            'guard_files': guards, 'configuration_only': True,
            'services_probed': False, 'credentials_read': False, 'build_executed': False}
