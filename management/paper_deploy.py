"""Plan, apply and roll back one Paper Studio instance deployment.

Generalizes the audited one-off scripts under docs/audit/*/deploy*.py.
Instance differences live in a deployment profile (paths, Runner unit, Tunnel
alias, release bindings, guard files); per-deployment edits live in a reviewed
change set. ``plan`` only reads. ``apply`` and ``rollback`` require a reviewed
plan or record plus ``--authorized``, write private backups first, update each
file only if it still has the planned bytes, prove readiness, and restore the
previous state automatically when a step fails. The Tunnel key is read only to
reconnect the same alias; it never enters plans, records or error text.
"""
from __future__ import annotations

import argparse
import copy
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
import tomllib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'paper-studio'))

PROFILE_SCHEMA = 'paper-studio.deployment-profile.v1'
CHANGES_SCHEMA = 'paper-studio.deployment-changes.v1'
PLAN_SCHEMA = 'paper-studio.deployment-plan.v2'
RECORD_SCHEMA = 'paper-studio.deployment-record.v1'
PROVIDER_SUFFIX = '/paper-studio/paper_studio/provider/main.py'
LABEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}')
MAX_FILE = 2 * 1024 * 1024


class DeployError(Exception):
    def __init__(self, code, message, **details):
        self.code, self.message, self.details = code, message, details
        super().__init__(message)

    def as_dict(self):
        return {'code': self.code, 'message': self.message, 'details': self.details}


def fail(code, message, **details):
    raise DeployError(code, message, **details)


SECRET_LINE = re.compile(r'(token|secret|api[_-]?key|password|credential)', re.I)
SECRET_VALUE = re.compile(r'([=:]\s*)(["\'])(?!/)([^"\']{8,})\2')


def redact(text):
    """Hide quoted non-path values on secret-looking lines of plan diffs."""
    return '\n'.join(SECRET_VALUE.sub(lambda m: m.group(1) + m.group(2) + '[REDACTED]' + m.group(2), line)
                     if SECRET_LINE.search(line) else line for line in text.split('\n'))


def digest(raw):
    return hashlib.sha256(raw).hexdigest() if raw is not None else None


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def json_bytes(value):
    """The encoding every edited instance JSON file already uses."""
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode()


def absolute(value, what):
    if not isinstance(value, str) or not value.startswith('/'):
        fail('PROFILE_INVALID', what + ' must be an absolute path')
    path = Path(value)
    if Path(os.path.abspath(path)) != path:
        fail('PROFILE_INVALID', what + ' must be canonical', path=value)
    for parent in [path, *path.parents]:
        if parent.is_symlink():
            fail('PATH_DENIED', what + ' must not traverse a symlink', path=value)
    return path


def read_file(path, *, missing_ok=False):
    try:
        info = path.lstat()
    except FileNotFoundError:
        if missing_ok:
            return None
        fail('NOT_FOUND', 'Deployment file is missing', path=str(path))
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_FILE:
        fail('PATH_DENIED', 'Deployment files must be ordinary single-link files below 2 MiB', path=str(path))
    return path.read_bytes()


def load_json(path, schema):
    value = json.loads(read_file(absolute(str(path), 'JSON input')))
    if not isinstance(value, dict) or value.get('schema') != schema:
        fail('PROFILE_INVALID', 'Unexpected document schema', path=str(path), expected=schema)
    return value


def load_profile(path):
    profile = load_json(path, PROFILE_SCHEMA)
    required = {'schema', 'instance_id', 'config_path', 'backup_root', 'runner_service', 'runner_config',
                'tunnel', 'release_bindings', 'guard_files', 'readiness'}
    if set(profile) != required:
        fail('PROFILE_INVALID', 'Exact deployment profile fields are required', missing=sorted(required - set(profile)),
             unknown=sorted(set(profile) - required))
    if not LABEL.fullmatch(profile['instance_id']):
        fail('PROFILE_INVALID', 'Invalid instance_id')
    for key in ('config_path', 'backup_root'):
        absolute(profile[key], key)
    if (profile['runner_service'] is None) != (profile['runner_config'] is None):
        fail('PROFILE_INVALID', 'runner_service and runner_config are configured together')
    if profile['runner_service'] is not None:
        if not re.fullmatch(r'[A-Za-z0-9@._-]{1,120}\.service', profile['runner_service']):
            fail('PROFILE_INVALID', 'Invalid systemd user service name')
        absolute(profile['runner_config'], 'runner_config')
    tunnel = profile['tunnel']
    if (not isinstance(tunnel, dict) or set(tunnel) != {'alias', 'profile_path', 'tunnel_id'}
            or not LABEL.fullmatch(str(tunnel.get('alias'))) or not re.fullmatch(r'tunnel_[0-9a-f]{32}', str(tunnel.get('tunnel_id')))):
        fail('PROFILE_INVALID', 'tunnel needs alias, profile_path and tunnel_id')
    profile_path = absolute(tunnel['profile_path'], 'tunnel.profile_path')
    if profile_path.name != tunnel['alias'] + '.yaml':
        fail('PROFILE_INVALID', 'The Tunnel profile file must be <alias>.yaml')
    bindings = profile['release_bindings']
    if not isinstance(bindings, list) or len(bindings) > 8:
        fail('PROFILE_INVALID', 'release_bindings must be a short list')
    for binding in bindings:
        if (not isinstance(binding, dict) or set(binding) != {'path', 'format', 'occurrences'}
                or binding['format'] not in {'python', 'toml', 'tunnel_profile'}
                or type(binding['occurrences']) is not int or not 1 <= binding['occurrences'] <= 8):
            fail('PROFILE_INVALID', 'Each release binding needs path, format and occurrences')
        absolute(binding['path'], 'release binding')
        if binding['format'] == 'tunnel_profile' and binding['path'] != tunnel['profile_path']:
            fail('PROFILE_INVALID', 'A tunnel_profile binding must name the configured Tunnel profile')
    if not isinstance(profile['guard_files'], list) or len(profile['guard_files']) > 32:
        fail('PROFILE_INVALID', 'guard_files must be a short list')
    for guard in profile['guard_files']:
        absolute(guard, 'guard file')
    readiness = profile['readiness']
    if (not isinstance(readiness, dict) or set(readiness) != {'attempts', 'interval_seconds'}
            or type(readiness['attempts']) is not int or not 1 <= readiness['attempts'] <= 180
            or not isinstance(readiness['interval_seconds'], (int, float)) or not 0 <= readiness['interval_seconds'] <= 10):
        fail('PROFILE_INVALID', 'readiness needs bounded attempts and interval_seconds')
    return profile


