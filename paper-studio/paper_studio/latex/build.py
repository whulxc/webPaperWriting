"""Public LaTeX build entry point. Uses the registered isolated worker."""
import json
import re
from pathlib import Path
from paper_studio.security.sandbox import relative_path, run_sandbox
from paper_studio.latex.worker import compatibility_options, directory_options, snapshot_directory


def build_tex(snapshot: Path, output: Path, request: dict, config: dict) -> dict:
    request = dict(request)
    request["tex_path"] = relative_path(request.get("tex_path"))
    if not request["tex_path"].endswith(".tex"):
        raise ValueError("tex_path must identify a .tex file")
    if request.get("build_profile", "xelatex") not in {"pdflatex", "xelatex", "lualatex"}:
        raise ValueError("Unsupported TeX build profile")
    if not isinstance(request.get("use_latexmk", True), bool):
        raise ValueError("use_latexmk must be boolean")
    if request.get('bibliography_engine','auto') not in {'auto','bibtex','biber'}:
        raise ValueError('Unsupported bibliography engine')
    compatibility_options(request)
    for directory in directory_options(request):
        snapshot_directory(Path(snapshot), directory)
    if "working_directory" in request:
        snapshot_directory(Path(snapshot), request["working_directory"])
    execution = run_sandbox(snapshot, output, "latex", request, config, tex=True)
    result = json.loads((Path(output) / "build.json").read_text())
    result["execution"] = execution
    from paper_studio.security.policy import fail
    diagnostics = result.setdefault('diagnostics', {})
    # XeLaTeX's PDF driver emits destination conflicts on stdout, outside the
    # final TeX .log. Check only persistent anchor errors across all passes.
    stdout_path = Path(output) / 'stdout.log'
    if stdout_path.is_file():
        driver_conflicts = [line for line in stdout_path.read_text(errors='replace').splitlines()
            if re.search(r'Object @\S+ already defined|destination with the same identifier|ignoring duplicate destination', line, re.I)]
        diagnostics['label_conflicts'] = list(dict.fromkeys(
            diagnostics.get('label_conflicts', []) + driver_conflicts))
    if diagnostics.get('label_conflicts'):
        fail('MANUSCRIPT_LABEL_CONFLICT', 'TeX reported duplicate labels or PDF destination anchors',
             diagnostics=diagnostics['label_conflicts'])
    if diagnostics.get('unresolved_references') or diagnostics.get('unresolved_citations'):
        fail('MANUSCRIPT_REFERENCE_UNRESOLVED', 'TeX reported unresolved references or citations',
             references=diagnostics.get('unresolved_references', []),
             citations=diagnostics.get('unresolved_citations', []))
    from paper_studio.latex.structure import auxiliary_float_anchors
    limits = config.get('limits', {})
    result['float_anchor_validation'] = auxiliary_float_anchors(output, result['pdf_path'],
        max_files=limits.get('max_files', 2048), max_bytes=limits.get('max_output_bytes', 64 * 1024**2))
    if '_manuscript_structure' in request:
        result['manuscript_structure'] = request['_manuscript_structure']
    return result
