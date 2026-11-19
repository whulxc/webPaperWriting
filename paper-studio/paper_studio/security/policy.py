"""Project and revision guards adapted from prior project-scoped snapshots.

Native WebCodex still owns normal FS edits; these guards also protect adapter
snapshots and generated artifacts. A configured provider remains trusted code.
"""
from __future__ import annotations
import contextlib,ctypes,errno,hashlib,json,os,re,stat,tempfile,time
try:
    import fcntl
except ImportError:
    fcntl = None
from pathlib import Path, PurePosixPath

class StudioError(Exception):
    def __init__(self, code, message, **details):
        self.code,self.message,self.details=code,message,details
        super().__init__(message)
    def as_dict(self):return {'code':self.code,'message':self.message,'details':self.details}

def sha(data):return hashlib.sha256(data).hexdigest()
def encoded(value):return (json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode()
def file_sha(path):return sha(Path(path).read_bytes())
def fail(code,msg,**details):raise StudioError(code,msg,**details)
def no_links(path):
    p=Path(path)
    if not p.is_absolute() or p != Path(os.path.abspath(p)):fail('INVALID_PATH','Absolute canonical configured path required')
    for part in [*reversed(p.parents),p]:
        try:s=part.lstat()
        except FileNotFoundError:continue
        if stat.S_ISLNK(s.st_mode) or getattr(s, 'st_file_attributes', 0) & 0x400:
            fail('PATH_DENIED','Symlink or reparse path component rejected')
    return p

# Additional projects inherit only workstation-wide settings; all identity and
# filesystem boundary fields must be explicit so a typo cannot reuse old aliases.
PROJECT_FIELDS={'project_id','project_root','artifact_root','state_root','native_project','native_artifact_project','writable_roots','readonly_roots'}
OPTIONAL_PROJECT_FIELDS={'artifact_layout','manuscript_roots','zotero_readonly','zotero_write','zotero_backend','literature','evidence_reader','research_state_path','drive_source_sync','drive_folder_id','chapter_workflow','execution_policy','version_management','pdf_preview','target_index','review_proposals','job_index'}

def builds_root(cfg):
    """Resolve job storage without changing immutable version member paths."""
    root=Path(cfg['artifact_root'])
    return root if cfg.get('artifact_layout')=='project-builds-v1' else root/'builds'

def artifact_project_relative(cfg):
    """A project-owned artifact tree is never a manuscript input or evidence."""
    root=Path(cfg['artifact_root']);project=Path(cfg['project_root'])
    return root.relative_to(project).as_posix() if root.is_relative_to(project) else None

def validate_project_config(cfg):
    from paper_studio.capabilities import validate_admission
    validate_admission(cfg)
    if 'job_index' in cfg:
        from paper_studio.provider.job_index import validate_config
        validate_config(cfg)
    if 'target_index' in cfg:
        from paper_studio.versions.target_index import validate_config
        validate_config(cfg)
    if 'review_proposals' in cfg:
        from paper_studio.versions.review_proposals import validate_config
        validate_config(cfg)
    if 'chapter_workflow' in cfg:
        from paper_studio.chapters.workflow import validate_config
        validate_config(cfg)
    if 'pdf_preview' in cfg:
        from paper_studio.gateway.preview import validate_config
        validate_config(cfg)
    if cfg.get('drive_folder_id') is not None:
        if not isinstance(cfg['drive_folder_id'],str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}',cfg['drive_folder_id']):
            fail('CONFIG_INVALID','Publisher folder must be an explicit valid Google Drive ID')
    if 'provider_id' in cfg and (not isinstance(cfg['provider_id'],str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}',cfg['provider_id'])):fail('CONFIG_INVALID','Invalid provider identity')
    if 'execution_policy' in cfg:
        from paper_studio.execution import validate_policy
        validate_policy(cfg)
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}',cfg.get('project_id','')):fail('CONFIG_INVALID','Invalid project identity')
    for key in ['project_root','artifact_root','state_root']:
        cfg[key]=str(no_links(Path(cfg[key])))
    roots=[Path(cfg[k]) for k in ['project_root','artifact_root','state_root']]
    layout=cfg.get('artifact_layout')
    if layout not in (None,'project-builds-v1'):fail('CONFIG_INVALID','Unknown artifact layout')
    if layout=='project-builds-v1' and roots[1]!=roots[0]/'builds':fail('CONFIG_INVALID','Project artifact layout requires the exact project builds directory')
    for i,left in enumerate(roots):
        for j,right in enumerate(roots[i+1:],i+1):
            if layout=='project-builds-v1' and (i,j)==(0,1):continue
            if left.is_relative_to(right) or right.is_relative_to(left):fail('CONFIG_INVALID','Project, artifact and state trees must be separate')
    if not roots[0].is_dir():fail('CONFIG_INVALID','Registered project root missing')
    for key in ['writable_roots','readonly_roots']:
        if not isinstance(cfg.get(key,[]),list):fail('CONFIG_INVALID','Path roots must be lists',field=key)
        for value in cfg.get(key,[]):relpath(value)
    if 'version_management' in cfg:
        vm=cfg['version_management']
        if not isinstance(vm,dict) or set(vm)!={'root','targets'} or vm['root']!='versions':
            fail('CONFIG_INVALID','Managed paper versions require the dedicated versions root')
        if not isinstance(vm['targets'],dict) or set(vm['targets'])!={'en','zh'}:
            fail('CONFIG_INVALID','Managed paper versions require explicit EN/ZH trusted targets')
        targets=cfg.get('execution_policy',{}).get('tex_targets',{})
        if any(name not in targets for name in vm['targets'].values()) or len(set(vm['targets'].values()))!=2:
            fail('CONFIG_INVALID','Managed version recipes must reference distinct registered targets')
        if vm['root'] not in cfg.get('writable_roots',[]):
            fail('CONFIG_INVALID','Managed version root requires explicit project write admission')
    if layout=='project-builds-v1':
        if 'builds' not in cfg.get('readonly_roots',[]):fail('CONFIG_INVALID','Project artifact builds tree requires explicit readonly protection')
        if any(Path(value).is_relative_to('builds') or Path('builds').is_relative_to(value) for value in cfg.get('writable_roots',[])):
            fail('CONFIG_INVALID','Writable scopes cannot overlap the project artifact tree')
    manuscripts=cfg.get('manuscript_roots',[])
    if not isinstance(manuscripts,list) or len(set(manuscripts))!=len(manuscripts):fail('CONFIG_INVALID','manuscript_roots must be a unique list')
    for value in manuscripts:
        relpath(value)
        if value not in cfg.get('writable_roots',[]) or value+'/builds' not in cfg.get('readonly_roots',[]):fail('CONFIG_INVALID','Manuscript requires explicit writable root and readonly builds subtree')
        if any(value!=other and (Path(value).is_relative_to(other) or Path(other).is_relative_to(value)) for other in manuscripts):fail('CONFIG_INVALID','Manuscript roots overlap')
    if 'zotero_readonly' in cfg:
        binding=cfg['zotero_readonly']
        from paper_studio.references.zotero_worker import valid_scope,ReadError
        try:valid_scope(binding)
        except ReadError as error:fail('CONFIG_INVALID',error.message)
        if not isinstance(binding['helper_path'],str) or not binding['helper_path'].endswith('/zotero.py'):
            fail('CONFIG_INVALID','Zotero helper must be an explicit installed zotero.py path')
        no_links(Path(binding['helper_path']))
        if not isinstance(binding['helper_sha256'],str) or not re.fullmatch('[0-9a-f]{64}',binding['helper_sha256']):
            fail('CONFIG_INVALID','Zotero helper SHA256 is required')
    if 'zotero_write' in cfg:
        from paper_studio.references.zotero_import import validate_binding
        validate_binding(cfg)
    if 'zotero_backend' in cfg:
        backend=cfg['zotero_backend']
        if not isinstance(backend,dict) or set(backend)!={'backend','sdk_path','server_id','credential_path','allow_create_collection','allow_update'} or backend['backend']!='pyzotero_local':fail('CONFIG_INVALID','Only the explicitly selected Pyzotero local backend is supported')
        no_links(Path(backend['sdk_path']))
        if backend['credential_path'] is not None:no_links(Path(backend['credential_path']))
        if backend['server_id'] is not None and (not isinstance(backend['server_id'],str) or not re.fullmatch('[A-Za-z0-9_-]{1,128}',backend['server_id'])):fail('CONFIG_INVALID','Invalid pinned Zotero server ID')
        if backend['credential_path'] is not None and backend['server_id'] is None:fail('CONFIG_INVALID','A local grant requires a pinned server ID')
        if type(backend['allow_create_collection']) is not bool or backend['allow_update'] is not False:fail('CONFIG_INVALID','Only explicit collection creation is supported; updates remain disabled')
        if 'zotero_readonly' not in cfg:fail('CONFIG_INVALID','Local backend requires explicit readonly project scope')
    if 'literature' in cfg:
        value=cfg['literature']
        if not isinstance(value,dict) or set(value)!={'enabled','providers','max_requests'} or type(value['enabled']) is not bool or value['providers']!=['crossref'] or type(value['max_requests']) is not int or not 1<=value['max_requests']<=4:fail('CONFIG_INVALID','Explicit bounded Crossref public metadata policy required')
    if 'evidence_reader' in cfg:
        value=cfg['evidence_reader']
        if not isinstance(value,dict) or set(value)!={'enabled','max_pdf_bytes'} or type(value['enabled']) is not bool or type(value['max_pdf_bytes']) is not int or not 1<=value['max_pdf_bytes']<=33554432:fail('CONFIG_INVALID','Explicit bounded PDF evidence policy required')
    if 'research_state_path' in cfg:
        path_for(cfg,cfg['research_state_path'],True)
        if not cfg['research_state_path'].endswith('.json'):fail('CONFIG_INVALID','Project continuity must identify one writable JSON file')
    if 'drive_source_sync' in cfg:
        from paper_studio.drive.source_sync import config
        config(cfg)
    return cfg

def load_config(path):
    p=no_links(Path(path));raw=p.read_bytes();cfg=json.loads(raw)
    if cfg.get('schema_version')!=1 or cfg.get('software_id')!='webcodex-paper-studio':fail('CONFIG_INVALID','Unsupported instance configuration')
    for key in ['instance_id','client_id']:
        if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}',cfg.get(key,'')):fail('CONFIG_INVALID','Invalid identity',field=key)
    cfg['_config_path']=str(p);cfg['_config_revision']=sha(raw)
    return resolve_projects(cfg)

