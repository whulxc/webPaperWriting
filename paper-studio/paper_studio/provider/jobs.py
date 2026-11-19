"""Research builds over native Plugin v1; rb IDs are not native Job IDs.

Adapted from prior project-scoped idempotency, private snapshots, immutable publication and
worker identity checks. No client-selected commands or host environment.
"""
from __future__ import annotations
import base64,contextlib,errno,hashlib,json,math,mimetypes,os,re,shutil,signal,subprocess,sys,time,traceback,uuid
from pathlib import Path
from paper_studio.security.policy import *
from paper_studio.evidence.index import inventory
from paper_studio.provider import job_index

BUILD_ID=re.compile(r'rb_[a-f0-9]{24}\Z')

def process_identity(pid):
    try:
        value=Path(f'/proc/{pid}/stat').read_text();fields=value[value.rfind(')')+2:].split()
        return {'pid':pid,'start_ticks':fields[19],'state':fields[0]}
    except (FileNotFoundError,ProcessLookupError):return None

def code_identity():
    root=Path(__file__).resolve().parents[1]
    return sha(encoded({p.relative_to(root).as_posix():file_sha(p) for p in sorted(root.rglob('*.py'))}))

def job_path(cfg,build_id):
    if not isinstance(build_id,str) or not BUILD_ID.fullmatch(build_id):fail('INVALID_ARGUMENT','Invalid build_id')
    p=builds_root(cfg)/build_id;no_links(p)
    if not p.is_dir():fail('NOT_FOUND','Unknown build_id for this project')
    return p

def _lifecycle_status(cfg,build_id):
    """Read state and worker identity without opening completed output files."""
    job=job_path(cfg,build_id)
    # A concurrent status replacement has been observed as a transient absence.
    # Bound the retry; persistent loss is an explicit error, never a new state.
    for attempt in range(3):
        try:s=json.loads((job/'status.json').read_text());break
        except OSError as error:
            if not isinstance(error,FileNotFoundError) and error.errno!=errno.ENODATA:raise
            if attempt==2:fail('BUILD_STATE_UNAVAILABLE','Build status remains unavailable after three reads',build_id=build_id)
            time.sleep(.01*(attempt+1))
        except ValueError:
            fail('BUILD_RECORD_CORRUPT','Build status is not valid JSON; local evidence review is required before any recovery',build_id=build_id)
    if not isinstance(s,dict) or not isinstance(s.get('status'),str):
        fail('BUILD_RECORD_CORRUPT','Build status must be an object with a known state',build_id=build_id)
    if s.get('project_id')!=cfg['project_id']:fail('PROJECT_DENIED','Build project mismatch')
    if s.get('build_id',build_id)!=build_id:
        fail('BUILD_RECORD_CORRUPT','Build status identity differs from its directory',build_id=build_id)
    if s['status'] not in ['queued','running','completed','failed','recovery_required']:
        fail('BUILD_RECORD_CORRUPT','Build status contains an unknown state',build_id=build_id)
    # Read-only status: a dead worker is reported without mutating its history.
    if s['status'] in ['queued','running']:
        identity=s.get('worker_identity')
        launch=job/'launch.json'
        if not identity and launch.exists():
            try:identity=json.loads(launch.read_text())
            except (ValueError,OSError):fail('BUILD_RECORD_CORRUPT','Worker launch identity is unavailable or invalid',build_id=build_id)
        if identity is not None and (not isinstance(identity,dict) or type(identity.get('pid')) is not int
                or identity['pid']<=0 or not isinstance(identity.get('start_ticks'),str)):
            fail('BUILD_RECORD_CORRUPT','Worker identity is invalid',build_id=build_id)
        created=s.get('created_at',0)
        if not isinstance(created,(int,float)) or not math.isfinite(created):
            fail('BUILD_RECORD_CORRUPT','Build creation time is invalid',build_id=build_id)
        live=process_identity(identity['pid']) if identity else None
        lost=(identity is not None and (live is None or live['state']=='Z' or live['start_ticks']!=identity['start_ticks']))
        if lost or (not identity and time.time()-created>15):
            s={**s,'status':'recovery_required','error':{'code':'WORKER_LOST','message':'Worker identity is absent or dead; operator recovery required'}}
    return s

def read_status(cfg,build_id,tail_lines=0):
    s=_lifecycle_status(cfg,build_id)
    job=job_path(cfg,build_id)
    if s['status']=='completed':
        for a in s.get('artifacts',[]):
            p=job/'output'/a['path'];no_links(p)
            if not p.is_file() or p.stat().st_nlink!=1 or file_sha(p)!=a['sha256']:fail('ARTIFACT_TAMPERED','Published artifact integrity check failed',path=a['path'])
    if tail_lines:
        if not isinstance(tail_lines,int) or not 0<=tail_lines<=80:fail('INVALID_ARGUMENT','tail_lines must be 0..80')
        p=job/'worker.log';s['log_tail']=p.read_text(errors='replace')[-32768:].splitlines()[-tail_lines:] if p.exists() else []
        if (job/'output/stdout.log').exists():
            raw=safe_read(job/'output','stdout.log',64*1024*1024)
            s['engine_log_tail']=raw[-32768:].decode('utf-8',errors='replace').splitlines()[-tail_lines:]
    return s

def _save_status(cfg,job,value,exclusive=False):
    atomic_json(job/'status.json',value,exclusive)
    job_index.observe(cfg,job.name,value)

def active_jobs(cfg):
    root=builds_root(cfg);out=[]
    if job_index.enabled(cfg):
        for build_id in job_index.active_ids(cfg):
            s=_lifecycle_status(cfg,build_id)
            if s.get('recovery_required') and s['status'] in {'completed','failed'}:
                from paper_studio.provider.operator_recovery import verified_operator_closure
                if verified_operator_closure(cfg,build_id,s):s={**s,'recovery_required':False}
            job_index.observe(cfg,build_id,s)
            if s['status'] in {'queued','running','recovery_required'} or s.get('recovery_required'):
                out.append(s)
        return out
    if root.exists():
        for p in sorted(root.glob('rb_*')):
            if (p/'status.json').exists():
                s=_lifecycle_status(cfg,p.name)
                if s['status'] in ['queued','running','recovery_required']:out.append(s)
    return out

