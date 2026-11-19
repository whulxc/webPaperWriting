"""Independent private Google Drive publisher. No connector credential reuse.

Only a locally authorized desktop OAuth file is used. drive.file remains the
default; broader Drive scope requires an explicit drive_full credential marker.
Local version manifests are verified before upload; sharing is never changed.
"""
from __future__ import annotations
import hashlib,http.client,json,math,mimetypes,os,re,secrets,stat,time,urllib.parse,urllib.request,urllib.error
from pathlib import Path
from paper_studio.security.policy import *
from paper_studio.versions.history import verify_version
from paper_studio.drive.oauth_scopes import (DRIVE_FILE_SCOPE,DRIVE_FULL_SCOPE,
    validate_scope_string,validate_credential_scopes)
SCOPE=DRIVE_FILE_SCOPE
FULL_SCOPE=DRIVE_FULL_SCOPE
API_ROOT='https://www.googleapis.com/'
MULTIPART_LIMIT=5*1024*1024
MAX_UPLOAD_BYTES=256*1024*1024
CHUNK_BYTES=1024*1024  # Four 256 KiB blocks; final chunk may be shorter.
RESPONSE_LIMIT=4*1024*1024
# Protocol: https://developers.google.com/workspace/drive/api/guides/manage-uploads
# Server checksum: https://developers.google.com/workspace/drive/api/reference/rest/v3/files

def session_url(value):
    """Upload sessions are capabilities: never follow arbitrary Location URLs."""
    if not isinstance(value,str) or len(value)>8192 or any(ord(c)<33 or ord(c)>126 for c in value):
        fail('DRIVE_SESSION_URL','Invalid upload session URL')
    try:parsed=urllib.parse.urlsplit(value)
    except ValueError:fail('DRIVE_SESSION_URL','Malformed upload session URL')
    if (parsed.scheme!='https' or parsed.netloc!='www.googleapis.com' or parsed.path!='/upload/drive/v3/files'
        or parsed.fragment or parsed.username or parsed.password):
        fail('DRIVE_SESSION_URL','Upload session must use the fixed Google Drive API origin and path')
    try:query=urllib.parse.parse_qs(parsed.query,strict_parsing=True)
    except ValueError:fail('DRIVE_SESSION_URL','Malformed upload session query')
    if query.get('uploadType')!=['resumable'] or len(query.get('upload_id',[]))!=1 or not query['upload_id'][0]:
        fail('DRIVE_SESSION_URL','Upload session identity is missing or ambiguous')
    return value

def grant_scopes(value):
    return validate_scope_string(value)

def file_identity(info):
    return (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns)

def verified_manifest(cfg,label):
    """Bind exact manifest bytes across verification using one held descriptor.

    verify_version returns parsed data, not the byte identity it read. A second
    verification while the captured descriptor is held binds both identities
    without changing the version subsystem's public contract.
    """
    root,first=verify_version(cfg,label)
    path=no_links(root/'manifest.json')
    with os.fdopen(os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK),'rb') as stream:
        before=os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or before.st_size>2*1024**2:
            fail('VERSION_TAMPERED','Invalid bounded version manifest')
        raw=stream.read(2*1024**2+1)
        if len(raw)!=before.st_size:fail('VERSION_TAMPERED','Version manifest changed during capture')
        captured=json.loads(raw)
        checked_root,checked=verify_version(cfg,label)
        no_links(path)
        if (checked_root!=root or captured!=first or checked!=captured
            or file_identity(before)!=file_identity(os.fstat(stream.fileno()))
            or file_identity(before)!=file_identity(path.stat())):
            fail('VERSION_TAMPERED','Version manifest identity changed during verification')
    return root,captured,{'sha256':sha(raw),'bytes':len(raw)}

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):fail('DRIVE_REDIRECT','Drive API redirect refused')


def transport_diagnostics(error,method,path,headers):
    """Allowlisted labels only: no URL, query, header, body, or error string."""
    stage='drive_metadata' if method=='GET' else 'drive_request'
    if path=='/upload/drive/v3/files':
        stage=('resumable_status' if str((headers or {}).get('Content-Range','')).startswith('bytes */')
               else 'resumable_chunk') if method=='PUT' else 'upload_create'
    cause=error
    nested=getattr(error,'reason',None)
    if isinstance(nested,BaseException):cause=nested
    output={'method':method,'stage':stage,'exception_type':type(error).__name__,'cause_type':type(cause).__name__}
    number=getattr(cause,'errno',None)
    if type(number) is int:output['errno']=number
    reason=getattr(cause,'reason',None)
    if isinstance(reason,str) and re.fullmatch(r'[A-Z0-9_]{1,80}',reason):output['ssl_reason']=reason
    return output


