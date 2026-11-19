"""Explicitly recover one failed Provider on an idle, singly scoped Runner."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import re
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'paper-studio'))

from management.native_client import NativeClient, NativeError
from paper_studio.gateway.main import Gateway
from paper_studio.security.policy import StudioError, fail, load_config, locked, no_links, safe_read, sha

SCHEMA = 'paper-studio.provider-recovery.v1'
PROBE = 'paper_project_context'
IDENTITY = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}')


class NativeHost:
    """Only Native reads, one explicit Provider reload and a fresh read probe."""

    def __init__(self, cfg, manager_token_file=None):
        credentials = cfg.get('credentials', {})
        manager = no_links(Path(manager_token_file)) if manager_token_file is not None else credentials.get('manager')
        if not manager or any(not credentials.get(role) for role in ('reader', 'builder')):
            fail('RECOVERY_CREDENTIALS_REQUIRED', 'Explicit reader, builder and manager credential references are required')
        self.reader = NativeClient(cfg['server_url'], credentials['reader'], timeout=8)
        self.builder = NativeClient(cfg['server_url'], credentials['builder'], timeout=15)
        self.manager = NativeClient(cfg['server_url'], manager, timeout=130)

    def _call(self, client, tool, arguments):
        try:
            value = Gateway.unwrap(client.call(tool, arguments))
            if isinstance(value, dict) and 'structuredContent' in value:
                value = Gateway.unwrap(value)
            return value
        except StudioError as error:
            # Native errors can include opaque bindings or provider stderr in
            # details. Recovery reports only their bounded machine code.
            code = error.code if IDENTITY.fullmatch(str(error.code)) else 'unknown'
            fail('RECOVERY_NATIVE_REJECTED', 'Native rejected the recovery operation', native_code=code)

    def runtime(self, cfg):
        return self._call(self.reader, 'runtime_status', {'client_id': cfg['client_id'], 'compact': False})

    def providers(self, cfg):
        return self._call(self.manager, 'plugin_tool', {'action': 'list', 'runner': cfg['client_id']})

    def check(self, cfg, provider):
        return self._call(self.manager, 'plugin_tool', {
            'action': 'check', 'runner': cfg['client_id'], 'plugin': provider})

    def reload(self, cfg):
        return self._call(self.manager, 'plugin_tool', {'action': 'reload', 'runner': cfg['client_id']})

    def active_work(self, cfg):
        from management.paper_deploy import SystemHost
        return SystemHost().active_work(cfg['_config_path'])

    def probe(self, cfg, provider):
        descriptor = self._call(self.builder, 'plugin_tool', {
            'action': 'describe', 'runner': cfg['client_id'], 'plugin': provider, 'tool': PROBE})
        if (not isinstance(descriptor, dict) or descriptor.get('runner') != cfg['client_id']
                or descriptor.get('plugin') != provider or not isinstance(descriptor.get('tool'), dict)
                or descriptor['tool'].get('name') != PROBE
                or not isinstance(descriptor.get('binding'), str)):
            fail('RECOVERY_PROBE_INVALID', 'Native did not describe the expected read-only probe')
        return self._call(self.builder, 'plugin_tool', {
            'action': 'call', 'binding': descriptor['binding'], 'arguments': {'project_id': cfg['project_id']}})

    def sleep(self, seconds):
        time.sleep(seconds)


def _provider_status(value, cfg, provider):
    if not isinstance(value, dict) or value.get('runner') != cfg['client_id']:
        fail('RECOVERY_IDENTITY_MISMATCH', 'Provider catalog belongs to another Runner')
    rows = value.get('plugins')
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        fail('RECOVERY_SCOPE_DENIED', 'Runner-wide reload requires exactly one configured Provider')
    row = rows[0]
    if row.get('plugin') != provider:
        fail('RECOVERY_IDENTITY_MISMATCH', 'Provider catalog does not match the expected Provider')
    return row


def _idle(cfg, host):
    runtime = host.runtime(cfg)
    if not isinstance(runtime, dict):
        fail('RECOVERY_STATE_UNKNOWN', 'Native runtime state is unavailable')
    focus = runtime.get('focus', {})
    clients = runtime.get('runners', {}).get('clients', [])
    matches = [r for r in clients if isinstance(r, dict) and r.get('client_id') == cfg['client_id']]
    if len(matches) != 1 or focus.get('client_id') != cfg['client_id']:
        fail('RECOVERY_IDENTITY_MISMATCH', 'Native must identify exactly the expected Runner')
    selected = matches[0]
    numbers = [focus.get('active_jobs'), runtime.get('jobs', {}).get('active_count'),
               selected.get('pending_requests')]
    if any(type(v) is not int or v != 0 for v in numbers):
        fail('RECOVERY_BUSY_OR_UNKNOWN', 'Active jobs and pending requests must be known and zero')
    if (focus.get('status') != 'online' or selected.get('status') != 'online'
            or selected.get('connected') is not True):
        fail('RECOVERY_BUSY_OR_UNKNOWN', 'Expected Runner must be connected and online')
    if host.active_work(cfg):
        fail('RECOVERY_BUSY_OR_UNKNOWN', 'Active or recovery-required builds or sync work block Provider recovery')
    return {'runner': cfg['client_id'], 'status': 'online', 'active_jobs': 0, 'pending_requests': 0}


def recover(config_path, *, project_id, client_id, provider, apply=False, authorized=False,
            host=None, wait_seconds=15, manager_token_file=None):
    """Inspect by default. Apply sends one reload and never replays an old call."""
    if apply and not authorized:
        fail('AUTHORIZATION_REQUIRED', 'Provider reload requires explicit operator authorization')
    for value in (project_id, client_id, provider):
        if not isinstance(value, str) or not IDENTITY.fullmatch(value):
            fail('INVALID_ARGUMENT', 'Explicit expected project, Runner and Provider identities are required')
    if not isinstance(wait_seconds, (int, float)) or not math.isfinite(wait_seconds) or not 0 < wait_seconds <= 120:
        fail('INVALID_ARGUMENT', 'Readiness wait must be between zero and 120 seconds')
    cfg = load_config(Path(config_path))
    if (cfg['project_id'] != project_id or cfg['client_id'] != client_id
            or cfg.get('provider_id', 'research-build') != provider):
        fail('RECOVERY_IDENTITY_MISMATCH', 'Configuration does not match the expected recovery identities')
    if cfg.get('_projects'):
        fail('RECOVERY_SCOPE_DENIED', 'Provider recovery requires one explicitly scoped project')
    host = host or NativeHost(cfg, manager_token_file=manager_token_file)
    with locked(cfg, 'routing'), locked(cfg, 'build'):
        current = load_config(Path(config_path))
        if current['_config_revision'] != cfg['_config_revision']:
            fail('CONFIG_CHANGED', 'Configuration changed before Provider recovery')
        native = _idle(cfg, host)
        failed = _provider_status(host.providers(cfg), cfg, provider)
        if failed.get('status') != 'failed' or not isinstance(failed.get('errorCode'), str):
            fail('RECOVERY_NOT_REQUIRED', 'Only an explicitly failed Provider can be recovered')
        code = failed['errorCode']
        if not IDENTITY.fullmatch(code):
            fail('RECOVERY_STATE_UNKNOWN', 'Provider failure reason is invalid')
        # The fixed probe relies on the maintained catalog, so this recovery
        # operation cannot accidentally start an unbounded project scan.
        catalog = json.loads(safe_read(cfg['project_root'], 'materials/INDEX_CATALOG.json', 64 * 1024))
        if (not isinstance(catalog, dict) or catalog.get('schema') != 'paper-editor.index-catalog.v1'
                or catalog.get('project_id') != project_id):
            fail('RECOVERY_PROBE_INVALID', 'A project-owned material catalog is required for the read-only probe')
        check = host.check(cfg, provider)
        if (not isinstance(check, dict) or check.get('runner') != client_id or check.get('plugin') != provider
                or check.get('ready') is not True):
            fail('RECOVERY_CHECK_FAILED', 'Manager validation did not confirm this Provider is ready to load')
        report = {'schema': SCHEMA, 'project_id': project_id, 'client_id': client_id, 'provider': provider,
                  'config_revision': cfg['_config_revision'], 'native': native, 'previous_error_code': code,
                  'manager_check_ready': True, 'dry_run': not apply, 'reload_count': 0,
                  'original_request_replayed': False}
        if not apply:
            return report
        # Check again after manager validation before changing the whole set.
        _idle(cfg, host)
        again = _provider_status(host.providers(cfg), cfg, provider)
        if again.get('status') != 'failed' or again.get('errorCode') != code:
            fail('RECOVERY_STATE_CHANGED', 'Provider state changed after the operator check')
        if sha(Path(config_path).read_bytes()) != cfg['_config_revision']:
            fail('CONFIG_CHANGED', 'Configuration changed before Provider reload')
        # No retry: a timeout here leaves an uncertain outcome that needs a new
        # read-only inspection, never another automatic reload.
        result = host.reload(cfg)
        row = _provider_status(result, cfg, provider)
        if row.get('status') != 'ready' or result.get('failures'):
            fail('RECOVERY_RELOAD_FAILED', 'Explicit reload did not return a ready Provider')
        deadline = time.monotonic() + wait_seconds
        while True:
            ready = _provider_status(host.providers(cfg), cfg, provider)
            if ready.get('status') == 'ready' and ready.get('errorCode') is None:
                break
            if ready.get('status') not in ('starting', 'initializing') or time.monotonic() >= deadline:
                fail('RECOVERY_READINESS_FAILED', 'Reloaded Provider did not become ready')
            host.sleep(min(0.25, max(0, deadline - time.monotonic())))
        probe = host.probe(cfg, provider)
        if (not isinstance(probe, dict) or probe.get('project_id') != project_id
                or probe.get('inventory', {}).get('scope') != 'material_catalog'
                or probe.get('material_catalog', {}).get('project_id') != project_id):
            fail('RECOVERY_PROBE_FAILED', 'Fresh read-only probe did not return this project material index')
        final = _provider_status(host.providers(cfg), cfg, provider)
        if final.get('status') != 'ready' or final.get('errorCode') is not None:
            fail('RECOVERY_READINESS_FAILED', 'Provider failed after the fresh read-only probe')
        if sha(Path(config_path).read_bytes()) != cfg['_config_revision']:
            fail('CONFIG_CHANGED', 'Configuration changed during recovery verification')
        return {**report, 'dry_run': False, 'reload_count': 1, 'status': 'ready', 'probe_tool': PROBE,
                'probe_project_id': project_id}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--project-id', required=True)
    parser.add_argument('--client-id', required=True)
    parser.add_argument('--provider', required=True)
    parser.add_argument('--manager-token-file', type=Path,
                        help='Explicit private local manager credential file; never a token value')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--authorized', action='store_true')
    args = parser.parse_args(argv)
    try:
        value = recover(args.config, project_id=args.project_id, client_id=args.client_id,
                        provider=args.provider, apply=args.apply, authorized=args.authorized,
                        manager_token_file=args.manager_token_file)
    except (StudioError, NativeError) as error:
        print(json.dumps({'error': error.as_dict()}, ensure_ascii=False, indent=2))
        return 1
    except (OSError, ValueError):
        print(json.dumps({'error': {'code': 'RECOVERY_INPUT_INVALID',
                                   'message': 'Recovery configuration or material catalog could not be read safely'}}))
        return 1
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