def snapshot_paths(cfg,request):
    # Author explicitly chooses input_paths for large projects. Default captures
    # all bounded, authorized project files, never hidden administrative trees.
    selected=request.get('input_paths')
    managed=[PurePosixPath(root)/'builds' for root in cfg.get('manuscript_roots',[])]
    artifact_tree=artifact_project_relative(cfg)
    if artifact_tree:managed.append(PurePosixPath(artifact_tree))
    def managed_record(path):return any(PurePosixPath(path).is_relative_to(root) for root in managed)
    if selected is None:
        inv=inventory(cfg,0,200,exclude_roots=managed);all_files=inv['files']
        offset=inv['next_offset']
        while offset is not None:
            page=inventory(cfg,offset,200,exclude_roots=managed);all_files.extend(page['files']);offset=page['next_offset']
        selected=[r['path'] for r in all_files if not managed_record(r['path'])]
    elif any(managed_record(path) for path in selected):
        fail('INPUT_DENIED','Build snapshots cannot include managed historical build records')
    if not isinstance(selected,list) or not selected or len(selected)>cfg.get('limits',{}).get('max_files',2048):fail('INPUT_LIMIT','A bounded nonempty input_paths list is required')
    return sorted(set(selected))

def prepare_snapshot_working_directory(root, request):
    """Create only a validated recipe cwd before the snapshot is handed to a worker."""
    if 'working_directory' not in request:
        return
    from paper_studio.latex.worker import directory_option, snapshot_directory
    root = no_links(Path(root))
    relative = directory_option(request['working_directory'])
    target = root if relative == '.' else root / relative
    no_links(target)
    if not target.is_relative_to(root):
        fail('PATH_DENIED', 'Working directory must stay inside the snapshot')
    target.mkdir(parents=True, exist_ok=True)
    # Keep the worker's existing alias/outside/missing-directory check intact.
    snapshot_directory(root, relative)


def snapshot(cfg,dest,request):
    selected=snapshot_paths(cfg,request);budget=cfg.get('limits',{}).get('max_snapshot_bytes',32*1024*1024);total=0;records=[]
    dest.mkdir(parents=True,exist_ok=False)
    for rel in selected:
        raw=safe_read(cfg['project_root'],rel,budget);total+=len(raw)
        if total>budget:fail('INPUT_LIMIT','Snapshot exceeds configured byte budget; supply input_paths')
        q=dest/rel;q.parent.mkdir(parents=True,exist_ok=True);q.write_bytes(raw);q.chmod(0o400)
        records.append({'path':rel,'bytes':len(raw),'sha256':sha(raw)})
    for r in records:
        if sha(safe_read(cfg['project_root'],r['path'],budget))!=r['sha256']:fail('REVISION_CONFLICT','Project changed while capturing input snapshot',path=r['path'])
    prepare_snapshot_working_directory(dest, request)
    return {'files':records,'bytes':total,'sha256':sha(encoded(records))}

def drive_reference(cfg,version):
    from paper_studio.drive.publisher import verified_manifest
    root,manifest,identity=verified_manifest(cfg,version)
    return {'schema':'paper-studio.drive-reference.v1','project_id':cfg['project_id'],'version':version,
            'manifest_path':str(root/'manifest.json'),'manifest_sha256':identity['sha256'],'manifest_bytes':identity['bytes'],
            'member_count':len(manifest['files'])}

def drive_resume_chain(cfg,resume_build_id,version,manifest_sha256):
    """Validate a failed Drive ancestry without rewriting historical jobs."""
    chain=[];current=resume_build_id
    while current is not None:
        if current in chain or len(chain)>=64:fail('DRIVE_RECOVERY_CHAIN','Invalid or excessive recovery ancestry')
        job=job_path(cfg,current);record=json.loads(safe_read(job,'request.json',4*1024*1024))
        request=record.get('request',{});status=read_status(cfg,current)
        if record.get('project_id')!=cfg['project_id'] or record.get('kind')!='drive':fail('PROJECT_DENIED','Recovery target is not a Drive job for this project')
        identity=sha(encoded({'kind':'drive','request':request,'project_id':cfg['project_id']}))
        if identity!=record.get('request_sha256') or status.get('request_sha256',identity)!=identity:
            fail('DRIVE_RECOVERY_CHAIN','Original Drive request identity changed')
        if request.get('version')!=version or request.get('expected_manifest_sha256')!=manifest_sha256:
            fail('VERSION_TAMPERED','Recovery must retain the original version and manifest identity')
        if status['status'] not in ['failed','recovery_required']:fail('DRIVE_RECOVERY_STATE','Only a failed or recovery-required Drive job can be resumed',build_id=current,status=status['status'])
        identity=status.get('worker_identity')
        if identity is None and (job/'launch.json').exists():identity=json.loads(safe_read(job,'launch.json',4096))
        live=process_identity(identity['pid']) if identity else None
        if live and live['state']!='Z' and live['start_ticks']==identity.get('start_ticks'):
            fail('BUILD_BUSY','The prior Drive worker is still alive; wait before recovery',build_id=current)
        chain.append(current);current=request.get('resume_build_id')
    if not chain:fail('INVALID_ARGUMENT','resume_build_id is required for recovery')
    for bid in chain:
        record=json.loads(safe_read(job_path(cfg,bid),'request.json',4*1024*1024))
        if record['request'].get('recovery_root_build_id',chain[-1])!=chain[-1]:fail('DRIVE_RECOVERY_CHAIN','Recovery root identity differs across ancestry')
    return chain

