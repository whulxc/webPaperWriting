"""Local approval for one renamed preview PNG or exact version manifest.

The unresolved original attempt is retained byte-for-byte in the shared ledger.
The replacement has its own durable generated ID; it never reuses an unknown
POST identity or converts absence into permission to repeat that POST.
"""
import json,os,re,stat
from pathlib import Path
from paper_studio.security.policy import *

SCHEMA='paper-studio.drive-resolution.v1'


def _supported_name(name):
    return isinstance(name,str) and (name=='manifest.json' or re.fullmatch(r'rb_[a-f0-9]{24}_preview_page_[0-9]{3}\.png',name))


def _paths(cfg,version):
    from paper_studio.drive.publisher import recovery_receipt_path
    parent=recovery_receipt_path(cfg,version,'resolution').parent
    return parent/'resolution.json',parent/'progress.json'


def prepare(cfg,version,expected_manifest_sha256,name,remote_name,expected_source_sha256,expected_bytes,reason,original_build_id):
    from paper_studio.drive import publisher as p
    root,manifest,identity=p.verified_manifest(cfg,version)
    if identity['sha256']!=expected_manifest_sha256:fail('VERSION_TAMPERED','Resolution requires the exact approved manifest')
    if not isinstance(reason,str) or not 8<=len(reason)<=1000:fail('INVALID_ARGUMENT','Explicit user approval reason required')
    if not _supported_name(name):fail('DRIVE_RESOLUTION_DENIED','Only an explicitly selected preview PNG or the version manifest is supported')
    if not isinstance(expected_source_sha256,str) or not re.fullmatch('[0-9a-f]{64}',expected_source_sha256):fail('INVALID_ARGUMENT','Exact approved source SHA256 required')
    if remote_name!='RECOVERY_'+expected_source_sha256[:12]+'_'+name:fail('DRIVE_RESOLUTION_DENIED','Recovery name must explicitly bind the exact source SHA and original name')
    if isinstance(expected_bytes,bool) or not isinstance(expected_bytes,int) or expected_bytes<=0:fail('INVALID_ARGUMENT','Positive approved byte size required')
    uploads=p.publication_uploads(root,manifest,identity)
    matched=[(path,ident) for path,logical,ident in uploads if logical==name]
    if len(matched)!=1 or matched[0][1]!={'sha256':expected_source_sha256,'bytes':expected_bytes}:fail('VERSION_TAMPERED','Approved source identity differs from immutable publication plan')
    if name=='manifest.json' and (matched[0][0]!=root/'manifest.json' or matched[0][1]!=identity):
        fail('VERSION_TAMPERED','Recovery must use the exact verified version manifest')
    resolution_path,ledger_path=_paths(cfg,version)
    ledger_raw=safe_read(ledger_path.parent,ledger_path.name,p.RESPONSE_LIMIT)
    ledger=json.loads(ledger_raw)
    from paper_studio.provider import jobs
    if jobs.active_jobs(cfg):fail('BUILD_BUSY','Wait for active project jobs before resolving a publication')
    chain=jobs.drive_resume_chain(cfg,original_build_id,version,expected_manifest_sha256)
    if len(chain)!=1:fail('DRIVE_RESOLUTION_DENIED','Resolution must bind the original publication job')
    original_job=jobs.job_path(cfg,original_build_id)
    original_status=safe_read(original_job,'status.json',p.RESPONSE_LIMIT)
    original_request=safe_read(original_job,'request.json',p.RESPONSE_LIMIT)
    if json.loads(original_status).get('error',{}).get('code')!='DRIVE_INTERRUPTED':
        fail('DRIVE_RESOLUTION_DENIED','Original publication must have an interrupted upload')
    original_path=no_links(Path(cfg['state_root'])/'drive-publications'/f'{version}.json')
    original_raw=safe_read(original_path.parent,original_path.name,p.RESPONSE_LIMIT)
    original=json.loads(original_raw)
    anchor={'project_id':cfg['project_id'],'version':version,'version_manifest_sha256':identity['sha256'],
            'original_receipt_sha256':sha(original_raw),'folder_id':original.get('folder_id')}
    if any(ledger.get(k)!=v for k,v in anchor.items()) or any(original.get(k)!=v for k,v in anchor.items() if k!='original_receipt_sha256'):
        fail('DRIVE_RESOLUTION_DENIED','Resolution project/version/original receipt binding differs')
    entry=ledger.get('entries',{}).get(name,{})
    attempt=entry.get('last_upload',entry.get('original_upload',{}))
    if (entry.get('name')!=name or entry.get('attempted') is not True or entry.get('id') or entry.get('verified')
        or entry.get('sha256')!=expected_source_sha256 or entry.get('bytes')!=expected_bytes
        or (name=='manifest.json' and entry.get('version_path')!='manifest.json')
        or attempt.get('transport')!='multipart' or attempt.get('confirmed_bytes')!=0 or attempt.get('id')):
        fail('DRIVE_RESOLUTION_DENIED','Resolution requires the exact unresolved zero-ack multipart attempt')
    resolution={'schema':SCHEMA,**anchor,'name':name,'remote_name':remote_name,
                'version_path':entry['version_path'],'sha256':expected_source_sha256,'bytes':expected_bytes,'md5':entry['md5'],
                'original_attempt_sha256':sha(encoded(entry)),'original_ledger_sha256':sha(ledger_raw),
                'original_build_id':original_build_id,'original_job_request_sha256':sha(original_request),
                'original_job_status_sha256':sha(original_status),'approval_reason':reason,'possible_prior_remote_object':True}
    if ledger.get('resolution') is not None:
        prior_raw=safe_read(resolution_path.parent,resolution_path.name,65536)
        prior=json.loads(prior_raw)
        resolution['original_ledger_sha256']=prior.get('original_ledger_sha256')
        if prior!=resolution:fail('DRIVE_RESOLUTION_DENIED','Existing approval differs from the requested resolution')
        effective_entries(cfg,version,ledger,anchor)
    raw=encoded(resolution)
    return {'resolution':resolution,'resolution_sha256':sha(raw),'apply_writes_drive':False}


