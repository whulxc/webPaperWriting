"""Standalone stdlib Windows worker for the explicitly scoped Zotero GET bridge."""
from __future__ import annotations
import hashlib,importlib.util,json,re,sys,time,unicodedata,urllib.error,urllib.parse,urllib.request
from pathlib import Path

LIMIT=2*1024*1024
BASE='http://127.0.0.1:23119'
KEY=re.compile(r'[A-Z0-9]{8}\Z')
CITE=re.compile(r'[A-Za-z][A-Za-z0-9:_-]{0,63}\Z')
DOI=re.compile(r'10\.\d{4,9}/[^\s{}\\\x00-\x1f]{1,180}\Z',re.I)
ACTIONS={'status','search','item','collections','attachments','export_preview'}
MAX_SCOPE_ITEMS=200
INPUT_LIMIT=512*1024
MAX_CREATORS=512

class ReadError(Exception):
    def __init__(self,code,message):self.code,self.message=code,message;super().__init__(message)
def fail(code,message):raise ReadError(code,message)
def digest(raw):return hashlib.sha256(raw).hexdigest()
def normalize_doi(value):
    if not isinstance(value,str):return ''
    return re.sub(r'^https?://(?:dx\.)?doi\.org/|^doi:\s*','',value.strip(),flags=re.I).lower()
def valid_scope(config):
    if not isinstance(config,dict) or not {'helper_path','helper_sha256','items'}<=set(config) or set(config)-{'helper_path','helper_sha256','items','collection_keys'}:fail('ZOTERO_CONFIG_INVALID','Exact readonly helper and item scope configuration required')
    if not re.fullmatch(r'[0-9a-f]{64}',config.get('helper_sha256','')):fail('ZOTERO_CONFIG_INVALID','Pinned helper SHA256 required')
    items=config['items']
    if not isinstance(items,dict) or not 1<=len(items)<=MAX_SCOPE_ITEMS:fail('ZOTERO_CONFIG_INVALID','One to 200 explicitly authorized citation keys required')
    collections=config.get('collection_keys',[])
    if not isinstance(collections,list) or len(collections)>32 or len(collections)!=len(set(collections)) or any(not isinstance(key,str) or not KEY.fullmatch(key) for key in collections):fail('ZOTERO_CONFIG_INVALID','Collection scope requires at most 32 explicit Zotero collection keys')
    seen=set();seen_keys=set()
    for key,item in items.items():
        if (not isinstance(key,str) or not CITE.fullmatch(key) or not isinstance(item,dict)
                or set(item)-{'doi','item_key','title','collection_keys'}):fail('ZOTERO_CONFIG_INVALID','Each citation key requires an exact DOI or pinned parent key and title')
        doi=normalize_doi(item.get('doi'))
        if item.get('doi') is not None and (not doi or not DOI.fullmatch(doi)):fail('ZOTERO_CONFIG_INVALID','Invalid authorized DOI')
        parent=item.get('item_key');title=item.get('title')
        if parent is not None:
            if (not isinstance(parent,str) or not KEY.fullmatch(parent) or not isinstance(title,str)
                    or not 1<=len(title)<=1000 or not title.strip() or any(ord(c)<32 for c in title)):
                fail('ZOTERO_CONFIG_INVALID','Pinned parents require an exact key and bounded title')
            if parent in seen_keys:fail('ZOTERO_CONFIG_INVALID','Duplicate authorized parent key')
            seen_keys.add(parent)
        elif title is not None:fail('ZOTERO_CONFIG_INVALID','A title cannot authorize an unpinned search')
        memberships=item.get('collection_keys',[])
        if (not isinstance(memberships,list) or len(memberships)>32
                or any(not isinstance(value,str) or not KEY.fullmatch(value) for value in memberships)
                or len(memberships)!=len(set(memberships))):
            fail('ZOTERO_CONFIG_INVALID','Optional parent membership scope requires at most 32 explicit Collection keys')
        if memberships and parent is None:fail('ZOTERO_CONFIG_INVALID','A Collection membership constraint requires a pinned parent key')
        if not doi and parent is None:fail('ZOTERO_CONFIG_INVALID','Non-DOI references require an exact pinned parent key and title')
        if doi:
            if doi in seen:fail('ZOTERO_CONFIG_INVALID','Duplicate authorized DOI')
            seen.add(doi)
    return config

