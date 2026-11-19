"""Small native OOXML author, using no Office automation or rasterised objects.

See SCHEMA.md. Every created shape, table and chart remains an OOXML object.
The lower-level document program provides the complete XML editing interface.
"""
from __future__ import annotations

import io
import json
import math
from pathlib import Path
import re
import xml.etree.ElementTree as ET
import zipfile

from .document import A, P, R, REL, CT, _validate_parts

C = 'http://schemas.openxmlformats.org/drawingml/2006/chart'
S = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
EMU = 36000
for prefix, uri in [('p', P), ('a', A), ('r', R), ('c', C)]:
    ET.register_namespace(prefix, uri)


def node(tag, attrs=None, text=None):
    prefix, local = tag.split(':')
    result = ET.Element('{'+{'p': P, 'a': A, 'r': R, 'c': C, 's': S}[prefix]+'}'+local,
                        {str(k): str(v) for k, v in (attrs or {}).items()})
    result.text = text
    return result


def child(parent, tag, attrs=None, text=None):
    result = node(tag, attrs, text)
    parent.append(result)
    return result


def xml(element):
    return ET.tostring(element, encoding='utf-8', xml_declaration=True)


def number(value, low=-2000, high=2000):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f'Expected finite numeric value in {low}..{high}')
    return value


def mm(value, low=-2000, high=2000):
    return round(number(value, low, high)*EMU)


def color(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-fA-F]{6}', value):
        raise ValueError('Colors must be six hex digits without #')
    return value.upper()


def fields(value, allowed, label):
    if not isinstance(value, dict) or not set(value) <= set(allowed.split()):
        raise ValueError(f'Unknown {label} field or non-object value')


def textbody(text, style=None, tag='p:txBody'):
    if not isinstance(text, str) or len(text) > 100000:
        raise ValueError('Text must be a string of at most 100000 characters')
    style = style or {}
    fields(style, 'font_size font_family bold italic color align vertical', 'text style')
    if any(key in style and type(style[key]) is not bool for key in ('bold', 'italic')):
        raise ValueError('bold and italic must be boolean')
    body = node(tag)
    vertical = style.get('vertical', 'top')
    if vertical not in ('top', 'middle', 'bottom'):
        raise ValueError('vertical must be top|middle|bottom')
    child(body, 'a:bodyPr', {'wrap': 'square', 'anchor': {'top': 't', 'middle': 'ctr', 'bottom': 'b'}[vertical]})
    child(body, 'a:lstStyle')
    size = round(number(style.get('font_size', 18), 1, 400)*100)
    for line in text.split('\n'):
        para = child(body, 'a:p')
        alignment = style.get('align', 'left')
        if alignment not in ('left', 'center', 'right'):
            raise ValueError('align must be left|center|right')
        child(para, 'a:pPr', {'algn': {'left': 'l', 'center': 'ctr', 'right': 'r'}[alignment]})
        run = child(para, 'a:r')
        props = child(run, 'a:rPr', {'lang': 'en-US', 'sz': size,
                                    'b': int(bool(style.get('bold', False))),
                                    'i': int(bool(style.get('italic', False)))})
        fill = child(props, 'a:solidFill')
        child(fill, 'a:srgbClr', {'val': color(style.get('color', '172B4D'))})
        family = style.get('font_family', 'Aptos')
        if not isinstance(family, str) or not 1 <= len(family) <= 128:
            raise ValueError('font_family must contain 1..128 characters')
        child(props, 'a:latin', {'typeface': family})
        child(props, 'a:ea', {'typeface': family})
        child(run, 'a:t', text=line)
        child(para, 'a:endParaRPr', {'lang': 'en-US', 'sz': size})
    return body


def transform(obj, tag='a:xfrm', group=False):
    x, y = mm(obj.get('x', 0)), mm(obj.get('y', 0))
    w, h = mm(obj.get('width', 50), 0.001), mm(obj.get('height', 20), 0.001)
    attrs = {}
    if 'rotation' in obj:
        attrs['rot'] = round(number(obj['rotation'], -36000, 36000)*60000)
    item = node(tag, attrs)
    child(item, 'a:off', {'x': x, 'y': y})
    child(item, 'a:ext', {'cx': w, 'cy': h})
    if group:
        child(item, 'a:chOff', {'x': 0, 'y': 0})
        child(item, 'a:chExt', {'cx': w, 'cy': h})
    return item


