"""Project-scoped orchestration for Zotero 10 Local API via Pyzotero.

The SDK transport owns HTTP and local credentials. This module owns explicit
source/collection authorization, durable request identity and response-loss
reconciliation. It never calls the Connector or silently updates an old item.
"""
from __future__ import annotations
import contextlib,fcntl,json,os,re,secrets,time,uuid
from pathlib import Path
from paper_studio.security.policy import StudioError,atomic_json,encoded,fail,no_links,safe_read,sha
from paper_studio.references.zotero_import import validate_binding
from paper_studio.references.zotero_import_worker import parse_source,differences
from paper_studio.references.zotero_worker import CITE,KEY,normalize_doi

REQUEST=re.compile(r'[A-Za-z0-9_-]{1,64}\Z')
ALPHABET='23456789ABCDEFGHJKLMNPQRSTUVWXYZ'
SAFE_REJECT={400,401,403,409,412,428,429}


def _private(path):
    path=no_links(Path(path));path.mkdir(parents=True,exist_ok=True,mode=0o700)
    stat=path.stat()
    if stat.st_uid!=os.getuid() or stat.st_mode&0o077:
        fail('ZOTERO_STATE_PERMISSIONS','Zotero transaction state must be private and owned')
    return path


def _save(path,value):
    atomic_json(path,value)
    fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:os.fsync(fd)
    finally:os.close(fd)


def _read(path):
    return json.loads(safe_read(path.parent,path.name,256*1024)) if path.exists() else None


@contextlib.contextmanager
def _database_lock(cfg,server_id,deadline=None):
    # Shared by projects using sibling state roots. Serialize the whole local
    # database so collection creation for different DOIs cannot race each other.
    state=_private(cfg['state_root'])
    shared=_private(state.parent/'zotero-local-coordination')
    shared=_private(shared/sha(server_id.encode()))
    fd=os.open(shared/'database.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        end=deadline if deadline is not None else time.monotonic()+40
        while True:
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);break
            except BlockingIOError:
                remaining=end-time.monotonic()
                if remaining<=0:fail('ZOTERO_BUSY','Another local database transaction exceeded the request wait budget')
                time.sleep(min(0.05,remaining))
        yield shared
    finally:os.close(fd)


def _backend(cfg,backend):
    deadline=time.monotonic()+40
    if backend is None:
        from paper_studio.references.zotero_transport import call
        backend=lambda operation,payload:call(cfg,operation,payload,budget_seconds=max(.1,deadline-time.monotonic()))
    else:backend=backend if callable(backend) else backend.call
    def bounded(operation,payload):
        if time.monotonic()>=deadline:fail('ZOTERO_DEADLINE','Local orchestration total request deadline exceeded')
        result=backend(operation,payload)
        if time.monotonic()>=deadline:fail('ZOTERO_DEADLINE','Local operation exceeded the orchestration deadline; no next request was sent')
        return result
    bounded.deadline=deadline
    return bounded


def _call(backend,operation,payload,server_id=None):
    result=backend(operation,{**payload,**({'server_id':server_id} if server_id else {})})
    if not isinstance(result,dict):fail('ZOTERO_BACKEND_PROTOCOL','Local SDK returned a non-object result')
    if server_id is not None and result.get('server_id')!=server_id:
        fail('ZOTERO_SERVER_ID_MISMATCH','Zotero database identity changed; no request was replayed')
    return result


