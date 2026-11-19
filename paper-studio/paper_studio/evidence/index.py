"""Bounded material indexing; file contents do not become scientific facts."""
from pathlib import Path
import csv,io,json,mimetypes,os,re
from paper_studio.security.policy import artifact_project_relative,fail,path_for,project_description,relpath,safe_read,sha,encoded
KINDS={'.md':'research_note','.csv':'tabular_data','.xlsx':'spreadsheet','.tsv':'tabular_data','.py':'python_source','.m':'matlab_source','.bib':'bibliography','.pdf':'pdf','.tex':'manuscript','.pptx':'presentation','.svg':'vector_figure','.png':'image','.jpg':'image','.jpeg':'image'}
FACT_TYPES=['measured_fact','verified_experiment','author_confirmed_decision','design_proposal','historical_draft','unverified_hypothesis']
CATALOG_PATH = 'materials/INDEX_CATALOG.json'


def catalog_inventory(cfg):
    """Read the maintained navigation files without enumerating the workspace."""
    root = cfg['project_root']
    if not path_for(cfg, CATALOG_PATH).exists():
        return None, None
    raw = safe_read(root, CATALOG_PATH, 64 * 1024)
    try:
        catalog = json.loads(raw)
    except (ValueError, RecursionError):
        fail('CATALOG_INVALID', 'Invalid material index JSON', path=CATALOG_PATH)
    if not isinstance(catalog, dict) or catalog.get('schema') != 'paper-editor.index-catalog.v1':
        fail('CATALOG_INVALID', 'Unsupported material index schema', path=CATALOG_PATH)
    if catalog.get('project_id') != cfg['project_id']:
        fail('CATALOG_INVALID', 'Material index belongs to a different project', path=CATALOG_PATH)
    roles = catalog.get('roles')
    maps = catalog.get('supporting_maps', [])
    if not isinstance(roles, (list, dict)) or not roles or not isinstance(maps, list):
        fail('CATALOG_INVALID', 'Expected index roles and a list of supporting paths')

    paths = []
    if isinstance(roles, dict):
        entries = roles.items()
    else:
        entries = [(row.get('role') if isinstance(row, dict) else None, row) for row in roles]
    for role, row in entries:
        if not isinstance(role, str) or not role.strip() or not isinstance(row, dict):
            fail('CATALOG_INVALID', 'Each material index entry needs a role and a path')
        paths.append(row.get('path'))
        if 'supplement' in row:
            paths.append(row['supplement'])

    records = {}
    artifact_tree = artifact_project_relative(cfg)
    # Use the context discovery byte budget across the listed files.
    remaining_bytes = 16 * 1024 * 1024
    for relative in [*paths, *maps]:
        path = relpath(relative)
        if artifact_tree and path.is_relative_to(artifact_tree):
            fail('PATH_DENIED', 'Generated artifacts cannot serve as material indexes', path=relative)
        if relative in records:
            continue
        if len(records) >= 200:
            fail('INPUT_LIMIT', 'Material index exceeds the context entry limit')
        content = safe_read(root, relative, remaining_bytes)
        remaining_bytes -= len(content)
        records[relative] = {
            'path': relative, 'kind': KINDS.get(path.suffix.lower(), 'other'),
            'bytes': len(content), 'sha256': sha(content), 'hash_status': 'computed',
            'scientific_status': 'unclassified', 'source': 'local_file',
        }
    files = list(records.values())
    inventory = {
        'project_id': cfg['project_id'], 'scope': 'material_catalog', 'files': files,
        'total': len(files), 'indexed_files': len(files), 'total_known': True,
        'next_offset': None, 'truncated': False, 'excluded': [], 'facts_inferred': False,
        'inventory_sha256': sha(encoded(files)),
    }
    return {**catalog, 'path': CATALOG_PATH, 'sha256': sha(raw)}, inventory

