"""On-demand, append-only project source mirror and create-only review intake.

Reuses the existing independent OAuth/Drive upload transport. Source files and
frozen versions are never overwritten; uncertain POSTs require reconciliation.
"""
from __future__ import annotations
import fnmatch,hashlib,json,mimetypes,os,re,time,urllib.parse,uuid
from pathlib import Path,PurePosixPath
from paper_studio.drive import publisher
from paper_studio.security.policy import (StudioError,atomic_json,encoded,fail,locked,no_links,
    path_for,project,relpath,safe_read,safe_write_new,sha)

MAX_BYTES=256*1024*1024
REVIEW_SUFFIXES={'.pdf','.md','.txt','.json','.csv','.docx','.pptx','.png','.jpg','.jpeg','.svg'}
BLOCKED_SUFFIXES={'.pt','.pth','.ckpt','.safetensors','.onnx','.h5','.hdf5','.pkl','.pickle','.bin'}
BLOCKED_PARTS={'.git','.svn','__pycache__','node_modules','checkpoints','checkpoint','raw','raw_data','datasets','builds','.fontcache'}
ROLES=['Sources','Figures','References','Reviews','Versions']


def config(cfg):
    value=cfg.get('drive_source_sync')
    if not isinstance(value,dict):fail('DRIVE_SYNC_NOT_CONFIGURED','Project source synchronization requires an explicit configuration')
    allowed={'enabled','root_folder_id','project_name','project_folder_id','project_binding_mode','include','exclude','approved_pdf_paths',
             'max_file_bytes','max_total_bytes','max_files','reviews_dir','max_review_bytes','deadline_seconds'}
    if set(value)-allowed or type(value.get('enabled')) is not bool:fail('CONFIG_INVALID','Unsupported source synchronization configuration')
    result={**{'exclude':[],'approved_pdf_paths':[],'max_file_bytes':32*1024*1024,'max_total_bytes':64*1024*1024,
               'max_files':128,'max_review_bytes':32*1024*1024,'deadline_seconds':120},**value}
    publisher._drive_id(result.get('root_folder_id'))
    if result.get('project_folder_id'):publisher._drive_id(result['project_folder_id'])
    if result.get('project_binding_mode','tagged') not in ('tagged','explicit_id'):fail('CONFIG_INVALID','Project binding mode must be tagged or explicit_id')
    if result.get('project_binding_mode')=='explicit_id' and not result.get('project_folder_id'):
        fail('CONFIG_INVALID','Explicit-ID project binding requires the approved project_folder_id')
    name=result.get('project_name')
    if not isinstance(name,str) or not 1<=len(name)<=160 or any(ord(c)<32 for c in name):fail('CONFIG_INVALID','Bounded project name required')
    include=result.get('include')
    if not isinstance(include,list) or not 1<=len(include)<=64 or any(not isinstance(p,str) for p in include) or len(set(include))!=len(include):fail('CONFIG_INVALID','One to 64 explicit source files or directories required')
    for path in include:relpath(path)
    for key in ['exclude','approved_pdf_paths']:
        if not isinstance(result[key],list) or len(result[key])>64 or any(not isinstance(p,str) or not p or len(p)>1024 for p in result[key]):fail('CONFIG_INVALID','Bounded exclusion and PDF permission lists required')
    for path in result['approved_pdf_paths']:relpath(path)
    for key in ['max_file_bytes','max_total_bytes','max_review_bytes']:
        if type(result[key]) is not int or not 1<=result[key]<=MAX_BYTES:fail('CONFIG_INVALID','Source/review byte budget must not exceed 256 MiB')
    if type(result['max_files']) is not int or not 1<=result['max_files']<=256:fail('CONFIG_INVALID','Source file count must be at most 256')
    if type(result['deadline_seconds']) is not int or not 1<=result['deadline_seconds']<=600:fail('CONFIG_INVALID','Source sync deadline must be one to 600 seconds')
    relpath(result.get('reviews_dir'))
    path_for(cfg,result['reviews_dir']+'/permission-probe.txt',True)
    return result


def excluded(relative,policy):
    path=PurePosixPath(relative)
    return (any(part.casefold() in BLOCKED_PARTS for part in path.parts)
            or path.suffix.casefold() in BLOCKED_SUFFIXES
            or any(fnmatch.fnmatchcase(relative,pattern) for pattern in policy['exclude'])
            or (path.suffix.casefold()=='.pdf' and relative not in policy['approved_pdf_paths']))