def _status(cfg,backend):
    config=cfg.get('zotero_backend')
    if not isinstance(config,dict) or config.get('backend')!='pyzotero_local':
        fail('ZOTERO_BACKEND_NOT_CONFIGURED','This project has no explicit Pyzotero local backend')
    actual=_call(backend,'status',{})
    version=actual.get('zotero_version')
    match=re.match(r'^(\d+)\.',version or '') if isinstance(version,str) else None
    if match is None:fail('ZOTERO_BACKEND_PROTOCOL','Local SDK did not return the Zotero version')
    major=int(match.group(1));sid=actual.get('server_id')
    if major>=10 and (not isinstance(sid,str) or not 1<=len(sid)<=128):
        fail('ZOTERO_BACKEND_PROTOCOL','Zotero 10 must return a bounded server ID')
    configured=config.get('server_id')
    if configured is not None and configured!=sid:
        fail('ZOTERO_SERVER_ID_MISMATCH','Configured server ID differs from this Zotero database')
    sdk=actual.get('sdk',{})
    public_sdk={k:sdk[k] for k in ['name','version'] if isinstance(sdk,dict) and isinstance(sdk.get(k),str)}
    # A GET cannot prove that a local write key is still valid. Ready means
    # explicitly bound; the actual write's 401/403 is authoritative.
    ready=major>=10 and configured==sid and configured is not None and bool(config.get('credential_path')) and actual.get('write_ready') is True
    result={'project_id':cfg['project_id'],'status':'ready','read_ready':True,'write_ready':ready,
            'blocked_version':major<10,'zotero_version':version,'server_id':sid,'sdk':public_sdk,
            'backend':'pyzotero_local','update_supported':False,'import_calls':0,'collection_writes':0}
    if major<10:result['code']='ZOTERO_UPGRADE_REQUIRED'
    elif not ready:result['code']='ZOTERO_LOCAL_WRITE_NOT_AUTHORIZED'
    return result


def _scope(cfg,name,args):
    if args.get('project_id',cfg['project_id'])!=cfg['project_id']:
        fail('PROJECT_DENIED','Zotero request belongs to another project')
    binding=validate_binding(cfg)
    if binding is None:fail('ZOTERO_WRITE_NOT_CONFIGURED','This project has no explicitly approved source references')
    refs=binding['references'];key=args.get('citation_key')
    if key is None and name=='paper_zotero_manage' and args.get('action')=='collection':
        matches=[key for key,value in refs.items() if value['collection_name']==args.get('collection_name')]
        if not matches:fail('ZOTERO_SCOPE_DENIED','Collection is outside project authorization')
        key=sorted(matches)[0]
    if key not in refs:fail('ZOTERO_SCOPE_DENIED','Citation key is outside project authorization')
    approved=refs[key]
    if args.get('collection_name',approved['collection_name'])!=approved['collection_name']:
        fail('ZOTERO_SCOPE_DENIED','Collection is outside project authorization')
    if name=='paper_zotero_add_reference' and normalize_doi(args.get('doi'))!=normalize_doi(approved['doi']):
        fail('ZOTERO_SCOPE_DENIED','DOI differs from the explicitly approved source')
    raw=safe_read(cfg['project_root'],approved['source_path'],16384)
    if sha(raw)!=approved['source_sha256']:fail('ZOTERO_SOURCE_CHANGED','Approved bibliography source bytes changed')
    try:parsed=parse_source(raw.decode('utf-8'),key,approved['doi'])
    except Exception:fail('ZOTERO_SOURCE_INVALID','Approved single-reference bibliography is invalid')
    return key,approved,parsed


def _rows(backend,operation,payload,field,sid):
    result=_call(backend,operation,payload,sid);rows=result.get(field)
    if result.get('complete') is not True or not isinstance(rows,list) or len(rows)>(25 if field=='items' else 1000):
        fail('ZOTERO_INCOMPLETE_RESULTS','Local SDK did not return a complete bounded search')
    if any(not isinstance(row,dict) for row in rows):fail('ZOTERO_BACKEND_PROTOCOL','Invalid local search row')
    return rows


def _key(obj):
    key=obj.get('key') or obj.get('data',{}).get('key')
    if not isinstance(key,str) or not KEY.fullmatch(key):fail('ZOTERO_BACKEND_PROTOCOL','Invalid Zotero object key')
    return key


def _data(obj):
    data=obj.get('data',obj)
    if not isinstance(data,dict):fail('ZOTERO_BACKEND_PROTOCOL','Invalid local object metadata')
    return data


def _items(backend,doi,sid):
    rows=_rows(backend,'find_items',{'doi':doi},'items',sid)
    return [row for row in rows if normalize_doi(_data(row).get('DOI'))==doi
            and _data(row).get('itemType') not in {'note','attachment','annotation'}]


def _collections(backend,name,sid):
    rows=_rows(backend,'find_collections',{'name':name},'collections',sid)
    return [row for row in rows if _data(row).get('name')==name]


