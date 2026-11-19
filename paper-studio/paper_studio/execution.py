"""Optional project-scoped trusted entry points over the existing job engines.

No commands or environment fragments are accepted from the caller. Projects
without this policy retain their existing Paper Studio behavior.
"""
from pathlib import PurePosixPath
import json,re
from paper_studio.security.policy import fail, relpath, path_for, safe_read, sha, encoded


def target_recipe(target):
    """Fixed maintainer-selected engines; clients cannot supply executable text."""
    engine=target.get('build_profile','xelatex')
    bibliography=target.get('bibliography_engine','bibtex')
    latexmk=target.get('use_latexmk',False)
    if not isinstance(engine,str) or engine not in {'pdflatex','xelatex','lualatex'}:
        fail('CONFIG_INVALID','Unsupported trusted TeX engine')
    if not isinstance(bibliography,str) or bibliography not in {'auto','bibtex','biber'}:
        fail('CONFIG_INVALID','Unsupported trusted bibliography engine')
    if type(latexmk) is not bool:
        fail('CONFIG_INVALID','Trusted use_latexmk must be boolean')
    if target.get('compatibility_hooks') and engine!='xelatex':
        fail('CONFIG_INVALID','The fixed IEEEtran compatibility hook requires XeLaTeX')
    return {'engine':engine,'bibliography_engine':bibliography,'use_latexmk':latexmk}


