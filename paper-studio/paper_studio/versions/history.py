"""Immutable scientific version manifests; native Git remains available separately."""
from pathlib import Path
import difflib,json,re,shutil,time,uuid
from paper_studio.security.policy import *
from paper_studio.provider.jobs import job_path,read_status

def version_path(cfg,label):
    if not isinstance(label,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}',label) or label in ['.','..']:fail('INVALID_ARGUMENT','Invalid version label')
    if label in cfg.get('execution_policy',{}).get('retired_version_labels',[]):
        fail('VERSION_RETIRED','This project version was retired and its label cannot be reused',version=label)
    return Path(cfg['artifact_root'])/'versions'/label

def _read_version_manifest(cfg,label):
    p=version_path(cfg,label);no_links(p)
    if not (p/'manifest.json').is_file():fail('NOT_FOUND','Version not found')
    raw=safe_read(p,'manifest.json',8*1024*1024)
    try:m=json.loads(raw)
    except (ValueError,RecursionError):fail('VERSION_INVALID','Version manifest is not valid JSON')
    if not isinstance(m,dict):fail('VERSION_INVALID','Version manifest must be an object')
    if m.get('schema')!='paper-studio.version.v1' or m.get('project_id')!=cfg['project_id'] or m.get('version')!=label:
        fail('PROJECT_DENIED','Version identity does not belong to current project')
    if not isinstance(m.get('files'),list):fail('VERSION_INVALID','Version manifest has no file inventory')
    return p,m,sha(raw)

def verify_version(cfg,label):
    p,m,_=_read_version_manifest(cfg,label)
    for f in m['files']:
        raw=safe_read(p,f['path'],128*1024*1024)
        if sha(raw)!=f['sha256']:fail('VERSION_TAMPERED','Version hash mismatch',path=f['path'])
    return p,m

def list_versions(cfg,verification='full',version=None):
    """Full verification stays the default; manifest discovery reads no members."""
    if verification not in ['full','manifest']:fail('INVALID_ARGUMENT','Unknown version verification mode')
    root=Path(cfg['artifact_root'])/'versions';items=[];failed=[]
    if version is not None:
        selected=[version_path(cfg,version)]
    else:
        selected=sorted(root.iterdir()) if root.exists() else []
    for p in selected:
        if p.name.startswith('.'):continue
        if p.name in cfg.get('execution_policy',{}).get('retired_version_labels',[]):continue
        try:
            if verification=='full':
                _,m=verify_version(cfg,p.name)
                manifest_sha=file_sha(p/'manifest.json')
            else:
                _,m,manifest_sha=_read_version_manifest(cfg,p.name)
            fields=['version','created_at','build_ids','changelog','source_snapshot_sha256']
            if any(k not in m for k in fields):fail('VERSION_INVALID','Version manifest metadata is incomplete')
        except StudioError as error:
            failed.append({'version':p.name,'verified':False,'error':{'code':error.code,'message':error.message,
                           **({'path':error.details['path']} if 'path' in error.details else {})}})
            continue
        item={**{k:m[k] for k in fields},'manifest_sha256':manifest_sha,'verified':verification=='full'}
        if verification=='manifest':item.update(verification='manifest',members_verified=False)
        items.append(item)
    result={'project_id':cfg['project_id'],'versions':items}
    if verification=='manifest':result.update(verification='manifest',members_verified=False)
    if failed:result['integrity_failures']=failed
    return result

