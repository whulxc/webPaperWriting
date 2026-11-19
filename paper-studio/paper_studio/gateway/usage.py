"""Project-neutral, bounded examples returned by paper_project_context."""
GUIDANCE={
 'files':{'create':{'kind':'create','path':'candidates/example.md','content':'Example\n'},
          'edit':{'kind':'edit','path':'candidates/example.md','expected_read_revision':'use the numeric read_revision from paper_read_files','edits':[{'kind':'replace_exact','old_text':'Example','new_text':'Revised example','expected_match_count':1}]}},
 'builds':{'request_id':'Unique 1..64 ASCII letters/digits/dash/underscore; reuse only for an identical request.',
           'expected_revision':'SHA256 of the exact source file from paper_read_files.file_identities.',
           'input_paths':'Optional complete source dependency subset; every relative dependency must be captured. Omit to snapshot the bounded authorized project.',
           'completion':'Poll paper_build_status until completed/failed/recovery_required; queued is not success.',
           'outputs':'Use paper_build_artifact action=copy_to_candidate with a NEW destination to insert generated PDF/PPTX into candidate sources.'},
 'python':{'cwd':'/input (immutable project snapshot root)','output':'Path(os.environ["PAPER_OUTPUT_DIR"])',
           'parameters':'json.loads(os.environ["PAPER_PARAMETERS"])','example':'data=pd.read_csv("materials/series.csv"); fig.savefig(Path(os.environ["PAPER_OUTPUT_DIR"])/"series.pdf")',
           'network':'disabled','matlab':'not supported by this isolated implementation'},
 'pptx':{'units':'geometry mm; rotation degrees; font/line points; slide index starts at1; colors six hex digits without #',
         'create_example':{'action':'create','deck':{'width_mm':254,'height_mm':142.875,'slides':[{'objects':[
           {'type':'textbox','name':'title','x':12,'y':8,'width':220,'height':18,'text':'Editable system diagram','text_style':{'font_size':24,'bold':True}},
           {'type':'shape','name':'sensor','x':15,'y':40,'width':55,'height':25,'geometry':'roundRect','text':'Sensor'},
           {'type':'shape','name':'model','x':100,'y':40,'width':55,'height':25,'text':'Model'},
           {'type':'connector','name':'flow','x':70,'y':52,'width':30,'height':0.1,'style':{'line_color':'42658F','arrow':True}},
           {'type':'table','name':'facts','x':15,'y':85,'width':140,'height':30,'header':True,'rows':[['Input','Status'],['Synthetic fixture','Not research data']]}]}]}},
         'edit_example':{'action':'edit','pptx_path':'candidates/system.pptx','commands':[{'op':'position','slide':1,'shape':'sensor','x':20},{'op':'style','slide':1,'shape':'sensor','fill':'DDF4E9','line_width':2}]},
         'program':'Registered OOXML operations: set, insert, replace, delete, move, text, part_data. Inspect native IDs before edits.',
         'connectors':'Moving a shape preserves attachment references but does not automatically reroute connector geometry; edit connector coordinates explicitly.',
         'preview':'Configured verified Windows user session exports deck.pdf and slide_01.png; keep deck.pptx editable.',
         'import_drive':{'action':'import_drive','request_id':'figure-pptx-intake-001','drive_source':{'file_id':'exact stored PPTX ID'},'destination':'figures/candidates/new-figure.pptx','dry_run':True},
         'import_apply':'Repeat the dry-run request with dry_run=false, authorized=true and expected_sha256/expected_bytes from its receipt. The destination must be a NEW writable figure path. Import returns import_complete and local_readback_verified directly, with no build_id. It follows exact parent IDs to the existing Publisher project folder and never lists Drive.',
         'import_routing':'If a cached Chat schema lacks import_drive, discover the actual project provider (normally paper-studio), describe its live paper_pptx_edit through plugin_tool and call the returned binding with the same project identity.'},
 'versions':{'save':'paper_version_save(version,build_ids,changelog) retains legacy immutable archives. Comparison includes all entry identities; legacy derive still selects the last build. Opt-in managed versions use paper_version_derive(action=status/restore/rollback/stage/dependency/revise/figure/select/promote/activate), default dry_run and expected VERSION/CURRENT hashes. Support files (.bib/.sty/sections): stage copies the frozen file to working/deps/<path>, edit it, capture dependency dep-NNN, then revise with dependency_selections (dep-NNN, base or absent). New or removed figures/tables: capture figure candidates for new figNN slots, then revise with figure_selections listing the final slots plus structure={figures,tables} and a reason. Build frozen revisions with existing build_plan/tex_build_submit; publish with existing publish_to_drive before activate.',
             'derive':'paper_version_derive(version,destination="candidates/new",source_prefix="candidates/old") never overwrites existing files.'},
 'latex':{'orchestration':'When paper_capabilities lists trusted targets for paper_build_plan (including managed version revisions), follow the plan engine, bibliography_engine, use_latexmk, expected_revision and input_sha256 exactly. Pass expected_input_sha256 when the observed schema supports it. Each target or frozen revision fixes its recipe; callers cannot override it. Legacy recipes retain XeLaTeX + BibTeX and use_latexmk=false. Projects without trusted targets may use an explicitly selected supported engine only if their existing project policy permits submission.',
          'compatibility':'If strict PDF review detects duplicate Group entries after pdfLaTeX imports a PowerPoint PDF, retain the failed output and try a separate XeLaTeX build. Do not disable strict checks or overwrite historical PDFs.'},
 'drive':{'publication':'paper_publish_to_drive(version=<formal archive label>) submits an idempotent asynchronous publication and returns build_id. For managed archives, <working-version>@rev-NNN also checks the exact promoted revision. A bare rev-NNN needs its parent manuscript. An existing working revision without a formal archive returns FORMAL_ARCHIVE_REQUIRED with review_pdf and explicit promote instructions. Poll paper_build_status; accepted/queued/running is not an upload success.',
          'single_review':'paper_publish_to_drive(review_pdf={build_id,sha256,language,project_folder_id,parent_id,name}) displays the exact primary PDF of one current controlled TeX build, including managed clean revisions and marked reviews. It returns a verified Drive link and review_receipt_id directly. Use that receipt for comments; no new paper version or formal archive is needed. Stale sources, unbound clean-review language or a mismatched PDF are rejected.',
          'verification':'Only completed plus result.upload_complete and verified file metadata establish publication. Full sanitized receipt is a job artifact; no large binary travels through the Tunnel.',
          'failure':'A partial publication is retained. Repeating the same version returns the same task, not a new upload. Do not delete partial files or replace published versions implicitly.',
          'review':'Drive API readback, connected Drive retrieval, actual ChatGPT page display and human acceptance are separate observations.'},
 'delivery':(
     'For display, render or inspect locally and return a verified project Drive link for each requested file. '
     'This applies to manuscripts, figures, page previews and editable decks. Reuse a matching receipt or use '
     'a publication route enabled for that artifact, checking file ID, size, parent and server SHA256 when available. '
     'If Chat needs the contents, read that exact Drive file with the connected Drive tool. Report only pages actually seen. '
     'When a link or reader is unavailable, report the missing step and keep the result locally. '
     'Local file exports, image returns, embedded viewers and chunked attachments require an explicit request '
     'for that delivery method and follow the host approval flow.')
}
