"""Expose a rendered, project-bound redline as a verified MCP resource."""
import base64
import json
import re

from paper_studio.capabilities import require_admitted
from paper_studio.security.policy import fail, select_project, sha

PREFIX = 'paper-review://'
_LABEL = r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}'
_DISPLAY_NAME = r'preview(?:[-_](?:en|zh))?\.(?:pdf|html)'
_LOCATOR = re.compile(rf'{PREFIX}(?P<project>{_LABEL})/(?P<proposal>{_LABEL})/(?P<render>{_LABEL})/'
                      rf'(?P<name>{_DISPLAY_NAME})(?:/content/(?P<offset>[0-9]{{1,12}})/(?P<limit>[1-9][0-9]{{0,5}}))?')


def read_locator(cfg,args):
    """Compatibility read of an already rendered review; never renders or records a decision."""
    from paper_studio.versions.review_proposals import dispatch
    require_admitted(cfg,'paper_review_proposal')
    items = args['items']
    if len(items) != 1 or set(items[0]) != {'path'}:
        fail('INVALID_ARGUMENT','Read one exact rendered review per request')
    match = _LOCATOR.fullmatch(items[0]['path'])
    if not match or match['project'] != cfg['project_id']:
        fail('INVALID_ARGUMENT','Expected a rendered review URI for this project')
    limit = int(match['limit'] or 1)
    budget = args.get('max_result_bytes',262144)
    if limit > 262144 or 4*((limit+2)//3)>budget:
        fail('INPUT_LIMIT','Select a smaller review file range')
    part = dispatch(cfg,{'action':'get','part':'artifact','proposal_id':match['proposal'],'render_id':match['render'],
                         'artifact_path':match['name'],'offset':int(match['offset'] or 0),'limit':limit})
    if not match['limit']:
        part = {k:part[k] for k in ('sha256','total','mime_type','render_id','proposal_id') if k in part}
    if len(json.dumps(part,ensure_ascii=False).encode()) > budget:
        fail('OUTPUT_LIMIT','Select a smaller review file range')
    return part


def issue(cfg, rendered, registry):
    result = dict(rendered)
    artifacts = []
    links = []
    linked = set()
    order = {'preview-zh.pdf':0, 'preview_zh.pdf':0, 'preview-en.pdf':1, 'preview_en.pdf':1, 'preview.pdf':2}
    for metadata in sorted(rendered['artifacts'], key=lambda item: order.get(item['path'], 3)):
        name = metadata['path']
        if not re.fullmatch(_DISPLAY_NAME, name):
            continue
        item = {k: metadata[k] for k in ('path', 'mime_type', 'bytes', 'sha256')}
        uri = f"paper-review://{cfg['project_id']}/{rendered['proposal_id']}/{rendered['render_id']}/{name}"
        item['uri'] = uri
        artifacts.append(item)
        # The transport includes the URI and base64 in a 3 MiB JSON envelope.
        if 4*((item['bytes']+2)//3) + len(uri.encode()) + 4096 > 3 * 1024**2:
            item['delivery'] = 'chunked'
            continue
        registry[uri] = {**item, 'project_id':cfg['project_id'], 'kind':'review',
                         'proposal_id':rendered['proposal_id'], 'render_id':rendered['render_id'],
                         'generation':cfg['_config_revision']}
        if item['sha256'] not in linked:
            links.append({'type':'resource_link','uri':uri,'name':name,'mimeType':item['mime_type'],'size':item['bytes']})
            linked.add(item['sha256'])
    result['artifacts'] = artifacts
    guidance = ('Open the review file to see marked changes.' if links else
                'Use get(part=artifact) with the issued identifiers to retrieve the review in chunks.')
    return {'_mcp_result':{'structuredContent':result,'content':[
        {'type':'text','text':guidance}, *links], 'isError':False}}


def read(cfg, record):
    from paper_studio.versions.review_proposals import dispatch
    project = select_project(cfg,record['project_id'])
    require_admitted(project, 'paper_review_proposal')
    if project['_config_revision'] != record['generation']:
        fail('RESOURCE_DENIED','The selected project binding changed')
    body = bytearray()
    while len(body) < record['bytes']:
        part = dispatch(project, {'action':'get','part':'artifact','proposal_id':record['proposal_id'],
                                  'render_id':record['render_id'],'artifact_path':record['path'],
                                  'offset':len(body),'limit':min(131072,record['bytes']-len(body))})
        chunk = base64.b64decode(part['data_base64'],validate=True)
        if (part['sha256'] != record['sha256'] or sha(chunk) != part['chunk_sha256'] or
                not 0 < len(chunk) <= min(131072, record['bytes']-len(body))):
            fail('RESOURCE_INTEGRITY','Review resource chunk differs from its issued identity')
        body.extend(chunk)
    if sha(body) != record['sha256']:
        fail('RESOURCE_INTEGRITY','Rendered review bytes differ from the issued resource')
    return {'contents':[{'uri':record['uri'], 'mimeType':record['mime_type'],
                         'blob':base64.b64encode(body).decode('ascii')}]}
