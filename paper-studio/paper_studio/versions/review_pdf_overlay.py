"""Read-only review copies of frozen PDFs; never mutate manuscript or version state.

Coordinates are unrotated PDF page points. Automatic anchors deliberately require
one unique line match; uncertain / multiline locations remain unmarked and receive
an explicit revision sheet. The separate source diff remains authoritative for TeX.
"""
from __future__ import annotations

import hashlib
import math
import re

MAX_PDF_BYTES = 64 * 1024**2
MAX_PAGES = 10000
MAX_CHANGES = 1024
MAX_TOTAL_TEXT = 256 * 1024
MAX_TEXT = 16000
MAX_AUTO_SEARCH_PAGES = 200


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _engine(engine):
    if engine is not None:
        return engine
    from paper_studio.pdf.pymupdf_review import engine as load
    return load()


def _open(pm, data, expected, code):
    if not isinstance(data, bytes) or not data or len(data) > MAX_PDF_BYTES:
        raise ValueError(code + '_BYTES')
    if not isinstance(expected, str) or _sha(data) != expected:
        raise ValueError(code + '_SHA256')
    try:
        doc = pm.open(stream=data, filetype='pdf')
    except Exception as error:
        raise ValueError(code + '_INVALID') from error
    if doc.is_encrypted or not 0 < len(doc) <= MAX_PAGES:
        doc.close()
        raise ValueError(code + '_UNSUPPORTED')
    return doc


def _prose(value):
    if value is None:
        return ''
    if not isinstance(value, str) or len(value) > MAX_TEXT or '\x00' in value:
        raise ValueError('REVIEW_TEXT_INVALID')
    return value


def _tex(value):
    return bool(re.search(r'\\[A-Za-z@]+|\\[{}%&#_$]|\$\$|\\\[', value))


def _locate(pm, doc, change):
    anchor = change.get('anchor') or {}
    if not isinstance(anchor, dict) or set(anchor) - {'page', 'rect', 'quote'}:
        raise ValueError('REVIEW_ANCHOR_INVALID')
    page = anchor.get('page')
    if page is not None and (type(page) is not int or not 1 <= page <= len(doc)):
        raise ValueError('REVIEW_ANCHOR_PAGE')
    rect = anchor.get('rect')
    if rect is not None:
        if page is None or not isinstance(rect, (list, tuple)) or len(rect) != 4:
            raise ValueError('REVIEW_ANCHOR_RECT')
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in rect):
            raise ValueError('REVIEW_ANCHOR_RECT')
        rect = pm.Rect(rect)
        bounds = doc[page-1].rect
        if rect.is_empty or rect.is_infinite or not bounds.contains(rect):
            raise ValueError('REVIEW_ANCHOR_RECT')
    # Nonstandard page transforms require a specialized geometry resolver.
    candidates = [page] if page else range(1, len(doc)+1)
    for number in candidates:
        candidate = doc[number-1]
        unit_type, unit_value = doc.xref_get_key(candidate.xref, 'UserUnit')
        if candidate.rotation or (unit_type != 'null' and float(unit_value) != 1):
            return page, [], 'unsupported_page_geometry'
    quote = _prose(anchor.get('quote') or change.get('old_text') or '')
    if change['kind'] == 'figure':
        return (page, [rect], 'explicit_region') if rect is not None else (page, [], 'figure_region_required')
    if any(_tex(value) for value in (quote, change.get('old_text', ''), change.get('new_text', ''))):
        return page, [], 'source_tex_requires_typesetting'
    if rect is not None and not quote:
        return page, [rect], 'explicit_region'
    if not quote:
        return page, [], 'quote_required'
    if page is None and len(doc) > MAX_AUTO_SEARCH_PAGES:
        return None, [], 'page_required_for_long_pdf'
    hits = []
    for number in candidates:
        p = doc[number-1]
        if p.rotation:
            continue
        for hit in p.search_for(quote, quads=True):
            if rect is None or rect.contains(hit.rect):
                hits.append((number, hit))
                if len(hits) > 1:
                    return page, [], 'anchor_not_unique'
    if len(hits) == 1:
        return hits[0][0], [hits[0][1]], 'unique_quote'
    return page, [], 'quote_not_found'