def publication_version(cfg, selection):
    """Resolve one explicit manuscript/revision selection without history scans."""
    from paper_studio.versions import history, lifecycle
    revision = None
    version = selection
    if isinstance(selection, str) and '@' in selection:
        version, revision = selection.split('@', 1)
        if not re.fullmatch(r'rev-\d{3,6}', revision):
            fail('INVALID_ARGUMENT', 'A qualified publication uses <working-version>@rev-NNN')
    archive = history.version_path(cfg, version)
    no_links(archive)
    if (archive / 'manifest.json').is_file():
        if revision is not None:
            archive, manifest, _ = history._read_version_manifest(cfg, version)
            member = next((row for row in manifest['files'] if row.get('path') == 'release/manifest.json'), None)
            if member is None:
                fail('VERSION_SELECTION_CONFLICT', 'The selected archive has no managed revision identity', version=version)
            raw = safe_read(archive, 'release/manifest.json', 8 * 1024**2)
            if sha(raw) != member.get('sha256'):
                fail('VERSION_TAMPERED', 'The archived release selection changed')
            try:
                release = json.loads(raw)
            except (ValueError, RecursionError):
                fail('VERSION_INVALID', 'The archived release selection is not valid JSON')
            if (not isinstance(release, dict) or release.get('schema') != 'paper-studio.release-selection.v1'
                    or release.get('project_id') != cfg['project_id'] or release.get('version') != version):
                fail('PROJECT_DENIED', 'The release selection belongs to another project or manuscript')
            if release.get('revision') != revision:
                fail('VERSION_SELECTION_CONFLICT', 'The archive contains a different managed revision',
                     version=version, requested_revision=revision, archived_revision=release.get('revision'))
        return version
    if not cfg.get('version_management'):
        return version
    review = {'tool': 'paper_publish_to_drive', 'argument': 'review_pdf',
              'required': ['build_id', 'sha256', 'language', 'project_folder_id', 'parent_id']}
    if revision is None and re.fullmatch(r'rev-\d{3,6}', version):
        from paper_studio.versions.review_deliveries import parents
        selected_parents = parents(cfg, version)
        manuscripts = [{'version': parent, 'revision': version, 'formal_selection': parent + '@' + version,
                        'discover': {'tool': 'paper_version_derive', 'action': 'status',
                                     'version': parent, 'revision': version}}
                       for parent in selected_parents[:32]]
        fail('VERSION_SELECTION_REQUIRED', 'A revision needs its parent manuscript or an exact review build',
             requested_revision=version, manuscripts=manuscripts, manuscripts_truncated=len(selected_parents) > 32,
             discover={'tool': 'paper_version_derive', 'action': 'status'},
             formal_selection='<working-version>@' + version, review=review)
    working = lifecycle.directory(cfg, version)
    if not (working / 'VERSION.json').is_file():
        if revision is not None:
            fail('VERSION_SELECTION_REQUIRED', 'The qualified working manuscript is not registered',
                 version=version, revision=revision, review=review)
        return version
    _, managed = lifecycle.read_version(cfg, version)
    if managed.get('status') == 'published':
        fail('VERSION_TAMPERED', 'Published manuscript metadata exists but its formal archive is missing', version=version)
    selected = revision or managed.get('current_revision')
    _, _, source = lifecycle._revision(cfg, version, selected)
    fail('FORMAL_ARCHIVE_REQUIRED', 'The managed revision has no formal archive; use its build for review or explicitly promote it',
         version=version, revision=selected, status=managed.get('status'),
         discover={'tool': 'paper_version_derive', 'action': 'status', 'version': version, 'revision': selected},
         review={**review, 'source_entries': {lang: item['entry_path'] for lang, item in source['languages'].items()}},
         formal={'tool': 'paper_version_derive', 'action': 'promote', 'version': version,
                 'revision': selected, 'dry_run': True,
                 'prerequisites': ['explicit author authorization', 'completed matching EN and ZH builds',
                                   'visual_review', 'expected_version_sha256', 'expected_current_sha256', 'request_id']})


def submit_drive(cfg,version,resume_build_id=None):
    version=publication_version(cfg,version)
    reference=drive_reference(cfg,version)
    key=sha(encoded({'project_id':cfg['project_id'],'version':version,'manifest_sha256':reference['manifest_sha256']}))
    request={'request_id':'drive_'+key[:58],'version':version,'expected_manifest_sha256':reference['manifest_sha256']}
    if resume_build_id is not None:
        chain=drive_resume_chain(cfg,resume_build_id,version,reference['manifest_sha256'])
        request.update(request_id='drvrec_'+sha(encoded([cfg['project_id'],resume_build_id,version,reference['manifest_sha256']]))[:57],
                       resume_build_id=resume_build_id,recovery_root_build_id=chain[-1])
    result=submit(cfg,'drive',request,drive_reference_value=reference)
    return {**result,**{key:request[key] for key in ['resume_build_id','recovery_root_build_id'] if key in request},'asynchronous':True,'accepted':result['status'] in ['queued','running'],
            'upload_complete':result['status']=='completed' and result.get('result',{}).get('upload_complete',False)}

def tex_engine_request(request):
    """Compare engine intent independently of retry IDs and source selection."""
    value={k:v for k,v in request.items() if k not in {'request_id','resume_build_id','recovery_root_build_id','input_paths','_input_manifest_sha256','compatibility_hooks'}}
    value.setdefault('build_profile','xelatex');value.setdefault('use_latexmk',True)
    return value

