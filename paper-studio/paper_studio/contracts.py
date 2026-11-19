"""Versioned logical MCP contracts; no undeclared native capabilities."""
from paper_studio.gateway.download import UI_URI
from paper_studio.security.policy import fail
from paper_studio.versions.target_index import MAX_CHANGES_PER_REQUEST
S={'type':'string'};I={'type':'integer'};B={'type':'boolean'};O={'type':'object'}
P={'project_id':S}
def obj(properties,required=()):return {'type':'object','properties':properties,'required':list(required),'additionalProperties':False}
def tool(name,description,props,required=(),readonly=False):
    return {'name':name,'description':description,'inputSchema':obj(props,required),'annotations':{'readOnlyHint':readonly,'destructiveHint':False,'idempotentHint':readonly,'openWorldHint':name=='paper_publish_to_drive'}}
def base(name,description,props,required=(),readonly=False):return tool(name,description,{**P,**props},('project_id',*required),readonly)
INPUTS={'input_paths':{'type':'array','items':S,'minItems':1,'maxItems':2048},'request_id':{**S,'minLength':1,'maxLength':64},'expected_revision':S}
LANG_PAIR=obj({'en':S,'zh':S},['en','zh'])
SHA256={**S,'minLength':64,'maxLength':64}
REVIEW_PDF=obj({'build_id':S,'sha256':SHA256,'project_folder_id':S,'parent_id':S,
                'language':{**S,'enum':['en','zh','shared']},'name':{**S,'maxLength':200}},
               ['build_id','sha256','project_folder_id','parent_id','language'])
NAMED_TARGET={**S,'minLength':1,'maxLength':96}
LABEL={**S,'minLength':1,'maxLength':64}
PAGE={**I,'minimum':1}
SELECTOR=obj({'role':S,'language':{**S,'enum':['en','zh','shared']},
              'figure_number':{**I,'minimum':1,'maximum':999},'target_key':NAMED_TARGET},['role','language'])
PINNED_PATH=obj({'path':S,'sha256':SHA256},['path','sha256'])
COMMENT_SOURCE=obj({'file_id':S,'comment_id':S,'path':S,'baseline_path':S,'sha256':SHA256,
                    'quote':{**S,'minLength':1,'maxLength':8192},
                    'replacement':{**S,'maxLength':8192},'page':PAGE,
                    'read_revision':{**I,'minimum':1}},
                   ['file_id','comment_id','path','sha256','quote','replacement','page','read_revision'])
VERSION_ACTIONS={
 'action':{**S,'enum':['derive','status','checkout','restore','rollback','stage','dependency','revise','figure','select','promote','activate']},
 'version':S,'destination':S,'source_prefix':S,'source_version':S,'source_builds':LANG_PAIR,
 'rollback_from':S,'initial_published_version':S,'expected_source_manifest_sha256':S,
 'expected_version_sha256':S,'expected_current_sha256':S,'request_id':S,'dry_run':B,
 'reason':S,'revision':S,'figure_id':S,'candidate_id':S,'output_path':S,
 'source_paths':{'type':'array','items':obj({'path':S,'sha256':S,'role':{**S,'enum':['source','output','preview','provenance','review']}},['path','sha256']),'maxItems':32},
 'figure_selections':O,'expected_sources':LANG_PAIR,'build_ids':LANG_PAIR,
 'path':S,'source_path':S,'source_sha256':{**S,'minLength':64,'maxLength':64},'dependency_selections':O,
 'structure':obj({'figures':{**I,'minimum':0,'maximum':100},'tables':{**I,'minimum':0,'maximum':100}},['figures','tables']),
 'visual_review':obj({'path':S,'sha256':S,'pages_seen':obj({lang:{'type':'array','items':I,'maxItems':100} for lang in ['en','zh']},['en','zh'])},['path','sha256','pages_seen']),
 'drive_build_id':S,'delivery_mode':{**S,'enum':['drive','local']},'changelog':S}