def _newkey():return ''.join(secrets.choice(ALPHABET) for _ in range(8))


def _definite(error):
    return isinstance(error,StudioError) and error.details.get('http_status') in SAFE_REJECT


def _create(backend,operation,field,payload,ledger_path,sid):
    """One durable object identity survives retries and lost responses."""
    ledger=_read(ledger_path)
    identity={'server_id':sid,'operation':operation,'payload':payload}
    if ledger is not None:
        if ledger.get('identity')!=identity:fail('ZOTERO_PENDING_CONFLICT','An earlier object transaction has a different approved identity')
        if ledger['state'] in {'attempted','unknown','acknowledged','completed'}:
            recovered=_call(backend,'get_'+field,{'key':payload['key']},sid).get(field)
            if recovered is None:
                fail('ZOTERO_WRITE_OUTCOME_UNKNOWN','Earlier write cannot be reconciled; no write was retried')
            return recovered,False
        if ledger.get('attempts',0)>=2:fail('ZOTERO_RETRY_LIMIT','The bounded safe write-attempt budget is exhausted')
    else:ledger={'identity':identity,'attempts':0,'state':'not_attempted','created_at':time.time()}
    ledger.update(state='attempted',attempts=ledger['attempts']+1,attempted_at=time.time());_save(ledger_path,ledger)
    try:
        obj=_call(backend,operation,payload,sid).get(field)
        if not isinstance(obj,dict) or _key(obj)!=payload['key']:
            fail('ZOTERO_BACKEND_PROTOCOL','Local SDK did not return the preallocated object identity')
    except Exception as error:
        ledger.update(state='definitely_rejected' if _definite(error) else 'unknown',error_code=getattr(error,'code','ZOTERO_TRANSPORT_FAILED'))
        _save(ledger_path,ledger);raise
    ledger.update(state='acknowledged',acknowledged_at=time.time());_save(ledger_path,ledger)
    return obj,True


def _verify_item(obj,expected,collection_key):
    if _data(obj).get('itemType')!='journalArticle':
        fail('ZOTERO_METADATA_CONFLICT','Approved article DOI belongs to a different library item type')
    changed=differences(obj,expected)
    if normalize_doi(_data(obj).get('DOI'))!=normalize_doi(expected['doi']):changed.append('doi')
    if changed:fail('ZOTERO_METADATA_CONFLICT','Existing or created item conflicts with the approved source',fields=changed)
    collections=_data(obj).get('collections',[])
    if not isinstance(collections,list):fail('ZOTERO_BACKEND_PROTOCOL','Invalid collection membership')
    return collection_key in collections if collection_key else False


def _version(obj):
    value=obj.get('version',_data(obj).get('version')) if obj else None
    return value if type(value) is int and value>=0 else None


def _collection(cfg,backend,approved,sid,shared,apply):
    name=approved['collection_name'];matches=_collections(backend,name,sid)
    ledger_path=shared/('collection-'+sha(name.encode())+'.json');old=_read(ledger_path)
    if len(matches)>1:fail('ZOTERO_COLLECTION_AMBIGUOUS','More than one collection has the approved name')
    if matches:
        obj=matches[0];key=_key(obj)
        if old and key!=old['identity']['payload']['key']:
            fail('ZOTERO_COLLECTION_MISMATCH','Collection name now identifies a different object than the durable transaction')
        if approved['collection_key'] is not None and key!=approved['collection_key']:
            fail('ZOTERO_COLLECTION_MISMATCH','Collection key differs from project authorization')
        if old and apply:
            old.update(state='completed',completed_at=old.get('completed_at',time.time()));_save(ledger_path,old)
        return obj,False
    if not apply:return None,False
    if cfg['zotero_backend'].get('allow_create_collection') is not True:
        fail('ZOTERO_COLLECTION_CREATE_DISABLED','Creating the approved missing collection is disabled')
    key=old['identity']['payload']['key'] if old else approved['collection_key'] or _newkey()
    if approved['collection_key'] is not None and key!=approved['collection_key']:
        fail('ZOTERO_COLLECTION_MISMATCH','Earlier collection transaction has a different pinned key')
    payload={'key':key,'version':0,'name':name,'parentCollection':False}
    obj,written=_create(backend,'create_collection','collection',payload,ledger_path,sid)
    if _data(obj).get('name')!=name:fail('ZOTERO_COLLECTION_MISMATCH','Created collection name differs from authorization')
    matches=_collections(backend,name,sid)
    if len(matches)!=1 or _key(matches[0])!=key:
        fail('ZOTERO_COLLECTION_AMBIGUOUS','Collection publication cannot be uniquely verified')
    final=_read(ledger_path);final.update(state='completed',completed_at=time.time());_save(ledger_path,final)
    return matches[0],written


