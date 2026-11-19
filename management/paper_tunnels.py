"""只读 Tunnel 清单与显式选择计划；真实换绑/创建保持关闭。

仅复用已安装 tunnel-client 的 list/status/get；绝不 run/connect/poll/create。
Read metadata 成功不能证明 Use 权限；offline 不能证明 unbound。
"""
from __future__ import annotations
import argparse,ast,copy,datetime,hashlib,json,os,re,shlex,shutil,stat,subprocess,tomllib
from pathlib import Path
import yaml
from management.native_client import NativeClient

ROOT=Path(__file__).resolve().parents[1]
binary_path = os.environ.get('PAPER_STUDIO_TUNNEL_CLIENT') or shutil.which('tunnel-client')
local_toolchains = ROOT / 'instances/local/toolchains.json'
if not binary_path and local_toolchains.is_file():
    binary_path = json.loads(local_toolchains.read_text())['tunnel_client']
BINARY = Path(binary_path or 'tunnel-client')
if binary_path and not BINARY.is_absolute():
    raise ValueError('Tunnel client configuration must name an absolute executable path')
TUNNEL_ID=re.compile(r'tunnel_[0-9a-f]{32}\Z')
MAX_TUNNELS=256
PROFILE_DIR=Path.home()/'.config/tunnel-client'
DEFAULT_PRIVATE=Path.home()/'.local/state/webcodex-paper-studio/paper-editor/tunnel-registry.json'
DOC='https://developers.openai.com/api/docs/guides/secure-mcp-tunnels'

class TunnelError(Exception):
    def __init__(self,code,message):self.code,self.message=code,message;super().__init__(message)
def fail(code,message):raise TunnelError(code,message)
def now():return datetime.datetime.now(datetime.timezone.utc).isoformat()
def digest(raw):return hashlib.sha256(raw).hexdigest()
def bounded_file(path,limit=2*1024*1024):
    path=Path(path)
    if not path.is_absolute() or path.resolve()!=path:fail('TUNNEL_PATH_DENIED','Absolute nonsymlink local paths required')
    info=path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size>limit:fail('TUNNEL_INPUT_LIMIT','Invalid or oversized local metadata file')
    return path.read_bytes()
def ref_id(reference):return 'secure-ref:'+digest(reference.encode())[:16]
def _json_error(text):
    if 'EOF' in text:return {'code':'PLATFORM_NETWORK_EOF','message':'Official metadata GET ended with EOF; permission result unknown'}
    match=re.search(r'\b(401|403|404|429|500|502|503)\b',text)
    if match:return {'code':'PLATFORM_HTTP_'+match[1],'message':'Official metadata GET did not succeed'}
    return {'code':'PLATFORM_LOOKUP_FAILED','message':'Official metadata GET failed; raw output suppressed'}
def _run(arguments,*,env=None,timeout=20):
    try:
        result=subprocess.run([str(BINARY),*arguments],capture_output=True,text=True,encoding='utf-8',timeout=timeout,shell=False,env=env)
    except subprocess.TimeoutExpired:fail('TUNNEL_TIMEOUT','Read-only tunnel-client operation timed out')
    except OSError:fail('TUNNEL_CLIENT_UNAVAILABLE','Installed tunnel-client could not run')
    if len(result.stdout.encode())>4*1024*1024:fail('TUNNEL_RESPONSE_LIMIT','Read-only client output exceeded metadata bound')
    if result.returncode:return {'_error':_json_error(result.stderr+result.stdout)}
    try:return json.loads(result.stdout)
    except (ValueError,UnicodeError):fail('TUNNEL_PROTOCOL','Read-only tunnel-client did not return JSON')