def local_plan(cfg,policy):
    root=Path(cfg['project_root']);selected=set();ignored=[]
    for prefix in policy['include']:
        base=no_links(root/prefix)
        if not base.exists():fail('DRIVE_SYNC_SOURCE_MISSING','An explicitly included source is missing',path=prefix)
        if base.is_file():candidates=[prefix]
        elif base.is_dir():
            candidates=[]
            for parent,dirs,files in os.walk(base,followlinks=False):
                accepted=[]
                for name in sorted(dirs):
                    item=Path(parent)/name;relative=item.relative_to(root).as_posix()
                    try:relpath(relative)
                    except StudioError:ignored.append({'path':relative,'reason':'protected_path'});continue
                    if item.is_symlink() or excluded(relative,policy):ignored.append({'path':relative,'reason':'excluded_directory'});continue
                    accepted.append(name)
                dirs[:]=accepted
                candidates.extend((Path(parent)/name).relative_to(root).as_posix() for name in sorted(files))
        else:fail('DRIVE_SYNC_SOURCE_INVALID','Source is not a regular file or directory',path=prefix)
        for relative in candidates:
            try:relpath(relative)
            except StudioError:ignored.append({'path':relative,'reason':'protected_path'});continue
            if excluded(relative,policy):ignored.append({'path':relative,'reason':'excluded_file_or_unapproved_pdf'});continue
            selected.add(relative)
            if len(selected)>policy['max_files']:fail('INPUT_LIMIT','Source synchronization file count exceeds its configured limit')
    records=[];total=0
    for relative in sorted(selected):
        path_for(cfg,relative);raw=safe_read(root,relative,policy['max_file_bytes'])
        if raw.startswith(b'%PDF-') and relative not in policy['approved_pdf_paths']:
            ignored.append({'path':relative,'reason':'unapproved_pdf_content'});continue
        total+=len(raw)
        if total>policy['max_total_bytes']:fail('INPUT_LIMIT','Source synchronization exceeds its configured total byte limit')
        records.append({'path':relative,'bytes':len(raw),'sha256':sha(raw),'md5':hashlib.md5(raw).hexdigest()})
    if not records:fail('DRIVE_SYNC_EMPTY','Explicit source policy selected no eligible files')
    manifest={'schema':'paper-editor.source-snapshot.v1','project_id':cfg['project_id'],'files':records}
    return {'project_id':cfg['project_id'],'snapshot_sha256':sha(encoded(manifest)),'files':records,'bytes':total,
            'excluded':ignored[:128],'excluded_truncated':len(ignored)>128,'manifest':manifest}


ENTRY_SUFFIXES={'.md','.yaml','.yml','.json','.txt'}


def coverage(cfg,plan):
    """Advisory only: root project documents that the explicit include omits.

    The include list stays the sole authority; nothing here changes the plan,
    its snapshot identity or what a sync may upload.
    """
    root=Path(cfg['project_root']);covered={row['path'] for row in plan['files']}
    excluded={row['path'] for row in plan.get('excluded',[])};missing=[]
    for path in sorted(root.iterdir(),key=lambda item:item.name):
        name=path.name
        if path.is_symlink() or not path.is_file() or name.startswith('.') or path.suffix.lower() not in ENTRY_SUFFIXES:continue
        try:relpath(name)
        except StudioError:continue
        if name not in covered and name not in excluded:missing.append(name)
    result={'advisory_only':True,'authority':'explicit_include_list','uncovered_root_documents':missing[:64],
            'uncovered_root_documents_truncated':len(missing)>64}
    if cfg.get('research_state_path'):result['research_state_uncovered']=cfg['research_state_path'] not in covered
    return result


def state_root(cfg):
    root=no_links(Path(cfg['state_root'])/'drive-source-sync');publisher._private_state_directory(root)
    return root


def load_json(path,limit=4*1024*1024):return json.loads(safe_read(path.parent,path.name,limit))
def tags(cfg,role):return {'software_id':'webcodex-paper-studio','project_id':cfg['project_id'],'source_sync_role':role}
def literal(value):return value.replace('\\','\\\\').replace("'","\\'")


def find_folder(drive,cfg,parent,role):
    properties=tags(cfg,role)
    query="trashed=false and mimeType='application/vnd.google-apps.folder' and '"+literal(parent)+"' in parents"
    for key,value in properties.items():query+=" and appProperties has { key='"+literal(key)+"' and value='"+literal(value)+"' }"
    response=drive.request('GET','drive/v3/files?'+urllib.parse.urlencode({'q':query,'fields':'nextPageToken,incompleteSearch,files(id,name)','pageSize':100}))
    rows=response.get('files')
    if not isinstance(rows,list) or response.get('nextPageToken') or response.get('incompleteSearch') or len(rows)>1:
        fail('DRIVE_SYNC_FOLDER_AMBIGUOUS','Project identity did not resolve to a complete unique folder')
    return publisher._drive_id(rows[0].get('id')) if rows else None


def verify_folder(drive,cfg,identity,parent=None,role=None,need_write=False):
    identity=publisher._drive_id(identity);meta=drive.private(identity)
    if meta.get('id')!=identity or meta.get('mimeType')!='application/vnd.google-apps.folder':fail('DRIVE_SYNC_FOLDER_MISMATCH','Configured Drive object is not the expected folder')
    if parent is not None and meta.get('parents')!=[parent]:fail('DRIVE_SYNC_FOLDER_MISMATCH','Drive folder is outside the configured project hierarchy')
    if role is not None and any(meta.get('appProperties',{}).get(k)!=v for k,v in tags(cfg,role).items()):fail('DRIVE_SYNC_FOLDER_MISMATCH','Drive folder appProperties do not identify this project and role')
    if need_write:
        capability=drive.request('GET','drive/v3/files/'+urllib.parse.quote(identity,safe='')+'?fields=id,capabilities(canAddChildren,canEdit)')
        if capability.get('id')!=identity or capability.get('capabilities',{}).get('canAddChildren') is not True:fail('DRIVE_SYNC_WRITE_DENIED','Publisher cannot add files to the explicitly configured target folder')
    return meta


