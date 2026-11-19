"""Owned-deck PowerPoint host. Run only through the managed Windows Python.

No service setup, global application Quit, arbitrary COM commands or access to
existing presentation contents. Active, unlocked input desktop is mandatory.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import math
from pathlib import Path
import sys

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from paper_studio.pptx.document import read_package
    from paper_studio.pptx.inventory import inspect_deck
else:
    from .document import read_package
    from .inventory import inspect_deck


def desktop_preflight():
    if sys.platform != 'win32':
        raise RuntimeError('Native PowerPoint rendering requires Windows')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    wts = ctypes.WinDLL('wtsapi32', use_last_error=True)
    user = ctypes.WinDLL('user32', use_last_error=True)
    kernel.GetCurrentProcessId.restype = wintypes.DWORD
    kernel.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    session = wintypes.DWORD()
    if not kernel.ProcessIdToSessionId(kernel.GetCurrentProcessId(), ctypes.byref(session)) or session.value == 0:
        raise RuntimeError('Office host must run in an interactive user session, not session zero')
    state_ptr = ctypes.c_void_p()
    size = wintypes.DWORD()
    wts.WTSQuerySessionInformationW.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_int,
                                               ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)]
    wts.WTSFreeMemory.argtypes = [ctypes.c_void_p]
    if not wts.WTSQuerySessionInformationW(None, session.value, 8, ctypes.byref(state_ptr), ctypes.byref(size)):
        raise RuntimeError('Cannot verify the Windows user session state')
    try:
        state = ctypes.cast(state_ptr, ctypes.POINTER(ctypes.c_int)).contents.value
    finally:
        wts.WTSFreeMemory(state_ptr)
    if state != 0:  # WTSActive
        raise RuntimeError('Windows user session is not active')
    user.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    user.OpenInputDesktop.restype = wintypes.HANDLE
    user.CloseDesktop.argtypes = [wintypes.HANDLE]
    user.GetUserObjectInformationW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                              wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    desktop = user.OpenInputDesktop(0, False, 1)  # DESKTOP_READOBJECTS
    if not desktop:
        raise RuntimeError('User input desktop is locked or inaccessible')
    try:
        name = ctypes.create_unicode_buffer(256)
        needed = wintypes.DWORD()
        if not user.GetUserObjectInformationW(desktop, 2, name, ctypes.sizeof(name), ctypes.byref(needed)):
            raise RuntimeError('Cannot verify input desktop identity')
        if name.value.casefold() != 'default':
            raise RuntimeError('Office host requires the unlocked Default user desktop')
    finally:
        user.CloseDesktop(desktop)
    return {'platform': sys.platform, 'process_session_id': session.value,
            'session_state': 'active', 'input_desktop': name.value,
            'interactive_desktop_verified': True}


def write_json(path, value):
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')


def native_diff(before, after):
    return [{'part': name, 'kind': 'added' if name not in before else 'removed' if name not in after else 'changed',
             'before_sha256': hashlib.sha256(before[name]).hexdigest() if name in before else None,
             'after_sha256': hashlib.sha256(after[name]).hexdigest() if name in after else None}
            for name in sorted(set(before) | set(after)) if before.get(name) != after.get(name)]


def shape_diff(before, after):
    left = {(s['slide'], o['id']): o for s in before['slides'] for o in s['objects']}
    right = {(s['slide'], o['id']): o for s in after['slides'] for o in s['objects']}
    return [{'slide': key[0], 'shape_id': key[1], 'before': left.get(key), 'after': right.get(key)}
            for key in sorted(set(left) | set(right)) if left.get(key) != right.get(key)]


def overflow_checks(deck):
    checks = []
    def walk(shapes, slide):
        for shape in shapes:
            if int(shape.Type) == 6:  # msoGroup, owned deck only
                walk(shape.GroupItems, slide)
            if bool(shape.HasTextFrame) and bool(shape.TextFrame.HasText):
                frame = shape.TextFrame2
                available_w = float(shape.Width)-float(frame.MarginLeft)-float(frame.MarginRight)
                available_h = float(shape.Height)-float(frame.MarginTop)-float(frame.MarginBottom)
                bound_w, bound_h = float(frame.TextRange.BoundWidth), float(frame.TextRange.BoundHeight)
                checks.append({'slide': slide, 'shape_id': str(shape.Id), 'name': str(shape.Name),
                               'available_width_pt': available_w, 'available_height_pt': available_h,
                               'text_width_pt': bound_w, 'text_height_pt': bound_h,
                               'possible_overflow': bound_w > available_w+1 or bound_h > available_h+1})
    for index in range(1, int(deck.Slides.Count)+1):
        walk(deck.Slides.Item(index).Shapes, index)
    return {'status': 'native_metrics_checked', 'checks': checks,
            'limitations': 'Shape text frames checked, including groups; table cell/chart label overflow requires image review.'}


def run(input_path, root):
    root, input_path = Path(root), Path(input_path)
    if (not root.is_absolute() or not input_path.is_absolute() or input_path.parent.resolve() != root.resolve()
            or input_path.name != 'deck.pptx' or any(p.is_symlink() for p in [root, input_path, *root.parents])):
        raise ValueError('Office may open only deck.pptx inside the explicit private job root')
    if not root.is_dir() or any((root/name).exists() for name in ('deck.pdf', 'PPTX_NATIVE_RECEIPT.json')) or list(root.glob('slide_*.png')):
        raise ValueError('Office export paths must be fresh')
    receipt = {'status': 'running', 'backend': 'Windows PowerPoint native save/PDF/PNG',
               'application_quit_called': False, 'existing_deck_contents_read': False,
               'owned_deck_closed': False, 'cleanup_errors': [], 'visual_acceptance': 'pending_actual_image_review'}
    app = deck = None
    initialized = False
    before_count = after_count = None
    failure = None
    try:
        receipt['desktop_preflight'] = desktop_preflight()
        original, original_parts = read_package(input_path)
        receipt['intent_sha256'] = hashlib.sha256(original).hexdigest()
        before = inspect_deck(input_path)
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        initialized = True
        app = win32com.client.DispatchEx('PowerPoint.Application')
        before_count = int(app.Presentations.Count)
        deck = app.Presentations.Open(str(input_path), 0, 0, 0)
        slide_count = int(deck.Slides.Count)
        if not 1 <= slide_count <= 20:
            raise ValueError('Slide budget exceeded')
        deck.Saved = 0
        deck.Save()
        _, saved_parts = read_package(input_path)
        receipt['native_part_changes'] = native_diff(original_parts, saved_parts)
        receipt['native_object_changes'] = shape_diff(before, inspect_deck(input_path))
        receipt['text_overflow'] = overflow_checks(deck)
        width, height = float(deck.PageSetup.SlideWidth), float(deck.PageSetup.SlideHeight)
        if not all(math.isfinite(v) and 0 < v <= 6000 for v in (width, height)) or not .1 <= height/width <= 10:
            raise ValueError('Invalid or excessive canvas')
        preview_height = max(1, round(1600*height/width))
        if preview_height*1600*slide_count > 80_000_000:
            raise ValueError('Preview budget exceeds 80 million pixels')
        deck.SaveAs(str(root/'deck.pdf'), 32)
        for index in range(1, slide_count+1):
            deck.Slides.Item(index).Export(str(root/f'slide_{index:02d}.png'), 'PNG', 1600, preview_height)
        if not (root/'deck.pdf').read_bytes().startswith(b'%PDF-'):
            raise RuntimeError('PowerPoint did not produce a PDF')
        from pypdf import PdfReader
        from PIL import Image
        if len(PdfReader(root/'deck.pdf').pages) != slide_count:
            raise RuntimeError('PDF page count does not match the deck')
        for preview in root.glob('slide_*.png'):
            with Image.open(preview) as image:
                image.verify()
        receipt.update(slide_count=slide_count, page_width_pt=width, page_height_pt=height,
                       saved_sha256=hashlib.sha256(input_path.read_bytes()).hexdigest())
    except Exception as exc:
        failure = exc
    finally:
        if deck is not None:
            try:
                deck.Saved = -1
                deck.Close()
                receipt['owned_deck_closed'] = True
            except Exception as exc:
                receipt['cleanup_errors'].append({'operation': 'close_owned_deck', 'error': str(exc)})
            deck = None
        if app is not None:
            try:
                after_count = int(app.Presentations.Count)
            except Exception as exc:
                receipt['cleanup_errors'].append({'operation': 'count_presentations', 'error': str(exc)})
            app = None
        if initialized:
            pythoncom.CoUninitialize()
    receipt.update(presentations_before_open=before_count, presentations_after_close=after_count,
                   presentation_count_restored=before_count is not None and before_count == after_count)
    if failure is None and (not receipt['owned_deck_closed'] or not receipt['presentation_count_restored'] or receipt['cleanup_errors']):
        failure = RuntimeError('Could not verify cleanup of the owned presentation')
    if failure is None:
        receipt['status'] = 'completed'
    else:
        receipt.update(status='failed', error={'type': type(failure).__name__, 'message': str(failure)})
    write_json(root/'PPTX_NATIVE_RECEIPT.json', receipt)
    if failure is not None:
        raise RuntimeError('Native rendering failed; private receipt retained') from failure
    return receipt


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--preflight', action='store_true')
    parser.add_argument('--input', type=Path)
    parser.add_argument('--root', type=Path)
    args = parser.parse_args()
    if args.preflight:
        print(json.dumps(desktop_preflight()))
    elif args.input and args.root:
        print(json.dumps(run(args.input, args.root), ensure_ascii=False))
    else:
        parser.error('Use --preflight or --input and --root')