def load_changes(path):
    changes = load_json(path, CHANGES_SCHEMA)
    if set(changes) - {'schema', 'summary', 'files', 'daemon_reload'} or not isinstance(changes.get('summary'), str):
        fail('CHANGES_INVALID', 'A change set has schema, summary, files and optional daemon_reload')
    if not isinstance(changes.get('files'), list) or not 1 <= len(changes['files']) <= 16:
        fail('CHANGES_INVALID', 'A change set edits 1..16 files')
    if type(changes.get('daemon_reload', False)) is not bool:
        fail('CHANGES_INVALID', 'daemon_reload must be boolean')
    for item in changes['files']:
        absolute(item.get('path'), 'changed file')
        if item.get('format') not in {'json', 'text'}:
            fail('CHANGES_INVALID', 'Changed files are json or text', path=item.get('path'))
        if 'content' in item:
            if set(item) != {'path', 'format', 'content'} or item['format'] != 'text' or not isinstance(item['content'], str):
                fail('CHANGES_INVALID', 'A created file has path, format=text and content only', path=item['path'])
        elif set(item) != {'path', 'format', 'ops'} or not isinstance(item['ops'], list) or not 1 <= len(item['ops']) <= 32:
            fail('CHANGES_INVALID', 'An edited file has path, format and 1..32 ops', path=item['path'])
    return changes


def _pointer(document, pointer):
    if not isinstance(pointer, str) or not pointer.startswith('/'):
        fail('CHANGES_INVALID', 'JSON pointers start with /', pointer=pointer)
    parts = [part.replace('~1', '/').replace('~0', '~') for part in pointer[1:].split('/')]
    parent = document
    for part in parts[:-1]:
        if not isinstance(parent, dict) or part not in parent:
            fail('CHANGE_PRECONDITION', 'JSON pointer parent is missing', pointer=pointer)
        parent = parent[part]
    if not isinstance(parent, dict):
        fail('CHANGE_PRECONDITION', 'JSON pointer parent is not an object', pointer=pointer)
    return parent, parts[-1]


def apply_json_ops(document, ops):
    document = copy.deepcopy(document)
    for op in ops:
        parent, key = _pointer(document, op.get('pointer'))
        kind = op.get('op')
        if kind in {'append', 'remove'}:
            if set(op) != {'op', 'pointer', 'value'} or not isinstance(parent.get(key), list):
                fail('CHANGE_PRECONDITION', 'append/remove act on an existing list', pointer=op.get('pointer'))
            count = parent[key].count(op['value'])
            if kind == 'append':
                if count:
                    fail('CHANGE_PRECONDITION', 'Value is already present', pointer=op['pointer'], value=op['value'])
                parent[key].append(op['value'])
            else:
                if count != 1:
                    fail('CHANGE_PRECONDITION', 'Value must be present exactly once', pointer=op['pointer'],
                         value=op['value'], found=count)
                parent[key].remove(op['value'])
        elif kind == 'delete':
            if set(op) != {'op', 'pointer', 'expect'}:
                fail('CHANGES_INVALID', 'delete needs the exact expected value', pointer=op.get('pointer'))
            if key not in parent or parent[key] != op['expect']:
                fail('CHANGE_PRECONDITION', 'Current value differs from expect', pointer=op['pointer'])
            del parent[key]
        elif kind == 'set':
            if set(op) - {'op', 'pointer', 'value', 'expect', 'expect_absent'} or ('expect' in op) == bool(op.get('expect_absent')):
                fail('CHANGES_INVALID', 'set needs exactly one of expect or expect_absent', pointer=op.get('pointer'))
            if op.get('expect_absent'):
                if key in parent:
                    fail('CHANGE_PRECONDITION', 'Key already exists', pointer=op['pointer'])
            elif parent.get(key, object()) != op['expect']:
                fail('CHANGE_PRECONDITION', 'Current value differs from expect', pointer=op['pointer'])
            parent[key] = op['value']
        else:
            fail('CHANGES_INVALID', 'Unknown JSON op', op=kind)
    return document


