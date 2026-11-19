"""Create-only Reviews intake, deduplicated across requests by remote ID/SHA.

All transport and project-folder checks reuse source_sync/Publisher. The
object ledger is private, holds no OAuth/session information, and never lets
an incoming review replace a manuscript or an existing edited local review.
"""
from __future__ import annotations

import hashlib,re,time,urllib.parse
from pathlib import Path,PurePosixPath
from paper_studio.drive import publisher,source_sync as sync
from paper_studio.security.policy import (StudioError,atomic_json,encoded,fail,locked,
    no_links,path_for,safe_read,safe_write_new,sha)


def _destination(cfg,policy,path):
    path_for(cfg,path,True)
    if not PurePosixPath(path).is_relative_to(policy['reviews_dir']) or PurePosixPath(path).suffix.lower() not in sync.REVIEW_SUFFIXES:
        fail('WRITE_DENIED','Reviews may only create new non-executable files in the configured reviews directory')
    return Path(cfg['project_root'])/path


def _verify_local(cfg,policy,path,identity):
    _destination(cfg,policy,path)
    raw=safe_read(cfg['project_root'],path,policy['max_review_bytes'])
    if len(raw)!=identity['bytes'] or sha(raw)!=identity['sha256'] or hashlib.md5(raw).hexdigest()!=identity['md5']:
        fail('REVISION_CONFLICT','Previously imported review changed locally; no incoming bytes may overwrite it')


def _object(cfg,policy,private,identity,persist=False):
    """Find the one canonical local copy, including earlier v1 import receipts."""
    anchor={'project_id':cfg['project_id'],'project_root':cfg['project_root'],
            **{key:identity[key] for key in ['remote_file_id','bytes','sha256','md5']}}
    directory=private/'review-objects';key=sha(encoded([anchor['project_id'],anchor['remote_file_id'],anchor['sha256']]))
    path=directory/(key+'.json');found=None
    if path.exists():
        found=sync.load_json(path)
        if any(found.get(field)!=value for field,value in anchor.items()):fail('IDEMPOTENCY_CONFLICT','Review object ledger belongs to a different project or content identity')
    else:
        imports=private/'imports'
        receipts=list(imports.glob('*.json')) if imports.exists() else []
        if len(receipts)>4096:fail('INPUT_LIMIT','Review reconciliation exceeds the bounded import-receipt history')
        choices={}
        for receipt_path in receipts:
            old=sync.load_json(receipt_path)
            if (all(old.get(field)==value for field,value in anchor.items() if field!='project_root')
                and (old.get('import_complete') is True or old.get('creation_attempted') is True)):
                choices[old['destination']]=old
        if len(choices)>1:fail('DRIVE_REVIEW_AMBIGUOUS','Earlier imports contain multiple local copies of the same remote ID/SHA; preserve them for explicit reconciliation')
        if choices:
            destination,old=next(iter(choices.items()))
            found={**anchor,'destination':destination,'origin_request_id':old['request_id'],
                   'schema':'paper-editor.review-object.v1'}
    if found:
        _verify_local(cfg,policy,found['destination'],anchor)
        if persist and not path.exists():
            publisher._private_state_directory(directory);atomic_json(path,found,True)
    return found


def _register(cfg,policy,private,receipt):
    # Receipt creation_attempted/sha is durable before O_EXCL. Even a crash
    # after creation can be reconciled by this request or a new request ID.
    value=_object(cfg,policy,private,receipt,persist=True)
    if value is None:fail('DRIVE_REVIEW_STATE','The created local review has no attributable import attempt')
    return value


