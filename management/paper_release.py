"""Build and verify one immutable Paper Studio source release, without activation.

This generalizes the capture/hash/stage pattern of management.studio.release.
It deliberately has no service, Tunnel, project binding or credential operation.
"""
from __future__ import annotations

import argparse
import ast
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import uuid
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = 'paper-studio.source-release.v1'
SOFTWARE_ID = 'webcodex-paper-studio'
SOURCE_TREE = PurePosixPath('paper-studio/paper_studio')
EXTRA_SOURCES = ('management/native_client.py', 'management/paper_rebind.py', 'management/paper_target_index.py',
                 'management/paper_deploy.py', 'management/paper_release.py', 'management/paper_handoff_catalog.py',
                 'management/paper_tunnels.py', 'management/paper_instance_registry.py',
                 'management/paper_build_recovery.py', 'management/paper_provider_recovery.py',
                 'management/paper_job_index.py', 'management/paper_history_index.py',
                 'management/private_files.py', 'management/paper_review_display.py',
                 'management/paper_review_comments.py',
                 'management/paper_tex_redline.py',
                 'management/paper_drive_folder_recovery.py',
                 'paper-studio/paper_studio/gateway/section-download.html',
                 'paper-studio/paper_studio/gateway/pdf-preview.html',
                 'paper-studio/paper_studio/gateway/preview_vendor/pdf.min.mjs',
                 'paper-studio/paper_studio/gateway/preview_vendor/pdf.worker.min.mjs',
                 'paper-studio/paper_studio/gateway/preview_vendor/LICENSE',
                 'paper-studio/paper_studio/gateway/preview_vendor/SOURCE.json',
                 'paper-studio/paper_studio/gateway/preview_vendor/cmaps.json',
                 'paper-studio/paper_studio/gateway/preview_vendor/CMAPS_LICENSE',
                 'paper-studio/requirements.optional.txt',
                 'paper-studio/requirements.zotero.txt')
MAX_SOURCE_BYTES = 64 * 1024 * 1024
EXCLUDED_SOURCE_PARTS = frozenset({'__pycache__', 'tests', 'fixtures', 'logs', 'artifacts',
                                  'credentials', 'secrets', 'backups', 'tmp'})


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def ordinary(path, *, directory=False):
    """Reject aliases before reading source/config or publishing a release."""
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('Explicit absolute path required')
    if path.resolve() != path:
        raise ValueError('Canonical nonsymlink absolute path required')
    for parent in [path, *path.parents]:
        if parent.is_symlink():
            raise ValueError('Symlink paths are not release inputs')
    if path.exists():
        info = path.stat()
        if directory and not stat.S_ISDIR(info.st_mode):
            raise ValueError('Directory required')
        if not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
            raise ValueError('Ordinary single-link file required')
    return path


def source_files(root):
    root = ordinary(root, directory=True)
    package = root / SOURCE_TREE
    if not package.is_dir():
        raise ValueError('Paper Studio source package is missing')
    paths = []
    for path in sorted(package.rglob('*')):
        if path.is_symlink():
            raise ValueError('Source package symlinks are not permitted')
        parts = path.relative_to(package).parts
        if any(part.startswith('.') or part in EXCLUDED_SOURCE_PARTS for part in parts):
            continue
        if path.is_file() and path.suffix == '.py':
            paths.append(ordinary(path))
    for relative in EXTRA_SOURCES:
        path = ordinary(root / relative)
        if not path.is_file():
            raise ValueError('Required release support file is missing: ' + relative)
        paths.append(path)
    paths = sorted(set(paths))
    if not (root / SOURCE_TREE / '__init__.py') in paths:
        raise ValueError('Versioned Paper Studio entry is missing')
    return root, paths


def git_identity(root):
    def run(*args):
        result = subprocess.run(['git', '-C', str(root), *args], capture_output=True,
                                timeout=10, check=False)
        return result.returncode, result.stdout
    code, inside = run('rev-parse', '--is-inside-work-tree')
    if code or inside.strip() != b'true':
        return {'commit': None, 'state': 'not_a_git_worktree', 'source_dirty': True,
                'status_sha256': None}
    commit_code, commit = run('rev-parse', '--verify', 'HEAD')
    status_code, status = run('status', '--porcelain=v1', '--untracked-files=normal')
    if status_code:
        raise RuntimeError('Git status could not be read; release provenance is unknown')
    return {'commit': commit.decode().strip() if not commit_code else None,
            'state': 'committed' if not commit_code else 'unborn',
            'source_dirty': bool(status) or bool(commit_code),
            'status_sha256': sha(status)}


def version_from_source(raw):
    for node in ast.parse(raw.decode('utf-8')).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == '__version__'
                                                for t in node.targets):
            version = ast.literal_eval(node.value)
            if isinstance(version, str) and re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+(?:[A-Za-z0-9.+-]*)', version):
                return version
    raise ValueError('A literal safe Paper Studio version is required')