def tex_resume_chain(cfg,resume_build_id):
    """Verify immutable failed TeX ancestry; never rewrite a prior job."""
    if not isinstance(resume_build_id,str) or not BUILD_ID.fullmatch(resume_build_id):fail('INVALID_ARGUMENT','Valid resume_build_id is required for TeX recovery')
    chain=[];current=resume_build_id;baseline=None;original=None;prior=None
    while current is not None:
        if current in chain or len(chain)>=64:fail('TEX_RECOVERY_CHAIN','Invalid or excessive TeX recovery ancestry')
        job=job_path(cfg,current);record=json.loads(safe_read(job,'request.json',4*1024*1024));status=read_status(cfg,current)
        request=record.get('request',{});snap=record.get('snapshot',{})
        if record.get('project_id')!=cfg['project_id'] or record.get('kind')!='tex' or status.get('kind')!='tex':
            fail('PROJECT_DENIED','Recovery target must be a TeX job for this project')
        if record.get('build_id')!=current or not isinstance(request,dict):fail('TEX_RECOVERY_CHAIN','TeX request identity differs from its build ID')
        digest=record.get('request_sha256')
        if not isinstance(digest,str) or not re.fullmatch('[0-9a-f]{64}',digest) or status.get('request_sha256')!=digest:
            fail('TEX_RECOVERY_CHAIN','Original TeX request identity changed')
        if 'raw_request' in record and sha(encoded({'kind':'tex','request':record['raw_request'],'project_id':cfg['project_id']}))!=digest:
            fail('TEX_RECOVERY_CHAIN','Original raw TeX request identity changed')
        if record.get('prepared_request_sha256',sha(encoded(request)))!=sha(encoded(request)):
            fail('TEX_RECOVERY_CHAIN','Prepared original TeX request identity changed')
        if status['status'] not in ['failed','recovery_required']:
            fail('TEX_RECOVERY_STATE','Only failed or recovery-required TeX jobs can be resumed',build_id=current,status=status['status'])
        identity=status.get('worker_identity')
        if identity is None and (job/'launch.json').exists():identity=json.loads(safe_read(job,'launch.json',4096))
        if not isinstance(identity,dict) or type(identity.get('pid')) is not int or identity['pid']<=0 or not isinstance(identity.get('start_ticks'),str):
            fail('TEX_RECOVERY_WORKER_UNKNOWN','Original worker identity is unavailable; recovery cannot prove it inactive')
        live=process_identity(identity['pid']) if identity else None
        if live and live['state']!='Z' and live['start_ticks']==identity.get('start_ticks'):
            fail('BUILD_BUSY','Prior TeX worker is still alive; wait before recovery',build_id=current)
        if record.get('snapshot_complete') is not True or not isinstance(snap,dict):fail('SNAPSHOT_TAMPERED','TeX recovery requires a complete original snapshot')
        rows=snap.get('files');seen=set();total=0
        if not isinstance(rows,list) or not rows or len(rows)>cfg.get('limits',{}).get('max_files',2048):fail('SNAPSHOT_TAMPERED','Original TeX snapshot has no bounded complete file manifest')
        for row in rows:
            if (not isinstance(row,dict) or not isinstance(row.get('path'),str) or row['path'] in seen
                or type(row.get('bytes')) is not int or row['bytes']<0
                or not isinstance(row.get('sha256'),str) or not re.fullmatch('[0-9a-f]{64}',row['sha256'])):
                fail('SNAPSHOT_TAMPERED','Original TeX snapshot file manifest is invalid')
            seen.add(row['path']);raw=safe_read(job/'source',row['path'],64*1024*1024)
            if len(raw)!=row['bytes'] or sha(raw)!=row['sha256']:fail('SNAPSHOT_TAMPERED','Original TeX snapshot bytes changed',path=row['path'])
            total+=len(raw)
            if total>cfg.get('limits',{}).get('max_snapshot_bytes',32*1024*1024):fail('SNAPSHOT_TAMPERED','Original snapshot exceeds the configured recovery byte limit')
        actual=set()
        for path in (job/'source').rglob('*'):
            no_links(path)
            if path.is_file():actual.add(path.relative_to(job/'source').as_posix())
            elif not path.is_dir():fail('SNAPSHOT_TAMPERED','Original TeX snapshot contains a nonregular object')
        main=next((row for row in rows if row['path']==request.get('tex_path')),None)
        if (actual!=seen or total!=snap.get('bytes') or sha(encoded(rows))!=snap.get('sha256')
            or status.get('snapshot_sha256')!=snap.get('sha256') or not main or main['sha256']!=request.get('expected_revision')):
            fail('SNAPSHOT_TAMPERED','Original TeX snapshot manifest identity or main source differs')
        if baseline is None:
            baseline=snap['sha256'];original=request
            prior={'config_revision':record.get('config_revision'),'code_sha256':record.get('code_sha256'),
                   'compatibility_hooks':request.get('compatibility_hooks',[])}
        elif baseline!=snap['sha256'] or tex_engine_request(original)!=tex_engine_request(request):
            fail('TEX_RECOVERY_CHAIN','TeX ancestry changed original sources or engine intent')
        chain.append(current);current=request.get('resume_build_id')
    for bid in chain:
        record=json.loads(safe_read(job_path(cfg,bid),'request.json',4*1024*1024))
        if record['request'].get('recovery_root_build_id',chain[-1])!=chain[-1]:fail('TEX_RECOVERY_CHAIN','TeX recovery root differs across ancestry')
    return {'build_ids':chain,'root_build_id':chain[-1],'snapshot_sha256':baseline,'request':original,'prior_runtime':prior}

def verify_tex_current_sources(cfg,request,recovery):
    if tex_engine_request(request)!=tex_engine_request(recovery['request']):
        fail('TEX_RECOVERY_INPUT_CHANGED','TeX recovery must retain the original source revision and engine options; use a new ordinary build for changed inputs')
    budget=cfg.get('limits',{}).get('max_snapshot_bytes',32*1024*1024);total=0;rows=[]
    for relative in snapshot_paths(cfg,request):
        raw=safe_read(cfg['project_root'],relative,budget);total+=len(raw)
        if total>budget:fail('INPUT_LIMIT','Current TeX recovery sources exceed the snapshot budget')
        rows.append({'path':relative,'bytes':len(raw),'sha256':sha(raw)})
    if sha(encoded(rows))!=recovery['snapshot_sha256']:
        fail('TEX_RECOVERY_INPUT_CHANGED','Current inputs differ from the failed snapshot; changed manuscripts require an ordinary new build')