def _lines(font, text, width, size):
    # Keep ordinary Latin words intact; CJK can wrap between characters.
    for paragraph in text.split('\n'):
        line = ''
        tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9'’.,:;()/+−—=\-]*[ \t]*|.", paragraph)
        for token in tokens:
            if font.text_length(token, fontsize=size) > width:
                pieces = list(token)
            else:
                pieces = [token]
            for piece in pieces:
                if line and font.text_length(line + piece, fontsize=size) > width:
                    yield line.rstrip()
                    line = ''
                line += piece if line else piece.lstrip()
        yield line.rstrip()


def _sheet(pm, change, record, language):
    doc = pm.open()
    font = pm.Font('cjk')
    page = None
    y = 0
    chinese = language == 'zh'

    def new_page():
        nonlocal page, y
        page = doc.new_page(width=595, height=842)
        page.insert_font(fontname='reviewcjk', fontbuffer=font.buffer)
        title = '修订审阅副本（不更新版本）' if chinese else 'Revision review copy (no version update)'
        page.insert_text((40, 38), title, fontname='reviewcjk', fontsize=13)
        y = 62

    def text(value, mode=None):
        nonlocal y
        for line in _lines(font, value, 515, 10):
            if page is None or y > 790:
                new_page()
            width = font.text_length(line, fontsize=10)
            if mode == 'new' and width:
                page.draw_rect(pm.Rect(39, y-10, 41+width, y+3), color=None, fill=(1, .96, .55), overlay=True)
            page.insert_text((40, y), line, fontname='reviewcjk', fontsize=10,
                             color=(.65, .08, .08) if mode == 'old' else (0, 0, 0))
            if mode == 'old' and width:
                page.draw_line((40, y-3), (40+width, y-3), color=(.8, .1, .1), width=.7)
            y += 15
        y += 8

    new_page()
    original = str(record['baseline_page']) if record['baseline_page'] else ('未定位' if chinese else 'unlocated')
    text(('改动 %s；原稿页 %s' if chinese else 'Change %s; original page %s') % (record['change'], original))
    if not record['marked']:
        text(('原稿未标记：位置尚未可靠核定。原因：' if chinese else
              'Original PDF was not marked: location could not be safely established. Reason: ') + record['anchor_status'])
    else:
        text('原稿页红色标记对应下列改动。' if chinese else 'Red marks on the original page identify the change below.')
    if change['kind'] == 'text':
        if record['anchor_status'] == 'source_tex_requires_typesetting':
            text('此改动包含 TeX 源码。请查看独立源差异；本页不把源码当作论文正文。' if chinese else
                 'This change contains TeX source. Consult the separate source diff; source code is not shown as manuscript prose.')
        else:
            text('删除文字：' if chinese else 'Deleted text:')
            text(change.get('old_text', ''), 'old')
            text('新增文字：' if chinese else 'Added text:')
            text(change.get('new_text', ''), 'new')
    else:
        text('替换图（保留原矢量内容）：' if chinese else 'Replacement figure (original vector content preserved):')
        replacement = _open(pm, change.get('replacement_pdf'), change.get('replacement_sha256'), 'REVIEW_FIGURE')
        try:
            if len(replacement) != 1:
                raise ValueError('REVIEW_FIGURE_ONE_PAGE_REQUIRED')
            if y > 350:
                new_page()
            page.show_pdf_page(pm.Rect(40, y, 555, min(785, y+420)), replacement, 0)
        finally:
            replacement.close()
    doc.subset_fonts()
    return doc