def resolve_projects(config):
    # Shared validation for the Gateway, provider and local manager.
    cfg=dict(config)
    if cfg.get('chat_file_delivery', 'native') not in ('native', 'drive_links_only'):
        fail('CONFIG_INVALID','chat_file_delivery must be native or drive_links_only')
    extra=cfg.get('projects',[])
    if not isinstance(extra,list):fail('CONFIG_INVALID','projects must be an array')
    validate_project_config(cfg)
    resolved=[]
    for entry in extra:
        if not isinstance(entry,dict) or not PROJECT_FIELDS<=set(entry) or set(entry)-PROJECT_FIELDS-OPTIONAL_PROJECT_FIELDS:fail('CONFIG_INVALID','Additional project requires exact project boundary fields')
        item={k:v for k,v in cfg.items() if k not in PROJECT_FIELDS|OPTIONAL_PROJECT_FIELDS|{'projects','_projects'}}
        item.update(entry);validate_project_config(item);resolved.append(item)
    projects=[cfg,*resolved];ids=set();aliases=set();trees=[]
    for item in projects:
        if item['project_id'] in ids:fail('CONFIG_INVALID','Duplicate project identity')
        ids.add(item['project_id'])
        for key in ['native_project','native_artifact_project']:
            alias=item.get(key)
            if alias is not None:
                if not isinstance(alias,str) or not alias or alias in aliases:fail('CONFIG_INVALID','Native project aliases must be unique')
                aliases.add(alias)
        current=[]
        for key in ['project_root','artifact_root','state_root']:
            tree=Path(item[key])
            if any(tree.is_relative_to(other) or other.is_relative_to(tree) for other in trees):fail('CONFIG_INVALID','Project filesystem boundaries overlap')
            current.append(tree)
        trees.extend(current)
    cfg['_projects']=resolved
    return cfg

