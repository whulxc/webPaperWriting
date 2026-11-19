"""Conservative project BibTeX synchronization over the scoped readonly bridge.

Library writes and claim verification are deliberately outside this interface.
Only the Gateway's native revision-checked callback may change a bibliography.
"""
from __future__ import annotations
import difflib,json,re,unicodedata,uuid
from pathlib import Path
from paper_studio.security.policy import (StudioError,atomic_json,fail,no_links,path_for,
    project,relpath,safe_read,sha)
from paper_studio.references import zotero_readonly
from paper_studio.references.zotero_worker import normalize_doi,valid_scope,ReadError,CITE,KEY

LIMIT=2*1024*1024


def uncomment(text):
    """Remove TeX line comments, respecting odd/even escaped percent signs."""
    result=[]
    for line in text.splitlines(keepends=True):
        cut=len(line)
        for i,char in enumerate(line):
            if char!='%':continue
            escapes=0;j=i-1
            while j>=0 and line[j]=='\\':escapes+=1;j-=1
            if escapes%2==0:cut=i;break
        result.append(line[:cut]+('\n' if cut<len(line) and line.endswith('\n') else ''))
    return ''.join(result)


def _skip(text,index):
    while index<len(text):
        if text[index].isspace() or text[index]=='\ufeff':index+=1
        elif text[index]=='%':
            end=text.find('\n',index);index=len(text) if end<0 else end+1
        else:break
    return index


def _balanced(text,index):
    start=index;depth=0
    for i in range(index,len(text)):
        ch=text[i];backslashes=0;j=i-1
        while j>=0 and text[j]=='\\':backslashes+=1;j-=1
        if backslashes%2:continue
        if ch=='{':depth+=1
        elif ch=='}':
            depth-=1
            if depth==0:return text[start+1:i],i+1
    fail('BIBTEX_UNSUPPORTED','Unclosed braced BibTeX value')


def _value(text,index):
    index=_skip(text,index)
    if index>=len(text):fail('BIBTEX_UNSUPPORTED','Missing BibTeX value')
    if text[index]=='{':return _balanced(text,index)
    if text[index]=='"':
        start=index+1;depth=0;i=start
        while i<len(text):
            if text[i]=='\\':i+=2;continue
            if text[i]=='{':depth+=1
            elif text[i]=='}':
                depth-=1
                if depth<0:fail('BIBTEX_UNSUPPORTED','Unbalanced quoted BibTeX value')
            elif text[i]=='"' and depth==0:return text[start:i],i+1
            i+=1
        fail('BIBTEX_UNSUPPORTED','Unclosed quoted BibTeX value')
    number=re.match(r'[0-9]+',text[index:])
    if number:return number.group(),index+len(number.group())
    fail('BIBTEX_UNSUPPORTED','BibTeX string macros and concatenation are unsupported')


def parse_bibtex(text):
    """Parse a bounded literal subset; return entries without rewriting input."""
    entries=[];i=0
    while True:
        i=_skip(text,i)
        if i==len(text):return entries
        start=i;match=re.match(r'@([A-Za-z]+)\s*([({])',text[i:])
        if not match:fail('BIBTEX_UNSUPPORTED','Expected a literal BibTeX entry or comment')
        kind=match.group(1).lower();opener=match.group(2);closer='}' if opener=='{' else ')';i+=match.end()
        if kind=='comment':
            if opener!='{':fail('BIBTEX_UNSUPPORTED','Only braced @comment entries are supported')
            _,i=_balanced(text,i-1);continue
        if kind in {'string','preamble'}:fail('BIBTEX_UNSUPPORTED','BibTeX macro/preamble evaluation is unsupported')
        key=re.match(r'\s*([A-Za-z][A-Za-z0-9:_-]{0,63})\s*,',text[i:])
        if not key:fail('BIBTEX_UNSUPPORTED','Literal project citation key and comma required')
        citation_key=key.group(1);i+=key.end();fields={}
        while True:
            i=_skip(text,i)
            if i<len(text) and text[i]==closer:i+=1;break
            field=re.match(r'([A-Za-z][A-Za-z0-9_-]*)\s*=',text[i:])
            if not field:fail('BIBTEX_UNSUPPORTED','Expected literal BibTeX field assignment')
            name=field.group(1).lower();i+=field.end()
            if name in fields:fail('BIBTEX_DUPLICATE_FIELD','Duplicate BibTeX field',field=name,citation_key=citation_key)
            value,i=_value(text,i);fields[name]=value;i=_skip(text,i)
            if i<len(text) and text[i]==',':i+=1;continue
            if i<len(text) and text[i]==closer:i+=1;break
            fail('BIBTEX_UNSUPPORTED','BibTeX concatenation or malformed field separator')
        entries.append({'key':citation_key,'type':kind,'fields':fields,'raw':text[start:i]})