def allowed_member(relative):
    if not isinstance(relative, str) or not relative:
        return False
    path = PurePosixPath(relative)
    return (path.as_posix() == relative and not path.is_absolute()
            and all(part not in {'', '.', '..'} and not part.startswith('.')
                    and part not in EXCLUDED_SOURCE_PARTS for part in path.parts)
            and ((path.is_relative_to(SOURCE_TREE) and path.suffix == '.py')
                 or relative in EXTRA_SOURCES))


def verify_release(release_path, expected_sha256=None):
    release = ordinary(release_path, directory=True)
    manifest_path = ordinary(release / 'manifest.json')
    if manifest_path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError('Release manifest exceeds its source inventory bound')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if manifest.get('schema') != SCHEMA or manifest.get('software_id') != SOFTWARE_ID:
        raise ValueError('Unknown release identity')
    identity = {k: v for k, v in manifest.items() if k not in {'release_sha256', 'release_id'}}
    actual = sha(encoded(identity))
    expected_name = 'paper-studio-' + manifest['version'] + '-' + actual[:16]
    if (manifest.get('release_sha256') != actual or manifest.get('release_id') != expected_name
            or release.name != expected_name or expected_sha256 is not None and actual != expected_sha256):
        raise RuntimeError('Immutable release manifest identity changed')
    rows = manifest.get('files')
    if not isinstance(rows, list) or not rows or len(rows) > 4096:
        raise ValueError('Release source manifest is empty')
    selected = set(); total = 0
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'path', 'bytes', 'sha256'} or not allowed_member(row['path']):
            raise ValueError('Release manifest contains an unapproved member')
        relative = row['path']
        if relative in selected:
            raise ValueError('Duplicate release member')
        selected.add(relative)
        path = ordinary(release / relative)
        raw = path.read_bytes(); total += len(raw)
        if len(raw) != row['bytes'] or sha(raw) != row['sha256']:
            raise RuntimeError('Immutable release member changed: ' + relative)
        if total > MAX_SOURCE_BYTES:
            raise ValueError('Release exceeds source byte limit')
    found = set()
    for path in release.rglob('*'):
        if path.is_symlink():
            raise ValueError('Release symlink is forbidden')
        if path.is_file():
            ordinary(path)
            found.add(path.relative_to(release).as_posix())
        elif not path.is_dir():
            raise ValueError('Release contains a nonregular member')
    if found != selected | {'manifest.json'}:
        raise RuntimeError('Immutable release member set changed')
    if sha(encoded(rows)) != manifest.get('source_sha256') or total != manifest.get('bytes'):
        raise RuntimeError('Immutable source inventory changed')
    if manifest['version'] != version_from_source((release / SOURCE_TREE / '__init__.py').read_bytes()):
        raise RuntimeError('Release version differs from its actual source')
    return manifest


def build_release(source_root, release_root):
    source_root, paths = source_files(source_root)
    destination = ordinary(release_root, directory=True)
    if destination.is_relative_to(source_root):
        raise ValueError('Release storage must be outside the source tree')
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    contents = {}; captured_bytes = 0
    for path in paths:
        if path.stat().st_size + captured_bytes > MAX_SOURCE_BYTES:
            raise ValueError('Source inventory exceeds release byte bounds')
        raw = path.read_bytes(); captured_bytes += len(raw)
        if captured_bytes > MAX_SOURCE_BYTES:
            raise ValueError('Source inventory changed beyond release byte bounds')
        contents[path.relative_to(source_root).as_posix()] = raw
    rows = [{'path': name, 'bytes': len(raw), 'sha256': sha(raw)}
            for name, raw in sorted(contents.items())]
    total = sum(row['bytes'] for row in rows)
    if len(rows) > 4096 or total > MAX_SOURCE_BYTES:
        raise ValueError('Source inventory exceeds release bounds')
    for relative, raw in contents.items():
        if not allowed_member(relative):
            raise ValueError('Unapproved release source')
        if relative.endswith('.py'):
            compile(raw, relative, 'exec')
    identity = {'schema': SCHEMA, 'software_id': SOFTWARE_ID,
                'version': version_from_source(contents[(SOURCE_TREE / '__init__.py').as_posix()]),
                'git': git_identity(source_root), 'files': rows, 'source_sha256': sha(encoded(rows)),
                'bytes': total, 'activation': 'not_performed'}
    release_sha = sha(encoded(identity))
    release_id = 'paper-studio-' + identity['version'] + '-' + release_sha[:16]
    manifest = {**identity, 'release_sha256': release_sha, 'release_id': release_id}
    target = destination / release_id
    # Coordinate concurrent publishers only in the selected release store.
    lock_path = ordinary(destination / '.paper-release.lock')
    with lock_path.open('a+b') as stream:
        os.chmod(lock_path, 0o600)
        fcntl.flock(stream, fcntl.LOCK_EX)
        if target.exists():
            if verify_release(target, release_sha) != manifest:
                raise RuntimeError('Existing immutable release differs')
            return {'release_id': release_id, 'release_sha256': release_sha,
                    'path': str(target), 'status': 'reused', 'manifest': manifest}
        # Refuse a mixed source capture rather than publishing changing code.
        _, current_paths = source_files(source_root)
        if set(current_paths) != set(paths) or any(path.read_bytes() != contents[path.relative_to(source_root).as_posix()]
                                                 for path in paths):
            raise RuntimeError('Source changed during release capture; wait for code freeze')
        stage = destination / ('.stage-' + uuid.uuid4().hex)
        stage.mkdir(mode=0o700)
        try:
            for relative, raw in contents.items():
                path = stage / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open('xb') as output:
                    output.write(raw)
                path.chmod(0o400)
            path = stage / 'manifest.json'
            path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + '\n', encoding='utf-8')
            path.chmod(0o400)
            stage.rename(target)
            verify_release(target, release_sha)
        except BaseException:
            # Only our unpublished staging may be removed; historical releases remain.
            if stage.exists():
                shutil.rmtree(stage)
            raise
    return {'release_id': release_id, 'release_sha256': release_sha,
            'path': str(target), 'status': 'created', 'manifest': manifest}


