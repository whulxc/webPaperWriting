"""One project-owned continuity file; editing reuses native revision CAS tools."""
import json,re
from paper_studio.security.policy import StudioError,fail,path_for,project,safe_read,sha


def continuation_summary(cfg, *, reader=safe_read):
    """Project declarations and named source checks, without choosing a new baseline."""
    result = {
        'status': 'not_recorded', 'state_path': None, 'state_sha256': None,
        'registered': {}, 'sources': [], 'latest_version_verified': False,
        'author_acceptance_verified': False,
    }
    state_path = cfg.get('research_state_path', 'PROJECT_STATE.json')
    try:
        path_for(cfg, state_path)
        result['state_path'] = state_path
        raw = reader(cfg['project_root'], state_path, 1024 * 1024)
    except StudioError as error:
        if error.code != 'NOT_FOUND':
            result.update(status='invalid', error_code=error.code)
        return result
    result['state_sha256'] = sha(raw)
    try:
        state = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        return {**result, 'status': 'invalid', 'error_code': 'RESEARCH_STATE_INVALID'}
    if (not isinstance(state, dict)
            or state.get('schema') != 'paper-studio.research-state.v1'
            or state.get('project_id') != cfg['project_id']):
        return {**result, 'status': 'invalid', 'error_code': 'RESEARCH_STATE_INVALID'}

    continuation = state.get('writing_continuation')
    if continuation is None:
        return result
    if not isinstance(continuation, dict):
        return {**result, 'status': 'invalid', 'error_code': 'CONTINUATION_INVALID'}
    fallback = {
        'version': state.get('current_writing_version'),
        'revision': state.get('current_writing_revision'),
        'status': state.get('current_writing_status'),
    }
    previous_review = continuation.get('last_delivered_review')
    if isinstance(previous_review, dict):
        fallback['last_delivered_review_version'] = previous_review.get('version')
    for key in ('display_version', 'version', 'revision', 'status', 'source_mode',
                'observed_at', 'author_acceptance', 'scientific_status',
                'last_delivered_review_version'):
        value = continuation.get(key, fallback.get(key))
        if value is None:
            continue
        if (not isinstance(value, str) or len(value) > 500
                or value.startswith(('/', '\\')) or re.match(r'^[A-Za-z]:', value)):
            return {**result, 'status': 'invalid', 'error_code': 'CONTINUATION_INVALID'}
        result['registered'][key] = value

    groups = []
    for role in ('source_files', 'registered_live_sources'):
        references = continuation.get(role, {})
        if not isinstance(references, dict):
            return {**result, 'status': 'invalid', 'error_code': 'CONTINUATION_INVALID'}
        groups.append((role, references))
    languages = continuation.get('languages', {})
    if not isinstance(languages, dict):
        return {**result, 'status': 'invalid', 'error_code': 'CONTINUATION_INVALID'}
    groups.append(('language_tex', {
        language: row['tex'] for language, row in languages.items()
        if language in ('en', 'zh', 'shared') and isinstance(row, dict) and 'tex' in row
    }))
    for role, references in groups:
        for language in ('en', 'zh', 'shared'):
            if language not in references:
                continue
            ref = references[language]
            check = {'role': role, 'language': language, 'status': 'invalid'}
            result['sources'].append(check)
            if not isinstance(ref, dict):
                check['error_code'] = 'CONTINUATION_SOURCE_INVALID'
                continue
            expected = ref.get('sha256')
            if expected is not None and (not isinstance(expected, str)
                                        or re.fullmatch('[0-9a-f]{64}', expected) is None):
                check['error_code'] = 'CONTINUATION_SOURCE_INVALID'
                continue
            try:
                path_for(cfg, ref.get('path'))
                check['path'] = ref['path']
                check['registered_sha256'] = expected
                source = reader(cfg['project_root'], ref['path'], 2 * 1024 * 1024)
                check['observed_sha256'] = sha(source)
                check['status'] = ('unverified' if expected is None else
                                   'match' if check['observed_sha256'] == expected else 'mismatch')
            except StudioError as error:
                check['status'] = 'missing' if error.code == 'NOT_FOUND' else 'invalid'
                check['error_code'] = error.code
    statuses = {check['status'] for check in result['sources']}
    result['status'] = ('invalid' if 'invalid' in statuses else
                        'source_conflict' if statuses & {'missing', 'mismatch'} else 'read')
    return result

