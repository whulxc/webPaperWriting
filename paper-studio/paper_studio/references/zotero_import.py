"""Audited single-reference Zotero preflight; exact-target imports fail closed.

Zotero 9.0.6 Connector import has no atomic explicit collection target. This
backend therefore never sends /connector/import, even with authorization.
"""
from __future__ import annotations
import fcntl,hashlib,json,os,re,subprocess,time
from pathlib import Path
from paper_studio.security.policy import atomic_json,encoded,fail,no_links,path_for,relpath,safe_read,sha
from paper_studio.references import zotero_readonly
from paper_studio.references.zotero_import_worker import parse_source
from paper_studio.references.zotero_worker import CITE,KEY,ReadError,normalize_doi,valid_scope

CAPABILITY_ERROR='ZOTERO_TARGET_BINDING_UNSUPPORTED'

def validate_binding(cfg):
    value=cfg.get('zotero_write')
    if value is None:return None
    if not isinstance(value,dict) or set(value)!={'references','bib_sync'}:fail('ZOTERO_CONFIG_INVALID','Exact zotero_write references/bib_sync configuration required')
    sync=value['bib_sync']
    if not isinstance(sync,dict) or set(sync)!={'paths','enabled'} or type(sync['enabled']) is not bool or not isinstance(sync['paths'],list) or len(sync['paths'])>16:fail('ZOTERO_CONFIG_INVALID','Explicit bibliography synchronization enabled flag and bounded path list required')
    if sync['enabled'] and not sync['paths']:fail('ZOTERO_CONFIG_INVALID','Enabled bibliography synchronization requires explicit authorized paths')
    for path in sync['paths']:path_for(cfg,path,True)
    try:valid_scope(cfg.get('zotero_readonly'))
    except ReadError as error:fail(error.code,error.message)
    references=value['references']
    if not isinstance(references,dict) or len(references)>8:fail('ZOTERO_CONFIG_INVALID','At most eight approved library-write source references are allowed')
    for key,reference in references.items():
        if (not isinstance(key,str) or not CITE.fullmatch(key) or not isinstance(reference,dict)
            or set(reference)!={'doi','source_path','source_sha256','collection_name','collection_key','library_id'}):fail('ZOTERO_CONFIG_INVALID','Unsupported single-reference import binding')
        if key not in cfg['zotero_readonly']['items'] or normalize_doi(reference['doi'])!=normalize_doi(cfg['zotero_readonly']['items'][key]['doi']):fail('ZOTERO_CONFIG_INVALID','Write reference must match an explicitly authorized readonly DOI')
        if not isinstance(reference['source_sha256'],str) or not re.fullmatch(r'[0-9a-f]{64}',reference['source_sha256']):fail('ZOTERO_CONFIG_INVALID','Exact approved source SHA256 required')
        if type(reference['library_id']) is not int or reference['library_id']!=1:fail('ZOTERO_CONFIG_INVALID','Only the authorized personal library is supported')
        if not isinstance(reference['collection_name'],str) or not 1<=len(reference['collection_name'])<=200 or any(ord(c)<32 for c in reference['collection_name']):fail('ZOTERO_CONFIG_INVALID','Explicit bounded collection name required')
        if reference['collection_key'] is not None and (not isinstance(reference['collection_key'],str) or not KEY.fullmatch(reference['collection_key'])):fail('ZOTERO_CONFIG_INVALID','Collection key must be null or an exact Zotero key')
        relpath(reference['source_path'])
    return value

def _private(path):
    no_links(path);path.mkdir(parents=True,exist_ok=True,mode=0o700)
    info=path.stat()
    if info.st_uid!=os.getuid() or info.st_mode&0o077:fail('ZOTERO_AUDIT_PERMISSIONS','Import audit directories must be private and owned')

def run_worker(cfg,key,approved,args,source):
    config=cfg['zotero_readonly'];helper=no_links(Path(config['helper_path']))
    if sha(safe_read(helper.parent,helper.name,256*1024))!=config['helper_sha256']:fail('ZOTERO_HELPER_CHANGED','Installed helper differs from project authorization')
    worker=no_links(Path(__file__).with_name('zotero_import_worker.py'))
    payload={'readonly':{**config,'helper_path':zotero_readonly.windows_path(helper)},'approved':approved,
             'source':source,'args':{'citation_key':key,'doi':approved['doi'],'phase':'preflight'}}
    environment={k:os.environ[k] for k in ['HOME','WSL_INTEROP','WSL_DISTRO_NAME','WSLENV','SYSTEMROOT','SystemRoot'] if k in os.environ}
    environment.update(PATH=str(Path.home() / '.local/bin') + ':/usr/local/bin:/usr/bin:/bin',LANG='C.UTF-8',PYTHONIOENCODING='utf-8',PYTHONDONTWRITEBYTECODE='1')
    try:
        result=subprocess.run([zotero_readonly.TOOLCHAIN,'run','python','--host','windows','--',zotero_readonly.windows_path(worker)],
            input=json.dumps(payload,ensure_ascii=False),capture_output=True,text=True,encoding='utf-8',timeout=45,shell=False,env=environment)
    except subprocess.TimeoutExpired:fail('ZOTERO_TIMEOUT','Scoped Windows preflight exceeded 45 seconds; no import request was sent')
    except OSError:fail('ZOTERO_WINDOWS_BRIDGE','Scoped Windows preflight could not start')
    if result.returncode or len(result.stdout.encode())>2*1024*1024:fail('ZOTERO_WORKER_PROTOCOL','Scoped Windows preflight failed or exceeded result bounds')
    try:reply=json.loads(result.stdout)
    except (ValueError,UnicodeError):fail('ZOTERO_WORKER_PROTOCOL','Scoped Windows preflight did not return JSON')
    if not isinstance(reply,dict) or not isinstance(reply.get('ok'),bool):fail('ZOTERO_WORKER_PROTOCOL','Invalid scoped Windows preflight envelope')
    if not reply['ok']:
        error=reply.get('error',{});fail(error.get('code','ZOTERO_IMPORT_WORKER_FAILED'),error.get('message','Scoped Windows preflight failed'))
    output=reply.get('result')
    if not isinstance(output,dict) or output.get('status') not in {'existing','conflict','not_found','permission_denied'} or output.get('import_calls')!=0:fail('ZOTERO_WORKER_PROTOCOL','Preflight cannot return an import success or a write attempt')
    return output