def client(cfg,policy):
    oauth=cfg.get('drive_oauth_file')
    if not oauth:fail('OAUTH_REQUIRED','Source synchronization requires the existing independent Publisher OAuth binding')
    return publisher.RecoveryDrive(oauth,persist_access_token=False,deadline=time.monotonic()+policy['deadline_seconds'])


def _binding_metadata(drive,identity):
    """One metadata revision supplies identity, privacy and PATCH precondition."""
    url=publisher.API_ROOT+'drive/v3/files/'+urllib.parse.quote(identity,safe='')+'?'+urllib.parse.urlencode({
        'fields':'id,name,mimeType,parents,trashed,permissions(type,role),appProperties,capabilities(canEdit,canAddChildren)'})
    _,headers,raw=drive.exchange('GET',url,allowed=(200,))
    value=publisher.Drive.decode(raw)
    if value.get('id')!=identity or value.get('mimeType')!='application/vnd.google-apps.folder' or value.get('trashed') is not False:
        fail('DRIVE_SYNC_FOLDER_MISMATCH','Explicit binding target must be the existing untrashed Drive folder')
    permissions=value.get('permissions')
    if not isinstance(permissions,list) or not permissions or any(not isinstance(p,dict) or p.get('type')!='user' or p.get('role')!='owner' for p in permissions):
        fail('DRIVE_NOT_PRIVATE','Explicit binding requires known owner-only folder permissions')
    capabilities=value.get('capabilities')
    if not isinstance(capabilities,dict) or any(capabilities.get(key) is not True for key in ['canEdit','canAddChildren']):
        fail('DRIVE_SYNC_WRITE_DENIED','Explicit binding requires canEdit and canAddChildren')
    properties=value.get('appProperties',{})
    if not isinstance(properties,dict) or any(not isinstance(k,str) or not isinstance(v,str) for k,v in properties.items()):
        fail('DRIVE_SYNC_FOLDER_MISMATCH','Folder appProperties must be a string map')
    etag=next((v for k,v in headers.items() if k.casefold()=='etag'),None)
    return value,etag


