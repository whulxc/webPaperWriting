"""Public catalog availability and fail-closed platform boundaries."""
import builtins
import subprocess
import sys
from pathlib import Path

import pytest

from paper_studio.security import policy, sandbox


def test_catalog_imports_when_posix_lock_module_is_unavailable():
    source = '''
import builtins
original = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if name == 'fcntl':
        raise ImportError('synthetic non-POSIX import environment')
    return original(name, *args, **kwargs)
builtins.__import__ = guarded_import
from paper_studio.gateway.main import Gateway
from paper_studio.security.policy import StudioError, require_posix_filesystem
gateway = Gateway('unused')
gateway.cfg = lambda: {'project_id': 'synthetic-paper'}
reply = gateway.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list', 'params': {}})
assert reply['tools']
try:
    require_posix_filesystem()
except StudioError as error:
    assert error.code == 'PLATFORM_UNSUPPORTED'
else:
    raise AssertionError('protected filesystem unexpectedly enabled')
'''
    result = subprocess.run([sys.executable, '-B', '-c', source], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_missing_posix_primitives_refuses_operations_before_creating_state(tmp_path, monkeypatch):
    monkeypatch.setattr(policy, 'fcntl', None)
    with pytest.raises(policy.StudioError) as error:
        with policy.locked({'state_root': str(tmp_path / 'new-state')}):
            pytest.fail('Unsupported lock entered')
    assert error.value.code == 'PLATFORM_UNSUPPORTED'
    assert not (tmp_path / 'new-state').exists()
    with pytest.raises(policy.StudioError, match='POSIX'):
        policy.safe_write_new(tmp_path, 'new.txt', b'synthetic')
    assert not (tmp_path / 'new.txt').exists()


def test_builds_reject_unsupported_platform_before_touching_output(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox.sys, 'platform', 'darwin')
    with pytest.raises(RuntimeError, match='Linux or WSL 2'):
        sandbox.run_sandbox(tmp_path / 'input', tmp_path / 'output', 'latex', {}, {})
    assert not (tmp_path / 'output').exists()