TOOLS=[
 base('paper_capabilities','Read the selected project identity, configured tool admission and action-level capability states. Pure configuration discovery does not probe services or grant any write permission. Host, Provider and Native identities remain distinct.',{},readonly=True),
 base('paper_build_plan','Resolve the registered current manuscript and its literal dependencies; returns exact source and input SHA256, bounded input paths, and configured engine/workdir. Read only; does not execute TeX.',{'tex_path':S},['tex_path'],True),
 tool('paper_runtime_status','Read actual native server/Runner/plugin and the currently authorized project.',{},readonly=True),
 base('paper_project_inventory','Bounded local file index with types and hashes; no automatic scientific fact verification.',{'offset':{**I,'minimum':0},'limit':{**I,'minimum':1,'maximum':200}},readonly=True),
 base('paper_project_context','Discover project entrypoints. Indexed projects return bounded entrypoints by default; view=evidence explicitly requests the verified evidence context.',{'view':{**S,'enum':['evidence']}},readonly=True),
 base('paper_read_files','Read at most eight files per native batch. Returned numeric read_revision is required for existing-file edits.',{'items':{'type':'array','minItems':1,'maxItems':8,'items':obj({'path':S,'start_line':{**I,'minimum':1},'limit':{**I,'minimum':1,'maximum':2000},'expected_read_revision':I},['path'])},'max_result_bytes':{**I,'minimum':1024,'maximum':262144}},['items'],True),
 base('paper_edit_files','Create/edit/rename/delete authorized candidate files through native revision-checked transactions. Delete/rename need explicit authorized=true. Read before existing-file changes.',{'changes':{'type':'array','minItems':1,'maxItems':16,'items':O},'dry_run':B,'authorized':B},['changes']),
 base('paper_reference_manage','Inspect BibTeX keys, duplicates, and citations. Use paper_edit_files to create or change source BibTeX; does not fabricate reference metadata.',{'bib_path':S,'tex_paths':{'type':'array','items':S,'maxItems':32}},['bib_path'],True),
 base('paper_zotero_read','Read Windows Zotero Local API through the installed hash-pinned skill. Only explicitly scoped project references are visible: exact DOI or pinned parent key/title; per-parent Collection membership constraints further narrow access when configured. Metadata includes parent/library/version and separates native export keys from observed persistent keys. No imports, attachment paths/fulltext, arbitrary API routes or file writes. export_preview preserves the project citation key and writes nothing.',{'action':{**S,'enum':['status','search','item','collections','attachments','export_preview']},'query':{**S,'maxLength':240},'citation_key':{**S,'maxLength':64}},['action'],True),
 base('paper_zotero_add_reference','Preflight or import ONE explicitly authorized hash-pinned source, deduplicating exact DOI and preserving project citation keys. Defaults to dry_run. Selected Zotero 10 local SDK backend binds an explicit collection key; writes require a pinned server and separately granted local key. Older Zotero is blocked; no UI Connector import.',{'citation_key':{**S,'maxLength':64},'doi':{**S,'maxLength':200},'collection_name':{**S,'maxLength':160},'request_id':{**S,'minLength':1,'maxLength':64},'dry_run':B,'authorized':B},['citation_key','doi','collection_name','request_id']),
 base('paper_zotero_manage','Inspect the selected local SDK backend or query/create ONE project-authorized collection. Defaults to dry_run; writes require explicit authorization and Zotero 10 local grant. update is disabled. No arbitrary route, deletion, attachment download or automatic authorization.',{'action':{**S,'enum':['status','collection','update']},'citation_key':{**S,'maxLength':64},'collection_name':{**S,'maxLength':160},'request_id':{**S,'maxLength':64},'dry_run':B,'authorized':B},['action']),
 base('paper_zotero_sync_bibtex','Plan project-cited Zotero entries, preserving project citation keys and all existing manual BibTeX bytes. Defaults to dry_run; appending missing entries requires separate configured permission, authorized=true, latest numeric read_revision and SHA256. Does not rewrite TeX or delete references.',{'bib_path':S,'tex_paths':{'type':'array','items':S,'minItems':1,'maxItems':32},'dry_run':B,'authorized':B,'expected_read_revision':{**I,'minimum':1,'maximum':9007199254740991},'expected_sha256':{**S,'minLength':64,'maxLength':64}},['bib_path','tex_paths']),
 base('paper_zotero_verify_citations','Compare explicit TeX citation keys, project BibTeX and real scoped Zotero bibliographic identities. Reports incomplete TeX coverage and conflicts separately. Claim support remains unverified; no PDF/fulltext retrieval or scientific evidence inference.',{'bib_path':S,'tex_paths':{'type':'array','items':S,'minItems':1,'maxItems':32}},['bib_path','tex_paths'],True),
 base('paper_literature_search','Search public scholarly metadata through the project-enabled provider. Returns candidates with provenance, never automatically confirmed references. No fulltext download or Zotero write.',{'provider':{**S,'enum':['crossref']},'query':{**S,'minLength':1,'maxLength':500},'max_results':{**I,'minimum':1,'maximum':10}},['query'],True),
 base('paper_reference_resolve','Resolve one DOI using public Crossref metadata. Preserves provider identity, retrieval time and uncertainty; does not merge conflicting sources or imply claim support.',{'provider':{**S,'enum':['crossref']},'doi':{**S,'maxLength':200}},['doi'],True),
 base('paper_citation_graph','Return bounded outgoing references deposited with Crossref. Coverage may be incomplete; incoming citations are unsupported. No recursive crawling.',{'provider':{**S,'enum':['crossref']},'doi':{**S,'maxLength':200},'max_results':{**I,'minimum':1,'maximum':10}},['doi'],True),
 base('paper_open_access_lookup','Inspect Crossref license/link metadata for a DOI. Open access remains unknown until independently verified; links are not fetched and no full text is downloaded.',{'provider':{**S,'enum':['crossref']},'doi':{**S,'maxLength':200}},['doi'],True),
 base('paper_evidence_read','Read text from 1..4 selected pages of a hash-bound project PDF in the existing isolated parser. Returns actual byte/text hashes, page count and truncation. Does not render, OCR, download attachments or establish scientific support.',{'pdf_path':S,'expected_sha256':{**S,'minLength':64,'maxLength':64},'pages':{'type':'array','items':{**I,'minimum':1,'maximum':60},'minItems':1,'maxItems':4},'max_chars':{**I,'minimum':1,'maximum':20000}},['pdf_path','expected_sha256','pages'],True),
 base('paper_research_state','Read the configured project continuity JSON and verify referenced file hashes. Scientific acceptance and build statuses are not automatically trusted. Use paper_read_files/edit_files for revision-checked updates and existing artifact/Drive tools for delivery.',{},readonly=True),
 base('paper_drive_verify','Read real Google Drive metadata using the configured local Publisher OAuth grant. Any accessible valid file/folder ID may be checked; omit file_ids to verify authorization and the configured project folder. Only metadata GETs, no downloads, sharing changes or publication permission. Optional checks compare server SHA256, byte size and parent; unavailable server checksums stay unverified.',{'file_ids':{'type':'array','items':{**S,'minLength':1,'maxLength':200},'minItems':1,'maxItems':20},'checks':{'type':'array','maxItems':20,'items':obj({'file_id':{**S,'minLength':1,'maxLength':200},'sha256':{**S,'minLength':64,'maxLength':64},'size':{**I,'minimum':0},'parent':{**S,'minLength':1,'maxLength':200}},['file_id'])}},readonly=True),
 base('paper_source_sync','Plan/status or explicitly authorized source sync with the existing local Drive Publisher. Default dry_run. Gateway apply-sync returns a durable background task immediately; poll status with the SAME request_id. Duplicates never start a second active worker. Completed historical requests return receipt_only=true without new remote verification. Reconcile a lost worker by explicitly repeating the same apply arguments; changed configuration is rejected. Review import stays scoped and create-only. Large bytes stay local-to-Drive, not in MCP responses.',{'action':{**S,'enum':['plan','sync','status','import_review']},'request_id':{**S,'minLength':1,'maxLength':64},'dry_run':B,'authorized':B,'check_remote':B,'expected_snapshot_sha256':{**S,'minLength':64,'maxLength':64},'remote_file_id':{**S,'minLength':1,'maxLength':200},'destination':S},['action']),
 base('paper_figure_build','Submit isolated Python, SVG or TikZ build; Python writes to PAPER_OUTPUT_DIR and reads immutable input. Returns asynchronous build_id.',{**INPUTS,'engine':{**S,'enum':['python','svg','tikz','matlab']},'source_path':S,'data_paths':{'type':'array','items':S,'maxItems':100},'parameters':O},['request_id','source_path','expected_revision','engine']),
 base('paper_pptx_edit','Create/edit/export native PPTX, or import_drive one exact stored PPTX from the bound project Publisher folder to a new writable figure path. import_drive defaults to dry_run; apply needs authorized=true and returns an import receipt directly.',{**INPUTS,'action':{**S,'enum':['create','edit','export','import_drive']},'pptx_path':S,'program':O,'commands':{'type':'array','items':O,'maxItems':100},'deck':O,
      'drive_source':obj({'file_id':S,'project_folder_id':S,'parent_id':S,'expected_sha256':SHA256,'expected_bytes':{**I,'minimum':1,'maximum':67108864}},['file_id']),
      'destination':S,'dry_run':B,'authorized':B},['request_id','action']),
 base('paper_pptx_inspect','Inspect validated PPTX native objects, geometry and package metadata with bounded paging.',{'pptx_path':S,'offset':{**I,'minimum':0},'limit':{**I,'minimum':1,'maximum':50}},['pptx_path'],True),
 base('paper_tex_build_submit','Capture authorized source revision and submit isolated LaTeX build. expected_input_sha256 optionally fences the full literal dependency plan, including BibTeX changes. Optional resume_build_id creates a NEW audited recovery job only for an inactive failed TeX job with unchanged source snapshot and engine options. Use a new stable request_id; the old job remains unchanged. Query paper_build_status; completion is distinct from delivery.',{**INPUTS,'tex_path':S,'expected_input_sha256':{**S,'minLength':64,'maxLength':64},'build_profile':{**S,'enum':['pdflatex','xelatex','lualatex']},'use_latexmk':B,'resume_build_id':S},['request_id','tex_path','expected_revision']),
 base('paper_manuscript_build','Manage immutable bNNN manuscript build records without creating a new paper version. submit snapshots the complete project excluding managed history; trusted targets execute only their exact dependency plan, separately bound to the full restorable source archive. status/list/read verify archived hashes; restore creates a NEW directory containing the complete project-relative source tree.',{'action':{**S,'enum':['submit','list','status','read','restore']},'workdir':S,'record_id':S,'request_id':S,'tex_path':S,'expected_revision':S,'expected_input_sha256':{**S,'minLength':64,'maxLength':64},'build_profile':{**S,'enum':['pdflatex','xelatex','lualatex']},'use_latexmk':B,'path':S,'offset':{**I,'minimum':0},'length':{**I,'minimum':1,'maximum':262144},'destination':S},['action','workdir']),
 base('paper_build_status','Read actual provider build state, output identity and bounded log tail. rb IDs are provider IDs, not native Job IDs.',{'build_id':S,'tail_lines':{**I,'minimum':0,'maximum':80}},['build_id'],True),
 base('paper_build_artifact','Read metadata, export native ResourceLink, return a PNG/JPEG as actual MCP image (action=image), get <=256KiB chunk, or copy to a NEW candidate file. A returned resource is not proof of visual review.',{'build_id':S,'path':S,'action':{**S,'enum':['metadata','export','image','chunk','copy_to_candidate']},'offset':{**I,'minimum':0},'length':{**I,'minimum':1,'maximum':262144},'destination':S},['build_id','path']),
 base('paper_pdf_review','Render selected PDF pages and report the total page count. Pages are 1-based; use pages or inclusive page_ranges. Large manuscripts default to a bounded preview.',{**INPUTS,'pdf_path':S,
      'pages':{'type':'array','minItems':1,'maxItems':60,'items':PAGE},
      'page_ranges':{'type':'array','minItems':1,'maxItems':60,'items':obj({'start':PAGE,'end':PAGE},['start','end'])}},['request_id','pdf_path','expected_revision']),
 base('paper_version_list','List immutable local paper versions. Default verification=full hashes every member. Explicit verification=manifest reads only archive metadata and returns verified=false, members_verified=false. Optional version selects one archive for discovery or full verification.',{'verification':{**S,'enum':['full','manifest']},'version':S},readonly=True),
 base('paper_version_save','Publish a new immutable local version from completed builds, source snapshots, outputs, logs and changelog.',{'version':S,'build_ids':{'type':'array','items':S,'minItems':1,'maxItems':16},'changelog':{**S,'minLength':1,'maxLength':16000}},['version','build_ids','changelog']),
 base('paper_version_compare','Compare two verified source snapshots and return file hashes and bounded text diffs.',{'left':S,'right':S},['left','right'],True),
 base('paper_version_derive','Legacy derive remains compatible. Opt-in managed versions support status/checkout/restore/rollback/stage/dependency/revise/figure/select/promote/activate. stage copies a frozen support file to working/deps for editing; dependency captures an immutable dep-NNN candidate; revise selects dependency candidates (dep-NNN/base/absent), figure candidates and, with an explicit structure declaration and reason, adds or removes figure/table slots. New writes default dry_run and require exact expected state hashes and request_id; frozen revisions and published versions cannot be edited. Build and Drive publication remain separate existing tools.',VERSION_ACTIONS),
 base('paper_publish_to_drive','Choose exactly one: review_pdf displays one current source-bound marked or controlled clean TeX build PDF in its bound private folder and returns a verified link without creating a paper version; version submits an existing formal archive job. A managed formal selection may use <working-version>@rev-NNN to check the exact promoted revision. A bare rev-NNN does not identify its parent manuscript. An unpromoted working revision returns FORMAL_ARCHIVE_REQUIRED with the review and promotion routes. Optional resume_build_id only recovers a failed formal publication. Never changes sharing.',{'version':{**S,'minLength':1},'resume_build_id':S,'review_pdf':REVIEW_PDF})
]
TOOLS.extend([
 base('paper_index_get','Look up a registered manuscript or figure by exact version, revision, language and role. Returns its immutable identity and verified Drive reference where available. Use part=content for bounded original bytes. Missing keys do not start a search.',
      {'version':S,'revision':S,'role':S,'language':{'type':'string','enum':['en','zh','shared']},
       'figure_number':{**I,'minimum':1,'maximum':999},'target_key':NAMED_TARGET,'part':{**S,'enum':['metadata','content']},
       'offset':{**I,'minimum':0},'limit':{**I,'minimum':1,'maximum':262144},
       'expected_revision_sha256':{**S,'minLength':64,'maxLength':64}},
      ['version','role','language'],True),
 base('paper_index_revise','Capture a working subrevision from explicit changed files and selected figure versions. Unchanged objects are inherited. The request budget applies to this edit, not to the total size of the manuscript. A working revision never creates a formal release.',
      {'version':S,'revision':S,'baseline':obj({'version':S,'revision':S},['version','revision']),
       'members':{'type':'array','maxItems':MAX_CHANGES_PER_REQUEST,'items':obj({'path':S,'sha256':{**S,'minLength':64,'maxLength':64},
                    'role':S,'language':{**S,'enum':['en','zh','shared']},'figure_number':{**I,'minimum':1,'maximum':999},
                    'figure_version':S,'target_key':NAMED_TARGET},['path','sha256','role','language'])},
       'figure_selections':{'type':'array','maxItems':MAX_CHANGES_PER_REQUEST,'items':obj({'figure_number':{**I,'minimum':1,'maximum':999},
                    'role':{**S,'enum':['figure_pdf','table_pdf']},'target_key':NAMED_TARGET,
                    'language':{**S,'enum':['en','zh','shared']},'figure_version':S},['language','figure_version'])},
       'expected_head':S,'request_id':INPUTS['request_id']},
      ['version','revision','baseline','members','expected_head','request_id']),
 base('paper_material_get','Read one indexed material handoff by change ID, manifest SHA, or the current handoff pointer.',{'change_id':LABEL,'manifest_sha256':SHA256},readonly=True),
 base('paper_review_proposal','Show a marked-up review against the current edition. Comments and previews leave its version unchanged. Apply only confirmed changes; a major version needs separate explicit authorization.',
      {'action':{**S,'enum':['propose','get','render','decide','apply']}, 'proposal_id':LABEL,
       'baseline':obj({'version':S,'revision':S},['version','revision']), 'expected_head':SHA256,
       'changes':{'type':'array','minItems':1,'maxItems':MAX_CHANGES_PER_REQUEST,'items':obj({
            'change_id':LABEL,'kind':{**S,'enum':['text','figure']},'selector':SELECTOR,
            'title':S,'reason':S,'before':S,'after':S,'occurrence':{**I,'minimum':1},
            'candidate':PINNED_PATH,'source':PINNED_PATH,
            'anchor':obj({'page':PAGE,'rect':{'type':'array','minItems':4,'maxItems':4,'items':{'type':'number'}},
                          'quote':S})},['change_id','kind','selector'])},
       'build_bundle':obj({'main_paths':LANG_PAIR,'inputs':{'type':'array','minItems':1,'maxItems':2048,'items':PINNED_PATH},
                           'bindings':{'type':'array','maxItems':2048,'items':obj({'selector':SELECTOR,'path':S},['selector','path'])},
                           'export_root':S},['main_paths','bindings','export_root']),
       'expected_proposal_sha256':SHA256,'expected_review_sha256':SHA256,
       'part':{**S,'enum':['summary','content','artifact']},'selector':SELECTOR,
       'view':{**S,'enum':['baseline','proposed','selected']},'artifact_path':S,'render_id':S,
       'offset':{**I,'minimum':0},'limit':{**I,'minimum':1,'maximum':262144},
       'format':{**S,'enum':['html','pdf','both']},'change_id':LABEL,
       'decision':{**S,'enum':['accept','reject','adjust','comment']},'reviewer':S,'comment':S,
       'confirmed':B,'adjusted_after':S,'adjusted_candidate':PINNED_PATH,'adjusted_source':PINNED_PATH,
       'request_id':INPUTS['request_id'],'authorized':B,
       'bump':{**S,'enum':['minor','major']},'major_authorized':B,'major_reason':S},
      ['action','proposal_id']),
 tool('paper_pdf_preview','Open the current binding\'s explicitly configured immutable bilingual PDF viewer. This read-only conversation-panel entrypoint takes no arguments and never switches project.',{},readonly=True),
 base('paper_section','Stage an immutable bilingual section from a captured material handoff, or compare two revisions. Stage defaults to dry_run.',{
      'action':{**S,'enum':['stage','compare']}, 'section_id':S, 'revision':S, 'scope':{**S,'maxLength':180},
      'expected_previous_sha256':S, 'material_manifest':S, 'material_revision':S,
      'bodies':LANG_PAIR, 'left_revision':S, 'right_revision':S, 'dry_run':B}, ['action','section_id']),
 base('paper_section_delivery','Open a download card for bilingual PDFs. Select section_id+revision or an immutable full-manuscript archive version, with its exact EN/ZH build IDs. No Drive account required.',
      {'section_id':S, 'revision':S, 'version':S, 'build_ids':LANG_PAIR}, ['build_ids'], True),
 base('paper_review_comments','Read hash-bound formal or lightweight Drive PDF comments. Select version or review_receipt_id; version=review:<receipt SHA> is an explicit lightweight compatibility locator. Collect defaults to dry_run. Apply requires a saved source binding and an explicit acceptance reply by the current Drive user in the same thread.',
      {'action':{**S,'enum':['plan','collect','status','apply']}, 'version':S,
       'review_receipt_id':SHA256,'dry_run':B,'authorized':B,'request_id':INPUTS['request_id'],
       'source_bindings':{'type':'array','items':COMMENT_SOURCE,'maxItems':16},
       'collection_request_id':INPUTS['request_id'],'expected_snapshot_sha256':SHA256,
       'confirmations':{'type':'array','minItems':1,'maxItems':16,
                        'items':obj({'file_id':S,'comment_id':S,'reply_id':S},['file_id','comment_id','reply_id'])}}, ['action'])
])
for item in TOOLS:
    if 'project_id' in item['inputSchema']['properties']:
        item['inputSchema']['properties']['expected_config_revision'] = {**S,'minLength':64,'maxLength':64}