def _item_payload(key,source,collection_key):
    return {'key':key,'version':0,'data':{'itemType':'journalArticle','DOI':source['doi'],
        'title':source['title'],'publicationTitle':source['journal'],'date':source['year'],
        'volume':source['volume'],'issue':source['number'],'pages':source['pages'],
        'creators':[{'creatorType':'author','lastName':last,'firstName':first} for last,first in source['authors']],
        'collections':[collection_key],'tags':[],'relations':{}}}


def _public_item(obj,citation_key,sid):
    data=_data(obj);library_keys=[]
    actual=data.get('citationKey')
    if isinstance(actual,str) and CITE.fullmatch(actual):library_keys.append({'value':actual,'source':'data.citationKey'})
    extra=data.get('extra','')
    if isinstance(extra,str):
        for line in extra.splitlines():
            match=re.fullmatch(r'\s*Citation Key:\s*([A-Za-z][A-Za-z0-9:_-]{0,63})\s*',line,re.I)
            if match:library_keys.append({'value':match.group(1),'source':'data.extra: Citation Key'})
    def clean(value):
        if not isinstance(value,str) or len(value)>1000 or any(ord(c)<32 for c in value):
            fail('ZOTERO_METADATA_CONFLICT','Library bibliographic text is invalid or excessive')
        return value
    creators=[]
    for creator in data.get('creators',[]):
        if len(creators)>=50:fail('ZOTERO_METADATA_CONFLICT','Library creator list exceeds bounds')
        creators.append({key:clean(creator[key]) for key in ['creatorType','firstName','lastName','name'] if key in creator})
    year=re.search(r'\d{4}',str(data.get('date','')))
    return {'project_citation_key':citation_key,'zotero_item_key':_key(obj),'library_citation_keys':library_keys[:4],
            'metadata':{'title':clean(data.get('title','')),'creators':creators,'year':year.group() if year else None,
                        'journal':clean(data.get('publicationTitle','')),'doi':normalize_doi(data.get('DOI')),
                        'source':{'server_id':sid,'zotero_item_key':_key(obj),'version':_version(obj)}},
            'claim_support':'unverified','fulltext_read':False}


@contextlib.contextmanager
def _audited_request(cfg,sid,key,request_id,intent,apply):
    path=_private(Path(cfg['state_root'])/'zotero-local'/sha(sid.encode())/key)/(request_id+'.json')
    previous=_read(path)
    if previous and previous['intent']!=intent:fail('IDEMPOTENCY_CONFLICT','request_id belongs to a different approved source or target')
    if apply and previous is None:_save(path,{'intent':intent,'state':'planned','created_at':time.time()})
    try:yield path
    except Exception as error:
        if apply:
            code=getattr(error,'code','ZOTERO_ORCHESTRATION_FAILED')
            record={'intent':intent,'state':'outcome_unknown' if code in {'ZOTERO_WRITE_OUTCOME_UNKNOWN','ZOTERO_INTERRUPTED','ZOTERO_TIMEOUT','ZOTERO_DEADLINE'} else 'failed',
                    'error_code':code,'finished_at':time.time()}
            _save(path.with_name(request_id+'-attempt-'+uuid.uuid4().hex+'.json'),record)
            current=_read(path)
            if current is None or current.get('state') not in {'completed','existing'}:_save(path,record)
        raise


def _finish_request(path,intent,result,apply):
    if apply:
        previous=_read(path)
        if previous is None or previous.get('state') not in {'completed','existing'}:
            _save(path,{'intent':intent,'state':'existing' if result['status']=='existing' else 'completed',
                        'result':result,'finished_at':time.time()})
    return result


