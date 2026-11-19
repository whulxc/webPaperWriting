"""Host-owned append-only manuscript build receipts and verified archives.

Runner permissions stay unchanged. Only this fixed host helper writes manuscript
build receipts; all engines and native jobs are the existing implementations.
"""
from __future__ import annotations
import argparse,base64,ctypes,errno,fcntl,json,os,re,shutil,subprocess,sys,time,uuid
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from paper_studio.security.policy import (StudioError,atomic_json,encoded,fail,file_sha,
    artifact_project_relative,load_config,locked,no_links,path_for,relpath,safe_read,safe_write_new,select_project,sha)
from paper_studio.provider import jobs
from paper_studio.pdf.review import review_pdf

RECORD=re.compile(r'b[0-9]{3,6}\Z')
TERMINAL={'completed','failed','recovery_required'}
MAX_BYTES=256*1024*1024

def publish_directory(stage,destination):
    # Linux renameat2(RENAME_NOREPLACE) publishes an entire directory without
    # replacing even a concurrently created empty destination.
    libc=ctypes.CDLL(None,use_errno=True)
    rename=getattr(libc,'renameat2',None)
    if rename is None:fail('ATOMIC_PUBLISH_UNAVAILABLE','renameat2 is required for exclusive directory publication')
    if rename(-100,os.fsencode(stage),-100,os.fsencode(destination),1)!=0:
        code=ctypes.get_errno()
        if code in {errno.EEXIST,errno.ENOTEMPTY}:fail('REVISION_CONFLICT','Destination already exists')
        if code in {errno.EINVAL,errno.ENOSYS,errno.ENOTSUP}:
            # DrvFS lacks RENAME_NOREPLACE. All module publishers hold the
            # record/helper lock; ordinary edits cannot target archive/builds.
            no_links(Path(destination))
            if Path(destination).exists():fail('REVISION_CONFLICT','Destination already exists')
            os.rename(stage,destination)
            return
        raise OSError(code,os.strerror(code))

def scope(cfg,workdir,record_id=None):
    relpath(workdir)
    if workdir not in cfg.get('manuscript_roots',[]):fail('WRITE_DENIED','workdir is not an explicitly configured manuscript root')
    root=no_links(Path(cfg['project_root'])/workdir)
    if not root.is_dir():fail('NOT_FOUND','Manuscript workdir does not exist')
    external=no_links(Path(cfg['artifact_root'])/'manuscript-builds'/workdir)
    if record_id is not None:
        if not isinstance(record_id,str) or not RECORD.fullmatch(record_id):fail('INVALID_ARGUMENT','record_id must be bNNN')
        root=no_links(root/'builds'/record_id);external=no_links(external/record_id)
    return root,external

def project_json(cfg,workdir,record_id,name):
    return json.loads(safe_read(cfg['project_root'],f'{workdir}/builds/{record_id}/{name}',1024*1024))

def sync_directory(path):
    fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:os.fsync(fd)
    finally:os.close(fd)

def append_receipt(cfg,workdir,record_id,name,value):
    parent=f'{workdir}/builds/{record_id}'
    temporary=parent+'/receipt-stage-'+uuid.uuid4().hex+'.json'
    safe_write_new(cfg['project_root'],temporary,encoded(value))
    staging=Path(cfg['project_root'])/temporary;destination=no_links(Path(cfg['project_root'])/parent/name)
    try:publish_directory(staging,destination)
    finally:
        if staging.exists():staging.unlink()
    sync_directory(destination.parent)
    return {'path':str(destination),'sha256':file_sha(destination)}

def publish_binding(cfg,workdir,record_id,value):
    temporary=f'{workdir}/builds/record-stage-'+uuid.uuid4().hex
    safe_write_new(cfg['project_root'],temporary+'/binding.json',encoded(value))
    staging=Path(cfg['project_root'])/temporary;destination=Path(cfg['project_root'])/workdir/'builds'/record_id
    sync_directory(staging);publish_directory(staging,destination);sync_directory(destination.parent)


