"""Free Crossref metadata adapter; candidates never become confirmed references.

Official API: https://www.crossref.org/documentation/retrieve-metadata/rest-api/
Link limitation: https://www.crossref.org/documentation/retrieve-metadata/text-and-data-mining/
Only public metadata is retrieved. No source links, full text, Zotero writes,
credentials, paid API pool, alternate providers or incoming-citation crawling.
"""
from __future__ import annotations
import datetime,hashlib,http.client,ipaddress,json,os,re,socket,ssl,threading,time,urllib.parse
from pathlib import Path
from paper_studio.security.policy import StudioError,atomic_json,encoded,fail,no_links,safe_read,sha

HOST='api.crossref.org'
ORIGIN='https://'+HOST
USER_AGENT='webcodex-paper-studio/literature-crossref (public scholarly metadata; no API key)'
MAX_RESPONSE=2*1024*1024
CACHE_TTL=3600
TOOLS={'paper_literature_search','paper_reference_resolve','paper_citation_graph','paper_open_access_lookup'}
CAPABILITIES={'crossref':{'search':'available','doi_resolve':'available','outgoing_references':'available_when_deposited',
                         'incoming_citations':'unsupported','open_access_verification':'unsupported','license_and_link_metadata':'available_when_deposited'},
              'openalex':{'status':'deferred_not_enabled'},'semantic_scholar':{'status':'deferred_not_enabled'},
              'arxiv':{'status':'deferred_not_enabled'},'unpaywall':{'status':'deferred_not_enabled'},
              'europe_pmc':{'status':'deferred_not_enabled'}}


def utc_now():return datetime.datetime.now(datetime.timezone.utc).isoformat()
def doi(value):
    if not isinstance(value,str) or len(value)>512:fail('LITERATURE_INVALID_ARGUMENT','A bounded DOI is required')
    value=re.sub(r'^https?://(?:dx\.)?doi\.org/|^doi:\s*','',value.strip(),flags=re.I)
    if not re.fullmatch(r'10\.\d{4,9}/[^\s\x00-\x20\x7f]+',value,re.I) or any(char in value for char in ['\\','?','#']) or any(part in {'.','..'} for part in value.split('/')):fail('LITERATURE_INVALID_ARGUMENT','Expected a DOI, not an arbitrary URL or query')
    return value.lower()

def bounded_text(value,limit=1500):
    if not isinstance(value,str):return None
    return ''.join(char for char in value if ord(char)>=32 and ord(char)!=127)[:limit]

def public_link(value):
    if not isinstance(value,str) or len(value)>2048 or any(ord(c)<33 for c in value):return None
    try:
        parsed=urllib.parse.urlsplit(value)
        if parsed.scheme not in {'http','https'} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment or parsed.port not in {None,80,443}:return None
        host=parsed.hostname.lower().rstrip('.')
        if host=='localhost' or host.endswith(('.localhost','.local','.internal')) or '.' not in host:return None
        try:
            if not ipaddress.ip_address(host).is_global:return None
        except ValueError:pass
        return value
    except (ValueError,UnicodeError):return None

def _private(path):
    no_links(path);path.mkdir(parents=True,exist_ok=True,mode=0o700)
    info=path.stat()
    if info.st_uid!=os.getuid() or info.st_mode&0o077:fail('LITERATURE_CACHE_PERMISSIONS','Literature cache must be private and owned')