def save_version(cfg,label,build_ids,changelog,*,extra_files=None):
    version_path(cfg,label)
    if cfg.get('version_management') and (Path(cfg['project_root'])/cfg['version_management']['root']/label).exists() and not extra_files:
        fail('MANAGED_PROMOTION_REQUIRED','Use managed promote for a working version; direct save cannot bypass revision and review checks')
    if not isinstance(build_ids,list) or not 1<=len(build_ids)<=16:fail('INVALID_ARGUMENT','1..16 completed build IDs required')
    if not isinstance(changelog,str) or not 1<=len(changelog)<=16000:fail('INVALID_ARGUMENT','Nonempty bounded changelog required')
    if extra_files is None:extra_files={}
    if not isinstance(extra_files,dict):fail('INVALID_ARGUMENT','Extra version members must be a path-to-bytes mapping')
    for relative,raw in extra_files.items():
        path=relpath(relative)
        if relative!='VERSION.json' and (len(path.parts)<2 or path.parts[0] not in {'release','revisions','figures','dependencies'}):
            fail('INVALID_ARGUMENT','Extra version members must be managed release, revision, figure or dependency records',path=relative)
        if not isinstance(raw,bytes):fail('INVALID_ARGUMENT','Extra version member must contain bytes',path=relative)
        if len(raw)>128*1024*1024:fail('OUTPUT_LIMIT','Extra version member exceeds 128 MiB',path=relative)
    for relative in extra_files:
        if any(parent.as_posix() in extra_files for parent in Path(relative).parents if parent.as_posix()!='.'):
            fail('INVALID_ARGUMENT','Extra version member conflicts with a parent file',path=relative)
    dest=version_path(cfg,label)
    with locked(cfg,'versions'):
        if dest.exists():fail('VERSION_EXISTS','Published version cannot be overwritten')
        dest.parent.mkdir(parents=True,exist_ok=True);stage=dest.parent/('.stage-'+uuid.uuid4().hex);stage.mkdir()
        total=0;current_proofs={}
        def write(relative,raw):
            nonlocal total
            total+=len(raw)
            if total>256*1024*1024:fail('OUTPUT_LIMIT','Version snapshot exceeds 256 MiB')
            q=stage/relative;q.parent.mkdir(parents=True,exist_ok=True)
            with q.open('xb') as stream:stream.write(raw)
        try:
            for bid in build_ids:
                read_status_result=read_status(cfg,bid)
                if read_status_result['status']!='completed':fail('BUILD_NOT_COMPLETE','Version requires completed builds',build_id=bid)
                from paper_studio.versions.current_inputs import verify_build_current
                current_proofs[bid]=verify_build_current(cfg,bid)
                source=job_path(cfg,bid)
                try:
                    request_raw=safe_read(source,'request.json',4*1024*1024)
                except StudioError as error:
                    if error.code!='NOT_FOUND' or read_status_result.get('snapshot_sha256'):
                        raise
                    # Historical output-only jobs had no request document.
                    request_raw=None
                record=json.loads(request_raw) if request_raw is not None else {}
                if record.get('kind')=='tex':
                    from paper_studio.execution import verify_material_snapshot
                    verify_material_snapshot(cfg,record)
                snapshot=record.get('snapshot')
                if snapshot is None and (record.get('schema') or read_status_result.get('snapshot_sha256')):
                    fail('SNAPSHOT_TAMPERED','Recorded input snapshot is missing')
                indexed={'output':{a['path']:a for a in read_status_result['artifacts']}}
                if snapshot is not None:
                    rows=snapshot['files']
                    if sha(encoded(rows))!=snapshot['sha256'] or read_status_result.get('snapshot_sha256',snapshot['sha256'])!=snapshot['sha256']:
                        fail('SNAPSHOT_TAMPERED','Recorded input manifest has changed')
                    indexed['source']={row['path']:row for row in rows}
                for folder in ['source','output']:
                    copied=set()
                    for p in sorted((source/folder).rglob('*')):
                        if p.is_file():
                            name=p.relative_to(source/folder).as_posix()
                            if folder=='output' and name not in indexed[folder]:continue
                            rel=p.relative_to(source).as_posix();raw=safe_read(source,rel,128*1024*1024)
                            if folder in indexed and sha(raw)!=indexed[folder].get(name,{}).get('sha256'):
                                fail('SNAPSHOT_TAMPERED','Archive member does not match its build record',path=rel)
                            copied.add(name)
                            write('builds/'+bid+'/'+rel,raw)
                    if folder in indexed and copied!=set(indexed[folder]):
                        fail('SNAPSHOT_TAMPERED','Build archive member is missing',kind=folder)
                if request_raw is not None:
                    write('builds/'+bid+'/request.json',request_raw)
                for name in ['manifest.json','worker.log']:
                    p=source/name
                    if p.is_file():write('builds/'+bid+'/'+name,safe_read(source,name,2*1024*1024))
            write('CHANGELOG.md',(changelog+'\n').encode('utf-8'))
            for relative,raw in sorted(extra_files.items()):write(relative,raw)
            files=[{'path':p.relative_to(stage).as_posix(),'bytes':p.stat().st_size,'sha256':file_sha(p)} for p in sorted(stage.rglob('*')) if p.is_file()]
            m={'schema':'paper-studio.version.v1','project_id':cfg['project_id'],'version':label,'created_at':time.time(),'build_ids':build_ids,'changelog':changelog,'files':files,'source_snapshot_sha256':sha(encoded(files)),'config_revision':cfg['_config_revision'],'cloud_publication':'not_requested','web_visual_acceptance':'not_verified'}
            scope='manuscript' if any(proof['formal_manuscript'] for proof in current_proofs.values()) else 'auxiliary'
            m['publication_scope']=scope
            atomic_json(stage/'manifest.json',m,True)
            # Capture and copying can take time. Recheck before committing the
            # immutable publication so concurrent source edits cannot be signed.
            for bid in build_ids:verify_build_current(cfg,bid)
            stage.rename(dest)
            return {'version':label,'manifest_sha256':file_sha(dest/'manifest.json'),'files':len(files),'bytes':total,'immutable':True,'build_ids':build_ids,'publication_scope':scope}
        finally:
            if stage.exists():shutil.rmtree(stage)