def validate_policy(cfg):
    value=cfg['execution_policy']
    required={'tex_targets','figure_root','figure_map','expected_figures','expected_tables','render_tex_previews'}
    if not isinstance(value,dict) or set(value)-{'allow_text_reviews','text_review_targets','retired_version_labels'}!=required:fail('CONFIG_INVALID','Exact execution policy fields required')
    retired=value.get('retired_version_labels',[])
    if (not isinstance(retired,list) or any(not isinstance(label,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}',label) for label in retired)
            or len(retired)!=len(set(retired))):
        fail('CONFIG_INVALID','Retired versions require unique valid version labels')
    if 'allow_text_reviews' in value and type(value['allow_text_reviews']) is not bool:
        fail('CONFIG_INVALID','Text review entrypoints require an explicit boolean policy')
    targets=value['tex_targets']
    minimum = 0 if cfg.get('chapter_workflow') else 1
    if not isinstance(targets,dict) or not minimum<=len(targets)<=8:fail('CONFIG_INVALID','Registered TeX targets or an explicit chapter workflow are required')
    seen=set()
    for name,target in targets.items():
        if (not re.fullmatch('[A-Za-z0-9_-]{1,64}',name) or not isinstance(target,dict)
                or not {'tex_path','working_directory','tex_search_paths'}.issubset(target)
                or set(target)-{'tex_path','working_directory','tex_search_paths','compatibility_hooks',
                                'build_profile','bibliography_engine','use_latexmk','expected_figures','expected_tables','material_binding',
                                'structure_scope','preview_pages','preview_page_ranges','language'}):
            fail('CONFIG_INVALID','Invalid trusted TeX target')
        if 'language' in target and target['language'] not in ('en','zh'):
            fail('CONFIG_INVALID','Trusted TeX target language must be en or zh')
        if target.get('structure_scope','main') not in {'main','reachable_tex'}:
            fail('CONFIG_INVALID','Trusted structure_scope must be main or reachable_tex')
        from paper_studio.pdf.selection import validate_selection
        try:validate_selection({k.removeprefix('preview_'):target[k] for k in ['preview_pages','preview_page_ranges'] if k in target})
        except ValueError as error:fail('CONFIG_INVALID',str(error))
        for field in ['expected_figures','expected_tables']:
            if field in target and (type(target[field]) is not int or not 0<=target[field]<=100):
                fail('CONFIG_INVALID','Bounded manuscript structure counts required')
        if target.get('compatibility_hooks',[]) not in ([],['ieeetran_xetex_dropcap_v1']):
            fail('CONFIG_INVALID','Only the fixed IEEEtran XeTeX compatibility hook is registered')
        target_recipe(target)
        if 'material_binding' in target:
            validate_material_binding(target['material_binding'])
        relpath(target['tex_path'])
        if not target['tex_path'].endswith('.tex') or target['tex_path'] in seen:fail('CONFIG_INVALID','TeX targets must be unique')
        seen.add(target['tex_path'])
        if target['working_directory']!='.':relpath(target['working_directory'])
        dirs=target['tex_search_paths']
        if not isinstance(dirs,list) or len(dirs)>16:fail('CONFIG_INVALID','Bounded TeX search directories required')
        for directory in dirs:relpath(directory)
    relpath(value['figure_root']);relpath(value['figure_map'])
    for name in ['expected_figures','expected_tables']:
        if type(value[name]) is not int or not 0<=value[name]<=100:fail('CONFIG_INVALID','Bounded manuscript structure counts required')
    if type(value['render_tex_previews']) is not bool:fail('CONFIG_INVALID','Explicit preview policy required')
    from paper_studio.latex.review_snapshot import validate_targets
    validate_targets(cfg)


def validate_material_binding(binding):
    if (not isinstance(binding,dict) or set(binding)!={'manifest_path','sha256'}
            or not isinstance(binding.get('sha256'),str)
            or not re.fullmatch('[0-9a-f]{64}',binding['sha256'])):
        fail('CONFIG_INVALID','Trusted manuscript material binding requires exact manifest_path and SHA256')
    relpath(binding['manifest_path'])


def material_provenance(cfg,binding):
    """Verify an explicitly selected indexed revision, including historical ones."""
    validate_material_binding(binding)
    from paper_studio.bootstrap.handoff import verify_indexed_handoff
    from paper_studio.bootstrap.workspace import BootstrapError
    from pathlib import PurePosixPath
    try:
        manifest=verify_indexed_handoff(cfg['project_root'],cfg['project_id'],binding['manifest_path'],binding['sha256'])
    except BootstrapError as error:
        fail(error.code,str(error))
    parent=PurePosixPath(binding['manifest_path']).parent
    expected={binding['manifest_path']:binding['sha256'],(parent/'HANDOFF.md').as_posix():manifest['handoff_sha256']}
    for source in manifest['sources']:
        relpath(source['snapshot_path'])
        expected[(parent/source['snapshot_path']).as_posix()]=source['sha256']
    for path,digest in expected.items():
        if sha(safe_read(cfg['project_root'],path,16*1024**2))!=digest:
            fail('EVIDENCE_STALE','Trusted manuscript material member changed',path=path)
    return expected


def verify_material_snapshot(cfg,record):
    """An archive must contain the exact material proof selected at submission."""
    binding=record.get('request',{}).get('_material_binding')
    if binding is None:
        source=record.get('request',{}).get('tex_path')
        configured=cfg.get('execution_policy',{}).get('tex_targets',{}).values()
        if any(target.get('tex_path')==source and target.get('material_binding') for target in configured):
            fail('MATERIAL_BINDING_REQUIRED','This manuscript archive requires its trusted material proof from submission')
        return
    expected=material_provenance(cfg,binding)
    rows={row['path']:row['sha256'] for row in record.get('snapshot',{}).get('files',[])}
    if any(rows.get(path)!=digest for path,digest in expected.items()):
        fail('SNAPSHOT_TAMPERED','Build snapshot omitted or changed its trusted manuscript material proof')


def target_for(cfg,tex_path):
    policy=cfg.get('execution_policy')
    if not policy:fail('BUILD_TARGETS_NOT_CONFIGURED','No trusted current manuscript targets registered')
    from paper_studio.latex.review_snapshot import selected
    review_selection = selected(cfg, tex_path)
    if review_selection is not None:
        target = policy['tex_targets'][review_selection['target']]
        return review_selection['target']+'-text-review', {**target, 'tex_path':tex_path}
    for name,target in policy['tex_targets'].items():
        if target['tex_path']==tex_path:return name,target
    if (cfg.get('admission_profile')=='paper-editor.v4' and policy.get('allow_text_reviews') is True
            and isinstance(tex_path,str) and tex_path.endswith('.review.tex')):
        original=tex_path.removesuffix('.review.tex')+'.tex'
        for name,target in policy['tex_targets'].items():
            if target['tex_path']==original and not original.endswith('.review.tex'):
                path_for(cfg,tex_path,True)
                review={key:value for key,value in target.items() if key!='material_binding'}
                return name+'-text-review',{**review,'tex_path':tex_path,'_text_review':{'base_tex_path':original}}
    if cfg.get('chapter_workflow'):
        from paper_studio.chapters.workflow import trusted_target
        target = trusted_target(cfg, tex_path)
        if target is not None:
            return target
    if cfg.get('review_proposals',{}).get('enabled'):
        from paper_studio.versions.review_proposals import trusted_target
        target = trusted_target(cfg, tex_path)
        if target is not None:
            return target
    fail('BUILD_TARGET_DENIED','Only the explicitly registered current manuscript may be compiled')


def build_plan(cfg,tex_path):
    if tex_path in cfg.get('execution_policy',{}).get('text_review_targets',{}):
        from paper_studio.latex.review_snapshot import build_plan as review_plan
        return review_plan(cfg, tex_path)
    if cfg.get('version_management'):
        from paper_studio.versions import lifecycle
        if lifecycle.is_managed(cfg,tex_path):return lifecycle.build_plan(cfg,tex_path)
    name,target=target_for(cfg,tex_path)
    recipe=target_recipe(target)
    from paper_studio.latex.dependencies import plan_dependencies
    plan=plan_dependencies(cfg['project_root'],tex_path,working_directory=target['working_directory'],
                           search_paths=target['tex_search_paths'],texlive_root=cfg['toolchain']['texlive_root'],
                           windows_fonts_root=cfg['toolchain'].get('windows_fonts_root'),build_profile=recipe['engine'])
    if any(not isinstance(item,dict) or item.get('required',True) for item in plan.get('missing',[])) or any(item.get('required',True) for item in plan.get('unresolved',[])):
        fail('DEPENDENCY_UNRESOLVED','Required literal manuscript inputs could not be resolved',missing=plan.get('missing'),unresolved=plan.get('unresolved'))
    raw=safe_read(cfg['project_root'],tex_path,4*1024*1024)
    main_sha=sha(raw)
    main=next((item for item in plan['files'] if item['path']==tex_path),None)
    if main is None or main['sha256']!=main_sha:fail('REVISION_CONFLICT','Main source changed during dependency planning')
    # Registered counts are a historical comparison point. Current reachable
    # TeX is the structure authority for this build, including split sections.
    policy=cfg['execution_policy']
    expected={kind:target.get(field,policy[field]) for kind,field in [('figure','expected_figures'),('table','expected_tables')]}
    from paper_studio.latex.structure import manuscript_structure
    structure=manuscript_structure(cfg['project_root'],tex_path,plan,expected)
    counts=structure['counts']
    inputs={row['path']:row for row in plan['files']}
    material=target.get('material_binding')
    if material:
        for relative,digest in material_provenance(cfg,material).items():
            proof_raw=safe_read(cfg['project_root'],relative,16*1024**2)
            if sha(proof_raw)!=digest:fail('EVIDENCE_STALE','Manuscript material changed while planning',path=relative)
            inputs[relative]={'path':relative,'bytes':len(proof_raw),'sha256':digest}
    if cfg.get('chapter_workflow'):
        from paper_studio.chapters.workflow import provenance_paths
        for relative in provenance_paths(cfg,tex_path):
            if relative not in inputs:
                evidence=safe_read(cfg['project_root'], relative, 16*1024**2)
                inputs[relative]={'path':relative,'bytes':len(evidence),'sha256':sha(evidence)}
    if cfg.get('review_proposals',{}).get('enabled'):
        from paper_studio.versions.review_proposals import provenance_paths
        for relative in provenance_paths(cfg,tex_path):
            relpath(relative)
            if relative not in inputs:
                evidence=safe_read(cfg['project_root'],relative,cfg.get('limits',{}).get('max_snapshot_bytes',32*1024**2))
                inputs[relative]={'path':relative,'bytes':len(evidence),'sha256':sha(evidence)}
    records=sorted(inputs.values(),key=lambda row:row['path'])
    records=[{key:row[key] for key in ['path','bytes','sha256']} for row in records]
    return {**plan,'project_id':cfg['project_id'],'target':name,'tex_path':tex_path,
            'expected_revision':main_sha,
            'working_directory':target['working_directory'],'tex_search_paths':target['tex_search_paths'],
            'compatibility_hooks':target.get('compatibility_hooks',[]),
            'files':records,'input_paths':[row['path'] for row in records],
            'input_sha256':sha(encoded(records)),'counts':counts,**recipe,
            'structure_scope':'reachable_tex','structure_files':[row['path'] for row in structure['source_files']],
            'manuscript_structure':structure,
            'preview_selection':{k.removeprefix('preview_'):target[k] for k in ['preview_pages','preview_page_ranges'] if k in target},
            **({'material_binding':dict(material)} if material else {}),
            **({'_review_binding':dict(target['_review_binding'])} if '_review_binding' in target else {}),
            **({'_text_review':dict(target['_text_review']),'review_only':True} if '_text_review' in target else {}),
            'build_executed':False,'visual_review':'not_performed'}


def prepare_request(cfg,kind,request):
    value=dict(request);policy=cfg['execution_policy']
    if kind=='tex':
        plan=build_plan(cfg,value['tex_path'])
        for key,default in [('engine','xelatex'),('bibliography_engine','bibtex'),('use_latexmk',False)]:
            plan.setdefault(key,default)
        if (value.get('build_profile',plan['engine'])!=plan['engine']
                or value.get('use_latexmk',plan['use_latexmk']) is not plan['use_latexmk']):
            fail('BUILD_PROFILE_DENIED','Trusted current targets require the configured fixed TeX recipe',
                 engine=plan['engine'],bibliography_engine=plan['bibliography_engine'],use_latexmk=plan['use_latexmk'])
        if value['expected_revision']!=plan['expected_revision']:fail('REVISION_CONFLICT','Current main source differs from requested SHA256')
        if value.get('expected_input_sha256') is not None and value['expected_input_sha256']!=plan['input_sha256']:
            fail('REVISION_CONFLICT','Current dependency inputs differ from the requested SHA256')
        if value.get('input_paths') is not None and set(value['input_paths'])!=set(plan['input_paths']):
            fail('INPUT_DENIED','Input paths must exactly match the current dependency plan')
        value.update(input_paths=plan['input_paths'],working_directory=plan['working_directory'],
                     tex_search_paths=plan['tex_search_paths'],build_profile=plan['engine'],
                     bibliography_engine=plan['bibliography_engine'],use_latexmk=plan['use_latexmk'],
                     _input_manifest_sha256=plan['input_sha256'])
        value['_preview_selection']=plan.get('preview_selection',{})
        value.pop('_manuscript_structure',None)
        if 'manuscript_structure' in plan:value['_manuscript_structure']=plan['manuscript_structure']
        if plan['compatibility_hooks']:value['compatibility_hooks']=plan['compatibility_hooks']
        if 'material_binding' in plan:value['_material_binding']=plan['material_binding']
        value.pop('_version_binding',None)
        if '_version_binding' in plan:value['_version_binding']=plan['_version_binding']
        value.pop('_review_binding',None)
        if '_review_binding' in plan:value['_review_binding']=plan['_review_binding']
        value.pop('_text_review',None)
        if '_text_review' in plan:value['_text_review']=plan['_text_review']
    elif kind in {'figure','pptx'}:
        path=value.get('source_path') if kind=='figure' else value.get('pptx_path')
        if kind=='pptx' and value.get('action')=='create':
            paths=value.get('input_paths') or [policy['figure_map']]
            for item in paths:validate_figure_input(cfg,item)
            value['input_paths']=sorted(set(paths))
        else:
            if not path or not relpath(path).is_relative_to(policy['figure_root']):
                fail('BUILD_TARGET_DENIED','Figure sources must be inside the project candidate figure root')
            path_for(cfg,path,True)
            paths=value.get('input_paths') or [path,*value.get('data_paths',[])]
            if path not in paths:fail('INPUT_DENIED','Candidate figure source must be in its immutable snapshot')
            # Data may be read from the project, but execution sources cannot
            # be redirected to a historical or unrelated writable root.
            for item in paths:validate_figure_input(cfg,item)
            value['input_paths']=sorted(set(paths))
        if kind=='figure' and value.get('engine')=='tikz':
            text=safe_read(cfg['project_root'],path,4*1024*1024).decode('utf-8')
            text=re.sub(r'(?<!\\)%[^\n]*','',text)
            if (not re.search(r'\\documentclass(?:\[[^\]]*\])?\s*\{standalone\}',text)
                    or not re.search(r'\\begin\s*\{tikzpicture\}',text)):
                fail('BUILD_TARGET_DENIED','Candidate TikZ must be a standalone figure, not another manuscript entry')
    elif kind=='pdf':
        value['input_paths']=value.get('input_paths') or [value['pdf_path']]
    return value


def validate_figure_input(cfg,path):
    path_for(cfg,path)
    p=relpath(path);policy=cfg['execution_policy']
    if p.is_relative_to(policy['figure_root']):return
    supports={directory for target in policy['tex_targets'].values() for directory in target['tex_search_paths']}
    if any(p.is_relative_to(directory) for directory in supports):return
    allowed_data={'.json','.csv','.tsv','.txt','.png','.jpg','.jpeg','.svg','.pdf','.pptx','.xlsx'}
    if p.suffix.lower() not in allowed_data:
        fail('INPUT_DENIED','Historical executable dependencies cannot be imported into a figure snapshot',path=path)
    if p.suffix.lower()=='.pdf':
        mapping=json.loads(safe_read(cfg['project_root'],policy['figure_map'],4*1024*1024))
        approved={item['project_relative_pdf'] for item in mapping}
        if path not in approved:fail('INPUT_DENIED','PDF figure inputs must be mapped current figures or candidates',path=path)


def render_tex_preview(cfg,job,output,result):
    """Reuse the PDF namespace and bind every page to the freshly built PDF."""
    import shutil
    from paper_studio.pdf.review import review_pdf
    pdf_path=result['pdf_path']
    review_input=job/'preview-input';review_input.mkdir()
    raw=safe_read(output,pdf_path,64*1024*1024)
    if sha(raw)!=result['pdf_sha256']:fail('ARTIFACT_TAMPERED','Fresh TeX PDF identity changed before rendering')
    (review_input/'main.pdf').write_bytes(raw)
    preview=job/'preview-output'
    record=json.loads((job/'request.json').read_text()) if (job/'request.json').exists() else {}
    selection=record.get('request',{}).get('_preview_selection',{})
    reviewed=review_pdf(review_input,preview,{'pdf_path':'main.pdf',**selection},cfg)
    if reviewed.get('source_sha256')!=result['pdf_sha256']:fail('ARTIFACT_TAMPERED','PDF preview belongs to another input')
    from paper_studio.pdf.selection import select_pages
    expected=select_pages(selection,result['pages'])
    if [row['page'] for row in reviewed.get('rendered_pages',[])]!=expected['selected_pages']:fail('PREVIEW_FAILED','Selected fresh PDF pages must have rendered previews')
    shutil.move(str(preview),str(output/'preview'))
    return {**result,'preview':reviewed,'preview_prefix':'preview/','pages_seen':False,
            'preview_pages':expected['selected_pages'],'preview_complete_document':expected['complete_document_review']}
