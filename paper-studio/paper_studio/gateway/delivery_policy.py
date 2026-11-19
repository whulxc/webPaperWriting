"""Connection-wide control of file bytes and embedded UI sent to Chat.

This boundary governs this Paper Studio connection. Runner-local builds,
candidate copies and Drive API traffic keep their existing authorization.
"""
from __future__ import annotations

import json
from pathlib import PurePosixPath

from paper_studio.security.policy import fail

MODE = 'drive_links_only'
ERROR = 'CHAT_FILE_DELIVERY_DISABLED'
MESSAGE = '此连接仅通过 Google Drive 链接交付文件。可读取文本、元数据和已有发布回执。'
BINARY_SUFFIXES = frozenset({
    '.pdf', '.pptx', '.ppt', '.docx', '.doc', '.xlsx', '.xls', '.zip', '.gz',
    '.png', '.jpg', '.jpeg', '.webp', '.gif', '.tif', '.tiff', '.bmp', '.ico',
    '.mp3', '.mp4', '.wav', '.ogg', '.mov', '.woff', '.woff2', '.ttf', '.otf',
})
BYTE_KEYS = frozenset({
    'blob', 'data_base64', 'bytes_base64', 'content_base64', 'raw_base64',
    'base64_data', 'base64_content', 'payload_base64', 'base64', 'b64',
    'binary_data', 'file_bytes',
})
WIDGET_KEYS = frozenset({
    'openai/outputTemplate', 'openai/widgetAccessible', 'openai/ui',
    'openai/file', 'openai/files', 'openai/attachments',
})


def restricted(cfg):
    return cfg.get('chat_file_delivery', 'native') == MODE


def blocked(operation):
    fail(ERROR, MESSAGE, chat_file_delivery=MODE, operation=operation)


def guard_call(cfg, name, args, *, binding_tool=None, depth=0):
    """Reject known byte-delivery routes before invoking a backend."""
    if not restricted(cfg):
        return
    if not isinstance(args, dict):
        blocked('unstructured_tool_arguments')
    if name == 'call_runtime_tool':
        if depth or set(args) != {'tool', 'arguments'} or not isinstance(args.get('tool'), str):
            blocked('nested_runtime_wrapper')
        return guard_call(cfg, args['tool'], args['arguments'], binding_tool=binding_tool, depth=1)
    if name == 'plugin_tool' and args.get('action') == 'call':
        selected = binding_tool(args.get('binding')) if binding_tool is not None else None
        if not isinstance(selected, str) or selected in {'plugin_tool', 'call_runtime_tool'}:
            blocked('unobserved_provider_binding')
        return guard_call(cfg, selected, args.get('arguments'), depth=depth + 1)
    if name == 'paper_build_artifact' and args.get('action', 'metadata') not in {'metadata', 'copy_to_candidate'}:
        blocked(name)
    if name == 'project_artifact' and args.get('action', 'inspect') not in {'inspect', 'metadata'}:
        blocked(name)
    if name == 'paper_manuscript_build' and args.get('action') == 'read':
        blocked(name)
    if name == 'paper_review_proposal' and args.get('action') == 'get' and args.get('part', 'summary') == 'artifact':
        blocked(name)
    if name == 'paper_index_get' and args.get('part', 'metadata') != 'metadata':
        blocked(name)
    if name in {'paper_pdf_preview', 'paper_section_delivery'}:
        blocked(name)
    if name in {'paper_read_files', 'read_files', 'search_and_read'}:
        items = args.get('items', [])
        if not isinstance(items, list):
            blocked(name)
        for item in items:
            path = item.get('path', '') if isinstance(item, dict) else ''
            if not isinstance(path, str):
                blocked(name)
            if path.startswith('paper-review://'):
                blocked(name)
            if path.startswith(('paper-index://', 'paper-history://')):
                if '/content/' in path:
                    blocked(name)
            elif PurePosixPath(path.split('#', 1)[0]).suffix.lower() in BINARY_SUFFIXES:
                blocked(name)


def guard_request(cfg, message, *, binding_tool=None):
    if not restricted(cfg):
        return
    method = message.get('method') if isinstance(message, dict) else None
    params = message.get('params', {}) if isinstance(message, dict) else {}
    if method == 'resources/read':
        blocked(method)
    if method == 'tools/call' and isinstance(params, dict):
        guard_call(cfg, params.get('name'), params.get('arguments', {}), binding_tool=binding_tool)