def apply_text_ops(text, ops, path):
    for op in ops:
        if op.get('op') != 'replace' or set(op) != {'op', 'old', 'new', 'count'} or type(op['count']) is not int:
            fail('CHANGES_INVALID', 'Text edits are exact replace operations with a count', path=path)
        found = text.count(op['old'])
        if not op['old'] or found != op['count']:
            fail('CHANGE_PRECONDITION', 'Replacement text count differs from the reviewed count', path=path,
                 expected=op['count'], found=found)
        text = text.replace(op['old'], op['new'])
    return text


IDENTITY_FIELDS = ('instance_id', 'client_id', 'project_id', 'native_project', 'native_artifact_project',
                   'project_root', 'artifact_root', 'state_root', 'server_url', 'credentials')


def _validate_instance_config(path, raw, original):
    """The candidate must pass the same validation the Gateway and provider apply."""
    from paper_studio.security.policy import StudioError, resolve_projects
    value = json.loads(raw); before = json.loads(original)
    if value.get('schema_version') != 1 or value.get('software_id') != 'webcodex-paper-studio':
        fail('CONFIG_INVALID', 'Candidate is not a Paper Studio instance configuration')
    for key in ('instance_id', 'client_id'):
        if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}', str(value.get(key, ''))):
            fail('CONFIG_INVALID', 'Invalid identity', field=key)
    changed = [key for key in IDENTITY_FIELDS if value.get(key) != before.get(key)]
    if changed or value.get('projects') != before.get('projects'):
        # Rebinding, credentials and additional projects belong to the guarded switch workflow.
        fail('CHANGE_PRECONDITION', 'A deployment cannot change instance, project, credential or root identity',
             fields=changed or ['projects'])
    try:
        resolve_projects({**value, '_config_path': str(path), '_config_revision': digest(raw)})
    except StudioError as error:
        fail('CONFIG_INVALID', 'Candidate instance configuration is invalid', reason=error.code, detail=error.message)


def _validate_target(path, raw, fmt):
    if path.suffix == '.py':
        compile(raw, str(path), 'exec')
    elif path.suffix == '.toml':
        tomllib.loads(raw.decode())
    elif fmt == 'json' or path.suffix == '.json':
        json.loads(raw)


def _release_identity(release_path, release_sha256):
    from management.paper_release import verify_release
    manifest = verify_release(Path(release_path), release_sha256)
    root = Path(release_path)
    return root, manifest, {'version': manifest['version'], 'release_id': manifest['release_id'],
        'source_sha256': manifest['source_sha256'], 'manifest_sha256': digest((root / 'manifest.json').read_bytes()),
        'git_commit': manifest['git']['commit'], 'manifest_path': str(root / 'manifest.json')}


def _tunnel_profile_check(before, after):
    import yaml
    old, new = yaml.safe_load(before), yaml.safe_load(after)
    commands_old = old.get('mcp', {}).get('commands', []); commands_new = new.get('mcp', {}).get('commands', [])
    stripped_old = copy.deepcopy(old); stripped_new = copy.deepcopy(new)
    for value in (stripped_old, stripped_new):
        for command in value.get('mcp', {}).get('commands', []):
            command.pop('command', None)
    if stripped_old != stripped_new or len(commands_old) != len(commands_new):
        fail('CHANGE_PRECONDITION', 'A Tunnel profile release binding may change only MCP command paths')


