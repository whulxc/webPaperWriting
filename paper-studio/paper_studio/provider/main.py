"""The single research-build native plugin, shared by all Paper Studio instances."""
from __future__ import annotations
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from paper_studio import __version__
from paper_studio.capabilities import require_admitted
from paper_studio.security.policy import *
from paper_studio.contracts import TOOLS,BY_NAME,validate,tool,obj,S,O
from paper_studio.provider import jobs
from paper_studio.capabilities import HOST_TOOLS

def native_schema(value):
    # v0.4.6 rejects numeric range keywords at admission. The complete logical
    # schema remains enforced by validate() for every direct or Gateway call.
    if isinstance(value,list):return [native_schema(v) for v in value]
    if isinstance(value,dict):return {k:native_schema(v) for k,v in value.items() if k not in {'minimum','maximum'}}
    return value

# Native Plugin v1 does not host MCP Apps. Keep the public descriptor metadata
# intact for Gateway tools/list, and omit it only from Native registration.
# The gateway and the historical Native runner both expose the read-only planner.
PROVIDER_TOOLS=[native_schema({k:v for k,v in t.items() if k!='_meta'}) for t in TOOLS
                if t['name'] not in HOST_TOOLS or t['name']=='paper_build_plan']

def dispatch(cfg,name,args):
    if name not in {t['name'] for t in PROVIDER_TOOLS}:fail('TOOL_DENIED','Unknown provider tool')
    validate(args,BY_NAME[name]['inputSchema']);cfg=select_project(cfg,args['project_id']);a={k:v for k,v in args.items() if k!='project_id'}
    if a.pop('expected_config_revision', cfg.get('_config_revision')) != cfg.get('_config_revision'):
        fail('CONFIG_CHANGED','Project binding changed before provider call')
    require_admitted(cfg,name)
    from paper_studio.capabilities import guard_release_route
    guard_release_route(cfg,name,a)
    if name=='paper_build_plan':
        from paper_studio.execution import build_plan
        return build_plan(cfg,a['tex_path'])
    if name=='paper_project_inventory':
        from paper_studio.evidence.index import inventory
        return inventory(cfg,**a)
    if name=='paper_project_context':
        if cfg.get('target_index', {}).get('enabled') and 'view' not in a:
            from paper_studio.gateway.target_lookup import entrypoints
            return entrypoints(cfg)
        from paper_studio.evidence.index import context
        return context(cfg)
    if name=='paper_reference_manage':
        from paper_studio.references.bib import inspect_bib
        return inspect_bib(cfg,**a)
    if name=='paper_pptx_edit' and a.get('action')=='import_drive':
        from paper_studio.drive.pptx_import import import_pptx
        return import_pptx(cfg,a)
    kinds={'paper_tex_build_submit':'tex','paper_figure_build':'figure','paper_pptx_edit':'pptx','paper_pdf_review':'pdf'}
    if name in kinds:return jobs.submit(cfg,kinds[name],a)
    if name=='paper_build_status':return jobs.read_status(cfg,**a)
    if name=='paper_build_artifact':
        if a.get('action')=='export':a['action']='metadata'
        return jobs.artifact(cfg,**a)
    if name=='paper_pptx_inspect':
        from paper_studio.pptx.build import inspect_pptx
        p=path_for(cfg,a.pop('pptx_path'));safe_read(cfg['project_root'],p.relative_to(cfg['project_root']).as_posix(),64*1024*1024)
        return inspect_pptx(p,config=cfg,**a)
    if name.startswith('paper_version_'):
        from paper_studio.versions import history
        action=name.removeprefix('paper_version_')
        if action=='list':return history.list_versions(cfg,**a)
        if action=='save':return history.save_version(cfg,a['version'],a['build_ids'],a['changelog'])
        if action=='compare':return history.compare_versions(cfg,**a)
        if action=='derive':
            if a.get('action','derive')!='derive':
                from paper_studio.versions.lifecycle import dispatch as version_dispatch
                return version_dispatch(cfg,a)
            if not a.get('version') or not a.get('destination'):fail('INVALID_ARGUMENT','Legacy derive requires version and destination')
            return history.derive_version(cfg,a['version'],a['destination'],a.get('source_prefix','candidates'))
    if name=='paper_drive_verify':
        from paper_studio.drive.verify import verify
        return verify(cfg,**a)
    if name=='paper_publish_to_drive':
        if 'review_pdf' in a:
            fail('GATEWAY_REQUIRED', 'Display a single review PDF through the project gateway')
        return jobs.submit_drive(cfg,a['version'],a.get('resume_build_id'))
    fail('TOOL_DENIED','Tool not implemented')

def serve(config_path):
    for raw in sys.stdin.buffer:
        message=None
        try:
            if len(raw)>1024*1024:fail('REQUEST_TOO_LARGE','Native plugin request exceeds 1 MiB')
            message=json.loads(raw)
            if not isinstance(message,dict):fail('INVALID_REQUEST','JSON-RPC object required')
            method=message.get('method');params=message.get('params',{})
            if method=='initialize':
                if params.get('protocolVersion')!='webcodex-plugin-v1':fail('PROTOCOL_VERSION','Expected webcodex-plugin-v1')
                result={'protocolVersion':'webcodex-plugin-v1','serverInfo':{'name':'webcodex-paper-studio','version':__version__}}
            elif method=='tools/list':result={'tools':PROVIDER_TOOLS}
            elif method=='tools/call':
                cfg=load_config(config_path);value=dispatch(cfg,params.get('name'),params.get('arguments',{}))
                # Native v0.4.6 also caps JSON depth at16/nodes at4096. Complex
                # native PPTX geometry is preserved in a bounded JSON-string
                # envelope, decoded back to structured data by the Gateway.
                payload=json.dumps(value,ensure_ascii=False,separators=(',',':'))
                result={'content':[{'type':'text','text':'Complete result in paper_studio_payload_json; the Paper Studio Gateway decodes this native transport envelope.'}],'structuredContent':{'paper_studio_payload_json':payload},'isError':False}
            else:fail('METHOD_NOT_FOUND','Unsupported native plugin method')
            if len(encoded(result))>480*1024:fail('RESPONSE_TOO_LARGE','Use smaller pages or artifact chunks')
        except Exception as exc:
            error=exc.as_dict() if isinstance(exc,StudioError) else {'code':'PROVIDER_ERROR','message':str(exc)[:1000]}
            result={'content':[{'type':'text','text':json.dumps(error,ensure_ascii=False)}],'structuredContent':{'error':error},'isError':True}
        if isinstance(message,dict) and 'id' in message:
            sys.stdout.write(json.dumps({'jsonrpc':'2.0','id':message['id'],'result':result},ensure_ascii=False,separators=(',',':'))+'\n');sys.stdout.flush()

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);parser.add_argument('--worker');parser.add_argument('--project-id');args=parser.parse_args()
    if args.worker:
        cfg=load_config(args.config);cfg=select_project(cfg,args.project_id or cfg['project_id']);result=jobs.worker(cfg,args.worker);print(json.dumps({'build_id':args.worker,'status':result['status']}));return
    serve(args.config)
if __name__=='__main__':main()