def platform_get(tunnel_id,credential_reference):
    """The supported official GET consumes a file/env ref without logging it."""
    if not isinstance(tunnel_id,str) or not TUNNEL_ID.fullmatch(tunnel_id):
        fail('TUNNEL_SCOPE_DENIED','An explicit valid Tunnel ID is required for metadata lookup')
    if not isinstance(credential_reference,str):fail('TUNNEL_CREDENTIAL_REF','Secure credential reference required')
    if credential_reference.startswith('file:'):
        path=Path(credential_reference[5:]);info=path.stat()
        if info.st_uid!=os.getuid() or info.st_mode&0o077:fail('TUNNEL_CREDENTIAL_PERMISSIONS','Runtime credential file must be private and owned')
        secret=bounded_file(path,16384).decode().strip()
    elif credential_reference.startswith('env:'):
        name=credential_reference[4:]
        if not re.fullmatch(r'[A-Z_][A-Z0-9_]*',name):fail('TUNNEL_CREDENTIAL_REF','Invalid environment credential reference')
        secret=os.environ.get(name,'')
    else:fail('TUNNEL_CREDENTIAL_REF','Inline keys are never accepted')
    if not secret or any(char.isspace() for char in secret):fail('TUNNEL_CREDENTIAL_REF','Runtime credential reference did not resolve safely')
    # Runtime and admin keys are different. This GET is documented to accept
    # runtime keys; no admin key, CRUD request or poll request is attempted.
    environment={k:v for k,v in os.environ.items() if not any(word in k.upper() for word in ['TOKEN','KEY','SECRET','PASSWORD'])}
    environment['CONTROL_PLANE_API_KEY']=secret
    value=_run(['admin','--json','tunnels','get',tunnel_id],env=environment)
    if '_error' in value:return {'read_permission':'UNKNOWN','use_permission':'UNKNOWN','error':value['_error'],'checked_at':now()}
    if value.get('id')!=tunnel_id:fail('TUNNEL_IDENTITY_MISMATCH','Platform returned a different Tunnel ID')
    remote={key:value.get(key) for key in ['id','name','description','organization_ids','workspace_ids','tenant_ids']}
    return {'read_permission':'VERIFIED_GET','use_permission':'UNKNOWN','metadata':remote,'checked_at':now(),
            'basis':'Official tunnel-client admin tunnels get using the existing runtime key; GET proves Read only'}

def runtime_status(alias):return _run(['runtimes','status',alias,'--json'],timeout=20)
def runtime_list():return _run(['runtimes','list','--json'])
def safe_runtime(value):
    if '_error' in value:return {'process_running':None,'runtime_state':'unknown','healthz':None,'readyz':None,'error':value['_error']}
    local=value.get('local',{});effective=local.get('effective_health',local.get('health',{}))
    return {'process_running':value.get('process_running'),'runtime_state':value.get('runtime_state'),
            'healthz':effective.get('healthz',{}).get('status'),'readyz':effective.get('readyz',{}).get('status'),
            'control_plane_poll_health':value.get('control_plane_poll_health',{'state':'unknown'}),
            'remote_lookup_error':_json_error(value['remote_error']) if value.get('remote_error') else None}

def _constants(path):
    values={}
    for node in ast.parse(bounded_file(path).decode()).body:
        if isinstance(node,ast.Assign):
            for target in node.targets:
                if isinstance(target,ast.Name) and target.id in {'URL','SOURCE','WRITER','RUNNER','PROJECT','BUILD_PROJECT','PREFIXES'}:
                    try:values[target.id]=ast.literal_eval(node.value)
                    except (ValueError,TypeError):pass
    return values

def _runner(state):
    path=state/'runner.toml'
    if not path.exists():return {},[]
    value=tomllib.loads(bounded_file(path).decode());policy=value.get('policy',{})
    registrations=[]
    directory=state/'project-registry'
    for registration in sorted(directory.glob('*.toml')):
        record=tomllib.loads(bounded_file(registration).decode())
        registrations.append({key:record.get(key) for key in ['id','path','allow_patch']})
    return {'client_id':value.get('client_id'),'server_url':value.get('server_url'),'runner_config':str(path),
            'runner_config_sha256':digest(bounded_file(path)),
            'native_allowed_roots':policy.get('allowed_roots',[]),'native_allow_raw_shell':policy.get('allow_raw_shell')},registrations