def bind_existing_project_folder(cfg,*,project_folder_id,expected_parent_id,expected_name,
                                 dry_run=True,authorized=False,request_id=None,drive=None):
    """Management-only, explicit-ID adoption; never creates or traverses folders.

    Default dry-run performs only GETs. Apply atomically merges our three tags
    using an ETag and records the attempt before PATCH. Unknown outcomes are
    reconciled by ID, never retried blindly, even under a different request ID.
    The caller must separately persist the pinned ID in its project config.
    """
    policy=config(cfg);identity=publisher._drive_id(project_folder_id);parent=publisher._drive_id(expected_parent_id)
    if type(dry_run) is not bool or type(authorized) is not bool:fail('INVALID_ARGUMENT','dry_run and authorized must be boolean')
    if identity==parent or parent!=policy['root_folder_id'] or expected_name!=policy['project_name'] or policy.get('project_folder_id') not in (None,identity):
        fail('DRIVE_SYNC_FOLDER_MISMATCH','Explicit folder ID, parent and name must match the configured project binding')
    if not dry_run:
        if authorized is not True:fail('AUTHORIZATION_REQUIRED','Existing-folder binding requires authorized=true')
        if not isinstance(request_id,str) or not re.fullmatch('[A-Za-z0-9_-]{1,64}',request_id):fail('INVALID_ARGUMENT','Binding apply requires a bounded stable request_id')
    drive=drive or client(cfg,policy);wanted=tags(cfg,'project')
    anchor={'schema':'paper-editor.source-folder-binding.v1','project_id':cfg['project_id'],
            'project_folder_id':identity,'expected_parent_id':parent,'expected_name':expected_name,'tags':wanted}
    def inspect():
        meta,etag=_binding_metadata(drive,identity)
        if meta.get('parents')!=[parent] or meta.get('name')!=expected_name:
            fail('DRIVE_SYNC_FOLDER_MISMATCH','Explicit folder parent or name differs from the approved identity')
        current=meta.get('appProperties',{})
        if any(key in current and current[key]!=value for key,value in wanted.items()):
            fail('DRIVE_SYNC_BINDING_CONFLICT','Existing appProperties already bind this folder to a conflicting software, project or role')
        found=find_folder(drive,cfg,parent,'project')
        if found not in (None,identity):fail('DRIVE_SYNC_BINDING_CONFLICT','Another folder already carries this project identity under the approved parent')
        return meta,etag,all(current.get(k)==v for k,v in wanted.items())
    def public(status,complete,**extra):
        return {**anchor,'status':status,'dry_run':dry_run,'binding_complete':complete,'sharing_changed':False,
                'children_touched':False,**extra}
    if dry_run:
        meta,etag,complete=inspect()
        return public('verified_existing' if complete else 'would_bind_existing',complete,
                      would_patch=not complete,conditional_update_available=bool(etag))
    with locked(cfg,'drive-source-sync'):
        private=state_root(cfg)/'bindings';publisher._private_state_directory(private)
        path=private/(sha(identity.encode())+'.json');receipt=load_json(path) if path.exists() else {**anchor,'attempted':False,'requests':[]}
        if any(receipt.get(k)!=v for k,v in anchor.items()):fail('IDEMPOTENCY_CONFLICT','Existing folder attempt belongs to another approved binding')
        # The request index also prevents the same request from adopting another
        # folder when the local configuration changes between invocations.
        index=private/('request-'+sha(request_id.encode())+'.json')
        if index.exists() and load_json(index)!=anchor:fail('IDEMPOTENCY_CONFLICT','Binding request_id belongs to a different folder or project')
        meta,etag,complete=inspect()
        if complete:
            if any(meta.get('appProperties',{}).get(k)!=v for k,v in receipt.get('before_properties',{}).items()):
                fail('DRIVE_SYNC_BINDING_CONFLICT','Binding reconciliation did not preserve the original unrelated appProperties')
            if receipt.get('status')!='completed':
                receipt.update(status='completed',binding_complete=True,reconciled=bool(receipt['attempted']))
                receipt.pop('error',None);atomic_json(path,receipt)
            if not index.exists():atomic_json(index,anchor,True)
            return public('completed',True,reconciled=receipt.get('reconciled',False))
        if receipt['attempted']:fail('DRIVE_SYNC_BINDING_UNCERTAIN','Previous PATCH has no verified binding; refusing another mutation pending reconciliation')
        if not isinstance(etag,str) or not etag or '\r' in etag or '\n' in etag:
            fail('DRIVE_SYNC_PRECONDITION_REQUIRED','Existing-folder binding requires a returned ETag for conditional PATCH')
        if not index.exists():atomic_json(index,anchor,True)
        receipt.update(attempted=True,status='patching',requests=[request_id],before_properties=meta.get('appProperties',{}))
        atomic_json(path,receipt)
        failure=None
        try:
            drive.exchange('PATCH',publisher.API_ROOT+'drive/v3/files/'+urllib.parse.quote(identity,safe='')+'?fields=id',
                           json.dumps({'appProperties':{**meta.get('appProperties',{}),**wanted}}).encode(),
                           {'Content-Type':'application/json','If-Match':etag},allowed=(200,))
        except StudioError as error:failure=error
        except Exception:failure=StudioError('DRIVE_SYNC_BINDING_UNCERTAIN','Conditional PATCH outcome is unknown; metadata reconciliation is required')
        try:
            after,_,complete=inspect()
            if not complete or any(after.get('appProperties',{}).get(k)!=v for k,v in meta.get('appProperties',{}).items()):
                fail('DRIVE_SYNC_BINDING_UNCERTAIN','PATCH outcome did not preserve and verify the approved project tags')
        except StudioError as error:
            detail=publisher.public_receipt(error.as_dict())
            receipt.setdefault('error_history',[]).append({'at':time.time(),'error':detail})
            receipt['error_history']=receipt['error_history'][-128:]
            receipt.update(status='recovery_required',binding_complete=False,error=detail)
            atomic_json(path,receipt)
            if failure is not None:raise failure
            raise
        receipt.update(status='completed',binding_complete=True,reconciled=failure is not None)
        receipt.pop('error',None);atomic_json(path,receipt)
        return public('completed',True,reconciled=failure is not None)


def remote_plan(cfg,policy,drive):
    root=verify_folder(drive,cfg,policy['root_folder_id'],need_write=True)
    found=find_folder(drive,cfg,root['id'],'project');project_id=policy.get('project_folder_id') or found
    if policy.get('project_binding_mode')=='explicit_id':
        verify_explicit_project_folder(cfg,policy,drive,found)
    else:
        if project_id and found!=project_id:fail('DRIVE_SYNC_FOLDER_MISMATCH','Pinned project folder is not the unique appProperties match under the configured root')
        if project_id:verify_folder(drive,cfg,project_id,root['id'],'project',True)
    return {'root_folder_id':root['id'],'publisher_can_add_children':True,'project_folder_id':project_id,
            'project_folder_state':'verified_existing' if project_id else 'would_create_new_tagged_project_folder',
            **({'project_binding_mode':'explicit_id','project_metadata_mutated':False} if policy.get('project_binding_mode')=='explicit_id' else {})}


def verify_explicit_project_folder(cfg,policy,drive,found):
    """The configured ID is the project identity; names only corroborate it.

    This path never adopts by name and never writes project-folder metadata.
    Child roles continue to require their own exact appProperties identities.
    """
    identity=policy['project_folder_id']
    if found not in (None,identity):fail('DRIVE_SYNC_BINDING_CONFLICT','Another folder carries this project identity under the approved parent')
    meta,_=_binding_metadata(drive,identity)
    if meta.get('parents')!=[policy['root_folder_id']] or meta.get('name')!=policy['project_name']:
        fail('DRIVE_SYNC_FOLDER_MISMATCH','Explicit project folder differs from the configured ID, parent or name')
    if any(k in meta.get('appProperties',{}) and meta['appProperties'][k]!=v for k,v in tags(cfg,'project').items()):
        fail('DRIVE_SYNC_BINDING_CONFLICT','Explicit project folder has conflicting software, project or role tags')
    return meta