def error_value(exc):
    return exc.as_dict() if isinstance(exc,StudioError) else {'code':'BUILD_RECORD_FAILED','message':str(exc)[:1000]}

def binding(cfg,workdir,record_id):
    value=project_json(cfg,workdir,record_id,'binding.json')
    if (value.get('project_id'),value.get('workdir'),value.get('record_id'))!=(cfg['project_id'],workdir,record_id):fail('PROJECT_DENIED','Build receipt identity mismatch')
    return value

def inputs(cfg):
    """Capture project-relative layout while excluding append-only build receipts."""
    result=[];root=Path(cfg['project_root']);excluded={str(Path(w)/'builds') for w in cfg.get('manuscript_roots',[])}
    artifact_tree=artifact_project_relative(cfg)
    if artifact_tree:excluded.add(artifact_tree)
    for parent,dirs,files in os.walk(root,followlinks=False):
        accepted=[]
        for name in sorted(dirs):
            p=Path(parent)/name;relative=p.relative_to(root).as_posix()
            try:relpath(relative)
            except StudioError:continue
            if relative not in excluded and not p.is_symlink():accepted.append(name)
        dirs[:]=accepted
        for name in sorted(files):
            relative=(Path(parent)/name).relative_to(root).as_posix()
            try:path_for(cfg,relative)
            except StudioError:continue
            result.append(relative)
            if len(result)>cfg.get('limits',{}).get('max_files',2048):fail('INPUT_LIMIT','Manuscript input file limit exceeded')
    return sorted(result)


def capture_trusted_sources(cfg,workdir,record_id,body):
    """Freeze the restorable project separately from the exact compiler inputs."""
    from paper_studio.execution import build_plan, prepare_request
    plan=build_plan(cfg,body['tex_path'])
    # Retain strict profile/source/dependency checks before allocating a build.
    prepare_request(cfg,'tex',body)
    _,external=scope(cfg,workdir,record_id);external.mkdir(parents=True,exist_ok=True)
    directory='source-capture-'+uuid.uuid4().hex
    container=external/directory;container.mkdir()
    try:
        captured=jobs.snapshot(cfg,container/'source',{'input_paths':inputs(cfg)})
        full={entry['path']:entry for entry in captured['files']}
        if any(full.get(entry['path'])!=entry for entry in plan['files']):
            fail('REVISION_CONFLICT','Dependency inputs changed during complete manuscript archival capture')
    except BaseException:
        shutil.rmtree(container)
        raise
    return {**captured,'directory':directory}, {'files':plan['files'],'sha256':plan['input_sha256']}, plan


def launch(cfg,workdir,record_id):
    _,external=scope(cfg,workdir,record_id);external.mkdir(parents=True,exist_ok=True)
    live_path=external/'launch.json'
    if live_path.exists():
        old=json.loads(safe_read(external,'launch.json',4096));live=jobs.process_identity(old['pid'])
        if live and live['state']!='Z' and live['start_ticks']==old.get('start_ticks'):return
    command=[sys.executable,'-B',str(Path(__file__).resolve()),'--config',cfg['_config_path'],
             '--project-id',cfg['project_id'],'--workdir',workdir,'--record-id',record_id]
    with (external/'helper.log').open('ab') as log:
        proc=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,
            start_new_session=True,env={'HOME':str(Path.home()),'PATH':'/usr/local/bin:/usr/bin:/bin',
            'LANG':'C.UTF-8','PYTHONDONTWRITEBYTECODE':'1','XDG_RUNTIME_DIR':f'/run/user/{os.getuid()}'})
    identity=jobs.process_identity(proc.pid)
    if identity:atomic_json(live_path,identity)