def normalized(value):
    text=unicodedata.normalize('NFKD',str(value)).casefold()
    text=re.sub(r'\\(?:textit|textbf|emph|textrm|textnormal)\b','',text)
    text=re.sub(r'\\([&_%#{}])',r'\1',text)
    return ''.join(c for c in text if c.isalnum() and not unicodedata.combining(c))


def doi_value(value):
    return normalize_doi(re.sub(r'\\([_&%#])',r'\1',str(value)).replace('{','').replace('}',''))


def authors(value):
    result=[]
    for author in re.split(r'\s+and\s+',value,flags=re.I):
        author=author.replace('{','').replace('}','').strip()
        if ',' in author:
            parts=author.split(',');last=parts[0];given=parts[-1]
        else:
            compact=re.fullmatch(r'((?:[A-Za-z]\.\s*)+)([^\s.]+)',author)
            if compact:given,last=compact.groups()
            else:
                parts=author.split();last=parts[-1] if parts else '';given=' '.join(parts[:-1])
        names=re.findall(r'[^\W\d_]+',given,flags=re.UNICODE);tokens=[]
        for name in names:
            # Multi-letter tokens remain full names even when uppercased.
            # Unpunctuated "JD" is ambiguous and is not expanded into initials.
            tokens.append((normalized(name),len(name)==1))
        result.append((normalized(last),tuple(tokens)))
    return result


def compatible_authors(left,right):
    left=authors(left);right=authors(right)
    if len(left)!=len(right):return False
    for (surname,a),(other,b) in zip(left,right):
        if surname!=other or len(a)!=len(b):return False
        for (name,initial),(other_name,other_initial) in zip(a,b):
            if not name or not other_name:return False
            if initial or other_initial:
                if name[0]!=other_name[0]:return False
            elif name!=other_name:return False
    return True


def page_range(value):
    value=str(value).replace(r'\textendash','-').replace('–','-').replace('—','-')
    return re.sub(r'\s+','',re.sub(r'-+','-',value.replace('{','').replace('}',''))).casefold()


def differences(existing,exported,require_doi=True):
    left=existing['fields'];right=exported['fields'];bad=[]
    for field in ['author','title','year']+(['doi'] if require_doi or left.get('doi') or right.get('doi') else []):
        if not left.get(field) or not right.get(field):bad.append(field+':missing');continue
        if field=='author':equal=compatible_authors(left[field],right[field])
        elif field=='doi':equal=doi_value(left[field])==doi_value(right[field])
        else:equal=normalized(left[field])==normalized(right[field])
        if not equal:bad.append(field)
    venue='journal' if exported['type']=='article' else 'booktitle' if exported['type']=='inproceedings' else 'publisher' if exported['type']=='book' else None
    if venue:
        if not left.get(venue) or not right.get(venue):bad.append(venue+':missing')
        elif normalized(left[venue])!=normalized(right[venue]):bad.append(venue)
    if existing['type']!=exported['type']:bad.append('entry_type')
    for field in ['volume','number','pages']:
        if left.get(field) and right.get(field):
            normalize=page_range if field=='pages' else normalized
            if normalize(left[field])!=normalize(right[field]):bad.append(field)
    return bad


def citations(cfg,paths):
    if not isinstance(paths,list) or not 1<=len(paths)<=32 or any(not isinstance(path,str) for path in paths) or len(paths)!=len(set(paths)):fail('INVALID_ARGUMENT','Explicit one to 32 unique tex_paths required')
    found=set();checked=[];unscanned=[];occurrences=[]
    for path in paths:
        path_for(cfg,path)
        if not path.endswith('.tex'):fail('INVALID_ARGUMENT','tex_paths must name .tex files')
        raw=safe_read(cfg['project_root'],path,LIMIT)
        try:text=uncomment(raw.decode('utf-8'))
        except UnicodeError:fail('BIBTEX_UNSUPPORTED','UTF-8 TeX sources required')
        # These literal environments do not execute citation commands.
        text=re.sub(r'\\begin\{(verbatim\*?|lstlisting|minted)\}[\s\S]*?\\end\{\1\}',lambda match:'\n'*match.group().count('\n'),text)
        text=re.sub(r'\\verb\*?([^\w\s])[^\n]*?\1',lambda match:' '*len(match.group()),text)
        for dependency in re.findall(r'\\(?:input|include)\s*\{([^}]+)\}',text):
            unscanned.append({'from':path,'literal_dependency':dependency,'scanned_transitively':False})
        for match in re.finditer(r'\\(cite|citep|citet|autocite|parencite|textcite|footcite|nocite|supercite|smartcite)\*?(?:\s*\[[^\]]*\])*\s*\{([^}]*)\}',text):
            chunk=match.group(2)
            for key in chunk.split(','):
                key=key.strip()
                if not CITE.fullmatch(key):fail('CITATION_UNSUPPORTED','Only explicit literal citation keys are supported')
                found.add(key)
                occurrences.append({'citation_key':key,'tex_path':path,'line':text.count('\n',0,match.start())+1,'command':match.group(1),'claim_support':'unverified'})
        checked.append({'path':path,'sha256':sha(raw)})
    return sorted(found),checked,unscanned,occurrences


