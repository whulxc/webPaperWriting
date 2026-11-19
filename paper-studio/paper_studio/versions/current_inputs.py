"""Check a selected build's frozen inputs before a new formal publication.

Historical archives remain readable. New publication, promotion and activation
must bind their exact frozen dependencies to the selected current manuscript.
"""
import json

from paper_studio.security.policy import StudioError, encoded, fail, relpath, safe_read, sha


def _document(root, path):
    try:
        value = json.loads(safe_read(root, path, 8 * 1024**2))
    except (ValueError, RecursionError):
        fail('BUILD_PROOF_INVALID', 'Publication evidence is not valid JSON', path=path)
    if not isinstance(value, dict):
        fail('BUILD_PROOF_INVALID', 'Publication evidence must be an object', path=path)
    return value


def _changed(path):
    fail('BUILD_PROOF_SOURCE_CHANGED', 'Current dependency differs from the successful build', path=path)


def _read_current(root, path, budget):
    try:
        return safe_read(root, path, budget)
    except StudioError as error:
        if error.code == 'NOT_FOUND':
            _changed(path)
        raise


def _managed_inputs(cfg, record, *, check_current=True):
    from paper_studio.versions import lifecycle
    request = record['request']
    binding = request['_version_binding']
    dest, current, revision = lifecycle._revision(cfg, binding.get('version'), binding.get('revision'))
    language = binding.get('language')
    if language not in lifecycle.LANGUAGES:
        fail('BUILD_PROOF_INVALID', 'Managed build language is not bound')
    if check_current and (current['current_revision'] != binding['revision']
            or current.get('selected_figures') != {key:value['candidate'] for key,value in revision['figures'].items()}
            or current.get('selected_dependencies', {}) != revision.get('dependency_selections', {})):
        fail('BUILD_PROOF_SOURCE_CHANGED', 'Selected managed revision or dependencies changed')
    manifest = safe_read(dest, 'revisions/' + binding['revision'] + '/manifest.json', 8 * 1024**2)
    if sha(manifest) != binding.get('manifest_sha256'):
        fail('BUILD_PROOF_SOURCE_CHANGED', 'Selected managed revision manifest changed')
    selected = revision['languages'][language]
    if selected['entry_path'] != request.get('tex_path'):
        fail('BUILD_PROOF_INVALID', 'Build does not identify the selected managed language')
    return dest, language, selected['inputs']