def verify_archive(cfg,workdir,record_id):
    _,external=scope(cfg,workdir,record_id);archive=external/'archive';no_links(archive)
    terminal=project_json(cfg,workdir,record_id,'terminal.json')
    raw=safe_read(archive,'manifest.json',4*1024*1024)
    if sha(raw)!=terminal.get('manifest_sha256'):fail('ARCHIVE_TAMPERED','Archive manifest identity mismatch')
    manifest=json.loads(raw)
    if (manifest.get('project_id'),manifest.get('workdir'),manifest.get('record_id'))!=(cfg['project_id'],workdir,record_id):fail('PROJECT_DENIED','Archive project identity mismatch')
    total=0;seen=set()
    for entry in manifest['files']:
        path=entry['path'];relpath(path)
        if path in seen:fail('ARCHIVE_TAMPERED','Duplicate archive member')
        seen.add(path);raw=safe_read(archive,path,MAX_BYTES);total+=len(raw)
        if total>MAX_BYTES or len(seen)>cfg.get('limits',{}).get('max_files',2048):fail('INPUT_LIMIT','Archive exceeds configured limits')
        if len(raw)!=entry['bytes'] or sha(raw)!=entry['sha256']:fail('ARCHIVE_TAMPERED','Archive member hash mismatch',path=path)
    return archive,manifest,terminal

def summary(cfg,workdir,record_id,relaunch=True):
    root,external=scope(cfg,workdir,record_id);bound=binding(cfg,workdir,record_id)
    value={'project_id':cfg['project_id'],'workdir':workdir,'record_id':record_id,'request_id':bound['request_id']}
    if (root/'terminal.json').exists():
        _,manifest,terminal=verify_archive(cfg,workdir,record_id)
        return {**value,'archive_status':terminal['status'],'build_id':terminal.get('native_build_id'),**{k:terminal[k] for k in ['status','native_build_id','manifest_sha256','error'] if k in terminal},
                'files':len(manifest['files']),'bytes':sum(x['bytes'] for x in manifest['files']),
                'source_verified':manifest.get('source_verified',False),'visual_review':'not_established_by_preview_generation'}
    if (root/'job.json').exists():
        job=project_json(cfg,workdir,record_id,'job.json');value.update(status='archiving',native_build_id=job['native_build_id'])
        value['build_id']=job['native_build_id']
        if relaunch:launch(cfg,workdir,record_id)
    else:
        value['status']='submission_pending'
        attempts=sorted(root.glob('attempt-*.json'))
        if attempts:
            attempt=project_json(cfg,workdir,record_id,attempts[-1].name)
            value.update(submission_attempts=len(attempts),last_submission_error=attempt.get('error'))
    value['archive_status']=value['status']
    return value