def gateway_binding(command):
    """Inspect registered source/config; never import or execute another gateway."""
    if not isinstance(command,str):return {'status':'UNKNOWN','reason':'MCP command is not a string'}
    words=shlex.split(command)
    candidates=[Path(word) for word in words if word.endswith('.py') and word.startswith('/')]
    if not candidates and '-m' in words:
        index=words.index('-m')
        if index+1<len(words) and words[index+1]=='paper_studio.gateway.main':
            # Inspect only the explicitly configured import roots. Never import
            # another Gateway or guess which Python environment wins resolution.
            paths=[word.split('=',1)[1] for word in words if word.startswith('PYTHONPATH=')]
            for value in paths:
                for root in value.split(os.pathsep):
                    if root.startswith('/'):
                        path=Path(root)/'paper_studio/gateway/main.py'
                        if path.is_file():candidates.append(path)
    if len(candidates)!=1:return {'status':'UNKNOWN','reason':'Gateway entrypoint is not unambiguous'}
    entry=candidates[0];constants=_constants(entry);config_path=None
    if '--config' in words:
        index=words.index('--config')
        if index+1>=len(words):return {'status':'UNKNOWN','reason':'Missing configured gateway path'}
        config_path=Path(words[index+1]);config=json.loads(bounded_file(config_path).decode())
    else:config=None
    from management.paper_instance_registry import attached_config
    attached=attached_config(entry);legacy_config=None;legacy_config_path=None
    if attached is not None and attached!=config_path:
        shared=json.loads(bounded_file(attached).decode())
        if shared.get('software_id')!='webcodex-paper-studio':fail('TUNNEL_IDENTITY_MISMATCH','Attached configuration is not Paper Studio')
        if config is not None and config.get('software_id')=='webcodex-paper-studio':
            fail('TUNNEL_IDENTITY_MISMATCH','Wrapper has conflicting shared Paper Studio configurations')
        legacy_config,legacy_config_path=config,config_path
        config,config_path=shared,attached
    result={'gateway_entrypoint':str(entry),'gateway_sha256':digest(bounded_file(entry)),'status':'VERIFIED_LOCAL_SOURCE','project_bindings':[]}
    if config is not None and config.get('software_id')=='webcodex-paper-studio':
        for item in [config,*config.get('projects',[])]:
            result['project_bindings'].append({key:item.get(key,[]) if key.endswith('_roots') else item.get(key) for key in ['project_id','project_root','writable_roots','readonly_roots','native_project','manuscript_roots']})
        result.update(gateway_kind='paper_studio',gateway_config=str(config_path),config_sha256=digest(bounded_file(config_path)),
                      primary_project=config['project_id'],runner=config['client_id'],server_url=config['server_url'],state_root=config['state_root'])
        if constants.get('RUNNER') not in (None,config['client_id']):
            fail('TUNNEL_IDENTITY_MISMATCH','Wrapper and shared configuration name different Runners')
        result['gateway_mode']='shared_extension' if attached is not None else 'shared_direct'
        from management.paper_instance_registry import runner_state
        state=runner_state(config);token=Path(config['credentials']['reader'])
    else:
        if config is not None:
            state=Path(config['state']);server=config['url'];runner=config['runner'];writer=config['writer_project'];source=config['source_project'];kind='legacy_if'
            writer_root=config['root'];prefixes=sorted(constants.get('PREFIXES',[]));token=state/'reader-token'
        else:
            try:
                from management.paper_tunnels_legacy_local import resolve_legacy
            except ImportError:
                return {**result,'status':'UNKNOWN','reason':'Optional site-local historical adapter is not installed'}
            legacy=resolve_legacy(entry,constants,bounded_file)
            if legacy is None:return {**result,'status':'UNKNOWN','reason':'Unrecognized gateway binding schema'}
            state,server,runner,kind,writer,source,writer_root,prefixes,token=(legacy[key] for key in
                ('state','server','runner','kind','writer','source','writer_root','prefixes','token'))
        registration,records=_runner(state)
        for record in records:
            full='agent:'+runner+':'+record['id']
            if full not in {source,writer}:continue
            result['project_bindings'].append({'project_id':full,'native_project':full,'project_root':record['path'],
                'writable_roots':prefixes if full==writer else [],'readonly_roots':[] if full==writer else ['.'],
                'native_allow_patch':record['allow_patch'],'gateway_role':'writer' if full==writer else 'source_read_only'})
        result.update(registration)
        result.update(gateway_kind=kind,primary_project=writer or source,runner=runner,server_url=server,state_root=str(state))
    registration,records=_runner(state)
    result.update(native_registration=records,runner_policy=registration)
    if config is not None and config.get('software_id')=='webcodex-paper-studio':
        identities={item.get(key) for item in [config,*config.get('projects',[])] for key in ['native_project','native_artifact_project']}
        legacy=[]
        if legacy_config is not None:
            if legacy_config.get('runner')!=config['client_id']:
                fail('TUNNEL_IDENTITY_MISMATCH','Legacy and shared configurations name different Runners')
            result['legacy_gateway_config']=str(legacy_config_path)
            result['legacy_gateway_config_sha256']=digest(bounded_file(legacy_config_path))
        for record in records:
            full='agent:'+config['client_id']+':'+record['id']
            is_source=legacy_config is not None and full==legacy_config.get('source_project')
            is_writer=legacy_config is not None and full==legacy_config.get('writer_project')
            if is_source or is_writer or full not in identities and record.get('allow_patch') is False:
                item={'project_id':full,'native_project':full,'project_root':record['path'],
                    'writable_roots':sorted(constants.get('PREFIXES',[])) if is_writer else [],
                    'readonly_roots':[] if is_writer else ['.'],'native_allow_patch':record['allow_patch'],
                    'gateway_role':'legacy_writer' if is_writer else 'source_read_only','interface':'legacy_native'}
                legacy.append(item)
                if full not in identities:result['project_bindings'].append(item)
        result['legacy_project_bindings']=legacy
        from management.paper_instance_registry import enrich_binding
        result=enrich_binding(result,config,entry)
    # Private only. The public inventory strips this before serialization.
    result['_reader_token_ref']='file:'+str(token)
    return result