def submit(cfg,kind,request,*,drive_reference_value=None):
    if kind not in ['tex','figure','pptx','pdf','drive']:fail('INVALID_ARGUMENT','Unsupported build kind')
    request=dict(request);project(cfg,request.pop('project_id',cfg['project_id']))
    if kind=='tex' and 'recovery_root_build_id' in request:fail('INVALID_ARGUMENT','TeX recovery ancestry is assigned by the provider')
    if kind=='tex' and 'compatibility_hooks' in request:fail('INVALID_ARGUMENT','TeX runtime compatibility hooks may only be injected by trusted execution policy')
    if kind=='tex' and '_material_binding' in request:fail('INVALID_ARGUMENT','Manuscript material bindings may only be injected by trusted execution policy')
    if kind=='tex' and 'resume_build_id' in request:
        if not cfg.get('execution_policy') or any(PurePosixPath(request.get('tex_path','')).is_relative_to(root) for root in cfg.get('manuscript_roots',[])):
            fail('RECOVERY_UNSUPPORTED','TeX recovery requires a trusted current execution target outside managed manuscript build records')
    request_id=request.get('request_id')
    if not isinstance(request_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',request_id):fail('INVALID_ARGUMENT','request_id must be 1..64 letters, digits, dash or underscore')
    body_hash=sha(encoded({'kind':kind,'request':request,'project_id':cfg['project_id']}))
    raw_request=dict(request)
    source=request.get('tex_path') if kind=='tex' else request.get('source_path') if kind=='figure' else request.get('pptx_path') if kind=='pptx' else request.get('pdf_path')
    with locked(cfg,'build'):
        root=builds_root(cfg);root.mkdir(parents=True,exist_ok=True)
        indexed=job_index.enabled(cfg)
        hit=job_index.find_request(cfg,request_id) if indexed else None
        candidates=[root/hit['build_id']/'request.json'] if hit else [] if indexed else root.glob('rb_*/request.json')
        for p in candidates:
            # Fail closed: an unreadable record might own this request_id.
            try:old=json.loads(p.read_text())
            except ValueError:
                fail('BUILD_RECORD_CORRUPT','A historical build request record is not valid JSON; local evidence review is required before new submissions',build_id=p.parent.name)
            except OSError:
                fail('BUILD_STATE_UNAVAILABLE','A historical build request record could not be read; retry the same request after checking the job directory',build_id=p.parent.name)
            if old['request'].get('request_id')==request_id:
                if indexed and (old.get('project_id')!=cfg['project_id'] or old.get('build_id')!=p.parent.name
                                or old.get('request_sha256')!=hit['request_sha256']):
                    fail('BUILD_RECORD_CORRUPT','Indexed request identity differs from its immutable record',build_id=p.parent.name)
                if old['request_sha256']!=body_hash:fail('IDEMPOTENCY_CONFLICT','request_id is already bound to another request')
                return read_status(cfg,p.parent.name)
            if indexed:fail('BUILD_RECORD_CORRUPT','Indexed request ID differs from its immutable record',build_id=p.parent.name)
        if kind=='drive':
            from paper_studio.versions.current_inputs import verify_version_current
            verify_version_current(cfg,request['version'])
        recovery=tex_resume_chain(cfg,request['resume_build_id']) if kind=='tex' and 'resume_build_id' in request else None
        resumed=set(drive_resume_chain(cfg,request['resume_build_id'],request['version'],request['expected_manifest_sha256'])) if kind=='drive' and request.get('resume_build_id') else set(recovery['build_ids']) if recovery else set()
        running=[item for item in active_jobs(cfg) if item['build_id'] not in resumed]
        if running:fail('BUILD_BUSY','A build needs completion or recovery before a new build',build_id=running[0]['build_id'])
        if source:require_revision(cfg,source,request.get('expected_revision'))
        if cfg.get('execution_policy'):
            from paper_studio.execution import prepare_request
            request=prepare_request(cfg,kind,request)
        if recovery:
            verify_tex_current_sources(cfg,request,recovery)
            request['recovery_root_build_id']=recovery['root_build_id']
        build_id='rb_'+uuid.uuid4().hex[:24];job=root/build_id
        created_at=time.time()
        job_index.reserve(cfg,build_id,request_id,body_hash,kind,created_at)
        job.mkdir(mode=0o700)
        # Bind the request before snapshot capture: failed captures must remain
        # idempotent and attributable to the same native job on retry.
        record={'schema':'paper-studio.build-request.v1','build_id':build_id,'project_id':cfg['project_id'],'kind':kind,'request':request,'request_sha256':body_hash,'config_revision':cfg['_config_revision'],'code_sha256':code_identity(),'snapshot':{'files':[],'bytes':0,'sha256':sha(encoded([]))},'snapshot_complete':False,'created_at':created_at,'extension_version':cfg['extension_version']}
        if kind=='tex':record.update(raw_request=raw_request,prepared_request_sha256=sha(encoded(request)))
        if recovery:
            record['recovery_context']={'resume_build_id':request['resume_build_id'],'recovery_root_build_id':recovery['root_build_id'],
                'snapshot_sha256':recovery['snapshot_sha256'],'prior_runtime':recovery['prior_runtime'],
                'current_runtime':{'config_revision':record['config_revision'],'code_sha256':record['code_sha256'],
                                   'compatibility_hooks':request.get('compatibility_hooks',[])}}
        atomic_json(job/'request.json',record,True)
        try:
            if kind=='drive':
                reference=drive_reference_value or drive_reference(cfg,request['version'])
                if reference['manifest_sha256']!=request.get('expected_manifest_sha256'):fail('VERSION_TAMPERED','Queued Drive manifest identity mismatch')
                dest=job/'source';dest.mkdir()
                atomic_json(dest/'version-reference.json',reference,True)
                records=[{'path':'version-reference.json','bytes':(dest/'version-reference.json').stat().st_size,'sha256':file_sha(dest/'version-reference.json')}]
                snap={'files':records,'bytes':records[0]['bytes'],'sha256':sha(encoded(records)),'mode':'verified-version-reference'}
            elif request.get('_text_review',{}).get('source_kind') == 'trusted_snapshot':
                from paper_studio.latex.review_snapshot import capture_snapshot
                snap=capture_snapshot(cfg,job/'source',request)
            elif request.get('_version_binding'):
                from paper_studio.versions.lifecycle import capture_snapshot
                snap=capture_snapshot(cfg,job/'source',request)
            else:snap=snapshot(cfg,job/'source',request)
            if recovery and snap['sha256']!=recovery['snapshot_sha256']:
                fail('TEX_RECOVERY_INPUT_CHANGED','TeX inputs changed during recovery snapshot capture; no worker was launched')
            if request.get('_input_manifest_sha256') and snap['sha256']!=request['_input_manifest_sha256']:
                fail('REVISION_CONFLICT','Dependency inputs changed between planning and immutable snapshot capture')
            if request.get('expected_input_sha256') is not None and snap['sha256']!=request['expected_input_sha256']:
                fail('REVISION_CONFLICT','Captured inputs differ from the requested dependency SHA256')
            if source:
                hit=next((r for r in snap['files'] if r['path']==source),None)
                if not hit or hit['sha256']!=request['expected_revision']:fail('REVISION_CONFLICT','Main source is absent from snapshot or changed')
            record.update(snapshot=snap,snapshot_complete=True)
            atomic_json(job/'request.json',record)
            s={'build_id':build_id,'project_id':cfg['project_id'],'kind':kind,'status':'queued','created_at':record['created_at'],'snapshot_sha256':snap['sha256'],'request_sha256':body_hash,'config_revision':cfg['_config_revision'],'artifacts':[]}
            s.update({key:request[key] for key in ['resume_build_id','recovery_root_build_id'] if key in request})
            if recovery:s['recovery_context']=record['recovery_context']
            _save_status(cfg,job,s,True)
            main=Path(__file__).with_name('main.py')
            with (job/'worker.log').open('xb') as log:
                proc=subprocess.Popen([sys.executable,'-B',str(main),'--config',cfg['_config_path'],'--worker',build_id,'--project-id',cfg['project_id']],stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,start_new_session=True,env={'PATH':'/usr/bin:/bin','HOME':str(Path.home()),'LANG':'C.UTF-8','PYTHONDONTWRITEBYTECODE':'1','XDG_RUNTIME_DIR':f'/run/user/{os.getuid()}','DBUS_SESSION_BUS_ADDRESS':f'unix:path=/run/user/{os.getuid()}/bus'})
            identity=process_identity(proc.pid)
            if not identity:fail('WORKER_START_FAILED','Worker exited before scheduling handshake')
            atomic_json(job/'launch.json',identity,True)
            # Worker owns the status transition; avoid racing it with a queued write.
            return {**s,'pid':proc.pid,'accepted':True}
        except BaseException as exc:
            error=exc.as_dict() if isinstance(exc,StudioError) else {'code':'SNAPSHOT_FAILED','message':str(exc)}
            _save_status(cfg,job,{'build_id':build_id,'project_id':cfg['project_id'],'kind':kind,'status':'failed','error':error,'artifacts':[],**{key:request[key] for key in ['resume_build_id','recovery_root_build_id'] if key in request}})
            fail(error['code'],error['message'],**{**error.get('details',{}),'build_id':build_id})

def verify_snapshot(job,record):
    records=record['snapshot']['files']
    for r in records:
        raw=safe_read(job/'source',r['path'],64*1024*1024)
        if sha(raw)!=r['sha256']:fail('SNAPSHOT_TAMPERED','Input snapshot changed')

@contextlib.contextmanager
def drive_deadline(seconds):
    if isinstance(seconds,bool) or not isinstance(seconds,(int,float)) or not math.isfinite(seconds) or not 0<seconds<=600:
        fail('INVALID_ARGUMENT','Drive worker deadline must be positive and at most 600 seconds')
    def expired(*_):fail('DRIVE_DEADLINE','Drive publication wall deadline exceeded; partial state retained')
    previous=signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM,expired);prior_timer=signal.setitimer(signal.ITIMER_REAL,seconds)
    try:yield
    finally:
        signal.setitimer(signal.ITIMER_REAL,0);signal.signal(signal.SIGALRM,previous)
        if prior_timer[0]>0:signal.setitimer(signal.ITIMER_REAL,*prior_timer)

