"""Build a portable, deterministic source candidate from an explicit allowlist."""
import argparse
import io
from pathlib import Path
import tarfile
import tomllib

from management import paper_editor_package, paper_release


def assets(root):
    _, runtime = paper_release.source_files(root)
    members = {p.relative_to(root).as_posix():p for p in runtime}
    documentation = root / 'distribution' if (root / 'distribution/pyproject.toml').is_file() else root
    for name in ('README.md','USER_GUIDE.md','LICENSE','NOTICE','pyproject.toml','.gitignore'):
        members[name] = documentation / name
    for name in (
        'management/__init__.py','management/paper_editor_package.py','management/paper_review_comments.py',
        'management/public_source.py',
        'management/paper_handoff_import.py','tests/test_paper_handoff_import.py',
        'examples/bilingual_sections/run.py','examples/bilingual_sections/README.md','instances/rebind-instance.template.json',
        'docs/DRIVE_REVIEW_COMMENTS.md',
        'tests/test_paper_bootstrap_handoff.py','tests/test_chapter_workflow.py',
        'tests/test_drive_review_comments.py','tests/test_version_history_bilingual.py',
        'tests/publication_fixture.py', 'tests/test_history_objects.py',
        'tests/test_public_source.py', 'tests/test_paper_editor_style_component.py',
    ):
        members[name] = root / name
    if documentation == root:
        public_extras = (
            '.gitattributes', 'AGENTS.md', 'CHANGELOG.md', 'CONTRIBUTING.md', 'README.zh-CN.md', 'SECURITY.md',
            '.github/ISSUE_TEMPLATE/bug.yml', '.github/ISSUE_TEMPLATE/config.yml',
            '.github/ISSUE_TEMPLATE/feature.yml', '.github/workflows/fixture-checks.yml',
            'docs/AI_ONBOARDING.md', 'docs/COMPATIBILITY.md', 'docs/INSTALL.md', 'docs/WORKFLOW.md',
            'instances/instance.template.json',
            'examples/quickstart/README.md', 'examples/quickstart/workflow.svg',
            'integrations/mcp/export_contracts.py', 'integrations/mcp/stdio-demo.json',
            'integrations/mcp/stdio-project.json', 'integrations/mcp/tools.schema.json',
            'tests/test_demo.py', 'tests/test_public_portability.py',
        )
        for name in public_extras:
            members[name] = root / name
    for name in paper_editor_package.source_manifest()['files']:
        relative = 'skills/paper-editor/' + name
        members[relative] = root / relative
    return members


def write_candidate(output):
    root = Path(__file__).resolve().parents[1]
    members = assets(root)
    with members['pyproject.toml'].open('rb') as metadata:
        project_name = tomllib.load(metadata)['project']['name']
    output = paper_release.ordinary(Path(output))
    from gzip import GzipFile
    # Assemble in memory before exclusive creation so missing inputs never leave a partial candidate.
    archive = io.BytesIO()
    with GzipFile(fileobj=archive, mode='wb', mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode='w|') as tar:
            for name, path in sorted(members.items()):
                paper_release.ordinary(path)
                data = path.read_bytes()
                header = tarfile.TarInfo(f'{project_name}/{name}')
                header.mode = 0o644
                header.size = len(data)
                tar.addfile(header, io.BytesIO(data))
    with output.open('xb') as stream:
        stream.write(archive.getbuffer())
    return output


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    print(write_candidate(parser.parse_args().output))
