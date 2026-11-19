"""Compose admitted Paper Studio tools with an existing Native stdio bridge.

The legacy bridge retains its own Native policy, transport and historical
provider routes. This adapter admits a closed Paper Studio tool set, uses the
shared Gateway for execution and never creates another stdio client.
"""
from __future__ import annotations

from copy import deepcopy
import json

from paper_studio.contracts import BY_NAME
from paper_studio.capabilities import (HOST_TOOLS, INDEX_TOOLS, admitted_tool_names,
    extension_admission_config, require_admitted, require_edit_kind, visible_index_tools)
from paper_studio.gateway.main import Gateway
from paper_studio.gateway.protocol import ProtocolError, dispatch
from paper_studio.gateway import delivery_policy
from paper_studio.security.policy import fail


PAPER_TOOLS = frozenset(BY_NAME)
PROVIDER_ID = 'paper-studio'


def _tool_result(value):
    return {'content': [{'type': 'text', 'text': json.dumps(value, ensure_ascii=False,
                                                          separators=(',', ':'))}],
            'structuredContent': value, 'isError': False}


class ExtensionBridge:
    def __init__(self, base, config_path):
        self.base = base
        self.legacy_forward = base.forward
        self.gateway = Gateway(config_path)
        self.bindings = {}
        self.retired_bindings = set()
        self.binding_generation = None

    def config(self):
        # The existing private bridge's exact scope contract is also required
        # for new routes; adding a public facade cannot bypass that guard.
        cfg = self.gateway.cfg()
        if hasattr(self.base, 'policy'):
            self.base.policy()
        elif cfg.get('legacy_provider_only'):
            # The legacy project bridge has a fixed writer boundary rather than a
            # policy() callback. Require the shared facade to match it exactly.
            if (str(getattr(self.base, 'ROOT', '')) != cfg['project_root']
                    or getattr(self.base, 'WRITER', None) != cfg['native_project']
                    or getattr(self.base, 'RUNNER', None) != cfg['client_id']
                    or set(getattr(self.base, 'PREFIXES', [])) != set(cfg['writable_roots'])):
                fail('CONFIG_INVALID', 'Shared facade differs from the existing legacy writer boundary')
        else:
            fail('CONFIG_INVALID', 'Existing Native policy guard is required')
        cfg = extension_admission_config(cfg)
        generation = cfg.get('_config_revision')
        if generation != self.binding_generation:
            # Keep a bounded rejection set so a binding retired by a config
            # change cannot be mistaken for a historical Native route.
            self.retired_bindings = set(self.bindings)
            self.bindings.clear()
            self.binding_generation = generation
        return cfg

    @staticmethod
    def names(cfg):
        # A new contract appears only after its actual implementation/schema is
        # installed. Never advertise a planned tool by name alone.
        return (PAPER_TOOLS.intersection(BY_NAME) - INDEX_TOOLS) | visible_index_tools(cfg)

    def instructions(self, cfg):
        return ('PaperWriting project ' + cfg['project_id'] + ', Native project '
                + cfg['native_project'] + ', root ' + cfg['project_root'] + '. '
                'Use paper_runtime_status and paper_project_context to discover '
                'the current controlled targets. Native historical projects stay '
                'read-only. ' + ('Historical fixed recipes retain their existing authority. ' if cfg.get('legacy_provider_only') else 'Historical research-build Round3 build_start remains disabled. ') + ' Candidate file changes allow create/edit only and '
                'require the actual project scope and latest read_revision. '
                'Writable candidate scopes: ' + self.writable_scopes(cfg) + '. '
                'Current trusted builds, figure tasks, artifact previews and '
                'Drive synchronization use the admitted paper_* tools. Builds '
                'are asynchronous. Upload completion, complete bytes received, '
                'page images reviewed and author acceptance are separate facts. '
                'No shell/process/script route or arbitrary provider management '
                'is available through this connection. ' +
                ('File delivery uses Drive links only. Keep file bytes and rendering on the Runner; '
                 'read text and metadata through the admitted tools. '
                 if delivery_policy.restricted(cfg) else ''))

    @staticmethod
    def managed_scope(cfg):
        managed = cfg.get('version_management')
        if not managed:
            return ''
        return (' Inside ' + managed['root'] + '/, only a draft version\'s working/main_en.tex, working/main_zh.tex '
                'and working/deps/<support file> are editable; revisions, candidates and published versions change '
                'only through paper_version_derive.')

    @staticmethod
    def writable_scopes(cfg):
        scopes = cfg.get('writable_roots', [])
        return ', '.join(str(scope) for scope in scopes) if scopes else 'the explicitly configured writable roots'

    def presented_tool(self, tool, cfg):
        """Present this instance's restrictions without changing shared schemas."""
        from paper_studio.gateway.preview import present_tool
        value = deepcopy(present_tool(tool,cfg))
        name = value.get('name', value.get('tool_name'))
        if name in {'paper_edit_files', 'edit_project_files'}:
            value['description'] = (
                'Create/edit only within this instance\'s configured current manuscript/log files, '
                'reviews and any admitted figure candidate scopes: ' + self.writable_scopes(cfg) + '. '
                'Read existing files first; edits require their latest numeric expected_read_revision. '
                'Create cannot replace an existing file. Rename/delete are unavailable.'
                + self.managed_scope(cfg))
        elif name == 'plugin_tool' and not cfg.get('legacy_provider_only'):
            value['description'] = (
                'List/describe/call the exact admitted providers. Historical research-build permits '
                'targets/status only; its Round3 build_start remains disabled. Current paper-studio '
                'permits the admitted current-project build, artifact and publication tools. '
                'Calls require an opaque binding observed from describe on this connection. '
                'No provider management, arbitrary commands or blind retry.')
        return value

    def presented_manifest(self, result, cfg):
        value = deepcopy(result)
        pending = [value]
        changed = False
        while pending:
            node = pending.pop()
            if isinstance(node, list):
                pending.extend(node)
            elif isinstance(node, dict):
                name = node.get('name', node.get('tool_name'))
                if isinstance(name, str) and name in {'paper_edit_files', 'edit_project_files', 'plugin_tool'} and 'description' in node:
                    node['description'] = self.presented_tool(node, cfg)['description']
                    changed = True
                pending.extend(node.values())
        if changed and isinstance(value.get('structuredContent'), dict):
            # Native often duplicates the structured manifest as a text block;
            # regenerate that presentation so it cannot retain stale permissions.
            value['content'] = [{'type': 'text', 'text': json.dumps(value['structuredContent'], ensure_ascii=False)}]
        return value

    @staticmethod
    def presented_historical_provider(result):
        value = deepcopy(result)
        pending = [value]
        changed = False
        while pending:
            node = pending.pop()
            if isinstance(node, list):
                pending.extend(node)
            elif isinstance(node, dict):
                if isinstance(node.get('tools'), list):
                    node['tools'] = [tool for tool in node['tools'] if isinstance(tool, dict)
                                     and tool.get('name') in {'build_targets', 'build_status'}]
                    for count in ('toolCount', 'tool_count'):
                        if count in node:
                            node[count] = len(node['tools'])
                    changed = True
                pending.extend(node.values())
        if changed and isinstance(value.get('structuredContent'), dict):
            value['content'] = [{'type': 'text', 'text': json.dumps(value['structuredContent'], ensure_ascii=False)}]
        return value

    def plugin(self, args, cfg):
        if cfg.get('provider_id') != PROVIDER_ID:
            fail('TOOL_DENIED', 'This connection has no admitted Paper Studio provider')
        action = args.get('action')
        provider_names = self.names(cfg).intersection(admitted_tool_names(cfg)) - HOST_TOOLS
        if action == 'list':
            if set(args) != {'action', 'runner', 'plugin'}:
                fail('TOOL_DENIED', 'List only the exact admitted provider')
        elif action == 'describe':
            if (set(args) != {'action', 'runner', 'plugin', 'tool'}
                    or args.get('tool') not in provider_names):
                fail('TOOL_DENIED', 'Describe only an admitted Paper Studio provider tool')
        elif action == 'call':
            binding = self.bindings.get(args.get('binding'))
            if (set(args) != {'action', 'binding', 'arguments'} or binding is None
                    or binding not in provider_names or not isinstance(args.get('arguments'), dict)
                    or args['arguments'].get('project_id') != cfg['project_id']):
                fail('PROJECT_DENIED', 'Call only an observed provider binding for this project')
            delivery_policy.guard_call(cfg,binding,args['arguments'])
        else:
            fail('TOOL_DENIED', 'Paper Studio provider management is unavailable')
        if action in {'list', 'describe'} and (
                args.get('runner') != cfg['client_id'] or args.get('plugin') != PROVIDER_ID):
            fail('PROJECT_DENIED', 'The provider belongs to the exact configured Runner')
        raw = self.gateway.client(cfg, 'builder').call('plugin_tool', args)
        value = self.gateway.unwrap(raw)
        if (action == 'call' and isinstance(value, dict)
                and ('structuredContent' in value or value.get('isError'))):
            # Native returns the provider ToolOutput inside its own result.
            # Decode the same transport envelope as Gateway.plugin and retain
            # provider failures before constructing a successful MCP result.
            value = self.gateway.unwrap(value)
        if action == 'list':
            value = deepcopy(value)
            if isinstance(value.get('tools'), list):
                value['tools'] = [t for t in value['tools']
                                  if isinstance(t, dict) and t.get('name') in provider_names]
                if 'toolCount' in value:
                    value['toolCount'] = len(value['tools'])
        elif action == 'describe':
            binding = value.get('binding')
            if (value.get('runner') != cfg['client_id'] or value.get('plugin') != PROVIDER_ID
                    or value.get('tool', {}).get('name') != args['tool']
                    or not isinstance(binding, str) or not binding.startswith('wc_pbind_')):
                fail('PLUGIN_BINDING', 'Provider did not return the requested exact tool binding')
            self.bindings[binding] = args['tool']
        return _tool_result(value)

    def paper_tool(self, message, cfg):
        params = message.get('params', {})
        name = params.get('name')
        args = params.get('arguments', {})
        if not isinstance(args, dict):
            fail('INVALID_ARGUMENT', 'Structured tool arguments required')
        if name == 'tool_manifest':
            tool_name = args.get('tool_name')
            if tool_name not in self.names(cfg):
                fail('TOOL_DENIED', 'Discover one admitted Paper Studio tool by exact name')
            return _tool_result({
                'tool_name': tool_name, 'tool': self.presented_tool(BY_NAME[tool_name], cfg),
                'route': {'primary': {'tool_name': tool_name},
                          'fallback': {'tool_name': 'call_runtime_tool',
                                       'fixed_arguments': {'tool': tool_name},
                                       'arguments_field': 'arguments'}},
            })
        if name == 'plugin_tool':
            return self.plugin(args, cfg)
        if name not in self.names(cfg):
            fail('TOOL_DENIED', 'Paper Studio tool is outside this connection scope')
        if name not in {'paper_runtime_status','paper_pdf_preview'} and args.get('project_id') != cfg['project_id']:
            fail('PROJECT_DENIED', 'This Paper Studio connection admits only its configured project')
        require_admitted(cfg,name)
        if name == 'paper_edit_files':
            changes = args.get('changes')
            if (not isinstance(changes, list) or not changes
                    or any(not isinstance(c, dict) for c in changes)):
                fail('WRITE_DENIED', 'Structured candidate changes required')
            for change in changes:
                require_edit_kind({**cfg,'allowed_edit_kinds':cfg.get('allowed_edit_kinds',['create','edit'])},change.get('kind'))
        return self.gateway.handle(message)

    def paper_reply(self, message):
        reply = dispatch(self, message)
        return [] if reply is None else [reply]

    def is_paper_plugin(self, args):
        if not isinstance(args, dict):
            return False
        binding = args.get('binding')
        return args.get('plugin') == PROVIDER_ID or (
            isinstance(binding, str) and binding in (self.bindings.keys() | self.retired_bindings))

    def legacy_result(self, message):
        # The original Native HTTP transport uses the legacy protocol unless
        # its artifact helper explicitly supplies modern headers. Never pass
        # the outer modern protocol metadata to that header-less request.
        # Artifact exports/reads may still upgrade themselves atomically in
        # the original helper with matching metadata AND HTTP headers.
        params = message.get('params', {})
        upstream_params = {k: v for k, v in params.items() if k != '_meta'}
        replies = self.legacy_forward({**message, 'params': upstream_params})
        matches = [reply for reply in replies if isinstance(reply, dict)
                   and reply.get('id') == message.get('id')]
        if len(matches) != 1:
            raise ProtocolError(-32603, 'Native bridge returned no unique matching response')
        reply = matches[0]
        if 'error' in reply:
            error = reply['error']
            if not isinstance(error, dict):
                raise ProtocolError(-32603, 'Native bridge returned an invalid error response')
            raise ProtocolError(error.get('code', -32603),
                                error.get('message', 'Native bridge rejected request'), error.get('data'))
        result = reply.get('result')
        if not isinstance(result, dict):
            raise ProtocolError(-32603, 'Native bridge returned an invalid result')
        return deepcopy(result)

    def filter_response(self, value):
        return delivery_policy.filter_response(self.config(), value)

    def binding_tool(self, binding):
        return self.bindings.get(binding) or getattr(self.base, 'BINDINGS', {}).get(binding)

    def handle(self, message):
        cfg = self.config()
        message = delivery_policy.normalize_request(cfg, message)
        delivery_policy.guard_request(cfg, message, binding_tool=self.binding_tool)
        return delivery_policy.filter_response(cfg, self._handle(message))

    def _handle(self, message):
        # All requests arrive here only after the common dispatcher validates
        # their protocol era, required metadata, request ID and argument shape.
        cfg = self.config()
        method = message['method']
        params = message.get('params', {})
        if method == 'server/discover':
            return {**self.gateway.handle(message), 'instructions': self.instructions(cfg)}
        from paper_studio.gateway.download import is_ui_uri, ui_resources
        from paper_studio.gateway.preview import UI_URI as PREVIEW_URI, ui_resources as preview_resources
        if method == 'resources/read' and (is_ui_uri(params.get('uri')) or params.get('uri') == PREVIEW_URI or params.get('uri') in self.gateway.resources):
            return self.gateway.handle(message)
        if method == 'tools/call':
            name, args = params.get('name'), params.get('arguments', {})
            if name == 'call_runtime_tool' and isinstance(args, dict):
                target = args.get('tool')
                extension_target = (isinstance(target, str) and target.startswith('paper_')
                                    or target == 'plugin_tool' and self.is_paper_plugin(args.get('arguments'))
                                    or target == 'tool_manifest' and isinstance(args.get('arguments'), dict)
                                    and isinstance(args['arguments'].get('tool_name'), str)
                                    and args['arguments']['tool_name'].startswith('paper_'))
                if target == 'call_runtime_tool' or extension_target:
                    if set(args) != {'tool', 'arguments'} or not isinstance(args.get('arguments'), dict):
                        fail('TOOL_DENIED', 'Only one structured admitted runtime wrapper is allowed')
                    if target == 'call_runtime_tool':
                        fail('TOOL_DENIED', 'Recursive runtime wrappers are unavailable')
                    replacement = {'name': target, 'arguments': args['arguments']}
                    return self.handle({**message, 'params': {**params, **replacement}})
            if (isinstance(name, str) and name.startswith('paper_')
                    or name == 'tool_manifest' and isinstance(args, dict)
                    and isinstance(args.get('tool_name'), str) and args['tool_name'].startswith('paper_')):
                return self.paper_tool(message, cfg)
            if name == 'plugin_tool' and self.is_paper_plugin(args):
                return self.plugin(args, cfg)
        result = self.legacy_result(message)
        if method == 'resources/list':
            result['resources'] = [entry for entry in result.get('resources', []) if not is_ui_uri(entry.get('uri')) and entry.get('uri') != PREVIEW_URI] + ui_resources(cfg) + preview_resources(cfg)
        if method == 'tools/call':
            name, args = params.get('name'), params.get('arguments', {})
            is_manifest = name == 'tool_manifest'
            if name == 'call_runtime_tool' and isinstance(args, dict):
                is_manifest = args.get('tool') == 'tool_manifest'
                name, args = args.get('tool'), args.get('arguments', {})
            if is_manifest:
                result = self.presented_manifest(result, cfg)
            if (name == 'plugin_tool' and isinstance(args, dict) and args.get('action') == 'list'
                    and args.get('plugin') == 'research-build' and args.get('runner') == cfg['client_id']
                    and not cfg.get('legacy_provider_only')):
                result = self.presented_historical_provider(result)
        if method in {'tools/list', 'initialize'}:
            result['instructions'] = self.instructions(cfg)
            if method == 'tools/list':
                old = result.get('tools', [])
                for tool in old:
                    if tool.get('name') == 'call_runtime_tool':
                        selector = tool.get('inputSchema', {}).get('properties', {}).get('tool', {})
                        if isinstance(selector.get('enum'), list):
                            selector['enum'] = sorted(set(selector['enum']) | self.names(cfg))
                result['tools'] = [self.presented_tool(t, cfg) for t in old if t.get('name') not in PAPER_TOOLS]
                result['tools'].extend(self.presented_tool(BY_NAME[n], cfg) for n in sorted(self.names(cfg)))
        return result

    def forward(self, message):
        # Apply one outer validation/encoding boundary to both new and legacy
        # routes. Legacy results retain their data, while modern wire fields
        # are issued for the requesting client rather than leaked upstream.
        return self.paper_reply(message)


def attach(base_module, config_path):
    """Return the composed forward callable; the caller keeps its stdio loop."""
    extension = ExtensionBridge(base_module, config_path)
    return extension.forward