def dispatch(cfg,args):
    project(cfg,args.get('project_id'))
    path=cfg.get('research_state_path')
    if path is None:fail('RESEARCH_STATE_NOT_CONFIGURED','No continuity file is configured for this project')
    path_for(cfg,path);raw=safe_read(cfg['project_root'],path,65536)
    try:state=json.loads(raw)
    except (ValueError,UnicodeError):fail('RESEARCH_STATE_INVALID','Continuity file is not valid UTF-8 JSON')
    required={'schema','project_id','workdir','current_task','accepted_fact_sources','frozen_result_sources','completed_builds','issues','deliverables','last_acceptance'}
    if not isinstance(state,dict) or set(state)!=required or state.get('schema')!='paper-studio.research-state.v1' or state.get('project_id')!=cfg['project_id']:
        fail('RESEARCH_STATE_INVALID','Continuity schema or project identity differs from the configured contract')
    if state['workdir'] not in cfg.get('manuscript_roots',[]):fail('RESEARCH_STATE_INVALID','Working manuscript is not registered')
    if not isinstance(state['current_task'],str) or len(state['current_task'])>2000:fail('RESEARCH_STATE_INVALID','Bounded current_task text required')
    if not isinstance(state['issues'],list) or len(state['issues'])>32 or any(not isinstance(x,str) or len(x)>1000 for x in state['issues']):fail('RESEARCH_STATE_INVALID','Bounded issue list required')
    checks=[]
    for group in ['accepted_fact_sources','frozen_result_sources','deliverables','last_acceptance']:
        refs=state[group] if group!='last_acceptance' else ([state[group]] if state[group] is not None else [])
        if not isinstance(refs,list) or len(refs)>64:fail('RESEARCH_STATE_INVALID','Bounded evidence references required')
        for ref in refs:
            if not isinstance(ref,dict) or set(ref)!={'path','sha256'} or not isinstance(ref['sha256'],str) or not re.fullmatch('[0-9a-f]{64}',ref['sha256']):fail('RESEARCH_STATE_INVALID','Each evidence reference requires exact path and SHA256')
            path_for(cfg,ref['path']);current=sha(safe_read(cfg['project_root'],ref['path'],32*1024*1024))
            checks.append({'group':group,'path':ref['path'],'expected_sha256':ref['sha256'],'actual_sha256':current,'matches':current==ref['sha256']})
    builds=state['completed_builds']
    if not isinstance(builds,list) or len(builds)>100:fail('RESEARCH_STATE_INVALID','Bounded build record list required')
    for b in builds:
        if not isinstance(b,dict) or set(b)!={'record_id','build_id'} or not re.fullmatch(r'b[0-9]{3,}',b.get('record_id','')) or not re.fullmatch(r'rb_[a-f0-9]{22,32}',b.get('build_id','')):fail('RESEARCH_STATE_INVALID','Explicit manuscript record and native build identities required')
    return {'project_id':cfg['project_id'],'state_path':path,'sha256':sha(raw),'state':state,'source_checks':checks,
            'status':'source_conflict' if any(not c['matches'] for c in checks) else 'read',
            'build_status_verified':False,'scientific_acceptance_verified':False,
            'guidance':'This is project-authored continuity, not a new scientific acceptance. Read this file with paper_read_files before updating through paper_edit_files using its latest numeric read_revision. Query each listed build with paper_manuscript_build status; use existing artifact/Drive tools for delivery.'}