def compute(profile, *, deployment_id, release_path=None, release_sha256=None, changes=None,
            profile_path=None, changes_path=None):
    """Return (plan, targets) without writing anything."""
    if not isinstance(deployment_id, str) or not LABEL.fullmatch(deployment_id):
        fail('INVALID_ARGUMENT', 'A bounded deployment_id is required')
    config_path = Path(profile['config_path'])
    originals, targets, kinds, formats = {}, {}, {}, {}

    def current(path, *, missing_ok=False):
        if path not in originals:
            originals[path] = read_file(path, missing_ok=missing_ok)
            targets[path] = originals[path]
        return targets[path]

    config_before = current(config_path)
    config = json.loads(config_before)
    if config.get('instance_id') != profile['instance_id']:
        fail('PROFILE_INVALID', 'Profile and configuration name different instances')
    release = None
    if release_path is not None:
        release_root, manifest, identity = _release_identity(release_path, release_sha256)
        old_root = str(Path(config['release']['manifest_path']).parent)
        if old_root == str(release_root):
            fail('ALREADY_DEPLOYED', 'The instance already references this release')
        config['extension_version'] = manifest['version']; config['release'] = identity
        release = {'path': str(release_root), 'release_sha256': manifest['release_sha256'],
                   'release_id': manifest['release_id'], 'version': manifest['version'],
                   'previous_root': old_root, 'previous_version': json.loads(config_before).get('extension_version')}
        for binding in profile['release_bindings']:
            path = Path(binding['path']); raw = current(path).decode()
            needle = old_root + (PROVIDER_SUFFIX if binding['format'] == 'toml' else '')
            found = raw.count(needle)
            if found != binding['occurrences']:
                fail('CHANGE_PRECONDITION', 'Release binding count differs from the profile', path=str(path),
                     expected=binding['occurrences'], found=found)
            updated = raw.replace(needle, str(release_root) + needle[len(old_root):]).encode()
            if binding['format'] == 'tunnel_profile':
                _tunnel_profile_check(originals[path], updated)
            targets[path] = updated; kinds[path] = 'release_binding'; formats[path] = binding['format']
        targets[config_path] = json_bytes(config); kinds[config_path] = 'instance_config'; formats[config_path] = 'json'
    for item in (changes or {}).get('files', []):
        path = Path(item['path'])
        if 'content' in item:
            if current(path, missing_ok=True) is not None:
                fail('CHANGE_PRECONDITION', 'A created file already exists', path=str(path))
            if not path.parent.is_dir():
                fail('CHANGE_PRECONDITION', 'A created file needs an existing parent directory', path=str(path))
            targets[path] = item['content'].encode(); kinds.setdefault(path, 'created')
        elif item['format'] == 'json':
            raw = current(path)
            targets[path] = json_bytes(apply_json_ops(json.loads(raw), item['ops']))
            kinds.setdefault(path, 'change_set')
        else:
            targets[path] = apply_text_ops(current(path).decode(), item['ops'], str(path)).encode()
            kinds.setdefault(path, 'change_set')
        formats.setdefault(path, item['format'])
    files = []
    for path in sorted(targets, key=str):
        before, after = originals[path], targets[path]
        if before == after:
            continue
        _validate_target(path, after, formats.get(path))
        if path == config_path:
            _validate_instance_config(path, after, before)
        mode = stat.S_IMODE(path.stat().st_mode) if before is not None else 0o600
        entry = {'path': str(path), 'kind': kinds.get(path, 'change_set'), 'before_sha256': digest(before),
                 'after_sha256': digest(after), 'mode': oct(mode)}
        if formats.get(path) == 'json' and before is not None and json_bytes(json.loads(before)) != before:
            entry['reformatted'] = True
        if profile['runner_config'] and path == Path(profile['runner_config']):
            entry['diff'] = 'suppressed: Runner configuration contains credentials'
            if release:
                entry['diff'] += '; provider release: ' + release['path']
        elif path == Path(profile['tunnel']['profile_path']):
            entry['diff'] = 'suppressed: Tunnel profile holds a credential; only MCP command paths may change'
        else:
            lines = list(difflib.unified_diff((before or b'').decode().splitlines(True), after.decode().splitlines(True),
                                              fromfile='before', tofile='after', n=1))
            entry['diff'] = redact(''.join(lines[:240]) + ('...[truncated]\n' if len(lines) > 240 else ''))
        files.append(entry)
    if not files:
        fail('NOTHING_TO_DEPLOY', 'The planned deployment changes no file')
    changed = {entry['path'] for entry in files}
    guards = {}
    for guard in profile['guard_files']:
        if guard in changed:
            fail('PROFILE_INVALID', 'A guard file cannot also be changed', path=guard)
        guards[guard] = digest(read_file(Path(guard)))
    tunnel = Path(profile['tunnel']['profile_path'])
    if str(tunnel) not in changed:
        guards[str(tunnel)] = digest(read_file(tunnel))
    daemon_reload = bool((changes or {}).get('daemon_reload')) or any(
        '/systemd/user/' in entry['path'] for entry in files)
    plan = {'schema': PLAN_SCHEMA, 'deployment_id': deployment_id, 'instance_id': profile['instance_id'],
        'inputs': {'profile_path': str(profile_path) if profile_path else None,
                   'profile_sha256': digest(canonical(profile)),
                   'changes_path': str(changes_path) if changes_path else None,
                   'changes_sha256': digest(canonical(changes)) if changes else None,
                   'change_summary': (changes or {}).get('summary')},
        'release': release, 'files': files, 'guards': guards, 'daemon_reload': daemon_reload,
        'runner_service': profile['runner_service'],
        'tunnel': {'alias': profile['tunnel']['alias'], 'tunnel_id': profile['tunnel']['tunnel_id']},
        'steps': ['verify reviewed plan and release; no active build/sync work',
                  'write private backups of every changed file',
                  'stop the Tunnel runtime, then the Runner service',
                  'under routing/build locks, write each file only if it still has its planned before_sha256',
                  ('daemon-reload, ' if daemon_reload else '') + 'start the same Runner service and wait for Native/provider readiness',
                  'reconnect the same Tunnel alias with one stdio client and wait for health/ready',
                  'confirm guard files unchanged; on any failure restore originals and the previous services'],
        'secrets_in_plan': False}
    identity = {key: plan[key] for key in ('deployment_id', 'instance_id', 'release', 'guards', 'daemon_reload',
                                           'runner_service', 'tunnel')}
    identity['files'] = [{k: entry[k] for k in ('path', 'before_sha256', 'after_sha256', 'mode')} for entry in files]
    identity['inputs'] = {k: plan['inputs'][k] for k in ('profile_sha256', 'changes_sha256')}
    plan['plan_sha256'] = digest(canonical(identity))
    return plan, {Path(entry['path']): (originals[Path(entry['path'])], targets[Path(entry['path'])]) for entry in files}