def _verify_record_identities(cfg, record, manifest, status):
    if not record.get('schema') or record.get('kind') not in {'tex','figure','pptx'}:
        fail('BUILD_PROOF_REQUIRED', 'Publication requires a source-bound successful build; rebuild this artifact')
    if (record.get('schema') != 'paper-studio.build-request.v1'
            or record.get('project_id') != cfg['project_id'] or not isinstance(manifest,dict)
            or manifest.get('schema') != 'paper-studio.build-manifest.v1'
            or manifest.get('project_id') != cfg['project_id']
            or any(not isinstance(record.get(key),str) or not record[key]
                   for key in ('build_id','request_sha256','config_revision','code_sha256'))
            or any(manifest.get(key) != record.get(key) for key in ('build_id','kind','request_sha256','config_revision','code_sha256'))
            or manifest.get('input') != record.get('snapshot')):
        fail('BUILD_PROOF_INVALID', 'Publication build evidence identities differ')
    request = record.get('request', {})
    if record.get('kind') == 'tex':
        if not isinstance(request.get('tex_path'),str) or not request['tex_path']:
            fail('BUILD_PROOF_REQUIRED', 'A formal manuscript requires its exact TeX entry')
        raw = record.get('raw_request')
        if (not isinstance(raw,dict)
                or record.get('request_sha256') != sha(encoded({'kind':'tex','request':raw,'project_id':cfg['project_id']}))
                or record.get('prepared_request_sha256') != sha(encoded(request))):
            fail('BUILD_PROOF_INVALID', 'Publication TeX request identity changed')
        result = manifest.get('result', {})
        binding = result.get('source_binding', {})
        snapshot = record.get('snapshot', {})
        if (relpath(request['tex_path']).suffix != '.tex'
                or request.get('_input_manifest_sha256') != snapshot.get('sha256')
                or binding.get('dependency_plan_bound', True) is not True):
            fail('BUILD_PROOF_REQUIRED', 'Publication requires the trusted complete manuscript dependency plan')
        expected = {'tex_path':request.get('tex_path'),'main_sha256':request.get('expected_revision'),
                    'dependencies_sha256':request.get('_input_manifest_sha256'),
                    'snapshot_sha256':snapshot.get('sha256'),'dependencies':snapshot.get('files')}
        if any(binding.get(key) != value for key,value in expected.items()):
            fail('BUILD_PROOF_INVALID', 'Publication source binding differs from its input inventory')
        execution = result.get('execution', {})
        isolation = execution.get('isolation', {})
        pdf = next((row for row in manifest.get('artifacts',[]) if row.get('path') == result.get('pdf_path')),None)
        if (result.get('status') != 'completed' or execution.get('status') != 'completed'
                or execution.get('program') != 'latex' or execution.get('returncode') != 0
                or isolation.get('input') != 'read-only' or isolation.get('host_fallback') is not False
                or not pdf or pdf.get('mime_type') != 'application/pdf'
                or pdf.get('sha256') != result.get('pdf_sha256')):
            fail('BUILD_PROOF_REQUIRED', 'A successful isolated TeX execution and bound primary PDF are required')
    else:
        raw=record.get('raw_request')
        candidates=[raw] if isinstance(raw,dict) else [request,{key:value for key,value in request.items() if key!='input_paths'}]
        if not any(record.get('request_sha256') == sha(encoded({'kind':record['kind'],'request':candidate,'project_id':cfg['project_id']}))
                   for candidate in candidates):
            fail('BUILD_PROOF_REQUIRED', 'Auxiliary publication request cannot be bound to its generation')
        if record.get('prepared_request_sha256') is not None and record['prepared_request_sha256'] != sha(encoded(request)):
            fail('BUILD_PROOF_INVALID', 'Prepared auxiliary generation request changed')
    if status is not None and (status.get('project_id') != cfg['project_id']
            or status.get('build_id') != record.get('build_id') or status.get('generated') is not True
            or status.get('artifacts') != manifest.get('artifacts') or status.get('result') != manifest.get('result')):
        fail('BUILD_PROOF_INVALID', 'Completed publication build manifest or outputs changed')