def _plan(cfg,name,args):
    project(cfg,args.get('project_id'));bib_path=args.get('bib_path');path_for(cfg,bib_path)
    if not bib_path.endswith('.bib'):fail('INVALID_ARGUMENT','bib_path must identify a .bib file')
    raw=safe_read(cfg['project_root'],bib_path,LIMIT)
    try:text=raw.decode('utf-8')
    except UnicodeError:fail('BIBTEX_UNSUPPORTED','UTF-8 bibliography required')
    entries=parse_bibtex(text);keys={};dois={};rejected=[]
    for entry in entries:
        folded=entry['key'].casefold()
        if folded in keys:rejected.append({'citation_key':entry['key'],'reason':'duplicate_bib_key'})
        else:keys[folded]=entry
        doi=doi_value(entry['fields'].get('doi',''))
        if doi:
            if doi in dois:rejected.append({'citation_key':entry['key'],'reason':'duplicate_bib_doi','other_key':dois[doi]})
            else:dois[doi]=entry['key']
    cited,checked,unscanned,occurrences=citations(cfg,args.get('tex_paths'))
    scope=cfg.get('zotero_readonly')
    if scope is None:fail('ZOTERO_NOT_CONFIGURED','This project has no explicit readonly Zotero scope')
    try:valid_scope(scope)
    except ReadError as error:fail(error.code,error.message)
    retained=[];added=[];mapping=[];append=[]
    for key in cited:
        local=keys.get(key.casefold())
        if local and local['key']!=key:
            rejected.append({'citation_key':key,'reason':'citation_key_case_conflict','bib_key':local['key']});continue
        if key not in scope['items']:
            rejected.append({'citation_key':key,'reason':'outside_project_zotero_scope'});continue
        item=zotero_readonly.dispatch(cfg,{'action':'item','citation_key':key})
        library_key=item.get('zotero_item_key')
        if item.get('status')!='found' or not isinstance(library_key,str) or not KEY.fullmatch(library_key):
            rejected.append({'citation_key':key,'reason':'zotero_item_unavailable','status':item.get('status','invalid_metadata')});continue
        preview=zotero_readonly.dispatch(cfg,{'action':'export_preview','citation_key':key})
        reference=scope['items'][key];exported=preview.get('bibtex');configured=normalize_doi(reference.get('doi'))
        if (preview.get('status')!='found' or preview.get('citation_key')!=key or preview.get('zotero_item_key')!=library_key
            or not isinstance(exported,str) or len(exported.encode())>65536 or preview.get('sha256')!=sha(exported.encode())):
            rejected.append({'citation_key':key,'reason':'zotero_export_identity_mismatch'});continue
        parsed=parse_bibtex(exported)
        if (len(parsed)!=1 or parsed[0]['key']!=key
                or configured and (doi_value(parsed[0]['fields'].get('doi',''))!=configured or normalize_doi(item.get('doi'))!=configured)
                or not configured and (reference.get('item_key')!=library_key or not reference.get('title')
                    or normalized(reference['title'])!=normalized(item.get('title','')))):
            rejected.append({'citation_key':key,'reason':'zotero_export_metadata_mismatch'});continue
        candidate=parsed[0]
        library_citation_keys=item.get('library_citation_keys',[])
        if (not isinstance(library_citation_keys,list) or len(library_citation_keys)>4
            or any(not isinstance(entry,dict) or set(entry)!={'value','source'} or not isinstance(entry.get('value'),str)
                   or not CITE.fullmatch(entry['value']) or entry.get('source') not in {'data.citationKey','data.extra: Citation Key'} for entry in library_citation_keys)):
            rejected.append({'citation_key':key,'reason':'zotero_library_key_metadata_invalid'});continue
        native_keys=preview.get('native_export_keys',[])
        if not isinstance(native_keys,list) or len(native_keys)>1 or any(not isinstance(value,str) or not CITE.fullmatch(value) for value in native_keys):
            rejected.append({'citation_key':key,'reason':'zotero_export_key_metadata_invalid'});continue
        mapping.append({'project_citation_key':key,'zotero_item_key':library_key,'library_citation_keys':library_citation_keys,
                        'native_export_keys':native_keys,'project_key_preserved':True,
                        'native_export_key_matches_project':native_keys==[key] if native_keys else None,
                        'native_export_key_matches_persistent':(all(row['value'] in native_keys for row in library_citation_keys)
                                                               if library_citation_keys and native_keys else None),
                        'persistent_key_conflict':len({row['value'] for row in library_citation_keys})>1,
                        'persistent_citation_key_status':'observed_metadata' if library_citation_keys else 'unknown_not_exposed',
                        'item_version':item.get('item_version'),'library':item.get('library'),
                        'identity_basis':'doi' if configured else 'pinned_parent_key_and_title',
                        'collection_membership_checked':bool(reference.get('collection_keys')),
                        'doi':configured or None,'source':'project_scoped_zotero_readonly','claim_support':'unverified'})
        if local:
            mismatch=differences(local,candidate,require_doi=bool(configured))
            mapping[-1]['optional_metadata_not_compared']=[field for field in ['volume','number','pages'] if not local['fields'].get(field) or not candidate['fields'].get(field)]
            if mismatch:rejected.append({'citation_key':key,'reason':'manual_entry_metadata_conflict','fields':mismatch})
            else:retained.append(key)
        elif configured and configured in dois:
            rejected.append({'citation_key':key,'reason':'doi_already_bound_to_another_key','other_key':dois[configured]})
        elif name=='paper_zotero_verify_citations':rejected.append({'citation_key':key,'reason':'missing_bibliography_entry'})
        else:
            added.append(key);append.append(exported.rstrip()+'\n')
            if configured:dois[configured]=key
    suffix=('' if not append else ('\n' if text.endswith('\n') else '\n\n')+'\n'.join(append))
    post=text+suffix
    diff=''.join(difflib.unified_diff(text.splitlines(keepends=True),post.splitlines(keepends=True),fromfile=bib_path,tofile=bib_path))
    report={'project_id':cfg['project_id'],'bib_path':bib_path,'pre_sha256':sha(raw),'post_sha256':sha(post.encode()),
            'dry_run':args.get('dry_run',True),'applied':False,'diff':diff[:32768],'diff_truncated':len(diff)>32768,
            'mapping':mapping,'retained':retained,'rejected':rejected,'added':added,'modified':[],
            'preserved_uncited_keys':[entry['key'] for entry in entries if entry['key'] not in cited],'cited_keys':cited,'citation_occurrences':occurrences,
            'checked_tex_paths':checked,'coverage':'explicit_paths_only','unscanned_dependencies':unscanned,
            'bibliographic_status':'conflicts' if rejected else 'verified' if cited else 'no_citations',
            'claim_support':'unverified','fulltext_read':False,'library_modified':False,
            'bibliographic_verification_scope':{'required_fields':['author','title','year','entry_type'],
                                               'doi':'required for DOI-scoped entries; exact parent key/title identity for no-DOI entries; per-parent Collection constraint when configured',
                                               'venue':'required for article/inproceedings/book; not invented for misc preprints/reports',
                                               'optional_fields_compared_only_when_present_on_both_sides':['volume','number','pages'],
                                               'other_fields':'preserved without metadata-equivalence verification'},
            'parser':'bounded literal BibTeX; string macros, preambles and concatenation rejected',
            'limitations':['Only explicitly supplied TeX paths are scanned; input/include dependencies and macros are not expanded.',
                           'Line comments and simple verb/verbatim/listings/minted literals are excluded; this is not a full TeX interpreter.',
                           'Citation positions identify text locations for later claim review; no PDF or sentence-level support is verified.']}
    return report,raw,text,post,suffix