def select_project(cfg,project_id):
    for item in [cfg,*cfg.get('_projects',[])]:
        if item['project_id']==project_id:return item
    fail('PROJECT_DENIED','Project is not bound to this workstation',project_id=project_id)

def project_description(cfg):
    return {**{key:cfg[key] for key in ['project_id','project_root','artifact_root','writable_roots','readonly_roots']},'chat_file_delivery':cfg.get('chat_file_delivery','native'),'artifact_layout':cfg.get('artifact_layout','external-v1'),'build_artifact_store':artifact_project_relative(cfg) or cfg['artifact_root'],'manuscript_roots':cfg.get('manuscript_roots',[]),
            'version_management':cfg.get('version_management'),
            'managed_version_write_policy':'draft working/main_en.tex, working/main_zh.tex and working/deps/<support file> only; all revisions, candidates, metadata and published versions protected' if cfg.get('version_management') else None}

def project(cfg, project_id):
    if project_id != cfg['project_id']:fail('PROJECT_DENIED','Project is not bound to this workstation',project_id=project_id)

DENY_NAMES={'.git','.ssh','.aws','.azure','.codex','.config','.env','credentials','secrets','node_modules','__pycache__'}
def relpath(value):
    if not isinstance(value,str) or not value or len(value)>1024:fail('INVALID_PATH','Nonempty project-relative path required')
    p=PurePosixPath(value)
    if p.is_absolute() or p.as_posix()!=value or any(x in {'..','.'} for x in p.parts) or any(x in value for x in ('\\',':','\x00')):fail('PATH_DENIED','Noncanonical or absolute path rejected')
    for x in p.parts:
        if x in DENY_NAMES or x.startswith('.env.') or x.lower().endswith(('.key','.pem','.p12')) or re.search(r'(^|[-_.])(token|secret|credentials?)([-_.]|$)',x,re.I):fail('PATH_DENIED','Credential or administrative path rejected')
    return p

