"""Standalone scoped import worker; Connector writes fail closed without atomic target proof."""
from __future__ import annotations
import importlib.util,json,re,sys,time,unicodedata,urllib.error,urllib.parse,urllib.request
from pathlib import Path

# Reuse the audited sibling's helper loader and GET bounds without importing the
# Paper Studio package (the Windows worker is stdlib-only).
_spec=importlib.util.spec_from_file_location('_studio_zotero_readonly',Path(__file__).with_name('zotero_worker.py'))
readonly=importlib.util.module_from_spec(_spec);sys.modules[_spec.name]=readonly;_spec.loader.exec_module(readonly)
ReadError=readonly.ReadError

def fail(code,message):readonly.fail(code,message)
def normalize(value):return re.sub(r'\s+',' ',unicodedata.normalize('NFKC',str(value)).replace('\\&','&').strip()).casefold()
def parse_source(raw,citation_key,doi):
    """Intentionally small BibTeX subset: no macros, files, notes or executable TeX."""
    if not isinstance(raw,str) or len(raw.encode())>16384:fail('ZOTERO_SOURCE_INVALID','Approved single-entry BibTeX is missing or exceeds 16 KiB')
    entry=re.fullmatch(r'\s*@article\s*\{\s*([A-Za-z][A-Za-z0-9:_-]{0,63})\s*,(.*)\}\s*',raw,re.S|re.I)
    if not entry or entry.group(1)!=citation_key:fail('ZOTERO_SOURCE_INVALID','Approved BibTeX must contain exactly the authorized article key')
    rest=entry.group(2);fields={};position=0
    field=re.compile(r'\s*([A-Za-z]+)\s*=\s*\{([^{}]*)\}\s*(?:,|$)',re.S)
    while position<len(rest):
        if not rest[position:].strip():break
        match=field.match(rest,position)
        if not match:fail('ZOTERO_SOURCE_INVALID','Unsupported BibTeX syntax or multiple entries')
        name,value=match.group(1).lower(),match.group(2).strip()
        if name not in {'author','title','journal','volume','number','pages','year','doi'} or name in fields:fail('ZOTERO_SOURCE_INVALID','Unexpected or duplicated bibliography field')
        if re.search(r'[\\{}$%#_^~]',value.replace('\\&','')) or any(ord(c)<32 for c in value):fail('ZOTERO_SOURCE_INVALID','Active TeX or complex bibliography fields are refused')
        fields[name]=value.replace('\\&','&');position=match.end()
    if set(fields)!={'author','title','journal','volume','number','pages','year','doi'} or readonly.normalize_doi(fields['doi'])!=readonly.normalize_doi(doi):fail('ZOTERO_SOURCE_INVALID','Approved bibliographic fields or DOI are incomplete or inconsistent')
    if not re.fullmatch(r'\d{4}',fields['year']):fail('ZOTERO_SOURCE_INVALID','Four digit publication year required')
    authors=[]
    for value in fields['author'].split(' and '):
        parts=value.split(',')
        if len(parts)!=2 or not all(part.strip() for part in parts):fail('ZOTERO_SOURCE_INVALID','Only explicit family, given author names are supported')
        authors.append([parts[0].strip(),parts[1].strip()])
    fields['authors']=authors
    return fields

def differences(item,expected):
    data=item.get('data',item);changed=[]
    for source,target in [('title','title'),('journal','publicationTitle'),('volume','volume'),('number','issue')]:
        if normalize(expected[source])!=normalize(data.get(target,'')):changed.append(source)
    if str(expected['year'])!=str((re.search(r'\d{4}',str(data.get('date',''))) or [''])[0]):changed.append('year')
    pages=lambda value:normalize(value).replace('--','-').replace('–','-').replace('—','-')
    if pages(expected['pages'])!=pages(data.get('pages','')):changed.append('pages')
    actual=[]
    for creator in data.get('creators',[]):
        if creator.get('creatorType')=='author':actual.append([normalize(creator.get('lastName','')),creator.get('firstName','')])
    def given_matches(left,right):
        words=lambda value:re.findall(r'[^\W\d_]+',normalize(value),re.UNICODE)
        left,right=words(left),words(right)
        if not left or not right or len(left)!=len(right):return False
        return all(a==b if len(a)>1 and len(b)>1 else a[0]==b[0] for a,b in zip(left,right))
    if (len(actual)!=len(expected['authors']) or any(normalize(last)!=family or not given_matches(given,first)
            for (last,given),(family,first) in zip(expected['authors'],actual))):changed.append('author')
    return changed