class Drive:
    resumable_transport_recoveries=0
    def __init__(self,path,*,persist_access_token=True,deadline=None):
        self.persist_access_token=persist_access_token
        self.deadline=deadline
        self.path=no_links(Path(path))
        if not self.path.is_file():fail('OAUTH_REQUIRED','Independent local Google Drive OAuth authorization is required',credential_file_ref=str(self.path))
        info=self.path.stat()
        if info.st_mode&0o077 or info.st_uid!=os.getuid() or info.st_nlink!=1:fail('OAUTH_PERMISSIONS','OAuth file must be private, single-link and owned by current user')
        with os.fdopen(os.open(self.path,os.O_RDONLY|os.O_NOFOLLOW),'rb') as stream:
            opened=os.fstat(stream.fileno())
            if (opened.st_dev,opened.st_ino)!=(info.st_dev,info.st_ino) or opened.st_uid!=os.getuid() or opened.st_mode&0o077 or opened.st_nlink!=1:
                fail('OAUTH_PERMISSIONS','OAuth file identity changed during open')
            raw=stream.read(65537)
        if len(raw)>65536:fail('OAUTH_PERMISSIONS','OAuth file exceeds 64 KiB limit')
        self.auth=json.loads(raw)
        self.authorization_mode,_=validate_credential_scopes(self.auth)
        self.opener=urllib.request.build_opener(NoRedirect())
        self.last_upload_receipt=None
        if self.auth.get('expires_at',0)<time.time()+60:self.refresh()
    def timeout(self,maximum):
        deadline=getattr(self,'deadline',None)
        if deadline is None:return maximum
        remaining=deadline-time.monotonic()
        if remaining<=0:fail('DRIVE_DEADLINE','Drive publication total deadline exceeded; partial state retained')
        return min(maximum,remaining)
    def refresh(self):
        mode,_=validate_credential_scopes(self.auth)
        if mode!=self.authorization_mode:fail('OAUTH_SCOPE','Drive authorization mode changed after credential admission')
        data=urllib.parse.urlencode({k:self.auth[k] for k in ['client_id','client_secret','refresh_token']}|{'grant_type':'refresh_token'}).encode()
        try:
            with self.opener.open(urllib.request.Request('https://oauth2.googleapis.com/token',data=data),timeout=self.timeout(30)) as r:
                raw=r.read(65537)
                if len(raw)>65536:fail('OAUTH_REFRESH_FAILED','OAuth refresh response exceeds limit')
                value=json.loads(raw)
        except StudioError:raise
        except urllib.error.HTTPError as error:
            # Only return allowlisted protocol labels, never provider payloads
            # (which may contain credentials or request diagnostics).
            provider_error=None
            try:
                response=error.read(65537)
                if len(response)<=65536:
                    candidate=json.loads(response).get('error')
                    if candidate in {'invalid_grant','invalid_client','unauthorized_client','temporarily_unavailable','server_error'}:
                        provider_error=candidate
            except Exception:pass
            needs_auth=provider_error=='invalid_grant'
            fail('OAUTH_REFRESH_FAILED',
                 'Google authorization expired or was revoked; repeat desktop authorization' if needs_auth
                 else 'Google rejected OAuth refresh; inspect sanitized error before reauthorizing',
                 http_status=error.code,provider_error=provider_error,
                 reauthorization_required=needs_auth,
                 retryable=error.code==429 or error.code>=500)
        except Exception:
            fail('OAUTH_REFRESH_FAILED','OAuth refresh connection failed; authorization validity is unknown',
                 reauthorization_required=False,authorization_validity='unknown',retryable=True)
        actual_scopes=validate_scope_string(value['scope'],mode) if 'scope' in value else None
        lifetime=value.get('expires_in')
        if not isinstance(value.get('access_token'),str) or not value['access_token'] or isinstance(lifetime,bool) or not isinstance(lifetime,(int,float)) or not math.isfinite(lifetime) or lifetime<=60:
            fail('OAUTH_REFRESH_FAILED','OAuth refresh did not return a valid access token/lifetime')
        self.auth.update(access_token=value['access_token'],expires_at=time.time()+lifetime)
        # An omitted refresh scope retains the previously validated grant. A
        # returned scope is stored exactly after mode-specific verification.
        if actual_scopes is not None:self.auth['scopes']=actual_scopes
        if getattr(self,'persist_access_token',True):atomic_json(self.path,self.auth);self.path.chmod(0o600)
    def exchange(self,method,url,payload=None,headers=None,allowed=(200,201)):
        parsed=urllib.parse.urlsplit(url)
        if parsed.scheme!='https' or parsed.netloc!='www.googleapis.com' or parsed.fragment:fail('DRIVE_URL','Untrusted Drive API origin')
        self.timeout(90)
        if 'expires_at' in self.auth and self.auth['expires_at']<time.time()+60:self.refresh()
        headers={'Authorization':'Bearer '+self.auth['access_token'],**(headers or {})}
        req=urllib.request.Request(url,data=payload,method=method,headers=headers)
        try:
            try:response=self.opener.open(req,timeout=self.timeout(90))
            except urllib.error.HTTPError as error:
                if error.code not in allowed:fail('DRIVE_HTTP_ERROR','Google Drive rejected request',http_status=error.code,
                    **{key:value for key,value in transport_diagnostics(error,method,parsed.path,headers).items() if key in {'method','stage'}})
                response=error
            with response as response:
                code=response.status if hasattr(response,'status') else response.code
                if code not in allowed:fail('DRIVE_HTTP_ERROR','Unexpected Drive HTTP response',http_status=code)
                if response.geturl()!=url:fail('DRIVE_REDIRECT','Drive API response changed origin or URL')
                raw=response.read(RESPONSE_LIMIT+1)
                if len(raw)>RESPONSE_LIMIT:fail('DRIVE_RESPONSE_LIMIT','Drive metadata response exceeded limit')
                return code,dict(response.headers),raw
        except StudioError:raise
        except (OSError,urllib.error.URLError,http.client.HTTPException) as error:
            fail('DRIVE_INTERRUPTED','Upload or metadata connection interrupted; state retained',
                 **transport_diagnostics(error,method,parsed.path,headers))
    @staticmethod
    def decode(raw):
        try:value=json.loads(raw) if raw else {}
        except (ValueError,UnicodeError):fail('DRIVE_PROTOCOL','Invalid Drive JSON response')
        if not isinstance(value,dict):fail('DRIVE_PROTOCOL','Drive JSON response must be an object')
        return value
    def request(self,method,path,body=None,content_type='application/json'):
        url=API_ROOT+path.lstrip('/')
        payload=json.dumps(body).encode() if isinstance(body,dict) else body
        _,_,raw=self.exchange(method,url,payload,{'Content-Type':content_type})
        return self.decode(raw)
    def metadata(self,id):return self.request('GET','drive/v3/files/'+urllib.parse.quote(id,safe='')+'?fields=id,name,mimeType,size,md5Checksum,sha256Checksum,parents,webViewLink,permissions(type,role),appProperties')
    def private(self,id):
        meta=self.metadata(id)
        if 'permissions' in meta and (not meta['permissions'] or any(p.get('type')!='user' or p.get('role')!='owner' for p in meta['permissions'])):fail('DRIVE_NOT_PRIVATE','Configured review folder must have owner-only permissions')
        if 'permissions' not in meta:fail('DRIVE_PRIVACY_UNKNOWN','Drive did not return permissions; publication remains blocked')
        return meta
    def folder(self,name,parent=None,exclusive=False,progress=None):
        query="trashed=false and mimeType='application/vnd.google-apps.folder' and name='"+name.replace("'","\\'")+"'"
        if parent:query+=" and '"+parent.replace("'","\\'")+"' in parents"
        response=self.request('GET','drive/v3/files?'+urllib.parse.urlencode({'q':query,'fields':'files(id,name)','pageSize':100}))
        if response.get('files'):
            if exclusive:fail('DRIVE_VERSION_EXISTS','Version folder already exists; immutable publication refuses overwrite')
            if len(response['files'])!=1:fail('DRIVE_AMBIGUOUS','Multiple folders match; configure an explicit parent')
            id=response['files'][0]['id'];self.private(id);return id
        meta={'name':name,'mimeType':'application/vnd.google-apps.folder'}
        if parent:meta['parents']=[parent]
        intent=None
        if progress is not None:
            if not exclusive or not parent:fail('INVALID_ARGUMENT','Folder checkpoints require an exclusive named parent')
            value=self.request('GET','drive/v3/files/generateIds?count=1&space=drive&type=files')
            ids=value.get('ids')
            if not isinstance(ids,list) or len(ids)!=1:fail('DRIVE_PROTOCOL','Drive did not reserve one version folder ID')
            meta['id']=_drive_id(ids[0])
            intent={'schema':'paper-studio.drive-folder-creation.v1','id':meta['id'],'name':name,
                    'parent_id':parent,'attempted':False,'attempts':0,'verified':False}
            progress(dict(intent))
            intent.update(attempted=True,attempts=1);progress(dict(intent))
        obj=self.request('POST','drive/v3/files?fields=id',meta)
        if intent is not None and obj.get('id')!=intent['id']:fail('DRIVE_PROTOCOL','Created version folder differs from its reserved ID')
        actual=self.private(obj['id'])
        if intent is not None:
            if (actual.get('id')!=intent['id'] or actual.get('name')!=name or actual.get('parents')!=[parent]
                or actual.get('mimeType')!='application/vnd.google-apps.folder'):
                fail('DRIVE_RECOVERY_MISMATCH','Created folder differs from its durable intent')
            intent['verified']=True;progress(dict(intent))
        return obj['id']
    def upload(self,path,name,parent,progress=None,*,expected_sha256=None,expected_bytes=None,preallocated_file_id=None,resume_receipt=None):
        """Bounded upload; checkpoints contain a private, sensitive session URI.

        No implicit new-session retry or OAuth flow. Partial acknowledgements
        seek to the actual confirmed byte, within fixed request/wire budgets.
        Callers may persist an official files.generateIds identity before the
        first request; reconciliation and any same-ID retry remain external.
        """
        if expected_sha256 is not None and (not isinstance(expected_sha256,str) or not re.fullmatch('[0-9a-f]{64}',expected_sha256)):
            fail('INVALID_ARGUMENT','Expected upload SHA256 must be a lowercase hexadecimal digest')
        if expected_bytes is not None and (isinstance(expected_bytes,bool) or not isinstance(expected_bytes,int) or expected_bytes<0):
            fail('INVALID_ARGUMENT','Expected upload byte count must be a nonnegative integer')
        if preallocated_file_id is not None and (not isinstance(preallocated_file_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,256}',preallocated_file_id)):
            fail('INVALID_ARGUMENT','Preallocated upload identity must be a bounded Drive file ID')
        path=no_links(Path(path))
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        receipt={'name':name,'status':'preparing','upload_complete':False,'confirmed_bytes':0,'metadata_verified':False}
        resume_admitted=resume_receipt is None
        def checkpoint():
            self.last_upload_receipt=json.loads(json.dumps(receipt))
            if progress:progress(self.last_upload_receipt)
        try:
            with os.fdopen(fd,'rb') as stream:
                before=os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or before.st_size>MAX_UPLOAD_BYTES:fail('OUTPUT_LIMIT','Upload must be a regular single-link file of at most 256 MiB')
                size=before.st_size;md5=hashlib.md5();sha256=hashlib.sha256()
                if expected_bytes is not None and size!=expected_bytes:fail('REVISION_CONFLICT','Upload byte count differs from verified version identity')
                hashed=0;blocks=[]
                while hashed<=size:
                    chunk=stream.read(min(CHUNK_BYTES,size+1-hashed))
                    if not chunk:break
                    hashed+=len(chunk);md5.update(chunk);sha256.update(chunk);blocks.append(hashlib.sha256(chunk).digest())
                if hashed!=size:fail('REVISION_CONFLICT','Upload source changed during hashing')
                digest=sha256.hexdigest();stream.seek(0)
                if expected_sha256 is not None and digest!=expected_sha256:fail('REVISION_CONFLICT','Upload SHA256 differs from verified version identity')
                def unchanged():
                    if file_identity(before)!=file_identity(os.fstat(stream.fileno())):
                        fail('REVISION_CONFLICT','Upload descriptor changed after identity verification')
                unchanged()
                def capture(offset,length):
                    # Verify whole captured blocks before slicing unaligned
                    # resumed ranges. Only immutable verified bytes reach HTTP.
                    unchanged();parts=[];end=offset+length;cursor=offset
                    while cursor<end:
                        index=cursor//CHUNK_BYTES;start=index*CHUNK_BYTES
                        stream.seek(start);block=stream.read(min(CHUNK_BYTES,size-start))
                        if hashlib.sha256(block).digest()!=blocks[index]:fail('REVISION_CONFLICT','Upload source block changed after capture')
                        stop=min(end,start+len(block));parts.append(block[cursor-start:stop-start]);cursor=stop
                    unchanged();return b''.join(parts)
                receipt.update(bytes=size,sha256=digest,md5=md5.hexdigest(),status='uploading',
                    expected_sha256=expected_sha256,expected_bytes=expected_bytes,
                    expected_identity_verified=expected_sha256 is not None and expected_bytes is not None,parent_id=parent)
                if resume_receipt is not None:
                    prior=resume_receipt
                    if (not isinstance(prior,dict) or size<=MULTIPART_LIMIT or prior.get('transport')!='resumable'
                        or prior.get('name')!=name or prior.get('bytes')!=size or prior.get('sha256')!=digest
                        or prior.get('md5')!=md5.hexdigest() or prior.get('expected_identity_verified') is not True
                        or prior.get('upload_complete') is not False
                        or prior.get('parent_id',parent)!=parent
                        or any(type(prior.get(key,0)) is not int or prior.get(key,0)<0
                               for key in ['confirmed_bytes','requests','wire_bytes','session_transport_recoveries'])
                        or prior.get('confirmed_bytes',0)>size or prior.get('wire_bytes',0)>2*size):
                        fail('DRIVE_RECOVERY_MISMATCH','Saved resumable session does not match the immutable member')
                    receipt.update({key:prior[key] for key in ['confirmed_bytes','requests','wire_bytes',
                        'last_attempted_range','session_transport_recoveries','network_events'] if key in prior})
                    receipt.update(session_url=session_url(prior.get('session_url')),transport='resumable',
                                   resumed_existing_session=True,new_session_started=False)
                    resume_admitted=True
                checkpoint()
                mime=mimetypes.guess_type(name)[0] or 'application/octet-stream'
                metadata={'name':name,'parents':[parent],'mimeType':mime,'appProperties':{'sha256':digest,'software_id':'webcodex-paper-studio'}}
                if preallocated_file_id is not None:metadata['id']=preallocated_file_id
                if size<=MULTIPART_LIMIT:
                    receipt['transport']='multipart';boundary='paper-studio-'+secrets.token_hex(16)
                    raw=capture(0,size)
                    if len(raw)!=size:fail('REVISION_CONFLICT','Upload source changed during capture')
                    payload=('--'+boundary+'\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n'+json.dumps(metadata)+'\r\n--'+boundary+'\r\nContent-Type: '+mime+'\r\n\r\n').encode()+raw+('\r\n--'+boundary+'--\r\n').encode()
                    unchanged();result=self.request('POST','upload/drive/v3/files?uploadType=multipart&fields=id',payload,'multipart/related; boundary='+boundary)
                    receipt['confirmed_bytes']=size
                else:
                    receipt['transport']='resumable'
                    if resume_receipt is None:
                        payload=json.dumps(metadata).encode()
                        unchanged()
                        _,headers,_=self.exchange('POST',API_ROOT+'upload/drive/v3/files?uploadType=resumable&fields=id',payload,
                            {'Content-Type':'application/json; charset=UTF-8','Content-Length':str(len(payload)),
                             'X-Upload-Content-Type':mime,'X-Upload-Content-Length':str(size)},allowed=(200,))
                        location=next((v for k,v in headers.items() if k.lower()=='location'),None)
                        receipt.update(session_url=session_url(location),new_session_started=True);checkpoint()
                    offset=receipt.get('confirmed_bytes',0);sent=receipt.get('wire_bytes',0);requests=receipt.get('requests',0);result=None
                    request_limit=2*math.ceil(size/CHUNK_BYTES)+4
                    def transient(error):
                        return isinstance(error,StudioError) and (error.code=='DRIVE_INTERRUPTED' or
                            error.code=='DRIVE_HTTP_ERROR' and error.details.get('http_status') in {429,500,502,503,504})
                    def note(error,event):
                        detail={k:v for k,v in error.details.items() if k in
                            {'method','stage','exception_type','cause_type','errno','ssl_reason','http_status'}}
                        receipt.setdefault('network_events',[]).append({'event':event,'error_code':error.code,**detail})
                        receipt['network_events']=receipt['network_events'][-16:];checkpoint()
                    def probe():
                        nonlocal requests,offset,result
                        for attempt in range(3):
                            if requests>=request_limit:fail('DRIVE_RETRY_LIMIT','Bounded session request budget exhausted')
                            requests+=1;receipt['requests']=requests;checkpoint();unchanged()
                            try:
                                code,headers,raw=self.exchange('PUT',receipt['session_url'],b'',
                                    {'Content-Length':'0','Content-Range':f'bytes */{size}'},allowed=(200,201,308,404,410))
                                break
                            except StudioError as error:
                                if not transient(error) or attempt==2:raise
                                note(error,'resumable_status_interrupted')
                        if any(k.lower()=='location' for k in headers):fail('DRIVE_REDIRECT','Upload session relocation refused')
                        if code in {404,410}:fail('DRIVE_SESSION_EXPIRED','Saved resumable session expired; no new upload session was created',http_status=code,method='PUT',stage='resumable_status')
                        if code in {200,201}:
                            result=self.decode(raw);offset=size
                        else:
                            value=next((v for k,v in headers.items() if k.lower()=='range'),None)
                            match=re.fullmatch(r'bytes=0-(\d+)',value or '')
                            confirmed=int(match.group(1))+1 if match else 0
                            if (value is not None and not match or confirmed<receipt.get('confirmed_bytes',0)
                                or not 0<=confirmed<=size):
                                fail('DRIVE_RANGE_MISMATCH','Session status contradicts durable confirmed bytes')
                            attempted=receipt.get('last_attempted_range')
                            if attempted and (not isinstance(attempted,list) or len(attempted)!=2
                                or any(type(x) is not int for x in attempted) or confirmed>attempted[1]+1):
                                fail('DRIVE_RANGE_MISMATCH','Session status exceeds the last durable attempted range')
                            offset=confirmed
                        receipt.update(confirmed_bytes=offset,session_status_http=code,requests=requests)
                        checkpoint()
                    if resume_receipt is not None:probe()
                    while offset<size:
                        if requests>=request_limit or sent>=2*size:fail('DRIVE_RETRY_LIMIT','Bounded session request budget exhausted')
                        chunk=capture(offset,min(CHUNK_BYTES,size-offset))
                        if not chunk or len(chunk)!=min(CHUNK_BYTES,size-offset):fail('REVISION_CONFLICT','Upload source changed during streaming')
                        if sent+len(chunk)>2*size:fail('DRIVE_RETRY_LIMIT','Bounded session wire-byte budget exhausted')
                        end=offset+len(chunk);requests+=1;sent+=len(chunk)
                        receipt.update(requests=requests,wire_bytes=sent,last_attempted_range=[offset,end-1]);checkpoint()
                        unchanged()
                        try:
                            code,headers,raw=self.exchange('PUT',receipt['session_url'],chunk,
                                {'Content-Type':mime,'Content-Length':str(len(chunk)),'Content-Range':f'bytes {offset}-{end-1}/{size}'},allowed=(200,201,308))
                        except StudioError as error:
                            recovered=receipt.get('session_transport_recoveries',0)
                            if not transient(error) or recovered>=self.resumable_transport_recoveries:raise
                            receipt['session_transport_recoveries']=recovered+1
                            note(error,'resumable_chunk_interrupted');probe()
                            continue
                        if any(k.lower()=='location' for k in headers):fail('DRIVE_REDIRECT','Upload session relocation refused')
                        receipt.update(requests=requests,wire_bytes=sent)
                        if code==308:
                            value=next((v for k,v in headers.items() if k.lower()=='range'),None)
                            match=re.fullmatch(r'bytes=0-(\d+)',value or '')
                            confirmed=int(match.group(1))+1 if match else 0
                            if not match or confirmed<=offset or confirmed>end or confirmed>=size:
                                fail('DRIVE_RANGE_MISMATCH','Invalid or nonprogressing resumable acknowledgement')
                            offset=confirmed;receipt['confirmed_bytes']=offset;checkpoint()
                        else:
                            if end!=size:fail('DRIVE_RANGE_MISMATCH','Server completed upload before the final source byte')
                            result=self.decode(raw);receipt['confirmed_bytes']=size;break
                    if result is None:fail('DRIVE_INCOMPLETE','Session did not return a completed file')
                file_id=result.get('id')
                if not isinstance(file_id,str) or not file_id:fail('DRIVE_PROTOCOL','Completed upload did not return a file ID')
                if preallocated_file_id is not None and file_id!=preallocated_file_id:
                    fail('DRIVE_PROTOCOL','Completed upload returned a different file ID than the preallocated identity')
                receipt.update(id=file_id,status='verifying');checkpoint()
                meta=self.metadata(file_id)
                try:remote_size=int(meta.get('size',-1))
                except (ValueError,TypeError):remote_size=-1
                if (meta.get('id')!=file_id or meta.get('name')!=name or meta.get('mimeType')!=mime or meta.get('parents')!=[parent]
                    or remote_size!=size or meta.get('md5Checksum')!=md5.hexdigest() or meta.get('sha256Checksum')!=digest
                    or meta.get('appProperties',{}).get('sha256')!=digest or meta.get('appProperties',{}).get('software_id')!='webcodex-paper-studio'):
                    fail('DRIVE_READBACK_MISMATCH','Drive metadata/checksum verification failed',file_id=file_id)
                if not meta.get('permissions') or any(p.get('type')!='user' or p.get('role')!='owner' for p in meta['permissions']):
                    fail('DRIVE_NOT_PRIVATE','Uploaded file privacy could not be confirmed as owner-only',file_id=file_id)
                after=os.fstat(stream.fileno())
                if file_identity(before)!=file_identity(after):fail('REVISION_CONFLICT','Local upload source changed during publication')
                receipt.update(status='completed',upload_complete=True,metadata_verified=True);checkpoint()
                return {'id':file_id,'name':name,'bytes':size,'sha256':digest,'md5':md5.hexdigest(),'url':meta.get('webViewLink'),
                        'transport':receipt['transport'],'metadata_verified':True,'server_sha256_verified':True,
                        'expected_identity_verified':receipt['expected_identity_verified'],
                        **{key:receipt[key] for key in ['resumed_existing_session','new_session_started','session_transport_recoveries','network_events'] if key in receipt},
                        'chatgpt_fetched':False,'pages_seen':False,'human_accepted':False}
        except Exception as error:
            receipt.update(status='partial_upload_retained',upload_complete=False,error_code=error.code if isinstance(error,StudioError) else 'UPLOAD_FAILED')
            if isinstance(error,StudioError):
                receipt['error_details']={k:v for k,v in error.details.items() if k in
                    {'method','stage','exception_type','cause_type','errno','ssl_reason','http_status','attempts'}}
            if resume_admitted:checkpoint()
            raise

def public_receipt(value):
    """Remove upload capabilities/token/credential fields from job artifacts."""
    if isinstance(value,list):return [public_receipt(item) for item in value]
    if isinstance(value,dict):
        return {key:public_receipt(item) for key,item in value.items()
                if not any(word in key.lower() for word in ('session','token','secret','credential'))}
    return value

def publication_uploads(root,manifest,manifest_identity):
    members={member['path']:member for member in manifest['files']}
    def member_identity(relative):
        member=members.get(relative,{})
        if not isinstance(member.get('bytes'),int) or isinstance(member.get('bytes'),bool) or member['bytes']<0 or not re.fullmatch('[0-9a-f]{64}',member.get('sha256','')):
            fail('VERSION_TAMPERED','Publication member lacks verified SHA256/byte identity',path=relative)
        return {'sha256':member['sha256'],'bytes':member['bytes']}
    selected=[]
    for bid in manifest['build_ids']:
        base=root/'builds'/bid/'output'
        prefix='builds/'+bid+'/output/'
        # Only hash-verified manifest members may leave the machine. Extra
        # files inserted beside a published version are never implicitly sent.
        for member in sorted(manifest['files'],key=lambda item:item['path']):
            if member['path'].startswith(prefix) and Path(member['path']).suffix.lower() in ['.pdf','.png','.pptx','.svg']:
                p=root/member['path']
                selected.append((p,bid+'_'+p.relative_to(base).as_posix().replace('/','_'),member_identity(member['path'])))
    if not any(p.suffix.lower()=='.pdf' for p,_,_ in selected):fail('DRIVE_NO_PDF','Selected version contains no PDF')
    uploads=[(root/'manifest.json','manifest.json',manifest_identity),(root/'CHANGELOG.md','CHANGELOG.md',member_identity('CHANGELOG.md')),*selected]
    # New managed releases opt in with an immutable VERSION descriptor. Legacy
    # publication plans remain byte-for-byte stable for idempotent recovery.
    if 'VERSION.json' in members:
        descriptor=json.loads(safe_read(root,'VERSION.json',2*1024*1024))
        if descriptor.get('project_id')!=manifest['project_id'] or descriptor.get('version')!=manifest['version']:
            fail('VERSION_TAMPERED','Managed publication identity differs from version manifest')
        for member in sorted(manifest['files'],key=lambda item:item['path']):
            relative=member['path']
            if relative=='VERSION.json' or relative.startswith(('release/','revisions/','figures/','dependencies/')) or '/source/' in relative:
                uploads.append((root/relative,'source_'+relative.replace('/','_'),member_identity(relative)))
    if sum(identity['bytes'] for _,_,identity in uploads)>MAX_UPLOAD_BYTES:fail('OUTPUT_LIMIT','Review upload exceeds 256 MiB including manifest/changelog')
    names=[name for _,name,_ in uploads]
    if len(names)!=len(set(names)):fail('VERSION_TAMPERED','Publication names collide after flattening')
    return uploads

def publish_version(cfg,label,*,expected_manifest_sha256=None,deadline_seconds=600):
    if isinstance(deadline_seconds,bool) or not isinstance(deadline_seconds,(int,float)) or not math.isfinite(deadline_seconds) or not 0<deadline_seconds<=600:
        fail('INVALID_ARGUMENT','Drive deadline must be positive and at most 600 seconds')
    deadline=time.monotonic()+deadline_seconds
    root,manifest,manifest_identity=verified_manifest(cfg,label)
    if expected_manifest_sha256 is not None and manifest_identity['sha256']!=expected_manifest_sha256:
        fail('VERSION_TAMPERED','Version manifest no longer matches the queued publication identity')
    state=no_links(Path(cfg['state_root']));state.mkdir(mode=0o700,parents=True,exist_ok=True)
    private=state/'drive-publications';private.mkdir(mode=0o700,exist_ok=True);no_links(private)
    for directory in (state,private):
        info=directory.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:
            fail('DRIVE_RECEIPT_PERMISSIONS','Publication/session receipts require a private owned native-filesystem state directory')
    target=no_links(private/f'{label}.json')
    if target.exists():fail('DRIVE_PUBLICATION_EXISTS','Retain the prior publication/partial receipt; implicit restart is disabled')
    from paper_studio.versions.current_inputs import verify_version_current
    current_proofs=verify_version_current(cfg,label)
    publication_scope='manuscript' if any(proof['formal_manuscript'] for proof in current_proofs) else 'auxiliary'
    parent=cfg.get('drive_folder_id')
    if not parent:fail('DRIVE_FOLDER_REQUIRED','Configure an independently authorized private review folder before publication')
    uploads=publication_uploads(root,manifest,manifest_identity)
    oauth=cfg.get('drive_oauth_file',str(Path.home()/'.config/webcodex-paper-studio/drive-oauth.json'))
    drive=Drive(oauth,persist_access_token=False,deadline=deadline);drive.private(parent)
    project_folder=drive.folder(cfg['project_id'],parent)
    receipt={'schema':'paper-studio.drive-publication.v1','project_id':cfg['project_id'],'version':label,'folder_id':None,'version_manifest_sha256':manifest_identity['sha256'],'publication_scope':publication_scope,'files':[],'upload_complete':False,'status':'partial_upload_retained','chatgpt_fetched':False,'pages_seen':False,'human_accepted':False}
    checkpoint_owned=False
    def folder_checkpoint(value):
        nonlocal checkpoint_owned
        receipt['folder_id']=value['id'];receipt['folder_creation']=value
        atomic_json(target,receipt,exclusive=not checkpoint_owned);checkpoint_owned=True
    # The reserved ID and attempted marker reach native private storage before
    # the first folder POST, including when its response or readback is lost.
    folder=drive.folder(label,project_folder,exclusive=True,progress=folder_checkpoint)
    receipt['folder_id']=folder
    atomic_json(target,receipt,exclusive=not checkpoint_owned)
    def checkpoint(value):
        receipt['current_upload']=value;atomic_json(target,receipt)
    try:
        for p,name,identity in uploads:
            uploaded=drive.upload(p,name,folder,progress=checkpoint,expected_sha256=identity['sha256'],expected_bytes=identity['bytes'])
            relative=p.relative_to(root).as_posix()
            uploaded.update(version_path=relative,build_id=relative.split('/')[1] if relative.startswith('builds/') else None)
            receipt['files'].append(uploaded);receipt.pop('current_upload',None);atomic_json(target,receipt)
        if sha(safe_read(root,'manifest.json',2*1024**2)) != manifest_identity['sha256']:
            fail('VERSION_TAMPERED','Publication manifest changed during upload')
        verify_version_current(cfg,label)
        receipt['upload_complete']=True;receipt['completed_at']=time.time();receipt['status']='completed'
    except Exception:
        receipt['status']='partial_upload_retained';atomic_json(target,receipt);raise
    atomic_json(target,receipt);return receipt


def recovery_receipt_path(cfg,label,recovery_id):
    """Private recovery sidecar; never replaces the original publication receipt."""
    if not isinstance(label,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}',label) or label in {'.','..'}:
        fail('INVALID_ARGUMENT','Invalid version label')
    if not isinstance(recovery_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',recovery_id) or recovery_id=='progress':
        fail('INVALID_ARGUMENT','Invalid recovery_id')
    return no_links(Path(cfg['state_root'])/'drive-recoveries'/label/(recovery_id+'.json'))


class RecoveryDrive(Drive):
    """Bounded GET retries and status-reconciled retries of the same upload session."""
    resumable_transport_recoveries=4
    def __init__(self,*args,on_retry=None,**kwargs):
        self.on_retry=on_retry
        super().__init__(*args,**kwargs)
    def request(self,method,path,body=None,content_type='application/json'):
        for attempt in range(3):
            try:return super().request(method,path,body,content_type)
            except StudioError as error:
                status=error.details.get('http_status')
                transient=(error.code=='DRIVE_INTERRUPTED' or error.code=='DRIVE_HTTP_ERROR' and isinstance(status,int) and
                           (status==429 or 500<=status<=599))
                if method!='GET' or not transient:raise
                if attempt==2:
                    fail(error.code,'Drive read failed after bounded retries; publication progress retained',
                         **{**error.details,'attempts':3,'operation':'GET'})
                delay=0.25*(2**attempt)
                self.timeout(delay)
                if self.on_retry:self.on_retry({'event':'get_retry','attempt':attempt+1,'error_code':error.code,'http_status':error.details.get('http_status')})
                deadline=getattr(self,'deadline',None)
                if deadline is not None and deadline-time.monotonic()<=delay:fail('DRIVE_DEADLINE','Recovery GET retry would exceed deadline')
                time.sleep(delay)


def _private_state_directory(path):
    no_links(path);path.mkdir(mode=0o700,parents=True,exist_ok=True)
    info=path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:
        fail('DRIVE_RECEIPT_PERMISSIONS','Recovery receipts require a private owned state directory')


def _drive_id(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,256}',value):fail('DRIVE_PROTOCOL','Invalid Drive object identity')
    return value


def _verify_recovery_file(drive,file_id,name,folder,identity,md5):
    file_id=_drive_id(file_id)
    unique=_find_attempted_file(drive,name,folder)
    if unique!=file_id:fail('DRIVE_RECOVERY_MISMATCH','Folder/name resolves to a different object than the recorded file ID',name=name)
    return _verified_recovery_metadata(drive.private(file_id),file_id,name,folder,identity,md5)


# One complete child listing replaces two GETs per already-existing object
# (name query + metadata) during reconciliation. The same identity checks apply;
# a listing row never establishes authority by name alone.
INDEX_FIELDS=('nextPageToken,incompleteSearch,files(id,name,mimeType,size,md5Checksum,sha256Checksum,'
              'parents,webViewLink,permissions(type,role),appProperties)')
INDEX_ROW_FIELDS=('id','name','mimeType','size','md5Checksum','sha256Checksum','parents','appProperties','permissions')
INDEX_PAGE_LIMIT=64


def _folder_index(drive,folder):
    """Return a complete, bounded name index of one folder's untrashed children.

    An incomplete or unbounded listing is an error, never evidence of absence.
    """
    folder=_drive_id(folder)
    query="trashed=false and '"+folder+"' in parents"
    names={};token=None;rows_seen=0
    for _ in range(INDEX_PAGE_LIMIT):
        params={'q':query,'fields':INDEX_FIELDS,'pageSize':1000}
        if token:params['pageToken']=token
        response=drive.request('GET','drive/v3/files?'+urllib.parse.urlencode(params))
        rows=response.get('files')
        if response.get('incompleteSearch') or not isinstance(rows,list):
            fail('DRIVE_RECOVERY_UNCERTAIN','Drive folder listing was incomplete; no object may be assumed absent',folder_id=folder)
        for row in rows:
            if not isinstance(row,dict) or not isinstance(row.get('name'),str):fail('DRIVE_PROTOCOL','Invalid Drive folder listing row')
            names.setdefault(row['name'],[]).append(row);rows_seen+=1
        token=response.get('nextPageToken')
        if not token:return {'folder':folder,'names':names,'rows':rows_seen}
        if not isinstance(token,str):fail('DRIVE_PROTOCOL','Invalid Drive listing page token')
    fail('DRIVE_RECOVERY_UNCERTAIN','Drive folder listing exceeded its bounded page budget',folder_id=folder)


def _try_folder_index(drive,folder):
    """A listing request rejected as malformed (HTTP 400) keeps the per-file checks.

    Every other failure, including an incomplete listing, still fails closed.
    """
    try:return _folder_index(drive,folder)
    except StudioError as error:
        if error.code=='DRIVE_HTTP_ERROR' and error.details.get('http_status')==400:return None
        raise


def _reconcile_id(drive,index,name,folder,*,allow_absent=False):
    if index is None:return _find_attempted_file(drive,name,folder,allow_absent=allow_absent)
    return _indexed_id(index,name,allow_absent=allow_absent)


def _verify_known_file(drive,index,file_id,name,folder,identity,md5):
    if index is None:return _verify_recovery_file(drive,file_id,name,folder,identity,md5)
    return _verify_indexed_file(drive,index,file_id,name,folder,identity,md5)


def _indexed_id(index,name,*,allow_absent=False):
    rows=index['names'].get(name,[])
    if allow_absent and not rows:return None
    if len(rows)!=1 or rows[0].get('name')!=name:
        fail('DRIVE_RECOVERY_UNCERTAIN','Previously attempted upload has no unique visible object; refusing to upload another copy',name=name,visible_matches=len(rows))
    return _drive_id(rows[0].get('id'))


def _verify_indexed_file(drive,index,file_id,name,folder,identity,md5):
    """Verify from a fresh complete listing; fall back to metadata GET if fields are absent."""
    file_id=_drive_id(file_id)
    if index.get('folder')!=folder:fail('DRIVE_RECOVERY_MISMATCH','Folder listing belongs to another Drive folder',name=name)
    if _indexed_id(index,name)!=file_id:fail('DRIVE_RECOVERY_MISMATCH','Folder/name resolves to a different object than the recorded file ID',name=name)
    meta=index['names'][name][0]
    if any(field not in meta for field in INDEX_ROW_FIELDS):
        meta=drive.private(file_id)
    elif not meta['permissions'] or any(not isinstance(p,dict) or p.get('type')!='user' or p.get('role')!='owner' for p in meta['permissions']):
        fail('DRIVE_NOT_PRIVATE','Recovery object privacy could not be confirmed as owner-only',file_id=file_id)
    return _verified_recovery_metadata(meta,file_id,name,folder,identity,md5)


def _verified_recovery_metadata(meta,file_id,name,folder,identity,md5):
    try:size=int(meta.get('size',-1))
    except (TypeError,ValueError):size=-1
    mime=mimetypes.guess_type(name)[0] or 'application/octet-stream'
    if (meta.get('id')!=file_id or meta.get('name')!=name or meta.get('mimeType')!=mime
        or meta.get('parents')!=[folder] or size!=identity['bytes'] or meta.get('md5Checksum')!=md5
        or meta.get('sha256Checksum')!=identity['sha256']
        or not isinstance(meta.get('appProperties') or {},dict)
        or (meta.get('appProperties') or {}).get('sha256')!=identity['sha256']
        or (meta.get('appProperties') or {}).get('software_id')!='webcodex-paper-studio'):
        fail('DRIVE_READBACK_MISMATCH','Recovery object identity/checksum does not match the immutable version',file_id=file_id,name=name)
    return {'id':file_id,'name':name,'bytes':identity['bytes'],'sha256':identity['sha256'],'md5':md5,
            'url':meta.get('webViewLink'),'metadata_verified':True,'server_sha256_verified':True,
            'expected_identity_verified':True,'chatgpt_fetched':False,'pages_seen':False,'human_accepted':False}


def _find_attempted_file(drive,name,folder,*,allow_absent=False):
    literal=lambda value:value.replace('\\','\\\\').replace("'","\\'")
    query="trashed=false and name='"+literal(name)+"' and '"+literal(folder)+"' in parents"
    response=drive.request('GET','drive/v3/files?'+urllib.parse.urlencode(
        {'q':query,'fields':'nextPageToken,incompleteSearch,files(id,name)','pageSize':100}))
    files=response.get('files')
    if allow_absent and files==[] and not response.get('nextPageToken') and not response.get('incompleteSearch'):return None
    if (not isinstance(files,list) or response.get('nextPageToken') or response.get('incompleteSearch')
            or len(files)!=1 or files[0].get('name')!=name):
        fail('DRIVE_RECOVERY_UNCERTAIN','Previously attempted upload has no unique visible object; refusing to upload another copy',name=name,visible_matches=len(files) if isinstance(files,list) else None)
    return _drive_id(files[0].get('id'))


def _verify_recovery_folders(drive,cfg,label,folder,parent):
    review_parent=drive.private(parent)
    version_folder=drive.private(folder)
    ancestors=version_folder.get('parents',[])
    if (review_parent.get('id')!=parent or version_folder.get('id')!=folder or version_folder.get('name')!=label
        or version_folder.get('mimeType')!='application/vnd.google-apps.folder' or len(ancestors)!=1):
        fail('DRIVE_RECOVERY_MISMATCH','Original version folder does not match its publication identity')
    project_folder=drive.private(_drive_id(ancestors[0]))
    if (project_folder.get('id')!=ancestors[0] or project_folder.get('name')!=cfg['project_id'] or project_folder.get('mimeType')!='application/vnd.google-apps.folder'
        or project_folder.get('parents')!=[parent]):fail('PROJECT_DENIED','Original Drive folder is outside this project review hierarchy')


def _resume_journaled_folder(drive,cfg,label,original,ledger,persist):
    """Recover only a previously reserved folder ID; never choose a new one."""
    intent=original.get('folder_creation')
    if intent is None:return
    folder=_drive_id(original.get('folder_id'));parent=_drive_id(cfg.get('drive_folder_id'))
    if (not isinstance(intent,dict) or intent.get('schema')!='paper-studio.drive-folder-creation.v1'
        or intent.get('id')!=folder or intent.get('name')!=label
        or type(intent.get('attempted')) is not bool or type(intent.get('verified')) is not bool
        or type(intent.get('attempts')) is not int or intent['attempts'] not in (0,1)
        or intent['attempted']!=(intent['attempts']==1) or intent['verified'] and not intent['attempted']):
        fail('DRIVE_RECOVERY_MISMATCH','Version folder creation journal is invalid')
    project_folder=_drive_id(intent.get('parent_id'));project_meta=drive.private(project_folder)
    if (project_meta.get('id')!=project_folder or project_meta.get('name')!=cfg['project_id']
        or project_meta.get('mimeType')!='application/vnd.google-apps.folder'
        or project_meta.get('parents')!=[parent]):
        fail('PROJECT_DENIED','Reserved folder creation is outside this project review hierarchy')
    drive.private(parent)
    saved=ledger.setdefault('folder_creation',{'id':folder,'attempts':intent['attempts'],'verified':False})
    if (saved.get('id')!=folder or type(saved.get('attempts')) is not int
        or not intent['attempts']<=saved['attempts']<=2):
        fail('DRIVE_RECOVERY_MISMATCH','Recovery folder journal differs from its reserved identity')
    try:
        drive.private(folder)
    except StudioError as error:
        if error.code!='DRIVE_HTTP_ERROR' or error.details.get('http_status')!=404:raise
        # A previously verified folder was later removed; do not silently
        # recreate published history or a folder with known file uploads.
        if intent['verified'] or saved.get('verified') or original.get('files') or original.get('current_upload'):
            fail('DRIVE_RECOVERY_MISMATCH','Previously verified publication folder is missing')
        collisions=_folder_index(drive,project_folder)['names'].get(label,[])
        if collisions:fail('DRIVE_AMBIGUOUS','Another version folder occupies the reserved name; no new folder created')
        if saved['attempts']>=2:fail('DRIVE_RECOVERY_UNCERTAIN','Reserved folder create attempt budget is exhausted')
        saved['attempts']+=1
        persist({'event':'reserved_folder_create_attempt_marked','id':folder})
        try:
            result=drive.request('POST','drive/v3/files?fields=id',
                {'id':folder,'name':label,'mimeType':'application/vnd.google-apps.folder','parents':[project_folder]})
            if result.get('id')!=folder:fail('DRIVE_RECOVERY_MISMATCH','Created folder differs from its reserved identity')
        except StudioError as post_error:
            # Same globally reserved ID cannot create a second object. A 409
            # may be a lost earlier acknowledgement, so verify that exact ID.
            if post_error.code!='DRIVE_HTTP_ERROR' or post_error.details.get('http_status')!=409:raise
    _verify_recovery_folders(drive,cfg,label,folder,parent)
    saved['verified']=True;persist({'event':'reserved_folder_verified','id':folder})


def recover_version(cfg,label,recovery_id,expected_manifest_sha256,*,deadline_seconds=600):
    """Reconcile a failed publication in its original folder, without blind POSTs.

    A shared per-version progress ledger carries upload attempts across different
    recovery jobs; each job also retains its own sidecar. The original failed
    receipt is only read. An upload attempt is durably marked before admission.
    If its response is lost, only a unique fully verified existing object can be
    reused; an absent/ambiguous result is never permission to upload again.
    """
    if isinstance(deadline_seconds,bool) or not isinstance(deadline_seconds,(int,float)) or not math.isfinite(deadline_seconds) or not 0<deadline_seconds<=600:
        fail('INVALID_ARGUMENT','Recovery deadline must be positive and at most 600 seconds')
    if not isinstance(expected_manifest_sha256,str) or not re.fullmatch(r'[0-9a-f]{64}',expected_manifest_sha256):fail('INVALID_ARGUMENT','Recovery requires the exact expected version manifest SHA256')
    target=recovery_receipt_path(cfg,label,recovery_id)
    state=no_links(Path(cfg['state_root']));_private_state_directory(state)
    for directory in [target.parent.parent,target.parent]:_private_state_directory(directory)
    deadline=time.monotonic()+deadline_seconds
    with locked(cfg,'drive-version-'+sha(label.encode())[:24]):
        root,manifest,manifest_identity=verified_manifest(cfg,label)
        if manifest_identity['sha256']!=expected_manifest_sha256:fail('VERSION_TAMPERED','Recovery version manifest differs from the requested identity')
        from paper_studio.versions.current_inputs import verify_version_current
        completed_replay = json.loads(safe_read(target.parent,target.name,RESPONSE_LIMIT)) if target.exists() else {}
        if completed_replay.get('status') != 'completed' or completed_replay.get('upload_complete') is not True:
            verify_version_current(cfg,label)
        uploads=publication_uploads(root,manifest,manifest_identity)
        original_path=no_links(state/'drive-publications'/f'{label}.json')
        original_raw=safe_read(original_path.parent,original_path.name,RESPONSE_LIMIT)
        original=json.loads(original_raw)
        if (original.get('schema')!='paper-studio.drive-publication.v1' or original.get('project_id')!=cfg['project_id']
            or original.get('version')!=label or original.get('version_manifest_sha256')!=expected_manifest_sha256):
            fail('PROJECT_DENIED','Original publication project/version/manifest identity mismatch')
        if original.get('upload_complete') is True:fail('DRIVE_RECOVERY_NOT_REQUIRED','Original publication is already complete')
        if original.get('status') not in {None,'partial_upload_retained','failed'}:
            fail('DRIVE_RECOVERY_NOT_READY','Original publication is not in an explicit failed/partial state')
        folder=_drive_id(original.get('folder_id'));parent=_drive_id(cfg.get('drive_folder_id'))
        ledger_path=no_links(target.parent/'progress.json')
        expected_entries={}
        for path,name,identity in uploads:
            relative=path.relative_to(root).as_posix();raw=safe_read(root,relative,MAX_UPLOAD_BYTES)
            if len(raw)!=identity['bytes'] or sha(raw)!=identity['sha256']:fail('VERSION_TAMPERED','Recovery source changed after verification',path=relative)
            expected_entries[name]={'name':name,'version_path':relative,'bytes':identity['bytes'],'sha256':identity['sha256'],
                                    'md5':hashlib.md5(raw).hexdigest(),'attempted':False}
        anchor={'project_id':cfg['project_id'],'version':label,'version_manifest_sha256':expected_manifest_sha256,
                'folder_id':folder,'original_receipt_sha256':sha(original_raw)}
        if ledger_path.exists():
            ledger=json.loads(safe_read(ledger_path.parent,ledger_path.name,RESPONSE_LIMIT))
            if any(ledger.get(k)!=v for k,v in anchor.items()):fail('DRIVE_RECOVERY_MISMATCH','Recovery ledger is bound to a different original publication')
            entries=ledger.get('entries',{})
            if set(entries)!=set(expected_entries):fail('DRIVE_RECOVERY_MISMATCH','Recovery upload plan differs from the immutable version')
            for name,entry in expected_entries.items():
                if any(entries[name].get(k)!=entry[k] for k in ['name','version_path','bytes','sha256','md5']):fail('DRIVE_RECOVERY_MISMATCH','Recovery member identity differs from the immutable version',name=name)
        else:
            ledger={'schema':'paper-studio.drive-recovery-progress.v1',**anchor,'entries':expected_entries,'events':[]}
            entries=ledger['entries']
            observed=list(original.get('files',[]))
            current=original.get('current_upload')
            if current is not None:observed.append(current)
            seen=set()
            for previous in observed:
                name=previous.get('name')
                if name not in entries or name in seen:fail('DRIVE_RECOVERY_MISMATCH','Original receipt has unknown or duplicated upload names')
                seen.add(name);entry=entries[name]
                for key in ['bytes','sha256','md5']:
                    if key in previous and previous[key]!=entry[key]:fail('DRIVE_RECOVERY_MISMATCH','Original upload identity differs from the immutable version',name=name)
                entry['attempted']=True
                if previous.get('id'):entry['id']=_drive_id(previous['id'])
                entry['original_upload']=previous
            atomic_json(ledger_path,ledger,True)
        from paper_studio.drive.resolution import effective_entries,upload_replacement
        entries,resolutions=effective_entries(cfg,label,ledger,anchor)
        prior=None
        if target.exists():
            prior=json.loads(safe_read(target.parent,target.name,RESPONSE_LIMIT))
            if any(prior.get(k)!=v for k,v in anchor.items()) or prior.get('recovery_id')!=recovery_id:fail('DRIVE_RECOVERY_MISMATCH','Recovery ID is bound to another publication')
        if prior is not None and prior.get('status')=='completed' and prior.get('upload_complete') is True:
            # Revalidate cloud state without mutating this job's completed
            # sidecar or the shared ledger. Terminal evidence stays immutable.
            oauth=cfg.get('drive_oauth_file',str(Path.home()/'.config/webcodex-paper-studio/drive-oauth.json'))
            drive=RecoveryDrive(oauth,persist_access_token=False,deadline=deadline)
            _verify_recovery_folders(drive,cfg,label,folder,parent)
            prior_files=prior.get('files')
            if not isinstance(prior_files,list) or len(prior_files)!=len(entries):fail('DRIVE_RECOVERY_MISMATCH','Completed recovery file count differs from the immutable plan')
            previous={item.get('name'):item for item in prior_files if isinstance(item,dict)}
            if len(previous)!=len(prior_files) or set(previous)!=set(entries):fail('DRIVE_RECOVERY_MISMATCH','Completed recovery file names are duplicated or differ from the immutable plan')
            counts=[prior.get('reused_count'),prior.get('new_count')]
            if any(type(value) is not int or value<0 for value in counts) or sum(counts)!=len(entries):fail('DRIVE_RECOVERY_MISMATCH','Completed recovery counts are inconsistent')
            actions=[item.get('recovery_action') for item in prior_files]
            if actions.count('reused')!=counts[0] or actions.count('uploaded')!=counts[1]:fail('DRIVE_RECOVERY_MISMATCH','Completed recovery action counts are inconsistent')
            for _,name,identity in uploads:
                entry=entries[name];saved=previous[name]
                expected_build_id=entry['version_path'].split('/')[1] if entry['version_path'].startswith('builds/') else None
                if (any(saved.get(key)!=entry.get(key) for key in ['id','name','bytes','sha256','md5','version_path'])
                    or saved.get('build_id')!=expected_build_id
                    or any(saved.get(key) is not True for key in ['metadata_verified','server_sha256_verified','expected_identity_verified'])):
                    fail('DRIVE_RECOVERY_MISMATCH','Completed recovery file identity differs from its ledger',name=name)
                if name in resolutions and (saved.get('remote_name')!=entry['remote_name']
                    or saved.get('resolution_sha256')!=entry['resolution_sha256']
                    or saved.get('possible_prior_remote_object') is not True):
                    fail('DRIVE_RESOLUTION_DENIED','Completed replacement mapping differs from approved resolution')
            # One fresh complete listing verifies every terminal object identity.
            index=_try_folder_index(drive,folder)
            for _,name,identity in uploads:
                _verify_known_file(drive,index,entries[name].get('id'),entries[name].get('remote_name',name),folder,identity,entries[name]['md5'])
            if safe_read(original_path.parent,original_path.name,RESPONSE_LIMIT)!=original_raw:fail('DRIVE_RECOVERY_MISMATCH','Original publication receipt changed during verification')
            return prior
        verify_version_current(cfg,label)
        receipt={'schema':'paper-studio.drive-recovery.v1',**anchor,'recovery_id':recovery_id,'status':'recovering',
                 'upload_complete':False,'files':[],'reused_count':0,'new_count':0,'chatgpt_fetched':False,'pages_seen':False,'human_accepted':False}
        def persist(event=None):
            if event:
                ledger.setdefault('events',[]).append({'at':time.time(),**event})
                ledger['events']=ledger['events'][-128:]
            receipt['events']=ledger.get('events',[])
            # Persist shared attempts/IDs first. A crash may leave this job's
            # sidecar behind, but the next recovery never forgets an upload.
            atomic_json(ledger_path,ledger);atomic_json(target,receipt)
        persist({'event':'recovery_started','recovery_id':recovery_id})
        drive=None
        try:
            oauth=cfg.get('drive_oauth_file',str(Path.home()/'.config/webcodex-paper-studio/drive-oauth.json'))
            drive=RecoveryDrive(oauth,persist_access_token=False,deadline=deadline,on_retry=persist)
            _resume_journaled_folder(drive,cfg,label,original,ledger,persist)
            _verify_recovery_folders(drive,cfg,label,folder,parent)
            # Objects that already exist are reconciled from one complete
            # listing taken before any POST in this run; newly uploaded objects
            # are verified individually below, then all are revalidated again.
            index=_try_folder_index(drive,folder)
            for path,name,identity in uploads:
                entry=entries[name]
                if name in resolutions:
                    verified,reused=upload_replacement(drive,path,folder,identity,entry,persist)
                    verified.update(name=name,remote_name=entry['remote_name'],
                                    resolution_sha256=entry['resolution_sha256'],possible_prior_remote_object=True)
                else:
                    reused=bool(entry.get('attempted'))
                    if not reused:
                        discovered=_reconcile_id(drive,index,name,folder,allow_absent=True)
                        if discovered is not None:
                            # Even an unattempted ledger slot can have a preexisting
                            # remote object. Reconcile it before considering POST.
                            _verify_known_file(drive,index,discovered,name,folder,identity,entry['md5'])
                            entry.update(attempted=True,id=discovered);reused=True
                            persist({'event':'unrecorded_object_discovered','name':name,'id':discovered})
                    if reused:
                        file_id=entry.get('id')
                        previous_upload=entry.get('last_upload',entry.get('original_upload',{}))
                        can_resume=(file_id is None and previous_upload.get('transport')=='resumable'
                                    and isinstance(previous_upload.get('session_url'),str))
                        if file_id is None:
                            file_id=_reconcile_id(drive,index,name,folder,allow_absent=can_resume)
                            if file_id is not None:
                                entry['id']=file_id;persist({'event':'object_discovered','name':name,'id':file_id})
                        if file_id is None and can_resume:
                            # The private ledger binds this capability to the
                            # exact original folder and immutable member. Query
                            # its accepted offset before sending any more bytes;
                            # never create another upload session for this slot.
                            saved_upload=json.loads(json.dumps(previous_upload))
                            persist({'event':'resumable_session_reconcile_started','name':name,
                                     'confirmed_bytes':saved_upload.get('confirmed_bytes',0)})
                            def resumed_progress(value):
                                receipt['current_upload']=value;entry['last_upload']=value
                                if value.get('id'):entry['id']=_drive_id(value['id'])
                                persist()
                            uploaded=drive.upload(path,name,folder,progress=resumed_progress,
                                expected_sha256=identity['sha256'],expected_bytes=identity['bytes'],resume_receipt=saved_upload)
                            file_id=_drive_id(uploaded.get('id'));entry['id']=file_id
                            persist({'event':'resumable_session_completed','name':name,'id':file_id})
                            verified=_verify_recovery_file(drive,file_id,name,folder,identity,entry['md5'])
                            verified.update({key:uploaded[key] for key in ['resumed_existing_session','new_session_started',
                                'session_transport_recoveries','network_events'] if key in uploaded})
                        else:
                            verified=_verify_known_file(drive,index,file_id,name,folder,identity,entry['md5'])
                    else:
                        entry['attempted']=True;entry['attempted_at']=time.time()
                        persist({'event':'upload_attempt_marked','name':name})
                        def progress(value):
                            receipt['current_upload']=value;entry['last_upload']=value
                            if value.get('id'):entry['id']=_drive_id(value['id'])
                            persist()
                        uploaded=drive.upload(path,name,folder,progress=progress,
                            expected_sha256=identity['sha256'],expected_bytes=identity['bytes'])
                        entry['id']=_drive_id(uploaded.get('id'));persist({'event':'upload_returned','name':name,'id':entry['id']})
                        verified=_verify_recovery_file(drive,entry['id'],name,folder,identity,entry['md5'])
                        verified.update({key:uploaded[key] for key in ['session_transport_recoveries','network_events'] if key in uploaded})
                verified.update(version_path=entry['version_path'],build_id=entry['version_path'].split('/')[1] if entry['version_path'].startswith('builds/') else None,
                                recovery_action='reused' if reused else 'uploaded')
                entry['verified']=True;entry['verified_at']=time.time();receipt['files'].append(verified)
                receipt['reused_count' if reused else 'new_count']+=1;receipt.pop('current_upload',None)
                persist({'event':'object_verified','name':name,'id':entry['id']})
            # Revalidate all remote identities after the final upload with a
            # fresh complete listing. A prior result alone cannot establish
            # complete recovery on this run.
            final=_try_folder_index(drive,folder)
            for _,name,identity in uploads:_verify_known_file(drive,final,entries[name]['id'],entries[name].get('remote_name',name),folder,identity,entries[name]['md5'])
            if safe_read(original_path.parent,original_path.name,RESPONSE_LIMIT)!=original_raw:fail('DRIVE_RECOVERY_MISMATCH','Original failed publication receipt changed during recovery')
            if sha(safe_read(root,'manifest.json',2*1024**2)) != manifest_identity['sha256']:
                fail('VERSION_TAMPERED','Publication manifest changed during recovery')
            verify_version_current(cfg,label)
            receipt.update(status='completed',upload_complete=True,completed_at=time.time())
            persist({'event':'recovery_completed','recovery_id':recovery_id})
            return receipt
        except Exception as error:
            receipt.update(status='partial_upload_retained',upload_complete=False,error=error.as_dict() if isinstance(error,StudioError)
                           else {'code':'DRIVE_RECOVERY_FAILED','message':type(error).__name__})
            persist({'event':'recovery_failed','error_code':receipt['error']['code']})
            raise
