"""Read-only, bounded layout screening. Findings never edit or fit objects.

Axis-aligned bounding boxes and approximate glyph widths are evidence for
review, not a renderer. Containment/explicit overlap markers are distinguished
from partial overlap; groups and connectors are not treated as colliding boxes.
"""
from __future__ import annotations

import math
import unicodedata
import xml.etree.ElementTree as ET

from .inventory import NS, EMU_PER_MM

MAX_OBJECTS = 2000
MAX_PAIR_CHECKS = 20000
MAX_FINDINGS = 100
MAX_TEXT_FRAMES = 250
MAX_TEXT_CHARACTERS = 100000
ALLOW_OVERLAP = 'paper-studio:allow-overlap'
PT_MM = 25.4/72


def _ref(slide, obj):
    return {'slide': slide, 'shape_id': obj['id'], 'key': obj['key'], 'name': obj['name'][:120]}


def _native(tree, key):
    current = tree
    for index in key.split('/'):
        current = current[int(index)]
    return current


def _identity(element):
    return next((n for n in element.iter() if n.tag == '{'+NS['p']+'}cNvPr'), None)


def _width(character, size):
    if unicodedata.combining(character):
        return 0
    if character == '\t':
        return 2*size
    if character.isspace():
        return .28*size
    if unicodedata.east_asian_width(character) in ('W', 'F'):
        return size
    if unicodedata.category(character).startswith('P'):
        return .30*size
    return (.62 if character.isupper() else .52)*size


def _estimate_text(element, obj, remaining):
    body = element.find('p:txBody', NS)
    if body is None or not obj.get('text'):
        return None, 0
    characters = sum(len(n.text or '') for n in body.findall('.//a:t', NS))
    if characters > min(20000, remaining):
        return {'status': 'not_estimated', 'reason': 'text_character_budget'}, 0
    props = body.find('a:bodyPr', NS)
    attrs = props.attrib if props is not None else {}
    if attrs.get('vert', 'horz') != 'horz':
        return {'status': 'not_estimated', 'reason': 'vertical_text_requires_render'}, characters
    xfrm = element.find('p:spPr/a:xfrm/a:ext', NS)
    if xfrm is None:
        return {'status': 'not_estimated', 'reason': 'inherited_or_missing_text_extents'}, characters
    try:
        width, height = [float(xfrm.get(k, 0))/EMU_PER_MM for k in ('cx', 'cy')]
        available_w = width-(float(attrs.get('lIns', 91440))+float(attrs.get('rIns', 91440)))/EMU_PER_MM
        available_h = height-(float(attrs.get('tIns', 45720))+float(attrs.get('bIns', 45720)))/EMU_PER_MM
    except ValueError:
        return {'status': 'not_estimated', 'reason': 'invalid_text_dimensions'}, characters
    if not all(math.isfinite(v) for v in (available_w, available_h)):
        return {'status': 'not_estimated', 'reason': 'nonfinite_text_dimensions'}, characters
    inherited_font = False
    estimated_h = max_line_w = 0.
    lines = 0
    wraps = attrs.get('wrap', 'square') != 'none'
    for para in body.findall('a:p', NS):
        default = para.find('a:pPr/a:defRPr', NS)
        fallback = para.find('a:endParaRPr', NS)
        default_size = (default.get('sz') if default is not None else None) or (fallback.get('sz') if fallback is not None else None)
        current_w, line_h = 0., 0.
        paragraph_h = 0.
        def finish_line():
            nonlocal current_w, line_h, paragraph_h, lines, max_line_w
            paragraph_h += line_h or 18*PT_MM*1.2
            max_line_w = max(max_line_w, current_w)
            current_w = line_h = 0.
            lines += 1
        for run in para:
            local = run.tag.rsplit('}', 1)[-1]
            if local == 'br':
                finish_line()
                continue
            if local not in ('r', 'fld'):
                continue
            rpr = run.find('a:rPr', NS)
            value = (rpr.get('sz') if rpr is not None else None) or default_size
            inherited_font = inherited_font or value is None
            try:
                size = float(value or 1800)/100*PT_MM
            except ValueError:
                return {'status': 'not_estimated', 'reason': 'invalid_font_size'}, characters
            if not math.isfinite(size) or not 0 < size <= 400*PT_MM:
                return {'status': 'not_estimated', 'reason': 'unsupported_font_size'}, characters
            for text in run.findall('a:t', NS):
                for character in text.text or '':
                    advance = _width(character, size)
                    if wraps and current_w > 0 and current_w+advance > max(available_w, .001):
                        finish_line()
                    current_w += advance
                    line_h = max(line_h, size*1.2)
        finish_line()
        estimated_h += paragraph_h
    autofit = any(body.find('a:bodyPr/a:'+tag, NS) is not None for tag in ('normAutofit', 'spAutoFit'))
    possible = available_w <= 0 or available_h <= 0 or estimated_h > available_h+.5 or (not wraps and max_line_w > available_w+.5)
    return {'status': 'estimated', 'model': 'approximate glyph widths and 1.2em line height',
            'estimated_lines': lines, 'estimated_height_mm': round(estimated_h, 3),
            'estimated_max_line_width_mm': round(max_line_w, 3),
            'available_size_mm': [round(available_w, 3), round(available_h, 3)],
            'possible_overflow': possible, 'inherited_font_assumed_18pt': inherited_font,
            'native_autofit_present': autofit,
            'confidence': 'low; fonts, kerning, paragraph spacing and renderer may change the result'}, characters