def paint(properties, style=None, connector=False):
    style = style or {}
    fields(style, 'fill line_color line_width dash arrow', 'shape style')
    if 'arrow' in style and type(style['arrow']) is not bool:
        raise ValueError('arrow must be boolean')
    fill = style.get('fill', None if connector else 'E8EFF8')
    if fill is None:
        child(properties, 'a:noFill')
    else:
        child(child(properties, 'a:solidFill'), 'a:srgbClr', {'val': color(fill)})
    line = child(properties, 'a:ln', {'w': round(number(style.get('line_width', 1), 0, 100)*12700)})
    line_color = style.get('line_color', '42658F')
    if line_color is None:
        child(line, 'a:noFill')
    else:
        child(child(line, 'a:solidFill'), 'a:srgbClr', {'val': color(line_color)})
    if 'dash' in style:
        if style['dash'] not in ('solid', 'dash', 'dot', 'dashDot', 'lgDash'):
            raise ValueError('Unsupported dash style')
        child(line, 'a:prstDash', {'val': style['dash']})
    if style.get('arrow'):
        child(line, 'a:tailEnd', {'type': 'triangle'})


def group_header(tree, ident=1, name=''):
    nonvisual = child(tree, 'p:nvGrpSpPr')
    child(nonvisual, 'p:cNvPr', {'id': ident, 'name': name})
    child(nonvisual, 'p:cNvGrpSpPr')
    child(nonvisual, 'p:nvPr')
    return child(tree, 'p:grpSpPr')


def relationships(items):
    root = ET.Element('{'+REL+'}Relationships')
    for rid, kind, target in items:
        ET.SubElement(root, '{'+REL+'}Relationship', {'Id': rid, 'Type': R+'/'+kind, 'Target': target})
    return xml(root)


def types(parts):
    root = ET.Element('{'+CT+'}Types')
    ET.SubElement(root, '{'+CT+'}Default', {'Extension': 'rels', 'ContentType': 'application/vnd.openxmlformats-package.relationships+xml'})
    ET.SubElement(root, '{'+CT+'}Default', {'Extension': 'xml', 'ContentType': 'application/xml'})
    for part, kind in parts.items():
        ET.SubElement(root, '{'+CT+'}Override', {'PartName': '/'+part, 'ContentType': kind})
    return xml(root)


def package_bytes(parts):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
        for part, data in sorted(parts.items()):
            archive.writestr(part, data)
    return stream.getvalue()


def workbook(categories, series):
    sheet = node('s:worksheet')
    data = child(sheet, 's:sheetData')
    rows = [['Category', *[s['name'] for s in series]],
            *[[value, *[s['values'][i] for s in series]] for i, value in enumerate(categories)]]
    for i, row in enumerate(rows, 1):
        r = child(data, 's:row', {'r': i})
        for j, value in enumerate(row):
            cell = child(r, 's:c', {'r': chr(65+j)+str(i)})
            if isinstance(value, str):
                cell.set('t', 'inlineStr')
                child(child(cell, 's:is'), 's:t', text=value)
            else:
                child(cell, 's:v', text=str(number(value, -1e100, 1e100)))
    book = node('s:workbook')
    sheets = child(book, 's:sheets')
    child(sheets, 's:sheet', {'name': 'Sheet1', 'sheetId': 1, '{'+R+'}id': 'rId1'})
    parts = {'xl/workbook.xml': xml(book), 'xl/worksheets/sheet1.xml': xml(sheet),
             '_rels/.rels': relationships([('rId1', 'officeDocument', 'xl/workbook.xml')]),
             'xl/_rels/workbook.xml.rels': relationships([('rId1', 'worksheet', 'worksheets/sheet1.xml')])}
    parts['[Content_Types].xml'] = types({'xl/workbook.xml': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml',
                                        'xl/worksheets/sheet1.xml': 'application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml'})
    return package_bytes(parts)