def normalize_request(cfg, message):
    """Keep Native project_artifact's omitted action as metadata inspection."""
    if not restricted(cfg) or not isinstance(message, dict) or message.get('method') != 'tools/call':
        return message
    params = message.get('params', {})
    if not isinstance(params, dict) or not isinstance(params.get('arguments', {}), dict):
        return message
    name, args = params.get('name'), params.get('arguments', {})
    if name == 'project_artifact' and 'action' not in args:
        return {**message, 'params': {**params, 'arguments': {**args, 'action': 'inspect'}}}
    if name == 'call_runtime_tool' and args.get('tool') == 'project_artifact' and isinstance(args.get('arguments'), dict):
        nested = args['arguments']
        if 'action' not in nested:
            return {**message, 'params': {**params, 'arguments': {
                **args, 'arguments': {**nested, 'action': 'inspect'}}}}
    return message


def _json_text(text):
    if not isinstance(text, str) or not text.lstrip().startswith(('{', '[')):
        return text
    try:
        decoded = json.loads(text)
    except (ValueError, RecursionError):
        return text
    cleaned = _clean(decoded)
    return text if cleaned == decoded else json.dumps(cleaned, ensure_ascii=False, separators=(',', ':'))


def _clean(value, *, content_block=False):
    if isinstance(value, (bytes, bytearray, memoryview)):
        blocked('binary_response')
    if isinstance(value, list):
        return [_clean(item, content_block=content_block) for item in value]
    if not isinstance(value, dict):
        return value
    kind = value.get('type')
    if isinstance(kind, str) and (kind in {'resource_link', 'resource', 'audio'} or kind == 'image' and 'data' in value):
        blocked('attachment_response')
    if (isinstance(value.get('data'), (str, list)) and
            (value.get('encoding') == 'base64' or isinstance(value.get('mimeType', value.get('mime_type')), str))):
        blocked('encoded_file_response')
    if isinstance(value.get('file_provider'), str) and value['file_provider'] != 'google_drive':
        blocked('native_file_response')
    result = {}
    for key, item in value.items():
        if key in BYTE_KEYS and isinstance(item, (str, bytes, bytearray, list)):
            blocked('encoded_file_response')
        if key in {'file_uri', 'download_uri', 'attachment_uri'} and isinstance(item, str):
            blocked('file_handle_response')
        if key in {'uri', 'url'} and isinstance(item, str) and item.startswith(('data:', 'sandbox:', 'attachment:', 'file:')):
            blocked('file_handle_response')
        if key in WIDGET_KEYS:
            continue
        if key in {'resourceUri', 'outputTemplate'} and isinstance(item, str):
            continue
        if key == '_meta' and isinstance(item, dict):
            item = {k: v for k, v in item.items() if k not in WIDGET_KEYS and k != 'ui'}
        if key in {'resources', 'resourceTemplates'} and isinstance(item, list):
            result[key] = []
            continue
        if key == 'contents' and isinstance(item, list) and any(
                isinstance(entry, dict) and 'uri' in entry for entry in item):
            blocked('resource_response')
        if key == 'paper_studio_payload_json' or key == 'text' and kind == 'text' and content_block:
            result[key] = _json_text(item)
        elif key == 'content' and isinstance(item, list):
            result[key] = _clean(item, content_block=True)
        else:
            result[key] = _clean(item)
    if (isinstance(result.get('structuredContent'), dict) and isinstance(result.get('content'), list)
            and result['structuredContent'] != value.get('structuredContent')):
        result['content'] = [{'type': 'text', 'text': json.dumps(result['structuredContent'], ensure_ascii=False, separators=(',', ':'))}]
    return result


def filter_response(cfg, value):
    """Keep ordinary text/metadata, discard widget metadata and reject bytes."""
    return _clean(value) if restricted(cfg) else value


def describe(cfg):
    mode = cfg.get('chat_file_delivery', 'native')
    return {'mode': mode, 'scope': 'this_connection_and_all_registered_projects',
            'file_bytes_to_chat': mode != MODE, 'delivery_widgets': mode != MODE,
            'local_builds_and_drive_api': 'existing_project_authorization',
            'platform_approval_policy': 'outside_this_connection_policy'}
