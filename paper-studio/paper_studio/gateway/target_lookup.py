"""Project-scoped indexed reads, including an exact-key route for existing clients."""
from contextvars import ContextVar
import json
import re
import sys
from pathlib import Path
from threading import Lock

from paper_studio.bootstrap.workspace import BootstrapError
from paper_studio.capabilities import require_admitted, require_edit_kind
from paper_studio.security.policy import StudioError, fail, path_for

PREFIX = 'paper-index://'
HANDOFF_PREFIX = 'paper-handoff://'
KEY = r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}'
LOCATOR = re.compile(
    rf'{PREFIX}(?P<version>{KEY})/(?P<revision>{KEY}|~)/(?P<language>en|zh|shared)/'
    r'(?P<role>[a-z][a-z0-9_]{0,39})(?:/(?:(?P<figure_number>[1-9][0-9]{0,5})|key/(?P<target_key>[A-Za-z0-9][A-Za-z0-9._-]{0,95})))?'
    r'(?:/content/(?P<offset>[0-9]{1,12})/(?P<limit>[1-9][0-9]{0,5}))?'
)
_io = ContextVar('paper_index_lookup_io', default=None)
_hook_lock = Lock()
_hook_installed = False


def _observe(event, args):
    counters = _io.get()
    if counters is None:
        return
    if event in ('os.listdir', 'os.scandir', 'os.walk', 'os.fwalk', 'glob.glob', 'pathlib.Path.glob', 'pathlib.Path.rglob'):
        counters['directory_enumeration_attempts'] += 1
        fail('INDEX_SCAN_DENIED', 'Directory enumeration is disabled during an indexed lookup', event=event)
    if event == 'open':
        counters['python_file_opens'] += 1
    elif event == 'sqlite3.connect':
        counters['sqlite_connections'] += 1


def _install_hook():
    global _hook_installed
    with _hook_lock:
        if not _hook_installed:
            sys.addaudithook(_observe)
            _hook_installed = True


def _configured(cfg):
    if not cfg.get('target_index', {}).get('enabled'):
        fail('INDEX_NOT_CONFIGURED', 'This project has not enabled exact-key version lookup')


def entrypoints(cfg):
    """Startup discovery for an indexed binding must not inventory research files."""
    from paper_studio.research_state import continuation_summary
    paths = ('PROJECT.yaml', 'PROJECT_STATE.json', 'HANDOFF.md', 'sync/handoff-index.json',
             'materials/INDEX_CATALOG.json', 'references/reference_index.json')
    entries = [path for path in paths if path_for(cfg, path).is_file()]
    prompt = 'CHAT_POLISH_PROMPT.md'
    try:
        if path_for(cfg, prompt).is_file():
            entries.append(prompt)
    except StudioError as error:
        if error.code != 'PATH_DENIED':
            raise
    return {'project_id': cfg['project_id'], 'config_revision': cfg['_config_revision'],
            'scope': 'indexed_entrypoints', 'entrypoints': entries,
            'continuation': continuation_summary(cfg),
            'inventory': {'scope': 'named_entrypoints', 'files': entries, 'members_verified': False},
            'guidance': 'Look up a named version, revision, language and figure with paper_index_get. '
                        'Existing clients may read paper-index://version/revision/language/role[/figure]. '
                        'Use the returned Drive file_id for that exact object. A missing key ends the lookup. '
                        + ('Use paper_review_proposal to show and annotate changes before confirming an edition. '
                           'Read handoffs by change ID or manifest SHA with paper_material_get. '
                           if cfg.get('admission_profile') == 'paper-editor.v4' else
                           'Record authorized working revisions with paper_index_revise. ')
                        + 'Formal release requires an explicit user request. Use view=evidence for an explicit evidence audit.'}


def lookup(cfg, args):
    _configured(cfg)
    require_admitted(cfg, 'paper_index_get')
    from paper_studio.versions.target_index import resolve

    selector = {key: value for key, value in args.items() if key != 'project_id'}
    selector.setdefault('revision', None)
    return _observed(resolve, cfg, **selector)


def _observed(reader, *args, **kwargs):
    _install_hook()
    counters = {'directory_enumeration_attempts': 0, 'python_file_opens': 0, 'sqlite_connections': 0}
    token = _io.set(counters)
    try:
        result = reader(*args, **kwargs)
        return {**result, 'lookup_io': counters}
    except StudioError as error:
        error.details['lookup_io'] = counters
        raise
    except BootstrapError as error:
        fail(error.code, error.message, lookup_io=counters)
    finally:
        _io.reset(token)