def path_for(cfg,value,write=False):
    p=relpath(value);root=Path(cfg['project_root']);dest=root.joinpath(*p.parts);no_links(dest)
    if write and cfg.get('chapter_workflow'):
        from paper_studio.chapters.workflow import guard_write
        guard_write(cfg,value)
    if write and cfg.get('version_management'):
        from paper_studio.versions.lifecycle import guard_write
        guard_write(cfg,value)
    if not dest.is_relative_to(root):fail('PATH_DENIED','Path escaped project')
    if write and any(p==PurePosixPath(r) or p.is_relative_to(r) for r in cfg.get('readonly_roots',[])):fail('WRITE_DENIED','Path is in a readonly root')
    if write and not any(p==PurePosixPath(w) or p.is_relative_to(w) for w in cfg.get('writable_roots',[])):fail('WRITE_DENIED','Path is outside candidate writable roots')
    if write and cfg.get('review_proposals',{}).get('enabled'):
        from paper_studio.versions.review_proposals import is_protected_export
        if is_protected_export(cfg,value):
            fail('WRITE_DENIED','Confirmed review source trees are immutable')
    if dest.exists():
        s=dest.stat()
        if not stat.S_ISREG(s.st_mode) or s.st_nlink!=1:fail('PATH_DENIED','Only regular single-link files are permitted')
    return dest

def require_posix_filesystem():
    if fcntl is None or not hasattr(os, 'O_DIRECTORY') or not hasattr(os, 'O_NOFOLLOW'):
        fail('PLATFORM_UNSUPPORTED', 'Protected project filesystem operations require a POSIX Gateway host; use WSL 2 or Linux on Windows')


