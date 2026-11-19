"""Windows-only SDK adapter. Fixed loopback, route/body guards, no auto-auth."""
from __future__ import annotations
import base64,ctypes,importlib.metadata,json,os,re,sys,time
from pathlib import Path

BASE='http://127.0.0.1:23119/api'
LIMIT=2*1024*1024
KEY=re.compile(r'[A-Z0-9]{8}')

class BridgeError(Exception):
    def __init__(self,code,message,http_status=None):self.code=code;self.message=message;self.http_status=http_status;super().__init__(message)
def fail(code,message,http_status=None):raise BridgeError(code,message,http_status)

def credential(binding):
    """Decrypt only a pinned local grant under the current Windows user."""
    if not binding.get('credential_path'):return None
    if os.name!='nt':fail('ZOTERO_CREDENTIAL_HOST','Local grants require Windows current-user DPAPI')
    path=Path(binding['credential_path'])
    if path.is_symlink() or path.stat().st_size>16384:fail('ZOTERO_CREDENTIAL_INVALID','Invalid protected local grant')
    record=json.loads(path.read_text(encoding='utf-8'))
    if record.get('purpose')!='paper-studio-zotero-local-v1' or record.get('server_id')!=binding.get('server_id'):
        fail('ZOTERO_SERVER_ID_MISMATCH','Protected grant belongs to a different Zotero database')
    class Blob(ctypes.Structure):_fields_=[('size',ctypes.c_ulong),('data',ctypes.POINTER(ctypes.c_ubyte))]
    raw=base64.b64decode(record['ciphertext_b64'],validate=True);buffer=ctypes.create_string_buffer(raw)
    source=Blob(len(raw),ctypes.cast(buffer,ctypes.POINTER(ctypes.c_ubyte)));output=Blob()
    if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(source),None,None,None,None,1,ctypes.byref(output)):
        fail('ZOTERO_CREDENTIAL_UNAVAILABLE','Windows could not decrypt this local grant')
    try:key=ctypes.string_at(output.data,output.size).decode('utf-8')
    finally:ctypes.windll.kernel32.LocalFree(output.data)
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,256}',key):fail('ZOTERO_CREDENTIAL_INVALID','Invalid local API grant')
    return key

def safe_object(obj,kind):
    if obj is None:return None
    data=obj.get('data',obj);key=obj.get('key',data.get('key'))
    if not isinstance(key,str) or not KEY.fullmatch(key):fail('ZOTERO_INVALID_IDENTITY','SDK returned an invalid object key')
    fields={'key','version','name','parentCollection'} if kind=='collection' else {'key','version','itemType','DOI','title','creators','date','publicationTitle','volume','issue','pages','collections','citationKey'}
    cleaned={k:v for k,v in data.items() if k in fields}
    if kind=='item' and isinstance(cleaned.get('creators'),list):
        cleaned['creators']=[{k:v for k,v in c.items() if k in {'creatorType','firstName','lastName','name'}} for c in cleaned['creators'] if isinstance(c,dict)]
    return {'key':key,'version':obj.get('version',data.get('version')),'data':cleaned}

