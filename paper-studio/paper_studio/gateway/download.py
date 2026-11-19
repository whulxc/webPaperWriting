"""Identity-checked bilingual PDFs from a section or immutable manuscript archive."""
import json
from pathlib import Path

from paper_studio.chapters.workflow import read_revision, label
from paper_studio.provider import jobs
from paper_studio.security.policy import fail, safe_read, file_sha, sha

# Resource URIs are host cache keys. Capture the bytes once so the advertised
# content-addressed URI cannot serve different HTML during this process.
_UI_BYTES = Path(__file__).with_name('section-download.html').read_bytes()
UI_SHA256 = sha(_UI_BYTES)
UI_URI = f'ui://paper-studio/sections-v2-{UI_SHA256}.html'
LEGACY_UI_URI = 'ui://paper-studio/sections-v1.html'
UI_MIME = 'text/html;profile=mcp-app'


def is_ui_uri(uri):
    return uri in (UI_URI, LEGACY_UI_URI)


def ui_resources(cfg):
    from paper_studio.capabilities import admitted_tool_names
    from paper_studio.gateway.delivery_policy import restricted
    if restricted(cfg):
        return []
    if 'paper_section_delivery' not in admitted_tool_names(cfg):
        return []
    if not any(project.get('chapter_workflow') or project.get('execution_policy', {}).get('tex_targets')
               for project in [cfg, *cfg.get('_projects', [])]):
        return []
    return [{'uri': UI_URI, 'name': 'Bilingual manuscript PDFs', 'mimeType': UI_MIME},
            {'uri': LEGACY_UI_URI, 'name': 'Bilingual manuscript PDFs (legacy alias)', 'mimeType': UI_MIME}]


def read_ui(cfg, uri=UI_URI):
    if not is_ui_uri(uri):
        fail('RESOURCE_DENIED', 'Unknown bilingual download UI resource')
    if not ui_resources(cfg):
        fail('RESOURCE_DENIED', 'The bilingual download UI is not enabled')
    return {'contents': [{'uri': uri, 'mimeType': UI_MIME, 'text': _UI_BYTES.decode('utf-8'),
                          '_meta': {'ui': {'csp': {'connectDomains': [], 'resourceDomains': []}}}}]}


def describe_delivery(cfg, args):
    if args.get('version') is not None:
        if args.get('section_id') is not None or args.get('revision') is not None:
            fail('INVALID_ARGUMENT', 'Choose an archive version or a section revision')
        return describe_archive(cfg, args)
    if not args.get('section_id') or not args.get('revision'):
        fail('INVALID_ARGUMENT', 'A section requires section_id and revision')
    compatibility = legacy_archive_selection(cfg, args)
    if compatibility is not None:
        return compatibility
    directory, revision, identity = read_revision(cfg, args['section_id'], args['revision'])
    files = {}
    for language, entry in revision['languages'].items():
        bid = args['build_ids'][language]
        status = jobs.read_status(cfg, bid)
        job = jobs.job_path(cfg, bid)
        record = json.loads(safe_read(job, 'request.json', 4 * 1024**2))
        source = (directory / entry['source']).relative_to(cfg['project_root']).as_posix()
        if (status['status'] != 'completed' or status['kind'] != 'tex'
                or record['request'].get('tex_path') != source
                or record['request'].get('expected_revision') != entry['source_sha256']
                or not any(row.get('path') == source and row.get('sha256') == entry['source_sha256']
                           for row in record['snapshot']['files'])):
            fail('BUILD_IDENTITY_CONFLICT', 'Select the completed build for this section, revision and language')
        result = status['result']
        file = next((a for a in status['artifacts'] if a['path'] == result['pdf_path']), None)
        if not file or file['mime_type'] != 'application/pdf' or file['sha256'] != result['pdf_sha256']:
            fail('ARTIFACT_CHANGED', 'The PDF differs from the completed TeX result')
        files[language] = {**file, 'build_id': bid, 'pages': result['pages'],
                           'filename': f"{args['section_id']}-{args['revision']}-{language}.pdf"}
    return {'project_id': cfg['project_id'], 'config_revision': cfg['_config_revision'],
            'section_id': args['section_id'], 'revision': args['revision'], 'scope': revision['scope'],
            'manifest_sha256': identity, 'files': files}