class ImportGuard(readonly.Guard):
    def __init__(self,helper,deadline):super().__init__(helper,deadline);self.collection_paths=set()
    def request(self,path,*,method='GET',data=None,headers=None,timeout=5):
        if method=='GET':return super().request(path,method=method,data=data,headers=headers,timeout=timeout)
        if method!='POST' or path!='/connector/getSelectedCollection' or data!={} or headers not in (None,{'Content-Type':'application/json'}):fail('ZOTERO_ROUTE_DENIED','Only the explicit selected-target probe is permitted before import')
        remaining=self.deadline-time.monotonic()
        if remaining<=0:fail('ZOTERO_DEADLINE','Import worker deadline exceeded')
        url=readonly.BASE+path;request=urllib.request.Request(url,data=b'{}',method='POST',headers={'Content-Type':'application/json','X-Zotero-Connector-API-Version':'3'})
        try:
            with self.opener.open(request,timeout=min(5,remaining)) as response:
                if response.geturl()!=url:fail('ZOTERO_REDIRECT_DENIED','Connector changed target URL')
                if response.status!=200:fail('ZOTERO_HTTP_ERROR','Selected-target probe did not return HTTP200')
                raw=response.read(readonly.LIMIT+1)
                if len(raw)>readonly.LIMIT:fail('ZOTERO_RESPONSE_LIMIT','Connector response exceeds limit')
                return self.helper.Response(200,dict(response.headers.items()),raw.decode())
        except ReadError:raise
        except (OSError,urllib.error.URLError):fail('ZOTERO_UNAVAILABLE','Connector selected-target probe failed')

def collection_target(helper,guard,approved):
    rows=[];start=0;total=None
    while total is None or start<total:
        path='/api/users/0/collections?'+helper.query({'limit':100,'start':start});guard.allow(path)
        response=helper.request(path);page=helper.parse_body(response);headers={key.lower():value for key,value in response.headers.items()}
        value=headers.get('total-results','')
        if not isinstance(page,list) or not value.isdigit() or len(page)>100:fail('ZOTERO_COLLECTION_INCOMPLETE','Cannot completely verify personal-library collection names')
        current=int(value)
        if current>1000 or total is not None and current!=total:fail('ZOTERO_COLLECTION_INCOMPLETE','Collection inventory changed or exceeds the bounded target check')
        total=current;rows.extend(page);start+=len(page)
        if len(rows)>total or not page and len(rows)<total:fail('ZOTERO_COLLECTION_INCOMPLETE','Collection target lookup is incomplete')
    matches=[row for row in rows if row.get('data',row).get('name')==approved['collection_name']]
    if not matches:return {'status':'not_found','reason':'collection_missing','import_calls':0}
    if len(matches)!=1:return {'status':'conflict','reason':'collection_name_ambiguous','import_calls':0}
    collection=matches[0];key=readonly.item_key(collection)
    if approved.get('collection_key') is not None and key!=approved['collection_key']:return {'status':'permission_denied','reason':'collection_key_mismatch','import_calls':0}
    selected=helper.parse_body(helper.connector_post('/connector/getSelectedCollection',{}))
    if not isinstance(selected,dict):fail('ZOTERO_TARGET_INVALID','Connector selected-target response is not an object')
    if (selected.get('libraryID')!=1 or selected.get('name')!=approved['collection_name']
        or selected.get('libraryEditable') is not True or selected.get('editable') is not True
        or type(selected.get('id')) is not int or selected['id']<=0):return {'status':'permission_denied','reason':'collection_not_selected_or_not_editable','import_calls':0}
    for field in ['key','collectionKey']:
        if selected.get(field) is not None and selected[field]!=key:return {'status':'permission_denied','reason':'selected_collection_key_mismatch','import_calls':0}
    return {'status':'target_verified','collection_key':key,'collection_id':selected['id'],'library_id':1,'collection_name':approved['collection_name']}