def _execute(cfg,name,args,callback):
    if name not in {'paper_zotero_sync_bibtex','paper_zotero_verify_citations'}:fail('TOOL_DENIED','Unknown Zotero bibliography action')
    if type(args.get('dry_run',True)) is not bool or type(args.get('authorized',False)) is not bool:fail('INVALID_ARGUMENT','dry_run and authorized must be boolean')
    apply=name=='paper_zotero_sync_bibtex' and args.get('dry_run',True) is False
    if apply:
        write=cfg.get('zotero_write',{})
        policy=write.get('bib_sync',{}) if isinstance(write,dict) else {}
        if not isinstance(policy,dict) or policy.get('enabled') is not True or args.get('bib_path') not in policy.get('paths',[]):fail('ZOTERO_BIB_SYNC_DISABLED','Bibliography synchronization is not enabled for this exact project path')
        if args.get('authorized') is not True:fail('AUTHORIZATION_REQUIRED','Apply requires authorized=true within enabled synchronization scope')
        revision=args.get('expected_read_revision')
        if type(revision) is not int or not 1<=revision<=9007199254740991:fail('REVISION_REQUIRED','Apply requires the latest numeric native read_revision')
        if not isinstance(args.get('expected_sha256'),str) or not re.fullmatch('[0-9a-f]{64}',args['expected_sha256']):fail('REVISION_REQUIRED','Apply requires expected_sha256 of the exact existing bibliography')
        path_for(cfg,args.get('bib_path'),True)
        if sha(safe_read(cfg['project_root'],args['bib_path'],LIMIT))!=args['expected_sha256']:fail('REVISION_CONFLICT','Bibliography SHA256 differs from the approved input')
    report,raw,text,post,suffix=_plan(cfg,name,args)
    if not apply:return report
    if report['rejected']:fail('BIB_SYNC_CONFLICT','Bibliographic conflicts block the entire synchronization transaction',rejected=report['rejected'])
    if report['pre_sha256']!=args['expected_sha256'] or safe_read(cfg['project_root'],args['bib_path'],LIMIT)!=raw:fail('REVISION_CONFLICT','Bibliography changed while resolving Zotero metadata')
    if not suffix:return {**report,'dry_run':False,'native_result':None,'no_changes':True,'revision_check':'No write was required; numeric revision was validated structurally, without a native edit transaction.'}
    if callback is None:fail('NATIVE_EDIT_REQUIRED','Apply requires the Gateway native revision-checked edit callback')
    if not text:fail('BIBTEX_UNSUPPORTED','An empty bibliography requires separate native initialization before synchronization')
    size=min(256,len(text));anchor=text[-size:]
    while text.count(anchor)!=1 and size<len(text) and size<262144:
        size=min(size*2,len(text));anchor=text[-size:]
    if text.count(anchor)!=1 or len(anchor+suffix)>524288:fail('INPUT_LIMIT','A bounded unique EOF anchor is required for native append')
    changes=[{'kind':'edit','path':args['bib_path'],'expected_read_revision':args['expected_read_revision'],
              'edits':[{'kind':'replace_exact','old_text':anchor,'new_text':anchor+suffix,'expected_match_count':1}]}]
    native=callback(changes)
    if safe_read(cfg['project_root'],args['bib_path'],LIMIT)!=post.encode():fail('REVISION_CONFLICT','Native edit returned but bibliography readback differs; inspect the retained audit before retrying')
    return {**report,'dry_run':False,'applied':True,'native_result':native}