def ensure_folders(cfg,policy,drive,private):
    anchor={'project_id':cfg['project_id'],'root_folder_id':policy['root_folder_id']}
    path=private/'folders.json';ledger=load_json(path) if path.exists() else {**anchor,'roles':{}}
    if any(ledger.get(k)!=v for k,v in anchor.items()):fail('DRIVE_SYNC_IDENTITY_CHANGED','Stored Drive binding belongs to another project/root')
    verify_folder(drive,cfg,policy['root_folder_id'],need_write=True)
    def ensure(role,parent,name,pinned=None):
        entry=ledger['roles'].setdefault(role,{})
        known=entry.get('id')
        if pinned and known and pinned!=known:fail('DRIVE_SYNC_FOLDER_MISMATCH','Configured folder ID differs from the preserved project binding')
        identity=pinned or known
        found=find_folder(drive,cfg,parent,role)
        if role=='project' and policy.get('project_binding_mode')=='explicit_id':
            verify_explicit_project_folder(cfg,policy,drive,found)
            entry.update(id=identity,attempted=True,binding_mode='explicit_id');atomic_json(path,ledger)
            return identity
        if identity:
            if found!=identity:fail('DRIVE_SYNC_FOLDER_AMBIGUOUS','Stored folder ID is not the unique project appProperties match')
        elif found:identity=found
        elif entry.get('attempted'):fail('DRIVE_SYNC_RECONCILIATION_REQUIRED','A previous folder POST has no unique visible object; refusing a second create')
        else:
            entry['attempted']=True;atomic_json(path,ledger)
            value=drive.request('POST','drive/v3/files?fields=id',{'name':name,'parents':[parent],
                'mimeType':'application/vnd.google-apps.folder','appProperties':tags(cfg,role)})
            identity=publisher._drive_id(value.get('id'));entry['id']=identity;atomic_json(path,ledger)
        verify_folder(drive,cfg,identity,parent,role,True)
        entry['id']=identity;entry['attempted']=True;atomic_json(path,ledger)
        return identity
    project_id=ensure('project',policy['root_folder_id'],policy['project_name'],policy.get('project_folder_id'))
    result={'project':project_id}
    for role in ROLES:result[role]=ensure(role,project_id,role)
    return result


def object_name(entry):
    path=PurePosixPath(entry['path']);stem=re.sub('[^A-Za-z0-9_.-]','_',path.stem)[:64] or 'source'
    return 's_'+entry['sha256']+'_'+sha(entry['path'].encode())[:16]+'_'+stem+path.suffix.lower()


def category(path):
    parts=PurePosixPath(path).parts
    if 'figures' in parts or PurePosixPath(path).suffix.lower() in {'.png','.jpg','.jpeg','.svg','.pptx'}:return 'Figures'
    if 'references' in parts or path.endswith('.bib'):return 'References'
    return 'Sources'


def upload_or_reconcile(drive,path,name,folder,identity,entry,persist,index=None):
    """Reuse one verified object or upload once; index is a complete folder listing.

    With an index, an existing object costs no per-file GET. Absence in a
    complete listing has the same meaning as an empty exact-name query.
    """
    known=entry.get('id');found=publisher._reconcile_id(drive,index,name,folder,allow_absent=not entry.get('attempted'))
    if known and found!=known:fail('DRIVE_SYNC_OBJECT_MISMATCH','Recorded file ID differs from the unique visible source object')
    if found:
        verified=publisher._verify_known_file(drive,index,found,name,folder,identity,identity['md5'])
        entry.update(id=found,verified=True);persist();return {**verified,'sync_action':'reused'}
    entry['attempted']=True;persist()
    def checkpoint(value):
        entry['last_upload']=value
        if value.get('id'):entry['id']=publisher._drive_id(value['id'])
        persist()
    uploaded=drive.upload(path,name,folder,progress=checkpoint,expected_sha256=identity['sha256'],expected_bytes=identity['bytes'])
    entry['id']=publisher._drive_id(uploaded.get('id'));persist()
    verified=publisher._verify_recovery_file(drive,entry['id'],name,folder,identity,identity['md5'])
    entry.update(verified=True);persist();return {**verified,'sync_action':'uploaded'}


def public_sync(value):
    allowed=['schema','project_id','request_id','snapshot_sha256','status','project_folder_id','files','manifest','uploaded_count',
             'reused_count','sync_complete','error','destination','remote_file_id','bytes','sha256','md5','import_complete',
             'requested_destination','remote_relative_path','reviews_folder_id','source_identity','reused','origin_request_id']
    return publisher.public_receipt({key:value[key] for key in allowed if key in value})

def save_operation_receipt(cfg,path,receipt,namespace,exclusive=False):
    from paper_studio.provider import job_index
    if exclusive:
        job_index.track_operation(cfg,namespace,receipt['request_id'],path.name,'preparing')
    atomic_json(path,receipt,exclusive)
    job_index.track_operation(cfg,namespace,receipt['request_id'],path.name,receipt['status'])