def dispatch(cfg,name,args,backend=None):
    if name not in {'paper_zotero_add_reference','paper_zotero_manage'}:fail('TOOL_DENIED','Unknown local Zotero tool')
    if args.get('project_id',cfg['project_id'])!=cfg['project_id']:fail('PROJECT_DENIED','Wrong Zotero project identity')
    if type(args.get('dry_run',True)) is not bool or type(args.get('authorized',False)) is not bool:
        fail('INVALID_ARGUMENT','dry_run and authorized must be booleans')
    backend=_backend(cfg,backend)
    if name=='paper_zotero_manage' and args.get('action')=='status':return _status(cfg,backend)
    if name=='paper_zotero_manage' and args.get('action')=='update':
        fail('ZOTERO_UPDATE_DISABLED','Updating existing library items is not authorized or implemented in this stage')
    if name=='paper_zotero_manage' and args.get('action')!='collection':fail('ZOTERO_ACTION_DENIED','Unknown local Zotero management action')
    if (name=='paper_zotero_manage' and args.get('dry_run',True) is True
            and not cfg.get('zotero_write',{}).get('references')):
        # Query exact project Collection keys without authorizing a library
        # mutation or enumerating collections outside the project.
        from paper_studio.references.zotero_worker import valid_scope,ReadError
        try:scope=valid_scope(cfg.get('zotero_readonly'))
        except ReadError as error:fail(error.code,error.message)
        if args.get('citation_key') is not None and args['citation_key'] not in scope['items']:
            fail('ZOTERO_SCOPE_DENIED','Citation key is outside project authorization')
        name_arg=args.get('collection_name')
        if not isinstance(name_arg,str) or not 1<=len(name_arg)<=160:
            fail('INVALID_ARGUMENT','A bounded exact Collection name is required')
        keys=scope.get('collection_keys',[])
        if not keys:fail('ZOTERO_SCOPE_DENIED','Project has no authorized Collection keys')
        status=_status(cfg,backend);sid=status['server_id'];matches=[]
        for key in keys:
            obj=_call(backend,'get_collection',{'key':key},sid).get('collection')
            if obj is None:continue
            if not isinstance(obj,dict) or _key(obj)!=key:fail('ZOTERO_COLLECTION_MISMATCH','Pinned Collection identity differs')
            if _data(obj).get('name')==name_arg:matches.append(obj)
        if len(matches)>1:fail('ZOTERO_COLLECTION_AMBIGUOUS','Authorized Collection name is not unique')
        return {'project_id':cfg['project_id'],'status':'found' if matches else 'not_found',
                'collection_name':name_arg,'collection_key':_key(matches[0]) if matches else None,
                'collection_version':_version(matches[0]) if matches else None,'dry_run':True,
                'import_calls':0,'collection_writes':0,'library_modified':False}
    key,approved,source=_scope(cfg,name,args)
    status=_status(cfg,backend)
    if status['blocked_version']:return {**status,'status':'blocked','code':'ZOTERO_UPGRADE_REQUIRED'}
    apply=args.get('dry_run',True) is False
    if type(args.get('dry_run',True)) is not bool or type(args.get('authorized',False)) is not bool:
        fail('INVALID_ARGUMENT','dry_run and authorized must be booleans')
    request_id=args.get('request_id')
    if request_id is None and not apply:request_id='preview'
    if not isinstance(request_id,str) or not REQUEST.fullmatch(request_id):fail('INVALID_ARGUMENT','A bounded request_id is required')
    if apply and (args.get('authorized') is not True or not status['write_ready']):
        fail('ZOTERO_LOCAL_WRITE_NOT_AUTHORIZED','Apply requires explicit authorization and a configured server/key binding')
    sid=status['server_id']
    intent={'project_id':cfg['project_id'],'citation_key':key,'tool':name,'doi':normalize_doi(approved['doi']),
            'source_sha256':approved['source_sha256'],'collection_name':approved['collection_name'],
            'collection_key':approved['collection_key'],'server_id':sid}
    with _database_lock(cfg,sid,backend.deadline) as shared, _audited_request(cfg,sid,key,request_id,intent,apply) as request_path:
        result={'project_id':cfg['project_id'],'citation_key':key,'doi':intent['doi'],'request_id':request_id,
                'server_id':sid,'dry_run':not apply,'import_calls':0,'collection_writes':0,'bib_written':False,
                'source_sha256':approved['source_sha256'],'claim_support':'unverified','update_supported':False}
        doi=intent['doi'];matches=_items(backend,doi,sid) if name=='paper_zotero_add_reference' else []
        if len(matches)>1:fail('ZOTERO_DUPLICATE_DOI','More than one exact DOI item exists; no item was selected')
        ledger_path=shared/('doi-'+sha(doi.encode())+'.json');old=_read(ledger_path) if name=='paper_zotero_add_reference' else None
        recovered_pending=bool(old and old['state'] in {'attempted','unknown','acknowledged'})
        if old and old['state'] in {'attempted','unknown','acknowledged','completed'}:
            chosen=old['identity']['payload']['key']
            recovered=_call(backend,'get_item',{'key':chosen},sid).get('item')
            if not isinstance(recovered,dict) or _key(recovered)!=chosen:
                fail('ZOTERO_WRITE_OUTCOME_UNKNOWN','A prior DOI write remains unresolved; no further collection or item was created')
            if matches and _key(matches[0])!=chosen:
                fail('ZOTERO_DUPLICATE_DOI','A different DOI object conflicts with the earlier write identity')
            matches=[recovered]
        # Old items are never moved or rewritten merely to satisfy a new target.
        if matches:
            _verify_item(matches[0],source,None)
            collection,_=_collection(cfg,backend,approved,sid,shared,False)
            collection_key=_key(collection) if collection else None
            member=_verify_item(matches[0],source,collection_key)
            if recovered_pending and not member:
                fail('ZOTERO_COLLECTION_MISMATCH','Earlier uncertain write cannot be reconciled into its approved collection')
            if old and apply:
                old.update(state='completed',completed_at=old.get('completed_at',time.time()));_save(ledger_path,old)
            value={**result,**_public_item(matches[0],key,sid),'status':'reconciled' if recovered_pending else 'existing',
                   'item_key':_key(matches[0]),'collection_key':collection_key,'item_version':_version(matches[0]),
                   'collection_version':_version(collection),'in_target_collection':member,'metadata_verified':True}
            return _finish_request(request_path,intent,value,apply)
        collection,written=_collection(cfg,backend,approved,sid,shared,apply)
        result['collection_writes']=int(written);collection_key=_key(collection) if collection else None
        result['collection_key']=collection_key
        result['collection_version']=_version(collection)
        if name=='paper_zotero_manage':return _finish_request(request_path,intent,{**result,'status':'created' if written else 'existing' if collection else 'not_found'},apply)
        if not apply:return {**result,'status':'not_found','would_create_item':True,'would_create_collection':collection is None}
        old=_read(ledger_path)
        item_key=old['identity']['payload']['key'] if old else _newkey()
        payload=_item_payload(item_key,source,collection_key)
        obj,written=_create(backend,'create_item','item',payload,ledger_path,sid)
        # Read by the durable key, then repeat the exact-DOI search. This binds
        # success to both object identity and all approved bibliographic fields.
        actual=_call(backend,'get_item',{'key':item_key},sid).get('item')
        if not isinstance(actual,dict) or _key(actual)!=item_key:
            fail('ZOTERO_WRITE_OUTCOME_UNKNOWN','Created item cannot be independently read back')
        if not _verify_item(actual,source,collection_key):fail('ZOTERO_COLLECTION_MISMATCH','Created item is not in the explicitly authorized collection')
        matches=_items(backend,doi,sid)
        if len(matches)!=1 or _key(matches[0])!=item_key:fail('ZOTERO_DUPLICATE_DOI','Created DOI cannot be uniquely reconciled')
        final=_read(ledger_path);final.update(state='completed',completed_at=time.time());_save(ledger_path,final)
        value={**result,**_public_item(actual,key,sid),'status':'imported' if written else 'reconciled','item_key':item_key,
               'item_version':_version(actual),'in_target_collection':True,'metadata_verified':True,'import_calls':int(written)}
        return _finish_request(request_path,intent,value,apply)
