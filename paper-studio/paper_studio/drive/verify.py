"""Bounded, metadata-only reads using the configured local Publisher grant.

Readable IDs need not belong to the project. Reading metadata never expands the
project's publication, source-sync or sharing permissions. Connector credentials
and user/account metadata are not queried or returned.
"""
from __future__ import annotations
import re
import time
import urllib.parse

from paper_studio.drive.publisher import Drive
from paper_studio.security.policy import StudioError,fail

ID=re.compile(r'[A-Za-z0-9_-]{1,200}\Z')
SHA=re.compile(r'[a-fA-F0-9]{64}\Z')
FIELDS=('id','name','mimeType','size','md5Checksum','sha256Checksum','parents','webViewLink')


def _error(error):
    code=error.code if isinstance(error,StudioError) else 'DRIVE_VERIFY_FAILED'
    details=error.details if isinstance(error,StudioError) else {}
    status=details.get('http_status')
    kind=({401:'authorization_failed',403:'access_denied',404:'not_found_or_inaccessible'}.get(status)
          or ('authorization_failed' if code.startswith('OAUTH_') else 'failed'))
    if (code=='OAUTH_REFRESH_FAILED' and details.get('provider_error')!='invalid_grant'
        and details.get('reauthorization_required') is not True
        and (details.get('authorization_validity')=='unknown' or details.get('reauthorization_required') is False)):
        kind='authorization_unknown'
    value={'status':kind,'error_code':code}
    if isinstance(status,int) and not isinstance(status,bool):value['http_status']=status
    for key in ['reauthorization_required','retryable']:
        if isinstance(details.get(key),bool):value[key]=details[key]
    if details.get('provider_error') in {'invalid_grant','invalid_client','unauthorized_client','temporarily_unavailable','server_error'}:
        value['provider_error']=details['provider_error']
    if details.get('authorization_validity') in {'unknown','valid','invalid'}:
        value['authorization_validity']=details['authorization_validity']
    return value


def verify(cfg,file_ids=None,checks=None):
    """Read actual Drive metadata, with optional caller-supplied identity checks."""
    supplied=file_ids is not None
    ids=file_ids if supplied else ([cfg['drive_folder_id']] if cfg.get('drive_folder_id') else [])
    if (not isinstance(ids,list) or (supplied and not 1<=len(ids)<=20) or len(ids)>20
        or any(not isinstance(ident,str) or not ID.fullmatch(ident) for ident in ids)
        or len(set(ids))!=len(ids)):
        fail('INVALID_ARGUMENT','file_ids must contain 1..20 unique valid Google Drive IDs')
    checks=[] if checks is None else checks
    if not isinstance(checks,list) or len(checks)>20:fail('INVALID_ARGUMENT','checks must be a bounded list')
    expected={}
    for item in checks:
        if (not isinstance(item,dict) or set(item)-{'file_id','sha256','size','parent'}
            or item.get('file_id') not in ids or item['file_id'] in expected
            or not set(item)-{'file_id'}):
            fail('INVALID_ARGUMENT','Each check must name one requested file ID and at least one expected identity')
        if 'sha256' in item and (not isinstance(item['sha256'],str) or not SHA.fullmatch(item['sha256'])):
            fail('INVALID_ARGUMENT','Expected SHA256 must contain exactly 64 hexadecimal characters')
        if 'size' in item and (isinstance(item['size'],bool) or not isinstance(item['size'],int) or item['size']<0):
            fail('INVALID_ARGUMENT','Expected size must be a nonnegative integer')
        if 'parent' in item and (not isinstance(item['parent'],str) or not ID.fullmatch(item['parent'])):
            fail('INVALID_ARGUMENT','Expected parent must be a valid Google Drive ID')
        expected[item['file_id']]=item
    result={'schema':'paper-studio.drive-verify.v1','project_id':cfg['project_id'],
            'credential_source':'local_publisher','metadata_only':True,
            'grants_write_permission':False,'project_folder_checked':not supplied and bool(ids),
            'files':[]}
    if not cfg.get('drive_oauth_file'):
        result['authorization']={'status':'not_configured','error_code':'OAUTH_REQUIRED'}
        return result
    try:
        # Metadata probes run under a readonly shared credential mount. Refresh
        # an expired access token in memory without rewriting the OAuth grant.
        drive=Drive(cfg['drive_oauth_file'],persist_access_token=False,deadline=time.monotonic()+60)
        # A non-personal About field verifies the real token without requesting
        # account email, permission IDs or other user metadata.
        drive.request('GET','drive/v3/about?fields=kind')
        result['authorization']={'status':'verified'}
    except Exception as error:
        result['authorization']=_error(error)
        return result
    for ident in ids:
        try:
            path='drive/v3/files/'+urllib.parse.quote(ident,safe='')+'?'+urllib.parse.urlencode({'fields':','.join(FIELDS),'supportsAllDrives':'true'})
            raw=drive.request('GET',path)
            if raw.get('id')!=ident:fail('DRIVE_PROTOCOL','Drive returned a different file identity')
            metadata={key:raw[key] for key in FIELDS if key in raw}
            item={'id':ident,'status':'verified','metadata':metadata}
            comparisons={}
            for key,value in expected.get(ident,{}).items():
                if key=='file_id':continue
                actual=metadata.get({'sha256':'sha256Checksum','size':'size','parent':'parents'}[key])
                if actual is None:outcome='unavailable'
                elif key=='parent':outcome='matched' if isinstance(actual,list) and value in actual else 'mismatch'
                elif key=='sha256':outcome='matched' if isinstance(actual,str) and actual.lower()==value.lower() else 'mismatch'
                else:outcome='matched' if str(actual)==str(value) else 'mismatch'
                comparisons[key]={'status':outcome}
            if comparisons:
                item.update(checks=comparisons,identity_verified=all(check['status']=='matched' for check in comparisons.values()))
            result['files'].append(item)
        except Exception as error:result['files'].append({'id':ident,**_error(error)})
    result['all_files_accessible']=all(item['status']=='verified' for item in result['files'])
    return result