def _verify_auxiliary_output(record,manifest,output_root):
    request=record['request'];result=manifest['result'];kind=record['kind']
    artifacts={row['path']:row for row in manifest['artifacts']}
    if result.get('status')!='completed':
        fail('BUILD_PROOF_REQUIRED','Auxiliary output has no completed generation')
    for path,row in artifacts.items():
        raw=safe_read(output_root,path,64*1024**2)
        if len(raw)!=row['bytes'] or sha(raw)!=row['sha256']:
            fail('BUILD_PROOF_INVALID','Auxiliary output differs from its completed build',path=path)
    if kind=='figure':
        if (not isinstance(request.get('source_path'),str) or not request['source_path']
                or not result.get('artifacts') or result.get('engine') != request.get('engine','python')):
            fail('BUILD_PROOF_REQUIRED','Figure publication requires its source and generated output identities')
        execution=result.get('execution',{});isolation=execution.get('isolation',{})
        if (execution.get('status')!='completed' or execution.get('program')!='figure' or execution.get('returncode')!=0
                or isolation.get('input')!='read-only' or isolation.get('host_fallback') is not False):
            fail('BUILD_PROOF_REQUIRED','A figure needs its successful isolated generation receipt')
        source_manifest=_document(output_root,'source_manifest.json')
        if source_manifest!={row['path']:row['sha256'] for row in record['snapshot']['files']}:
            fail('BUILD_PROOF_INVALID','Figure execution used another input inventory')
        executed=_document(output_root,'request.json')
        expected={key:value for key,value in request.items() if key not in {'request_id','expected_revision','input_paths'}}
        expected.setdefault('engine','python');expected.setdefault('data_paths',[])
        if executed!=expected:
            fail('BUILD_PROOF_INVALID','Figure execution request differs from its build')
        for row in result.get('artifacts',[]):
            if artifacts.get(row.get('path'),{}).get('sha256')!=row.get('sha256'):
                fail('BUILD_PROOF_INVALID','Figure result does not bind its generated outputs')
    else:
        receipt=_document(output_root,'PPTX_RECEIPT.json')
        inputs={row['path']:row for row in record['snapshot']['files']}
        if (receipt!=result or receipt.get('schema')!='paper-studio.pptx.build.v1'
                or receipt.get('action')!=request.get('action') or receipt.get('source_unchanged') is not True):
            fail('BUILD_PROOF_REQUIRED','PPTX output needs its complete generation receipt')
        source=request.get('pptx_path')
        if request.get('action') not in {'create','edit','export'} or (request['action']!='create' and not source):
            fail('BUILD_PROOF_REQUIRED','PPTX edit/export publication requires its source package')
        if source and receipt.get('source_sha256')!=inputs.get(source,{}).get('sha256'):
            fail('BUILD_PROOF_INVALID','PPTX source identity differs from the frozen input')
        if not receipt.get('artifacts'):
            fail('BUILD_PROOF_REQUIRED','PPTX receipt has no bound generated artifact')
        for row in receipt['artifacts']:
            bound=artifacts.get(row.get('relative_path'),{})
            if bound.get('sha256')!=row.get('sha256') or bound.get('bytes')!=row.get('bytes'):
                fail('BUILD_PROOF_INVALID','PPTX result does not bind its generated outputs')


def verify_record_current(cfg, record, source_root, *, build_manifest=None, status=None, check_current=True):
    """Validate every frozen input and its current selected source by exact path."""
    request = record.get('request', {})
    if not isinstance(request, dict):
        fail('BUILD_PROOF_REQUIRED', 'A source-bound build request is required for publication')
    if request.get('_text_review') or str(request.get('tex_path', '')).endswith('.review.tex'):
        fail('BUILD_PROOF_REVIEW_ONLY', 'A marked-up review cannot establish a formal manuscript build')
    _verify_record_identities(cfg, record, build_manifest, status)
    if record['kind']=='tex':
        result = build_manifest['result']
        artifact = next(row for row in build_manifest['artifacts'] if row['path'] == result['pdf_path'])
        raw_pdf = safe_read(source_root.parent/'output', result['pdf_path'],
                            cfg.get('limits',{}).get('max_output_bytes',64*1024**2))
        if (not raw_pdf.startswith(b'%PDF-') or len(raw_pdf) != artifact.get('bytes')
                or sha(raw_pdf) != artifact.get('sha256')):
            fail('BUILD_PROOF_INVALID', 'Primary publication PDF differs from its successful build')
    else:
        _verify_auxiliary_output(record,build_manifest,source_root.parent/'output')
    snapshot = record.get('snapshot')
    rows = snapshot.get('files') if isinstance(snapshot, dict) else None
    if not isinstance(rows, list) or not rows or len(rows) > cfg.get('limits', {}).get('max_files', 2048):
        fail('BUILD_PROOF_REQUIRED', 'Publication requires the complete frozen input inventory; rebuild this manuscript')
    if snapshot.get('sha256') != sha(encoded(rows)) or record.get('snapshot_complete') is not True:
        fail('SNAPSHOT_TAMPERED', 'Publication input inventory changed')
    budget = cfg.get('limits', {}).get('max_snapshot_bytes', 32 * 1024**2)
    managed = _managed_inputs(cfg, record, check_current=check_current) if request.get('_version_binding') else None
    seen = set()
    total = 0
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'path', 'bytes', 'sha256'}:
            fail('BUILD_PROOF_INVALID', 'Invalid frozen input identity')
        path = row['path']; relpath(path)
        if path in seen or type(row['bytes']) is not int or row['bytes'] < 0:
            fail('BUILD_PROOF_INVALID', 'Duplicate or invalid frozen input')
        seen.add(path)
        try:
            frozen = safe_read(source_root, path, budget)
        except StudioError as error:
            if error.code == 'NOT_FOUND':
                fail('SNAPSHOT_TAMPERED', 'Publication frozen input is missing', path=path)
            raise
        if len(frozen) != row['bytes'] or sha(frozen) != row['sha256']:
            fail('SNAPSHOT_TAMPERED', 'Publication input bytes changed', path=path)
        total += len(frozen)
        if total > budget:
            fail('INPUT_LIMIT', 'Publication inputs exceed the configured byte budget')
        if managed:
            dest, language, references = managed
            reference = references.get(path)
            if reference is None:
                fail('BUILD_PROOF_INVALID', 'Frozen input is absent from the selected managed revision', path=path)
            from paper_studio.versions.lifecycle import _bytes
            current = _bytes(cfg, reference)
            working = 'working/main_' + language + '.tex' if path == request['tex_path'] else 'working/deps/' + path
            if check_current and (dest / working).exists() and _read_current(dest, working, budget) != frozen:
                _changed(str(dest.relative_to(cfg['project_root']) / working))
        else:
            current = _read_current(cfg['project_root'], path, budget) if check_current else frozen
        if current != frozen:
            _changed(path)
    if managed and seen != set(managed[2]):
        fail('BUILD_PROOF_INVALID', 'Managed build omits selected revision dependencies')
    main = request.get('tex_path') if record['kind']=='tex' else request.get('source_path',request.get('pptx_path'))
    if (snapshot.get('bytes') != total or main and (main not in seen
            or request.get('expected_revision') != sha(safe_read(source_root, main, budget)))):
        fail('BUILD_PROOF_INVALID', 'Main manuscript identity differs from the frozen inputs')
    return {'kind':record['kind'],'formal_manuscript':record['kind']=='tex',
            'input_count': len(rows), 'input_sha256': sha(encoded(rows)), 'current_inputs_verified': check_current}