def execute(request,client_factory=None):
    binding=request['binding'];operation=request['operation'];payload=request.get('payload',{})
    if operation not in {'status','find_items','find_collections','get_item','get_collection','create_item','create_collection'}:
        fail('ZOTERO_OPERATION_DENIED','Only scoped metadata operations are exposed')
    sys.path.insert(0,binding['sdk_path'])
    if importlib.metadata.version('pyzotero')!='1.16.0':fail('ZOTERO_SDK_VERSION','Pyzotero 1.16.0 is required')
    import httpx2
    from pyzotero import Zotero
    sid=binding.get('server_id');deadline=time.monotonic()+min(35,float(request.get('budget_seconds',35)))
    writes=operation in {'create_item','create_collection'}
    allowed_get={'/api/'};allowed_post={}
    if operation=='find_items':
        doi=payload.get('doi')
        if not isinstance(doi,str) or not re.fullmatch(r'10\.\d{4,9}/[^\s{}\\]{1,180}',doi):fail('ZOTERO_INVALID_ARGUMENT','An exact DOI is required')
        allowed_get.add('/api/users/0/items/top')
    if operation=='find_collections':allowed_get.add('/api/users/0/collections')
    if operation in {'get_item','get_collection','create_item','create_collection'}:
        key=payload.get('key')
        if not isinstance(key,str) or not KEY.fullmatch(key):fail('ZOTERO_INVALID_ARGUMENT','An exact object key is required')
        group='items' if operation.endswith('item') else 'collections';allowed_get.add('/api/users/0/'+group+'/'+key)
    if operation=='create_collection':
        if payload.get('parentCollection') not in (False,None) or payload.get('version')!=0 or not isinstance(payload.get('name'),str) or not 1<=len(payload['name'])<=160:fail('ZOTERO_INVALID_ARGUMENT','Only an approved new top-level collection is supported')
        allowed_post['/api/users/0/collections']=[{'key':key,'version':0,'name':payload['name'],'parentCollection':False}]
    if operation=='create_item':
        data=payload.get('data',{});fields={'itemType','DOI','title','creators','date','publicationTitle','volume','issue','pages','collections','tags','relations'}
        if not isinstance(data,dict) or set(data)-fields or data.get('itemType')!='journalArticle' or payload.get('version')!=0:
            fail('ZOTERO_INVALID_ARGUMENT','Only an approved new journal article is supported')
        collections=data.get('collections')
        if not isinstance(collections,list) or len(collections)!=1 or not KEY.fullmatch(collections[0]):fail('ZOTERO_INVALID_ARGUMENT','Exactly one confirmed collection key is required')
        if data.get('tags',[])!=[] or data.get('relations',{})!={}:fail('ZOTERO_INVALID_ARGUMENT','No extra tags or relations are permitted')
        allowed_post['/api/users/0/items']=[{**data,'key':key,'version':0}]
    class Guard(httpx2.BaseTransport):
        def __init__(self):self.inner=httpx2.HTTPTransport(retries=0);self.count=0;self.posts=0
        def handle_request(self,r):
            self.count+=1
            if self.count>14 or time.monotonic()>=deadline:fail('ZOTERO_BUDGET','SDK operation exhausted its bounded request budget')
            if r.url.scheme!='http' or r.url.host!='127.0.0.1' or r.url.port!=23119:fail('ZOTERO_ROUTE_DENIED','SDK cannot leave the local API')
            path=r.url.path
            if r.method=='GET':
                if path not in allowed_get:fail('ZOTERO_ROUTE_DENIED','Read route is outside the selected operation')
            elif r.method=='POST':
                self.posts+=1
                if self.posts>1 or path not in allowed_post or json.loads(r.content)!=allowed_post[path]:fail('ZOTERO_ROUTE_DENIED','Write payload is outside the approved one-object operation')
                if not sid or r.headers.get('Zotero-Server-ID')!=sid or not r.headers.get('Zotero-API-Key'):fail('ZOTERO_LOCAL_AUTH_REQUIRED','A pinned instance and local grant are required')
            else:fail('ZOTERO_ROUTE_DENIED','Updates, deletion, attachments and authorization are not exposed')
            if sid and r.headers.get('Zotero-Server-ID')!=sid:fail('ZOTERO_SERVER_ID_MISMATCH','Every request must use the pinned local database identity')
            r.extensions['timeout']={k:min(5,max(.1,deadline-time.monotonic())) for k in ['connect','read','write','pool']}
            r.headers['Accept-Encoding']='identity'
            response=self.inner.handle_request(r)
            try:
                if response.headers.get('Content-Encoding','identity').lower() not in {'','identity'}:
                    fail('ZOTERO_RESPONSE_ENCODING','Compressed SDK responses are refused before decoding')
                chunks=[];size=0
                for chunk in response.iter_raw():
                    size+=len(chunk)
                    if size>LIMIT or time.monotonic()>deadline:fail('ZOTERO_RESPONSE_LIMIT','SDK response exceeded byte or time budget')
                    chunks.append(chunk)
                if 300<=response.status_code<400:fail('ZOTERO_REDIRECT_DENIED','Redirects are not followed')
                explicit_sid=response.headers.get('Zotero-Server-ID')
                if sid and explicit_sid and explicit_sid!=sid:fail('ZOTERO_SERVER_ID_MISMATCH','Local database identity changed; no retry',response.status_code)
                if response.status_code>=400 and not (response.status_code==404 and r.method=='GET'):
                    codes={401:'ZOTERO_LOCAL_AUTH_REQUIRED',403:'ZOTERO_PERMISSION_DENIED',412:'ZOTERO_VERSION_CONFLICT',428:'ZOTERO_PRECONDITION_REQUIRED',429:'ZOTERO_RATE_LIMITED'}
                    fail('ZOTERO_RESPONSE_UNKNOWN' if response.status_code>=500 else codes.get(response.status_code,'ZOTERO_HTTP_REJECTED'),
                         'Local API rejected or failed the request; no automatic write retry',response.status_code)
                actual=response.headers.get('Zotero-Server-ID')
                if sid and actual!=sid:fail('ZOTERO_SERVER_ID_MISMATCH','Local database identity changed; no retry',response.status_code)
                if response.headers.get('Backoff') or response.headers.get('Retry-After'):
                    fail('ZOTERO_RATE_LIMITED','API backoff requires a later reconciled request; SDK sleep is disabled')
                return httpx2.Response(response.status_code,headers=response.headers,content=b''.join(chunks))
            finally:response.close()
        def close(self):self.inner.close()
    client=client_factory(allowed_get,allowed_post) if client_factory else httpx2.Client(transport=Guard(),timeout=5,follow_redirects=False,trust_env=False)
    with client:
        header={'Zotero-Server-ID':sid} if sid else {}
        probe=client.get(BASE+'/',headers=header)
        if probe.status_code!=200:fail('ZOTERO_UNAVAILABLE','Local API health probe did not return HTTP200')
        version=probe.headers.get('X-Zotero-Version','');actual=probe.headers.get('Zotero-Server-ID')
        if sid and actual!=sid:fail('ZOTERO_SERVER_ID_MISMATCH','Configured local instance differs from the running database')
        if operation=='status':
            return {'server_id':actual,'zotero_version':version,'write_authorized':False,
                    'credential_configured':bool(binding.get('credential_path')),'authorization_checked':False,
                    'sdk':{'name':'pyzotero','version':'1.16.0'},'local_api_http_status':200,
                    'library_type':'user','library_id':'0','identity_scope':'current_local_personal_library',
                    'write_ready':bool(actual and sid and binding.get('credential_path') and version.split('.')[0].isdigit() and int(version.split('.')[0])>=10),
                    'write_authorization_verified':False,
                    'blocker':'ZOTERO_UPGRADE_REQUIRED' if not actual or not version.split('.')[0].isdigit() or int(version.split('.')[0])<10 else 'ZOTERO_LOCAL_AUTH_REQUIRED' if not sid or not binding.get('credential_path') else None}
        if writes and (not actual or not version.split('.')[0].isdigit() or int(version.split('.')[0])<10):fail('ZOTERO_UPGRADE_REQUIRED','Zotero 10+ is required for atomic local writes')
        secret=credential(binding) if writes else None
        if writes and (not sid or payload.get('server_id')!=sid or not secret):fail('ZOTERO_LOCAL_AUTH_REQUIRED','Explicit local authorization and pinned server ID are required')
        zot=Zotero('0','user',local=True,server_id=sid,local_api_key=secret,client=client)
        if operation=='find_items':
            rows=zot.top(q=payload['doi'],qmode='everything',limit=25,format='json')
            total=zot.request.headers.get('Total-Results')
            if total is None or int(total)!=len(rows):fail('ZOTERO_INCOMPLETE_RESULTS','DOI candidate result is incomplete')
            exact=[x for x in rows if str(x.get('data',{}).get('DOI','')).strip().casefold()==payload['doi'].casefold() and x.get('data',{}).get('itemType') not in {'attachment','note','annotation'}]
            return {'server_id':actual,'items':[safe_object(x,'item') for x in exact],'complete':True}
        if operation=='find_collections':
            rows=[];total=None
            while total is None or len(rows)<total:
                page=zot.collections(limit=100,start=len(rows),format='json');value=zot.request.headers.get('Total-Results')
                if value is None or not str(value).isdigit() or int(value)>1000 or not isinstance(page,list) or len(page)>100 or (total is not None and total!=int(value)) or not page and len(rows)<int(value):fail('ZOTERO_INCOMPLETE_RESULTS','Collection inventory is incomplete or changed')
                total=int(value);rows.extend(page)
                if len(rows)>total:fail('ZOTERO_INCOMPLETE_RESULTS','Collection result exceeds its declared total')
            return {'server_id':actual,'collections':[safe_object(x,'collection') for x in rows if x.get('data',{}).get('name')==payload['name']],'complete':True}
        kind='item' if operation.endswith('item') else 'collection'
        getter=zot.item if kind=='item' else zot.collection
        if writes:
            objects=next(iter(allowed_post.values()))
            result=zot.create_items(objects) if kind=='item' else zot.create_collections(objects)
            if result.get('failed') or result.get('success',{}).get('0')!=key and result.get('successful',{}).get('0',{}).get('key')!=key:
                fail('ZOTERO_WRITE_REJECTED','Local API did not confirm the exact requested object key; reconcile')
        try:obj=getter(key)
        except Exception as error:
            if type(error).__name__=='ResourceNotFoundError' and not writes:return {'server_id':actual,kind:None}
            raise
        return {'server_id':actual,kind:safe_object(obj,kind)}