def native_probe(binding):
    reference=binding.get('_reader_token_ref','')
    if not reference.startswith('file:'):return {'status':'UNKNOWN','reason':'No authoritative reader credential reference'}
    try:
        result=NativeClient(binding['server_url'],reference[5:],timeout=8).call('runtime_status',{'client_id':binding['runner'],'compact':False})
        value=result.get('structuredContent',{})
        if result.get('isError'):return {'status':'UNKNOWN','reason':'Native runtime tool rejected read-only query'}
        if value.get('success') is True:value=value.get('output',{})
        runners=value.get('runners',{});clients=runners.get('clients',[])
        selected=[client for client in clients if client.get('client_id')==binding['runner']]
        focus=value.get('focus',{})
        if focus.get('client_id')!=binding['runner']:focus={}
        client=selected[0] if selected else focus
        return {'status':'VERIFIED_NATIVE_READ','runner_online':client.get('status')=='online',
                'native_version':value.get('version'),'runner_client_id':binding['runner'],
                'protocol_compatibility':focus.get('protocol_compatibility'),
                'build_alignment':focus.get('build_alignment'),
                'active_jobs':value.get('jobs',{}).get('active_count'),'pending_requests':client.get('pending_requests'),
                'runner_project_count':client.get('projects_count',client.get('project_count')),'checked_at':now()}
    except Exception:return {'status':'UNKNOWN','runner_online':None,'active_jobs':None,'reason':'Native runtime read unavailable; no raw credential/response output'}