def load_helper(config):
    path=Path(config['helper_path'])
    if not path.is_absolute() or path.is_symlink() or not path.is_file() or path.stat().st_nlink!=1:fail('ZOTERO_HELPER_INVALID','Pinned installed helper is not a regular file')
    raw=path.read_bytes()
    if len(raw)>256*1024 or digest(raw)!=config['helper_sha256']:fail('ZOTERO_HELPER_CHANGED','Installed helper SHA256 differs from project authorization')
    name='_paper_studio_installed_zotero';spec=importlib.util.spec_from_file_location(name,path)
    if spec is None or spec.loader is None:fail('ZOTERO_HELPER_INVALID','Cannot load pinned installed helper')
    helper=importlib.util.module_from_spec(spec);sys.modules[name]=helper
    # Execute the exact hash-verified bytes, avoiding a second unpinned read
    # and avoiding creation of helper __pycache__ files.
    exec(compile(raw,str(path),'exec'),helper.__dict__)
    return helper

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):fail('ZOTERO_REDIRECT_DENIED','Zotero redirects are disabled')

class Guard:
    def __init__(self,helper,deadline=None):
        self.helper=helper;self.allowed={('/api/',())};self.deadline=deadline if deadline is not None else time.monotonic()+35
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    @staticmethod
    def identity(path):
        if not isinstance(path,str) or len(path)>2048 or '\\' in path or any(ord(c)<32 for c in path):fail('ZOTERO_ROUTE_DENIED','Noncanonical API route')
        parsed=urllib.parse.urlsplit(path)
        if parsed.scheme or parsed.netloc or parsed.fragment or '..' in parsed.path.split('/'):fail('ZOTERO_ROUTE_DENIED','Only explicit relative local API routes are allowed')
        values=urllib.parse.parse_qsl(parsed.query,keep_blank_values=True,strict_parsing=True)
        if len({key for key,_ in values})!=len(values):fail('ZOTERO_ROUTE_DENIED','Duplicate API query fields')
        return parsed.path,tuple(sorted(values))
    def allow(self,path):self.allowed.add(self.identity(path));return path
    def request(self,path,*,method='GET',data=None,headers=None,timeout=5):
        if method!='GET' or data is not None or self.identity(path) not in self.allowed:fail('ZOTERO_ROUTE_DENIED','Only this action\'s explicitly authorized GET routes are allowed')
        if headers:fail('ZOTERO_ROUTE_DENIED','Caller-selected HTTP headers are disabled')
        remaining=self.deadline-time.monotonic()
        if remaining<=0:fail('ZOTERO_DEADLINE','Readonly Zotero worker total deadline exceeded')
        url=BASE+path;request=urllib.request.Request(url,method='GET',headers={'Zotero-API-Version':'3'})
        try:
            with self.opener.open(request,timeout=min(5,remaining)) as response:
                if response.geturl()!=url:fail('ZOTERO_REDIRECT_DENIED','Zotero response changed URL')
                if response.status!=200:fail('ZOTERO_HTTP_ERROR','Zotero local API did not return HTTP200')
                raw=response.read(LIMIT+1)
                if time.monotonic()>self.deadline:fail('ZOTERO_DEADLINE','Readonly Zotero worker total deadline exceeded')
                if len(raw)>LIMIT:fail('ZOTERO_RESPONSE_LIMIT','Zotero response exceeds 2 MiB')
                return self.helper.Response(200,dict(response.headers.items()),raw.decode('utf-8'))
        except ReadError:raise
        except urllib.error.HTTPError as error:
            if 300<=error.code<400:fail('ZOTERO_REDIRECT_DENIED','Zotero redirects are disabled')
            fail('ZOTERO_HTTP_ERROR','Zotero local API rejected the authorized GET')
        except (TimeoutError,OSError,urllib.error.URLError):fail('ZOTERO_UNAVAILABLE','Windows Zotero localhost API is unavailable or timed out')

def bounded_rows(helper,response):
    rows=helper.parse_body(response);headers={k.lower():v for k,v in response.headers.items()}
    total=headers.get('total-results')
    if not isinstance(rows,list) or len(rows)>25 or not isinstance(total,str) or not total.isdigit() or int(total)!=len(rows):
        fail('ZOTERO_INCOMPLETE_RESULTS','Zotero search/children response is not a complete bounded result')
    link=headers.get('link','')
    if link:
        for segment in link.split(','):
            if not re.search(r'<[^>]+>\s*;',segment) or not re.search(r'\brel\s*=\s*"?[A-Za-z ]+"?',segment):fail('ZOTERO_INCOMPLETE_RESULTS','Malformed pagination metadata')
        if re.search(r'\brel\s*=\s*"?[^";,]*\bnext\b',link,re.I):fail('ZOTERO_INCOMPLETE_RESULTS','Zotero pagination would require more than the authorized bounded request')
    return rows

