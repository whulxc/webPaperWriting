"""Optional scoped Gateway reads; no library mutation or attachment bytes."""
from __future__ import annotations
import re
import time
from pathlib import Path

from .workspace import BootstrapError, no_links

READ_ACTIONS = {'search', 'item', 'collections', 'attachments'}
OBJECT_KEY = re.compile(r'[A-Z0-9]{8}')
CITATION_KEY = re.compile(r'[A-Za-z][A-Za-z0-9:_-]{0,63}')


def error_code(error):
    code = getattr(error, 'code', None)
    # Messages/details may contain a private path or a remote response. Preserve
    # only the implementation's structured error code, never str(exception).
    return code if isinstance(code, str) and re.fullmatch(r'[A-Z][A-Z0-9_]{0,79}', code) else 'ZOTERO_GATEWAY_READ_FAILED'


class GatewayRead:
    def __init__(self, config_path, project_id, paper_root):
        from paper_studio.gateway.main import Gateway
        from paper_studio.security.policy import select_project
        self.gateway = Gateway(str(no_links(Path(config_path))))
        self.config = self.gateway.cfg()
        selected = select_project(self.config, project_id)
        if no_links(Path(selected['project_root'])) != paper_root:
            raise BootstrapError('BOOTSTRAP_GATEWAY_ROOT_MISMATCH', 'Gateway project root differs from the selected paper workspace')
        binding = selected.get('zotero_readonly')
        if not isinstance(binding, dict):
            raise BootstrapError('ZOTERO_NOT_CONFIGURED', 'Selected project has no explicit readonly Zotero scope')
        self.project_id = project_id
        self.paper_root = paper_root
        self.identities = {key: dict(row) for key, row in binding['items'].items()}
        self.items = {key: (row.get('doi') or '').strip().casefold() for key, row in binding['items'].items()}
        self.collections = set(binding.get('collection_keys', [])) | {
            value for row in binding['items'].values() for value in row.get('collection_keys', [])}
        self.manuscript_roots = selected.get('manuscript_roots', [])
        self.revision = self.config['_config_revision']
        self.deadline = None

    def call(self, name, arguments):
        from paper_studio.contracts import BY_NAME, validate
        from paper_studio.security.policy import locked, select_project
        if self.deadline is None:
            self.deadline = time.monotonic() + 45
        if time.monotonic() >= self.deadline:
            raise BootstrapError('ZOTERO_BOOTSTRAP_BUDGET', 'Readonly index budget exhausted; remaining reads were not attempted')
        if name == 'paper_zotero_read':
            if arguments.get('action') not in READ_ACTIONS:
                raise BootstrapError('ZOTERO_ACTION_DENIED', 'Only scoped metadata reads are permitted')
        elif name != 'paper_zotero_verify_citations':
            raise BootstrapError('ZOTERO_ACTION_DENIED', 'Bootstrap cannot invoke a write or arbitrary Gateway tool')
        if arguments.get('project_id') != self.project_id:
            raise BootstrapError('PROJECT_DENIED', 'Readonly index cannot change project identity')
        validate(arguments, BY_NAME[name]['inputSchema'])
        with locked(self.config, 'routing'):
            current = self.gateway.cfg()
            if current['_config_revision'] != self.revision:
                raise BootstrapError('BOOTSTRAP_GATEWAY_CONFIG_CHANGED', 'Gateway configuration changed during readonly indexing')
            selected = select_project(current, self.project_id)
            if Path(selected['project_root']) != self.paper_root:
                raise BootstrapError('BOOTSTRAP_GATEWAY_ROOT_MISMATCH', 'Gateway project root changed during readonly indexing')
            if (Path(current['state_root']) / 'routing-unavailable.json').exists():
                raise BootstrapError('BINDING_UNAVAILABLE', 'Gateway routing requires management recovery')
            # This is the existing Gateway's exact schema/project routing path.
            # Neither native tools nor new HTTP endpoints are introduced.
            result = self.gateway._call_locked(current, name, arguments)
        if not isinstance(result, dict):
            raise BootstrapError('ZOTERO_WORKER_PROTOCOL', 'Readonly Gateway result must be an object')
        if name == 'paper_zotero_read' and (result.get('project_id') != self.project_id or result.get('read_only') is not True):
            raise BootstrapError('ZOTERO_SCOPE_DENIED', 'Readonly Gateway result did not confirm the selected project')
        if name == 'paper_zotero_verify_citations' and (
                result.get('project_id') != self.project_id or result.get('library_modified') is not False
                or result.get('fulltext_read') is not False or result.get('applied') is not False):
            raise BootstrapError('ZOTERO_SCOPE_DENIED', 'Citation verification did not confirm its readonly boundaries')
        return result