def dispatch(cfg,name,args,native_edit_callback=None):
    apply=name=='paper_zotero_sync_bibtex' and args.get('dry_run',True) is False
    audit=None
    if apply:
        state=no_links(Path(cfg['state_root'])/'zotero-bib-sync'/uuid.uuid4().hex);state.mkdir(mode=0o700,parents=True)
        audit={'project_id':cfg['project_id'],'tool':name,'bib_path':args.get('bib_path'),
               'expected_read_revision':args.get('expected_read_revision'),'expected_sha256':args.get('expected_sha256')}
        atomic_json(state/'attempt.json',audit,True)
    try:
        result=_execute(cfg,name,args,native_edit_callback)
    except Exception as error:
        if audit is not None:atomic_json(state/'result.json',{**audit,'status':'failed','error':error.as_dict() if isinstance(error,StudioError) else {'code':'BIB_SYNC_FAILED','message':'Bibliography sync failed; inspect native transaction and retained inputs'}},True)
        raise
    if audit is not None:
        atomic_json(state/'result.json',{**audit,'status':'applied' if result['applied'] else 'no_changes','pre_sha256':result['pre_sha256'],'post_sha256':result['post_sha256'],'added':result['added'],'retained':result['retained']},True)
        result['audit_id']=state.name
    return result