def shared_object_entry(cfg,private,folder,name,identity):
    """One durable attempt binding across every request and source snapshot."""
    directory=private/'objects';publisher._private_state_directory(directory)
    key=sha(encoded([folder,name]));path=directory/(key+'.json')
    anchor={'project_id':cfg['project_id'],'folder_id':folder,'name':name,
            **{field:identity[field] for field in ['bytes','sha256','md5']}}
    if path.exists():
        entry=load_json(path)
        if any(entry.get(field)!=value for field,value in anchor.items()):fail('DRIVE_SYNC_OBJECT_MISMATCH','Shared upload attempt is bound to a different immutable object')
    else:
        entry={'schema':'paper-editor.source-object-attempt.v1',**anchor,'attempted':False}
        atomic_json(path,entry,True)
    return key,path,entry


def sync(cfg,args,policy):
    if args.get('dry_run',True) is True:
        plan=local_plan(cfg,policy);drive=client(cfg,policy)
        return {**{k:v for k,v in plan.items() if k!='manifest'},'coverage':coverage(cfg,plan),'dry_run':True,
                'remote':remote_plan(cfg,policy,drive),'enabled':policy['enabled']}
    if policy['enabled'] is not True or args.get('authorized') is not True:fail('AUTHORIZATION_REQUIRED','Source sync apply requires enabled policy and authorized=true')
    request_id=args.get('request_id');expected=args.get('expected_snapshot_sha256')
    if not isinstance(request_id,str) or not re.fullmatch('[A-Za-z0-9_-]{1,64}',request_id):fail('INVALID_ARGUMENT','Sync requires a bounded stable request_id')
    if not isinstance(expected,str) or not re.fullmatch('[0-9a-f]{64}',expected):fail('REVISION_REQUIRED','Sync apply requires the exact snapshot SHA256 returned by plan')
    with locked(cfg,'drive-source-sync'):
        private=state_root(cfg);requests=private/'requests';publisher._private_state_directory(requests)
        record_path=requests/(sha(request_id.encode())+'.json')
        if (private/'imports'/record_path.name).exists():fail('IDEMPOTENCY_CONFLICT','Request ID already belongs to a review import')
        anchor={'project_id':cfg['project_id'],'request_id':request_id,'snapshot_sha256':expected,'policy_sha256':sha(encoded(policy))}
        if record_path.exists():
            receipt=load_json(record_path)
            if any(receipt.get(k)!=v for k,v in anchor.items()):fail('IDEMPOTENCY_CONFLICT','Sync request_id is bound to a different project/policy/snapshot')
        else:
            plan=local_plan(cfg,policy)
            if plan['snapshot_sha256']!=expected:fail('REVISION_CONFLICT','Current project sources differ from the approved synchronization plan')
            snapshot=no_links(Path(cfg['artifact_root'])/'source-sync'/expected/'source')
            snapshot.mkdir(parents=True,exist_ok=True)
            for entry in plan['files']:
                raw=safe_read(cfg['project_root'],entry['path'],policy['max_file_bytes'])
                if len(raw)!=entry['bytes'] or sha(raw)!=entry['sha256']:fail('REVISION_CONFLICT','Project source changed during snapshot capture',path=entry['path'])
                dest=snapshot/entry['path']
                if dest.exists():
                    if safe_read(snapshot,entry['path'],policy['max_file_bytes'])!=raw:fail('REVISION_CONFLICT','Existing immutable source snapshot differs')
                else:safe_write_new(snapshot,entry['path'],raw);dest.chmod(0o400)
            receipt={'schema':'paper-editor.source-sync.v1',**anchor,'status':'prepared','sync_complete':False,
                     'source_files':plan['files'],'entries':{},'files':[],'created_at':time.time()}
            save_operation_receipt(cfg,record_path,receipt,'sync_engine',True)
        snapshot=no_links(Path(cfg['artifact_root'])/'source-sync'/expected/'source')
        def persist():save_operation_receipt(cfg,record_path,receipt,'sync_engine')
        indexes={}
        def index_for(folder):
            # One complete listing per role folder per attempt. Objects created
            # later in this attempt are verified individually after upload.
            if folder not in indexes:indexes[folder]=publisher._try_folder_index(drive,folder)
            return indexes[folder]
        def transfer(path,name,folder,identity,request_key):
            key,shared_path,entry=shared_object_entry(cfg,private,folder,name,identity)
            def persist_shared():
                # Global attempts/IDs are durable before a per-request receipt
                # can advance, so changing request_id cannot forget a POST.
                atomic_json(shared_path,entry)
                receipt['entries'][request_key]={'object_attempt_key':key,'attempted':entry.get('attempted',False)}
                if entry.get('id'):receipt['entries'][request_key]['id']=entry['id']
                persist()
            return upload_or_reconcile(drive,path,name,folder,identity,entry,persist_shared,index_for(folder))
        completed_before=receipt.get('sync_complete') is True and receipt.get('status')=='completed'
        try:
            drive=client(cfg,policy);folders=ensure_folders(cfg,policy,drive,private)
            if receipt.get('sync_complete') is True and receipt.get('status')=='completed':
                # Recheck immutable remote identities without rewriting the
                # completed request or moving a newer current pointer backward.
                for source,remote in zip(receipt['source_files'],receipt['files']):
                    if source['path']!=remote.get('source_path'):fail('DRIVE_SYNC_OBJECT_MISMATCH','Completed source mapping order changed')
                    raw=safe_read(snapshot,source['path'],policy['max_file_bytes'])
                    if len(raw)!=source['bytes'] or sha(raw)!=source['sha256']:fail('REVISION_CONFLICT','Completed source snapshot identity changed')
                    folder=folders[category(source['path'])]
                    publisher._verify_known_file(drive,index_for(folder),remote['id'],object_name(source),folder,source,source['md5'])
                if len(receipt['source_files'])!=len(receipt['files']):fail('DRIVE_SYNC_OBJECT_MISMATCH','Completed source mapping count changed')
                member=receipt['manifest'];raw=safe_read(snapshot.parent,'sync-manifest.json',4*1024*1024)
                identity={'bytes':len(raw),'sha256':sha(raw),'md5':hashlib.md5(raw).hexdigest()}
                publisher._verify_known_file(drive,index_for(folders['Sources']),member['id'],'sync-manifest.'+expected+'.json',folders['Sources'],identity,identity['md5'])
                return public_sync(receipt)
            receipt['project_folder_id']=folders['project'];receipt.update(status='syncing',files=[]);persist()
            for entry in receipt['source_files']:
                raw=safe_read(snapshot,entry['path'],policy['max_file_bytes'])
                if len(raw)!=entry['bytes'] or sha(raw)!=entry['sha256']:fail('REVISION_CONFLICT','Immutable source snapshot identity changed')
                remote=transfer(snapshot/entry['path'],object_name(entry),folders[category(entry['path'])],entry,entry['path'])
                receipt['files'].append({**remote,'source_path':entry['path']});persist()
            manifest={'schema':'paper-editor.source-mirror.v1','project_id':cfg['project_id'],'snapshot_sha256':expected,
                      'project_folder_id':folders['project'],'files':[{k:item[k] for k in ['source_path','id','bytes','sha256','md5','url']} for item in receipt['files']]}
            manifest_raw=encoded(manifest);manifest_path=snapshot.parent/'sync-manifest.json'
            if manifest_path.exists():
                if safe_read(snapshot.parent,'sync-manifest.json',4*1024*1024)!=manifest_raw:fail('DRIVE_SYNC_MANIFEST_CONFLICT','Immutable snapshot manifest differs from its verified remote mapping')
            else:safe_write_new(snapshot.parent,'sync-manifest.json',manifest_raw);manifest_path.chmod(0o400)
            identity={'bytes':len(manifest_raw),'sha256':sha(manifest_raw),'md5':hashlib.md5(manifest_raw).hexdigest()}
            receipt['manifest']=transfer(manifest_path,'sync-manifest.'+expected+'.json',folders['Sources'],identity,'@manifest')
            receipt.pop('error',None)
            receipt.update(status='completed',sync_complete=True,uploaded_count=sum(item['sync_action']=='uploaded' for item in receipt['files']),
                           reused_count=sum(item['sync_action']=='reused' for item in receipt['files']))
            persist();atomic_json(private/'current.json',{'project_id':cfg['project_id'],'request_id':request_id,'snapshot_sha256':expected,'receipt_file':record_path.name})
            return public_sync(receipt)
        except Exception as error:
            detail=publisher.public_receipt(error.as_dict()) if isinstance(error,StudioError) else {'code':'DRIVE_SYNC_FAILED','message':'Source synchronization failed; retained progress must be reconciled before retry'}
            if completed_before:
                audits=private/'verification-failures';publisher._private_state_directory(audits)
                atomic_json(audits/(uuid.uuid4().hex+'.json'),{**anchor,'status':'verification_failed','error':detail},True)
                raise
            receipt.setdefault('error_history',[]).append({'at':time.time(),'error':detail})
            receipt['error_history']=receipt['error_history'][-128:]
            receipt.update(status='partial_sync_retained',sync_complete=False,error=detail)
            persist();raise