def _download(drive,remote_id,meta,size):
    chunks=[];offset=0
    while offset<size:
        end=min(offset+publisher.CHUNK_BYTES,size)-1
        code,headers,raw=drive.exchange('GET',publisher.API_ROOT+'drive/v3/files/'+urllib.parse.quote(remote_id,safe='')+'?alt=media',
            headers={'Range':f'bytes={offset}-{end}'},allowed=(200,206))
        if len(raw)!=end-offset+1 or code==200 and not (offset==0 and end+1==size):fail('DRIVE_REVIEW_RANGE','Review download response differs from the requested bounded byte range')
        returned_range=next((value for key,value in headers.items() if key.casefold()=='content-range'),None)
        if code==206 and returned_range!=f'bytes {offset}-{end}/{size}':fail('DRIVE_REVIEW_RANGE','Review Content-Range does not match the requested file slice')
        chunks.append(raw);offset+=len(raw)
    raw=b''.join(chunks);digest=sha(raw)
    if hashlib.md5(raw).hexdigest()!=meta['md5Checksum'].lower() or meta.get('sha256Checksum',digest)!=digest:
        fail('DRIVE_READBACK_MISMATCH','Downloaded review bytes differ from server checksums')
    return raw,digest


def _confirm(cfg,policy,drive,remote_id,before):
    after,_=sync.remote_review(cfg,policy,drive,remote_id)
    if any(after.get(key)!=before.get(key) for key in ['id','name','parents','size','md5Checksum','sha256Checksum','_review_source']):
        fail('REVISION_CONFLICT','Remote review changed identity or project ancestry during download')


def _conflict(private,anchor,message):
    directory=private/'review-conflicts';publisher._private_state_directory(directory)
    atomic_json(directory/(sha(encoded([anchor,time.time_ns()]))+'.json'),
                {**anchor,'status':'conflict','error':{'code':'IDEMPOTENCY_CONFLICT','message':message}},True)
    fail('IDEMPOTENCY_CONFLICT',message)