def dispatch(cfg,args):
    base={'project_id':cfg['project_id'],'import_calls':0,'collection_writes':0,'bib_written':False,'import_supported':False}
    if args.get('project_id',cfg['project_id'])!=cfg['project_id']:return {**base,'status':'permission_denied','reason':'project_mismatch'}
    binding=validate_binding(cfg)
    if binding is None:return {**base,'status':'permission_denied','reason':'ZOTERO_WRITE_NOT_CONFIGURED'}
    key=args.get('citation_key');approved=binding['references'].get(key)
    if approved is None:return {**base,'status':'permission_denied','reason':'citation_key_not_authorized'}
    if normalize_doi(args.get('doi'))!=normalize_doi(approved['doi']) or args.get('collection_name')!=approved['collection_name']:
        return {**base,'status':'permission_denied','reason':'doi_or_collection_not_authorized'}
    request_id=args.get('request_id')
    if not isinstance(request_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',request_id):fail('ZOTERO_INVALID_ARGUMENT','Bounded request_id required')
    dry_run=args.get('dry_run',True);authorized=args.get('authorized',False)
    if type(dry_run) is not bool or type(authorized) is not bool:fail('ZOTERO_INVALID_ARGUMENT','dry_run and authorized must be booleans')
    if not dry_run and not authorized:return {**base,'status':'permission_denied','reason':'explicit_authorization_required'}
    state=no_links(Path(cfg['state_root']));_private(state)
    coordination=state.parent/'zotero-import-coordination';_private(coordination)
    # A DOI has one shared lock across sibling project state roots. The lock
    # conveys no project permission: every caller validates its own binding.
    lock_name=sha(encoded([1,normalize_doi(approved['doi'])]))
    fd=os.open(coordination/(lock_name+'.lock'),os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX)
        audit=state/'zotero-imports'/key/request_id
        for directory in [state/'zotero-imports',state/'zotero-imports'/key,audit]:_private(directory)
        intent={'project_id':cfg['project_id'],'citation_key':key,**approved};body_hash=sha(encoded(intent));record=audit/'binding.json'
        if record.exists():
            old=json.loads(safe_read(audit,'binding.json',65536))
            if old.get('intent_sha256')!=body_hash:fail('IDEMPOTENCY_CONFLICT','request_id already belongs to a different approved import intent')
        else:atomic_json(record,{'schema':'paper-studio.zotero-import-intent.v1','intent':intent,'intent_sha256':body_hash,'created_at':time.time()},True)
        attempts=list(audit.glob('attempt-*.json'));attempt_path=audit/f'attempt-{len(attempts)+1:06d}.json'
        attempt={'started_at':time.time(),'dry_run':dry_run,'authorized':authorized,'source_sha256':approved['source_sha256'],'import_calls':0}
        stage='source_validation'
        try:
            path_for(cfg,approved['source_path'])
            raw=safe_read(cfg['project_root'],approved['source_path'],16384)
            if sha(raw)!=approved['source_sha256']:fail('ZOTERO_SOURCE_CHANGED','Approved source changed before preflight')
            try:parse_source(raw.decode('utf-8'),key,approved['doi'])
            except (ReadError,UnicodeError):fail('ZOTERO_SOURCE_INVALID','Approved bibliography is not the authorized single safe reference')
            stage='preflight'
            result=run_worker(cfg,key,approved,args,raw.decode('utf-8'))
        except Exception as error:
            attempt.update(status='probe_unknown' if stage=='preflight' else 'validation_failed',error_code=getattr(error,'code','ZOTERO_PREFLIGHT_FAILED'),finished_at=time.time(),import_outcome='not_attempted')
            atomic_json(attempt_path,attempt,True);raise
        result={**base,**result,'citation_key':key,'doi':result.get('doi',approved['doi']),'request_id':request_id,'dry_run':dry_run,'source_sha256':approved['source_sha256']}
        if result.get('target_verified') and result['status']=='not_found' and not dry_run:
            result.update(status='permission_denied',reason=CAPABILITY_ERROR,code=CAPABILITY_ERROR)
        result['capability_gap']=CAPABILITY_ERROR
        attempt.update(status=result['status'],finished_at=time.time(),result=result,import_outcome='not_attempted')
        atomic_json(attempt_path,attempt,True)
        atomic_json(audit/'latest.json',result)
        return result
    finally:os.close(fd)