def remote_review(cfg,policy,drive,remote_id):
    root=policy['root_folder_id'];verify_folder(drive,cfg,root)
    project_id=policy.get('project_folder_id') or find_folder(drive,cfg,root,'project')
    if project_id is None:fail('DRIVE_SYNC_FOLDER_REQUIRED','Project Drive folder has not been provisioned')
    if policy.get('project_binding_mode')=='explicit_id':
        verify_explicit_project_folder(cfg,policy,drive,find_folder(drive,cfg,root,'project'))
    else:verify_folder(drive,cfg,project_id,root,'project')
    reviews=find_folder(drive,cfg,project_id,'Reviews')
    if reviews is None:fail('DRIVE_REVIEWS_FOLDER_REQUIRED','Verified project Reviews folder is missing')
    review_folder=verify_folder(drive,cfg,reviews,project_id,'Reviews')
    meta=drive.private(publisher._drive_id(remote_id))
    if meta.get('id')!=remote_id or meta.get('mimeType')=='application/vnd.google-apps.folder' or meta.get('trashed') is True:
        fail('PROJECT_DENIED','Review identity is not an available binary file')
    # Descend only by following this exact file's single-parent ancestry back
    # to the verified project Reviews role. Names never establish authority.
    def component(value):
        if (not isinstance(value,str) or not value or value in {'.','..'} or '/' in value or '\\' in value
            or any(ord(char)<32 for char in value)):
            fail('DRIVE_REVIEW_UNSUPPORTED','Review names must be literal path components')
        return value
    parts=[component(meta.get('name'))];parent=meta.get('parents');seen={remote_id}
    for _ in range(16):
        if not isinstance(parent,list) or len(parent)!=1:fail('PROJECT_DENIED','Review requires one unambiguous Reviews ancestry')
        identity=publisher._drive_id(parent[0])
        if identity==reviews:break
        if identity in seen or identity in {project_id,root}:fail('PROJECT_DENIED','Review is outside this project Reviews subtree')
        seen.add(identity);folder=drive.private(identity)
        if (folder.get('id')!=identity or folder.get('mimeType')!='application/vnd.google-apps.folder'
            or folder.get('trashed') is True):fail('PROJECT_DENIED','Review ancestor is not an available folder')
        properties=folder.get('appProperties',{})
        if (not isinstance(properties,dict)
            or properties.get('project_id',cfg['project_id'])!=cfg['project_id']
            or properties.get('software_id','webcodex-paper-studio')!='webcodex-paper-studio'
            or properties.get('source_sync_role','Reviews')!='Reviews'):
            fail('PROJECT_DENIED','Review ancestor carries a conflicting project or role identity')
        parts.append(component(folder.get('name')));parent=folder.get('parents')
    else:fail('PROJECT_DENIED','Review ancestry exceeds the bounded project subtree depth')
    try:size=int(meta.get('size',-1))
    except (ValueError,TypeError):size=-1
    if not 0<=size<=policy['max_review_bytes'] or not re.fullmatch('[0-9a-fA-F]{32}',meta.get('md5Checksum','')):fail('DRIVE_REVIEW_UNSUPPORTED','Only bounded binary review files with server MD5 identity may be imported')
    if PurePosixPath(meta.get('name','')).suffix.lower() not in REVIEW_SUFFIXES:fail('DRIVE_REVIEW_UNSUPPORTED','Review source extension is outside the create-only intake policy')
    return {**meta,'_review_source':{'project_folder_id':project_id,'reviews_folder_id':reviews,
            'remote_relative_path':component(review_folder.get('name'))+'/'+'/'.join(reversed(parts))}},size


