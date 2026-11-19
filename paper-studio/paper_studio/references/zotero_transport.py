"""Fixed Windows Pyzotero worker transport. No secrets in RPC or exceptions."""
import json,os,subprocess
from pathlib import Path
from paper_studio.security.policy import fail,no_links
from paper_studio.references.zotero_readonly import TOOLCHAIN,windows_path

def call(cfg,operation,payload,*,budget_seconds=45):
    backend=cfg.get('zotero_backend')
    if not backend or backend.get('backend')!='pyzotero_local':fail('ZOTERO_BACKEND_NOT_CONFIGURED','No selected local SDK backend')
    if operation not in {'status','find_items','find_collections','get_item','get_collection','create_item','create_collection'}:
        fail('ZOTERO_OPERATION_DENIED','Operation is not exposed by the selected adapter')
    binding={k:backend.get(k) for k in ['server_id','sdk_path','credential_path']}
    for key in ['sdk_path','credential_path']:
        if binding[key]:binding[key]=windows_path(no_links(Path(binding[key])))
    worker=no_links(Path(__file__).with_name('zotero_sdk_worker.py'))
    env={k:os.environ[k] for k in ['HOME','WSL_INTEROP','WSL_DISTRO_NAME','WSLENV','SYSTEMROOT','SystemRoot'] if k in os.environ}
    env.update(PATH=str(Path.home() / '.local/bin') + ':/usr/local/bin:/usr/bin:/bin',LANG='C.UTF-8',PYTHONIOENCODING='utf-8',PYTHONDONTWRITEBYTECODE='1')
    try:
        result=subprocess.run([TOOLCHAIN,'run','python','--host','windows','--',windows_path(worker)],
            input=json.dumps({'binding':binding,'operation':operation,'payload':payload,'budget_seconds':min(35,budget_seconds)}),capture_output=True,text=True,
            encoding='utf-8',timeout=min(45,budget_seconds),shell=False,env=env)
    except subprocess.TimeoutExpired:fail('ZOTERO_RESPONSE_UNKNOWN','Worker deadline exceeded; reconcile before retrying any write')
    except OSError:fail('ZOTERO_WINDOWS_BRIDGE','Could not start the scoped SDK worker')
    if result.returncode or len(result.stdout.encode())>2*1024*1024:fail('ZOTERO_WORKER_PROTOCOL','SDK worker failed or returned excessive output')
    try:reply=json.loads(result.stdout)
    except ValueError:fail('ZOTERO_WORKER_PROTOCOL','SDK worker did not return a valid envelope')
    if not isinstance(reply,dict) or type(reply.get('ok')) is not bool:fail('ZOTERO_WORKER_PROTOCOL','Invalid SDK envelope')
    if not reply['ok']:
        error=reply.get('error',{});details={}
        if type(error.get('http_status')) is int:details['http_status']=error['http_status']
        fail(error.get('code','ZOTERO_BACKEND_ERROR'),error.get('message','SDK operation failed'),**details)
    return reply['result']