def drive_summary(receipt):
    result={key:value for key,value in receipt.items() if key not in {'files','current_upload'}}
    files=receipt.get('files',[])
    result.update(files=files[:64],file_count=len(files),files_truncated=len(files)>64,receipt_path='receipt.json')
    if 'current_upload' in receipt:result['current_upload']=receipt['current_upload']
    return result

def drive_preupload_recovery(cfg,request):
    """Admit a first upload only with an audited OAuth failure before Drive writes.

    Missing private state alone is never evidence that an upload is safe. Older
    recovery workers reported NOT_FOUND before opening Drive when the original
    OAuth failure had no private publication receipt; that exact failed ancestry
    can be continued without fabricating or rewriting a historical receipt.
    The caller holds the same per-version lock used by partial recovery.
    """
    version=request['version']
    private=no_links(Path(cfg['state_root'])/'drive-publications'/f'{version}.json')
    if private.exists():return False
    chain=drive_resume_chain(cfg,request['resume_build_id'],version,request['expected_manifest_sha256'])
    oauth_errors={'OAUTH_REQUIRED','OAUTH_REFRESH_FAILED','OAUTH_SCOPE','OAUTH_PERMISSIONS'}
    for index,bid in enumerate(chain):
        job=job_path(cfg,bid);status=read_status(cfg,bid)
        error=status.get('error',{})
        allowed=error.get('code') in oauth_errors
        if index<len(chain)-1 and error.get('code')=='NOT_FOUND':
            allowed=error.get('details')=={'path':f'{version}.json'}
        receipt_path=job/'output/receipt.json'
        raw=safe_read(receipt_path.parent,receipt_path.name,2*1024*1024)
        receipt=json.loads(raw)
        identities=[item for item in status.get('artifacts',[]) if item.get('path')=='receipt.json']
        result=status.get('result',{})
        if (not allowed or len(identities)!=1 or identities[0].get('sha256')!=sha(raw)
            or identities[0].get('bytes')!=len(raw)
            or any(value.get('status')!='failed_before_upload'
                   or value.get('upload_complete') is not False
                   or value.get('version')!=version
                   or value.get('version_manifest_sha256')!=request['expected_manifest_sha256']
                   or value.get('files',[]) or 'current_upload' in value
                   for value in (receipt,result))
            or result.get('file_count')!=0 or result.get('files_truncated') is not False):
            fail('DRIVE_RECOVERY_NOT_READY','Absent publication state requires verified OAuth-before-upload evidence; retain failed jobs for local review')
        # A recovery sidecar/ledger can represent remote writes even if the
        # original publication receipt was removed. Never reinitialize it.
        from paper_studio.drive.publisher import recovery_receipt_path
        recovery=recovery_receipt_path(cfg,version,bid)
        if recovery.exists() or (recovery.parent/'progress.json').exists():
            fail('DRIVE_RECOVERY_NOT_READY','Recovery progress exists without its original publication receipt')
    return True


