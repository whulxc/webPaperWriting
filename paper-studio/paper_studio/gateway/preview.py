"""Opt-in current-binding manuscript viewer; never a global PDF file handler."""
from copy import deepcopy
import json
from pathlib import Path
import re

from paper_studio.gateway.download import describe_archive, UI_MIME
from paper_studio.security.policy import fail, sha

TOOL = 'paper_pdf_preview'
_ROOT = Path(__file__).parent


def validate_config(cfg):
    value = cfg.get('pdf_preview')
    if value is None:
        return
    if (not isinstance(value, dict) or set(value) != {'version', 'build_ids'}
            or not isinstance(value['version'], str)
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', value['version'])
            or not isinstance(value['build_ids'], dict)
            or set(value['build_ids']) != {'en', 'zh'}
            or any(not isinstance(bid, str) or not re.fullmatch(r'rb_[a-f0-9]{24}', bid)
                   for bid in value['build_ids'].values())
            or value['build_ids']['en'] == value['build_ids']['zh']):
        fail('CONFIG_INVALID', 'pdf_preview requires one immutable version and distinct exact EN/ZH build IDs')


def _html():
    manifest = json.loads((_ROOT / 'preview_vendor/SOURCE.json').read_text())
    parts = {}
    for name in ('pdf.worker.min.mjs', 'pdf.min.mjs'):
        raw = (_ROOT / 'preview_vendor' / name).read_bytes()
        if sha(raw) != manifest['files'][name]:
            fail('RESOURCE_INTEGRITY', 'Bundled PDF renderer differs from its pinned source')
        parts[name] = raw.decode().replace('</script', '<\\/script')
    cmaps = (_ROOT / 'preview_vendor/cmaps.json').read_bytes()
    if sha(cmaps) != manifest['files']['cmaps.json']:
        fail('RESOURCE_INTEGRITY', 'Bundled PDF CMaps differ from their pinned source')
    return (_ROOT / 'pdf-preview.html').read_text().replace(
        '@@PDFJS_WORKER@@', parts['pdf.worker.min.mjs']).replace(
        '@@PDFJS_MAIN@@', parts['pdf.min.mjs']).replace('@@PDFJS_CMAPS@@', cmaps.decode())


# A resource URI is a cache key. Its bytes stay fixed for this loaded runtime.
_HTML = _html()
UI_URI = f'ui://paper-studio/pdf-preview-v1-{sha(_HTML.encode())}.html'


def enabled(cfg):
    from paper_studio.capabilities import admitted_tool_names
    from paper_studio.gateway.delivery_policy import restricted
    return not restricted(cfg) and bool(cfg.get('pdf_preview')) and {TOOL, 'paper_build_artifact'} <= admitted_tool_names(cfg)


def present_tool(tool, cfg):
    from paper_studio.gateway.delivery_policy import restricted
    if restricted(cfg):
        value = deepcopy(tool)
        value.pop('_meta', None)
        if tool.get('name') in {'paper_build_artifact', 'project_artifact', 'paper_section_delivery', TOOL}:
            value['description'] = value.get('description', '') + ' This connection uses chat_file_delivery=drive_links_only. Return text and metadata or existing Drive links. File export to Chat, image/chunk replies and delivery widgets are disabled.'
        return value
    if tool.get('name') != TOOL:
        return tool
    value = deepcopy(tool)
    value.pop('_meta', None)
    if enabled(cfg):
        value['_meta'] = {'ui': {'resourceUri': UI_URI, 'visibility': ['model', 'app']},
                          'openai/outputTemplate': UI_URI,
                          'openai/ui': {'entrypoints': [{'type': 'thread'}]},
                          'openai/widgetAccessible': True}
    return value


def ui_resources(cfg):
    if not enabled(cfg):
        return []
    return [{'uri': UI_URI, 'mimeType': UI_MIME, 'name': 'Current project bilingual PDF viewer'}]


def read_ui(cfg):
    if not enabled(cfg):
        fail('RESOURCE_DENIED', 'This project has not enabled the PDF preview panel')
    return {'contents': [{'uri': UI_URI, 'mimeType': UI_MIME, 'text': _HTML,
        '_meta': {'ui': {'csp': {'connectDomains': [], 'resourceDomains': []}},
                  'openai/ui': {'availableDisplayModes': ['inline', 'fullscreen']}}}]}


def describe_current(cfg):
    from paper_studio.capabilities import require_admitted
    require_admitted(cfg, TOOL)
    require_admitted(cfg, 'paper_build_artifact')
    validate_config(cfg)
    if not cfg.get('pdf_preview'):
        fail('PREVIEW_NOT_CONFIGURED', 'Current binding has no explicitly selected PDF archive')
    required = ('project_id', 'native_project', 'client_id', 'instance_id', '_config_revision')
    if any(not isinstance(cfg.get(key), str) or not cfg[key] for key in required):
        fail('CONFIG_INVALID', 'PDF preview requires the current project and native binding identity')
    result = describe_archive(cfg, cfg['pdf_preview'])
    return {**result, 'schema': 'paper-studio.pdf-preview.v1', 'preview_context': {
        key: cfg[key] for key in required if key != '_config_revision'}}
