"""Pure manifest comparison for append-only project mirrors.

Inputs are already selected manifests, not directory listings or permission.
No timestamps decide winners, and no function here reads or writes local/cloud
bytes. A remote-only record is a candidate for a separately authorized import.
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath
from paper_studio.security.policy import fail, relpath


GROUPS = ('matched', 'local_only', 'remote_only', 'diverged', 'excluded')


def _path(value):
    if (not isinstance(value, str) or not value or '\\' in value or '\x00' in value
            or PurePosixPath(value).is_absolute() or PurePosixPath(value).as_posix() != value
            or any(part in {'.', '..'} for part in value.split('/'))):
        fail('MANIFEST_INVALID', 'Manifest paths must be canonical project-relative paths')
    return value


def _exclusions(manifests, paths):
    if not isinstance(paths,(list,tuple,set,frozenset)) or len(paths)>4096:
        fail('MANIFEST_INVALID', 'Explicit excluded paths require a bounded sequence')
    result = set(_path(value) for value in paths)
    for manifest in manifests:
        rows=manifest.get('excluded', [])
        if not isinstance(rows,list) or len(rows)>4096:
            fail('MANIFEST_INVALID', 'Manifest exclusions require a bounded list')
        for row in rows:
            value = row.get('path') if isinstance(row, dict) else row
            result.add(_path(value))
    return result


def _index(manifest, remote, excluded):
    rows = manifest.get('files')
    if not isinstance(rows, list) or len(rows) > 4096:
        fail('MANIFEST_INVALID', 'A bounded files list is required')
    result = {}
    for row in rows:
        if not isinstance(row, dict):
            fail('MANIFEST_INVALID', 'Manifest file records must be objects')
        path = _path(row.get('source_path', row.get('path')))
        if path in result:
            fail('MANIFEST_AMBIGUOUS', 'One selected manifest cannot contain duplicate paths', path=path)
        if not any(PurePosixPath(path).is_relative_to(value) for value in excluded):
            relpath(path)
        digest, size = row.get('sha256'), row.get('bytes')
        if (not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest)
                or type(size) is not int or size < 0):
            fail('MANIFEST_INVALID', 'Every selected file requires byte size and SHA256', path=path)
        value = {'path': path, 'bytes': size, 'sha256': digest}
        if remote:
            identity = row.get('id', row.get('remote_file_id'))
            if not isinstance(identity, str) or not re.fullmatch('[A-Za-z0-9_-]{1,200}', identity):
                fail('MANIFEST_INVALID', 'Remote records require an exact Drive file ID', path=path)
            value['id'] = identity
            for key in ('parent', 'remote_relative_path', 'role'):
                if key in row:
                    value[key] = row[key]
        result[path] = value
    return result


def reconcile_manifests(local, remote, *, project_id=None, excluded_paths=()):
    """Classify selected paths by identity; preserve every divergent version."""
    if not isinstance(local, dict) or not isinstance(remote, dict):
        fail('MANIFEST_INVALID', 'Local and remote manifests must be objects')
    identity = project_id or local.get('project_id')
    if not isinstance(identity, str) or not identity or any(m.get('project_id') != identity for m in (local, remote)):
        fail('PROJECT_DENIED', 'Both manifests must explicitly identify the selected project')
    excluded = _exclusions((local, remote), excluded_paths)
    left, right = _index(local, False, excluded), _index(remote, True, excluded)
    result = {key: [] for key in GROUPS}
    decisions = {'matched': 'reuse', 'local_only': 'upload_candidate',
                 'remote_only': 'import_candidate_after_role_and_scope_verification',
                 'diverged': 'preserve_both_and_report_conflict', 'excluded': 'do_not_mirror'}
    for path in sorted(set(left) | set(right)):
        a, b = left.get(path), right.get(path)
        if any(PurePosixPath(path).is_relative_to(value) for value in excluded):
            group = 'excluded'
        elif a is None:
            group = 'remote_only'
        elif b is None:
            group = 'local_only'
        elif (a['sha256'], a['bytes']) == (b['sha256'], b['bytes']):
            group = 'matched'
        else:
            group = 'diverged'
        result[group].append({'path': path, 'classification': group, 'decision': decisions[group],
                              'local': a, 'remote': b})
    return {'schema': 'paper-editor.manifest-reconciliation.v1', 'project_id': identity,
            **result, 'counts': {key: len(result[key]) for key in GROUPS},
            'exclusion_rules': sorted(excluded),
            'local_manifest_files': len(left), 'remote_manifest_files': len(right),
            'mutations_performed': False,
            'limitations': ['Only the selected manifest coverage is compared; this is not an inventory of all project assets.',
                            'SHA values are supplied manifest identities; actual remote metadata/bytes must be verified separately.',
                            'Remote-only files are never automatically allowed to overwrite local manuscript sources.']}