def apply(cfg,*,authorized=False,expected_resolution_sha256=None,**args):
    if authorized is not True:fail('AUTHORIZATION_REQUIRED','Applying a local recovery resolution requires explicit authorization')
    version=args['version']
    with locked(cfg,'drive-version-'+sha(version.encode())[:24]):
        plan=prepare(cfg,**args)
        if plan['resolution_sha256']!=expected_resolution_sha256:fail('REVISION_CONFLICT','Reviewed resolution identity changed')
        path,ledger_path=_paths(cfg,version);raw=encoded(plan['resolution'])
        ledger=json.loads(safe_read(ledger_path.parent,ledger_path.name,4*1024*1024))
        pin={'sha256':sha(raw),'name':plan['resolution']['name']}
        if ledger.get('resolution') not in (None,pin):fail('DRIVE_RESOLUTION_DENIED','This version already has a different approved resolution')
        if path.exists():
            if safe_read(path.parent,path.name,65536)!=raw:fail('DRIVE_RESOLUTION_DENIED','Immutable resolution already differs')
        else:
            safe_write_new(path.parent,path.name,raw);path.chmod(0o400)
        ledger['resolution']=pin
        ledger.setdefault('replacement_state',{})
        atomic_json(ledger_path,ledger)
        return {'status':'applied','resolution_sha256':sha(raw),'name':pin['name'],'remote_name':plan['resolution']['remote_name'],'drive_writes':0}


