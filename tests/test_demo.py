"""Synthetic demo regressions for continuation and read-only protocol scope."""
import io
import json

import pytest

from paper_studio import demo
from paper_studio.gateway import protocol


def test_demo_retains_both_revisions_and_source_identity(tmp_path):
    root = tmp_path / 'fixture'
    result = demo.create(root)
    assert result['stdio_messages_verified'] == 3
    assert result['continuation']['status'] == 'read'
    assert {row['status'] for row in result['continuation']['sources']} == {'match'}
    assert result['continuation']['author_acceptance_verified'] is False
    for language in ('en', 'zh'):
        initial = (root / f'manuscripts/rev-001/main_{language}.md').read_text(encoding='utf-8')
        revised = (root / f'manuscripts/rev-002/main_{language}.md').read_text(encoding='utf-8')
        assert initial.split('\n\n')[:-1] == revised.split('\n\n')[:-1]
        assert initial.splitlines()[-1].split('.')[0] == revised.splitlines()[-1].split('.')[0]
    (root / 'manuscripts/rev-002/main_en.md').write_text('changed synthetic source\n', encoding='utf-8')
    assert demo.inspect(root)['continuation']['status'] == 'source_conflict'


def test_demo_refuses_overwrite_and_relative_output(tmp_path):
    root = tmp_path / 'fixture'
    demo.create(root)
    original = (root / 'DEMO.json').read_bytes()
    with pytest.raises(FileExistsError):
        demo.create(root)
    assert (root / 'DEMO.json').read_bytes() == original
    with pytest.raises(demo.BootstrapError):
        demo.create('relative-demo')


def test_demo_stdio_is_bounded_read_only_and_project_scoped(tmp_path):
    root = tmp_path / 'fixture'
    demo.create(root)
    gateway = demo.DemoGateway(root)
    calls = [
        {'name': 'paper_read_files', 'arguments': {'project_id': demo.PROJECT_ID, 'items': [{'path': 'materials/measurements.csv'}]}},
        {'name': 'paper_project_context', 'arguments': {'project_id': 'other-project'}},
        {'name': 'paper_read_files', 'arguments': {'project_id': demo.PROJECT_ID, 'items': [{'path': '../outside.md'}]}},
        {'name': 'paper_edit_files', 'arguments': {}},
    ]
    frames = [{'jsonrpc': '2.0', 'id': number, 'method': 'tools/call', 'params': call}
              for number, call in enumerate(calls, 1)]
    frames.insert(0, {'jsonrpc': '2.0', 'method': 'notifications/initialized'})
    incoming = io.BytesIO(b''.join(json.dumps(frame).encode() + b'\n' for frame in frames))
    outgoing = io.StringIO()
    protocol.serve(gateway, incoming, outgoing)
    replies = [json.loads(line) for line in outgoing.getvalue().splitlines()]
    assert len(replies) == 4
    read = replies[0]['result']['structuredContent']['files'][0]
    assert read['sha256'] == demo.digest((root / 'materials/measurements.csv').read_bytes())
    assert read['text'].startswith('fixture,baseline_accuracy,')
    assert [reply['result']['structuredContent']['error']['code'] for reply in replies[1:]] == [
        'PROJECT_DENIED', 'PATH_DENIED', 'TOOL_DENIED']
    marker = (root / 'DEMO.json').read_bytes()
    assert marker == demo.encoded({'schema': demo.MARKER, 'project_id': demo.PROJECT_ID, 'synthetic': True})


def test_demo_requires_generated_marker(tmp_path):
    root = tmp_path / 'unrelated'
    root.mkdir()
    (root / 'DEMO.json').write_text('{}', encoding='utf-8')
    with pytest.raises(demo.StudioError) as caught:
        demo.DemoGateway(root)
    assert caught.value.code == 'DEMO_IDENTITY'


def test_demo_runs_with_posix_project_backend_unavailable(tmp_path, monkeypatch):
    from paper_studio.security import policy
    with monkeypatch.context() as patch:
        patch.setattr(policy, 'fcntl', None)
        patch.delattr(policy.os, 'O_NOFOLLOW', raising=False)
        root = tmp_path / 'portable-fixture'
        result = demo.create(root)
        assert result['continuation']['status'] == 'read'
        assert demo.inspect(root)['stdio_messages_verified'] == 3
        with pytest.raises(policy.StudioError):
            policy.safe_read(root, 'PROJECT_STATE.json')