def chart_xml(spec):
    fields(spec, 'type name x y width height rotation kind categories series', 'chart')
    categories, series = spec.get('categories'), spec.get('series')
    if not isinstance(categories, list) or not 1 <= len(categories) <= 100 or not all(isinstance(x, str) and len(x) <= 1000 for x in categories):
        raise ValueError('Chart requires 1..100 string categories')
    if not isinstance(series, list) or not 1 <= len(series) <= 10:
        raise ValueError('Chart requires 1..10 series')
    for item in series:
        fields(item, 'name values', 'chart series')
        if not isinstance(item.get('name'), str) or not isinstance(item.get('values'), list) or len(item['values']) != len(categories):
            raise ValueError('Each series requires a name and one numeric value per category')
        for value in item['values']:
            number(value, -1e100, 1e100)
    kind = spec.get('kind', 'bar')
    if kind not in ('bar', 'line', 'pie'):
        raise ValueError('Chart kind must be bar|line|pie')
    root = node('c:chartSpace')
    chart = child(root, 'c:chart')
    child(chart, 'c:autoTitleDeleted', {'val': 1})
    plot = child(chart, 'c:plotArea')
    child(plot, 'c:layout')
    plotkind = child(plot, 'c:'+kind+'Chart')
    if kind == 'bar':
        child(plotkind, 'c:barDir', {'val': 'col'})
    if kind != 'pie':
        child(plotkind, 'c:grouping', {'val': 'clustered' if kind == 'bar' else 'standard'})
    child(plotkind, 'c:varyColors', {'val': int(kind == 'pie')})
    for index, item in enumerate(series):
        ser = child(plotkind, 'c:ser')
        child(ser, 'c:idx', {'val': index})
        child(ser, 'c:order', {'val': index})
        child(child(ser, 'c:tx'), 'c:v', text=item['name'])
        cat = child(child(ser, 'c:cat'), 'c:strRef')
        child(cat, 'c:f', text=f'Sheet1!$A$2:$A${len(categories)+1}')
        cache = child(cat, 'c:strCache')
        child(cache, 'c:ptCount', {'val': len(categories)})
        for i, value in enumerate(categories):
            child(child(cache, 'c:pt', {'idx': i}), 'c:v', text=value)
        values = child(child(ser, 'c:val'), 'c:numRef')
        column = chr(66+index)
        child(values, 'c:f', text=f'Sheet1!${column}$2:${column}${len(categories)+1}')
        cache = child(values, 'c:numCache')
        child(cache, 'c:formatCode', text='General')
        child(cache, 'c:ptCount', {'val': len(categories)})
        for i, value in enumerate(item['values']):
            child(child(cache, 'c:pt', {'idx': i}), 'c:v', text=str(value))
    if kind != 'pie':
        for ident in (12345, 67890):
            child(plotkind, 'c:axId', {'val': ident})
        for typ, ident, cross, pos in [('catAx', 12345, 67890, 'b'), ('valAx', 67890, 12345, 'l')]:
            axis = child(plot, 'c:'+typ)
            child(axis, 'c:axId', {'val': ident})
            child(child(axis, 'c:scaling'), 'c:orientation', {'val': 'minMax'})
            child(axis, 'c:delete', {'val': 0})
            child(axis, 'c:axPos', {'val': pos})
            child(axis, 'c:crossAx', {'val': cross})
            child(axis, 'c:crosses', {'val': 'autoZero'})
    if len(series) > 1:
        legend = child(chart, 'c:legend')
        child(legend, 'c:legendPos', {'val': 'b'})
        child(legend, 'c:layout')
        child(legend, 'c:overlay', {'val': 0})
    child(chart, 'c:plotVisOnly', {'val': 1})
    child(child(root, 'c:externalData', {'{'+R+'}id': 'rId1'}), 'c:autoUpdate', {'val': 0})
    return xml(root), workbook(categories, series)