def diagnose_layout(inventory, parts):
    findings = []
    checks = objects = text_frames = characters = 0
    truncated = False
    exclusions = {'containers': 0, 'connectors': 0, 'hidden': 0, 'missing_bounds': 0}
    counts = {'outside_slide': 0, 'potential_overlap': 0, 'contained_overlap': 0,
              'marked_intentional_overlap': 0, 'background_overlap': 0, 'text_overflow_estimate': 0}
    text_summary = {'frames_considered': 0, 'frames_not_estimated': 0, 'not_estimated_reasons': {}}
    size = inventory['slide_size_emu']
    canvas_w, canvas_h = [int(size[k])/EMU_PER_MM for k in ('cx', 'cy')]
    def add(kind, severity, detail):
        nonlocal truncated
        counts[kind] += 1
        if len(findings) < MAX_FINDINGS:
            findings.append({'kind': kind, 'severity': severity, **detail})
        else:
            truncated = True
    for slide in inventory['slides']:
        tree = ET.fromstring(parts[slide['part']]).find('p:cSld/p:spTree', NS)
        eligible = []
        hidden_keys = []
        for obj in slide['objects']:
            if objects >= MAX_OBJECTS:
                truncated = True
                break
            objects += 1
            element = _native(tree, obj['key'])
            ident = _identity(element)
            if ((ident is not None and ident.get('hidden') in ('1', 'true'))
                    or any(obj['key'].startswith(key+'/') for key in hidden_keys)):
                hidden_keys.append(obj['key'])
                exclusions['hidden'] += 1
                continue
            if obj['type'] == 'grpSp':
                exclusions['containers'] += 1
                continue
            if obj['type'] == 'cxnSp':
                exclusions['connectors'] += 1
                continue
            box = obj.get('bbox_mm')
            if not box or not all(math.isfinite(v) for v in box):
                exclusions['missing_bounds'] += 1
                continue
            ref = _ref(slide['slide'], obj)
            x, y, width, height = box
            if x < -.01 or y < -.01 or x+width > canvas_w+.01 or y+height > canvas_h+.01:
                add('outside_slide', 'warning', {'object': ref, 'bbox_mm': box})
            marked = ident is not None and ALLOW_OVERLAP in ident.get('descr', '').splitlines()
            background = x <= .01 and y <= .01 and x+width >= canvas_w-.01 and y+height >= canvas_h-.01
            eligible.append((box, ref, marked, background))
            if text_summary['frames_considered'] < MAX_TEXT_FRAMES and characters < MAX_TEXT_CHARACTERS:
                estimate, used = _estimate_text(element, obj, MAX_TEXT_CHARACTERS-characters)
                characters += used
                if estimate is not None:
                    text_summary['frames_considered'] += 1
                    if estimate['status'] == 'estimated':
                        text_frames += 1
                    else:
                        text_summary['frames_not_estimated'] += 1
                        reason = estimate['reason']
                        reasons = text_summary['not_estimated_reasons']
                        reasons[reason] = reasons.get(reason, 0)+1
                        if reason == 'text_character_budget':
                            truncated = True
                    if estimate.get('possible_overflow'):
                        add('text_overflow_estimate', 'warning', {'object': ref, 'estimate': estimate})
            elif obj.get('text'):
                truncated = True
        eligible.sort(key=lambda item: item[0][0])
        for i, left in enumerate(eligible):
            if checks >= MAX_PAIR_CHECKS:
                truncated = True
                break
            a, aref, amarked, abackground = left
            for b, bref, bmarked, bbackground in eligible[i+1:]:
                if b[0] >= a[0]+a[2]-.01:
                    break
                if checks >= MAX_PAIR_CHECKS:
                    truncated = True
                    break
                checks += 1
                iw = min(a[0]+a[2], b[0]+b[2])-max(a[0], b[0])
                ih = min(a[1]+a[3], b[1]+b[3])-max(a[1], b[1])
                if iw <= .01 or ih <= .01:
                    continue
                area = iw*ih
                contained = abs(area-min(a[2]*a[3], b[2]*b[3])) <= .01
                if amarked or bmarked:
                    kind, severity = 'marked_intentional_overlap', 'info'
                elif abackground or bbackground:
                    kind, severity = 'background_overlap', 'info'
                elif contained:
                    kind, severity = 'contained_overlap', 'info'
                else:
                    kind, severity = 'potential_overlap', 'warning'
                add(kind, severity, {'objects': [aref, bref], 'intersection_area_mm2': round(area, 3),
                                     'interpretation': 'Only explicit markers establish declared intent; other containment/background relations may be intentional.'})
    return {'schema': 'paper-studio.pptx.layout-screen.v1', 'status': 'screened', 'read_only': True,
            'findings': findings, 'counts': counts, 'excluded': exclusions,
            'objects_screened': objects, 'pair_checks': checks, 'text_frames_estimated': text_frames,
            'text_screening': text_summary,
            'text_characters_screened': characters, 'truncated': truncated,
            'limits': {'objects': MAX_OBJECTS, 'pair_checks': MAX_PAIR_CHECKS, 'findings': MAX_FINDINGS,
                       'text_frames': MAX_TEXT_FRAMES, 'text_characters': MAX_TEXT_CHARACTERS},
            'text_overflow_status': 'estimated_only; native rendering and actual image review remain required',
            'limitations': ['Axis-aligned overlap can over-report rotated/freeform shapes.',
                            'Containers and connectors are excluded; containment is informational, not proof of intended layout.',
                            'Table cells, chart labels, inherited text geometry and precise font layout are not evaluated.',
                            'No positions, text, font sizes, groups, z-order or package parts are changed.']}
