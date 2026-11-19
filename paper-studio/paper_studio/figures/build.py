"""Reusable figure engines with immutable inputs and explicit output roots."""
import json
from pathlib import Path
from paper_studio.security.sandbox import relative_path, run_sandbox


def build_figure(snapshot: Path, output: Path, request: dict, config: dict) -> dict:
    request = dict(request)
    engine = request.get("engine", "python")
    if engine == "matlab":
        raise ValueError("MATLAB adapter is unsupported; no arbitrary Windows scripts are launched")
    if engine not in {"python", "svg", "tikz"}:
        raise ValueError("Unsupported figure engine")
    request["engine"] = engine
    request["source_path"] = relative_path(request.get("source_path"))
    expected = {"python": ".py", "svg": ".svg", "tikz": ".tex"}[engine]
    if not request["source_path"].lower().endswith(expected):
        raise ValueError("Source extension does not match figure engine")
    paths = request.get("data_paths", [])
    if not isinstance(paths, list):
        raise ValueError("data_paths must be a list")
    request["data_paths"] = [relative_path(p) for p in paths]
    if not isinstance(request.get("parameters", {}), dict):
        raise ValueError("parameters must be an object")
    execution = run_sandbox(snapshot, output, "figure", request, config, tex=engine == "tikz")
    result = json.loads((Path(output) / "figure.json").read_text())
    result["execution"] = execution
    return result