def item_key(item):
    value=item.get('key') or item.get('data',{}).get('key')
    if not isinstance(value,str) or not KEY.fullmatch(value):fail('ZOTERO_METADATA_INVALID','Zotero returned an invalid item/collection key')
    return value

def text(value,maximum=1000):
    if not isinstance(value,str) or len(value)>maximum or any(ord(c)<32 for c in value):fail('ZOTERO_METADATA_INVALID','Unsupported or excessive bibliographic text')
    return value

def metadata(helper,item,citation_key):
    data=item.get('data',item);summary=helper.summarize_item(item)
    result={'citation_key':citation_key,'zotero_item_key':item_key(item),'item_type':text(data.get('itemType','')),
            'title':text(summary.get('title') or ''),'creators':[text(value,300) for value in summary.get('creators',[])],
            'year':summary.get('year'),'doi':normalize_doi(data.get('DOI')),
            'parent_item_key':None,'library':{'type':'user','local_id':0}}
    version=item.get('version',data.get('version'))
    if version is not None:
        if type(version) is not int or version<0:fail('ZOTERO_METADATA_INVALID','Invalid parent item version')
        result['item_version']=version
    library=item.get('library')
    if isinstance(library,dict) and library.get('type')=='user' and type(library.get('id')) is int:
        result['library']['id']=library['id']
    if len(result['creators'])>MAX_CREATORS:fail('ZOTERO_METADATA_INVALID','Creator list exceeds the explicit 512-name bound')
    for source,target in [('publicationTitle','publication_title'),('volume','volume'),('issue','issue'),('pages','pages'),('date','date'),('publisher','publisher')]:
        if data.get(source):result[target]=text(data[source])
    if not result.get('publication_title') and data.get('proceedingsTitle'):
        result['publication_title']=text(data['proceedingsTitle'])
    keys=[]
    if data.get('citationKey'):
        value=data['citationKey']
        if isinstance(value,str) and CITE.fullmatch(value):keys.append({'value':value,'source':'data.citationKey'})
    extra=data.get('extra','')
    if isinstance(extra,str):
        for line in extra.splitlines():
            match=re.fullmatch(r'\s*Citation Key:\s*([A-Za-z][A-Za-z0-9:_-]{0,63})\s*',line,re.I)
            if match:keys.append({'value':match.group(1),'source':'data.extra: Citation Key'})
    if keys:result['library_citation_keys']=keys[:4]
    result['persistent_citation_key_status']='observed_metadata' if keys else 'unknown_not_exposed'
    result['citation_key_source']='project_configuration'
    return result

def safe_bibtex(helper,guard,item,citation_key,info):
    key=item_key(item);path=f'/api/users/0/items?'+helper.query({'itemKey':key,'format':'bibtex','limit':helper.API_PAGE_LIMIT})
    guard.allow(path)
    raw=helper.export_bibtex(key);export_keys=helper.extract_bibtex_keys(raw)
    if len(raw.encode())>LIMIT or len(export_keys)!=1:fail('ZOTERO_EXPORT_INVALID','Installed helper did not export exactly the authorized item')
    # Never forward raw exported fields (file, note, URL, arbitrary TeX macros).
    # A small metadata subset is rendered without interpreting TeX syntax.
    def safe(value):
        value=text(value,1000)
        if re.search(r'[\\{}$%#_^~]',value):fail('ZOTERO_EXPORT_UNSUPPORTED','Complex or TeX-active bibliographic metadata is unsupported by this readonly preview')
        return value.replace('&',r'\&')
    kind={'journalArticle':'article','conferencePaper':'inproceedings','book':'book',
          'preprint':'misc','report':'misc','document':'misc','manuscript':'misc'}.get(info['item_type'])
    if kind is None:fail('ZOTERO_EXPORT_UNSUPPORTED','This bibliographic item type is unsupported by the safe export preview')
    fields={'title':safe(info['title'])}
    if info['doi']:fields['doi']=safe(info['doi'])
    data=item.get('data',item);authors=[]
    for creator in data.get('creators',[]):
        if creator.get('creatorType') not in {'author',None}:continue
        if creator.get('name'):
            # Preserve single-field creators literally; do not guess whether
            # they are people or organizations or split/reorder their names.
            authors.append('{'+safe(creator['name'])+'}');continue
        first=safe(creator.get('firstName',''));last=safe(creator.get('lastName',''))
        if not last or ' and ' in first.lower() or ' and ' in last.lower():fail('ZOTERO_EXPORT_UNSUPPORTED','Complex creator metadata is unsupported')
        authors.append(last+(', '+first if first else ''))
    if authors:fields['author']=' and '.join(authors)
    for source,target in [('year','year'),('publication_title','journal' if kind=='article' else 'booktitle'),('volume','volume'),('issue','number'),('pages','pages'),('publisher','publisher')]:
        if info.get(source):fields[target]=safe(str(info[source]))
    bib='@'+kind+'{'+citation_key+',\n'+''.join('  '+name+' = {'+value+'},\n' for name,value in fields.items())+'}\n'
    return {'citation_key':citation_key,'zotero_item_key':key,'bibtex':bib,'sha256':digest(bib.encode()),'written':False,
            'native_export_keys':export_keys,'persistent_citation_key_status':info['persistent_citation_key_status'],
            'native_export_key_matches_persistent':(all(row['value'] in export_keys for row in info['library_citation_keys'])
                                                     if info.get('library_citation_keys') else None),
            'citation_key_preserved':True,'export_key_matches_project':export_keys==[citation_key],
            'source':'installed_helper_single_item_export_and_whitelisted_metadata','raw_export_forwarded':False}