def compare_versions(cfg,left,right):
    lp,lm=verify_version(cfg,left);rp,rm=verify_version(cfg,right)
    a,b=_version_entries(lp,lm),_version_entries(rp,rm)
    changes=[];output_changes=[];entries=[]
    for entry in sorted(set(a)|set(b)):
        x,y=a.get(entry),b.get(entry)
        source_changes=_file_changes(lp,rp,x.get('source',{}) if x else {},y.get('source',{}) if y else {},left,right,entry,True)
        outputs=_file_changes(lp,rp,x.get('output',{}) if x else {},y.get('output',{}) if y else {},left,right,entry,False)
        changes.extend(source_changes);output_changes.extend(outputs)
        before=_entry_summary(lp,x) if x else None;after=_entry_summary(rp,y) if y else None
        entries.append({'entry':entry,'before':before,'after':after,
                        'total_changes':len(source_changes),'total_output_changes':len(outputs)})
    result={'left':left,'right':right,'changes':changes[:200],'total_changes':len(changes),
            'output_changes':output_changes[:200],'total_output_changes':len(output_changes),'entries':entries,
            'pdf_comparison':'verified byte identities and recorded page counts; actual visual review is separate',
            'compare_scope':'all selected build entry identities; the last selected build for the same entry wins',
            'semantic_comparison':'literal TeX signatures only; scientific equivalence and claim support are not inferred'}
    managed=_managed_comparison(_managed_record(lp,lm),_managed_record(rp,rm))
    if managed:result['managed']=managed
    return result


def _managed_record(root,manifest):
    """Release-level structure, figure slots and dependency choices of a managed archive."""
    members={f['path'] for f in manifest['files']}
    if 'release/manifest.json' not in members:return None
    release=json.loads(safe_read(root,'release/manifest.json',8*1024*1024))
    revision={}
    path='revisions/'+str(release.get('revision'))+'/manifest.json'
    if path in members:revision=json.loads(safe_read(root,path,8*1024*1024))
    return {'revision':release.get('revision'),
            'structure':release.get('structure') or revision.get('structure') or 'trusted_configuration_baseline',
            'figures':{fig:{'output_path':item.get('output_path'),'candidate':item.get('candidate'),
                            'sha256':(item.get('output') or {}).get('sha256')} for fig,item in sorted((release.get('figures') or {}).items())},
            'dependencies':release.get('dependencies') or {},
            'input_changes':release.get('input_changes')}