def run_drive(cfg,job,out,request):
    from paper_studio.drive import publisher
    public_receipt=publisher.public_receipt
    reference=json.loads(safe_read(job/'source','version-reference.json',65536))
    if (reference['project_id']!=cfg['project_id'] or reference['version']!=request['version']
        or reference['manifest_sha256']!=request['expected_manifest_sha256']):fail('SNAPSHOT_TAMPERED','Drive reference differs from queued request')
    out.mkdir(parents=True,exist_ok=False)
    seconds=cfg.get('limits',{}).get('drive_timeout_seconds',600)
    using_publication=not request.get('resume_build_id')
    try:
        with drive_deadline(seconds):
            options={'expected_manifest_sha256':request['expected_manifest_sha256'],'deadline_seconds':seconds}
            if request.get('resume_build_id'):
                receipt=None
                with locked(cfg,'drive-version-'+sha(request['version'].encode())[:24]):
                    if drive_preupload_recovery(cfg,request):
                        using_publication=True
                        receipt=publisher.publish_version(cfg,request['version'],**options)
                        receipt.update(recovery_mode='first_upload_after_oauth_failure')
                if receipt is None:
                    receipt=publisher.recover_version(cfg,request['version'],recovery_id=job.name,**options)
            else:receipt=publisher.publish_version(cfg,request['version'],**options)
        receipt=public_receipt(receipt)
        receipt.update({key:request[key] for key in ['resume_build_id','recovery_root_build_id'] if key in request})
        if receipt.get('upload_complete') is not True:fail('DRIVE_INCOMPLETE','Publisher did not verify all uploaded files')
        atomic_json(out/'receipt.json',receipt,True)
        return drive_summary(receipt)
    except BaseException:
        private=Path(cfg['state_root'])/'drive-publications'/f"{request['version']}.json" if using_publication else publisher.recovery_receipt_path(cfg,request['version'],job.name)
        receipt={'version':request['version'],'version_manifest_sha256':request['expected_manifest_sha256'],
                 'upload_complete':False,'status':'failed_before_upload'}
        try:
            if private.is_file():receipt=public_receipt(json.loads(safe_read(private.parent,private.name,2*1024*1024)))
        except (OSError,StudioError,ValueError):receipt['private_receipt_readable']=False
        receipt['upload_complete']=False
        receipt.update({key:request[key] for key in ['resume_build_id','recovery_root_build_id'] if key in request})
        atomic_json(out/'receipt.json',receipt)
        raise

def worker(cfg,build_id):
    job=job_path(cfg,build_id);record=json.loads((job/'request.json').read_text());s=json.loads((job/'status.json').read_text())
    try:
        if record['config_revision']!=cfg['_config_revision']:fail('CONFIG_CHANGED','Binding changed before build execution')
        if record['code_sha256']!=code_identity():fail('CODE_CHANGED','Extension changed before execution; resubmit under current version')
        verify_snapshot(job,record)
        s.update(status='running',pid=os.getpid(),worker_identity=process_identity(os.getpid()),started_at=time.time());_save_status(cfg,job,s)
        out=job/'output';req={k:v for k,v in record['request'].items() if k not in {'request_id','expected_revision','input_paths'}};kind=record['kind']
        if kind=='tex':
            req={k:v for k,v in req.items() if k not in {'resume_build_id','recovery_root_build_id'}}
            from paper_studio.latex.build import build_tex
            result=build_tex(job/'source',out,req,cfg)
            result={**result,'source_binding':{'tex_path':record['request']['tex_path'],
                'main_sha256':record['request']['expected_revision'],
                'dependencies_sha256':record['request'].get('_input_manifest_sha256'),
                'dependency_plan_bound':record['request'].get('_input_manifest_sha256') is not None,
                'snapshot_sha256':record['snapshot']['sha256'],
                'dependencies':record['snapshot']['files'],
                **({'review_binding':record['request']['_review_binding']} if '_review_binding' in record['request'] else {}),
                **({'text_review':record['request']['_text_review']} if '_text_review' in record['request'] else {})}}
            if cfg.get('execution_policy',{}).get('render_tex_previews'):
                from paper_studio.execution import render_tex_preview
                result=render_tex_preview(cfg,job,out,result)
        elif kind=='figure':
            from paper_studio.figures.build import build_figure
            result=build_figure(job/'source',out,req,cfg)
            if cfg.get('execution_policy') and req.get('engine')=='tikz':
                from pypdf import PdfReader
                if any(len(PdfReader(p).pages)!=1 for p in out.rglob('*.pdf')):
                    fail('FIGURE_PAGE_LIMIT','A standalone TikZ figure must render exactly one page per PDF')
        elif kind=='pptx':
            from paper_studio.pptx.build import run_pptx
            result=run_pptx(job/'source',out,req,cfg)
        elif kind=='pdf':
            from paper_studio.pdf.review import review_pdf
            result=review_pdf(job/'source',out,req,cfg)
        elif kind=='drive':result=run_drive(cfg,job,out,req)
        else:fail('INVALID_ARGUMENT','Unrecognized worker kind')
        verify_snapshot(job,record)
        if record['code_sha256']!=code_identity():fail('CODE_CHANGED','Extension changed during execution; output not published')
        if not out.is_dir():fail('BUILD_FAILED','Engine did not create output')
        artifacts=[];total=0
        for p in sorted(out.rglob('*')):
            no_links(p)
            if p.is_dir():continue
            if not p.is_file() or p.stat().st_nlink!=1:fail('UNSAFE_OUTPUT','Build created nonregular output')
            size=p.stat().st_size;total+=size
            if len(artifacts)>=cfg.get('limits',{}).get('max_files',2048) or total>cfg.get('limits',{}).get('max_output_bytes',64*1024*1024):fail('OUTPUT_LIMIT','Build output budget exceeded')
            relative=p.relative_to(out)
            if any(part.startswith('.') for part in relative.parts):continue
            artifacts.append({'path':relative.as_posix(),'bytes':size,'sha256':file_sha(p),'mime_type':mimetypes.guess_type(p.name)[0] or 'application/octet-stream'})
        manifest={'schema':'paper-studio.build-manifest.v1','build_id':build_id,'project_id':cfg['project_id'],'kind':kind,'input':record['snapshot'],'config_revision':record['config_revision'],'code_sha256':record['code_sha256'],'request_sha256':record['request_sha256'],'result':result,'artifacts':artifacts,'completed_at':time.time()}
        atomic_json(job/'manifest.json',manifest,True)
        s.update(status='completed',finished_at=time.time(),artifacts=artifacts,result=result,manifest_sha256=file_sha(job/'manifest.json'),generated=True,resource_link_returned=False,chatgpt_bytes_received=False,user_opened=False)
    except BaseException as exc:
        is_drive=record.get('kind')=='drive'
        if is_drive:
            from paper_studio.drive.publisher import public_receipt
            error=public_receipt(exc.as_dict()) if isinstance(exc,StudioError) else {'code':'DRIVE_JOB_FAILED','message':'Drive publication failed; inspect the retained private receipt.'}
        else:error=exc.as_dict() if isinstance(exc,StudioError) else {'code':'BUILD_FAILED','message':str(exc)[:1500],'exception_type':type(exc).__name__}
        s.update(status='failed',finished_at=time.time(),error=error)
        if is_drive and (job/'output/receipt.json').is_file():
            s['result']=drive_summary(json.loads(safe_read(job/'output','receipt.json',2*1024*1024)))
            raw=safe_read(job/'output','receipt.json',2*1024*1024)
            s['artifacts']=[{'path':'receipt.json','bytes':len(raw),'sha256':sha(raw),'mime_type':'application/json'}]
        recovery=isinstance(exc,subprocess.TimeoutExpired)
        for name in ['execution.json','PPTX_RECEIPT.json','PPTX_NATIVE_RECEIPT.json']:
            p=job/'output'/name
            if p.is_file() and not p.is_symlink():
                try:
                    receipt=json.loads(safe_read(job/'output',name,2*1024*1024))
                    if isinstance(receipt,dict):
                        receipt_error=receipt.get('error')
                        timed_out=isinstance(receipt_error,dict) and receipt_error.get('type')=='TimeoutExpired'
                        recovery=recovery or receipt.get('recovery_required',False) is True or timed_out
                except (StudioError,ValueError):pass
        if recovery:s.update(status='recovery_required',recovery_required=True)
        if is_drive:print(json.dumps({'drive_job_error':error['code'],'build_id':build_id}),flush=True)
        else:traceback.print_exc()
    _save_status(cfg,job,s)
    return s

