"""Pin v2 library tools/templates beside the existing PaperWriting contract.

The upstream initializer is read as data, never executed. The bundle lives in
sync/ so library examples do not become paper evidence or citation candidates.
Only explicit local initialization uses this adapter; it grants no MCP scopes.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
import io
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import zipfile

from .workspace import (BootstrapError, _atomic, _json, _lock, _project_metadata,
                        _read, digest, encoded, fail, initialize, no_links, relative)

BINDING_SCHEMA = 'paper-editor.library-template-binding.v1'
BUNDLE_SCHEMA = 'paper-editor.library-template-bundle.v1'
BINDING_PATH = 'sync/library_templates.json'
BUNDLE_ROOT = 'sync/library_templates'
IEEE_ZIP = '01_Venues/ieee_IEEEtran_v1.8b_ctan_20261101.zip'
VENDOR_ROOT = '00_Common/figure_kit/vendor/scienceplots_b9b1695'
VENDOR_FILES = ('bright.mplstyle', 'high-contrast.mplstyle', 'ieee.mplstyle',
                'light.mplstyle', 'muted.mplstyle', 'nature.mplstyle',
                'no-latex.mplstyle', 'science.mplstyle', 'vibrant.mplstyle',
                'LICENSE', 'README.md', 'SHA256.txt')
TOOLS = {
    '00_Common/figure_kit/figkit.py': 'tools/sci_hub/figure_kit/figkit.py',
    '00_Common/figure_kit/check_figures.py': 'tools/sci_hub/figure_kit/check_figures.py',
    '00_Common/tools/english_style_audit.py': 'tools/sci_hub/english_style_audit.py',
    '00_Common/tools/pptx_figure_check.py': 'tools/sci_hub/pptx_figure_check.py',
}


@dataclass(frozen=True)
class TemplatePlan:
    library_root: Path
    manifest: dict
    payload: dict

    @property
    def bundle_sha256(self):
        return digest(encoded(self.manifest))

    @property
    def relative_bundle(self):
        return BUNDLE_ROOT + '/' + self.bundle_sha256


def plan_library_templates(library_root, expected_library_manifest_sha256,
                           library_release_id=None, library_venue='generic'):
    """Read bounded pinned source bytes and produce a deterministic local plan."""
    root = no_links(Path(library_root))
    if not root.is_dir():
        fail('LIBRARY_SOURCE_MISSING', 'A readable complete local library is required')
    if not isinstance(expected_library_manifest_sha256, str) or not re.fullmatch('[a-f0-9]{64}', expected_library_manifest_sha256):
        fail('LIBRARY_IDENTITY_REQUIRED', 'Pin the actual LIBRARY_MANIFEST.json SHA256')
    raw_manifest = _read(root / 'LIBRARY_MANIFEST.json', 1024 * 1024)
    if digest(raw_manifest) != expected_library_manifest_sha256:
        fail('LIBRARY_REVISION_CONFLICT', 'Library manifest differs from the explicitly selected SHA256')
    try:
        library = json.loads(raw_manifest)
    except (ValueError, UnicodeError):
        fail('LIBRARY_MANIFEST_INVALID', 'Library manifest must be UTF-8 JSON')
    if not isinstance(library, dict) or library.get('library_version') != '2.0':
        fail('LIBRARY_VERSION_UNSUPPORTED', 'This adapter validates the existing v2.0 template layout')
    if library_venue not in {'ieee-journal', 'ieee-compsoc', 'generic'}:
        fail('LIBRARY_VENUE_UNSUPPORTED', 'Choose the actual supported template family')
    release_id = library_release_id or 'local-v2.0-' + expected_library_manifest_sha256[:16]
    if not isinstance(release_id, str) or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.-]{0,95}', release_id):
        fail('LIBRARY_IDENTITY_REQUIRED', 'Use a bounded explicit library release identity')
    payload = {}; sources = {}; total = 0

    def read(source):
        nonlocal total
        relative(source)
        if source in sources:
            return sources[source][1]
        raw = _read(root / source, 16 * 1024 * 1024); total += len(raw)
        if total > 32 * 1024 * 1024:
            fail('LIBRARY_SOURCE_LIMIT', 'Selected library template inputs exceed32MiB')
        sources[source] = ({'path': source, 'bytes': len(raw), 'sha256': digest(raw)}, raw)
        return raw

    def copy(source, destination):
        raw = read(source)
        if source.endswith('.py'):
            try:
                compile(raw, source, 'exec')
            except (SyntaxError, UnicodeError):
                fail('LIBRARY_TOOL_INVALID', 'A selected upstream tool does not parse')
        payload[destination] = raw

    for source, destination in TOOLS.items():
        copy(source, destination)
    for name in VENDOR_FILES:
        copy(VENDOR_ROOT + '/' + name, 'tools/sci_hub/figure_kit/vendor/scienceplots_b9b1695/' + name)
    vendor_hashes = read(VENDOR_ROOT + '/SHA256.txt').decode('utf-8').splitlines()
    vendor_names = []
    for line in vendor_hashes:
        fields = line.split()
        if len(fields) != 2 or fields[1] not in VENDOR_FILES or not re.fullmatch('[a-f0-9]{64}', fields[0]):
            fail('LIBRARY_VENDOR_MANIFEST_INVALID', 'Vendored style identity list is not the approved layout')
        if digest(read(VENDOR_ROOT + '/' + fields[1])) != fields[0]:
            fail('LIBRARY_VENDOR_HASH_MISMATCH', 'Vendored SciencePlots bytes differ from their recorded identity')
        vendor_names.append(fields[1])
    if len(vendor_names) != len(set(vendor_names)) or set(vendor_names) != set(VENDOR_FILES) - {'SHA256.txt'}:
        fail('LIBRARY_VENDOR_MANIFEST_INVALID', 'Vendored style identity list must cover each approved style/license file exactly once')
    # Extract literal template values without importing or executing the
    # upstream script, whose refresh path deletes tools/sci_hub.
    initializer_path = '00_Common/tools/init_paper_project.py'
    try:
        tree = ast.parse(read(initializer_path).decode('utf-8'))
        figure_template = next(ast.literal_eval(n.value) for n in tree.body
                               if isinstance(n, ast.Assign)
                               and any(isinstance(t, ast.Name) and t.id == 'FIG_TEMPLATE' for t in n.targets))
        evidence_header = next(n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)
                               and isinstance(n.value, str) and n.value.startswith('claim_id,claim_en,section,evidence_file,'))
    except (ValueError, StopIteration, SyntaxError, UnicodeError):
        fail('LIBRARY_TEMPLATE_INVALID', 'The upstream literal figure/evidence template layout differs')
    if not isinstance(figure_template, str) or len(figure_template.encode()) > 128 * 1024 or len(evidence_header) > 1024:
        fail('LIBRARY_TEMPLATE_INVALID', 'Upstream template literals exceed their bounded format')
    compile(figure_template, 'templates/fig_template.py', 'exec')
    payload['templates/fig_template.py'] = figure_template.encode('utf-8')
    payload['templates/claims_evidence.csv'] = evidence_header.encode('utf-8')
    payload['templates/DATA_NEEDED.md'] = b'# Evidence gaps\n\nNo scientific facts are supplied by this template bundle.\n'
    if library_venue != 'generic':
        archive_bytes = read(IEEE_ZIP)
        selected = {'IEEEtran/IEEEtran.cls': 'venue/IEEEtran.cls',
                    'IEEEtran/bibtex/IEEEtran.bst': 'venue/IEEEtran.bst',
                    ('IEEEtran/bare_jrnl_compsoc.tex' if library_venue == 'ieee-compsoc' else 'IEEEtran/bare_jrnl.tex'): 'venue/official_example.tex'}
        try:
            with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
                names = archive.namelist()
                if len(names) > 4096 or len(names) != len(set(names)):
                    fail('LIBRARY_ARCHIVE_INVALID', 'Official template archive has duplicate or excessive entries')
                for info in archive.infolist():
                    p = PurePosixPath(info.filename)
                    if (p.is_absolute() or '..' in p.parts or '\\' in info.filename
                            or stat.S_ISLNK(info.external_attr >> 16)):
                        fail('LIBRARY_ARCHIVE_INVALID', 'Template archive contains an unsafe path or link')
                for name, destination in selected.items():
                    info = archive.getinfo(name)
                    if info.file_size > 4 * 1024 * 1024:
                        fail('LIBRARY_ARCHIVE_INVALID', 'Selected official template member exceeds its bound')
                    payload[destination] = archive.read(name)
        except (zipfile.BadZipFile, KeyError):
            fail('LIBRARY_ARCHIVE_INVALID', 'The selected official template archive is incomplete')
    payload['README.md'] = (
        '# Pinned library template resources\n\n'
        'This read-only bundle supplements the existing PROJECT.yaml contract.\n'
        'It does not create another PAPER_PROJECT or manuscript authority.\n'
        'No current TeX/Bib/figure/data was adopted or replaced.\n'
        'Official example text and placeholder templates are not project evidence.\n'
        'Copy a specifically selected template into the authorized candidate area before adapting it.\n'
        'Do not run the upstream init_paper_project.py refresh or build scripts against an existing paper.\n'
        'Use the existing controlled Paper Studio builder for real manuscript builds.\n'
        'The fig_template.py is a source template, not a registered execution recipe.\n').encode('utf-8')
    manifest = {'schema': BUNDLE_SCHEMA, 'library_version': '2.0', 'library_release_id': release_id,
                'library_manifest_sha256': expected_library_manifest_sha256,
                'venue': library_venue, 'source_files': [row[0] for _, row in sorted(sources.items())],
                'files': [{'path': p, 'bytes': len(raw), 'sha256': digest(raw)} for p, raw in sorted(payload.items())],
                'source_initializer_executed': False, 'active_manuscript_modified': False,
                'cloud_verification': 'not_performed_by_local_template_adapter',
                'scientific_acceptance': 'not_assessed'}
    return TemplatePlan(root, manifest, payload)


def _expected_binding(plan, paper, project_id):
    return {'schema': BINDING_SCHEMA, 'project_id': project_id, 'paper_writing_root': str(paper),
            'library_version': plan.manifest['library_version'],
            'library_release_id': plan.manifest['library_release_id'],
            'library_manifest_sha256': plan.manifest['library_manifest_sha256'],
            'venue': plan.manifest['venue'], 'bundle_sha256': plan.bundle_sha256,
            'bundle_path': plan.relative_bundle,
            'bundle_manifest_path': plan.relative_bundle + '/manifest.json'}


def _verify_bundle(paper, plan):
    directory = no_links(paper / plan.relative_bundle)
    expected = {**plan.payload, 'manifest.json': encoded(plan.manifest)}
    try:
        actual = set()
        for path in directory.rglob('*'):
            no_links(path)
            if path.is_file():
                actual.add(path.relative_to(directory).as_posix())
            elif not path.is_dir():
                fail('LIBRARY_TEMPLATE_TAMPERED', 'A template bundle contains a nonregular member')
        if actual != set(expected) or any(_read(directory / p) != raw for p, raw in expected.items()):
            fail('LIBRARY_TEMPLATE_TAMPERED', 'Pinned template files differ; no files were replaced')
    except OSError:
        fail('LIBRARY_TEMPLATE_TAMPERED', 'Pinned template bundle is missing or unreadable')


def _preflight_binding(paper, project_id, plan):
    pin = no_links(paper / BINDING_PATH)
    if pin.exists():
        old = _json(pin); expected = _expected_binding(plan, paper, project_id)
        if any(old.get(k) != v for k, v in expected.items()):
            fail('LIBRARY_VERSION_PINNED', 'Existing project library version/identity is pinned; propose an explicit upgrade separately')
        _verify_bundle(paper, plan)
        return True
    if (paper / plan.relative_bundle).exists():
        _verify_bundle(paper, plan)
    return False


def install_library_templates(paper_root, project_id, plan):
    """Create a verified versioned bundle and pin; never rewrite existing bytes."""
    paper = no_links(Path(paper_root))
    expected_rows = [{'path': p, 'bytes': len(raw), 'sha256': digest(raw)} for p, raw in sorted(plan.payload.items())]
    if expected_rows != plan.manifest['files']:
        fail('LIBRARY_PLAN_CHANGED', 'Template plan bytes changed after source validation')
    metadata = _project_metadata(paper / 'PROJECT.yaml')
    if metadata.get('project_id') != project_id:
        fail('BOOTSTRAP_PROJECT_MISMATCH', 'Initialize or select the existing project identity before adding templates')
    no_links(paper / 'sync')
    with _lock(paper / '.paper-editor-library-templates.lock'):
        reused = _preflight_binding(paper, project_id, plan)
        if reused:
            return {**_expected_binding(plan, paper, project_id), 'status': 'reused', 'created_files': 0,
                    'source_initializer_executed': False, 'active_manuscript_modified': False}
        destination = no_links(paper / plan.relative_bundle)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            stage = Path(tempfile.mkdtemp(prefix='.library-template-', dir=destination.parent))
            try:
                for p, raw in {**plan.payload, 'manifest.json': encoded(plan.manifest)}.items():
                    relative(p); path = stage / p; path.parent.mkdir(parents=True, exist_ok=True)
                    _atomic(path, raw, exclusive=True); path.chmod(0o400)
                stage.rename(destination)
            finally:
                if stage.exists():
                    shutil.rmtree(stage)
        _verify_bundle(paper, plan)
        pin = {**_expected_binding(plan, paper, project_id), 'first_local_source_root': str(plan.library_root),
               'active_manuscript_modified': False, 'source_initializer_executed': False,
               'file_count': len(plan.payload), 'scientific_acceptance': 'not_assessed'}
        _atomic(paper / BINDING_PATH, encoded(pin), exclusive=True)
        return {**pin, 'status': 'installed', 'created_files': len(plan.payload) + 2}


def select_venue_family(target_venue=None, library_venue=None):
    """A venue name never silently installs an unrelated IEEE manuscript class."""
    if target_venue is not None and not isinstance(target_venue, str):
        fail('BOOTSTRAP_VENUE_INVALID', 'target_venue must be a bounded venue name')
    target = (target_venue or '').strip().casefold()
    if library_venue is not None and library_venue not in {'ieee-journal','ieee-compsoc','generic'}:
        fail('LIBRARY_VENUE_UNSUPPORTED', 'Choose the actual supported template family')
    non_ieee = not target.startswith('ieee') and any(name in target for name in ('information fusion','elsevier','acm','nature','science'))
    if library_venue and library_venue.startswith('ieee-') and non_ieee:
        fail('LIBRARY_VENUE_CONFLICT', 'The selected IEEE template conflicts with the declared target venue')
    return library_venue or ('ieee-journal' if target.startswith('ieee') else 'generic')


def initialize_with_library(*, library_root, expected_library_manifest_sha256,
                            library_release_id=None, library_venue=None, **kwargs):
    """Use the existing initializer once, then add explicitly pinned resources."""
    root = no_links(Path(kwargs['project_root']))
    if kwargs.get('paper_root') is None:
        writing, legacy = root / 'PaperWriting', root / 'PaperWrite'
        if writing.exists() and legacy.exists():
            fail('BOOTSTRAP_MAPPING_REQUIRED', 'Both paper roots exist; explicitly select the existing one')
        paper = legacy if legacy.exists() else writing
    else:
        paper = no_links(Path(kwargs['paper_root']))
    target_venue = kwargs.get('target_venue') or _project_metadata(paper/'PROJECT.yaml').get('target_venue')
    # Retain an existing explicit template pin when no new family is requested.
    # A conflicting declared venue still rejects before initialization writes.
    family = library_venue
    if family is None and (paper/BINDING_PATH).exists():family = _json(paper/BINDING_PATH).get('venue')
    family = select_venue_family(target_venue, family)
    plan = plan_library_templates(library_root, expected_library_manifest_sha256,
                                  library_release_id, family)
    if (root.is_relative_to(plan.library_root) or plan.library_root.is_relative_to(root)
            or paper.is_relative_to(plan.library_root) or plan.library_root.is_relative_to(paper)):
        fail('LIBRARY_PROJECT_ISOLATION', 'Shared library and research/project roots must be independent')
    _preflight_binding(paper, kwargs['project_id'], plan)
    result = initialize(**{**kwargs, 'paper_root': str(paper)})
    added = install_library_templates(result['paper_writing_root'], kwargs['project_id'], plan)
    return {**result, 'library_templates': added}