def _managed_comparison(left,right):
    if left is None and right is None:return None
    a,b=left or {},right or {}
    figures=[]
    for fig in sorted(set(a.get('figures',{}))|set(b.get('figures',{}))):
        x,y=a.get('figures',{}).get(fig),b.get('figures',{}).get(fig)
        if x!=y:figures.append({'figure_id':fig,'change':'added' if not x else 'removed' if not y else 'modified','before':x,'after':y})
    dependencies=[]
    for path in sorted(set(a.get('dependencies',{}))|set(b.get('dependencies',{}))):
        x,y=a.get('dependencies',{}).get(path),b.get('dependencies',{}).get(path)
        if x!=y:dependencies.append({'path':path,'before':x,'after':y})
    return {'left':left,'right':right,'structure_changed':a.get('structure')!=b.get('structure'),
            'figure_slot_changes':figures,'dependency_selection_changes':dependencies,
            'scope':'release records only; source byte differences are listed in changes'}


def _version_entries(root,manifest):
    """Resolve builds by manuscript identity rather than transient rb IDs."""
    members={f['path']:f for f in manifest['files']};entries={}
    def document(path):
        if path not in members:return {}
        return json.loads(safe_read(root,path,4*1024*1024))
    for bid in manifest['build_ids']:
        base='builds/'+bid+'/'
        record=document(base+'request.json');request=record.get('request',record)
        source_prefix=base+'source/';output_prefix=base+'output/'
        sources={f['path'][len(source_prefix):]:f for f in manifest['files'] if f['path'].startswith(source_prefix)}
        outputs={f['path'][len(output_prefix):]:f for f in manifest['files'] if f['path'].startswith(output_prefix)}
        tex=request.get('tex_path')
        if not tex:
            candidates=[p for p in sources if p.endswith('.tex')]
            mains=[p for p in candidates if Path(p).name.startswith('main')]
            tex=mains[0] if len(mains)==1 else candidates[0] if len(candidates)==1 else None
        language=None
        if tex:
            match=re.search(r'(?:^|[/_.-])(en|zh)(?=[/_.-]|$)',tex)
            if match:language=match.group(1)
        if language:identity=language
        elif tex:identity='tex:'+tex
        else:identity=str(record.get('kind','build'))+':'+str(request.get('source_path',request.get('pptx_path','unspecified')))
        # The logical main slot permits candidate -> canonical main_en.tex
        # transitions while keeping the archived physical path in each record.
        logical_sources=dict(sources)
        if tex in logical_sources:logical_sources['@main.tex']=logical_sources.pop(tex)
        logical_outputs={}
        for path,member in outputs.items():
            key='@main.pdf' if tex and path==str(Path(tex).with_suffix('.pdf').name) else path
            logical_outputs[key]=member
        entries[identity]={'build_id':bid,'tex_path':tex,'language':language,'source':logical_sources,
                           'output':logical_outputs,'manifest':document(base+'manifest.json')}
    return entries


def _file_changes(lp,rp,left,right,left_label,right_label,entry,text_diff):
    changes=[]
    for path in sorted(set(left)|set(right)):
        x,y=left.get(path),right.get(path)
        if x and y and x['sha256']==y['sha256']:continue
        display=path
        if path in {'@main.tex','@main.pdf'}:
            selected=y or x;display=selected['path'].split('/source/',1)[-1].split('/output/',1)[-1]
        rec={'path':display,'entry':entry,'change':'added' if not x else 'removed' if not y else 'modified',
             'before_sha256':x['sha256'] if x else None,'after_sha256':y['sha256'] if y else None,
             'before_path':x['path'] if x else None,'after_path':y['path'] if y else None,
             'before_bytes':x['bytes'] if x else None,'after_bytes':y['bytes'] if y else None}
        if text_diff and Path(path).suffix in ['.tex','.bib','.py','.md','.csv','.json','.svg']:
            before=safe_read(lp,x['path'],262144).decode('utf-8',errors='replace') if x and x['bytes']<262144 else ''
            after=safe_read(rp,y['path'],262144).decode('utf-8',errors='replace') if y and y['bytes']<262144 else ''
            rec['diff']=''.join(difflib.unified_diff(before.splitlines(True),after.splitlines(True),fromfile=left_label+'/'+display,tofile=right_label+'/'+display))[:16000]
            if x and x['bytes']>=262144 or y and y['bytes']>=262144:rec['diff_truncated']=True
        changes.append(rec)
    return changes