def normalized_title(value):
    return ' '.join(unicodedata.normalize('NFKC',value).casefold().split())

def resolve_item(helper,guard,config,reference):
    """Use direct parent GET for pinned scopes; DOI-only legacy scopes stay bounded."""
    doi=normalize_doi(reference.get('doi'));parent=reference.get('item_key')
    if parent:
        path=f'/api/users/0/items/{parent}';guard.allow(path)
        response=helper.request(path)
        item=helper.parse_body(response)
        if not isinstance(item,dict) or item_key(item)!=parent:fail('ZOTERO_IDENTITY_CONFLICT','Pinned Zotero parent identity changed')
        data=item.get('data',item)
        if (data.get('itemType') in {'attachment','note','annotation'} or data.get('parentItem')
                or not isinstance(data.get('title'),str)
                or normalized_title(data['title'])!=normalized_title(reference['title'])):
            fail('ZOTERO_IDENTITY_CONFLICT','Pinned Zotero parent title or type differs from project authorization')
        if doi and normalize_doi(data.get('DOI'))!=doi:fail('ZOTERO_IDENTITY_CONFLICT','Pinned parent DOI differs from project authorization')
        # An exact project-authorized parent key/title is sufficient identity.
        # Per-parent Collection keys optionally narrow that authorization;
        # top-level keys authorize collection metadata, not every parent's membership.
        allowed=set(reference.get('collection_keys',[]));membership=data.get('collections',[])
        if not isinstance(membership,list) or any(not isinstance(k,str) or not KEY.fullmatch(k) for k in membership):
            fail('ZOTERO_METADATA_INVALID','Invalid collection membership')
        if allowed and not allowed.intersection(membership):fail('ZOTERO_SCOPE_DENIED','Pinned parent is outside the authorized project collections')
        return [item]
    path='/api/users/0/items/top?'+helper.query({'q':doi,'qmode':'everything','format':'json','limit':25})
    guard.allow(path);rows=bounded_rows(helper,helper.request(path))
    return [item for item in rows if isinstance(item,dict) and normalize_doi(item.get('data',item).get('DOI'))==doi
            and item.get('data',item).get('itemType') not in {'attachment','note','annotation'} and not item.get('data',item).get('parentItem')]