def collect_inventory(*,profile_dir=PROFILE_DIR,list_aliases=runtime_list,probe_runtime=runtime_status,
                      get_platform=platform_get,probe_native=native_probe,use_evidence=None,shared_credential_ref=None,
                      target_ids=None):
    """Discover N explicitly configured identities without creating or claiming any.

    Runtime aliases alone are not a registration grant: targets come from valid
    local profiles or an explicit caller list, never from arbitrary remote names.
    An explicit shared reference can inspect caller-selected remote-only IDs;
    otherwise each target uses its own configured secure credential reference.
    """
    if target_ids is not None and (not isinstance(target_ids,(list,tuple)) or len(target_ids)>MAX_TUNNELS
            or any(not isinstance(x,str) or not TUNNEL_ID.fullmatch(x) for x in target_ids)
            or len(set(target_ids))!=len(target_ids)):
        fail('TUNNEL_SCOPE_DENIED','At most 256 unique explicit Tunnel IDs are allowed')
    selected=set(target_ids) if target_ids is not None else None
    aliases=list_aliases().get('aliases',[]);entries={};references={};unknown_profiles=[]
    for profile in sorted(Path(profile_dir).glob('*.yaml')):
        try:
            value=yaml.safe_load(bounded_file(profile,65536))
            if not isinstance(value,dict) or not isinstance(value.get('control_plane'),dict):raise ValueError('Object profile required')
            control=value['control_plane'];tunnel_id=control.get('tunnel_id')
        except (OSError,ValueError,yaml.YAMLError,TunnelError):
            unknown_profiles.append({'profile':str(profile),'status':'config_error','reason':'Profile could not be parsed; no identity was guessed'})
            continue
        if not isinstance(tunnel_id,str) or not TUNNEL_ID.fullmatch(tunnel_id):
            unknown_profiles.append({'profile':str(profile),'status':'config_error','reason':'Profile has no valid explicit Tunnel ID'})
            continue
        if selected is not None and tunnel_id not in selected:continue
        if tunnel_id not in entries and len(entries)>=MAX_TUNNELS:
            fail('TUNNEL_INPUT_LIMIT','Configured Tunnel inventory exceeds its bounded size')
        reference=control.get('api_key')
        valid_reference=isinstance(reference,str) and reference.startswith(('file:','env:'))
        reference_id=ref_id(reference) if valid_reference else None
        if valid_reference:references[reference_id]=reference
        commands=value.get('mcp',{}).get('commands',[])
        if len(commands)!=1:binding={'status':'UNKNOWN','reason':'Expected exactly one main MCP command'}
        elif not valid_reference:binding={'status':'UNKNOWN','reason':'Credential must use a private file/env reference; value suppressed'}
        else:
            try:binding=gateway_binding(commands[0].get('command'))
            except Exception:binding={'status':'UNKNOWN','reason':'Gateway binding could not be independently parsed'}
        alias=profile.stem;registered=next((item for item in aliases if item.get('alias')==alias),{})
        status=safe_runtime(probe_runtime(alias));binding['runtime']=probe_native(binding) if binding.get('runner') else {'status':'UNKNOWN'}
        entries.setdefault(tunnel_id,[]).append({'alias':alias,'profile':str(profile),'profile_sha256':digest(bounded_file(profile)),
                    'credential_ref':reference_id,'cached_name':registered.get('name'),**status,'binding':binding})
    shared_id=ref_id(shared_credential_ref) if shared_credential_ref is not None else None
    if shared_id is not None:references[shared_id]=shared_credential_ref
    records=[]
    for tunnel_id in sorted(selected if selected is not None else entries,reverse=True):
        local=entries.get(tunnel_id,[]);active=[item for item in local if item.get('process_running') is True]
        target_reference=shared_credential_ref
        if target_reference is None:
            available=[item['credential_ref'] for item in (active or local) if item.get('credential_ref') in references]
            if available:target_reference=references[available[0]]
        target_ref_id=ref_id(target_reference) if target_reference is not None else None
        try:
            permission=get_platform(tunnel_id,target_reference) if target_reference is not None else {
                'read_permission':'UNKNOWN','use_permission':'UNKNOWN','error':{'code':'TUNNEL_CREDENTIAL_REF','message':'No configured secure credential reference was observed for this Tunnel'}}
        except TunnelError as error:permission={'read_permission':'UNKNOWN','use_permission':'UNKNOWN','error':{'code':error.code,'message':error.message}}
        if use_evidence and tunnel_id in use_evidence:permission['use_evidence']=use_evidence[tunnel_id]
        name=permission.get('metadata',{}).get('name');projects=[]
        for item in (active or local):
            for project in item['binding'].get('project_bindings',[]):
                if project.get('project_id') not in {p.get('project_id') for p in projects}:projects.append(project)
        online=bool(active and all(item.get('healthz')==200 and item.get('readyz')==200 for item in active))
        state='online' if online else 'offline' if local and all(item.get('process_running') is False for item in local) else 'error'
        record={'tunnel_id':tunnel_id,'platform_name':name,'display_mapping_status':'VERIFIED_PLATFORM' if name else 'UNKNOWN',
                'aliases':local,'state':state,'binding_state':'bound' if local else 'UNKNOWN','registered_projects':projects,
                'primary_bindings':[{'alias':item['alias'],'project_id':item['binding'].get('primary_project')} for item in active],
                'shared_runtime_key_ref':target_ref_id,'shared_key_permission':permission,
                'safe_to_rebind':False,'rebind_reason':'Explicit user selection and lifecycle checks required; occupied/production mappings preserved',
                'last_successful_verification':{'at':now(),'scope':'local_runtime_health_only'} if online else None,'free':False}
        if len(active)>1:record.update(state='error',rebind_reason='Multiple active aliases share one Tunnel; do not attach another client')
        if any(item['binding'].get('status')=='UNKNOWN' for item in local):
            record.update(state='error',rebind_reason='Local project configuration is unverified; healthy connections were not changed')
        records.append(record)
    return {'schema':'paper-editor.tunnel-registry.v1','observed_at':now(),'client_version':'0.0.14','tunnels':records,
            'official_permissions_doc':DOC,'platform_read_and_use_are_distinct':True,'offline_is_not_free':True,
            'mutation_actions':0,'stdio_clients_started':0,'unknown_profiles':unknown_profiles,
            'discovery_scope':'explicit_target_ids' if selected is not None else 'configured_local_profiles',
            'dynamic_target_count':len(records),'_private_credential_refs':references}