class Crossref:
    def __init__(self,cfg):
        self.cfg=cfg;self.started=time.monotonic();self.deadline=self.started+32
        self.budget=min(4,cfg['literature']['max_requests']);self.requests_used=0;self.cache_hits=0
        self.cache=no_links(Path(cfg['state_root'])/'literature-cache');_private(self.cache)
    def remaining(self):
        value=self.deadline-time.monotonic()
        if value<=0:fail('LITERATURE_DEADLINE','Public metadata call exceeded its total deadline')
        return min(8,value)
    def _exchange(self,path):
        """Pin an approved public IP while preserving HTTPS hostname/SNI checks."""
        timeout=self.remaining()
        resolved=[];errors=[];done=threading.Event()
        def resolve():
            try:resolved.extend(socket.getaddrinfo(HOST,443,type=socket.SOCK_STREAM))
            except OSError as error:errors.append(error)
            finally:done.set()
        # libc DNS has no socket timeout. Only a daemon resolver may outlive
        # this bounded wait; it cannot initiate an HTTP request afterward.
        threading.Thread(target=resolve,daemon=True).start()
        if not done.wait(timeout) or errors:fail('LITERATURE_NETWORK_ERROR','Crossref DNS resolution failed or timed out')
        addresses=resolved
        if not addresses:fail('LITERATURE_NETWORK_ERROR','Crossref DNS returned no address')
        checked=[]
        for _,_,_,_,address in addresses:
            try:public=ipaddress.ip_address(address[0]).is_global
            except ValueError:public=False
            if not public:fail('LITERATURE_ADDRESS_DENIED','Crossref resolved to a nonpublic address')
            checked.append(address[0])
        pinned=checked[0]
        connection=http.client.HTTPSConnection(HOST,443,timeout=min(timeout,self.remaining()),context=ssl.create_default_context())
        connection._create_connection=lambda address,timeout,source_address=None:socket.create_connection((pinned,443),timeout,source_address)
        try:
            connection.request('GET',path,headers={'User-Agent':USER_AGENT,'Accept':'application/json','Host':HOST,'Accept-Encoding':'identity'})
            connected_socket=connection.sock
            connected_socket.settimeout(self.remaining())
            response=connection.getresponse()
            if 300<=response.status<400:fail('LITERATURE_REDIRECT_DENIED','Crossref redirects are disabled')
            if response.status==404:fail('LITERATURE_NOT_FOUND','Crossref has no record for the requested DOI')
            if response.status!=200:fail('LITERATURE_HTTP_ERROR','Crossref metadata API rejected the request',http_status=response.status)
            if 'json' not in response.getheader('Content-Type','').lower():fail('LITERATURE_PROTOCOL','Crossref did not return JSON metadata')
            length=response.getheader('Content-Length')
            if length is not None and (not length.isdigit() or int(length)>MAX_RESPONSE):fail('LITERATURE_RESPONSE_LIMIT','Crossref response exceeds 2 MiB')
            chunks=[];size=0
            while True:
                connected_socket.settimeout(self.remaining())
                block=response.read1(min(65536,MAX_RESPONSE+1-size))
                if not block:break
                chunks.append(block);size+=len(block)
                if size>MAX_RESPONSE:fail('LITERATURE_RESPONSE_LIMIT','Crossref response exceeds 2 MiB')
            self.remaining();return b''.join(chunks)
        except StudioError:raise
        except (OSError,http.client.HTTPException):fail('LITERATURE_NETWORK_ERROR','Crossref HTTPS metadata request failed or timed out')
        finally:connection.close()
    def get(self,path):
        # No public method accepts a URL, another authority, or an arbitrary
        # endpoint. Search and DOI paths are constructed by this module only.
        parsed=urllib.parse.urlsplit(path)
        if parsed.scheme or parsed.netloc or parsed.fragment or '\\' in path:fail('LITERATURE_URL_DENIED','Only fixed Crossref works routes are permitted')
        if parsed.path=='/works':
            parameters=urllib.parse.parse_qs(parsed.query,keep_blank_values=True)
            if set(parameters)!={'rows','query.bibliographic'} or any(len(value)!=1 for value in parameters.values()) or not parameters['rows'][0].isdigit() or not 1<=int(parameters['rows'][0])<=10:
                fail('LITERATURE_URL_DENIED','Only bounded bibliographic search parameters are permitted')
        elif parsed.path.startswith('/works/') and not parsed.query:
            suffix=parsed.path[len('/works/'):]
            try:canonical=urllib.parse.quote(doi(urllib.parse.unquote(suffix)),safe='')
            except StudioError:fail('LITERATURE_URL_DENIED','Only one exact encoded DOI is permitted')
            if canonical!=suffix:fail('LITERATURE_URL_DENIED','Noncanonical DOI route')
        else:fail('LITERATURE_URL_DENIED','Only fixed Crossref works routes are permitted')
        key=sha(encoded([self.cfg['project_id'],'crossref',path]));target=self.cache/(key+'.json')
        if target.exists():
            cached=json.loads(safe_read(self.cache,target.name,4*1024*1024))
            if (cached.get('project_id')==self.cfg['project_id'] and cached.get('provider')=='crossref' and cached.get('path')==path
                and type(cached.get('cached_at')) in {int,float} and 0<=time.time()-cached['cached_at']<CACHE_TTL):
                raw=cached.get('raw','').encode()
                if len(raw)<=MAX_RESPONSE and sha(raw)==cached.get('response_sha256'):
                    self.cache_hits+=1
                    return self.decode(raw),{'source_url':ORIGIN+path,'provider':'crossref','query_time':cached['query_time'],'cache_hit':True}
        if self.requests_used>=self.budget:fail('LITERATURE_REQUEST_BUDGET','Per-call API request budget exhausted')
        self.requests_used+=1;raw=self._exchange(path);data=self.decode(raw);queried=utc_now()
        atomic_json(target,{'schema':'paper-studio.literature-cache.v1','project_id':self.cfg['project_id'],'provider':'crossref',
                           'path':path,'cached_at':time.time(),'query_time':queried,'response_sha256':sha(raw),'raw':raw.decode('utf-8')})
        return data,{'source_url':ORIGIN+path,'provider':'crossref','query_time':queried,'cache_hit':False}
    @staticmethod
    def decode(raw):
        try:data=json.loads(raw)
        except (ValueError,UnicodeError):fail('LITERATURE_PROTOCOL','Crossref returned invalid JSON')
        if not isinstance(data,dict) or data.get('status')!='ok' or not isinstance(data.get('message'),dict):fail('LITERATURE_PROTOCOL','Invalid Crossref response envelope')
        return data