class SystemHost:
    """Real local operations; tests substitute a recording fake."""

    def runtime_status(self, config_path):
        from paper_studio.gateway.main import Gateway
        return Gateway(config_path).call('paper_runtime_status', {})

    def active_work(self, config_path):
        from paper_studio.drive.sync_tasks import active_tasks
        from paper_studio.provider.jobs import active_jobs
        from paper_studio.security.policy import load_config
        cfg = load_config(config_path); found = []
        for item in [cfg, *cfg.get('_projects', [])]:
            found += [{'project_id': item['project_id'], 'build_id': job['build_id'], 'status': job['status']}
                      for job in active_jobs(item)]
            found += [{'project_id': item['project_id'], **task} for task in active_tasks(item)]
        return found

    def systemctl(self, *args):
        result = subprocess.run(['systemctl', '--user', *args], capture_output=True, text=True, timeout=120)
        if result.returncode:
            fail('SERVICE_COMMAND_FAILED', 'systemctl failed', command=list(args), detail=(result.stderr or result.stdout)[-1500:])

    def tunnel(self, *args, secret=None):
        from management.paper_tunnels import BINARY
        result = subprocess.run([str(BINARY), 'runtimes', *args], capture_output=True, text=True, timeout=120)
        if result.returncode:
            detail = (result.stderr + '\n' + result.stdout)[-1500:]
            if secret:
                detail = detail.replace(secret, '[REDACTED]')
            detail = re.sub(r'(?:sk-|eyJ)[A-Za-z0-9_.-]+', '[REDACTED]', detail)
            fail('TUNNEL_COMMAND_FAILED', 'tunnel-client failed', command=args[0], exit_code=result.returncode, detail=detail)
        return result.stdout

    def tunnel_status(self, alias):
        from management.paper_tunnels import runtime_status, safe_runtime
        raw = runtime_status(alias)
        if not isinstance(raw, dict):
            fail('TUNNEL_STATUS_INVALID', 'Tunnel runtime status must be an object')
        # Current tunnel-client reports tunnel_id at the top level. Older
        # responses used tunnel.id; accept either without hiding disagreement.
        identities = []
        if raw.get('tunnel_id') is not None:
            identities.append(raw['tunnel_id'])
        legacy = raw.get('tunnel')
        if legacy is not None:
            if not isinstance(legacy, dict):
                fail('TUNNEL_STATUS_INVALID', 'Legacy Tunnel identity must be an object')
            if legacy.get('id') is not None:
                identities.append(legacy['id'])
        if any(not isinstance(value, str) or not re.fullmatch(r'tunnel_[0-9a-f]{32}', value)
               for value in identities):
            fail('TUNNEL_IDENTITY_INVALID', 'Tunnel runtime returned an invalid identity')
        if len(set(identities)) > 1:
            fail('TUNNEL_IDENTITY_CONFLICT', 'Tunnel runtime returned conflicting identities')
        return {**safe_runtime(raw), 'tunnel_id': identities[0] if identities else None}

    def processes(self):
        rows = []
        for entry in Path('/proc').iterdir():
            if not entry.name.isdigit():
                continue
            try:
                command = (entry / 'cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace').strip()
                parent = int((entry / 'stat').read_text().rsplit(')', 1)[1].split()[1])
            except (OSError, ValueError, IndexError):
                continue
            rows.append({'pid': int(entry.name), 'ppid': parent, 'command': command})
        return rows

    def sleep(self, seconds):
        time.sleep(seconds)


def _write(path, raw, mode):
    path = Path(path)
    stage = path.with_name('.' + path.name + '.deploy-' + str(os.getpid()))
    fd = os.open(stage, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        view = memoryview(raw)
        while view:
            view = view[os.write(fd, view):]
        os.fchmod(fd, mode); os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(stage, path)


def _private_write(path, raw):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, raw); os.fsync(fd)
    finally:
        os.close(fd)


def _record(path, value):
    stage = path.with_name('.' + path.name + '.stage')
    stage.write_bytes(json_bytes(value)); stage.chmod(0o600); os.replace(stage, path)


def _tunnel_secret(profile):
    import yaml
    value = yaml.safe_load(read_file(Path(profile['tunnel']['profile_path'])))
    key = (value.get('control_plane') or {}).get('api_key')
    if not isinstance(key, str) or not key:
        fail('TUNNEL_PROFILE_INVALID', 'The Tunnel profile has no runtime key')
    return key


def _command(profile_bytes):
    import yaml
    commands = (yaml.safe_load(profile_bytes).get('mcp') or {}).get('commands') or []
    if not commands or not isinstance(commands[0].get('command'), str):
        fail('TUNNEL_PROFILE_INVALID', 'The Tunnel profile has no MCP command')
    return commands[0]['command']


def observe(profile, host, *, release_root=None, tunnel=True):
    """One readiness observation; never starts or stops anything."""
    config_path = profile['config_path']; result = {'ready': False}
    config = json.loads(read_file(Path(config_path)))
    try:
        status = host.runtime_status(config_path)
        focus = status['native']['focus']
        result.update(native=focus.get('status'), runner=focus.get('client_id'), active_jobs=focus.get('active_jobs'),
                      provider=(status.get('provider') or {}).get('status'), extension_version=status.get('extension_version'))
    except Exception as error:  # A probe failure is a not-ready observation.
        result['runtime_error'] = getattr(error, 'code', type(error).__name__)
        return result
    ok = result['native'] == 'online' and result['runner'] == config['client_id'] and result['provider'] == 'ready'
    if release_root and profile['runner_service']:
        provider = str(release_root) + PROVIDER_SUFFIX; runner_flag = '--config ' + profile['runner_config']
        rows = host.processes(); commands = {row['pid']: row['command'] for row in rows}
        running = [row['pid'] for row in rows if provider in row['command']
                   and runner_flag in commands.get(row['ppid'], '')]
        result['provider_process_release'] = bool(running)
        ok = ok and bool(running)
    if tunnel:
        try:
            state = host.tunnel_status(profile['tunnel']['alias'])
        except Exception as error:
            state = {'error': getattr(error, 'code', type(error).__name__)}
        result['tunnel'] = {key: state.get(key) for key in ('process_running', 'healthz', 'readyz', 'tunnel_id', 'error')}
        ok = ok and state.get('process_running') is True and state.get('healthz') == 200 and state.get('readyz') == 200 \
            and state.get('tunnel_id') == profile['tunnel']['tunnel_id']
    result['ready'] = ok
    return result


