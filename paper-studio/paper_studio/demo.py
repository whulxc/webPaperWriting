"""Credential-free synthetic writing and read-only MCP demonstration."""
from __future__ import annotations

import argparse
import copy
import difflib
import hashlib
import io
import json
from pathlib import Path
import sys

from paper_studio import __version__
from paper_studio.bootstrap.workspace import BootstrapError, _read, no_links
from paper_studio.contracts import BY_NAME, validate
from paper_studio.gateway import protocol
from paper_studio.research_state import continuation_summary
from paper_studio.security.policy import StudioError, fail

PROJECT_ID = 'synthetic-quickstart'
MARKER = 'paper-studio.synthetic-demo.v1'
TOOL_NAMES = ('paper_runtime_status', 'paper_project_context', 'paper_read_files')
FILES = (
    'materials/measurements.csv', 'WRITING_BRIEF.md', 'PROJECT_STATE.json',
    'manuscripts/rev-001/main_en.md', 'manuscripts/rev-001/main_zh.md',
    'manuscripts/rev-002/main_en.md', 'manuscripts/rev-002/main_zh.md',
    'review/changes.diff', 'review/simulated-feedback.json', 'DEMO_REPORT.md',
)


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def write_new(root, relative, raw):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as stream:
        stream.write(raw.encode('utf-8') if isinstance(raw, str) else raw)


def read_fixture(root, relative, max_bytes=262144):
    """Portable bounded reader restricted to this explicitly synthetic fixture."""
    if relative not in (*FILES, 'DEMO.json'):
        fail('PATH_DENIED', 'Only generated synthetic fixture paths are readable')
    root = no_links(Path(root))
    path = no_links(root / relative)
    before = path.stat()
    raw = _read(path, limit=max_bytes)
    after = no_links(path).stat()
    fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
    if any(getattr(before, key) != getattr(after, key) for key in fields):
        fail('REVISION_CONFLICT', 'Synthetic fixture changed during this read')
    return raw


def project_root(value):
    root = no_links(Path(value))
    if not root.is_dir():
        fail('DEMO_NOT_FOUND', 'Create a synthetic project first')
    marker = json.loads(read_fixture(root, 'DEMO.json', 4096))
    if marker != {'schema': MARKER, 'project_id': PROJECT_ID, 'synthetic': True}:
        fail('DEMO_IDENTITY', 'Expected a generated synthetic quickstart project')
    return root