def reference(item,provenance,*,expected_doi=None,search=False):
    if not isinstance(item,dict):fail('LITERATURE_PROTOCOL','Crossref work is not an object')
    identity=doi(item.get('DOI'))
    if expected_doi is not None and identity!=expected_doi:fail('LITERATURE_IDENTITY_MISMATCH','Crossref returned a different DOI than requested')
    titles=item.get('title',[]);publications=item.get('container-title',[])
    title=bounded_text(titles[0]) if isinstance(titles,list) and titles else None
    publication=bounded_text(publications[0]) if isinstance(publications,list) and publications else None
    authors=[]
    for author in item.get('author',[])[:100]:
        if not isinstance(author,dict):continue
        name=bounded_text(author.get('name') or ' '.join(part for part in [author.get('given'),author.get('family')] if isinstance(part,str)),300)
        if name:authors.append(name)
    years={}
    for field in ['published','published-print','published-online','issued']:
        value=item.get(field,{})
        if isinstance(value,dict):
            parts=value.get('date-parts',[])
            if isinstance(parts,list) and parts and isinstance(parts[0],list) and parts[0] and type(parts[0][0]) is int and 1000<=parts[0][0]<=3000:years[field]=parts[0][0]
    year=next(iter(years.values()),None);conflicts=[]
    if len(set(years.values()))>1:conflicts.append({'field':'year','provider_values':years,'reason':'Crossref publication date fields differ'})
    version=bounded_text(item.get('version'),200)
    return {**provenance,'title':title,'authors':authors,'year':year,'publication':publication,'doi':identity,
            'version':version,'work_type':bounded_text(item.get('type'),100),'confidence':{'level':'candidate','basis':'provider_search_result' if search else 'exact_doi_provider_record'},
            'conflicts':conflicts,'confirmation_status':'candidate','scientific_support_verified':False,
            'metadata_complete':bool(title and authors and year and publication)}


def _limit(args):
    maximum=args.get('max_results',5)
    if type(maximum) is not int or not 1<=maximum<=10:fail('LITERATURE_INVALID_ARGUMENT','max_results must be 1..10')
    return maximum