def effective_entries(cfg,version,ledger,anchor):
    """Return ordinary entries plus the one independent approved replacement."""
    path,_=_paths(cfg,version);pin=ledger.get('resolution')
    if pin is None:
        if path.exists() or ledger.get('replacement_state'):fail('DRIVE_RESOLUTION_DENIED','Unregistered resolution state exists')
        return ledger['entries'],{}
    info=path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_nlink!=1 or info.st_mode&0o277:
        fail('DRIVE_RESOLUTION_DENIED','Resolution must be private, immutable and single-link')
    raw=safe_read(path.parent,path.name,65536)
    if not isinstance(pin,dict) or set(pin)!={'sha256','name'} or sha(raw)!=pin.get('sha256'):fail('DRIVE_RESOLUTION_DENIED','Approved resolution bytes changed')
    value=json.loads(raw);name=pin['name'];source=ledger['entries'].get(name,{})
    keys={'schema',*anchor,'name','remote_name','version_path','sha256','bytes','md5','original_attempt_sha256','original_ledger_sha256','original_build_id','original_job_request_sha256','original_job_status_sha256','approval_reason','possible_prior_remote_object'}
    if (set(value)!=keys or value.get('schema')!=SCHEMA or any(value.get(k)!=v for k,v in anchor.items())
        or value.get('name')!=name or source.get('name')!=name or not _supported_name(name)
        or value.get('remote_name')!='RECOVERY_'+source.get('sha256','')[:12]+'_'+name
        or value.get('original_attempt_sha256')!=sha(encoded(source))
        or any(value.get(k)!=source.get(k) for k in ('version_path','sha256','bytes','md5'))
        or value.get('possible_prior_remote_object') is not True):
        fail('DRIVE_RESOLUTION_DENIED','Resolution identity differs from the original immutable attempt')
    if name=='manifest.json':
        from paper_studio.drive import publisher as p
        root,manifest,identity=p.verified_manifest(cfg,version)
        matched=[(source_path,ident) for source_path,logical,ident in p.publication_uploads(root,manifest,identity) if logical==name]
        if (value['version_path']!='manifest.json' or value['sha256']!=anchor['version_manifest_sha256']
            or {'sha256':value['sha256'],'bytes':value['bytes']}!=identity
            or len(matched)!=1 or matched[0]!=(root/'manifest.json',identity)):
            fail('DRIVE_RESOLUTION_DENIED','Approved manifest replacement differs from the verified version upload plan')
    from paper_studio.provider import jobs
    original_job=jobs.job_path(cfg,value['original_build_id'])
    if (sha(safe_read(original_job,'request.json',4*1024*1024))!=value['original_job_request_sha256']
        or sha(safe_read(original_job,'status.json',4*1024*1024))!=value['original_job_status_sha256']):
        fail('DRIVE_RESOLUTION_DENIED','Original publication job changed after local approval')
    states=ledger.setdefault('replacement_state',{})
    if set(states)-{name}:fail('DRIVE_RESOLUTION_DENIED','Unknown replacement slots exist')
    expected={k:value[k] for k in ('name','remote_name','version_path','sha256','bytes','md5')}
    expected['resolution_sha256']=pin['sha256']
    entry=states.setdefault(name,{**expected,'attempted':False})
    if any(entry.get(k)!=v for k,v in expected.items()):fail('DRIVE_RESOLUTION_DENIED','Replacement state identity changed')
    effective=dict(ledger['entries']);effective[name]=entry
    return effective,{name:value}


def upload_replacement(drive,path,folder,identity,entry,persist):
    """Use the replacement's OWN persisted generated ID, never the old attempt."""
    from paper_studio.drive import publisher as p
    remote=entry['remote_name']
    if entry.get('attempted') and not entry.get('preallocated_id'):
        fail('DRIVE_RESOLUTION_DENIED','Replacement attempt has no durable generated identity')
    if not entry.get('preallocated_id'):
        if p._find_attempted_file(drive,remote,folder,allow_absent=True) is not None:
            fail('DRIVE_RESOLUTION_DENIED','A different object occupies the approved replacement name')
        value=drive.request('GET','drive/v3/files/generateIds?count=1&space=drive&type=files')
        ids=value.get('ids')
        if not isinstance(ids,list) or len(ids)!=1:fail('DRIVE_PROTOCOL','One generated replacement ID required')
        entry['preallocated_id']=p._drive_id(ids[0]);persist({'event':'replacement_id_reserved','name':entry['name'],'id':ids[0]})
    ident=p._drive_id(entry['preallocated_id'])
    if entry.get('id') not in (None,ident):fail('DRIVE_RESOLUTION_DENIED','Replacement returned a different generated identity')
    try:present=drive.private(ident)
    except StudioError as error:
        if error.code=='DRIVE_HTTP_ERROR' and error.details.get('http_status')==404:present=None
        else:raise
    reused=present is not None
    if not reused:
        occupied=p._find_attempted_file(drive,remote,folder,allow_absent=True)
        if occupied is not None:fail('DRIVE_RESOLUTION_DENIED','Replacement name is occupied while its reserved identity is absent')
        entry['attempted']=True
        persist({'event':'replacement_upload_attempt_marked','name':entry['name'],'id':ident})
        def checkpoint(value):
            entry['last_upload']=value
            if value.get('id') and value['id']!=ident:fail('DRIVE_RESOLUTION_DENIED','Replacement checkpoint identity changed')
            persist()
        value=drive.upload(path,remote,folder,progress=checkpoint,expected_sha256=identity['sha256'],
                           expected_bytes=identity['bytes'],preallocated_file_id=ident)
        if value.get('id')!=ident:fail('DRIVE_RESOLUTION_DENIED','Replacement upload identity changed')
    entry['id']=ident;persist({'event':'replacement_object_observed','name':entry['name'],'id':ident})
    return p._verify_recovery_file(drive,ident,remote,folder,identity,entry['md5']),reused