def create(value):
    root = no_links(Path(value))
    if not root.parent.is_dir():
        fail('DEMO_PARENT', 'The absolute output parent directory must already exist')
    root.mkdir(exist_ok=False)
    evidence = 'fixture,baseline_accuracy,candidate_accuracy\nsynthetic-1,0.72,0.77\nsynthetic-2,0.74,0.78\nsynthetic-3,0.76,0.79\n'
    first = {
        'en': '# Synthetic classification study\n\n## Motivation\n\nThis fixture demonstrates evidence-linked revision.\n\n## Results\n\nThe synthetic candidate achieved a mean accuracy of 0.78. The candidate improved accuracy by 4%.\n',
        'zh': '# 合成分类研究\n\n## 研究动机\n\n此合成示例展示依据证据修订稿件的流程。\n\n## 结果\n\n合成候选方法的平均准确率为 0.78。候选方法将准确率提高了 4%。\n',
    }
    second = {
        'en': first['en'].replace('The candidate improved accuracy by 4%.', 'Across three synthetic fixtures, mean accuracy increased from 0.74 to 0.78, a gain of 4 percentage points.'),
        'zh': first['zh'].replace('候选方法将准确率提高了 4%。', '在三个合成测试样本上，平均准确率从 0.74 提升至 0.78，提高了 4 个百分点。'),
    }
    write_new(root, 'materials/measurements.csv', evidence)
    write_new(root, 'WRITING_BRIEF.md', '# Synthetic writing brief\n\nUse materials/measurements.csv as the sole numeric evidence. Both languages describe the same three fixtures. Revise only the second Results sentence. Preserve the title, Motivation and accepted first Results sentence. Show the scoped diff and carry revision 002 into a fresh session. All feedback and acceptance records are simulated.\n')
    diff = []
    for language in ('en', 'zh'):
        old = f'manuscripts/rev-001/main_{language}.md'
        new = f'manuscripts/rev-002/main_{language}.md'
        write_new(root, old, first[language])
        write_new(root, new, second[language])
        diff.extend(difflib.unified_diff(first[language].splitlines(True), second[language].splitlines(True), fromfile=old, tofile=new))
    write_new(root, 'review/changes.diff', ''.join(diff))
    feedback = {'synthetic': True, 'simulation': 'Illustrative feedback and acceptance for this generated fixture',
                'request': 'State the baseline, sample count and percentage-point gain in the second Results sentence.',
                'scope': 'Results sentence 2 in both languages', 'simulated_accepted_revision': 'rev-002',
                'real_author_acceptance_verified': False}
    write_new(root, 'review/simulated-feedback.json', encoded(feedback))
    refs = {language: {'path': f'manuscripts/rev-002/main_{language}.md', 'sha256': digest(second[language].encode())}
            for language in ('en', 'zh')}
    state = {'schema': 'paper-studio.research-state.v1', 'project_id': PROJECT_ID,
             'current_writing_version': 'synthetic-v1', 'current_writing_revision': 'rev-002',
             'writing_continuation': {'version': 'synthetic-v1', 'revision': 'rev-002',
                                      'status': 'synthetic_candidate', 'source_mode': 'synthetic_fixture',
                                      'author_acceptance': 'simulated_feedback_only', 'source_files': refs},
             'synthetic': True, 'evidence': {'path': 'materials/measurements.csv', 'sha256': digest(evidence.encode())}}
    write_new(root, 'PROJECT_STATE.json', encoded(state))
    write_new(root, 'DEMO_REPORT.md', '# Synthetic quickstart result\n\nThis generated fixture contains two bilingual revisions and simulated author feedback. Both revisions are retained. The second revision changes only Results sentence 2. The CSV has three rows with baseline mean 0.74 and candidate mean 0.78. The change is 4 percentage points.\n\nRead review/changes.diff, then call paper_project_context in a fresh MCP session. The shared continuity checker recomputes both source hashes. If a current source changes, the next context read reports source_conflict. No PDF build is recorded.\n\nThe demo adapter exposes three read-only tools for these generated files. Connect a personal project using the production gateway documented in docs/AI_ONBOARDING.md.\n')
    write_new(root, 'DEMO.json', encoded({'schema': MARKER, 'project_id': PROJECT_ID, 'synthetic': True}))
    return inspect(root)