def atomic_import(helper,guard,raw,target,session):
    # getSelectedCollection + import is not an atomic binding. No undocumented
    # target parameters are invented. Keep the real write closed until a proven
    # Connector route can bind the approved collection in the write itself.
    return {'status':'permission_denied','reason':'ZOTERO_TARGET_BINDING_UNSUPPORTED','code':'ZOTERO_TARGET_BINDING_UNSUPPORTED','import_calls':0}

def execute(payload):
    config=readonly.valid_scope(payload['readonly']);args=payload['args'];approved=payload['approved']
    citation_key=args['citation_key'];doi=args['doi'];raw=payload['source']
    expected=parse_source(raw,citation_key,doi)
    if readonly.digest(raw.encode())!=approved.get('source_sha256'):fail('ZOTERO_SOURCE_CHANGED','Approved bibliography payload differs from its pinned SHA256')
    if approved.get('library_id')!=1:fail('ZOTERO_SCOPE_DENIED','Only the explicitly approved personal library is supported')
    if citation_key not in config['items'] or readonly.normalize_doi(config['items'][citation_key]['doi'])!=readonly.normalize_doi(doi):fail('ZOTERO_SCOPE_DENIED','Import DOI is outside the project readonly scope')
    helper=readonly.load_helper(config);guard=ImportGuard(helper,time.monotonic()+35);helper.request=guard.request
    path='/api/users/0/items/top?'+helper.query({'q':readonly.normalize_doi(doi),'qmode':'everything','format':'json','limit':25});guard.allow(path)
    rows=readonly.bounded_rows(helper,helper.request(path));exact=[row for row in rows if isinstance(row,dict) and readonly.normalize_doi(row.get('data',row).get('DOI'))==readonly.normalize_doi(doi) and row.get('data',row).get('itemType') not in {'attachment','note','annotation'}]
    if len(exact)>1:return {'status':'conflict','reason':'duplicate_exact_doi','item_keys':[readonly.item_key(item) for item in exact],'import_calls':0}
    if exact:
        item=exact[0];changed=differences(item,expected)
        if changed:return {'status':'conflict','reason':'existing_metadata_mismatch','item_keys':[readonly.item_key(item)],'conflicting_fields':changed,'import_calls':0}
        info=readonly.metadata(helper,item,citation_key)
        pinned=approved.get('collection_key')
        membership=pinned in item.get('data',item).get('collections',[]) if pinned else None
        return {'status':'existing',**info,'item_key':readonly.item_key(item),
                'project_citation_key':citation_key,'zotero_item_key':readonly.item_key(item),
                'metadata_verified':True,'target_collection_key':pinned,
                'in_target_collection':membership,'target_membership_verified':pinned is not None,
                'target_collection_status':'member' if membership is True else 'not_member' if membership is False else 'unverified_unpinned',
                'attachments':{'status':'not_checked','local_bytes_verified':False,'fulltext_read':False},'import_calls':0}

    target=collection_target(helper,guard,approved)
    if target['status']!='target_verified':return target
    if args.get('phase')=='preflight':return {'status':'not_found','reason':'doi_absent','target_verified':True,'target':target,'import_calls':0}
    if args.get('phase')!='import' or args.get('authorized') is not True:fail('ZOTERO_PERMISSION_DENIED','Import phase requires explicit authorization')
    result=atomic_import(helper,guard,raw,target,args['session'])
    return result

def main():
    try:
        raw=sys.stdin.buffer.read(65537)
        if len(raw)>65536:fail('ZOTERO_INPUT_LIMIT','Import request exceeds 64 KiB')
        result=execute(json.loads(raw));reply={'ok':True,'result':result}
    except BaseException as error:
        reply={'ok':False,'error':{'code':error.code if isinstance(error,ReadError) else 'ZOTERO_IMPORT_WORKER_FAILED','message':str(error)[:300] if isinstance(error,ReadError) else 'Scoped Zotero import worker failed'}}
    sys.stdout.write(json.dumps(reply,ensure_ascii=False,separators=(',',':'))+'\n')

if __name__=='__main__':main()