def wait_ready(profile, host, *, release_root=None, tunnel=True):
    last = None
    for _ in range(profile['readiness']['attempts']):
        last = observe(profile, host, release_root=release_root, tunnel=tunnel)
        if last['ready']:
            return last
        host.sleep(profile['readiness']['interval_seconds'])
    fail('READINESS_FAILED', 'Instance did not become ready', last=last, tunnel_checked=tunnel)


def _connect(profile, host, command, secret, release_root):
    tunnel = profile['tunnel']; directory = str(Path(tunnel['profile_path']).parent)
    try:
        alive = host.tunnel_status(tunnel['alias']).get('process_running')
    except Exception:
        alive = None  # The preceding stop returned success; an unreadable status is recorded, not assumed live.
    if alive is True:
        # A runtime still reported running for this alias must never get a second stdio client.
        fail('TUNNEL_STILL_RUNNING', 'The Tunnel runtime is still running; refusing a second connect', alias=tunnel['alias'])
    try:
        host.tunnel('connect', '--alias', tunnel['alias'], '--profile', tunnel['alias'], '--profile-dir', directory,
                    '--tunnel-id', tunnel['tunnel_id'], '--runtime-api-key', secret, '--mcp-command', command, '--json',
                    secret=secret)
    except DeployError as error:
        # tunnel-client can exit non-zero while its one process is still starting.
        # Observe that exact alias; never start a second stdio client.
        if error.code != 'TUNNEL_COMMAND_FAILED':
            raise
    return wait_ready(profile, host, release_root=release_root, tunnel=True)


def _restore_profile_bytes(profile, expected):
    """tunnel-client may re-serialize its profile; keep reviewed bytes when semantics match."""
    import yaml
    path = Path(profile['tunnel']['profile_path']); actual = read_file(path)
    if actual == expected:
        return 'unchanged'
    if yaml.safe_load(actual) != yaml.safe_load(expected):
        fail('TUNNEL_PROFILE_CHANGED', 'Tunnel profile changed semantically during connect; retained for review')
    _write(path, expected, stat.S_IMODE(path.stat().st_mode))
    return 'reserialized_bytes_restored'


def _quiesce(profile, host):
    work = host.active_work(profile['config_path'])
    if work:
        fail('DEPLOY_BLOCKED', 'Active or recovery-required build/sync work blocks deployment', work=work[:20])


def _activate_job_indexes(previous, current):
    """Import exactly when a reviewed config starts an indexed writer."""
    from paper_studio.provider import job_index
    old_projects = {item['project_id']: item for item in (previous, *previous.get('_projects', []))}
    results = []
    for project in (current, *current.get('_projects', [])):
        if job_index.enabled(project) and not job_index.enabled(old_projects.get(project['project_id'], {})):
            # No Runner or tunnel is live here. Following a rollback the old writer
            # may have appended jobs, so an existing index must be reconciled.
            results.append(job_index.import_history(project, authorized=True, reconcile=True))
    return results


def _switch(profile, host, writes, applied, progress, *, daemon_reload, release_root, profile_target, secret):
    """Stop, write (compare-and-swap), restart and prove readiness. Returns observations."""
    from paper_studio.security.policy import load_config, locked
    host.tunnel('stop', profile['tunnel']['alias'], '--json'); progress['tunnel_stopped'] = True
    if profile['runner_service']:
        progress['runner_stop_attempted'] = True
        host.systemctl('stop', profile['runner_service'])
    cfg = load_config(profile['config_path'])
    with locked(cfg, 'routing'):
        with locked(cfg, 'build'):
            _quiesce(profile, host)
            for path, (expected, _target, _mode) in writes.items():
                if read_file(path, missing_ok=True) != expected:
                    fail('CONCURRENT_CHANGE', 'A file changed after review; nothing was written', path=str(path))
            for path, (_expected, target, mode) in writes.items():
                _write(path, target, mode); applied.append(path)
        current = load_config(profile['config_path'])
        job_indexes = _activate_job_indexes(cfg, current)
    if daemon_reload:
        host.systemctl('daemon-reload')
    if profile['runner_service']:
        host.systemctl('start', profile['runner_service'])
    local = wait_ready(profile, host, release_root=release_root, tunnel=False)
    remote = _connect(profile, host, _command(profile_target), secret, release_root)
    return {'local': local, 'remote': remote, 'tunnel_profile_bytes': _restore_profile_bytes(profile, profile_target),
            'job_indexes_imported': job_indexes}