def import_review(cfg,args,policy):
    from paper_studio.drive.review_import import import_review as execute
    return execute(cfg,args,policy)


def dispatch(cfg,args):
    project(cfg,args.get('project_id'));policy=config(cfg);action=args.get('action')
    common={'project_id','action'}
    allowed={'plan':common|{'check_remote'},'sync':common|{'request_id','dry_run','authorized','expected_snapshot_sha256'},
             'status':common|{'request_id'},'import_review':common|{'request_id','remote_file_id','destination','dry_run','authorized'}}
    if action not in allowed or set(args)-allowed[action]:fail('INVALID_ARGUMENT','Unsupported source sync action or action-specific fields')
    if type(args.get('dry_run',True)) is not bool or type(args.get('authorized',False)) is not bool:fail('INVALID_ARGUMENT','dry_run and authorized must be boolean')
    if action=='plan':
        if type(args.get('check_remote',True)) is not bool:fail('INVALID_ARGUMENT','check_remote must be boolean')
        plan=local_plan(cfg,policy);result={k:v for k,v in plan.items() if k!='manifest'}
        result['coverage']=coverage(cfg,plan)
        if args.get('check_remote',True):result['remote']=remote_plan(cfg,policy,client(cfg,policy))
        return {**result,'dry_run':True,'enabled':policy['enabled']}
    if action=='sync':return sync(cfg,args,policy)
    if action=='import_review':return import_review(cfg,args,policy)
    if action=='status':
        private=no_links(Path(cfg['state_root'])/'drive-source-sync');request_id=args.get('request_id')
        if request_id is not None:
            if not isinstance(request_id,str) or not re.fullmatch('[A-Za-z0-9_-]{1,64}',request_id):fail('INVALID_ARGUMENT','Invalid sync request_id')
            path=private/'requests'/(sha(request_id.encode())+'.json')
            imported=private/'imports'/path.name
            if path.exists() and imported.exists():fail('IDEMPOTENCY_CONFLICT','Request ID is ambiguous between source synchronization and review import')
            if imported.exists():path=imported
        else:
            current=private/'current.json'
            if not current.exists():return {'project_id':cfg['project_id'],'status':'not_synchronized','sync_complete':False}
            pointer=load_json(current)
            if pointer.get('project_id')!=cfg['project_id'] or not re.fullmatch(r'[0-9a-f]{64}\.json',pointer.get('receipt_file','')):fail('PROJECT_DENIED','Current sync pointer identity differs from this project')
            path=private/'requests'/pointer['receipt_file']
        if not path.exists():fail('NOT_FOUND','Source synchronization request is unknown')
        value=load_json(path)
        if value.get('project_id')!=cfg['project_id']:fail('PROJECT_DENIED','Sync receipt belongs to a different project')
        return public_sync(value)
    fail('INVALID_ARGUMENT','Unknown source synchronization action')