def deployment_plan(release_path, instance_id, config_path, *, gateway_mode='direct'):
    manifest = verify_release(release_path)
    if not isinstance(instance_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', instance_id):
        raise ValueError('Explicit instance identity required')
    if gateway_mode not in {'direct', 'extension'}:
        raise ValueError('Known Gateway integration mode required')
    config_path = ordinary(config_path)
    if config_path.stat().st_size > 1024 * 1024:
        raise ValueError('Instance configuration exceeds plan limit')
    config_raw = config_path.read_bytes()
    config = json.loads(config_raw)
    required = ('instance_id', 'client_id', 'project_id', 'native_project', 'project_root')
    if any(not isinstance(config.get(k), str) or not config[k] for k in required):
        raise ValueError('Existing instance/project identity required')
    if config['instance_id'] != instance_id:
        raise ValueError('Selected instance differs from configuration')
    release = ordinary(release_path, directory=True)
    return {'schema': 'paper-studio.deployment-plan.v1', 'activation': 'not_performed',
            'instance': {k: config[k] for k in required},
            'release_id': manifest['release_id'], 'release_sha256': manifest['release_sha256'],
            'source_sha256': manifest['source_sha256'], 'version': manifest['version'], 'git': manifest['git'],
            'gateway_mode': gateway_mode,
            'before': {'config_path': str(config_path), 'config_sha256': sha(config_raw)},
            'entrypoints': {'gateway': str(release / SOURCE_TREE / 'gateway/main.py'),
                           'provider': str(release / SOURCE_TREE / 'provider/main.py'),
                           'paper_studio_import_root': str(release / 'paper-studio'),
                           'management_import_root': str(release)},
            'allowed_config_update': {'extension_version': manifest['version']},
            'preserve': ['Runner client_id', 'Native project IDs', 'project roots and permissions',
                         'Tunnel ID/profile/key reference', 'credential files', 'artifact/history identities'],
            'preconditions': ['Verify this immutable release before activation',
                              'Read actual active builds/edits/publications and wait for terminal state',
                              'Save private reversible routing/config state without exporting credentials',
                              'Use the existing single instance service/Tunnel maintenance path'],
            'rollback': {'config_path': str(config_path), 'expected_prior_config_sha256': sha(config_raw),
                         'requires_saved_prior_routing': True,
                         'scope': 'Only changes applied to this instance; preserve subsequent unrelated edits'},
            'secrets_copied': False, 'services_changed': False, 'tunnels_changed': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    command = sub.add_parser('build')
    command.add_argument('--source-root', type=Path, default=ROOT)
    command.add_argument('--release-root', type=Path, required=True)
    command = sub.add_parser('verify')
    command.add_argument('--release-path', type=Path, required=True)
    command.add_argument('--expected-sha256')
    command = sub.add_parser('plan')
    command.add_argument('--release-path', type=Path, required=True)
    command.add_argument('--instance-id', required=True)
    command.add_argument('--config', type=Path, required=True)
    command.add_argument('--gateway-mode', choices=['direct', 'extension'], default='direct')
    command.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.action == 'build':
        result = build_release(args.source_root, args.release_root)
    elif args.action == 'verify':
        result = verify_release(args.release_path, args.expected_sha256)
    else:
        result = deployment_plan(args.release_path, args.instance_id, args.config, gateway_mode=args.gateway_mode)
        output = ordinary(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open('x', encoding='utf-8') as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
