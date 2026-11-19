"""Stable paper_* MCP facade over native WebCodex v0.4.6.

Derived from prior project-scoped role routing and artifact transport. Native FS,
project registrations and plugin bindings remain the authoritative backends.
"""
from __future__ import annotations
import argparse,base64,json,sys,hashlib,time
import re
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
sys.path.insert(0,str(Path(__file__).resolve().parents[3]))
from management.native_client import NativeClient,NativeError
from paper_studio import __version__
from paper_studio.capabilities import require_admitted, require_edit_kind, describe_capabilities, guard_release_route
from paper_studio.security.policy import *
from paper_studio.contracts import TOOLS,BY_NAME,validate
from paper_studio.gateway.protocol import LEGACY_VERSIONS,SUPPORTED_VERSIONS,SERVER_INFO,ProtocolError,serve
from paper_studio.gateway import delivery_policy

class Gateway:
    def __init__(self,config_path):self.config_path=config_path;self.resources={};self.generation=None
    def cfg(self):
        cfg=load_config(self.config_path)
        if self.generation!=cfg['_config_revision']:
            self.resources.clear();self.generation=cfg['_config_revision']
        return cfg
    def client(self,cfg,role,max_bytes=3*1024*1024):
        return NativeClient(cfg['server_url'],cfg['credentials'][role],max_bytes=max_bytes)
    @staticmethod
    def unwrap(result):
        value=result.get('structuredContent')
        if result.get('isError'):
            if isinstance(value,dict) and isinstance(value.get('error'),dict):
                e=value['error'];fail(e.get('code','NATIVE_REJECTED'),e.get('message','Native tool rejected'),**e.get('details',{}))
            fail('NATIVE_REJECTED','Native tool rejected',reason=result.get('content',[])[:1])
        if isinstance(value,dict) and 'success' in value:
            if not value['success']:fail('NATIVE_REJECTED','Native operation failed',reason=value.get('error'))
            value=value.get('output')
        if value is None:fail('NATIVE_RESPONSE','Missing structured native result')
        if isinstance(value,dict) and set(value)=={'paper_studio_payload_json'}:
            value=json.loads(value['paper_studio_payload_json'])
        return value
    def plugin(self,cfg,name,args):
        client=self.client(cfg,'builder')
        stage = 'describe'
        started = time.monotonic()
        try:
            descriptor=self.unwrap(client.call('plugin_tool',{'action':'describe','runner':cfg['client_id'],'plugin':cfg.get('provider_id','research-build'),'tool':name}))
            binding=descriptor.get('binding')
            if not isinstance(binding,str) or not binding.startswith('wc_pbind_'):fail('PLUGIN_BINDING','Native plugin did not return a valid binding')
            stage = 'call'
            result=client.call('plugin_tool',{'action':'call','binding':binding,'arguments':args})
            value=self.unwrap(result)
            if isinstance(value,dict) and 'structuredContent' in value:value=self.unwrap(value)
            return value
        except (StudioError, NativeError) as error:
            error.details.update(paper_tool=name, provider_stage=stage,
                                 gateway_elapsed_seconds=round(time.monotonic() - started, 4))
            raise

    def provider_status(self, cfg):
        """Keep the Runner observation available if exact provider discovery fails."""
        client = self.client(cfg, 'builder')
        runner = cfg['client_id']
        provider = cfg.get('provider_id', 'research-build')
        try:
            return self.unwrap(client.call('plugin_tool', {'action': 'list', 'runner': runner, 'plugin': provider}))
        except (StudioError, NativeError) as error:
            result = {'runner': runner, 'plugin': provider, 'status': 'unknown',
                      'errorCode': error.code, 'discovery_error': error.code}
            if isinstance(error, NativeError):
                result['discovery_diagnostics'] = error.details
            if error.code not in ('plugin_replaced', 'plugin_timeout'):
                return result
        # v0.4.6 can reject a named lookup after retiring a timed-out provider.
        # Its runner-scoped catalog still reports the cause. Expose only the
        # requested provider, and never use a summary to declare it ready.
        try:
            catalog = self.unwrap(client.call('plugin_tool', {'action': 'list', 'runner': runner}))
        except (StudioError, NativeError) as error:
            result['catalog_error'] = error.code
            return result
        if not isinstance(catalog, dict) or not isinstance(catalog.get('plugins'), list):
            result['catalog_error'] = 'NATIVE_RESPONSE'
            return result
        matches = [row for row in catalog['plugins'] if isinstance(row, dict) and row.get('plugin') == provider]
        if len(matches) == 1 and matches[0].get('status') == 'failed':
            result.update(status='failed', errorCode=matches[0].get('errorCode') or result['errorCode'])
        return result
    def call(self,name,args):
        initial=self.cfg()
        # Same lock and order as manager switch: hold through native completion,
        # including reads, so fixed native aliases cannot change underneath us.
        with locked(initial,'routing'):
            cfg=self.cfg()
            if (Path(cfg['state_root'])/'routing-unavailable.json').exists():
                fail('BINDING_UNAVAILABLE','Project binding recovery is incomplete; local management action required')
            return delivery_policy.filter_response(cfg,self._call_locked(cfg,name,args))
    def _call_locked(self,cfg,name,args):
        if name not in BY_NAME:fail('TOOL_DENIED','Tool is not in the Paper Studio whitelist')
        validate(args,BY_NAME[name]['inputSchema'])
        delivery_policy.guard_call(cfg,name,args)
        if name=='paper_runtime_status':
            native=self.unwrap(self.client(cfg,'reader').call('runtime_status',{'client_id':cfg['client_id'],'compact':False}))
            available=self.provider_status(cfg)
            from paper_studio.drive.sync_tasks import active_tasks
            summaries=[]
            for selected in [cfg,*cfg.get('_projects',[])]:
                try:summaries.append({'project_id':selected['project_id'],'tasks':active_tasks(selected)})
                except StudioError as error:summaries.append({'project_id':selected['project_id'],'status':'unknown','error':{'code':error.code,'message':'Local source sync task state requires maintenance'}})
            return {'software_id':'webcodex-paper-studio','extension_version':cfg['extension_version'],'gateway_version':__version__,'instance_id':cfg['instance_id'],'client_id':cfg['client_id'],'chat_file_delivery':cfg.get('chat_file_delivery','native'),'authorized_project':project_description(cfg),'authorized_projects':[project_description(c) for c in [cfg,*cfg.get('_projects',[])]],'native':native,'provider':available,'source_sync_tasks':summaries,'config_revision':cfg['_config_revision'],'web_acceptance':'not_verified_by_local_status'}
        if name=='paper_pdf_preview':
            from paper_studio.gateway.preview import describe_current
            return describe_current(cfg)
        cfg=select_project(cfg,args['project_id'])
        require_admitted(cfg,name)
        guard_release_route(cfg,name,args)
        if 'expected_config_revision' in args:
            args = dict(args)
            if args.pop('expected_config_revision') != cfg['_config_revision']:
                fail('CONFIG_CHANGED','The selected project binding changed; refresh project context')
        if name=='paper_capabilities':return describe_capabilities(cfg)
        if name=='paper_publish_to_drive' and 'review_pdf' in args:
            from management.paper_review_display import show_review_build
            return show_review_build(cfg,args['review_pdf'])
        if name=='paper_project_context':
            if cfg.get('target_index', {}).get('enabled') and 'view' not in args:
                from paper_studio.gateway.target_lookup import entrypoints
                return entrypoints(cfg)
            if not cfg.get('target_index', {}).get('enabled'):
                args = {k:v for k,v in args.items() if k!='view'}
        if name in {'paper_index_get', 'paper_index_revise'}:
            from paper_studio.gateway import target_lookup
            return (target_lookup.lookup if name=='paper_index_get' else target_lookup.revise)(cfg,args)
        if name=='paper_material_get':
            from paper_studio.gateway.target_lookup import material
            return material(cfg,args)
        if name=='paper_review_proposal':
            from paper_studio.versions.review_proposals import dispatch
            if args['action'] in {'propose','decide','apply'}:
                require_edit_kind(cfg,'create')
                if not cfg.get('writable_roots'):
                    fail('WRITE_DENIED','The selected project has no writable review scope')
            result = dispatch(cfg,{k:v for k,v in args.items() if k!='project_id'})
            if args['action']=='render' and not delivery_policy.restricted(cfg):
                from paper_studio.gateway.review_resource import issue
                return issue(cfg,result,self.resources)
            return result
        if cfg.get('legacy_provider_only') and name in {'paper_project_context','paper_project_inventory'}:
            from paper_studio.evidence.index import context,inventory
            return context(cfg) if name=='paper_project_context' else inventory(cfg,**{k:v for k,v in args.items() if k!='project_id'})
        if name=='paper_build_plan':
            from paper_studio.execution import build_plan
            return build_plan(cfg,args['tex_path'])
        if name=='paper_pdf_review' and cfg.get('admission_profile')=='paper-editor.v4':
            # Existing Chat clients already send pdf_path, even when their
            # cached schema omits pages. Accept the standard PDF page fragment.
            path, separator, fragment = args['pdf_path'].rpartition('#page=')
            if separator and path.lower().endswith('.pdf'):
                if (not re.fullmatch(r'[1-9][0-9]{0,4}',fragment)
                        or 'pages' in args or 'page_ranges' in args):
                    fail('INVALID_ARGUMENT','Use one PDF #page=N fragment or one explicit page selection')
                args = {**args, 'pdf_path':path, 'pages':[int(fragment)]}
        if (Path(cfg['state_root'])/'routing-unavailable.json').exists():
            fail('BINDING_UNAVAILABLE','Project binding recovery is incomplete; local management action required')
        if name=='paper_section':
            from paper_studio.chapters.workflow import dispatch
            if args['action']=='stage':
                require_edit_kind(cfg,'create')
            return dispatch(cfg,args)
        if name=='paper_section_delivery':
            from paper_studio.gateway.download import describe_delivery
            require_admitted(cfg,'paper_build_artifact')
            return describe_delivery(cfg,args)
        if name=='paper_review_comments':
            from paper_studio.drive.review_comments import dispatch
            if args['action']=='collect' and args.get('dry_run',True) is False:
                require_edit_kind(cfg,'create')
            if args['action']=='apply':
                require_edit_kind(cfg,'edit')
                def edit_review_sources(changes):
                    for change in changes:
                        path_for(cfg,change['path'],True)
                        if change.get('kind')!='edit' or not change.get('expected_read_revision'):
                            fail('REVISION_REQUIRED','Confirmed source edits require native read revisions')
                    return self.unwrap(self.client(cfg,'writer').call('edit_project_files',{
                        'project':cfg['native_project'],'changes':changes,'dry_run':False}))
                with locked(cfg,'lifecycle') if cfg.get('version_management') else contextlib.nullcontext():
                    with locked(cfg):
                        return dispatch(cfg,args,edit_sources=edit_review_sources)
            return dispatch(cfg,args)
        if name=='paper_zotero_read':
            from paper_studio.references.zotero_readonly import dispatch
            return dispatch(cfg,args)
        if name=='paper_zotero_add_reference':
            if cfg.get('zotero_backend'):
                from paper_studio.references.zotero_local import dispatch
                return dispatch(cfg,name,args)
            from paper_studio.references.zotero_import import dispatch
            return dispatch(cfg,args)
        if name=='paper_zotero_manage':
            from paper_studio.references.zotero_local import dispatch
            return dispatch(cfg,name,args)
        if name in {'paper_literature_search','paper_reference_resolve','paper_citation_graph','paper_open_access_lookup'}:
            from paper_studio.references.literature import dispatch
            return dispatch(cfg,name,args)
        if name=='paper_evidence_read':
            from paper_studio.pdf.evidence import dispatch
            return dispatch(cfg,args)
        if name=='paper_research_state':
            from paper_studio.research_state import dispatch
            return dispatch(cfg,args)
        if name=='paper_source_sync':
            from paper_studio.drive.sync_tasks import dispatch
            return dispatch(cfg,args)
        if name in {'paper_zotero_sync_bibtex','paper_zotero_verify_citations'}:
            from paper_studio.references.zotero_bibtex import dispatch
            def edit_bib(changes):
                # This callback is private to the planner. Native owns the
                # compare-and-swap revision and atomic transaction.
                for change in changes:
                    if change.get('kind')!='edit' or not change.get('expected_read_revision'):
                        fail('REVISION_REQUIRED','Bibliography edits require an existing-file revision')
                    path_for(cfg,change.get('path'),True)
                return self.unwrap(self.client(cfg,'writer').call('edit_project_files',{
                    'project':cfg['native_project'],'changes':changes,'dry_run':False}))
            with locked(cfg):
                return dispatch(cfg,name,args,edit_bib)
        if name=='paper_manuscript_build':
            from paper_studio.versions.build_records import dispatch
            return dispatch(cfg,args,lambda request:self.plugin(cfg,'paper_tex_build_submit',request))
        if name=='paper_version_list' and cfg.get('manuscript_roots'):
            from paper_studio.versions.build_records import dispatch
            value=self.plugin(cfg,name,args)
            return {**value,'manuscript_builds':[dispatch(cfg,{'action':'list','workdir':root}) for root in cfg['manuscript_roots']]}
        if name=='paper_tex_build_submit':
            roots=[root for root in cfg.get('manuscript_roots',[]) if PurePosixPath(args['tex_path']).is_relative_to(root)]
            if roots:
                if args.get('resume_build_id') is not None:
                    fail('RECOVERY_UNSUPPORTED','Managed bNNN manuscripts retain their existing build-record workflow; direct TeX recovery is not enabled')
                # Full source capture is required for a restorable paper record.
                if 'input_paths' in args:fail('ARCHIVE_INPUT_SCOPE','Managed manuscript builds capture the full project; omit input_paths')
                from paper_studio.versions.build_records import dispatch
                return dispatch(cfg,{**args,'action':'submit','workdir':roots[0]},lambda request:self.plugin(cfg,'paper_tex_build_submit',request))
        if name=='paper_read_files':
            from paper_studio.gateway import history_lookup
            if any(item['path'].startswith(history_lookup.PREFIX) for item in args['items']):
                return history_lookup.read_locator(cfg,args)
            from paper_studio.gateway.target_lookup import HANDOFF_PREFIX, PREFIX, read_locator, read_material_locator
            from paper_studio.gateway import review_resource
            if any(item['path'].startswith(review_resource.PREFIX) for item in args['items']):
                return review_resource.read_locator(cfg,args)
            if any(item['path'].startswith(HANDOFF_PREFIX) for item in args['items']):
                return read_material_locator(cfg, args)
            if any(item['path'].startswith(PREFIX) for item in args['items']):
                return read_locator(cfg, args)
            before={};total=0
            for item in args['items']:
                path_for(cfg,item['path']);raw=safe_read(cfg['project_root'],item['path'],32*1024*1024);total+=len(raw)
                if total>32*1024*1024:fail('INPUT_LIMIT','Read identity batch exceeds 32 MiB; read fewer files')
                before[item['path']]=raw
            value=self.unwrap(self.client(cfg,'reader').call('read_files',{'project':cfg['native_project'],'items':args['items'],'with_line_numbers':False,'max_result_bytes':args.get('max_result_bytes',262144)}))
            identities=[]
            for item in args['items']:
                rel=item['path'];raw=before[rel]
                if safe_read(cfg['project_root'],rel,32*1024*1024)!=raw:fail('REVISION_CONFLICT','File changed across native read; retry before using a build hash',path=rel)
                returned=next((r.get('output',{}) for r in value.get('items',[]) if r.get('path')==rel),{})
                if 'text' in returned:
                    lines=raw.decode('utf-8').splitlines();start=item.get('start_line',1)-1
                    actual=returned['text'].splitlines()
                    if actual!=lines[start:start+len(actual)]:fail('REVISION_CONFLICT','Native read content and local byte identity do not match',path=rel)
                identities.append({'path':rel,'sha256':sha(raw)})
            return {'project_id':cfg['project_id'],'native_result':value,'file_identities':identities,'guidance':'Use the native read_revision for edits; SHA256 for build expected_revision.'}
        if name=='paper_edit_files':
            for change in args['changes']:
                if not isinstance(change,dict):fail('INVALID_ARGUMENT','Each change must be an object')
                kind=change.get('kind')
                require_edit_kind(cfg,kind)
                if kind not in ['create','edit','rename','delete']:fail('WRITE_DENIED','Unsupported native edit kind')
                path_for(cfg,change.get('path'),True)
                if kind in ['rename','delete'] and args.get('authorized') is not True:fail('AUTHORIZATION_REQUIRED','Set authorized=true only for an explicitly requested candidate rename/delete')
                if kind=='rename':path_for(cfg,change.get('to_path'),True)
                if kind!='create' and not change.get('expected_read_revision'):fail('REVISION_REQUIRED','Existing-file edits require native expected_read_revision')
            with locked(cfg,'lifecycle') if cfg.get('version_management') else contextlib.nullcontext():
                with locked(cfg):
                    # Recheck dynamic freeze state while promotion is excluded.
                    for change in args['changes']:
                        path_for(cfg,change.get('path'),True)
                        if change.get('kind')=='rename':path_for(cfg,change.get('to_path'),True)
                    return self.unwrap(self.client(cfg,'writer').call('edit_project_files',{'project':cfg['native_project'],'changes':args['changes'],'dry_run':args.get('dry_run',False)}))
        if name=='paper_build_artifact' and args.get('action') in ['export','image']:
            meta=self.plugin(cfg,name,{**args,'action':'metadata'})
            if args['action']=='image':
                if meta['mime_type'] not in ['image/png','image/jpeg'] or meta['bytes']>1024*1024:
                    fail('IMAGE_LIMIT','MCP image accepts a published PNG/JPEG of at most1MiB; create a smaller preview')
                raw=self.client(cfg,'reader').call('project_artifact',{'project':meta['native_project'],'path':meta['native_path'],'action':'image'})
                identity=self.unwrap(raw)
                images=[x for x in raw.get('content',[]) if x.get('type')=='image']
                if len(images)!=1 or identity.get('sha256')!=meta['sha256'] or identity.get('file_bytes')!=meta['bytes']:
                    fail('ARTIFACT_CHANGED','Native image identity differs from verified build artifact')
                try:data=base64.b64decode(images[0]['data'],validate=True)
                except (ValueError,TypeError,KeyError):fail('RESOURCE_INTEGRITY','Native image returned invalid bytes')
                if len(data)!=meta['bytes'] or sha(data)!=meta['sha256']:
                    fail('RESOURCE_INTEGRITY','Native image bytes differ from issued artifact identity')
                raw['structuredContent']['paper_studio_delivery']={**meta,'mcp_image_returned':True,'chatgpt_visual_review':'not_established_by_transport'}
                return {'_mcp_result':raw}
            if meta['bytes']>1024*1024:
                return {**meta,'delivery':'chunked','part_bytes':262144,'part_count':(meta['bytes']+262143)//262144,'instructions':'Call paper_build_artifact action=chunk for offsets 0,262144,...; reassemble bytes, verify complete bytes/SHA256. A manifest is not delivery.'}
            # Native v0.4.6 accepts expected_sha256 only for inspect. Export
            # captures an immutable native resource; fence its returned identity.
            raw=self.client(cfg,'reader').call('project_artifact',{'project':meta['native_project'],'path':meta['native_path'],'action':'export'})
            identity=self.unwrap(raw)
            if identity.get('sha256')!=meta['sha256'] or identity.get('bytes')!=meta['bytes']:
                fail('ARTIFACT_CHANGED','Native export identity differs from verified build artifact')
            for item in raw.get('content',[]):
                if item.get('type')=='resource_link':self.resources[item['uri']]={'generation':self.generation,'sha256':meta['sha256'],'bytes':meta['bytes']}
            raw.setdefault('structuredContent',{})['paper_studio_delivery']={**meta,'resource_link_returned':any(x.get('type')=='resource_link' for x in raw.get('content',[])),'chatgpt_bytes_received':False,'user_opened':False}
            return {'_mcp_result':raw}
        result = self.plugin(cfg, name, args)
        if name == 'paper_project_context' and isinstance(result, dict) and 'continuation' not in result:
            # The Native provider can still be on the previous release.
            from paper_studio.research_state import continuation_summary
            result = {**result, 'continuation': continuation_summary(cfg)}
        return result
    def filter_response(self,value):
        return delivery_policy.filter_response(self.cfg(),value)
    def handle(self,message):
        cfg=self.cfg()
        delivery_policy.guard_request(cfg,message)
        return delivery_policy.filter_response(cfg,self._handle(message))
    def _handle(self,message):
        if not isinstance(message,dict):fail('INVALID_REQUEST','JSON-RPC object required')
        method=message.get('method');params=message.get('params',{})
        if not isinstance(params,dict):fail('INVALID_REQUEST','Object params required')
        if method=='initialize':
            self.cfg()
            version=params.get('protocolVersion')
            return {'protocolVersion':version if version in LEGACY_VERSIONS else LEGACY_VERSIONS[0],'capabilities':{'tools':{'listChanged':False},'resources':{}},'serverInfo':SERVER_INFO,'instructions':'Use paper_runtime_status authorized_projects to discover registered project_id values; specify project_id on every project request. Read exact local files, cite hashes, keep fixtures separate from research facts. Candidate edits require native read_revision. Builds are asynchronous. ResourceLink, complete bytes received, and visual review are separate states. Never infer production or other-project access.'}
        if method=='server/discover':
            self.cfg()
            return {'supportedVersions':list(SUPPORTED_VERSIONS),'capabilities':{'tools':{},'resources':{}}}
        if method in ['ping','notifications/initialized']:return {}
        if method=='tools/list':
            from paper_studio.gateway.preview import present_tool
            from paper_studio.capabilities import INDEX_TOOLS, visible_index_tools
            cfg=self.cfg()
            active = set().union(*(visible_index_tools(p) for p in [cfg, *cfg.get('_projects', [])]))
            return {'tools':[present_tool(tool,cfg) for tool in TOOLS if tool['name'] not in INDEX_TOOLS or tool['name'] in active]}
        if method=='resources/list':
            from paper_studio.gateway.download import ui_resources
            from paper_studio.gateway.preview import ui_resources as preview_resources
            cfg=self.cfg();return {'resources':ui_resources(cfg)+preview_resources(cfg)}
        if method=='resources/templates/list':self.cfg();return {'resourceTemplates':[]}
        if method=='resources/read':
            cfg=self.cfg()
            with locked(cfg,'routing'):
                cfg=self.cfg();uri=params.get('uri')
                if (Path(cfg['state_root'])/'routing-unavailable.json').exists():fail('BINDING_UNAVAILABLE','Project binding recovery is incomplete')
                from paper_studio.gateway.download import is_ui_uri, read_ui
                if is_ui_uri(uri):
                    return read_ui(cfg, uri)
                from paper_studio.gateway.preview import UI_URI as PREVIEW_URI, read_ui as preview_ui
                if uri==PREVIEW_URI:
                    return preview_ui(cfg)
                if uri not in self.resources:fail('RESOURCE_DENIED','Only an artifact URI issued by this connection/current binding may be read')
                expected=self.resources[uri]
                if expected.get('kind')=='review':
                    from paper_studio.gateway.review_resource import read
                    return read(cfg, expected)
                result=self.client(cfg,'reader').request('resources/read',{'uri':uri})
                try:
                    data=b''.join(base64.b64decode(x['blob'],validate=True) for x in result.get('contents',[]) if 'blob' in x)
                except (ValueError,TypeError):fail('RESOURCE_INTEGRITY','Native resource did not return valid complete binary bytes')
                if len(data)!=expected['bytes'] or sha(data)!=expected['sha256']:
                    fail('RESOURCE_INTEGRITY','Native resource bytes do not match issued artifact identity')
                return result
        if method=='tools/call':
            if params.get('name') not in BY_NAME:raise ProtocolError(-32602,'Unknown tool')
            value=self.call(params.get('name'),params.get('arguments',{}))
            if isinstance(value,dict) and '_mcp_result' in value:return value['_mcp_result']
            return {'content':[{'type':'text','text':json.dumps(value,ensure_ascii=False,separators=(',',':')) if len(encoded(value))<120000 else 'Complete bounded result in structuredContent.'}],'structuredContent':value,'isError':False}
        raise ProtocolError(-32601,'Unsupported MCP method')

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);args=parser.parse_args();gateway=Gateway(args.config)
    serve(gateway,sys.stdin.buffer,sys.stdout)
if __name__=='__main__':main()