def public_inventory(value):
    if isinstance(value,list):return [public_inventory(item) for item in value]
    if isinstance(value,dict):return {key:public_inventory(item) for key,item in value.items() if not key.startswith('_')}
    return value

def write_registry(value,*,private_path=DEFAULT_PRIVATE,public_path=None):
    # New management state only; no existing Tunnel/Runner config is modified.
    private_path=Path(private_path)
    if not private_path.is_absolute() or private_path.resolve()!=private_path:fail('TUNNEL_PATH_DENIED','Private registry requires an absolute canonical path')
    private_path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    if private_path.parent.stat().st_mode&0o077:fail('TUNNEL_REGISTRY_PERMISSIONS','Private registry directory must be private')
    from management.private_files import private_write
    private_write(private_path,json.dumps(value,ensure_ascii=False,indent=2)+'\n',replace=private_path.exists())
    if public_path is not None:
        path=Path(public_path)
        if not path.is_absolute() or path.resolve()!=path:fail('TUNNEL_PATH_DENIED','Public registry requires an absolute canonical path')
        path.parent.mkdir(parents=True,exist_ok=True)
        with path.open('x',encoding='utf-8') as stream:json.dump(public_inventory(value),stream,ensure_ascii=False,indent=2);stream.write('\n')
    return {'private_registry_written':True,'public_registry_written':public_path is not None,'tunnel_configuration_changed':False}

def choices(inventory,project_id):
    result={'available_unbound':[],'bound_online':[],'bound_offline':[],'current_project':[],'unknown':[]}
    for record in inventory['tunnels']:
        identity=record['tunnel_id']
        if any(item.get('project_id')==project_id for item in record.get('registered_projects',[])):result['current_project'].append(identity)
        if record.get('binding_state')=='bound':result['bound_online' if record.get('state')=='online' else 'bound_offline'].append(identity)
        elif (record.get('binding_state')=='unbound' and record.get('unbound_verified') is True and record.get('state')=='offline'
              and record.get('shared_key_permission',{}).get('use_permission')=='VERIFIED_READONLY_INTROSPECTION'):
            result['available_unbound'].append(identity)
        else:result['unknown'].append(identity)
    return result