def prepare(config_path, project_id, paper_root):
    """Fail before workspace writes for unknown projects or wrong roots."""
    try:
        return GatewayRead(config_path, project_id, paper_root)
    except BootstrapError:
        raise
    except Exception as error:
        raise BootstrapError(error_code(error), 'Existing Gateway configuration did not authorize this project and root') from None


def enrich(index, reader):
    """Return an explicitly non-atomic, metadata-only observation and gaps."""
    report = {'status': 'not_requested', 'read_only': True, 'calls': [], 'errors': [],
              'claim_support': 'unverified', 'attachment_bytes_read': False,
              'snapshot_consistency': 'independent_readonly_observations', 'verification': []}
    index['zotero'] = report
    if reader is None:
        return [{'code': 'ZOTERO_BINDING_NOT_VERIFIED'}]
    index['verification'] = 'local_parse_with_optional_scoped_readonly_zotero_observations_no_fulltext_access'
    if not index['entries']:
        report['status'] = 'no_indexed_references'
        return [{'code': 'ZOTERO_NO_SCOPED_LOCAL_REFERENCES'}]
    report['status'] = 'read_completed'
    gaps, cache = [], {}

    def call(action, key):
        args = {'project_id': reader.project_id, 'action': action, 'citation_key': key}
        if action == 'search':
            args['query'] = reader.items[key] or key
        result = reader.call('paper_zotero_read', args)
        if result.get('status') not in {'found', 'not_found', 'ambiguous'}:
            raise BootstrapError('ZOTERO_WORKER_PROTOCOL', 'Readonly metadata returned an unsupported status')
        report['calls'].append({'tool': 'paper_zotero_read', 'action': action, 'citation_key': key,
                                'status': result.get('status')})
        return result

    def failure(key, action, error):
        code = error_code(error)
        report['errors'].append({'citation_key': key, 'action': action, 'code': code})
        report['status'] = 'read_incomplete'
        gaps.append({'code': code, 'project_citation_key': key, 'action': action})
        return {'status': 'error', 'error_code': code}

    for entry in index['entries']:
        key, doi = entry['project_citation_key'], entry.get('doi')
        if report['errors']:
            entry['zotero_read_status'] = 'not_attempted_after_error'
            continue
        scope = getattr(reader, 'identities', {}).get(key, {'doi': reader.items.get(key)})
        from paper_studio.references.zotero_worker import normalized_title
        scoped_doi = (scope.get('doi') or '').strip().casefold()
        pinned = bool(scope.get('item_key') and scope.get('title'))
        local_title = entry.get('metadata', {}).get('title', '')
        permitted = (doi and doi.strip().casefold() == scoped_doi) if scoped_doi else (
            pinned and normalized_title(local_title.replace('{','').replace('}','')) == normalized_title(scope['title']))
        if key not in reader.items or not permitted:
            entry['zotero_read_status'] = 'scope_denied'
            gaps.append({'code': 'ZOTERO_SCOPE_DENIED', 'project_citation_key': key})
            report['status'] = 'read_incomplete'
            continue
        if key not in cache:
            observations = {}
            for action in ['search', 'item', 'collections', 'attachments']:
                try:
                    observations[action] = call(action, key)
                except Exception as error:
                    observations[action] = failure(key, action, error)
                    break  # No retry or additional lookup after transport failure.
                if observations[action].get('status') != 'found':
                    break
            cache[key] = observations
        observed = cache[key]
        if any(row.get('status') == 'error' for row in observed.values()):
            entry['zotero_read_status'] = 'error'
            entry['zotero_errors'] = [{'action': action, 'code': row['error_code']}
                                      for action, row in observed.items() if row.get('status') == 'error']
            continue
        search, item = observed.get('search', {}), observed.get('item', {})
        if search.get('status') != 'found' or item.get('status') != 'found':
            status = search.get('status') if search.get('status') != 'found' else item.get('status')
            entry['zotero_read_status'] = status or 'not_read'
            gaps.append({'code': 'ZOTERO_ITEM_NOT_RESOLVED', 'project_citation_key': key, 'status': entry['zotero_read_status']})
            report['status'] = 'read_incomplete'
            continue
        item_key = item.get('zotero_item_key')
        search_rows = search.get('results', [])
        if not isinstance(search_rows, list) or len(search_rows) > 8:
            failure(key, 'search', BootstrapError('ZOTERO_WORKER_PROTOCOL', 'Invalid bounded search rows'))
            entry['zotero_read_status'] = 'error'
            continue
        def identity_matches(row):
            if not isinstance(row, dict) or row.get('citation_key') != key or row.get('status') != 'found':return False
            if scoped_doi and (not isinstance(row.get('doi'), str) or row['doi'].casefold() != scoped_doi):return False
            if pinned and (row.get('zotero_item_key') != scope['item_key']
                           or normalized_title(row.get('title', '')) != normalized_title(scope['title'])):return False
            return True
        matching = [row for row in search_rows if identity_matches(row) and row.get('zotero_item_key') == item_key]
        if (not identity_matches(item) or not isinstance(item_key, str) or not OBJECT_KEY.fullmatch(item_key) or len(matching) != 1):
            entry['zotero_read_status'] = 'identity_conflict'
            gaps.append({'code': 'ZOTERO_METADATA_IDENTITY_MISMATCH', 'project_citation_key': key})
            report['status'] = 'read_incomplete'
            continue
        collection_result = observed.get('collections', {})
        attachment_result = observed.get('attachments', {})
        if (collection_result.get('status') != 'found' or collection_result.get('citation_key') != key
                or attachment_result.get('status') != 'found' or attachment_result.get('citation_key') != key):
            entry['zotero_read_status'] = 'incomplete'
            gaps.append({'code': 'ZOTERO_METADATA_INCOMPLETE', 'project_citation_key': key})
            report['status'] = 'read_incomplete'
            continue
        collections = collection_result.get('collections', [])
        attachments = attachment_result.get('attachments', [])
        if not isinstance(collections, list) or not isinstance(attachments, list) or len(collections) > 32 or len(attachments) > 25:
            failure(key, 'metadata_schema', BootstrapError('ZOTERO_WORKER_PROTOCOL', 'Invalid bounded metadata'))
            entry['zotero_read_status'] = 'error'
            continue
        if any(not isinstance(row, dict) or row.get('key') not in reader.collections for row in collections):
            failure(key, 'collections', BootstrapError('ZOTERO_SCOPE_DENIED', 'Unexpected collection scope'))
            entry['zotero_read_status'] = 'scope_denied'
            continue
        allowed = [row['key'] for row in collections if isinstance(row, dict) and row.get('key') in reader.collections]
        # No attachment file name, local path, URL, notes, bytes or child text.
        available = [row for row in attachments if isinstance(row, dict)
                     and isinstance(row.get('key'), str) and OBJECT_KEY.fullmatch(row['key'])]
        library_keys = []
        native_keys = item.get('library_citation_keys', [])
        if not isinstance(native_keys, list) or len(native_keys) > 4:
            failure(key, 'item', BootstrapError('ZOTERO_WORKER_PROTOCOL', 'Invalid native citation key metadata'))
            entry['zotero_read_status'] = 'error'
            continue
        for row in native_keys:
            if (isinstance(row, dict) and isinstance(row.get('value'), str) and CITATION_KEY.fullmatch(row['value'])
                    and row.get('source') in {'data.citationKey', 'data.extra: Citation Key'}):
                library_keys.append({'value': row['value'], 'source': row['source']})
        entry.update(zotero_read_status='found', zotero_item_key=item_key,
                     zotero_collection_key=allowed[0] if len(allowed) == 1 else None,
                     zotero_collection_keys=allowed, library_citation_keys=library_keys,
                     zotero_verified=True, metadata_status='scoped_zotero_identity_read',
                     attachment_available=None, attachment_metadata_present=bool(available),
                     attachment_availability_status='metadata_only', attachment_count=len(available),
                     attachment_local_bytes_verified=False)

    # Reuse existing citation verification only for registered manuscript paths
    # located under this Gateway's paper root. Never send original-project paths.
    def manuscript(path):
        return any(path.startswith(root + '/') for root in reader.manuscript_roots)
    bibs = sorted({row['source_path'] for row in index['entries'] if row['source_root'] == 'paper_writing' and manuscript(row['source_path'])})
    tex = sorted({row['source_path'] for row in index['citations'] if row['source_root'] == 'paper_writing' and manuscript(row['source_path'])})
    if bibs and tex and len(tex) <= 32 and len(bibs) <= 4 and not report['errors']:
        for bib in bibs:
            try:
                result = reader.call('paper_zotero_verify_citations', {'project_id': reader.project_id, 'bib_path': bib, 'tex_paths': tex})
                expected_bib = {row['source_sha256'] for row in index['entries'] if row['source_root'] == 'paper_writing' and row['source_path'] == bib}
                expected_tex = {row['source_path']: row['source_sha256'] for row in index['citations'] if row['source_root'] == 'paper_writing' and row['source_path'] in tex}
                checked = result.get('checked_tex_paths', [])
                if (result.get('pre_sha256') not in expected_bib or not isinstance(checked, list)
                        or any(not isinstance(row, dict) for row in checked)
                        or {row.get('path'): row.get('sha256') for row in checked} != expected_tex):
                    raise BootstrapError('BOOTSTRAP_SOURCE_CHANGED', 'Citation sources differ from the inventoried snapshot')
                status = result.get('bibliographic_status')
                if status not in {'verified', 'conflicts', 'no_citations'}:
                    raise BootstrapError('ZOTERO_WORKER_PROTOCOL', 'Citation verification did not report a bibliographic status')
                report['verification'].append({'bib_path': bib, 'status': status,
                                               'claim_support': 'unverified', 'coverage': 'explicit_manuscript_paths_only'})
                report['calls'].append({'tool': 'paper_zotero_verify_citations', 'status': status})
                if status == 'conflicts':
                    gaps.append({'code': 'ZOTERO_BIBLIOGRAPHIC_CONFLICT', 'source_path': bib})
                    report['status'] = 'read_incomplete'
                    for entry in index['entries']:
                        if entry['source_root'] == 'paper_writing' and entry['source_path'] == bib:
                            entry['zotero_verified'] = False
                            entry['metadata_status'] = 'bibliographic_conflict'
            except Exception as error:
                failure(None, 'verify_citations', error)
    else:
        report['verification'].append({'status': 'not_run', 'reason': 'no_bounded_registered_manuscript_paths', 'claim_support': 'unverified'})
    if report['errors']:
        for entry in index['entries']:
            entry['zotero_verified'] = False
            if entry.get('metadata_status') == 'scoped_zotero_identity_read':
                entry['metadata_status'] = 'scoped_zotero_identity_read_incomplete'
    return gaps