def submit(cfg,args,callback):
    workdir=args['workdir'];root,_=scope(cfg,workdir)
    if cfg.get('limits',{}).get('max_files',2048)<8:fail('INPUT_LIMIT','Archival requires a file budget of at least 8')
    request_id=args.get('request_id')
    if not isinstance(request_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',request_id):fail('INVALID_ARGUMENT','request_id must be 1..64 letters, digits, dash or underscore')
    source=args.get('tex_path');relpath(source)
    if not Path(source).is_relative_to(workdir):fail('PATH_DENIED','tex_path must be inside the manuscript workdir')
    path_for(cfg,source)
    body={k:args[k] for k in ['tex_path','expected_revision','expected_input_sha256','build_profile','use_latexmk'] if k in args}
    body_hash=sha(encoded(body))
    with locked(cfg,'manuscript-records'):
        records=root/'builds';no_links(records)
        matches=[]
        if records.exists():
            for child in sorted(records.iterdir()):
                if RECORD.fullmatch(child.name):
                    try:item=binding(cfg,workdir,child.name)
                    except (StudioError,ValueError,OSError):continue  # Preserve corrupt allocated numbers.
                    if item['request_id']==request_id:matches.append(item)
        if len(matches)>1:fail('ARCHIVE_TAMPERED','Duplicate manuscript request binding')
        if matches:
            bound=matches[0]
            if bound['body_sha256']!=body_hash:fail('IDEMPOTENCY_CONFLICT','request_id is bound to another manuscript build')
            record_id=bound['record_id']
        else:
            numbers=[int(p.name[1:]) for p in records.iterdir() if RECORD.fullmatch(p.name)] if records.exists() else []
            number=max(numbers,default=0)+1
            if number>999999:fail('INPUT_LIMIT','Manuscript record numbering exhausted')
            record_id=f'b{number:03d}'
            bound={'schema':'paper-studio.manuscript-binding.v1','project_id':cfg['project_id'],'workdir':workdir,
                   'record_id':record_id,'request_id':request_id,'body_sha256':body_hash,'request':body,'created_at':time.time(),
                   'native_args':{'project_id':cfg['project_id'],'request_id':'mr_'+sha(encoded([workdir,request_id]))[:60],
                                  **body,'input_paths':inputs(cfg)}}
            if cfg.get('execution_policy'):
                captured,execution,plan=capture_trusted_sources(cfg,workdir,record_id,body)
                bound.update(archive_snapshot=captured,execution_input_manifest=execution)
                bound['native_args'].update(input_paths=plan['input_paths'],expected_input_sha256=plan['input_sha256'])
            publish_binding(cfg,workdir,record_id,bound)
        record_root,external=scope(cfg,workdir,record_id)
        if (record_root/'terminal.json').exists():return summary(cfg,workdir,record_id,False)
        if not (record_root/'job.json').exists():
            if callback is None:fail('SUBMIT_CALLBACK_REQUIRED','Native submission callback is required')
            native_args=bound['native_args']
            # A retry repeats the exact original native request, including the
            # captured input path list, even if new source files now exist.
            try:
                result=callback(native_args)
                bid=result.get('build_id')
                if not isinstance(bid,str) or not jobs.BUILD_ID.fullmatch(bid):fail('NATIVE_RESPONSE','Native submission did not return a valid build_id')
            except Exception as exc:
                attempts=list(record_root.glob('attempt-*.json'))
                append_receipt(cfg,workdir,record_id,f'attempt-{len(attempts)+1:06d}.json',
                               {'attempted_at':time.time(),'error':error_value(exc)})
                bid=exc.details.get('build_id') if isinstance(exc,StudioError) else None
                if bid and jobs.BUILD_ID.fullmatch(bid):
                    append_receipt(cfg,workdir,record_id,'job.json',{'native_build_id':bid,'admitted_at':time.time()})
                    launch(cfg,workdir,record_id)
                    return summary(cfg,workdir,record_id,False)
                definite=isinstance(exc,StudioError) and exc.code in {'BUILD_BUSY','REVISION_CONFLICT','INPUT_LIMIT','WRITE_DENIED','PATH_DENIED','NOT_FOUND','INVALID_ARGUMENT','SNAPSHOT_FAILED','IDEMPOTENCY_CONFLICT','BUILD_PROFILE_DENIED','INPUT_DENIED'}
                if definite:
                    append_receipt(cfg,workdir,record_id,'job.json',{'native_build_id':None,'submission_error':error_value(exc)})
                    return seal(cfg,workdir,record_id,'failed',error_value(exc))
                return summary(cfg,workdir,record_id,False)
            append_receipt(cfg,workdir,record_id,'job.json',{'native_build_id':bid,'admitted_at':time.time()})
        launch(cfg,workdir,record_id)
        return summary(cfg,workdir,record_id,False)

def seal(cfg,workdir,record_id,status,error=None):
    """Called with helper.lock held; archive publication and terminal are append-only."""
    root,external=scope(cfg,workdir,record_id);bound=binding(cfg,workdir,record_id)
    if (root/'terminal.json').exists():return summary(cfg,workdir,record_id,False)
    archive=external/'archive'
    if archive.exists():
        # Crash after archive rename: recover its immutable terminal payload.
        manifest=json.loads(safe_read(archive,'manifest.json',4*1024*1024))
        terminal=manifest['terminal'];append_receipt(cfg,workdir,record_id,'terminal.json',{**terminal,'manifest_sha256':file_sha(archive/'manifest.json')})
        return summary(cfg,workdir,record_id,False)
    stage=external/('stage-'+uuid.uuid4().hex);stage.mkdir(parents=True)
    job=project_json(cfg,workdir,record_id,'job.json');bid=job['native_build_id'];source_verified=False
    budget={'bytes':0,'files':0};maxfiles=cfg.get('limits',{}).get('max_files',2048)
    def copy(raw,relative):
        budget['bytes']+=len(raw);budget['files']+=1
        if budget['bytes']>MAX_BYTES-4*1024*1024 or budget['files']>maxfiles-4:fail('OUTPUT_LIMIT','Manuscript archive exceeds limits')
        safe_write_new(stage,relative,raw)
    copy(encoded(bound),'binding.json');copy(encoded(job),'job.json')
    try:
        for attempt in sorted(root.glob('attempt-*.json')):
            copy(safe_read(root,attempt.name,1024*1024),attempt.name)
        if bid is not None:
            native=jobs.job_path(cfg,bid);record=json.loads(safe_read(native,'request.json',4*1024*1024))
            if record.get('project_id')!=cfg['project_id']:fail('PROJECT_DENIED','Native build project mismatch')
            for name in ['request.json','status.json','manifest.json','worker.log']:
                if (native/name).exists():copy(safe_read(native,name,MAX_BYTES),'native-manifest.json' if name=='manifest.json' else name)
            if record.get('snapshot_complete',True) is not True:fail('SNAPSHOT_INCOMPLETE','Native submission failed before capturing a complete source snapshot')
            jobs.verify_snapshot(native,record)
            source_root=native/'source';source_entries=record['snapshot']['files']
            if 'archive_snapshot' in bound:
                captured=bound['archive_snapshot'];execution=bound['execution_input_manifest']
                if (record['snapshot']['files']!=execution['files']
                        or sha(encoded(record['snapshot']['files']))!=execution['sha256']):
                    fail('SNAPSHOT_TAMPERED','Native compiler inputs differ from the bound dependency manifest')
                full={entry['path']:entry for entry in captured['files']}
                if (sha(encoded(captured['files']))!=captured['sha256']
                        or any(full.get(entry['path'])!=entry for entry in execution['files'])):
                    fail('SNAPSHOT_TAMPERED','Compiler dependencies differ from the complete source archive')
                relpath(captured['directory'])
                source_root=no_links(external/captured['directory']/'source')
                source_entries=captured['files']
                copy(encoded({'schema':'paper-studio.manuscript-inputs.v1',
                    'compiler_inputs':execution,'complete_source_archive':{
                        key:captured[key] for key in ['files','bytes','sha256']},
                    'scope':'Only compiler_inputs were supplied to TeX; other source files are retained for restoration'}),
                    'input-manifests.json')
            for entry in source_entries:
                raw=safe_read(source_root,entry['path'],MAX_BYTES)
                if sha(raw)!=entry['sha256']:fail('SNAPSHOT_TAMPERED','Source changed during archival')
                copy(raw,'source/'+entry['path'])
            source_verified=True
            native_status=jobs.read_status(cfg,bid)
            published={x['path']:x for x in native_status.get('artifacts',[])}
            if (native/'output').exists():
                for p in sorted((native/'output').rglob('*')):
                    no_links(p)
                    if p.is_dir():continue
                    relative=p.relative_to(native/'output').as_posix();raw=safe_read(native/'output',relative,MAX_BYTES)
                    if status=='completed':
                        entry=published.get(relative)
                        if entry is None:continue
                        if sha(raw)!=entry['sha256'] or len(raw)!=entry['bytes']:fail('ARTIFACT_TAMPERED','Output changed during archival')
                    copy(raw,'output/'+relative)
            if status=='completed':
                pdfs=[x for x in published if x.lower().endswith('.pdf')]
                expected=Path(bound['request']['tex_path']).stem+'.pdf'
                pdf=next((p for p in pdfs if Path(p).name==expected),None)
                if pdf is None:fail('PREVIEW_FAILED','Completed TeX build has no main PDF')
                review_input=external/('preview-input-'+uuid.uuid4().hex);review_input.mkdir()
                safe_write_new(review_input,'main.pdf',safe_read(stage,'output/'+pdf,MAX_BYTES))
                preview=external/('preview-output-'+uuid.uuid4().hex)
                try:
                    reviewed=review_pdf(review_input,preview,{'pdf_path':'main.pdf'},cfg)
                    rendered=reviewed.get('rendered_pages',[])
                    if not rendered or len(rendered)!=len(reviewed.get('pages',[])):fail('PREVIEW_FAILED','PDF preview did not render every page')
                    for p in sorted(preview.rglob('*')):
                        no_links(p)
                        if p.is_dir():continue
                        relative=p.relative_to(preview).as_posix();copy(safe_read(preview,relative,MAX_BYTES),'preview/'+relative)
                finally:
                    shutil.rmtree(review_input,ignore_errors=True)
                    if preview.exists():
                        for p in sorted(preview.rglob('*')):
                            no_links(p)
                            if p.is_dir():continue
                            relative=p.relative_to(preview).as_posix()
                            if not (stage/'preview'/relative).exists():copy(safe_read(preview,relative,MAX_BYTES),'preview/'+relative)
    except Exception as exc:
        status='failed';error=error_value(exc)
    terminal={'schema':'paper-studio.manuscript-terminal.v1','project_id':cfg['project_id'],'workdir':workdir,
              'record_id':record_id,'native_build_id':bid,'status':status,'finished_at':time.time()}
    if error:terminal['error']=error
    safe_write_new(stage,'terminal-status.json',encoded(terminal))
    files=[{'path':p.relative_to(stage).as_posix(),'bytes':p.stat().st_size,'sha256':file_sha(p)} for p in sorted(stage.rglob('*')) if p.is_file()]
    manifest={'schema':'paper-studio.manuscript-archive.v1','project_id':cfg['project_id'],'workdir':workdir,
              'record_id':record_id,'native_build_id':bid,'status':status,'source_verified':source_verified,
              'files':files,'terminal':terminal}
    atomic_json(stage/'manifest.json',manifest,True)
    if archive.exists():fail('REVISION_CONFLICT','Archive destination already exists')
    publish_directory(stage,archive)
    append_receipt(cfg,workdir,record_id,'terminal.json',{**terminal,'manifest_sha256':file_sha(archive/'manifest.json')})
    return summary(cfg,workdir,record_id,False)

def helper(cfg,workdir,record_id):
    root,external=scope(cfg,workdir,record_id);external.mkdir(parents=True,exist_ok=True)
    fd=os.open(external/'helper.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return
        if (root/'terminal.json').exists():return
        job=project_json(cfg,workdir,record_id,'job.json');deadline=time.monotonic()+cfg.get('limits',{}).get('timeout_seconds',180)+120
        while True:
            try:status=jobs.read_status(cfg,job['native_build_id'])
            except Exception as exc:return seal(cfg,workdir,record_id,'failed',error_value(exc))
            if status['status'] in TERMINAL:return seal(cfg,workdir,record_id,status['status'],status.get('error'))
            if time.monotonic()>deadline:return seal(cfg,workdir,record_id,'recovery_required',{'code':'ARCHIVE_WAIT_TIMEOUT','message':'Native build did not reach a terminal state before archival deadline'})
            time.sleep(1)
    finally:os.close(fd)

def list_summary(cfg,workdir,record_id):
    try:return summary(cfg,workdir,record_id)
    except (StudioError,ValueError,OSError) as exc:
        return {'project_id':cfg['project_id'],'workdir':workdir,'record_id':record_id,
                'status':'corrupt_record','archive_status':'corrupt_record',
                'error':{'code':'CORRUPT_RECORD','message':'Record cannot be verified','cause':error_value(exc)}}

def dispatch(cfg,args,submit_tex_callback=None):
    action=args.get('action');workdir=args.get('workdir');root,_=scope(cfg,workdir)
    if action=='submit':return submit(cfg,args,submit_tex_callback)
    if action=='list':
        offset=args.get('offset',0);length=args.get('length',50)
        if type(offset) is not int or offset<0 or type(length) is not int or not 1<=length<=100:fail('INVALID_ARGUMENT','list offset>=0 and length 1..100 required')
        records=sorted((p.name for p in (root/'builds').iterdir() if RECORD.fullmatch(p.name)),key=lambda n:int(n[1:])) if (root/'builds').exists() else []
        return {'project_id':cfg['project_id'],'workdir':workdir,'records':[list_summary(cfg,workdir,r) for r in records[offset:offset+length]],'total':len(records),'next_offset':offset+length if offset+length<len(records) else None}
    record_id=args.get('record_id');scope(cfg,workdir,record_id);binding(cfg,workdir,record_id)
    if action=='status':return summary(cfg,workdir,record_id)
    archive,manifest,_=verify_archive(cfg,workdir,record_id)
    if action=='read':
        relative=args.get('path');relpath(relative)
        entry=next((r for r in manifest['files'] if r['path']==relative),None)
        if relative=='manifest.json':entry={'path':relative,'bytes':(archive/relative).stat().st_size,'sha256':file_sha(archive/relative)}
        if entry is None:fail('NOT_FOUND','Path is not in the verified archive manifest')
        offset=args.get('offset',0);length=args.get('length',262144)
        if type(offset) is not int or not 0<=offset<=entry['bytes'] or type(length) is not int or not 1<=length<=262144:fail('INVALID_ARGUMENT','Invalid bounded chunk range')
        raw=safe_read(archive,relative,MAX_BYTES)[offset:offset+length]
        return {**entry,'record_id':record_id,'offset':offset,'chunk_bytes':len(raw),'data_base64':base64.b64encode(raw).decode(),'next_offset':offset+len(raw) if offset+len(raw)<entry['bytes'] else None}
    if action=='restore':
        if not manifest.get('source_verified'):fail('SNAPSHOT_TAMPERED','Archive does not contain verified restorable sources')
        destination=args.get('destination');relpath(destination);dest=no_links(Path(cfg['project_root'])/destination)
        if dest.exists():fail('REVISION_CONFLICT','Restore destination must be completely new')
        selected=[r for r in manifest['files'] if r['path'].startswith('source/')]
        if not selected:fail('NOT_FOUND','Archive contains no source tree')
        for entry in selected:
            target=destination+'/'+entry['path'][7:];path_for(cfg,target,True)
            if any(Path(target).is_relative_to(Path(w)/'builds') for w in cfg.get('manuscript_roots',[])):fail('WRITE_DENIED','Manuscript build records cannot be restore destinations')
        stage_relative=(Path(destination).parent/('restore-stage-'+uuid.uuid4().hex)).as_posix()
        for entry in selected:path_for(cfg,stage_relative+'/'+entry['path'][7:],True)
        with locked(cfg),locked(cfg,'manuscript-records'):
            if dest.exists():fail('REVISION_CONFLICT','Restore destination already exists')
            for entry in selected:safe_write_new(cfg['project_root'],stage_relative+'/'+entry['path'][7:],safe_read(archive,entry['path'],MAX_BYTES))
            publish_directory(Path(cfg['project_root'])/stage_relative,dest)
        return {'record_id':record_id,'destination':destination,'files':len(selected),'layout':'complete project-relative source tree under destination'}
    fail('INVALID_ARGUMENT','Unknown manuscript build action')

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);parser.add_argument('--project-id',required=True)
    parser.add_argument('--workdir',required=True);parser.add_argument('--record-id',required=True);args=parser.parse_args()
    cfg=select_project(load_config(args.config),args.project_id);helper(cfg,args.workdir,args.record_id)

if __name__=='__main__':main()