def material(cfg, args):
    require_admitted(cfg, 'paper_material_get')
    from paper_studio.bootstrap.handoff import lookup_handoff
    return _observed(lookup_handoff, Path(cfg['project_root']), cfg['project_id'],
                     change_id=args.get('change_id'), manifest_sha256=args.get('manifest_sha256'))


def read_material_locator(cfg, args):
    items = args['items']
    if len(items) != 1 or set(items[0]) != {'path'}:
        fail('INVALID_ARGUMENT', 'Read one precise material handoff per request')
    ref = items[0]['path'][len(HANDOFF_PREFIX):]
    if ref == '~':
        selector = {}
    elif re.fullmatch(KEY, ref):
        selector = {'change_id': ref}
    elif re.fullmatch('sha256/[0-9a-f]{64}', ref):
        selector = {'manifest_sha256': ref.split('/')[1]}
    else:
        fail('INVALID_ARGUMENT', 'Use paper-handoff://ID, paper-handoff://sha256/HASH or paper-handoff://~')
    result = material(cfg, selector)
    if len(json.dumps(result, ensure_ascii=False).encode()) > args.get('max_result_bytes', 262144):
        fail('OUTPUT_LIMIT', 'Material metadata exceeds the requested reply size')
    return result


def _parse_locator(item):
    if set(item) != {'path'}:
        fail('INVALID_ARGUMENT', 'An indexed locator has no filesystem line options')
    match = LOCATOR.fullmatch(item['path'])
    if not match:
        fail('INVALID_ARGUMENT', 'Expected paper-index://version/revision/language/role[/figure][/content/offset/limit]')
    selector = {k: v for k, v in match.groupdict().items() if v is not None}
    if selector['revision'] == '~':
        selector['revision'] = None
    for key in ('figure_number', 'offset', 'limit'):
        if key in selector:
            selector[key] = int(selector[key])
    return selector


def _batch_item(cfg, path, selector):
    try:
        result = lookup(cfg, selector)
    except StudioError as error:
        if error.code != 'INDEX_TARGET_NOT_FOUND':
            raise
        return {'path': path, 'error': error.code,
                **{key: error.details[key] for key in ('lookup_io', 'selection_storage') if key in error.details}}
    fields = ('resolved_revision', 'effective_version', 'effective_revision', 'figure_version',
              'inherited', 'current_for_revision', 'sha256', 'bytes', 'mime_type', 'lookup_io')
    item = {key: result[key] for key in fields}
    if 'selection_storage' in result:
        item['selection_storage'] = result['selection_storage']
    receipt = result['drive_receipt']
    item['drive_receipt'] = {k: receipt[k] for k in ('file_id', 'url')} if receipt else None
    return {'path': path, **item}


def read_locator(cfg, args):
    """Read explicit indexed keys, with a single bounded file range per binary reply."""
    items = args['items']
    selectors = [_parse_locator(item) for item in items]
    budget = args.get('max_result_bytes', 262144)
    if len(items) > 1:
        if any('limit' in selector for selector in selectors):
            fail('INVALID_ARGUMENT', 'Request one file content range at a time; metadata supports a batch')
        result = {'project_id': cfg['project_id'], 'config_revision': cfg['_config_revision'],
                  'items': [_batch_item(cfg, item['path'], selector) for item, selector in zip(items, selectors)]}
    else:
        result = _read_single(cfg, selectors[0], budget)
    if len(json.dumps(result, ensure_ascii=False).encode()) > budget:
        fail('OUTPUT_LIMIT', 'Indexed reply exceeds max_result_bytes; request fewer items or a smaller chunk')
    return result


def _read_single(cfg, selector, budget):
    if 'limit' in selector:
        if selector['limit'] > 262144 or 4 * ((selector['limit'] + 2) // 3) > budget:
            fail('INPUT_LIMIT', 'Choose a smaller indexed file chunk for this reply budget')
        selector['part'] = 'content'
    return lookup(cfg, selector)


def revise(cfg, args):
    _configured(cfg)
    require_admitted(cfg, 'paper_index_revise')
    if cfg.get('admission_profile') == 'paper-editor.v4':
        fail('REVIEW_CONFIRMATION_REQUIRED', 'Confirm the reviewed changes before creating an edition')
    require_edit_kind(cfg, 'create')
    for member in args['members']:
        path_for(cfg, member['path'], True)
    from paper_studio.versions.target_index import create_revision
    return create_revision(cfg, **{k: v for k, v in args.items() if k != 'project_id'})