def _entry_summary(root,entry):
    main=entry['source'].get('@main.tex');signatures=None
    if main:
        text=safe_read(root,main['path'],128*1024*1024).decode('utf-8',errors='replace')
        text=re.sub(r'(?<!\\)%[^\n]*','',text)
        formulas=re.findall(r'\\begin\{(equation\*?|align\*?|gather\*?|multline\*?|displaymath)\}(.*?)\\end\{\1\}',text,re.S)
        displays=re.findall(r'\\\[(.*?)\\\]|\$\$(.*?)\$\$',text,re.S)
        inline=re.findall(r'\\\((.*?)\\\)|(?<![\\$])\$(?!\$)(.*?)(?<!\\)\$(?!\$)',text,re.S)
        tables=re.findall(r'\\begin\{(table\*?)\}(.*?)\\end\{\1\}',text,re.S)
        citations=re.findall(r'\\cite\w*\*?(?:\[[^\]]*\]){0,2}\s*\{([^}]*)\}',text)
        keys=sorted(set(k.strip() for group in citations for k in group.split(',') if k.strip()))
        signatures={'equations':{'count':len(formulas)+len(displays),'sha256':sha(encoded([formulas,displays]))},
                    'inline_math':{'count':len(inline),'sha256':sha(encoded(inline))},
                    'tables':{'count':len(tables),'sha256':sha(encoded(tables))},
                    'citations':{'commands':len(citations),'keys':keys,'sha256':sha(encoded(citations))}}
    result=entry['manifest'].get('result',{});pages=result.get('pages')
    if not isinstance(pages,int) or isinstance(pages,bool):pages=None
    return {'build_id':entry['build_id'],'tex_path':entry['tex_path'],'language':entry['language'],
            'main_sha256':main['sha256'] if main else None,'tex_signatures':signatures,'pdf_pages':pages,
            'figures':[{'path':path,'sha256':f['sha256']} for path,f in sorted(entry['source'].items())
                       if Path(path).suffix.lower() in {'.pdf','.png','.jpg','.jpeg','.svg','.eps','.pptx'}],
            'pdfs':[{'path':path,'sha256':f['sha256'],'bytes':f['bytes']} for path,f in sorted(entry['output'].items())
                    if Path(path).suffix.lower()=='.pdf']}

def derive_version(cfg,label,destination,source_prefix='candidates'):
    root,m=verify_version(cfg,label);relpath(destination);relpath(source_prefix)
    path_for(cfg,destination+'/manifest.json',True)
    dest=Path(cfg['project_root'])/destination
    with locked(cfg):
        if dest.exists():fail('REVISION_CONFLICT','Derived candidate directory already exists')
        prefix='builds/'+m['build_ids'][-1]+'/source/'+source_prefix+'/'
        selected=[f for f in m['files'] if f['path'].startswith(prefix)]
        if not selected:fail('NOT_FOUND','Version has no sources under source_prefix')
        # Check every target before creating any member: a source may include
        # a readonly nested builds tree even when destination itself is writable.
        for f in selected:path_for(cfg,destination+'/'+f['path'][len(prefix):],True)
        for f in selected:
            raw=safe_read(root,f['path'],128*1024*1024)
            safe_write_new(cfg['project_root'],destination+'/'+f['path'][len(prefix):],raw)
        return {'from_version':label,'destination':destination,'source_prefix':source_prefix,'files':len(selected),'old_version_unchanged':True}