def safe_read(root,relative,max_bytes=2*1024*1024):
    """openat O_NOFOLLOW traversal; compare identity after bounded read."""
    require_posix_filesystem()
    root=no_links(Path(root));p=relpath(relative);fds=[]
    try:
        fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW);fds.append(fd)
        for name in p.parts[:-1]:
            fd=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd);fds.append(fd)
        fd=os.open(p.parts[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd);fds.append(fd)
        before=os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or before.st_size>max_bytes:fail('INPUT_LIMIT','Input is nonregular, linked, or too large',path=relative)
        chunks=[];size=0
        while True:
            b=os.read(fd,min(65536,max_bytes+1-size))
            if not b:break
            chunks.append(b);size+=len(b)
            if size>max_bytes:fail('INPUT_LIMIT','Input exceeds byte limit',path=relative)
        after=os.fstat(fd)
        fields=lambda x:(x.st_dev,x.st_ino,x.st_size,x.st_mtime_ns,x.st_ctime_ns)
        if fields(before)!=fields(after):fail('REVISION_CONFLICT','Input changed during capture',path=relative)
        return b''.join(chunks)
    except (FileNotFoundError,NotADirectoryError):fail('NOT_FOUND','Project input does not exist',path=relative)
    except OSError:fail('PATH_DENIED','Input could not be opened safely',path=relative)
    finally:
        for fd in reversed(fds):os.close(fd)

def revision(cfg,path):
    dest=path_for(cfg,path)
    return sha(safe_read(cfg['project_root'],path,64*1024*1024)) if dest.exists() else 'absent'

def require_revision(cfg,path,expected):
    if not isinstance(expected,str):fail('REVISION_REQUIRED','Expected SHA256 or absent is required',path=path)
    actual=revision(cfg,path)
    if actual!=expected:fail('REVISION_CONFLICT','Expected revision does not match current file',path=path,actual_revision=actual)
    return actual

@contextlib.contextmanager
def locked(cfg,name='project'):
    require_posix_filesystem()
    state=Path(cfg['state_root']);state.mkdir(mode=0o700,parents=True,exist_ok=True)
    p=state/(name+'.lock');no_links(p)
    fd=os.open(p,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:fcntl.flock(fd,fcntl.LOCK_EX);yield
    finally:fcntl.flock(fd,fcntl.LOCK_UN);os.close(fd)

def _create_mode():
    """Mode an O_CREAT file would receive under the current umask."""
    try:
        for line in Path('/proc/self/status').read_text().splitlines():
            if line.startswith('Umask:'):return 0o666&~int(line.split()[1],8)
    except (OSError,ValueError,IndexError):pass
    return 0o644


# Filesystem devices observed without RENAME_NOREPLACE (for example WSL DrvFS).
_NOREPLACE_UNSUPPORTED=set()


def _rename_noreplace(source,destination):
    """Publish a complete file only if the name is absent; False if unsupported."""
    if os.name == 'nt':
        return False
    rename=getattr(ctypes.CDLL(None,use_errno=True),'renameat2',None)
    if rename is None:return False
    if rename(-100,os.fsencode(str(source)),-100,os.fsencode(str(destination)),1)==0:return True
    code=ctypes.get_errno()
    if code==errno.EEXIST:raise FileExistsError(code,os.strerror(code),str(destination))
    if code in {errno.EINVAL,errno.ENOSYS,errno.ENOTSUP,errno.EOPNOTSUPP}:return False
    raise OSError(code,os.strerror(code),str(destination))


def atomic_json(path,value,exclusive=False):
    p=Path(path);no_links(p);p.parent.mkdir(parents=True,exist_ok=True)
    raw=encoded(value)
    if exclusive:
        # Create-only records are written completely and fsynced under a temporary
        # name, then published with RENAME_NOREPLACE. A crash can no longer leave
        # a truncated record that blocks every later reader or re-creation.
        if os.path.lexists(p):raise FileExistsError(errno.EEXIST,os.strerror(errno.EEXIST),str(p))
        device=os.stat(p.parent).st_dev
        if device in _NOREPLACE_UNSUPPORTED:
            # Filesystems without RENAME_NOREPLACE keep exclusive-create semantics.
            with p.open('xb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
            return
        fd,tmp=tempfile.mkstemp(prefix='.studio-',dir=p.parent)
        try:
            if hasattr(os, 'fchmod'):
                os.fchmod(fd,_create_mode())
            with os.fdopen(fd,'wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
            if _rename_noreplace(tmp,p):
                with contextlib.suppress(OSError):
                    directory=os.open(p.parent,os.O_RDONLY|os.O_DIRECTORY)
                    try:os.fsync(directory)
                    finally:os.close(directory)
            else:
                _NOREPLACE_UNSUPPORTED.add(device)
                with p.open('xb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
        finally:
            if os.path.lexists(tmp):os.unlink(tmp)
    else:
        fd,tmp=tempfile.mkstemp(prefix='.studio-',dir=p.parent)
        try:
            with os.fdopen(fd,'wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
            os.replace(tmp,p)
        finally:
            if os.path.exists(tmp):os.unlink(tmp)

def safe_write_new(root,relative,data):
    """Generated artifact materialization only; native FS owns ordinary editing."""
    require_posix_filesystem()
    p=relpath(relative);root=no_links(Path(root));fds=[]
    try:
        fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW);fds.append(fd)
        for component in p.parts[:-1]:
            try:os.mkdir(component,mode=0o755,dir_fd=fd)
            except FileExistsError:pass
            fd=os.open(component,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd);fds.append(fd)
        fd=os.open(p.parts[-1],os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o644,dir_fd=fd);fds.append(fd)
        view=memoryview(data)
        while view:view=view[os.write(fd,view):]
        os.fsync(fd)
    except FileExistsError:fail('REVISION_CONFLICT','Destination already exists',path=relative)
    finally:
        for fd in reversed(fds):os.close(fd)
    return {'path':relative,'bytes':len(data),'sha256':sha(data)}