def render_baseline_pdf(baseline_pdf, changes, *, expected_sha256, language='en', engine=None):
    """Return (review_pdf_bytes, serializable_evidence), using frozen bytes only.

    Each change has kind text|figure, optional old_text/new_text and anchor
    {page, rect, quote}. Figure bytes and their expected SHA are mandatory.
    Rectangles are explicitly supplied unrotated PDF page points. No source
    files, catalog, history, network, or version pointers are read or written.
    """
    if language not in ('en', 'zh'):
        raise ValueError('REVIEW_LANGUAGE')
    if not isinstance(changes, list) or not 1 <= len(changes) <= MAX_CHANGES:
        raise ValueError('REVIEW_CHANGES')
    # Bound the entire render, including binary blobs absent from JSON budgets.
    text_bytes = 0
    attachment_bytes = len(baseline_pdf) if isinstance(baseline_pdf, bytes) else 0
    for change in changes:
        if not isinstance(change, dict):
            raise ValueError('REVIEW_CHANGE_KIND')
        for key in ('old_text', 'new_text'):
            text_bytes += len(_prose(change.get(key)).encode('utf-8'))
        blob = change.get('replacement_pdf')
        if isinstance(blob, bytes):
            attachment_bytes += len(blob)
    if text_bytes > MAX_TOTAL_TEXT or attachment_bytes > MAX_PDF_BYTES:
        raise ValueError('REVIEW_TOTAL_INPUT_LIMIT')
    pm = _engine(engine)
    doc = _open(pm, baseline_pdf, expected_sha256, 'REVIEW_BASELINE')
    sheets = []
    records = []
    try:
        original_count = len(doc)
        for index, raw in enumerate(changes, 1):
            if not isinstance(raw, dict) or raw.get('kind') not in ('text', 'figure'):
                raise ValueError('REVIEW_CHANGE_KIND')
            change = dict(raw)
            for key in ('old_text', 'new_text'):
                change[key] = _prose(change.get(key))
            number, marks, status = _locate(pm, doc, change)
            record = {'change': index, 'kind': change['kind'], 'baseline_page': number,
                      'anchor_status': status, 'marked': bool(marks), 'rectangles': []}
            if marks:
                page = doc[number-1]
                for mark in marks:
                    rect = mark.rect if hasattr(mark, 'rect') else mark
                    record['rectangles'].append(list(rect))
                    if change['kind'] == 'text':
                        annotation = page.add_strikeout_annot(mark)
                        annotation.set_colors(stroke=(.85, .1, .1))
                        annotation.set_info(content='Review deletion; replacement is on the following revision sheet.')
                        annotation.update()
                    else:
                        page.draw_rect(rect, color=(.85, .1, .1), width=1)
                        page.draw_line(rect.tl, rect.br, color=(.85, .1, .1), width=1.5)
                        page.draw_line(rect.tr, rect.bl, color=(.85, .1, .1), width=1.5)
            sheet = _sheet(pm, change, record, language)
            sheets.append((number or original_count, sheet, record))
            if change['kind'] == 'figure':
                record['replacement_sha256'] = _sha(change['replacement_pdf'])
            records.append(record)
        # Insert backwards so coordinates/page numbers always refer to baseline.
        for number, sheet, record in sorted(sheets, key=lambda row: (row[0], row[2]['change']), reverse=True):
            doc.insert_pdf(sheet, start_at=number)
        shift = 0
        mapping = []
        for number in range(1, original_count+1):
            mapping.append({'baseline_page': number, 'review_page': number+shift})
            cursor = number+shift+1
            for _, sheet, record in sorted((x for x in sheets if x[0] == number), key=lambda row: row[2]['change']):
                record['revision_pages'] = list(range(cursor, cursor+len(sheet)))
                cursor += len(sheet)
                shift += len(sheet)
        output = doc.tobytes(garbage=3, deflate=True)
        if len(output) > 128 * 1024**2:
            raise ValueError('REVIEW_OUTPUT_LIMIT')
        evidence = {'schema': 'paper-studio.baseline-review.v1', 'language': language,
                    'baseline_sha256': expected_sha256, 'review_sha256': _sha(output),
                    'baseline_pages': original_count, 'review_pages': len(doc),
                    'baseline_page_mapping': mapping, 'changes': records,
                    'source_unchanged': True, 'version_updated': False,
                    'mode': 'baseline_pdf_with_revision_sheets',
                    'coordinate_system': 'unrotated_pdf_page_points'}
        return output, evidence
    finally:
        for _, sheet, _ in sheets:
            sheet.close()
        doc.close()