def dispatch(cfg,name,args):
    config=cfg.get('literature')
    if not isinstance(config,dict) or config.get('enabled') is not True:fail('LITERATURE_NOT_CONFIGURED','Public literature metadata is not enabled for this project')
    if (config.get('providers')!=['crossref'] or type(config.get('max_requests')) is not int or not 1<=config['max_requests']<=4):fail('LITERATURE_CONFIG_INVALID','Only the bounded public Crossref provider is supported')
    if name not in TOOLS:fail('LITERATURE_TOOL_DENIED','Unknown public literature tool')
    if args.get('project_id',cfg['project_id'])!=cfg['project_id']:fail('PROJECT_DENIED','Literature scope belongs to another project')
    if args.get('provider','crossref')!='crossref':fail('LITERATURE_PROVIDER_UNAVAILABLE','Requested provider is not enabled',capabilities=CAPABILITIES)
    maximum=_limit(args)
    client=Crossref(cfg)
    if name=='paper_literature_search':
        query=args.get('query')
        if not isinstance(query,str) or not 1<=len(query.strip())<=500 or any(ord(char)<32 for char in query):fail('LITERATURE_INVALID_ARGUMENT','A bounded plain-text bibliographic query is required')
        path='/works?'+urllib.parse.urlencode({'query.bibliographic':query.strip(),'rows':maximum})
        data,provenance=client.get(path);items=data['message'].get('items')
        if not isinstance(items,list) or len(items)>maximum:fail('LITERATURE_PROTOCOL','Crossref search exceeded the requested result bound')
        result={'status':'found' if items else 'not_found','query':query.strip(),'candidates':[reference(item,provenance,search=True) for item in items],
                'total_provider_results':data['message'].get('total-results'),'max_results':maximum,**provenance}
    else:
        identity=doi(args.get('doi'));path='/works/'+urllib.parse.quote(identity,safe='')
        data,provenance=client.get(path);item=data['message'];record=reference(item,provenance,expected_doi=identity)
        if name=='paper_reference_resolve':result={'status':'found','reference':record,**provenance}
        elif name=='paper_citation_graph':
            deposited=item.get('reference');deposited=[] if deposited is None else deposited
            if not isinstance(deposited,list):fail('LITERATURE_PROTOCOL','Crossref reference field is not a list')
            outgoing=[]
            for entry in deposited[:maximum]:
                if not isinstance(entry,dict):fail('LITERATURE_PROTOCOL','Crossref reference entry is not an object')
                target=None
                if entry.get('DOI'):
                    try:target=doi(entry['DOI'])
                    except StudioError:pass
                outgoing.append({'source_doi':identity,'target_doi':target,'title':bounded_text(entry.get('article-title')),
                                 'authors':[bounded_text(entry['author'],300)] if entry.get('author') else [],
                                 'year':bounded_text(str(entry['year']),20) if entry.get('year') else None,
                                 'publication':bounded_text(entry.get('journal-title')),'version':None,
                                 **provenance,'confidence':{'level':'candidate','basis':'deposited_reference_not_independently_resolved'},'conflicts':[],
                                 'confirmation_status':'candidate'})
            result={'status':'found','reference':record,'outgoing':outgoing,'incoming':{'status':'unsupported','reason':'Crossref public work metadata supplies no incoming citation identities'},
                    'deposited_reference_count':len(deposited),'reported_reference_count':item.get('reference-count'),
                    'returned_count':len(outgoing),'truncated':len(deposited)>maximum,'graph_complete':False,**provenance}
        else:
            licenses=[];links=[]
            for entry in item.get('license',[])[:25]:
                if not isinstance(entry,dict):continue
                url=public_link(entry.get('URL'))
                if url:licenses.append({'url':url,'applies_to':bounded_text(entry.get('content-version'),100),'delay_days':entry.get('delay-in-days') if type(entry.get('delay-in-days')) is int else None})
            for entry in item.get('link',[])[:25]:
                if not isinstance(entry,dict):continue
                url=public_link(entry.get('URL'))
                if url:links.append({'url':url,'content_type':bounded_text(entry.get('content-type'),100),'content_version':bounded_text(entry.get('content-version'),100),'intended_application':bounded_text(entry.get('intended-application'),100)})
            result={'status':'metadata_only','reference':record,'open_access':'unknown','availability_verified':False,
                    'license_records':licenses,'link_records':links,'fulltext_downloaded':False,
                    'limitation':'Crossref links and license metadata do not establish open access, accessible full text, or permission to reuse a particular version.',**provenance}
    return {'project_id':cfg['project_id'],**result,'requests_used':client.requests_used,'cache_hits':client.cache_hits,
            'api_key_used':False,'paid_service_used':False,'library_written':False,'provider_capabilities':CAPABILITIES}
