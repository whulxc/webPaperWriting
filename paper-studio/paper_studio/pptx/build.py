"""Public PPTX engine. Paths come from the trusted snapshot/job manager."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess

from .create import create_deck, mm, color
from .document import apply_program, read_package, validate_program
from .inventory import inspect_deck, page_slide_inventory
from .diagnostics import diagnose_layout
from .interop import _check_validated


def _json(path, value):
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _ordinary_path(path):
    path = Path(path)
    if not path.is_absolute() or any(p.is_symlink() for p in [path, *path.parents]):
        raise ValueError('Use an absolute path without symlink components')
    return path.resolve()


def _private_work_path(path, root):
    """Validate explicit private job ownership; no hardcoded production paths."""
    path, root = _ordinary_path(path), _ordinary_path(root)
    if path == root or not path.is_relative_to(root):
        raise ValueError('Output must be a child of the configured PPTX job root')
    return path


def _source(snapshot, relative):
    if not isinstance(relative, str) or not relative or '\\' in relative or ':' in relative:
        raise ValueError('pptx_path must be a relative POSIX path')
    pure = PurePosixPath(relative)
    if pure.is_absolute() or '..' in pure.parts or str(pure) != relative:
        raise ValueError('Unsafe pptx_path')
    snapshot = _ordinary_path(snapshot)
    path = _ordinary_path(snapshot / relative)
    if not path.is_relative_to(snapshot):
        raise ValueError('PPTX source escapes snapshot')
    read_package(path)
    return path


def inspect_pptx(path: Path, offset: int = 0, limit: int = 25, *, config: dict | None = None) -> dict:
    """Bounded global object page plus slide, master/theme and safety metadata.

    Offset counts all native objects in presentation order, including groups.
    Detailed geometry/native XML remains available through inventory/document.
    """
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('Use offset>=0 and limit=1..100')
    inventory = inspect_deck(path)
    raw, parts = read_package(path)
    if hashlib.sha256(raw).hexdigest() != inventory['input_sha256']:
        raise ValueError('Source changed across inspection reads; retry with the latest source')
    width, height = [int(inventory['slide_size_emu'][key])/36000 for key in ('cx', 'cy')]
    rows = []
    summaries = []
    warnings = []
    count = 0
    for slide in inventory['slides']:
        summaries.append({k: v for k, v in slide.items() if k != 'objects'})
        for obj in slide['objects']:
            box = obj.get('bbox_mm')
            if box and (box[0] < -.01 or box[1] < -.01 or box[0]+box[2] > width+.01 or box[1]+box[3] > height+.01):
                if len(warnings) < 100:
                    warnings.append({'slide': slide['slide'], 'shape_id': obj['id'], 'kind': 'outside_slide', 'bbox_mm': box})
        start = max(0, offset-count)
        if count+slide['object_count'] > offset and len(rows) < limit:
            page = page_slide_inventory(inventory, slide['slide'], start, min(100, limit-len(rows)))
            rows.extend({'slide': slide['slide'], **obj} for obj in page['objects'])
        count += slide['object_count']
    # A page is also byte-bounded: a single text-heavy object cannot defeat paging.
    while rows and len(json.dumps(rows, ensure_ascii=False).encode()) > 512*1024:
        if len(rows) == 1:
            row = rows[0]
            rows[0] = {'slide': row['slide'], 'id': row['id'], 'name': row['name'],
                       'type': row['type'], 'detail_omitted': 'Object exceeds 512 KiB response budget'}
            break
        rows.pop()
    end = offset+len(rows)
    metadata = [{'part': name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
                for name, data in sorted(parts.items())
                if name.startswith(('ppt/slideMasters/', 'ppt/slideLayouts/', 'ppt/theme/')) and name.endswith('.xml')]
    interoperability = _check_validated(raw, config)
    if interoperability.get('status') == 'checked':
        interoperability['native_inventory_comparison'] = {
            'slide_count_matches': interoperability['slide_count'] == inventory['slide_count'],
            'object_count_matches': interoperability['object_count'] == inventory['object_count'],
            'canvas_matches': interoperability['slide_size_emu'] == [int(inventory['slide_size_emu'][k]) for k in ('cx', 'cy')],
            'scope': 'counts and canvas only; geometry records are local native values, not rendered equivalence'}
    return {'schema': 'paper-studio.pptx.inspect.v1',
            **{k: inventory[k] for k in ('input_sha256', 'input_bytes', 'slide_count', 'object_count', 'slide_size_emu', 'media_entry_count')},
            'offset': offset, 'limit': limit, 'next_offset': end if end < count else None,
            'objects': rows, 'slides': summaries, 'masters_layouts_themes': metadata[:100],
            'masters_layouts_themes_total': len(metadata), 'warnings': warnings,
            'warnings_truncated': len(warnings) == 100,
            'text_overflow': {'status': 'requires_native_render', 'reason': 'OOXML has no reliable rendered text metrics'},
            'layout_diagnostics': diagnose_layout(inventory, parts),
            'interoperability': interoperability,
            'validation': 'bounded package, XML, relationships, active content and static-workbook validation; not full OOXML schema validation'}


def semantic_program(commands, source):
    """Translate convenient commands to the mature native OOXML operations."""
    if not isinstance(commands, list) or len(commands) > 100:
        raise ValueError('commands must contain at most 100 objects')
    inventory = inspect_deck(source)
    operations = []
    for command in commands:
        if not isinstance(command, dict) or not set(command) <= {'op', 'slide', 'shape', 'shape_id', 'text', 'x', 'y', 'width', 'height', 'fill', 'line_color', 'line_width'}:
            raise ValueError('Unknown semantic command field')
        target = {key: command[key] for key in ('slide', 'shape', 'shape_id') if key in command}
        validate_program({'operations': [{'op': 'text', 'target': target, 'text': ''}]})
        matches = [obj for slide in inventory['slides'] if slide['slide'] == target['slide']
                   for obj in slide['objects'] if obj['name'] == target.get('shape') or obj['id'] == target.get('shape_id')]
        if len(matches) != 1:
            raise ValueError('Semantic selection must match one object')
        shape = matches[0]
        op = command.get('op')
        if op == 'text':
            if set(command) - {'op', 'slide', 'shape', 'shape_id', 'text'}:
                raise ValueError('Text command accepts text only')
            operations.append({'op': 'text', 'target': target, 'text': command.get('text')})
        elif op == 'position':
            allowed = {'op', 'slide', 'shape', 'shape_id', 'x', 'y', 'width', 'height'}
            if set(command)-allowed or not set(command) & {'x', 'y', 'width', 'height'}:
                raise ValueError('Position requires x/y/width/height')
            base = 'p:xfrm' if shape['type'] == 'graphicFrame' else 'p:grpSpPr/a:xfrm' if shape['type'] == 'grpSp' else 'p:spPr/a:xfrm'
            for keys, suffix in [(('x', 'y'), '/a:off'), (('width', 'height'), '/a:ext')]:
                attrs = {({'width': 'cx', 'height': 'cy'}.get(key, key)): str(mm(command[key], .001 if key in ('width', 'height') else -2000)) for key in keys if key in command}
                if attrs:
                    operations.append({'op': 'set', 'target': {**target, 'path': base+suffix}, 'attrs': attrs})
        elif op == 'style':
            if shape['type'] not in ('sp', 'cxnSp'):
                raise ValueError('Style command supports shapes/textboxes/connectors; use program for other objects')
            if set(command)-{'op', 'slide', 'shape', 'shape_id', 'fill', 'line_color', 'line_width'} or not set(command)&{'fill', 'line_color', 'line_width'}:
                raise ValueError('Style requires fill/line_color/line_width')
            # Operate on a copy of the actual native subtree, preserving geometry,
            # effects, connection attachments and all unknown object properties.
            from .document import _parse, _serialize, _find_path, _elements
            _, parts = read_package(source)
            part = inventory['slides'][target['slide']-1]['part']
            document = _parse(parts[part])
            prop = next(n for n in document.getElementsByTagNameNS('http://schemas.openxmlformats.org/presentationml/2006/main', 'cNvPr') if n.getAttribute('id') == shape['id'])
            sppr = _find_path(prop.parentNode.parentNode, 'p:spPr')
            def replace_fill(parent, value):
                old = [n for n in _elements(parent) if n.localName in ('noFill', 'solidFill', 'gradFill', 'blipFill', 'pattFill', 'grpFill')]
                new = _parse(f'<a:noFill xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"/>' if value is None else f'<a:solidFill xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><a:srgbClr val="{color(value)}"/></a:solidFill>').documentElement
                if old:
                    parent.replaceChild(document.importNode(new, True), old[0])
                    for other in old[1:]:
                        parent.removeChild(other)
                else:
                    children = _elements(parent)
                    # Fill follows transform/geometry and precedes line/effects.
                    anchor = next((n for n in children if n.localName in ('ln', 'effectLst', 'effectDag', 'scene3d', 'sp3d', 'extLst')), None)
                    parent.insertBefore(document.importNode(new, True), anchor)
            if 'fill' in command:
                replace_fill(sppr, command['fill'])
            if set(command)&{'line_color', 'line_width'}:
                line = next((n for n in _elements(sppr) if n.localName == 'ln'), None)
                if line is None:
                    line = document.createElementNS('http://schemas.openxmlformats.org/drawingml/2006/main', 'a:ln')
                    anchor = next((n for n in _elements(sppr) if n.localName in ('effectLst', 'effectDag', 'scene3d', 'sp3d', 'extLst')), None)
                    sppr.insertBefore(line, anchor)
                if 'line_color' in command:
                    replace_fill(line, command['line_color'])
                if 'line_width' in command:
                    from .create import number
                    line.setAttribute('w', str(round(number(command['line_width'], 0, 100)*12700)))
            # A detached replacement must carry inherited namespace bindings.
            fragment = sppr.cloneNode(deep=True)
            ancestor = sppr
            while ancestor is not None and ancestor.nodeType == ancestor.ELEMENT_NODE:
                for index in range(ancestor.attributes.length):
                    attr = ancestor.attributes.item(index)
                    if (attr.name == 'xmlns' or attr.name.startswith('xmlns:')) and not fragment.hasAttribute(attr.name):
                        fragment.setAttribute(attr.name, attr.value)
                ancestor = ancestor.parentNode
            operations.append({'op': 'replace', 'target': {**target, 'path': 'p:spPr'}, 'xml': _serialize(fragment).decode()})
        else:
            raise ValueError('Semantic op must be text|position|style; use program for full native edits')
    # Style commands are snapshots of original spPr, so prevent clobbering prior
    # position/style edits on the same shape. Translate each command sequentially
    # in run_pptx instead of applying this function to multiple commands there.
    validate_program({'operations': operations})
    return {'operations': operations}


def _windows_path(path):
    value = str(Path(path).absolute())
    match = re.fullmatch(r'/mnt/([a-z])/(.*)', value)
    if match:
        return match[1].upper()+':\\'+match[2].replace('/', '\\')
    if value.startswith('/home/'):
        return '\\\\wsl.localhost\\Ubuntu'+value.replace('/', '\\')
    raise ValueError('Windows export requires a mounted drive or WSL home source')


def run_pptx(snapshot: Path, output: Path, request: dict, config: dict) -> dict:
    """Build one fresh private job. Return artifacts plus truthful stage receipts."""
    if not isinstance(request, dict) or not set(request) <= {'action', 'pptx_path', 'program', 'commands', 'deck'}:
        raise ValueError('Unknown PPTX request fields')
    action = request.get('action')
    if action not in ('create', 'edit', 'export'):
        raise ValueError('action must be create|edit|export')
    if not isinstance(config, dict) or type(config.get('windows_render', False)) is not bool:
        raise ValueError('windows_render must be boolean')
    root = config.get('pptx_job_root', config.get('artifact_root'))
    if not root:
        raise ValueError('An explicit pptx_job_root or artifact_root is required')
    output = _private_work_path(output, root)
    snapshot = _ordinary_path(snapshot)
    if not snapshot.is_dir() or output.is_relative_to(snapshot) or snapshot.is_relative_to(output):
        raise ValueError('Snapshot and output must be separate directories')
    if output.exists():
        raise FileExistsError('A PPTX build requires a fresh output directory')
    if action == 'create':
        if set(request)-{'action', 'deck'} or 'deck' not in request:
            raise ValueError('Create accepts deck only')
        source = None
    else:
        if 'deck' in request or (action == 'export' and set(request)-{'action', 'pptx_path'}):
            raise ValueError('Invalid fields for edit/export')
        source = _source(snapshot, request.get('pptx_path'))
    if action == 'export' and not config.get('windows_render', False):
        raise ValueError('Export requires verified Windows desktop rendering enabled in trusted config')
    if action == 'edit' and 'program' not in request and 'commands' not in request:
        raise ValueError('Edit requires program and/or commands')
    if 'program' in request:
        validate_program(request['program'])
    output.mkdir(parents=True, exist_ok=False)
    candidate = output/'deck.pptx'
    receipt = {'schema': 'paper-studio.pptx.build.v1', 'action': action, 'status': 'running',
               'source_sha256': _sha(source) if source else None, 'render': {'status': 'disabled'},
               'visual_acceptance': 'pending_actual_image_review', 'edits': []}
    try:
        if action == 'create':
            create_deck(candidate, request['deck'])
        elif action == 'export':
            with candidate.open('xb') as stream:
                stream.write(source.read_bytes())
        else:
            current = source
            programs = [request['program']] if 'program' in request else []
            commands = request.get('commands', [])
            if not isinstance(commands, list) or len(commands) > 100:
                raise ValueError('commands must contain at most 100 objects')
            # Apply semantic commands one at a time against the current package.
            # This preserves earlier edits when commands touch the same spPr.
            for index in range(len(programs)+len(commands)):
                program = programs[index] if index < len(programs) else semantic_program([commands[index-len(programs)]], current)
                dest = output/f'edit_{index:03d}.pptx'
                receipt['edits'].append(apply_program(current, dest, program))
                if current != source:
                    current.unlink()  # Owned transient stage; hashes remain in the operation receipt.
                current = dest
            if current == source:
                receipt['edits'].append(apply_program(source, candidate, {'operations': []}))
            else:
                current.rename(candidate)
            receipt['intermediate_packages'] = 'transient; per-stage input/output hashes and native operations retained in edits'
        read_package(candidate)
        inspection = inspect_pptx(candidate, config=config)
        _json(output/('PRE_RENDER_INSPECT.json' if config.get('windows_render', False) else 'INSPECT.json'), inspection)
        if config.get('windows_render', False):
            helper = Path(__file__).with_name('windows.py')
            command = [config.get('toolchain_wrapper', 'codex-toolchain'), 'run', 'python', '--host', 'windows', '--',
                       _windows_path(helper), '--input', _windows_path(candidate), '--root', _windows_path(output)]
            completed = subprocess.run(command, capture_output=True, text=True,
                                       timeout=config.get('pptx_render_timeout', 180), check=False)
            _json(output/'WINDOWS_PROCESS.json', {'returncode': completed.returncode,
                                                 'stdout': completed.stdout[-16000:], 'stderr': completed.stderr[-16000:]})
            render_receipt = output/'PPTX_NATIVE_RECEIPT.json'
            if render_receipt.is_file():
                receipt['render'] = json.loads(render_receipt.read_text(encoding='utf-8'))
            if completed.returncode != 0 or receipt['render'].get('status') != 'completed':
                raise RuntimeError('Windows native save/export failed; see private job receipts')
            read_package(candidate)
            inspection = inspect_pptx(candidate, config=config)
            _json(output/'INSPECT.json', inspection)
        if source and _sha(source) != receipt['source_sha256']:
            raise RuntimeError('Snapshot PPTX changed during build')
        receipt['source_unchanged'] = True
        paths = [candidate, *sorted(output.glob('deck.pdf')), *sorted(output.glob('slide_*.png'))]
        receipt['artifacts'] = [{'path': str(path), 'relative_path': path.name, 'bytes': path.stat().st_size,
                                 'sha256': _sha(path), 'kind': path.suffix[1:]} for path in paths]
        receipt['status'] = 'completed'
        receipt['inspection'] = inspection
    except Exception as exc:
        receipt['status'] = 'failed'
        receipt['error'] = {'type': type(exc).__name__, 'message': str(exc)}
        _json(output/'PPTX_RECEIPT.json', receipt)
        raise
    _json(output/'PPTX_RECEIPT.json', receipt)
    return receipt
