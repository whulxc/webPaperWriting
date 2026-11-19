"""The public export contains its portable sources and excludes local records."""
from pathlib import Path, PurePosixPath
import tarfile

import pytest

from management import public_source


def test_public_source_is_reproducible_and_uses_only_public_inputs(tmp_path):
    first = public_source.write_candidate(tmp_path / 'first.tar.gz')
    second = public_source.write_candidate(tmp_path / 'second.tar.gz')
    assert first.read_bytes() == second.read_bytes()
    with tarfile.open(first) as archive:
        names = set(archive.getnames())
        for member in archive.getmembers():
            path = PurePosixPath(member.name)
            assert member.isfile() and not path.is_absolute()
            assert path.parts[0] == 'webPaperWriting' and '..' not in path.parts
            assert not {'credentials', '__pycache__', 'audit'} & set(path.parts)
            assert 'instances/local/' not in member.name
        assert 'webPaperWriting/PROJECT_MEMORY.md' not in names
        for name in ('README.md', 'LICENSE', 'NOTICE', '.gitignore',
                     'management/public_source.py', 'skills/paper-editor/SKILL.md'):
            assert 'webPaperWriting/' + name in names
        root = Path(public_source.__file__).resolve().parents[1]
        assert archive.extractfile('webPaperWriting/README.md').read() == public_source.assets(root)['README.md'].read_bytes()


def test_public_source_requires_absolute_output_and_preserves_existing_file(tmp_path):
    with pytest.raises(ValueError, match='absolute'):
        public_source.write_candidate('relative.tar.gz')
    output = tmp_path / 'existing.tar.gz'
    output.write_bytes(b'existing output')
    with pytest.raises(FileExistsError):
        public_source.write_candidate(output)
    assert output.read_bytes() == b'existing output'
