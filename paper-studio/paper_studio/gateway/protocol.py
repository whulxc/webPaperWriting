"""MCP wire compatibility for legacy sessions and 2026-07-28 requests.

The native backend keeps its own protocol; this boundary only encodes the
public stdio facade. Protocol failures must never look like tool results.
"""
from __future__ import annotations

import json
from management.native_client import NativeError
from paper_studio import __version__
from paper_studio.security.policy import StudioError

MODERN_VERSION = '2026-07-28'
LEGACY_VERSIONS = ('2025-06-18', '2025-03-26')
SUPPORTED_VERSIONS = (MODERN_VERSION, *LEGACY_VERSIONS)
VERSION_KEY = 'io.modelcontextprotocol/protocolVersion'
CAPABILITIES_KEY = 'io.modelcontextprotocol/clientCapabilities'
SERVER_KEY = 'io.modelcontextprotocol/serverInfo'
SERVER_INFO = {'name': 'webcodex-paper-studio', 'version': __version__}
CACHEABLE = {'server/discover', 'tools/list', 'resources/list', 'resources/read',
             'resources/templates/list'}


class ProtocolError(Exception):
    def __init__(self, code, message, data=None):
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


def error_reply(request_id, error):
    value = {'code': error.code, 'message': error.message}
    if error.data is not None:
        value['data'] = error.data
    return {'jsonrpc': '2.0', 'id': request_id, 'error': value}


def encode_result(result, modern, method):
    value = dict(result)
    if modern:
        value['resultType'] = 'complete'
        value['_meta'] = {**value.get('_meta', {}), SERVER_KEY: SERVER_INFO}
        if method in CACHEABLE:
            value.update(ttlMs=0, cacheScope='private')
    else:
        # Native artifacts may already use modern wire fields. Preserve content
        # and structuredContent while adapting only the protocol envelope.
        for key in ('resultType', 'ttlMs', 'cacheScope'):
            value.pop(key, None)
    return value


def dispatch(gateway, message):
    reply = _dispatch(gateway, message)
    if reply is None or not hasattr(gateway, 'filter_response'):
        return reply
    try:
        return gateway.filter_response(reply)
    except Exception as error:
        from paper_studio.gateway.delivery_policy import ERROR, MESSAGE
        code = error.code if isinstance(error, StudioError) else ERROR
        description = error.message if isinstance(error, StudioError) else MESSAGE
        # Keep only scalar labels at the last wire boundary. Nested native
        # diagnostics may themselves contain rejected resources or byte fields.
        detail = {'code': code, 'message': description}
        if isinstance(message, dict) and message.get('method') == 'tools/call':
            result = {
                'content': [{'type': 'text', 'text': json.dumps(detail, ensure_ascii=False)}],
                'structuredContent': {'error': detail}, 'isError': True}
            modern = isinstance(reply.get('result'), dict) and reply['result'].get('resultType') == 'complete'
            return {'jsonrpc': '2.0', 'id': reply.get('id'),
                    'result': encode_result(result, modern, 'tools/call')}
        return error_reply(reply.get('id'), ProtocolError(-32603, description, detail))


