"""Review actual PDF bytes in an isolated parser/renderer namespace."""
import json
from pathlib import Path
from paper_studio.security.sandbox import relative_path, run_sandbox
from paper_studio.pdf.selection import validate_selection, select_pages
import hashlib


def review_pdf(snapshot: Path, output: Path, request: dict, config: dict) -> dict:
    request = dict(request)
    validate_selection(request)
    request["pdf_path"] = relative_path(request.get("pdf_path"))
    if not request["pdf_path"].lower().endswith(".pdf"):
        raise ValueError("pdf_path must identify a PDF")
    backend = config.get("toolchain", {}).get("pdf_backend", "pdfium")
    if backend not in {"pdfium", "pymupdf"}:
        raise ValueError("Unsupported trusted PDF backend")
    # Client fields cannot select a parser or weaken trusted limits.
    request["_pdf_backend"] = backend
    execution = run_sandbox(snapshot, output, "pdf", request, config)
    result = json.loads((Path(output) / "review.json").read_text())
    identity = hashlib.sha256((Path(snapshot)/request['pdf_path']).read_bytes()).hexdigest()
    expected = select_pages(request,result.get('page_count',len(result.get('pages',[]))))
    if (result.get('source_sha256')!=identity
            or [p.get('page') for p in result.get('rendered_pages',[])]!=expected['selected_pages']):
        raise ValueError('PDF review output differs from the selected immutable PDF/pages')
    result.update(expected)
    result["execution"] = execution
    return result
