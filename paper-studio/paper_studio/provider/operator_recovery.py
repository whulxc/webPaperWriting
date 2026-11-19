"""Read-only provider verification of retained local operator closure evidence.

Runtime workers can run from the installed paper_studio package without the
management checkout. Keep closure writes and operator commands in management.
"""
from __future__ import annotations
import json
import re
from paper_studio.security.policy import StudioError, safe_read, sha

SCHEMA = 'paper-studio.build-closure.v1'


def verified_operator_closure(cfg, build_id, stored=None):
    """Recognize a hash-bound terminal closure, including legacy retained flags.

    This does not change old status/history. A failed job whose obsolete
    recovery_required flag survived the old closure implementation is terminal
    only when its exact project-scoped closure receipt is intact.
    """
    from paper_studio.provider.jobs import job_path
    job=job_path(cfg,build_id)
    value=stored if stored is not None else json.loads(safe_read(job,'status.json',4*1024*1024))
    reference=value.get('operator_closure')
    if (value.get('project_id')!=cfg['project_id'] or value.get('build_id')!=build_id
            or value.get('status')!='failed' or (value.get('error') or {}).get('code')!='OPERATOR_CLOSED'
            or not isinstance(reference,dict) or reference.get('path')!='operator-closure.json'
            or not isinstance(reference.get('sha256'),str)):
        return False
    try:
        raw=safe_read(job,'operator-closure.json',1024*1024)
        if sha(raw)!=reference['sha256']:return False
        closure=json.loads(raw)
        if (closure.get('schema')!=SCHEMA or closure.get('project_id')!=cfg['project_id']
                or closure.get('build_id')!=build_id or closure.get('outputs_published') is not False
                or closure.get('deleted_files')!=[] or not re.fullmatch(r'[0-9a-f]{64}',closure.get('prior_status_sha256',''))):
            return False
        if closure.get('prior_status_file') is not None:
            if closure['prior_status_file']!='operator-prior-status.json':return False
            if sha(safe_read(job,'operator-prior-status.json',4*1024*1024))!=closure['prior_status_sha256']:return False
        return True
    except (OSError,ValueError,StudioError):
        return False

