"""Existing paper_read_files schema: explicit project-private history locators."""
import json
import re
from paper_studio.capabilities import require_admitted
from paper_studio.gateway.target_lookup import _observed
from paper_studio.security.policy import fail

PREFIX='paper-history://'
LABEL=r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}'
LOCATOR=re.compile(rf'{PREFIX}(?P<version>{LABEL})/(?P<revision>{LABEL}|~)/(?P<language>en|zh|shared|unknown)/'
                   r'(?P<role>[a-z][a-z0-9_]{0,39})(?:/(?:(?P<figure_number>[1-9][0-9]{0,5})|key/(?P<target_key>[A-Za-z0-9][A-Za-z0-9._-]{0,95})))?'
                   r'(?:/object/(?P<object_id>[0-9a-f]{64}))?'
                   r'(?:/content/(?P<offset>[0-9]{1,12})/(?P<limit>[1-9][0-9]{0,5}))?')


def parse(item):
    if not isinstance(item,dict) or set(item)!={'path'}:
        fail('HISTORY_ARGUMENT','A historical locator has no filesystem line options')
    match=LOCATOR.fullmatch(item['path'])
    if not match:fail('HISTORY_ARGUMENT','Use paper-history://version/revision-or-~/language/role[/figure|/key/name][/object/id][/content/offset/limit]')
    values={key:value for key,value in match.groupdict().items() if value is not None}
    if values['revision']=='~':values['revision']=None
    for key in ('figure_number','offset','limit'):
        if key in values:values[key]=int(values[key])
    return values


def read_locator(cfg,args):
    require_admitted(cfg,'paper_read_files')
    from paper_studio.versions.history_objects import resolve
    items=args['items'];selectors=[parse(item) for item in items]
    budget=args.get('max_result_bytes',262144)
    if len(items)>1 and any('offset' in item for item in selectors):
        fail('HISTORY_ARGUMENT','Read at most one content chunk; metadata supports up to eight exact locators')
    for item in selectors:
        if item.get('limit',0)>262144 or 4*((item.get('limit',0)+2)//3)>budget:
            fail('INPUT_LIMIT','Choose a smaller history chunk for this reply budget')
    values=[{'path':item['path'],**_observed(resolve,cfg,**selector)} for item,selector in zip(items,selectors)]
    result={'project_id':cfg['project_id'],'config_revision':cfg.get('_config_revision'),'items':values}
    if len(json.dumps(result,ensure_ascii=False).encode())>budget:
        fail('OUTPUT_LIMIT','Historical reply exceeds requested budget; request fewer objects or a smaller chunk')
    return result