class Author:
    def __init__(self):
        self.parts, self.content_types = {}, {}
        self.chart_count = 0

    def shape(self, spec, ids, slide_rels):
        typ = spec.get('type', 'shape')
        extras = {'shape': 'text text_style style geometry', 'textbox': 'text text_style style geometry',
                  'connector': 'style geometry start end', 'table': 'rows columns header text_style',
                  'chart': 'kind categories series', 'group': 'children'}
        if typ not in extras:
            raise ValueError('Object type must be shape|textbox|connector|table|chart|group')
        fields(spec, 'type name x y width height rotation '+extras[typ], typ)
        name = spec.get('name')
        if name not in ids:
            raise ValueError('Every object requires a unique nonempty name')
        ident = ids[name]
        if typ == 'group':
            group = node('p:grpSp')
            properties = group_header(group, ident, name)
            properties.append(transform(spec, group=True))
            for item in spec.get('children', []):
                group.append(self.shape(item, ids, slide_rels))
            return group
        if typ in ('table', 'chart'):
            root = node('p:graphicFrame')
            nv = child(root, 'p:nvGraphicFramePr')
            child(nv, 'p:cNvPr', {'id': ident, 'name': name})
            child(nv, 'p:cNvGraphicFramePr')
            child(nv, 'p:nvPr')
            root.append(transform(spec, 'p:xfrm'))
            data = child(child(root, 'a:graphic'), 'a:graphicData',
                         {'uri': 'http://schemas.openxmlformats.org/drawingml/2006/table' if typ == 'table' else C})
            if typ == 'chart':
                self.chart_count += 1
                index = self.chart_count
                payload, book = chart_xml(spec)
                part = f'ppt/charts/chart{index}.xml'
                self.parts[part] = payload
                self.content_types[part] = 'application/vnd.openxmlformats-officedocument.drawingml.chart+xml'
                workbook_part = f'ppt/embeddings/workbook{index}.xlsx'
                self.parts[workbook_part] = book
                self.content_types[workbook_part] = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
                self.parts[f'ppt/charts/_rels/chart{index}.xml.rels'] = relationships([('rId1', 'package', f'../embeddings/workbook{index}.xlsx')])
                rid = 'rId'+str(len(slide_rels)+1)
                slide_rels.append((rid, 'chart', f'../charts/chart{index}.xml'))
                child(data, 'c:chart', {'{'+R+'}id': rid})
            else:
                if 'header' in spec and type(spec['header']) is not bool:
                    raise ValueError('header must be boolean')
                rows = spec.get('rows')
                if not isinstance(rows, list) or not 1 <= len(rows) <= 100 or not all(isinstance(r, list) and r for r in rows):
                    raise ValueError('Table rows must be a nonempty matrix of strings')
                cols = len(rows[0])
                if cols > 30 or any(len(r) != cols or any(not isinstance(v, str) for v in r) for r in rows):
                    raise ValueError('Table rows must be a rectangular string matrix (max 30 columns)')
                table = child(data, 'a:tbl')
                props = child(table, 'a:tblPr', {'firstRow': int(bool(spec.get('header', True))), 'bandRow': 1})
                child(props, 'a:tableStyleId', text='{5C22544A-7EE6-4342-B048-85BDC9FD1C3A}')
                grid = child(table, 'a:tblGrid')
                widths = spec.get('columns', [spec.get('width', 50)/cols]*cols)
                if not isinstance(widths, list) or len(widths) != cols:
                    raise ValueError('columns must provide one width in mm per table column')
                if abs(sum(number(x, .001, 2000) for x in widths)-spec.get('width', 50)) > .01:
                    raise ValueError('Column widths must sum to table width')
                for width in widths:
                    child(grid, 'a:gridCol', {'w': mm(width, .001)})
                for row_index, row in enumerate(rows):
                    tr = child(table, 'a:tr', {'h': mm(spec.get('height', 20)/len(rows), .001)})
                    for value in row:
                        cell = child(tr, 'a:tc')
                        cell_style = dict(spec.get('text_style') or {})
                        if row_index == 0 and spec.get('header', True):
                            cell_style.update(color='FFFFFF', bold=True)
                        cell.append(textbody(value, cell_style, 'a:txBody'))
                        cell_props = child(cell, 'a:tcPr')
                        if row_index == 0 and spec.get('header', True):
                            child(child(cell_props, 'a:solidFill'), 'a:srgbClr', {'val': '42658F'})
            return root
        if typ not in ('shape', 'textbox', 'connector'):
            raise ValueError('Object type must be shape|textbox|connector|table|chart|group')
        connector = typ == 'connector'
        root = node('p:cxnSp' if connector else 'p:sp')
        nv = child(root, 'p:nvCxnSpPr' if connector else 'p:nvSpPr')
        child(nv, 'p:cNvPr', {'id': ident, 'name': name})
        cnv = child(nv, 'p:cNvCxnSpPr' if connector else 'p:cNvSpPr', {'txBox': 1} if typ == 'textbox' else {})
        if connector:
            for key, tag in [('start', 'a:stCxn'), ('end', 'a:endCxn')]:
                if key in spec:
                    connection = spec[key]
                    fields(connection, 'shape site', 'connector attachment')
                    if connection.get('shape') not in ids or type(connection.get('site', 0)) is not int or not 0 <= connection.get('site', 0) <= 255:
                        raise ValueError('Connector attachment must name an existing shape and site 0..255')
                    child(cnv, tag, {'id': ids[connection['shape']], 'idx': connection.get('site', 0)})
        child(nv, 'p:nvPr')
        props = child(root, 'p:spPr')
        props.append(transform(spec))
        geometry = spec.get('geometry', 'line' if connector else 'rect')
        if not isinstance(geometry, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9]{0,100}', geometry):
            raise ValueError('geometry must be an OOXML preset name')
        child(child(props, 'a:prstGeom', {'prst': geometry}), 'a:avLst')
        paint(props, spec.get('style', {'fill': None, 'line_color': None} if typ == 'textbox' else None), connector)
        if not connector:
            root.append(textbody(spec.get('text', ''), spec.get('text_style')))
        return root