def _restore(profile, host, applied, originals, targets, *, daemon_reload, release_root, profile_bytes, secret,
             progress=None):
    progress = {} if progress is None else progress
    try:
        host.tunnel('stop', profile['tunnel']['alias'], '--json')
    except DeployError:
        pass  # _connect independently refuses while the runtime is still reported running.
    if profile['runner_service']:
        host.systemctl('stop', profile['runner_service'])
    from paper_studio.security.policy import load_config, locked
    cfg = load_config(profile['config_path'])
    with locked(cfg, 'routing'):
        with locked(cfg, 'build'):
            for path in applied:
                if read_file(path, missing_ok=True) != targets[path]:
                    fail('ROLLBACK_BLOCKED', 'A changed file was edited again; automatic restore stopped', path=str(path))
            for path in reversed(applied):
                if originals[path] is None:
                    path.unlink(missing_ok=True)
                else:
                    _write(path, originals[path], stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600)
        if Path(profile['config_path']) in applied:
            _activate_job_indexes(cfg, load_config(profile['config_path']))
    progress['files_restored'] = True
    if daemon_reload:
        host.systemctl('daemon-reload')
    if profile['runner_service']:
        host.systemctl('start', profile['runner_service'])
    progress['local_ready'] = wait_ready(profile, host, release_root=release_root, tunnel=False)['ready']
    remote = _connect(profile, host, _command(profile_bytes), secret, release_root)
    progress['tunnel_ready'] = remote['ready']
    _restore_profile_bytes(profile, profile_bytes)
    return remote


def apply(profile_path, plan_path, *, authorized=False, host=None, evidence_dir=None):
    if not authorized:
        fail('AUTHORIZATION_REQUIRED', 'apply writes production files; pass --authorized after review')
    host = host or SystemHost()
    reviewed = load_json(plan_path, PLAN_SCHEMA)
    profile = load_profile(profile_path)
    changes_path = reviewed['inputs']['changes_path']
    changes = load_changes(changes_path) if changes_path else None
    release = reviewed['release']
    plan, targets = compute(profile, deployment_id=reviewed['deployment_id'],
                            release_path=release['path'] if release else None,
                            release_sha256=release['release_sha256'] if release else None,
                            changes=changes, profile_path=profile_path, changes_path=changes_path)
    if plan['plan_sha256'] != reviewed['plan_sha256']:
        fail('PLAN_STALE', 'Current files, inputs or release differ from the reviewed plan; plan again',
             reviewed=reviewed['plan_sha256'], current=plan['plan_sha256'])
    config = json.loads(read_file(Path(profile['config_path'])))
    before = host.runtime_status(profile['config_path'])
    focus = before['native']['focus']
    if focus.get('status') != 'online' or focus.get('client_id') != config['client_id'] or focus.get('active_jobs'):
        fail('DEPLOY_BLOCKED', 'Runner must be online, idle and match the instance', runner=focus.get('client_id'),
             status=focus.get('status'), active_jobs=focus.get('active_jobs'))
    _quiesce(profile, host)
    backup = Path(profile['backup_root']) / reviewed['deployment_id']
    backup.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        backup.mkdir(mode=0o700)
    except FileExistsError:
        fail('DEPLOYMENT_EXISTS', 'This deployment_id already has a backup; inspect its record', path=str(backup))
    originals = {path: pair[0] for path, pair in targets.items()}
    after = {path: pair[1] for path, pair in targets.items()}
    record = {'schema': RECORD_SCHEMA, 'status': 'prepared', 'deployment_id': reviewed['deployment_id'],
              'instance_id': profile['instance_id'], 'plan_sha256': plan['plan_sha256'],
              'profile_path': str(profile_path), 'release': release, 'daemon_reload': plan['daemon_reload'],
              'backup_directory': str(backup), 'changes': [], 'secrets_in_record': False}
    for index, entry in enumerate(plan['files']):
        path = Path(entry['path']); saved = None
        if originals[path] is not None:
            saved = backup / f'{index:02d}.original'; _private_write(saved, originals[path])
        record['changes'].append({'path': entry['path'], 'kind': entry['kind'], 'mode': entry['mode'],
                                  'before_sha256': entry['before_sha256'], 'after_sha256': entry['after_sha256'],
                                  'backup': str(saved) if saved else None})
    record_path = backup / 'deployment.json'; _record(record_path, record)
    secret = _tunnel_secret(profile)
    tunnel_path = Path(profile['tunnel']['profile_path'])
    profile_before = originals.get(tunnel_path) or read_file(tunnel_path)
    profile_after = after.get(tunnel_path, profile_before)
    release_root = Path(release['path']) if release else None
    previous_root = Path(release['previous_root']) if release else None
    writes = {Path(entry['path']): (originals[Path(entry['path'])], after[Path(entry['path'])], int(entry['mode'], 8))
              for entry in plan['files']}
    applied = []; progress = {'tunnel_stopped': False}
    try:
        observed = _switch(profile, host, writes, applied, progress, daemon_reload=plan['daemon_reload'],
                           release_root=release_root, profile_target=profile_after, secret=secret)
        for guard, expected in plan['guards'].items():
            if digest(read_file(Path(guard))) != expected:
                fail('GUARD_CHANGED', 'An unrelated guarded file changed during deployment', path=guard)
        record.update(status='deployed', observations=observed, completed_at=time.time())
    except Exception as error:
        record.update(error=error.as_dict() if isinstance(error, DeployError) else {'code': type(error).__name__,
                      'message': str(error)[:500].replace(secret, '[REDACTED]')})
        if not progress['tunnel_stopped']:
            # The first stop failed: no service was stopped and no file was written.
            record.update(status='failed_before_change')
            _record(record_path, record); _evidence(evidence_dir, record)
            raise
        record['status'] = 'restoring'; _record(record_path, record)
        restore = {}
        try:
            remote = _restore(profile, host, applied, originals, after,
                              daemon_reload=plan['daemon_reload'], release_root=previous_root,
                              profile_bytes=profile_before, secret=secret, progress=restore)
            record.update(status='rolled_back', restored_observation=remote)
        except Exception as restore_error:
            record.update(status='rollback_failed', restore_error=restore_error.as_dict()
                          if isinstance(restore_error, DeployError) else {'code': type(restore_error).__name__})
        record.update(progress=progress, restore_progress=restore)
        _record(record_path, record); _evidence(evidence_dir, record)
        raise
    _record(record_path, record); _evidence(evidence_dir, record)
    return record