def import_review(cfg,args,policy):
    requested=args.get('destination');target=_destination(cfg,policy,requested)
    remote_id=publisher._drive_id(args.get('remote_file_id'));drive=sync.client(cfg,policy)
    meta,size=sync.remote_review(cfg,policy,drive,remote_id)
    if PurePosixPath(requested).suffix.lower()!=PurePosixPath(meta['name']).suffix.lower():fail('DRIVE_REVIEW_UNSUPPORTED','Review destination must preserve the original extension')
    source=meta['_review_source'];md5=meta['md5Checksum'].lower();server_sha=meta.get('sha256Checksum')
    if server_sha is not None and (not isinstance(server_sha,str) or not re.fullmatch('[0-9a-f]{64}',server_sha)):
        fail('DRIVE_REVIEW_UNSUPPORTED','Remote SHA256 metadata is malformed')
    private=no_links(Path(cfg['state_root'])/'drive-source-sync')
    identity={'project_id':cfg['project_id'],'remote_file_id':remote_id,'bytes':size,'md5':md5,
              **({'sha256':server_sha} if server_sha is not None else {})}
    preview={**identity,**source,'destination':requested,'dry_run':True,'would_create':True}
    if args.get('dry_run',True) is True:
        known=_object(cfg,policy,private,identity) if server_sha is not None else None
        if known:
            if target.exists() and requested!=known['destination']:fail('REVISION_CONFLICT','Requested review destination already exists independently of the imported object')
            return {**preview,'destination':known['destination'],'requested_destination':requested,'reused':True,'would_create':False}
        if target.exists():fail('REVISION_CONFLICT','Review destination already exists')
        return preview
    if policy['enabled'] is not True or args.get('authorized') is not True:fail('AUTHORIZATION_REQUIRED','Review import apply requires enabled policy and authorized=true')
    request_id=args.get('request_id')
    if not isinstance(request_id,str) or not re.fullmatch('[A-Za-z0-9_-]{1,64}',request_id):fail('INVALID_ARGUMENT','Review import requires a stable request_id')
    with locked(cfg,'drive-source-sync'):
        private=sync.state_root(cfg);imports=private/'imports';publisher._private_state_directory(imports)
        record_path=imports/(sha(request_id.encode())+'.json')
        if (private/'requests'/record_path.name).exists():fail('IDEMPOTENCY_CONFLICT','Request ID already belongs to source synchronization')
        anchor={**{key:identity[key] for key in ['project_id','remote_file_id','bytes','md5']},'request_id':request_id}
        if record_path.exists():
            receipt=sync.load_json(record_path)
            if (any(receipt.get(key)!=value for key,value in anchor.items())
                or receipt.get('requested_destination',receipt.get('destination'))!=requested
                or server_sha is not None and receipt.get('sha256',server_sha)!=server_sha):
                _conflict(private,{**anchor,'requested_destination':requested,'remote_sha256':server_sha},
                          'Review request ID belongs to another file/destination/content identity')
        else:
            receipt={'schema':'paper-editor.review-import.v1',**anchor,**source,
                     'requested_destination':requested,'destination':requested,
                     'status':'downloading','import_complete':False,'reused':False}
            sync.save_operation_receipt(cfg,record_path,receipt,'review_import',True)
        completed_before=receipt.get('import_complete') is True
        try:
            # Metadata with server SHA can reuse a verified prior copy without
            # downloading another copy. MD5 alone never substitutes for SHA.
            raw=None;digest=server_sha
            if digest is None:
                raw,digest=_download(drive,remote_id,meta,size)
                _confirm(cfg,policy,drive,remote_id,meta)
            identity['sha256']=digest
            known=_object(cfg,policy,private,identity,persist=True)
            if completed_before:
                if receipt.get('sha256')!=digest:fail('REVISION_CONFLICT','Remote review bytes differ from the completed import')
                _verify_local(cfg,policy,receipt['destination'],identity)
                return sync.public_sync(receipt)
            if known:
                if target.exists() and requested!=known['destination']:fail('REVISION_CONFLICT','Requested review destination already exists independently of the imported object')
                receipt.update(status='completed',import_complete=True,sha256=digest,reused=True,
                               destination=known['destination'],origin_request_id=known['origin_request_id'],
                               source_identity={'remote_file_id':remote_id,**source,'sha256':digest,'md5':md5,'bytes':size})
                receipt.pop('error',None);sync.save_operation_receipt(cfg,record_path,receipt,'review_import')
                return sync.public_sync(receipt)
            if target.exists():fail('REVISION_CONFLICT','Review destination already exists; overwrite is prohibited')
            if raw is None:raw,digest=_download(drive,remote_id,meta,size)
            _confirm(cfg,policy,drive,remote_id,meta)
            receipt.update(status='creating',creation_attempted=True,sha256=digest,
                           source_identity={'remote_file_id':remote_id,**source,'sha256':digest,'md5':md5,'bytes':size})
            sync.save_operation_receipt(cfg,record_path,receipt,'review_import')
            safe_write_new(cfg['project_root'],requested,raw)
            receipt.pop('error',None);receipt.update(status='completed',import_complete=True)
            sync.save_operation_receipt(cfg,record_path,receipt,'review_import');_register(cfg,policy,private,receipt)
            return sync.public_sync(receipt)
        except Exception as error:
            detail=publisher.public_receipt(error.as_dict()) if isinstance(error,StudioError) else {'code':'DRIVE_REVIEW_FAILED','message':'Review intake failed; no source overwrite is permitted'}
            if completed_before:
                directory=private/'verification-failures';publisher._private_state_directory(directory)
                atomic_json(directory/(sha(encoded([request_id,time.time_ns()]))+'.json'),{**anchor,'error':detail,'status':'review_verification_failed'},True)
                raise
            receipt.setdefault('error_history',[]).append({'at':time.time(),'error':detail});receipt['error_history']=receipt['error_history'][-128:]
            receipt.update(status='failed',error=detail);sync.save_operation_receipt(cfg,record_path,receipt,'review_import');raise
