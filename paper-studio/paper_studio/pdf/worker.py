"""PDF page rendering adapted from prior project-scoped review workers.

Structural/font screening and actual PNG rendering remain distinct from human
visual inspection. No automated overflow or scientific correctness claim.
"""
import hashlib
import json
from pathlib import Path
import sys
import runpy


def page_selection(request, total):
    module = runpy.run_path(str(Path(__file__).with_name('selection.py')))
    return module['select_pages'](request, total)


def inspect_pdf(path, request=None):
    from pypdf import PdfReader
    reader = PdfReader(path, strict=True)
    if reader.is_encrypted:
        raise ValueError("Encrypted PDF review is unsupported")
    selection = page_selection(request or {}, len(reader.pages))
    fonts = []
    pages = []
    visited = set()

    def dereference(value):
        return value.get_object() if hasattr(value, "get_object") else value

    def resource_fonts(resources, page_number):
        resources = dereference(resources)
        if not resources:
            return
        for key, reference in dereference(resources.get("/Font", {})).items():
            font = reference.get_object()
            descendants = font.get("/DescendantFonts", [])
            details = [f.get_object() for f in descendants] if descendants else [font]
            embedded = all(bool((f.get("/FontDescriptor") or {}).get_object().get("/FontFile") or
                                (f.get("/FontDescriptor") or {}).get_object().get("/FontFile2") or
                                (f.get("/FontDescriptor") or {}).get_object().get("/FontFile3"))
                           if f.get("/FontDescriptor") else False for f in details)
            if str(font.get("/Subtype")) == "/Type3":
                embedded = bool(font.get("/CharProcs"))
            fonts.append({"page": page_number, "resource": str(key), "base_font": str(font.get("/BaseFont", "")),
                          "subtype": str(font.get("/Subtype", "")), "embedded": embedded})
        for reference in dereference(resources.get("/XObject", {})).values():
            obj = reference.get_object()
            identity = (getattr(reference, "idnum", None), getattr(reference, "generation", None))
            if identity[0] is not None and identity in visited:
                continue
            visited.add(identity)
            if obj.get("/Resources"):
                resource_fonts(obj["/Resources"], page_number)

    for i in selection['selected_pages']:
        page = reader.pages[i-1]
        unit = float(page.get("/UserUnit", 1))
        width, height = float(page.mediabox.width) * unit, float(page.mediabox.height) * unit
        if not 0 < min(width, height) or max(width, height) > 1200:
            raise ValueError("PDF physical page dimensions exceed limit")
        pages.append({"page": i, "size_points": [width, height], "rotation": page.rotation,
                      "cropbox_points": [float(v) * unit for v in page.cropbox],
                      "text_characters": len(page.extract_text() or "")})
        resource_fonts(page.get("/Resources"), i)
    return {**selection, "inspection_scope": "selected_pages", "pages": pages, "fonts": fonts, "all_fonts_embedded": all(f["embedded"] for f in fonts) if fonts else None,
            "font_status": "screened" if fonts else "no_font_resources", "metadata": {str(k): str(v) for k, v in (reader.metadata or {}).items()}}


def main(request):
    if request.get('_annotation_identity') is True:
        module = runpy.run_path('/code/pdf/comment_identity_worker.py')
        module['main'](request)
        return
    path = Path("/input") / request["pdf_path"]
    work = Path("/work")
    if request.get("_evidence_only") is True:
        evidence = runpy.run_path("/code/pdf/evidence_worker.py")
        try:
            result = evidence["extract_pdf"](path, request)
        except evidence["EvidenceError"] as error:
            (work / "evidence-error.json").write_text(json.dumps({"code": error.code, "message": error.message}))
            raise
        (work / "evidence.json").write_text(json.dumps(result, ensure_ascii=False))
        return
    info = inspect_pdf(path, request)
    if request.get("_pdf_backend", "pdfium") == "pymupdf":
        enhanced = runpy.run_path("/code/pdf/pymupdf_review.py")
        result = enhanced["review"](path, work, info)
        (work / "review.json").write_text(json.dumps(result, indent=2))
        return
    import pypdfium2 as pdfium
    document = pdfium.PdfDocument(path)
    rendered = []
    try:
        for number in info['selected_pages']:
            i = number - 1
            page = document[i]
            if max(page.get_size()) > 1200:
                raise ValueError("Rendered PDF page dimensions exceed limit")
            bitmap = page.render(scale=150 / 72)
            image = bitmap.to_pil().convert("RGB")
            destination = work / f"page_{i + 1:03}.png"
            image.save(destination, dpi=(150, 150))
            rendered.append({"page": i + 1, "path": destination.name, "pixels": list(image.size), "dpi": 150,
                             "sha256": hashlib.sha256(destination.read_bytes()).hexdigest()})
            image.close()
            bitmap.close()
            page.close()
    finally:
        document.close()
    result = {"status": "completed", "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
              **info, "rendered_pages": rendered, "visual_status": "not_reviewed",
              "overflow_status": "not_determined_by_structural_checks",
              "limitations": ["Page images require visual inspection; font embedding does not establish readable glyphs or correct layout."]}
    (work / "review.json").write_text(json.dumps(result, indent=2))


if __name__ == "__main__":
    main(json.loads(Path(sys.argv[1]).read_text()))
