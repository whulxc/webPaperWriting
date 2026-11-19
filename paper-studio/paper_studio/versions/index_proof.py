"""Attest selected manuscript bytes against a completed immutable native job.

This module never accepts caller-supplied build evidence. It reopens the exact
project job, its frozen inputs and main PDF, and checks current project paths.
Publication repeats these checks. A PDF registration alone is not attestation.
"""
from __future__ import annotations

import json
import time

from paper_studio.security.policy import encoded, fail, relpath, safe_read, sha
from paper_studio.provider.jobs import job_path

SCHEMA = 'paper-studio.index-build-proof.v1'
INPUT_ROLES = frozenset({'manuscript_tex', 'figure_pdf', 'table_pdf', 'section_tex', 'bibliography', 'named_source'})


def _json(root, name, budget=8*1024*1024):
    raw = safe_read(root, name, budget)
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError):
        fail('BUILD_PROOF_INVALID', 'Native build evidence is not valid JSON')
    if not isinstance(value, dict):
        fail('BUILD_PROOF_INVALID', 'Native build evidence must be an object')
    return value, sha(raw)


def _require(condition, message, **details):
    if not condition:
        fail('BUILD_PROOF_INVALID', message, **details)


def verify_native(cfg, build_id, *, original_paths=None):
    """Return verified frozen input rows and output identity; no history scans."""
    job = job_path(cfg, build_id)
    record, request_document_sha = _json(job, 'request.json')
    status, _ = _json(job, 'status.json')
    manifest, manifest_sha = _json(job, 'manifest.json')
    for value, schema in ((record,'paper-studio.build-request.v1'), (manifest,'paper-studio.build-manifest.v1')):
        _require(value.get('schema') == schema and value.get('project_id') == cfg['project_id']
                 and value.get('build_id') == build_id and value.get('kind') == 'tex',
                 'The selected job is not a native TeX build for this project')
    _require(status.get('project_id') == cfg['project_id'] and status.get('build_id') == build_id
             and status.get('status') == 'completed' and status.get('generated') is True,
             'A successful native completed job is required')
    _require(status.get('manifest_sha256') == manifest_sha, 'The immutable native manifest changed')
    request = record.get('request', {})
    if request.get('_text_review') or str(request.get('tex_path','')).endswith('.review.tex'):
        fail('BUILD_PROOF_REVIEW_ONLY','A marked-up text review cannot establish a formal manuscript build')
    raw_request = record.get('raw_request')
    _require(isinstance(raw_request,dict) and record.get('request_sha256') == sha(encoded({
        'kind':'tex','request':raw_request,'project_id':cfg['project_id']})), 'Original native request identity changed')
    _require(record.get('prepared_request_sha256') == sha(encoded(request)), 'Prepared native request changed')
    for field in ('request_sha256','config_revision','code_sha256'):
        _require(manifest.get(field) == record.get(field) and isinstance(record.get(field), str),
                 'Native evidence identities differ', field=field)
    snapshot = record.get('snapshot', {})
    rows = snapshot.get('files')
    _require(record.get('snapshot_complete') is True and isinstance(rows,list) and rows
             and len(rows) <= cfg.get('limits',{}).get('max_files',2048), 'A bounded complete frozen input inventory is required')
    _require(snapshot == manifest.get('input') and snapshot.get('sha256') == sha(encoded(rows))
             and request.get('_input_manifest_sha256') == snapshot['sha256']
             and status.get('snapshot_sha256') == snapshot['sha256'], 'Native frozen input identity changed')
    inputs = {}
    total = 0
    budget = cfg.get('limits',{}).get('max_snapshot_bytes',32*1024*1024)
    for row in rows:
        _require(isinstance(row,dict) and set(row) == {'path','bytes','sha256'}, 'Invalid frozen input row')
        path = row['path']
        relpath(path)
        _require(path not in inputs and type(row['bytes']) is int and row['bytes'] >= 0,
                 'Duplicate or invalid frozen input path')
        frozen = safe_read(job/'source', path, budget)
        _require(len(frozen) == row['bytes'] and sha(frozen) == row['sha256'], 'Frozen source bytes changed', path=path)
        # This also covers unindexed references/chapters/dependencies. The
        # caller cannot omit a binding to hide an external project-file edit.
        current = safe_read(cfg['project_root'], path, budget)
        if len(current) != row['bytes'] or sha(current) != row['sha256']:
            fail('BUILD_PROOF_SOURCE_CHANGED', 'Current dependency differs from the successful build', path=path)
        total += len(frozen)
        _require(total <= budget, 'Native frozen inputs exceed the configured proof budget')
        inputs[path] = row
    _require(snapshot.get('bytes') == total, 'Frozen input total changed')
    main = request.get('tex_path')
    _require(main in inputs and inputs[main]['sha256'] == request.get('expected_revision'), 'Main source identity changed')
    result = manifest.get('result', {})
    binding = result.get('source_binding', {})
    expected_binding = {'tex_path':main,'main_sha256':request['expected_revision'],
                        'dependencies_sha256':request.get('_input_manifest_sha256'),
                        'snapshot_sha256':snapshot['sha256'],'dependencies':rows}
    if '_review_binding' in request:
        expected_binding['review_binding'] = request['_review_binding']
    _require(all(binding.get(key) == value for key,value in expected_binding.items())
             and binding.get('dependency_plan_bound',True) is True,
             'Native source binding differs from the complete frozen inputs')
    execution = result.get('execution', {})
    isolation = execution.get('isolation', {})
    _require(result.get('status') == 'completed' and execution.get('status') == 'completed'
             and execution.get('program') == 'latex' and execution.get('returncode') == 0
             and isolation.get('input') == 'read-only' and isolation.get('host_fallback') is False,
             'A successful isolated native LaTeX execution is required')
    artifacts = manifest.get('artifacts')
    _require(isinstance(artifacts,list) and artifacts == status.get('artifacts') and result == status.get('result'),
             'Completed native outputs differ from the immutable manifest')
    output_budget = cfg.get('limits',{}).get('max_output_bytes',64*1024*1024)
    output_total = 0
    output_paths = set()
    for artifact in artifacts:
        _require(isinstance(artifact,dict) and isinstance(artifact.get('path'),str), 'Invalid native artifact row')
        path = artifact['path']
        relpath(path)
        _require(path not in output_paths, 'Duplicate native output path')
        output_paths.add(path)
        raw = safe_read(job/'output',path,output_budget)
        _require(len(raw) == artifact.get('bytes') and sha(raw) == artifact.get('sha256'),
                 'Immutable native output changed', path=path)
        output_total += len(raw)
        _require(output_total <= output_budget, 'Native outputs exceed the proof byte budget')
    pdf_path = result.get('pdf_path')
    relpath(pdf_path)
    pdf = next((row for row in artifacts if row.get('path') == pdf_path), None)
    _require(pdf is not None and pdf.get('sha256') == result.get('pdf_sha256'), 'Main native PDF is not an indexed output')
    output = safe_read(job/'output',pdf_path,output_budget)
    _require(output.startswith(b'%PDF-') and len(output) == pdf['bytes'] and sha(output) == pdf['sha256'],
             'Main native PDF bytes changed')
    mapping = {}
    if request.get('_review_binding') is not None:
        from paper_studio.versions.review_proposals import approved_build_mapping
        mapping = approved_build_mapping(cfg,main,expected_binding=request['_review_binding'],
                                         original_paths=original_paths or [])
        _require(isinstance(mapping,dict), 'Invalid protected review export mapping')
        for original, physical in mapping.items():
            relpath(original)
            relpath(physical)
    return {'build_id':build_id,'request_document_sha256':request_document_sha,
            'manifest_sha256':manifest_sha,'snapshot_sha256':snapshot['sha256'],
            'main_path':main,'inputs':inputs,'pdf':pdf,'code_sha256':record['code_sha256'],
            'config_revision':record['config_revision'],'approved_mapping':mapping}