def theme():
    root = node('a:theme', {'name': 'Paper Studio'})
    elements = child(root, 'a:themeElements')
    colors = child(elements, 'a:clrScheme', {'name': 'Paper Studio'})
    for name, value in [('dk1', '172B4D'), ('lt1', 'FFFFFF'), ('dk2', '344563'), ('lt2', 'F4F5F7'),
                        ('accent1', '42658F'), ('accent2', 'C56B45'), ('accent3', '3C897A'),
                        ('accent4', '8465A7'), ('accent5', 'B69540'), ('accent6', '507DA3'),
                        ('hlink', '0563C1'), ('folHlink', '954F72')]:
        child(child(colors, 'a:'+name), 'a:srgbClr', {'val': value})
    fonts = child(elements, 'a:fontScheme', {'name': 'Paper Studio'})
    for typ in ('majorFont', 'minorFont'):
        font = child(fonts, 'a:'+typ)
        for name in ('latin', 'ea', 'cs'):
            child(font, 'a:'+name, {'typeface': 'Aptos'})
    fmt = child(elements, 'a:fmtScheme', {'name': 'Paper Studio'})
    for tag in ('fillStyleLst', 'lnStyleLst', 'effectStyleLst', 'bgFillStyleLst'):
        lst = child(fmt, 'a:'+tag)
        for i in range(3):
            if tag == 'effectStyleLst':
                child(child(lst, 'a:effectStyle'), 'a:effectLst')
            else:
                parent = child(lst, 'a:ln', {'w': 12700}) if tag == 'lnStyleLst' else lst
                child(child(parent, 'a:solidFill'), 'a:schemeClr', {'val': 'phClr'})
    return xml(root)