class DemoGateway:
    """Read-only fixture adapter using the runtime protocol and source checker."""

    def __init__(self, root):
        self.root = project_root(root)
        self.config = {'project_id': PROJECT_ID, 'project_root': str(self.root)}

    def tools(self):
        result = []
        for name in TOOL_NAMES:
            tool = copy.deepcopy(BY_NAME[name])
            tool['description'] = 'Synthetic read-only demo. ' + tool['description']
            if name == 'paper_runtime_status':
                tool['description'] = 'Report this synthetic local adapter and fixture identity.'
            if name == 'paper_read_files':
                tool['description'] = 'Read up to eight generated demo files with SHA-256. This read-only adapter has no edit revision tokens.'
                tool['inputSchema']['properties']['items']['items']['properties'].pop('expected_read_revision', None)
            result.append(tool)
        return result

    def call(self, name, arguments):
        if name not in TOOL_NAMES:
            fail('TOOL_DENIED', 'The synthetic demo exposes three read-only tools')
        schema = next(tool['inputSchema'] for tool in self.tools() if tool['name'] == name)
        validate(arguments, schema)
        if name != 'paper_runtime_status' and arguments.get('project_id') != PROJECT_ID:
            fail('PROJECT_DENIED', 'Use the synthetic project returned by paper_runtime_status')
        if name == 'paper_runtime_status':
            return {'synthetic': True, 'adapter': 'local_read_only_demo', 'native_connection': 'not_configured',
                    'authorized_projects': [{'project_id': PROJECT_ID, 'name': 'Synthetic quickstart'}],
                    'builds_executed': False}
        if name == 'paper_project_context':
            evidence = read_fixture(self.root, 'materials/measurements.csv', 65536)
            return {'synthetic': True, 'project_id': PROJECT_ID,
                    'entrypoints': ['WRITING_BRIEF.md', 'materials/measurements.csv', 'review/changes.diff'],
                    'evidence': {'path': 'materials/measurements.csv', 'sha256': digest(evidence), 'scientific_status': 'synthetic_fixture'},
                    'continuation': continuation_summary(self.config, reader=read_fixture), 'real_author_acceptance_verified': False}
        remaining = arguments.get('max_result_bytes', 262144)
        files = []
        for item in arguments['items']:
            relative = item['path']
            if relative not in FILES:
                fail('PATH_DENIED', 'Only the generated fixture files are readable')
            raw = read_fixture(self.root, relative, 262144)
            lines = raw.decode('utf-8').splitlines(keepends=True)
            start = item.get('start_line', 1)
            limit = item.get('limit', 200)
            text = ''.join(lines[start - 1:start - 1 + limit])
            remaining -= len(encoded({'path': relative, 'text': text}))
            if remaining < 0:
                fail('INPUT_LIMIT', 'Reduce the requested file range')
            files.append({'path': relative, 'sha256': digest(raw), 'start_line': start,
                          'text': text, 'truncated': start - 1 + limit < len(lines)})
        return {'synthetic': True, 'project_id': PROJECT_ID, 'files': files}

    def handle(self, message):
        method = message['method']
        params = message.get('params', {})
        info = {'name': 'webPaperWriting-synthetic-demo', 'version': __version__}
        if method == 'initialize':
            requested = params['protocolVersion']
            if requested not in protocol.LEGACY_VERSIONS:
                raise protocol.ProtocolError(-32602, 'Use a supported legacy initialization version')
            return {'protocolVersion': requested, 'capabilities': {'tools': {}}, 'serverInfo': info,
                    'instructions': 'Synthetic fixtures only. Read runtime identity, then context and the scoped diff. All feedback is simulated.'}
        if method == 'server/discover':
            return {'supportedVersions': list(protocol.SUPPORTED_VERSIONS), 'capabilities': {'tools': {}}}
        if method == 'ping':
            return {}
        if method == 'tools/list':
            return {'tools': self.tools()}
        if method == 'tools/call':
            value = self.call(params['name'], params.get('arguments', {}))
            return {'content': [{'type': 'text', 'text': json.dumps(value, ensure_ascii=False)}], 'structuredContent': value}
        raise protocol.ProtocolError(-32601, 'Unsupported synthetic demo method')


def inspect(root):
    gateway = DemoGateway(root)
    requests = [
        {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18', 'capabilities': {}}},
        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}},
        {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'paper_project_context', 'arguments': {'project_id': PROJECT_ID}}},
    ]
    incoming = io.BytesIO(b''.join(encoded(request).replace(b'\n', b'') + b'\n' for request in requests))
    outgoing = io.StringIO()
    protocol.serve(gateway, incoming, outgoing)
    replies = [json.loads(line) for line in outgoing.getvalue().splitlines()]
    if any('error' in row or row.get('result', {}).get('isError') for row in replies):
        fail('DEMO_PROTOCOL_FAILED', 'The synthetic protocol roundtrip failed')
    return {'synthetic': True, 'project_id': PROJECT_ID, 'project_root': str(gateway.root),
            'stdio_messages_verified': len(replies), 'tool_count': len(replies[1]['result']['tools']),
            'continuation': replies[2]['result']['structuredContent']['continuation'],
            'report': 'DEMO_REPORT.md', 'diff': 'review/changes.diff'}


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    commands.add_parser('create', help='Generate a new synthetic bilingual project').add_argument('--output-root', required=True)
    for name in ('inspect', 'serve'):
        commands.add_parser(name, help='Read synthetic fixture state' if name == 'inspect' else 'Serve the read-only synthetic adapter over stdio').add_argument('--project-root', required=True)
    options = parser.parse_args(argv)
    try:
        if options.action == 'serve':
            protocol.serve(DemoGateway(options.project_root), sys.stdin.buffer, sys.stdout)
            return 0
        result = create(options.output_root) if options.action == 'create' else inspect(options.project_root)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (BootstrapError, StudioError, OSError, ValueError) as error:
        code = getattr(error, 'code', 'DEMO_FAILED')
        print(json.dumps({'error': {'code': code, 'message': str(error)}}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
