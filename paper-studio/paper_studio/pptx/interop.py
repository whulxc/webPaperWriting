"""Optional read-only python-pptx interoperability probe, never a writer.

The existing package/XML validator remains authoritative. The extra reader runs
in an isolated Python process so trusted addon imports do not alter provider
module state. No Presentation.save or fit_text call belongs on this path.
"""
from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path
import subprocess
import sys

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from paper_studio.pptx.document import MAX_PACKAGE, _unzip, _validate_parts, read_package
else:
    from .document import MAX_PACKAGE, _unzip, _validate_parts, read_package

MAX_ROWS = 100
MAX_RESULT_BYTES = 128 * 1024
TIMEOUT_SECONDS = 15


def _addon_path(value):
    if not isinstance(value, (str, Path)):
        raise ValueError('Trusted python_addons must be an absolute directory')
    path = Path(value)
    if not path.is_absolute() or str(path) != os.path.abspath(path):
        raise ValueError('Trusted python_addons must be canonical and absolute')
    if any(p.is_symlink() for p in [path, *path.parents]):
        raise ValueError('Trusted python_addons must not traverse symlinks')
    return path


def check_interoperability(path: Path, config: dict | None = None) -> dict:
    """Validate first; optionally open the exact bytes with a second reader.

    Only trusted configuration supplies the import directory, never a request
    field. Missing dependencies or reader errors are advisory, explicit states;
    package/XML validation errors continue to raise as before.
    """
    raw, _ = read_package(path)
    return _check_validated(raw, config)


def _check_validated(raw: bytes, config: dict | None = None) -> dict:
    """Internal fast path: raw must come from the existing read_package gate."""
    toolchain = (config or {}).get('toolchain', {})
    addons = toolchain.get('python_addons') if isinstance(toolchain, dict) else None
    common = {'backend': 'python-pptx', 'read_only': True, 'source_saved': False,
              'scope': 'reader interoperability, not XSD validation, rendering or scientific validation'}
    if not addons:
        return {**common, 'status': 'not_run', 'reason': 'python_addons_not_configured'}
    try:
        directory = _addon_path(addons)
    except ValueError:
        return {**common, 'status': 'not_run', 'reason': 'invalid_trusted_addons_path'}
    if not directory.is_dir():
        return {**common, 'status': 'not_run', 'reason': 'python_addons_unavailable'}
    if not (directory/'pptx/__init__.py').is_file():
        return {**common, 'status': 'not_run', 'reason': 'dependency_unavailable', 'missing_module': 'pptx'}
    command = [sys.executable, '-I', '-B', str(Path(__file__).resolve()), '--reader', '--addons', str(directory)]
    try:
        result = subprocess.run(command, input=raw, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=TIMEOUT_SECONDS, check=False,
                                env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'PYTHONDONTWRITEBYTECODE': '1'})
    except subprocess.TimeoutExpired:
        return {**common, 'status': 'not_run', 'reason': 'reader_timeout'}
    except OSError as exc:
        return {**common, 'status': 'not_run', 'reason': 'reader_launch_failed', 'error_type': type(exc).__name__}
    if result.returncode or len(result.stdout) > MAX_RESULT_BYTES:
        return {**common, 'status': 'not_run', 'reason': 'reader_process_failed', 'returncode': result.returncode}
    try:
        report = json.loads(result.stdout)
        if not isinstance(report, dict) or report.get('status') not in ('checked', 'not_run', 'reader_error'):
            raise ValueError('Invalid reader result')
    except (ValueError, UnicodeError):
        return {**common, 'status': 'not_run', 'reason': 'invalid_reader_result'}
    return {**common, **report}


def _read(raw, addons):
    # The standalone helper also fails closed if invoked without the parent.
    budget = {'entries': 0, 'expanded': 0}
    parts = _unzip(raw, budget)
    _validate_parts(parts, budget)
    del parts
    import gc
    gc.collect()
    directory = _addon_path(addons)
    sys.path.insert(0, str(directory))
    try:
        import pptx
        from pptx import Presentation
    except ImportError as exc:
        return {'status': 'not_run', 'reason': 'dependency_unavailable', 'missing_module': (exc.name or 'unknown')[:100]}
    origin = Path(pptx.__file__).resolve()
    if not origin.is_relative_to(directory.resolve()):
        return {'status': 'not_run', 'reason': 'reader_outside_trusted_addons'}
    try:
        presentation = Presentation(io.BytesIO(raw))
        slides = []
        rows = []
        total = 0
        def walk(shapes, slide, ancestors=()):
            nonlocal total
            for shape in shapes:
                total += 1
                ident = str(shape.shape_id)
                if len(rows) < MAX_ROWS:
                    row = {'slide': slide, 'shape_id': ident, 'name': str(shape.name)[:120],
                           'ancestors': list(ancestors), 'shape_type': str(shape.shape_type)[:80],
                           'geometry_emu': [int(v) if v is not None else None for v in
                                            (shape.left, shape.top, shape.width, shape.height)],
                           'has_text_frame': bool(shape.has_text_frame),
                           'has_table': bool(shape.has_table), 'has_chart': bool(shape.has_chart)}
                    if shape.has_table:
                        row['table_size'] = [len(shape.table.rows), len(shape.table.columns)]
                    if shape.has_chart:
                        row['chart_series_count'] = len(shape.chart.series)
                    rows.append(row)
                if hasattr(shape, 'shapes'):
                    walk(shape.shapes, slide, (*ancestors, ident))
        for index, slide in enumerate(presentation.slides, 1):
            before = total
            walk(slide.shapes, index)
            slides.append({'slide': index, 'object_count': total-before})
        return {'status': 'checked', 'version': str(pptx.__version__),
                'slide_count': len(presentation.slides), 'object_count': total,
                'slide_size_emu': [int(presentation.slide_width), int(presentation.slide_height)],
                'slides': slides, 'objects': rows, 'objects_truncated': total > len(rows),
                'geometry_scope': 'native local x/y/width/height; group transforms are not flattened by this reader'}
    except Exception as exc:
        return {'status': 'reader_error', 'reason': 'optional_reader_rejected',
                'error_type': type(exc).__name__, 'message': str(exc)[:500]}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--reader', action='store_true', required=True)
    parser.add_argument('--addons', required=True)
    args = parser.parse_args()
    data = sys.stdin.buffer.read(MAX_PACKAGE+1)
    if len(data) > MAX_PACKAGE:
        raise ValueError('Reader input exceeds package byte budget')
    report = _read(data, args.addons)
    encoded = json.dumps(report, ensure_ascii=False).encode()
    if len(encoded) > MAX_RESULT_BYTES:
        raise ValueError('Reader result exceeds bounded response size')
    sys.stdout.buffer.write(encoded)