BY_NAME={t['name']:t for t in TOOLS}
BY_NAME['paper_pdf_preview']['title']='双语原稿预览'
BY_NAME['paper_section_delivery']['_meta']={
    'ui':{'resourceUri':UI_URI}, 'openai/outputTemplate':UI_URI}
# This tool is already admitted to the model and governed by the same project
# and operation checks. Make that existing surface explicit for UI hosts.
BY_NAME['paper_build_artifact']['_meta']={
    'ui':{'visibility':['model','app']}, 'openai/widgetAccessible':True}
BY_NAME['paper_review_comments']['annotations']['openWorldHint']=True
BY_NAME['paper_edit_files']['annotations']['destructiveHint']=True
BY_NAME['paper_source_sync']['annotations']['openWorldHint']=True
BY_NAME['paper_drive_verify']['annotations']['openWorldHint']=True
BY_NAME['paper_pptx_edit']['annotations']['openWorldHint']=True

def validate(value,schema,path='$'):
    typ=schema.get('type');expected={'object':dict,'array':list,'string':str,'integer':int,'boolean':bool,'number':(int,float)}
    if typ and (not isinstance(value,expected[typ]) or typ in ['integer','number'] and isinstance(value,bool)):fail('SCHEMA_INVALID','Unexpected argument type',path=path,expected=typ)
    if 'enum' in schema and value not in schema['enum']:fail('SCHEMA_INVALID','Value is not in enum',path=path)
    if isinstance(value,dict):
        for k in schema.get('required',[]):
            if k not in value:fail('SCHEMA_INVALID','Missing required argument',path=path+'.'+k)
        props=schema.get('properties',{})
        if schema.get('additionalProperties') is False:
            if set(value)-set(props):fail('SCHEMA_INVALID','Unknown arguments',path=path,fields=sorted(set(value)-set(props)))
        for k,v in value.items():
            if k in props:validate(v,props[k],path+'.'+k)
    if isinstance(value,list):
        if len(value)<schema.get('minItems',0) or len(value)>schema.get('maxItems',100000):fail('SCHEMA_INVALID','Array size out of bounds',path=path)
        for i,v in enumerate(value):validate(v,schema.get('items',{}),f'{path}[{i}]')
    if isinstance(value,str) and (len(value)<schema.get('minLength',0) or len(value)>schema.get('maxLength',2*1024*1024)):fail('SCHEMA_INVALID','String size out of bounds',path=path)
    if typ in ['integer','number'] and (value<schema.get('minimum',float('-inf')) or value>schema.get('maximum',float('inf'))):fail('SCHEMA_INVALID','Number out of bounds',path=path)