def create_deck(path: Path, deck: dict):
    fields(deck, 'width_mm height_mm slides', 'deck')
    encoded = json.dumps(deck, allow_nan=False, ensure_ascii=False).encode()
    if len(encoded) > 8*1024*1024:
        raise ValueError('Deck exceeds 8 MiB instruction budget')
    slides = deck.get('slides')
    if not isinstance(slides, list) or not 1 <= len(slides) <= 20:
        raise ValueError('Deck requires 1..20 slides')
    width, height = mm(deck.get('width_mm', 338.666667), 9), mm(deck.get('height_mm', 190.5), 9)
    author = Author()
    parts, content = author.parts, author.content_types
    presentation = node('p:presentation')
    masters = child(presentation, 'p:sldMasterIdLst')
    child(masters, 'p:sldMasterId', {'id': 2147483648, '{'+R+'}id': 'rId1'})
    ids = child(presentation, 'p:sldIdLst')
    pres_rels = [('rId1', 'slideMaster', 'slideMasters/slideMaster1.xml')]
    for index, spec in enumerate(slides, 1):
        fields(spec, 'objects background layout title subtitle', 'slide')
        if not isinstance(spec.get('objects', []), list):
            raise ValueError('Slide objects must be a list')
        objects = list(spec.get('objects', []))
        layout = spec.get('layout', 'blank')
        if layout not in ('blank', 'title', 'title_content'):
            raise ValueError('layout must be blank|title|title_content')
        if layout != 'blank':
            objects.insert(0, {'type': 'textbox', 'name': '_layout_title', 'x': 16, 'y': 15,
                               'width': width/EMU-32, 'height': 28, 'text': spec.get('title', ''),
                               'text_style': {'font_size': 30, 'bold': True}})
            if 'subtitle' in spec:
                objects.insert(1, {'type': 'textbox', 'name': '_layout_subtitle', 'x': 16, 'y': 50,
                                   'width': width/EMU-32, 'height': height/EMU-65,
                                   'text': spec['subtitle'], 'text_style': {'font_size': 20}})
        names = {}
        def collect(items, depth=0):
            if depth > 12 or not isinstance(items, list):
                raise ValueError('Group nesting exceeds 12 or objects are not a list')
            for item in items:
                if not isinstance(item, dict):
                    raise ValueError('Objects must be dictionaries')
                name = item.get('name')
                if not isinstance(name, str) or not 1 <= len(name) <= 1024 or name in names:
                    raise ValueError('Object names must be unique per slide and nonempty')
                names[name] = len(names)+2
                if len(names) > 500:
                    raise ValueError('Creation is limited to 500 objects per slide')
                if item.get('type') == 'group':
                    collect(item.get('children', []), depth+1)
        collect(objects)
        slide = node('p:sld')
        common = child(slide, 'p:cSld')
        if 'background' in spec:
            bg = child(child(common, 'p:bg'), 'p:bgPr')
            child(child(bg, 'a:solidFill'), 'a:srgbClr', {'val': color(spec['background'])})
        tree = child(common, 'p:spTree')
        props = group_header(tree)
        props.append(transform({'width': width/EMU, 'height': height/EMU}, group=True))
        rels = [('rId1', 'slideLayout', '../slideLayouts/slideLayout1.xml')]
        for item in objects:
            tree.append(author.shape(item, names, rels))
        child(child(slide, 'p:clrMapOvr'), 'a:masterClrMapping')
        part = f'ppt/slides/slide{index}.xml'
        parts[part] = xml(slide)
        parts[f'ppt/slides/_rels/slide{index}.xml.rels'] = relationships(rels)
        content[part] = 'application/vnd.openxmlformats-officedocument.presentationml.slide+xml'
        rid = 'rId'+str(index+1)
        child(ids, 'p:sldId', {'id': 255+index, '{'+R+'}id': rid})
        pres_rels.append((rid, 'slide', f'slides/slide{index}.xml'))
    child(presentation, 'p:sldSz', {'cx': width, 'cy': height})
    child(presentation, 'p:notesSz', {'cx': 6858000, 'cy': 9144000})
    parts['ppt/presentation.xml'] = xml(presentation)
    content['ppt/presentation.xml'] = 'application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml'
    parts['_rels/.rels'] = relationships([('rId1', 'officeDocument', 'ppt/presentation.xml')])
    parts['ppt/_rels/presentation.xml.rels'] = relationships(pres_rels)
    master = node('p:sldMaster')
    group_header(child(child(master, 'p:cSld'), 'p:spTree'))
    child(master, 'p:clrMap', {name: name for name in ('accent1', 'accent2', 'accent3', 'accent4', 'accent5', 'accent6', 'hlink', 'folHlink')} | {'bg1': 'lt1', 'tx1': 'dk1', 'bg2': 'lt2', 'tx2': 'dk2'})
    child(child(master, 'p:sldLayoutIdLst'), 'p:sldLayoutId', {'id': 2147483649, '{'+R+'}id': 'rId1'})
    tx = child(master, 'p:txStyles')
    for name in ('titleStyle', 'bodyStyle', 'otherStyle'):
        child(tx, 'p:'+name)
    layout = node('p:sldLayout', {'type': 'blank', 'preserve': 1})
    group_header(child(child(layout, 'p:cSld'), 'p:spTree'))
    child(child(layout, 'p:clrMapOvr'), 'a:masterClrMapping')
    parts.update({'ppt/slideMasters/slideMaster1.xml': xml(master),
                  'ppt/slideLayouts/slideLayout1.xml': xml(layout), 'ppt/theme/theme1.xml': theme(),
                  'ppt/slideMasters/_rels/slideMaster1.xml.rels': relationships([('rId1', 'slideLayout', '../slideLayouts/slideLayout1.xml'), ('rId2', 'theme', '../theme/theme1.xml')]),
                  'ppt/slideLayouts/_rels/slideLayout1.xml.rels': relationships([('rId1', 'slideMaster', '../slideMasters/slideMaster1.xml')])})
    for part, typ in [('ppt/slideMasters/slideMaster1.xml', 'slideMaster'), ('ppt/slideLayouts/slideLayout1.xml', 'slideLayout')]:
        content[part] = 'application/vnd.openxmlformats-officedocument.presentationml.'+typ+'+xml'
    content['ppt/theme/theme1.xml'] = 'application/vnd.openxmlformats-officedocument.theme+xml'
    parts['[Content_Types].xml'] = types(content)
    _validate_parts(parts)
    payload = package_bytes(parts)
    with Path(path).open('xb') as stream:
        stream.write(payload)