def verify_build_current(cfg, build_id):
    return _verify_build(cfg, build_id, check_current=True)


def verify_build_frozen(cfg, build_id):
    """Read a historical review's original source-bound PDF without selecting it anew."""
    return _verify_build(cfg, build_id, check_current=False)


def _verify_build(cfg, build_id, *, check_current):
    from paper_studio.provider.jobs import job_path, read_status
    job = job_path(cfg, build_id)
    state = read_status(cfg, build_id)
    if state['status'] != 'completed':
        fail('BUILD_NOT_COMPLETE', 'Formal publication requires a completed build', build_id=build_id)
    record = _document(job, 'request.json')
    manifest = _document(job, 'manifest.json') if record.get('schema') else None
    if manifest is not None and state.get('manifest_sha256') != sha(safe_read(job,'manifest.json',8*1024**2)):
        fail('BUILD_PROOF_INVALID', 'Publication build manifest bytes changed')
    return verify_record_current(cfg, record, job / 'source', build_manifest=manifest, status=state,
                                 check_current=check_current)


def verify_version_current(cfg, label):
    """Verify a new upload/activation against exact members of one saved archive."""
    from paper_studio.versions.history import verify_version
    archive, manifest = verify_version(cfg, label)
    evidence = []
    if not manifest.get('build_ids'):
        fail('BUILD_PROOF_REQUIRED', 'Publication archive has no source-bound build')
    for build_id in manifest['build_ids']:
        prefix = 'builds/' + build_id + '/'
        try:
            record = _document(archive, prefix + 'request.json')
        except StudioError as error:
            if error.code == 'NOT_FOUND':
                fail('BUILD_PROOF_REQUIRED', 'Historical output has no source-bound build request; rebuild before publication')
            raise
        if record.get('snapshot') is None:
            fail('BUILD_PROOF_REQUIRED', 'Recorded build has no complete input inventory')
        build_manifest = _document(archive, prefix+'manifest.json') if record.get('schema') else None
        evidence.append(verify_record_current(cfg, record, archive / prefix / 'source',
                                              build_manifest=build_manifest))
    return evidence
