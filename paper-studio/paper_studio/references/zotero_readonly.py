"""Gateway-host-only transport to the existing Windows Zotero helper."""
from __future__ import annotations
import hashlib,json,os,subprocess
from pathlib import Path
from paper_studio.security.policy import fail,no_links,safe_read
from paper_studio.references.zotero_worker import valid_scope,ReadError,ACTIONS

TOOLCHAIN=str(Path.home() / '.local/bin/codex-toolchain')

def windows_path(path):
    value=str(path)
    if value.startswith('/mnt/') and len(value)>7 and value[5].isalpha() and value[6]=='/':return value[5].upper()+':\\'+value[7:].replace('/','\\')
    result=subprocess.run(['/usr/bin/wslpath','-w',value],capture_output=True,text=True,encoding='utf-8',timeout=5,shell=False)
    converted=result.stdout.strip()
    if result.returncode or not converted.startswith(('\\\\wsl.localhost\\','\\\\wsl$\\')):fail('ZOTERO_WINDOWS_BRIDGE','Cannot resolve the fixed worker path through WSL interop')
    return converted

def dispatch(cfg,args):
    config=cfg.get('zotero_readonly')
    if config is None:fail('ZOTERO_NOT_CONFIGURED','This project has no explicit readonly Zotero scope')
    try:valid_scope(config)
    except ReadError as error:fail(error.code,error.message)
    if not isinstance(args,dict) or args.get('action') not in ACTIONS:fail('ZOTERO_ACTION_DENIED','Unsupported readonly Zotero action')
    key=args.get('citation_key')
    if key is not None and key not in config['items']:fail('ZOTERO_SCOPE_DENIED','Citation key is outside this project authorization')
    if args['action'] not in {'status','search'} and key is None:fail('ZOTERO_SCOPE_DENIED','This action requires a project-authorized citation_key')
    if not isinstance(args.get('query',''),str) or len(args.get('query',''))>256:fail('ZOTERO_INVALID_ARGUMENT','Bounded local query text required')
    helper=no_links(Path(config['helper_path']))
    raw=safe_read(helper.parent,helper.name,256*1024)
    if hashlib.sha256(raw).hexdigest()!=config['helper_sha256']:fail('ZOTERO_HELPER_CHANGED','Installed helper differs from the configured SHA256')
    worker=no_links(Path(__file__).with_name('zotero_worker.py'))
    payload={'config':{**config,'helper_path':windows_path(helper)},'args':{k:args[k] for k in ['action','citation_key','query'] if k in args}}
    environment={k:os.environ[k] for k in ['HOME','WSL_INTEROP','WSL_DISTRO_NAME','WSLENV','SYSTEMROOT','SystemRoot'] if k in os.environ}
    environment.update(PATH=str(Path.home() / '.local/bin') + ':/usr/local/bin:/usr/bin:/bin',LANG='C.UTF-8',PYTHONIOENCODING='utf-8',PYTHONDONTWRITEBYTECODE='1')
    try:
        result=subprocess.run([TOOLCHAIN,'run','python','--host','windows','--',windows_path(worker)],
            input=json.dumps(payload,ensure_ascii=False),capture_output=True,text=True,encoding='utf-8',timeout=45,shell=False,env=environment)
    except subprocess.TimeoutExpired:fail('ZOTERO_TIMEOUT','Windows readonly Zotero bridge exceeded 45 seconds')
    except OSError:fail('ZOTERO_WINDOWS_BRIDGE','Windows readonly Zotero worker could not be launched')
    if result.returncode:fail('ZOTERO_WINDOWS_BRIDGE','Windows readonly Zotero worker exited unsuccessfully')
    if len(result.stdout.encode())>2*1024*1024:fail('ZOTERO_RESPONSE_LIMIT','Windows readonly result exceeds 2 MiB')
    try:reply=json.loads(result.stdout)
    except (ValueError,UnicodeError):fail('ZOTERO_WORKER_PROTOCOL','Windows readonly worker did not return a JSON result')
    if not isinstance(reply,dict) or not isinstance(reply.get('ok'),bool):fail('ZOTERO_WORKER_PROTOCOL','Invalid Windows readonly worker envelope')
    if reply['ok'] is not True:
        error=reply.get('error',{});fail(error.get('code','ZOTERO_WORKER_FAILED'),error.get('message','Readonly Zotero worker failed'))
    return {'project_id':cfg['project_id'],**reply['result'],'host':'windows','helper_sha256':config['helper_sha256'],'read_only':True}