def binding_plan(inventory,project,*,selected_tunnel_id=None,allow_rebind=False,create_new=False):
    if not isinstance(project,dict) or not project.get('project_id') or not isinstance(project.get('project_root'),str):fail('TUNNEL_PROJECT_INVALID','Exact project identity/root required')
    root=Path(project['project_root'])
    if not root.is_absolute() or root.resolve()!=root:fail('TUNNEL_PROJECT_INVALID','Canonical project root required')
    plan={'schema':'paper-editor.tunnel-binding-plan.v1','observed_at':inventory['observed_at'],'project':project,
          'choices':choices(inventory,project['project_id']),'selected_tunnel_id':selected_tunnel_id,'live_apply_enabled':False,
          'manager_entrypoints':{'plan':'management/paper_rebind.py plan --profile INSTANCE --project-id PROJECT --operation-id ID --output PLAN',
                                'switch':'management/paper_rebind.py apply --profile INSTANCE --plan PLAN --authorized',
                                'rollback':'management/paper_rebind.py rollback --record RECORD --authorized',
                                'recover':'management/paper_rebind.py recover --record RECORD --authorized'},
          'validation_required':['idle builds/edits/publish','fresh profile/config hashes','same Tunnel only one stdio client','native registration and fine path permissions','tunnel-client doctor after approved binding','health/ready','ChatGPT exact project_id/root readback'],
          'changes_executed':0}
    if create_new:
        return {**plan,'status':'BLOCKED','blockers':['CREATE_NOT_SELECTED_OR_AUTHORIZED_IN_THIS_RUN'],'create_executed':False}
    if selected_tunnel_id is None:return {**plan,'status':'AWAITING_USER_SELECTION','blockers':['NO_TUNNEL_SELECTED']}
    record=next((item for item in inventory['tunnels'] if item['tunnel_id']==selected_tunnel_id),None)
    if record is None:fail('TUNNEL_SCOPE_DENIED','Selected Tunnel is outside the verified registry')
    blockers=[]
    if (record.get('binding_state') not in {'bound','unbound'}
        or record.get('binding_state')=='unbound' and record.get('unbound_verified') is not True):
        blockers.append('BINDING_STATE_UNVERIFIED')
    if record.get('state')=='error':blockers.append('CONFIGURATION_ERROR')
    if record.get('binding_state')=='bound' and not allow_rebind:blockers.append('OCCUPIED_TUNNEL_REQUIRES_EXPLICIT_REBIND_SELECTION')
    if record.get('shared_key_permission',{}).get('use_permission')!='VERIFIED_READONLY_INTROSPECTION' and record.get('shared_key_permission',{}).get('use_evidence',{}).get('status')!='VERIFIED_EXISTING_CONNECTION':blockers.append('TUNNEL_USE_PERMISSION_UNKNOWN')
    for alias in record.get('aliases',[]):
        binding=alias.get('binding',{});runtime=binding.get('runtime',{})
        if runtime.get('active_jobs')!=0 or runtime.get('pending_requests')!=0:blockers.append('ACTIVE_OR_UNKNOWN_JOBS_OR_EDITS')
        activity=binding.get('local_activity',{})
        if activity.get('active_count',0) or activity.get('unknown_records',0):blockers.append('ACTIVE_OR_UNKNOWN_PROVIDER_BUILDS_OR_SYNCS')
        if binding.get('source_release',{}).get('status')=='invalid_release':blockers.append('SOURCE_RELEASE_INVALID')
        if binding.get('source_release',{}).get('status')=='unable_to_verify':blockers.append('SOURCE_RELEASE_UNVERIFIED')
        if binding.get('gateway_kind')!='paper_studio':blockers.append('PRODUCTION_OR_LEGACY_BINDING_NOT_AUTHORIZED_FOR_APPLY')
        elif len(binding.get('project_bindings',[]))>1:blockers.append('LEGACY_MANAGER_MULTIPROJECT_SWITCH_FAILS_CLOSED')
    return {**plan,'status':'BLOCKED' if blockers else 'PLAN_READY_REQUIRES_USER_APPROVAL',
            'blockers':sorted(set(blockers)),'previous_binding_preserved':True,'credential_rotated':False,
            'prior_route_identity':[{'profile':alias.get('profile'),'profile_sha256':alias.get('profile_sha256'),
                'gateway_config':alias.get('binding',{}).get('gateway_config'),
                'config_sha256':alias.get('binding',{}).get('config_sha256'),
                'project_bindings':alias.get('binding',{}).get('project_bindings',[])} for alias in record.get('aliases',[])],
            'rollback_plan':{'strategy':'Preserve exact prior route/config bytes before an authorized apply; verify current hashes before restoring only this change',
                'retain_project_history':True,'recheck_active_operations':True,'chatgpt_new_root_readback_required':True}}