def artifact(cfg,build_id,path,action='metadata',offset=0,length=262144,destination=None):
    s=read_status(cfg,build_id)
    failed_receipt=s.get('kind')=='drive' and s['status'] in ['failed','recovery_required'] and path=='receipt.json' and action in ['metadata','chunk','export']
    if s['status']!='completed' and not failed_receipt:fail('BUILD_NOT_COMPLETE','Only a failed Drive job public receipt may be read before successful completion',status=s['status'])
    relpath(path);entry=next((a for a in s['artifacts'] if a['path']==path),None)
    job=job_path(cfg,build_id);p=job/'output'/path
    if failed_receipt:
        from paper_studio.drive.publisher import public_receipt
        raw=safe_read(job/'output',path,2*1024*1024)
        try:value=json.loads(raw)
        except ValueError:fail('ARTIFACT_TAMPERED','Public Drive receipt is not valid JSON')
        if not isinstance(value,dict) or public_receipt(value)!=value:fail('ARTIFACT_TAMPERED','Failed Drive receipt contains private fields')
        actual={'path':path,'bytes':len(raw),'sha256':sha(raw),'mime_type':'application/json'}
        if entry is not None and (entry['sha256']!=actual['sha256'] or entry['bytes']!=actual['bytes']):fail('ARTIFACT_TAMPERED','Failed Drive receipt identity changed')
        entry=actual
    if not entry:fail('NOT_FOUND','Path is not a published artifact')
    if action=='export':action='metadata'  # Gateway performs the native ResourceLink export.
    if action=='metadata':return {**entry,'build_id':build_id,'generated':True,'resource_link_returned':False,'chatgpt_bytes_received':False,'user_opened':False,'native_project':cfg['native_artifact_project'],'native_path':p.relative_to(cfg['artifact_root']).as_posix()}
    if action=='chunk':
        if not isinstance(offset,int) or offset<0 or offset>entry['bytes'] or not isinstance(length,int) or not 1<=length<=262144:fail('INVALID_ARGUMENT','Invalid bounded chunk range')
        with p.open('rb') as f:f.seek(offset);raw=f.read(length)
        return {**entry,'build_id':build_id,'offset':offset,'chunk_bytes':len(raw),'chunk_sha256':sha(raw),'data_base64':base64.b64encode(raw).decode(),'next_offset':offset+len(raw) if offset+len(raw)<entry['bytes'] else None,'client_reassembly_required':True}
    if action=='image':
        fail('HOST_ROUTE_REQUIRED','action=image is served only by the Host paper_build_artifact tool, which returns the verified MCP image; call that tool directly rather than this Provider binding',build_id=build_id,path=path)
    if action=='copy_to_candidate':
        path_for(cfg,destination,True)
        with locked(cfg,'lifecycle') if cfg.get('version_management') else contextlib.nullcontext():
            with locked(cfg):
                path_for(cfg,destination,True)
                require_revision(cfg,destination,'absent')
                copied=safe_write_new(cfg['project_root'],destination,p.read_bytes())
        return {**copied,'source_build_id':build_id,'source_artifact':path}
    fail('INVALID_ARGUMENT','Unknown artifact action')