def execute(payload,helper=None):
    if not isinstance(payload,dict):fail('ZOTERO_CONFIG_INVALID','Object request required')
    deadline=time.monotonic()+35
    config=valid_scope(payload['config']);args=payload['args'];action=args.get('action')
    if action not in ACTIONS:fail('ZOTERO_ACTION_DENIED','Unsupported readonly action')
    citation_key=args.get('citation_key')
    if citation_key is not None and citation_key not in config['items']:fail('ZOTERO_SCOPE_DENIED','Citation key is outside the project authorization')
    if action not in {'status','search'} and citation_key is None:fail('ZOTERO_SCOPE_DENIED','This action requires an explicitly authorized citation_key')
    query=args.get('query','')
    if not isinstance(query,str) or len(query)>256:fail('ZOTERO_INVALID_ARGUMENT','Bounded local query text required')
    helper=helper or load_helper(config);guard=Guard(helper,deadline);helper.request=guard.request
    if action=='status':
        response=helper.request('/api/');headers={k.lower():v for k,v in response.headers.items()}
        return {'status':'ready','local_api_http_status':response.status,'zotero_version':headers.get('x-zotero-version') or headers.get('zotero-version'),
                'api_version':headers.get('zotero-api-version') or headers.get('x-zotero-api-version'),
                'schema_version':headers.get('zotero-schema-version') or headers.get('x-zotero-schema-version'),
                'authorized_scope':[{'citation_key':key,**value} for key,value in config['items'].items()],
                'library_search_performed':False,'read_only':True}
    selected=[citation_key] if citation_key else list(config['items'])
    results=[]
    for key in selected:
        doi=normalize_doi(config['items'][key].get('doi'));exact=resolve_item(helper,guard,config,config['items'][key])
        if not exact:results.append({'citation_key':key,'doi':doi,'status':'not_found'});continue
        if len(exact)>1:results.append({'citation_key':key,'doi':doi,'status':'ambiguous','exact_match_count':len(exact)});continue
        item=exact[0];info=metadata(helper,item,key)
        info['identity_lookup']='pinned_parent_get' if config['items'][key].get('item_key') else 'exact_doi_search'
        info['collection_membership_checked']=bool(config['items'][key].get('collection_keys'))
        if query and query.casefold() not in json.dumps(info,ensure_ascii=False).casefold():continue
        if action=='search':results.append({'status':'found',**info});continue
        if action=='item':return {'status':'found',**info}
        if action=='collections':
            collection_keys=item.get('data',item).get('collections',[])
            if not isinstance(collection_keys,list) or len(collection_keys)>25:fail('ZOTERO_METADATA_INVALID','Collection membership exceeds scope limit')
            output=[];excluded=0
            allowed_collections=set(config.get('collection_keys',[]))
            for collection in collection_keys:
                if not isinstance(collection,str) or not KEY.fullmatch(collection):fail('ZOTERO_METADATA_INVALID','Invalid collection key')
                if collection not in allowed_collections:
                    excluded+=1;continue
                path=f'/api/users/0/collections/{collection}';guard.allow(path);value=helper.parse_body(helper.request(path))
                if not isinstance(value,dict) or item_key(value)!=collection:fail('ZOTERO_METADATA_INVALID','Collection identity mismatch')
                summarized=helper.summarize_collection(value)
                output.append({'key':collection,'name':text(summarized.get('name') or ''),'source':'authorized_item_membership'})
            return {'status':'found','citation_key':key,'collections':output,'excluded_membership_count':excluded}
        if action=='attachments':
            path=f'/api/users/0/items/{item_key(item)}/children?'+helper.query({'format':'json','limit':25});guard.allow(path)
            children=bounded_rows(helper,helper.request(path));output=[]
            for child in children:
                if not isinstance(child,dict):continue
                data=child.get('data',child)
                if data.get('itemType')!='attachment' or data.get('parentItem')!=item_key(item):continue
                mime=data.get('contentType','');mode=data.get('linkMode','')
                if not isinstance(mime,str) or not re.fullmatch(r'[A-Za-z0-9.+_-]+/[A-Za-z0-9.+_-]+',mime):mime=None
                if mode not in {'imported_file','imported_url','linked_file','linked_url','embedded_image'}:mode=None
                output.append({'key':item_key(child),'mime_type':mime,'link_mode':mode,'availability':'metadata_only','local_bytes_verified':False})
            return {'status':'found','citation_key':key,'attachments':output,'local_bytes_verified':False}
        if action=='export_preview':return {'status':'found',**safe_bibtex(helper,guard,item,key,info)}
    if action=='search':return {'status':'found' if any(row['status']=='found' for row in results) else 'ambiguous' if any(row['status']=='ambiguous' for row in results) else 'not_found','results':results,'query_applied_locally':True,'read_only':True}
    return results[0] if results else {'citation_key':citation_key,'status':'not_found'}

def main():
    try:
        raw=sys.stdin.buffer.read(INPUT_LIMIT+1)
        if len(raw)>INPUT_LIMIT:fail('ZOTERO_INPUT_LIMIT','Worker request exceeds 512 KiB')
        result=execute(json.loads(raw));reply={'ok':True,'result':result}
    except BaseException as error:
        reply={'ok':False,'error':{'code':error.code if isinstance(error,ReadError) else 'ZOTERO_WORKER_FAILED',
                                 'message':error.message if isinstance(error,ReadError) else 'Readonly worker failed before returning authorized metadata'}}
    sys.stdout.write(json.dumps(reply,ensure_ascii=False,separators=(',',':'))+'\n')

if __name__=='__main__':main()