def instance_registry(inventory):
    """Return safe shared instance facts without modifying existing bindings."""
    from management.paper_instance_registry import instance_registry as project
    return project(inventory)

def apply_fixture(plan,bindings,*,selected_tunnel_id=None,manager_switch=None,manager_rollback=None,verify=None,fixture=False):
    """Inject the existing switch/rollback contract into isolated fixture state.

    There is intentionally no live adapter or CLI apply command. A local source
    fixture proves selection/rollback mechanics, never production acceptance.
    """
    if not fixture:fail('TUNNEL_LIVE_APPLY_BLOCKED','Real binding/create requires a separately selected, approved and implemented lifecycle operation')
    chosen=plan.get('selected_tunnel_id')
    if not chosen or chosen!=selected_tunnel_id:fail('TUNNEL_SELECTION_REQUIRED','Fixture must explicitly select exactly the planned Tunnel')
    if plan.get('status')!='PLAN_READY_REQUIRES_USER_APPROVAL':fail('TUNNEL_PLAN_BLOCKED','Blocked plans cannot be applied even in a fixture')
    if any(callback is None for callback in [manager_switch,manager_rollback,verify]):fail('TUNNEL_FIXTURE_INVALID','Existing manager switch/rollback and validation callbacks are required')
    previous=copy.deepcopy(bindings[chosen]);other={key:copy.deepcopy(value) for key,value in bindings.items() if key!=chosen}
    try:
        manager_switch(chosen,copy.deepcopy(plan['project']))
        bindings[chosen]=copy.deepcopy(plan['project'])
        if verify(chosen,plan['project']) is not True:raise RuntimeError('Fixture validation failed')
        if any(bindings[key]!=value for key,value in other.items()):raise RuntimeError('Unselected fixture binding changed')
    except Exception:
        manager_rollback(chosen,previous);bindings[chosen]=previous
        for key,value in other.items():bindings[key]=value
        return {'status':'ROLLED_BACK_FIXTURE','fixture_only':True,'selected_tunnel_id':chosen,'production_changed':False}
    return {'status':'APPLIED_FIXTURE','fixture_only':True,'selected_tunnel_id':chosen,'production_changed':False}

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--public-output',type=Path);parser.add_argument('--private-output',type=Path,default=DEFAULT_PRIVATE)
    parser.add_argument('--instance-output',type=Path,help='Also write the public unified instance registry; no live binding action')
    args=parser.parse_args();inventory=collect_inventory()
    write_registry(inventory,private_path=args.private_output,public_path=args.public_output)
    if args.instance_output:
        path=args.instance_output
        if not path.is_absolute() or path.resolve()!=path:fail('TUNNEL_PATH_DENIED','Canonical absolute output path required')
        path.parent.mkdir(parents=True,exist_ok=True)
        with path.open('x',encoding='utf-8') as stream:json.dump(instance_registry(inventory),stream,ensure_ascii=False,indent=2);stream.write('\n')
    print(json.dumps(public_inventory(inventory),ensure_ascii=False,indent=2))

if __name__=='__main__':main()