def inventory(cfg,offset=0,limit=100,exclude_roots=(),allow_partial=False):
    if not isinstance(offset,int) or offset<0 or not isinstance(limit,int) or not 1<=limit<=200:fail('INVALID_ARGUMENT','offset>=0 and limit 1..200 required')
    root=Path(cfg['project_root']);records=[];excluded=[];max_files=cfg.get('limits',{}).get('max_files',2048);truncated=False;hashed_bytes=0
    hash_budget=16*1024*1024 if allow_partial else None
    artifact_tree=artifact_project_relative(cfg)
    exclude_roots=(*exclude_roots,*([artifact_tree] if artifact_tree else []))
    for parent,dirs,files in os.walk(root,followlinks=False):
        accepted=[]
        for n in sorted(dirs):
            p=Path(parent)/n;r=p.relative_to(root).as_posix()
            if any(Path(r).is_relative_to(excluded) for excluded in exclude_roots):continue
            try:relpath(r)
            except Exception:continue
            if not p.is_symlink():accepted.append(n)
            else:excluded.append({'path':r,'reason':'symlink'})
        dirs[:]=accepted
        for n in sorted(files):
            p=Path(parent)/n;r=p.relative_to(root).as_posix()
            try:relpath(r)
            except Exception:excluded.append({'path':r,'reason':'protected_name'});continue
            if len(records)>=max_files:
                if not allow_partial:fail('INPUT_LIMIT','Project inventory exceeds file limit; narrow the registered workspace')
                truncated=True;break
            if p.is_symlink() or not p.is_file() or p.stat().st_nlink!=1:excluded.append({'path':r,'reason':'nonregular_or_link'});continue
            size=p.stat().st_size
            # Large research files can be indexed by metadata; their hash is explicitly unknown.
            can_hash=size<=64*1024*1024 and (hash_budget is None or hashed_bytes+size<=hash_budget)
            raw=safe_read(root,r,64*1024*1024) if can_hash else None
            if raw is not None:hashed_bytes+=len(raw)
            rec={'path':r,'kind':KINDS.get(p.suffix.lower(),'other'),'bytes':size,'sha256':sha(raw) if raw is not None else None,'hash_status':'computed' if raw is not None else ('not_read_context_budget' if hash_budget is not None and size<=64*1024*1024 else 'not_read_size_limit'),'scientific_status':'unclassified','source':'local_file'}
            if p.suffix.lower()=='.csv' and raw is not None and len(raw)<=2*1024*1024:
                try:
                    rows=list(csv.reader(io.StringIO(raw.decode('utf-8-sig'))));rec.update(columns=rows[0] if rows else [],data_rows=max(0,len(rows)-1))
                except (UnicodeError,csv.Error):rec['parse_status']='unreadable_csv'
            if p.suffix.lower()=='.bib' and raw is not None:
                rec['citation_keys']=re.findall(r'@\w+\s*\{\s*([^,\s]+)',raw.decode('utf-8',errors='replace'))
            records.append(rec)
        if truncated:break
    records.sort(key=lambda r:r['path'])
    return {'project_id':cfg['project_id'],'files':records[offset:offset+limit],'total':None if truncated else len(records),'indexed_files':len(records),'total_known':not truncated,'truncated':truncated,'next_offset':offset+limit if offset+limit<len(records) else None,'excluded':excluded[:100],'inventory_sha256':sha(encoded(records)),'facts_inferred':False}

def context(cfg):
    from paper_studio.gateway.usage import GUIDANCE
    from paper_studio.research_state import continuation_summary
    # Context discovery must work before a full inventory, even when materials
    # or historical review trees exceed the execution snapshot file budget.
    catalog, inv = catalog_inventory(cfg)
    if catalog is None:
        discovery={**cfg,'limits':{**cfg.get('limits',{}),'max_files':min(400,cfg.get('limits',{}).get('max_files',2048))}}
        inv=inventory(discovery,0,200,allow_partial=True)
    facts=[]
    entrypoints=[]
    for relative in ['PROJECT.yaml','PROJECT_STATE.json','README.md','HANDOFF.md','CHAT_POLISH_PROMPT.md',
                     CATALOG_PATH,'materials/evidence_registry.csv','references/reference_index.json',
                     'sync/handoff-index.json']:
        path=Path(cfg['project_root'])/relative
        if path.is_file() and not path.is_symlink():entrypoints.append(relative)
    from paper_studio.bootstrap.handoff import discover_handoff
    from paper_studio.bootstrap.workspace import BootstrapError
    try:material_handoff=discover_handoff(cfg['project_root'],cfg['project_id'])
    except BootstrapError as error:
        material_handoff={'status':'invalid','error_code':error.code,'index_relative_path':'sync/handoff-index.json',
                          'scientific_acceptance':'not_inferred','guidance':'Retain the invalid pack and inspect its hash-bound index; do not silently accept it.'}
    path=Path(cfg['project_root'])/'candidates/evidence.json'
    if path.exists():
        obj=json.loads(safe_read(cfg['project_root'],'candidates/evidence.json',2*1024*1024));facts=obj.get('facts',[])
        for f in facts:
            if f.get('kind') not in FACT_TYPES:fail('EVIDENCE_INVALID','Unknown fact classification')
            if not isinstance(f.get('sources'),list) or not f['sources']:fail('EVIDENCE_INVALID','Facts must carry source path and SHA256')
            for src in f['sources']:
                raw=safe_read(cfg['project_root'],src['path'],64*1024*1024)
                if sha(raw)!=src.get('sha256'):fail('EVIDENCE_STALE','Fact source revision changed',fact_id=f.get('id'))
    gaps=[] if facts else ['No author-verified fact registry exists. Scanned file contents are not automatically verified results.']
    if material_handoff and material_handoff.get('status')=='invalid':gaps.append('The latest material handoff failed integrity verification: '+material_handoff['error_code'])
    if inv['truncated']:gaps.append('Context inventory is partial; read the named entrypoints and latest material handoff directly. Pagination cannot establish an unbounded complete scan.')
    return {**{key:project_description(cfg)[key] for key in ['artifact_root','artifact_layout','build_artifact_store']},'project_id':cfg['project_id'],'config_revision':cfg['_config_revision'],'entrypoints':entrypoints,'continuation':continuation_summary(cfg),'latest_material_handoff':material_handoff,'material_catalog':catalog,'inventory':inv,'facts':facts,'fact_types':FACT_TYPES,'conflicts_and_gaps':gaps,'writing_precedence':['current_official_venue_requirements','confirmed_facts_and_author_decisions','project_rules','SCI_common_rules','actually_read_examples'],'guidance':'Read the listed project entrypoints and latest_material_handoff before inferring facts. A material handoff contains pinned source evidence and author statements, not automatic scientific acceptance. Create candidates/evidence.json with facts[{id,kind,statement,sources:[{path,sha256}],verified_by,verification_date}] only when explicitly accepted author evidence exists.','tool_guidance':GUIDANCE}