def main():
    try:
        raw=sys.stdin.buffer.read(65537)
        if len(raw)>65536:fail('ZOTERO_INPUT_LIMIT','SDK request exceeds 64 KiB')
        result=execute(json.loads(raw));reply={'ok':True,'result':result}
    except Exception as e:
        mapping={'ServerIDMismatchError':'ZOTERO_SERVER_ID_MISMATCH','LocalAPIKeyRequiredError':'ZOTERO_LOCAL_AUTH_REQUIRED','PreConditionFailedError':'ZOTERO_VERSION_CONFLICT','PreconditionFailedError':'ZOTERO_VERSION_CONFLICT','ReadTimeout':'ZOTERO_RESPONSE_UNKNOWN','ConnectError':'ZOTERO_UNAVAILABLE','ConnectTimeout':'ZOTERO_UNAVAILABLE'}
        reply={'ok':False,'error':{'code':e.code if isinstance(e,BridgeError) else mapping.get(type(e).__name__,'ZOTERO_SDK_ERROR'),'message':e.message if isinstance(e,BridgeError) else 'Scoped SDK operation failed; inspect capability and reconcile before retrying a write'}}
        if isinstance(e,BridgeError) and e.http_status is not None:reply['error']['http_status']=e.http_status
    print(json.dumps(reply,ensure_ascii=False))

if __name__=='__main__':main()