def rollback(record_path, *, authorized=False, host=None, evidence_dir=None):
    """Return a deployed instance to its backed-up files and previous release."""
    if not authorized:
        fail('AUTHORIZATION_REQUIRED', 'rollback writes production files; pass --authorized after review')
    host = host or SystemHost(); record_path = absolute(str(record_path), 'record')
    record = json.loads(read_file(record_path))
    if record.get('schema') != RECORD_SCHEMA or record.get('status') != 'deployed':
        fail('ROLLBACK_NOT_APPLICABLE', 'Only a deployed record can be rolled back', status=record.get('status'))
    profile = load_profile(record['profile_path'])
    originals, after, applied = {}, {}, []
    for change in record['changes']:
        path = Path(change['path']); applied.append(path)
        if digest(read_file(path, missing_ok=True)) != change['after_sha256']:
            fail('ROLLBACK_BLOCKED', 'File changed after deployment; reconcile manually', path=str(path))
        after[path] = read_file(path, missing_ok=True)
        originals[path] = read_file(Path(change['backup'])) if change['backup'] else None
        if digest(originals[path]) != change['before_sha256']:
            fail('BACKUP_TAMPERED', 'Backup bytes differ from the recorded original', path=str(path))
    _quiesce(profile, host)
    secret = _tunnel_secret(profile)
    tunnel_path = Path(profile['tunnel']['profile_path'])
    profile_bytes = originals.get(tunnel_path) or read_file(tunnel_path)
    previous = Path(record['release']['previous_root']) if record.get('release') else None
    result = {'schema': RECORD_SCHEMA, 'status': 'rolling_back', 'deployment_id': record['deployment_id'],
              'rollback_of': str(record_path), 'started_at': time.time(), 'secrets_in_record': False}
    target = record_path.with_name('rollback.json')
    if target.exists():
        fail('DEPLOYMENT_EXISTS', 'A rollback record already exists', path=str(target))
    _record(target, result)
    try:
        remote = _restore(profile, host, applied, originals, after, daemon_reload=record['daemon_reload'],
                          release_root=previous, profile_bytes=profile_bytes, secret=secret)
        result.update(status='rolled_back', observation=remote)
    except Exception as error:
        result.update(status='rollback_failed', error=error.as_dict() if isinstance(error, DeployError)
                      else {'code': type(error).__name__})
        _record(target, result); _evidence(evidence_dir, result, 'rollback')
        raise
    _record(target, result); _evidence(evidence_dir, result, 'rollback')
    return result


def _evidence(directory, record, kind='deployment'):
    if directory:
        directory = absolute(str(directory), 'evidence directory'); directory.mkdir(parents=True, exist_ok=True)
        _record(directory / f"{record['deployment_id']}-{kind}.json", record)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    command = sub.add_parser('plan')
    command.add_argument('--profile', type=Path, required=True)
    command.add_argument('--deployment-id', required=True)
    command.add_argument('--release-path', type=Path)
    command.add_argument('--release-sha256')
    command.add_argument('--changes', type=Path)
    command.add_argument('--output', type=Path)
    command = sub.add_parser('apply')
    command.add_argument('--profile', type=Path, required=True)
    command.add_argument('--plan', type=Path, required=True)
    command.add_argument('--authorized', action='store_true')
    command.add_argument('--evidence-dir', type=Path)
    command = sub.add_parser('rollback')
    command.add_argument('--record', type=Path, required=True)
    command.add_argument('--authorized', action='store_true')
    command.add_argument('--evidence-dir', type=Path)
    command = sub.add_parser('readiness')
    command.add_argument('--profile', type=Path, required=True)
    command.add_argument('--no-tunnel', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.action == 'plan':
            if (args.release_path is None) != (args.release_sha256 is None):
                fail('INVALID_ARGUMENT', '--release-path and --release-sha256 are given together')
            profile = load_profile(args.profile)
            changes = load_changes(args.changes) if args.changes else None
            result, _ = compute(profile, deployment_id=args.deployment_id, release_path=args.release_path,
                                release_sha256=args.release_sha256, changes=changes, profile_path=args.profile,
                                changes_path=args.changes)
            if args.output:
                output = absolute(str(args.output), 'plan output'); output.parent.mkdir(parents=True, exist_ok=True)
                with output.open('x', encoding='utf-8') as stream:
                    stream.write(json_bytes(result).decode())
        elif args.action == 'apply':
            result = apply(args.profile, args.plan, authorized=args.authorized, evidence_dir=args.evidence_dir)
        elif args.action == 'rollback':
            result = rollback(args.record, authorized=args.authorized, evidence_dir=args.evidence_dir)
        else:
            result = observe(load_profile(args.profile), SystemHost(), tunnel=not args.no_tunnel)
    except DeployError as error:
        print(json.dumps({'error': error.as_dict()}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get('ready', True) is not False else 1


if __name__ == '__main__':
    sys.exit(main())