def _dispatch(gateway, message):
    request_id = None
    notification = False
    modern = False
    method = None
    try:
        if not isinstance(message, dict) or message.get('jsonrpc') != '2.0':
            raise ProtocolError(-32600, 'A JSON-RPC 2.0 object is required')
        if 'id' in message:
            if type(message['id']) not in (int, str):
                raise ProtocolError(-32600, 'Request id must be a string or integer')
            request_id = message['id']
        else:
            notification = True
        method = message.get('method')
        if not isinstance(method, str) or not method:
            raise ProtocolError(-32600, 'A method string is required')
        params = message.get('params', {})
        if not isinstance(params, dict):
            raise ProtocolError(-32602, 'Object params are required')
        meta = params.get('_meta', {})
        if not isinstance(meta, dict):
            raise ProtocolError(-32602, 'Object _meta is required')
        version = meta.get(VERSION_KEY)
        if VERSION_KEY in meta:
            if not isinstance(version, str):
                raise ProtocolError(-32602, 'Protocol version must be a string')
            if version not in SUPPORTED_VERSIONS:
                raise ProtocolError(-32022, 'Unsupported protocol version',
                                    {'supported': list(SUPPORTED_VERSIONS), 'requested': version})
            modern = version == MODERN_VERSION
        if method == 'server/discover' and not modern:
            raise ProtocolError(-32602, 'server/discover requires modern request metadata')
        if modern and not isinstance(meta.get(CAPABILITIES_KEY), dict):
            raise ProtocolError(-32602, 'Modern requests require clientCapabilities metadata')
        if notification:
            # No notification may execute a file edit or launch a build. Unknown
            # notifications are ignored and never receive a JSON-RPC response.
            return None
        if method.startswith('notifications/'):
            raise ProtocolError(-32600, 'Notifications must not contain an id')
        if modern and method == 'ping':
            raise ProtocolError(-32601, 'ping is not part of the modern protocol')
        if method == 'initialize':
            if modern:
                raise ProtocolError(-32601, 'Use server/discover for the modern protocol')
            if not isinstance(params.get('protocolVersion'), str):
                raise ProtocolError(-32602, 'initialize requires protocolVersion')
            if not isinstance(params.get('capabilities', {}), dict):
                raise ProtocolError(-32602, 'initialize capabilities must be an object')
        if method == 'tools/call':
            if not isinstance(params.get('name'), str) or not isinstance(params.get('arguments', {}), dict):
                raise ProtocolError(-32602, 'tools/call requires a name and object arguments')
        if method == 'resources/read' and not isinstance(params.get('uri'), str):
            raise ProtocolError(-32602, 'resources/read requires uri')
        if method in CACHEABLE and params.get('cursor'):
            raise ProtocolError(-32602, 'This complete bounded catalog has no continuation cursor')
        result = encode_result(gateway.handle(message), modern, method)
        if len(json.dumps(result, ensure_ascii=False).encode()) > 3 * 1024 * 1024:
            raise ProtocolError(-32603, 'Response exceeds limit; use artifact chunks or smaller read pages')
        return {'jsonrpc': '2.0', 'id': request_id, 'result': result}
    except ProtocolError as error:
        return None if notification else error_reply(request_id, error)
    except Exception as error:
        detail = error.as_dict() if isinstance(error, (StudioError, NativeError)) else {
            'code': 'GATEWAY_ERROR', 'message': str(error)[:1000]}
        if notification:
            return None
        if method == 'tools/call':
            result = {'content': [{'type': 'text', 'text': json.dumps(detail, ensure_ascii=False)}],
                      'structuredContent': {'error': detail}, 'isError': True}
            return {'jsonrpc': '2.0', 'id': request_id,
                    'result': encode_result(result, modern, method)}
        return error_reply(request_id, ProtocolError(-32603, 'MCP request failed', detail))


def serve(gateway, incoming, outgoing):
    limit = 1024 * 1024
    while True:
        raw = incoming.readline(limit + 1)
        if not raw:
            return
        if len(raw) > limit:
            # Drain only this oversized frame, not the next valid request.
            while raw and not raw.endswith(b'\n'):
                raw = incoming.readline(limit + 1)
            reply = error_reply(None, ProtocolError(-32600, 'Request exceeds 1 MiB'))
        else:
            try:
                message = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            except (ValueError, UnicodeError, RecursionError):
                reply = error_reply(None, ProtocolError(-32700, 'Invalid JSON'))
            else:
                reply = dispatch(gateway, message)
        if reply is not None:
            outgoing.write(json.dumps(reply, ensure_ascii=False, separators=(',', ':')) + '\n')
            outgoing.flush()
