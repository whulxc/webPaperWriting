"""Local operator closure for a provider build that can no longer finish.

A queued/running job whose worker identity is dead, or a job that already ended
as recovery_required (for example after a sandbox timeout), blocks every new
build of its project. Provider recovery only resumes TeX/Drive jobs with
unchanged inputs; other kinds and changed manuscripts have no resume path.

This management-only command records an audited terminal ``failed`` state after
proving that the worker is not alive and that no Paper Studio sandbox unit is
active. It never deletes job files, never publishes outputs, keeps the exact
prior status bytes, and is not exposed through MCP. Default is a dry run.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'paper-studio'))
from paper_studio.provider import jobs  # noqa: E402
from paper_studio.security.sandbox import SandboxUserBusError, user_bus_environment  # noqa: E402
from paper_studio.security.policy import (StudioError, atomic_json, encoded, fail, load_config,  # noqa: E402
                                          locked, safe_read, safe_write_new, select_project, sha)

from paper_studio.provider.operator_recovery import SCHEMA, verified_operator_closure  # noqa: E402
UNIT = re.compile(r'paper-studio-build-[0-9a-f]{32}\.service\Z')


def active_sandbox_units():
    """Return active isolated build units for this user; unknown state fails closed."""
    systemctl = shutil.which('systemctl')
    if not systemctl:
        fail('SANDBOX_UNIT_STATE_UNKNOWN', 'systemctl is unavailable; cannot prove that no isolated build is running')
    try:
        environment = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', **user_bus_environment()}
        probe = subprocess.run([systemctl, '--user', 'list-units', '--all', '--plain', '--no-legend',
                                '--no-pager', 'paper-studio-build-*'], capture_output=True, text=True, timeout=10,
                               env=environment)
    except (OSError, subprocess.TimeoutExpired, SandboxUserBusError):
        fail('SANDBOX_UNIT_STATE_UNKNOWN', 'Isolated build units could not be listed')
    if probe.returncode:
        fail('SANDBOX_UNIT_STATE_UNKNOWN', 'Isolated build units could not be listed', returncode=probe.returncode)
    units = []
    for line in probe.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 4 and UNIT.fullmatch(fields[0]) and fields[2] in {'active', 'activating', 'deactivating', 'reloading'}:
            units.append(fields[0])
    return units


def _identity(job, stored):
    identity = stored.get('worker_identity')
    if identity is None and (job / 'launch.json').exists():
        identity = json.loads(safe_read(job, 'launch.json', 4096))
    return identity if isinstance(identity, dict) else None


def inspect(cfg, build_id, *, units=active_sandbox_units):
    job = jobs.job_path(cfg, build_id)
    raw = safe_read(job, 'status.json', 4 * 1024 * 1024)
    try:
        stored = json.loads(raw)
    except ValueError:
        fail('BUILD_RECORD_CORRUPT', 'Stored build status is not valid JSON; repair needs manual evidence review', build_id=build_id)
    if stored.get('project_id') != cfg['project_id'] or stored.get('build_id') != build_id:
        fail('PROJECT_DENIED', 'Build status belongs to another project or job')
    observed = jobs.read_status(cfg, build_id)
    identity = _identity(job, stored)
    live = (jobs.process_identity(identity['pid'])
            if identity and type(identity.get('pid')) is int and identity['pid'] > 0 else None)
    alive = bool(live and live.get('state') != 'Z' and live.get('start_ticks') == identity.get('start_ticks'))
    closable = observed['status'] == 'recovery_required' and not alive
    report = {'project_id': cfg['project_id'], 'build_id': build_id, 'kind': stored.get('kind'),
              'stored_status': stored.get('status'), 'observed_status': observed['status'],
              'observed_error': observed.get('error'), 'worker_identity': identity, 'worker_alive': alive,
              'prior_status_sha256': sha(raw), 'closable': closable,
              'blocks_new_builds': observed['status'] in {'queued', 'running', 'recovery_required'}}
    if closable:
        report['active_sandbox_units'] = units()
        report['closable'] = not report['active_sandbox_units']
    return job, raw, stored, report


def blocking(cfg):
    """List jobs that currently block new provider builds of this project."""
    rows = []
    for status in jobs.active_jobs(cfg):
        rows.append({'build_id': status['build_id'], 'kind': status.get('kind'), 'status': status['status'],
                     'error': status.get('error')})
    return {'project_id': cfg['project_id'], 'blocking_jobs': rows}


def close(cfg, build_id, reason, *, authorized=False, units=active_sandbox_units):
    if not isinstance(reason, str) or not 8 <= len(reason.strip()) <= 1000:
        fail('INVALID_ARGUMENT', 'A specific operator reason of 8..1000 characters is required')
    with locked(cfg, 'build'):
        job, raw, stored, report = inspect(cfg, build_id, units=units)
        if report['observed_status'] in {'completed', 'failed'}:
            fail('BUILD_CLOSURE_NOT_REQUIRED', 'Build already has a terminal state', status=report['observed_status'])
        if report['worker_alive'] or report['observed_status'] in {'queued', 'running'}:
            fail('BUILD_BUSY', 'The worker is still alive; wait for its own terminal state', build_id=build_id)
        if report.get('active_sandbox_units'):
            fail('SANDBOX_UNIT_ACTIVE', 'An isolated build unit is still active; wait until it stops',
                 units=report['active_sandbox_units'])
        if not report['closable']:
            fail('BUILD_CLOSURE_DENIED', 'Build state does not permit operator closure', status=report['observed_status'])
        closure = {'schema': SCHEMA, **{k: report[k] for k in ['project_id', 'build_id', 'kind', 'stored_status',
                   'observed_status', 'observed_error', 'worker_identity', 'prior_status_sha256']},
                   'reason': reason.strip(), 'active_sandbox_units': [], 'outputs_published': False,
                   'deleted_files': [], 'closed_at': time.time(),
                   'prior_status_file': 'operator-prior-status.json',
                   'prior_recovery_required': stored.get('recovery_required',False)}
        if not authorized:
            return {**report, 'dry_run': True, 'would_write': ['operator-prior-status.json', 'operator-closure.json', 'status.json'],
                    'closure': {k: v for k, v in closure.items() if k != 'closed_at'}}
        if (job / 'operator-closure.json').exists():
            fail('BUILD_CLOSURE_EXISTS', 'An operator closure record already exists for this build')
        if (job / 'operator-prior-status.json').exists():
            fail('BUILD_CLOSURE_INCOMPLETE', 'Prior status evidence already exists; inspect the interrupted closure before retrying')
        safe_write_new(job,'operator-prior-status.json',raw)
        atomic_json(job / 'operator-closure.json', closure, True)
        closure_raw = safe_read(job, 'operator-closure.json', 1024 * 1024)
        # Compare-and-swap on the exact inspected status bytes under the build lock.
        if safe_read(job, 'status.json', 4 * 1024 * 1024) != raw:
            fail('REVISION_CONFLICT', 'Build status changed during operator closure; the closure record is retained for review')
        prior_error = stored.get('error') or report['observed_error']
        value = {**stored, 'status': 'failed', 'recovery_required':False,
                 'finished_at': stored.get('finished_at') or closure['closed_at'],
                 'error': {'code': 'OPERATOR_CLOSED',
                           'message': 'Local operator closed a build that could no longer finish; all evidence is retained',
                           'prior_status': stored.get('status'), 'prior_error': prior_error},
                 'operator_closure': {'path': 'operator-closure.json', 'sha256': sha(closure_raw)}}
        jobs._save_status(cfg,job,value)
        return {**report, 'dry_run': False, 'closed': True, 'status': 'failed',
                'operator_closure_sha256': sha(closure_raw), 'status_sha256': sha(encoded(value))}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('action', choices=['list', 'inspect', 'close'])
    parser.add_argument('--config', required=True, help='Explicit absolute private instance configuration')
    parser.add_argument('--project-id', required=True)
    parser.add_argument('--build-id')
    parser.add_argument('--reason')
    parser.add_argument('--authorized', action='store_true', help='Apply the closure; omitted means dry run')
    args = parser.parse_args(argv)
    try:
        cfg = select_project(load_config(args.config), args.project_id)
        if args.action == 'list':
            result = blocking(cfg)
        elif not args.build_id:
            fail('INVALID_ARGUMENT', '--build-id is required')
        elif args.action == 'inspect':
            result = inspect(cfg, args.build_id)[3]
        else:
            result = close(cfg, args.build_id, args.reason, authorized=args.authorized)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except StudioError as error:
        print(json.dumps(error.as_dict(), ensure_ascii=False))
        return 1


if __name__ == '__main__':
    sys.exit(main())