def legacy_archive_selection(cfg, args):
    """Old host signatures can select only exact registered manuscript pairs.

    The response explicitly identifies an immutable archive. No chapter or
    section manifest is created to route an archived complete manuscript.
    """
    section_id, revision = label(args['section_id']), label(args['revision'])
    expected = {language: f'manuscripts/{section_id}/{revision}/main_{language}.tex'
                for language in ['en', 'zh']}
    registered = {target.get('tex_path')
                  for target in cfg.get('execution_policy', {}).get('tex_targets', {}).values()}
    if not set(expected.values()) <= registered:
        return None
    chapter_root = cfg.get('chapter_workflow', {}).get('root')
    if chapter_root and (Path(cfg['project_root']) / chapter_root / section_id / revision / 'manifest.json').exists():
        fail('DELIVERY_SCOPE_AMBIGUOUS', 'A chapter and complete manuscript share this selector; use explicit archive version')
    for language, source in expected.items():
        bid = args['build_ids'][language]
        record = json.loads(safe_read(jobs.job_path(cfg, bid), 'request.json', 4 * 1024**2))
        if record.get('request', {}).get('tex_path') != source:
            fail('BUILD_IDENTITY_CONFLICT', 'Legacy archive selector requires its exact registered manuscript and language')
    result = describe_archive(cfg, {'version': section_id + '-' + revision, 'build_ids': args['build_ids']})
    return {**result, 'request_compatibility': 'registered_manuscript_pair',
            'legacy_request': {'section_id': section_id, 'revision': revision}}


def describe_archive(cfg, args):
    from paper_studio.versions import history
    archive, manifest = history.verify_version(cfg, args['version'])
    entries = history._version_entries(archive, manifest)
    selected = args['build_ids']
    if set(selected) != {'en', 'zh'} or selected['en'] == selected['zh']:
        fail('INVALID_ARGUMENT', 'Select two distinct English and Chinese manuscript builds')
    members = {row['path']: row for row in manifest['files']}
    files = {}
    for language in ['en', 'zh']:
        entry = entries.get(language)
        bid = selected[language]
        if not entry or entry['build_id'] != bid or entry['language'] != language:
            fail('BUILD_IDENTITY_CONFLICT', 'Build and language must match the selected immutable archive')
        job = jobs.job_path(cfg, bid)
        request_raw = safe_read(job, 'request.json', 4 * 1024**2)
        archived_request = members.get(f'builds/{bid}/request.json')
        if not archived_request or archived_request['sha256'] != sha(request_raw):
            fail('BUILD_IDENTITY_CONFLICT', 'Live build request differs from its archived original')
        record = json.loads(request_raw)
        state = jobs.read_status(cfg, bid)
        if (state['status'] != 'completed' or state['kind'] != 'tex'
                or record.get('kind') != 'tex' or record.get('project_id') != cfg['project_id']):
            fail('BUILD_IDENTITY_CONFLICT', 'Archive delivery requires completed manuscript builds for this project')
        jobs.verify_snapshot(job, record)
        result = state['result']
        file = next((item for item in state['artifacts'] if item['path'] == result['pdf_path']), None)
        archived_pdf = members.get(f"builds/{bid}/output/{result['pdf_path']}")
        if (not file or not archived_pdf or file['mime_type'] != 'application/pdf'
                or file['sha256'] != result['pdf_sha256']
                or any(file[key] != archived_pdf[key] for key in ['sha256', 'bytes'])):
            fail('ARTIFACT_CHANGED', 'Delivery PDF differs from the selected archive')
        files[language] = {**file, 'build_id': bid, 'pages': result['pages'],
            'filename': f"{args['version']}-{language}.pdf"}
    return {'project_id': cfg['project_id'], 'config_revision': cfg['_config_revision'],
        'version': args['version'], 'revision': args['version'], 'source_type': 'immutable_version_archive',
        'scope': '稿件归档 · ' + args['version'], 'manifest_sha256': file_sha(archive / 'manifest.json'),
        'files': files}
