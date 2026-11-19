"""Fixed isolated figure worker, using project-scoped sandbox and figure receipt patterns.

Python sources are user code, confined to the namespace; only this trusted
worker can load configured isolated addons. Source/data hashes are retained by
the caller's source_manifest.json and unmodified snapshot.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET


def validate_svg(path):
    data = path.read_bytes()
    if len(data) > 16 * 1024**2:
        raise ValueError("SVG exceeds parser input limit")
    lowered = data.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered or b"<?xml-stylesheet" in lowered:
        raise ValueError("SVG entities, doctypes and external stylesheets are forbidden")
    root = ET.fromstring(data)
    if root.tag.split("}")[-1] != "svg":
        raise ValueError("SVG root element required")
    for element in root.iter():
        if element.tag.split("}")[-1].lower() in {"script", "foreignobject", "image", "feimage", "iframe", "object", "embed",
                                                        "audio", "video", "animate", "animatemotion", "animatetransform", "set", "discard"}:
            raise ValueError("SVG scripts, embedded documents and external images are forbidden")
        values = list(element.attrib.values()) + ([element.text] if element.text else [])
        for key, value in element.attrib.items():
            if key.lower().split("}")[-1].startswith("on"):
                raise ValueError("SVG event handlers are forbidden")
            if key.split("}")[-1].lower() in {"href", "src"} and not value.startswith("#"):
                raise ValueError("SVG external references are forbidden")
        for value in values:
            if "\\" in value:
                raise ValueError("Escaped SVG CSS/resource values are forbidden")
            if re.search(r"@import", value, re.I):
                raise ValueError("SVG external resources are forbidden")
            for reference in re.findall(r"url\s*\((.*?)\)", value, re.I):
                if not reference.strip(" \t\r\n\"'").startswith("#"):
                    raise ValueError("SVG external CSS resources are forbidden")
    return root


def main(request):
    work = Path("/work")
    source = Path("/input") / request["source_path"]
    if not source.is_file():
        raise FileNotFoundError("Figure source missing")
    for data in request.get("data_paths", []):
        if not (Path("/input") / data).is_file():
            raise FileNotFoundError("Figure data missing: " + data)
    engine = request["engine"]
    before = set(work.rglob("*"))
    if engine == "python":
        # Fresh interpreter excludes configured addons and inherited PYTHONPATH.
        # Input cwd is read-only and PAPER_OUTPUT_DIR is the sole output mount.
        subprocess.run([sys.executable, "-I", "-B", str(source)], check=True, stdin=subprocess.DEVNULL, cwd="/input")
    elif engine == "svg":
        validate_svg(source)
        if Path("/opt/addons").is_dir():
            sys.path.insert(0, "/opt/addons")
        try:
            import cairosvg
        except ImportError as exc:
            raise RuntimeError("SVG conversion requires CairoSVG in configured isolated python_addons") from exc
        shutil.copyfile(source, work / "figure.svg")
        # Validation rejects all external resources before CairoSVG parsing;
        # network and host paths are independently inaccessible in this namespace.
        cairosvg.svg2pdf(bytestring=source.read_bytes(), write_to=str(work / "figure.pdf"), unsafe=False)
        cairosvg.svg2png(bytestring=source.read_bytes(), write_to=str(work / "figure.png"), unsafe=False)
    else:
        module = runpy.run_path("/code/latex/worker.py")
        module["main"]({"tex_path": request["source_path"], "build_profile": "xelatex", "use_latexmk": True})
    artifacts = []
    inspect_pdf = runpy.run_path("/code/pdf/worker.py")["inspect_pdf"]
    for path in sorted(set(work.rglob("*")) - before):
        if not path.is_file() or path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".pdf", ".svg"}:
            continue
        metadata = {}
        if path.suffix.lower() == ".svg":
            # Matplotlib emits this fixed SVG 1.1 declaration. Remove its
            # external DTD reference without changing graphic elements; all
            # other declarations/entities continue to fail validation.
            original = path.read_bytes()
            sanitized = re.sub(br'<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1\.1//EN"\s+"http://www\.w3\.org/Graphics/SVG/1\.1/DTD/svg11\.dtd">', b'', original)
            if sanitized != original:
                path.write_bytes(sanitized)
            root = validate_svg(path)
            metadata = {"width": root.get("width"), "height": root.get("height"), "viewBox": root.get("viewBox"),
                        "font_policy": "inspect source; text may depend on installed fonts",
                        "standard_svg11_doctype_removed": sanitized != original}
        elif path.suffix.lower() == ".pdf":
            metadata = inspect_pdf(path)
        else:
            from PIL import Image
            with Image.open(path) as image:
                metadata = {"pixels": list(image.size), "mode": image.mode, "dpi": image.info.get("dpi")}
        artifacts.append({"path": path.relative_to(work).as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "metadata": metadata})
    if not artifacts:
        raise RuntimeError("Figure script produced no PDF, PNG, JPEG or SVG artifacts")
    result = {"status": "completed", "engine": engine, "artifacts": artifacts, "parameters": request.get("parameters", {}),
              "data_paths": request.get("data_paths", []), "visual_status": "not_reviewed",
              "scientific_status": "source and data preserved; transformations require scientific review"}
    (work / "figure.json").write_text(json.dumps(result, indent=2))


if __name__ == "__main__":
    main(json.loads(Path(sys.argv[1]).read_text()))