def attest(cfg, selections, language, build_id, *, require_selected_pdf=False):
    from paper_studio.versions.target_index import _key
    if language not in {'en','zh'}:
        fail('INDEX_ARGUMENT', 'Build proof requires EN or ZH')
    relevant = [(key,entry) for key,entry in selections.items()
                if entry['role'] in INPUT_ROLES and entry['language'] in {language,'shared'}]
    native = verify_native(cfg, build_id,original_paths=[entry.get('project_path') for _,entry in relevant
                                                      if isinstance(entry.get('project_path'),str)])
    pdf = selections.get(_key({'role':'manuscript_pdf','language':language}))
    if require_selected_pdf and (not pdf or not pdf['current_for_revision'] or any(pdf.get(key) != native['pdf'][key] for key in ('sha256','bytes'))):
        fail('BUILD_PROOF_PDF_MISMATCH', 'Selected manuscript PDF is not the successful native output')
    bound = []
    for key, entry in relevant:
        path = entry.get('project_path')
        if not isinstance(path,str):
            fail('BUILD_PROOF_PATH_REQUIRED', 'Selected source has no exact project path; capture it in a new working revision', selector=key)
        build_path = native['approved_mapping'].get(path,path)
        row = native['inputs'].get(build_path)
        if row is None or any(entry.get(field) != row[field] for field in ('sha256','bytes')):
            fail('BUILD_PROOF_SELECTION_CHANGED', 'A selected source is not the exact frozen native input', selector=key, path=path)
        if entry['role'] == 'manuscript_tex' and build_path != native['main_path']:
            fail('BUILD_PROOF_SELECTION_CHANGED', 'Selected main source must match the native entry path')
        bound.append({'selector':key,'path':path,'build_path':build_path,'sha256':entry['sha256'],'bytes':entry['bytes']})
    main_key = _key({'role':'manuscript_tex','language':language})
    if not any(item['selector'] == main_key for item in bound):
        fail('BUILD_PROOF_PATH_REQUIRED', 'The selected native main TeX must be registered')
    bound.sort(key=lambda item:item['selector'])
    return {'schema':SCHEMA,'project_id':cfg['project_id'],'language':language,
            'build_id':build_id,'build_manifest_sha256':native['manifest_sha256'],
            'request_document_sha256':native['request_document_sha256'],
            'snapshot_sha256':native['snapshot_sha256'],'pdf_sha256':native['pdf']['sha256'],'pdf_bytes':native['pdf']['bytes'],
            'pdf_artifact_path':native['pdf']['path'],
            'selected_inputs':bound,'selection_sha256':sha(encoded(bound)),
            'dependency_count':len(native['inputs']),'dependencies':list(native['inputs'].values()),
            'verified_at':time.time(),'generation_verified':True}
