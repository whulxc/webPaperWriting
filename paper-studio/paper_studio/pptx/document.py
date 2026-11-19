"""Bounded native OOXML tree editing. No shell, COM, network, or user code.

Targets: {slide: 1, shape: 'unique name', path: 'p:spPr/a:xfrm/a:off'}
or {slide: 1, shape_id: '7', path: ...} or {part: 'ppt/...xml', path: ...}.
Paths contain QName steps and optional ZERO-BASED same-name child indices;
empty/'.' selects the target itself. A leading root step is also supported.
Operations: set(attrs/text), insert(xml,index), replace(xml), delete,
move(destination,index), text(text), part_data(action,part,xml/data_base64,
content_type). All changes are validated together before a fresh output exists.
"""
from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import io
import json
import math
from pathlib import Path, PurePosixPath
import posixpath
import re
import struct
from urllib.parse import unquote
from xml.dom import Node, minidom
from xml.parsers import expat
import zipfile

from PIL import Image

P = 'http://schemas.openxmlformats.org/presentationml/2006/main'
A = 'http://schemas.openxmlformats.org/drawingml/2006/main'
R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
REL = 'http://schemas.openxmlformats.org/package/2006/relationships'
CT = 'http://schemas.openxmlformats.org/package/2006/content-types'
XMLNS = 'http://www.w3.org/2000/xmlns/'
NS = {'p': P, 'a': A, 'r': R, 'rel': REL, 'ct': CT,
      'xml': 'http://www.w3.org/XML/1998/namespace', 'xmlns': XMLNS}
MAX_PROGRAM = 8 * 1024 * 1024  # File-backed requests; transport has a lower cap.
MAX_PACKAGE = 64 * 1024 * 1024
MAX_EXPANDED = 128 * 1024 * 1024
MAX_ENTRIES = 3000
# Added in Paper Studio after the original snapshot: bound DOM amplification before
# minidom allocates nodes. Shared package accounting includes embedded XLSX.
MAX_XML_BYTES = 8 * 1024 * 1024
MAX_XML_NODES = 50_000
MAX_XML_DEPTH = 128
MAX_PACKAGE_XML_NODES = 200_000
QNAME = r'[A-Za-z_][A-Za-z0-9_.-]*(?::[A-Za-z_][A-Za-z0-9_.-]*)?'
STEP = re.compile(r'('+QNAME+r')(?:\[([0-9]+)\])?\Z')
NAME = re.compile(QNAME+r'\Z')
REL_ID = re.compile(r'[A-Za-z_][A-Za-z0-9_.-]*\Z')
IMAGE_TYPES = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg'}
XLSX_TYPE = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _suffix(name):
    return '.rels' if name.lower().endswith('.rels') else PurePosixPath(name).suffix.lower()


def _part_name(name):
    if not isinstance(name, str) or not 1 <= len(name) <= 512:
        raise ValueError('Part name must contain 1..512 characters')
    p = PurePosixPath(name)
    decoded = unquote(name)
    if (p.is_absolute() or posixpath.normpath(name) != name or name.endswith('/')
            or any(x in name for x in ('\\', ':', '\x00', '?', '#', '%'))
            or '..' in p.parts or any(ord(c) < 32 for c in name)
            or decoded != name):
        raise ValueError('Unsafe package part name')
    lower = name.lower()
    if lower.endswith('.rels') and not name.endswith('.rels'):
        raise ValueError('Relationship part extensions must use canonical lowercase .rels')
    if any(s in lower for s in ('vbaproject', '/activex/', '/oleobject', 'externalink', '/externallinks/')):
        raise ValueError('Executable or external-link part is prohibited')
    if '/embeddings/' in lower and not lower.endswith('.xlsx'):
        raise ValueError('Only static .xlsx chart workbooks may be embedded')
    return name


def _xml_preflight(raw, budget=None):
    """Allocation-light XML pass before DOM creation, with shared node limits.

    Charge elements, two nodes per attribute (Attr plus value Text), text runs,
    comments and processing instructions. This is deliberately conservative
    relative to minidom; namespace declarations count as attributes too.
    """
    if isinstance(raw, str):
        raw = raw.encode('utf-8')
    if len(raw) > MAX_XML_BYTES:
        raise ValueError('XML part exceeds 8 MiB byte budget')
    # Detect declarations after UTF-16/32 NUL removal as well as UTF-8.
    scan = raw.replace(b'\x00', b'').upper()
    if b'<!DOCTYPE' in scan or b'<!ENTITY' in scan:
        raise ValueError('DTD and entities are prohibited')
    package_nodes = 0 if budget is None else budget.get('xml_nodes', 0)
    count = depth = max_depth = 0
    in_text = False
    parser = expat.ParserCreate()
    parser.buffer_text = True

    def charge(amount):
        nonlocal count
        count += amount
        if count > MAX_XML_NODES:
            raise ValueError('XML part exceeds 50000-node budget')
        if package_nodes + count > MAX_PACKAGE_XML_NODES:
            raise ValueError('Package exceeds 200000 XML-node budget')

    def start(name, attrs):
        nonlocal depth, max_depth, in_text
        depth += 1
        max_depth = max(max_depth, depth)
        if depth > MAX_XML_DEPTH:
            raise ValueError('XML part exceeds 128-level depth budget')
        charge(1 + 2*len(attrs))
        in_text = False

    def end(name):
        nonlocal depth, in_text
        depth -= 1
        in_text = False

    def text(value):
        nonlocal in_text
        if value and not in_text:
            charge(1)
            in_text = True

    def leaf(*args):
        nonlocal in_text
        charge(1)
        in_text = False

    def text_boundary():
        nonlocal in_text
        in_text = False

    def forbidden(*args):
        raise ValueError('DTD and entities are prohibited')

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = text
    parser.CommentHandler = leaf
    parser.ProcessingInstructionHandler = leaf
    parser.StartCdataSectionHandler = text_boundary
    parser.EndCdataSectionHandler = text_boundary
    parser.StartDoctypeDeclHandler = forbidden
    parser.EntityDeclHandler = forbidden
    parser.ExternalEntityRefHandler = forbidden
    try:
        parser.Parse(raw, True)
    except expat.ExpatError as exc:
        raise ValueError('Invalid XML') from exc
    if budget is not None:
        budget['xml_nodes'] = package_nodes + count
    return {'bytes': len(raw), 'nodes': count, 'depth': max_depth}


def _parse(raw, budget=None):
    _xml_preflight(raw, budget)
    if isinstance(raw, str):
        raw = raw.encode('utf-8')
    try:
        doc = minidom.parseString(raw)
    except Exception as exc:
        raise ValueError('Invalid XML') from exc
    if doc.doctype is not None:
        raise ValueError('DTD is prohibited')
    for node in doc.getElementsByTagName('*'):
        if node.localName.lower() in ('oleobj', 'oleobject', 'activex', 'control', 'controls', 'vbaproject') or (node.namespaceURI == P and node.localName == 'cmd'):
            raise ValueError('OLE, ActiveX and controls are prohibited')
        for i in range(node.attributes.length):
            attr = node.attributes.item(i)
            if 'ppaction:' in attr.value.lower():
                raise ValueError('PowerPoint action execution is prohibited')
    return doc


def _serialize(doc):
    # minidom retains prefix spellings and all namespace declarations; notably
    # mc:Ignorable values do not become stale after namespace prefix rewriting.
    return doc.toxml(encoding='utf-8')


def _elements(node):
    return [n for n in node.childNodes if n.nodeType == Node.ELEMENT_NODE]


def _resolve(part, target):
    if not target or target.startswith('//') or any(c in target for c in ('\\', ':', '\x00', '?', '#', '%')):
        raise ValueError('Unsafe relationship target')
    result = target.lstrip('/') if target.startswith('/') else posixpath.normpath(
        posixpath.join(posixpath.dirname(part), target))
    return _part_name(result)


def _relationship_source(name):
    if name == '_rels/.rels':
        return ''
    directory, basename = posixpath.split(name)
    if not directory.endswith('/_rels') or not basename.endswith('.rels'):
        raise ValueError('Invalid relationship part location')
    return posixpath.join(posixpath.dirname(directory), basename[:-5])


def _rel_name(part):
    return posixpath.join(posixpath.dirname(part), '_rels', posixpath.basename(part)+'.rels')


def _content_types(parts, docs):
    if '[Content_Types].xml' not in docs:
        raise ValueError('Missing content types')
    root = docs['[Content_Types].xml'].documentElement
    if root.namespaceURI != CT or root.localName != 'Types':
        raise ValueError('Invalid content-types document')
    defaults, overrides = {}, {}
    for node in _elements(root):
        if node.namespaceURI != CT:
            raise ValueError('Content-type entries must use the native namespace')
        kind, value = node.localName, node.getAttribute('ContentType')
        low = value.lower()
        if (not value or any(s in low for s in ('macroenabled', 'macrosheet', 'vba', 'activex', 'oleobject'))):
            raise ValueError('Unsafe content type')
        if kind == 'Default':
            ext = node.getAttribute('Extension').lower()
            if not re.fullmatch(r'[a-z0-9]+', ext) or ext in defaults:
                raise ValueError('Invalid or duplicate content-type extension')
            defaults[ext] = value
        elif kind == 'Override':
            name = node.getAttribute('PartName')
            if not name.startswith('/'):
                raise ValueError('Override must name an absolute package part')
            name = _part_name(name[1:])
            if name in overrides or name not in parts:
                raise ValueError('Duplicate or dangling content-type override')
            overrides[name] = value
        else:
            raise ValueError('Unknown content-types element')
    result = {}
    for name in parts:
        if name == '[Content_Types].xml':
            continue
        value = overrides.get(name, defaults.get(_suffix(name)[1:]))
        if not value:
            raise ValueError('Part has no content type: '+name)
        result[name] = value
    return result


def _image_dimensions(raw, suffix):
    if suffix == '.png':
        if len(raw) < 33 or raw[:8] != b'\x89PNG\r\n\x1a\n' or raw[12:16] != b'IHDR':
            raise ValueError('Invalid PNG header')
        width, height = struct.unpack('>II', raw[16:24])
    else:
        if len(raw) < 4 or raw[:2] != b'\xff\xd8' or not raw.endswith(b'\xff\xd9'):
            raise ValueError('Invalid JPEG envelope')
        index, width, height = 2, 0, 0
        while index < len(raw)-1:
            if raw[index] != 255:
                raise ValueError('Invalid JPEG marker stream')
            while index < len(raw) and raw[index] == 255:
                index += 1
            if index >= len(raw):
                break
            marker = raw[index]
            index += 1
            if marker in (0x01, 0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                continue
            if index+2 > len(raw):
                break
            length = int.from_bytes(raw[index:index+2], 'big')
            if length < 2 or index+length > len(raw):
                raise ValueError('Invalid JPEG segment length')
            if marker in (0xC0,0xC1,0xC2,0xC3,0xC5,0xC6,0xC7,0xC9,0xCA,0xCB,0xCD,0xCE,0xCF):
                if length < 8:
                    raise ValueError('Invalid JPEG frame')
                height, width = struct.unpack('>HH', raw[index+3:index+7])
                break
            if marker == 0xDA:
                break
            index += length
        if not width or not height:
            raise ValueError('JPEG lacks supported frame dimensions')
    if not width or not height or max(width, height) > 32768 or width*height > 40_000_000:
        raise ValueError('Image exceeds static preview pixel budget')
    expected = 'PNG' if suffix == '.png' else 'JPEG'
    try:
        with Image.open(io.BytesIO(raw)) as image:
            if image.format != expected or image.size != (width,height):
                raise ValueError('Image decoder format/dimensions mismatch')
            image.verify()
        with Image.open(io.BytesIO(raw)) as image:
            image.load()  # verify alone does not decode all JPEG pixel data.
    except Exception as exc:
        raise ValueError('Image content verification/decoding failed') from exc
    return [width, height]


def _unzip(raw, budget):
    if len(raw) > MAX_PACKAGE:
        raise ValueError('Package exceeds 64 MiB')
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
        with archive:
            infos = archive.infolist()
            budget['entries'] += len(infos)
            budget['expanded'] += sum(info.file_size for info in infos)
            if budget['entries'] > MAX_ENTRIES or budget['expanded'] > MAX_EXPANDED:
                raise ValueError('Package exceeds entry/128 MiB expansion budget')
            names = [info.filename for info in infos]
            if len(names) != len(set(names)) or len(names) != len(set(n.casefold() for n in names)):
                raise ValueError('Duplicate ZIP entries')
            for info in infos:
                _part_name(info.filename)
                if info.flag_bits & 1 or (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError('Encrypted or symlink ZIP entries are prohibited')
            parts = {info.filename: archive.read(info) for info in infos}
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
        raise ValueError('Invalid or unsupported ZIP package') from exc
    return parts


def _embedded_font(name, raw):
    """Validate a bounded EOT/MTX envelope without decompressing or installing it.

    This admits opaque font preservation and native object inspection. It is
    not a validation of glyph bytecode or an independent font renderer.
    """
    if not re.fullmatch(r'ppt/fonts/[A-Za-z0-9_. -]+\.fntdata', name) or not 194 <= len(raw) <= 8*1024*1024:
        raise ValueError('Invalid embedded font path or byte budget')
    total, size, version, flags = struct.unpack_from('<4I', raw)
    if total != len(raw) or not 10 < size < total or version != 0x00020002 or flags not in (4, 5):
        raise ValueError('Unsupported or inconsistent embedded EOT envelope')
    if struct.unpack_from('<H',raw,34)[0] != 0x504C or any(raw[64:80]):
        raise ValueError('Invalid embedded EOT header')
    cursor = 80
    names = []
    for index in range(5):
        if cursor+4 > total-size:
            raise ValueError('Truncated embedded EOT names')
        padding, count = struct.unpack_from('<HH',raw,cursor)
        cursor += 4
        if padding or count % 2 or count > 4096 or cursor+count > total-size:
            raise ValueError('Invalid embedded EOT name length')
        value = raw[cursor:cursor+count]
        value.decode('utf-16le')
        names.append(value)
        cursor += count
    if cursor+20 != total-size:
        raise ValueError('Invalid embedded EOT data offset')
    root_checksum, codepage, padding, signature, eudc_flags, eudc_size = struct.unpack_from('<IIHHII',raw,cursor)
    if root_checksum != (sum(names[4]) ^ 0x50475342) or any((codepage,padding,signature,eudc_flags,eudc_size)):
        raise ValueError('Unsupported embedded EOT auxiliary data')
    cursor += 20
    mtx = memoryview(raw)[cursor:]
    copy_limit = int.from_bytes(mtx[1:4],'big')
    second = int.from_bytes(mtx[4:7],'big')
    third = int.from_bytes(mtx[7:10],'big')
    if mtx[0] != 3 or not 0 < copy_limit <= 16*1024*1024 or not 10 < second < third < size:
        raise ValueError('Invalid embedded MTX envelope')


def _validate_parts(parts, budget=None, workbook=False):
    if budget is None:
        budget = {'entries':len(parts), 'expanded':sum(map(len,parts.values()))}
        if budget['entries'] > MAX_ENTRIES or budget['expanded'] > MAX_EXPANDED:
            raise ValueError('Package exceeds entry/expansion budget')
    docs = {}
    for name, raw in parts.items():
        _part_name(name)
        if workbook and not (name in ('[Content_Types].xml','_rels/.rels','xl/workbook.xml',
                                       'xl/_rels/workbook.xml.rels','xl/sharedStrings.xml','xl/styles.xml')
                or re.fullmatch(r'xl/worksheets/[^/]+\.xml',name)
                or re.fullmatch(r'xl/theme/[^/]+\.xml',name)
                or name in ('docProps/core.xml','docProps/app.xml','docProps/custom.xml')):
            raise ValueError('Embedded XLSX may contain only basic static workbook parts')
        suffix = _suffix(name)
        if suffix in ('.xml', '.rels'):
            docs[name] = _parse(raw, budget)
        elif suffix in IMAGE_TYPES:
            _image_dimensions(raw, suffix)
        elif suffix == '.xlsx' and not workbook:
            nested = _unzip(raw, budget)
            _validate_parts(nested, budget, workbook=True)
        elif suffix == '.fntdata' and not workbook:
            _embedded_font(name, raw)
        else:
            raise ValueError('Unsupported package asset: '+name)
    types = _content_types(parts, docs)
    for name, value in types.items():
        suffix = _suffix(name)
        if suffix in IMAGE_TYPES and value != IMAGE_TYPES[suffix]:
            raise ValueError('Image content type disagrees with extension')
        if suffix == '.xlsx' and value != XLSX_TYPE:
            raise ValueError('Embedded workbook content type must be static XLSX')
        if suffix == '.fntdata' and value != 'application/x-fontdata':
            raise ValueError('Embedded font content type disagrees with extension')
        if suffix in ('.xml', '.rels') and not (value.endswith('+xml') or value in ('application/xml', 'text/xml')):
            raise ValueError('XML part has a non-XML content type')
    relationships = {}
    for name, doc in docs.items():
        if not name.endswith('.rels'):
            continue
        source = _relationship_source(name)
        if source and source not in parts:
            raise ValueError('Orphan relationship part')
        root, mapping = doc.documentElement, {}
        if root.namespaceURI != REL or root.localName != 'Relationships':
            raise ValueError('Invalid relationship root')
        for rel in _elements(root):
            rid, mode, typ = rel.getAttribute('Id'), rel.getAttribute('TargetMode'), rel.getAttribute('Type')
            if rel.namespaceURI != REL or rel.localName != 'Relationship' or not REL_ID.fullmatch(rid) or rid in mapping:
                raise ValueError('Invalid or duplicate relationship ID')
            if mode.lower() not in ('', 'internal'):
                raise ValueError('External relationships are prohibited')
            if not typ or any(c.isspace() for c in typ):
                raise ValueError('Relationship type must be a nonempty URI')
            if any(s in typ.lower() for s in ('vbaproject', 'oleobject', 'activex', 'externallink', '/control')):
                raise ValueError('Executable relationship type is prohibited')
            if workbook and typ not in {
                R+'/officeDocument',R+'/worksheet',R+'/sharedStrings',R+'/styles',R+'/theme',
                R+'/extended-properties',R+'/custom-properties',
                'http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties'}:
                raise ValueError('Embedded XLSX relationship has non-static capabilities')
            target = _resolve(source, rel.getAttribute('Target'))
            if target not in parts:
                raise ValueError('Dangling relationship target: '+target)
            mapping[rid] = {'target': target, 'type': typ}
        relationships[source] = mapping
    fonts = {name for name in parts if _suffix(name) == '.fntdata'}
    if len(fonts) > 16 or sum(len(parts[name]) for name in fonts) > 16*1024*1024:
        raise ValueError('Embedded fonts exceed count or byte budget')
    referenced_fonts = set()
    for source, mapping in relationships.items():
        for rid, relation in mapping.items():
            target = relation['target']
            if target not in fonts:
                continue
            if source != 'ppt/presentation.xml' or relation['type'] != R+'/font':
                raise ValueError('Embedded font relationship is not a presentation font')
            nodes = docs[source].getElementsByTagNameNS(P,'embeddedFont')
            if not any(child.namespaceURI == P and child.localName in ('regular','bold','italic','boldItalic')
                       and child.getAttributeNS(R,'id') == rid
                       for node in nodes for child in _elements(node)):
                raise ValueError('Embedded font lacks a native font declaration')
            referenced_fonts.add(target)
    if referenced_fonts != fonts:
        raise ValueError('Orphan embedded font asset')
    for name, doc in docs.items():
        if name.endswith('.rels'):
            continue
        ids = []
        for node in doc.getElementsByTagNameNS(P, 'cNvPr'):
            shape_id = node.getAttribute('id')
            if not re.fullmatch(r'[0-9]+', shape_id) or int(shape_id) > 0xFFFFFFFF:
                raise ValueError('Invalid native shape ID')
            ids.append(int(shape_id))
        if len(ids) != len(set(ids)):
            raise ValueError('Duplicate native shape ID in part: '+name)
        id_set = set(ids)
        for node in doc.getElementsByTagName('*'):
            if workbook:
                forbidden = {'f','connections','connection','querytable','externalreferences',
                             'externalreference','ddelink','olelink','macrosheet','intlmacrosheet',
                             'definednames','definedname','webpr','dbpr','olappr','externallink',
                             'externalbook','querytablerefresh','pivotcachedefinition','pivotcacherecords'}
                if node.localName.lower() in forbidden or any(node.attributes.item(i).localName.lower() == 'refreshonload' for i in range(node.attributes.length)):
                    raise ValueError('Embedded XLSX contains formulas, names, or external-data capabilities')
            elif node.localName == 'f':
                formula = ''.join(n.data for n in node.childNodes if n.nodeType in (Node.TEXT_NODE,Node.CDATA_SECTION_NODE))
                if (any(value in formula for value in ('[',']','\\','//'))
                        or re.search(r'(?:https?|file|ftp|data|javascript|mhtml|ms-excel)\s*:',formula,re.I)
                        or re.search(r'(?i)\.(?:xlsx?|xlsm|xlsb)\b.*!',formula)):
                    raise ValueError('Chart formula may only reference internal workbook data')
            if node.namespaceURI == A and node.localName in ('stCxn', 'endCxn'):
                value = node.getAttribute('id')
                if not value.isascii() or not value.isdigit() or int(value) not in id_set:
                    raise ValueError('Dangling native connector shape reference')
            for i in range(node.attributes.length):
                attr = node.attributes.item(i)
                if attr.namespaceURI == R and attr.localName in ('id', 'embed', 'link'):
                    if attr.value not in relationships.get(name, {}):
                        raise ValueError('Dangling XML relationship ID in '+name)
    if workbook:
        if 'xl/workbook.xml' not in docs:
            raise ValueError('Embedded XLSX lacks workbook.xml')
        spreadsheet_ns = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
        for name, doc in docs.items():
            expected = ('workbook' if name == 'xl/workbook.xml' else
                        'worksheet' if name.startswith('xl/worksheets/') else
                        'sst' if name == 'xl/sharedStrings.xml' else
                        'styleSheet' if name == 'xl/styles.xml' else None)
            if expected and (doc.documentElement.localName != expected or doc.documentElement.namespaceURI != spreadsheet_ns):
                raise ValueError('Embedded XLSX has a non-static native document root')
        return docs, relationships, types
    if 'ppt/presentation.xml' not in docs:
        raise ValueError('Missing presentation document')
    presentation = docs['ppt/presentation.xml']
    if presentation.documentElement.namespaceURI != P or presentation.documentElement.localName != 'presentation':
        raise ValueError('Invalid presentation root')
    sizes = [n for n in _elements(presentation.documentElement) if n.namespaceURI == P and n.localName == 'sldSz']
    if len(sizes) != 1:
        raise ValueError('Presentation requires exactly one native canvas size')
    dimensions = []
    for attr in ('cx','cy'):
        value = sizes[0].getAttribute(attr)
        if not re.fullmatch(r'[0-9]{1,12}',value):
            raise ValueError('Canvas dimensions must be finite positive integer EMU')
        mm = int(value)/36000
        if not math.isfinite(mm) or not 9 <= mm <= 2000:
            raise ValueError('Canvas dimensions must be 9..2000 mm')
        dimensions.append(mm)
    if not .1 <= dimensions[0]/dimensions[1] <= 10:
        raise ValueError('Canvas aspect ratio must be 1/10..10')
    slides = _slide_names(docs, relationships)
    if not 1 <= len(slides) <= 20 or len(set(slides)) != len(slides):
        raise ValueError('Expected 1..20 unique slides')
    for part in slides:
        root = docs[part].documentElement
        if root.namespaceURI != P or root.localName != 'sld':
            raise ValueError('Invalid slide root')
    return docs, relationships, types


def _slide_names(docs, relationships):
    presentation = docs['ppt/presentation.xml']
    refs = presentation.getElementsByTagNameNS(P, 'sldId')
    result = []
    for ref in refs:
        rid = ref.getAttributeNS(R, 'id')
        rel = relationships.get('ppt/presentation.xml', {}).get(rid)
        if not rel or not rel['type'].endswith('/slide') or rel['target'] not in docs:
            raise ValueError('Invalid presentation slide reference')
        result.append(rel['target'])
    return result


def read_package(path):
    """Read and safely validate a PPTX, including static embedded XLSX assets."""
    path = Path(path)
    if path.suffix.lower() != '.pptx' or path.is_symlink() or not path.is_file():
        raise ValueError('Input must be an ordinary .pptx file')
    if path.stat().st_size > MAX_PACKAGE:
        raise ValueError('Package exceeds 64 MiB')
    raw = path.read_bytes()
    budget = {'entries': 0, 'expanded': 0}
    parts = _unzip(raw, budget)
    _validate_parts(parts, budget)
    return raw, parts


def _target_schema(target):
    if not isinstance(target, dict):
        raise ValueError('target must be an object')
    if 'part' in target:
        if not set(target) <= {'part', 'path'}:
            raise ValueError('Part target accepts part/path only')
        _part_name(target['part'])
        if not target['part'].endswith(('.xml', '.rels')):
            raise ValueError('Tree targets must name XML or .rels parts')
    else:
        if not set(target) <= {'slide', 'shape', 'shape_id', 'path'} or type(target.get('slide')) is not int or not 1 <= target['slide'] <= 20:
            raise ValueError('Shape target requires slide 1..20')
        selectors = [k for k in ('shape', 'shape_id') if k in target]
        if len(selectors) != 1 or not isinstance(target[selectors[0]], str) or not 1 <= len(target[selectors[0]]) <= 1024:
            raise ValueError('Supply exactly one nonempty shape or shape_id')
    path = target.get('path', '')
    if not isinstance(path, str) or len(path) > 4096:
        raise ValueError('Invalid native element path')
    if path not in ('', '.'):
        tokens = path.removeprefix('/').split('/')
        if len(tokens) > 128 or not all(STEP.fullmatch(t) for t in tokens):
            raise ValueError('Paths accept only QName steps and zero-based numeric indices')


def validate_program(program):
    """Validate the bounded declarative schema, without touching any files."""
    if not isinstance(program, dict) or set(program) != {'operations'}:
        raise ValueError('Program requires exactly operations')
    try:
        raw = json.dumps(program, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError('Program must be finite JSON data') from exc
    if len(raw) > MAX_PROGRAM:
        raise ValueError('Program exceeds 8 MiB file-backed request budget')
    operations = program['operations']
    if not isinstance(operations, list) or not 0 <= len(operations) <= 256:
        raise ValueError('Expected 0..256 operations')
    schemas = {'set': {'op','target','attrs','text'}, 'insert': {'op','target','xml','index'},
               'replace': {'op','target','xml'}, 'delete': {'op','target'},
               'move': {'op','target','destination','index'}, 'text': {'op','target','text'},
               'part_data': {'op','action','part','xml','data_base64','content_type'}}
    for operation in operations:
        if not isinstance(operation, dict) or not isinstance(operation.get('op'),str) or operation['op'] not in schemas or not set(operation) <= schemas[operation['op']]:
            raise ValueError('Unknown operation or field')
        op = operation['op']
        if op == 'part_data':
            _part_name(operation.get('part'))
            if operation.get('action') not in ('add', 'replace', 'delete'):
                raise ValueError('part_data action must be add/replace/delete')
            sources = [k for k in ('xml', 'data_base64') if k in operation]
            if operation['action'] == 'delete':
                if sources or 'content_type' in operation:
                    raise ValueError('Part deletion accepts no content')
            else:
                if len(sources) != 1 or not isinstance(operation[sources[0]], str):
                    raise ValueError('Supply exactly one XML or base64 payload')
                suffix = _suffix(operation['part'])
                if (sources[0] == 'xml') != (suffix in ('.xml', '.rels')):
                    raise ValueError('XML payload/part extension mismatch')
                if suffix not in ('.xml', '.rels', '.png', '.jpg', '.jpeg', '.xlsx'):
                    raise ValueError('Part payload format is unsupported')
                if 'xml' in operation:
                    _parse(operation['xml'])
                else:
                    try:
                        base64.b64decode(operation['data_base64'], validate=True)
                    except (ValueError, binascii.Error) as exc:
                        raise ValueError('Invalid strict base64 payload') from exc
                content_type = operation.get('content_type')
                if operation['action'] == 'add' and not content_type:
                    raise ValueError('Added part requires content_type')
                if content_type is not None and (not isinstance(content_type, str) or not 1 <= len(content_type) <= 512):
                    raise ValueError('Invalid content_type')
            continue
        _target_schema(operation.get('target'))
        if op == 'move':
            _target_schema(operation.get('destination'))
        if 'index' in operation and (type(operation['index']) is not int or not 0 <= operation['index'] <= 100000):
            raise ValueError('index must be a nonnegative child-element index')
        if op in ('insert', 'replace'):
            if not isinstance(operation.get('xml'), str):
                raise ValueError('Operation requires one XML element')
            _parse(operation['xml'])
        if op == 'set':
            if not any(k in operation for k in ('attrs', 'text')):
                raise ValueError('set requires attrs and/or text')
            attrs = operation.get('attrs', {})
            if not isinstance(attrs, dict) or not attrs or not all(isinstance(k,str) and NAME.fullmatch(k) and (isinstance(v,str) or v is None) for k,v in attrs.items()):
                if 'attrs' in operation:
                    raise ValueError('attrs maps QName to string or null (remove)')
        if 'text' in operation and (not isinstance(operation['text'], str) or '\x00' in operation['text']):
            raise ValueError('text must be an XML-compatible string')
        if op == 'text' and 'text' not in operation:
            raise ValueError('text operation requires text')
    return {'valid': True, 'program_bytes': len(raw), 'operation_count': len(operations)}


def _namespace(node, prefix):
    if prefix in NS:
        return NS[prefix]
    while node is not None and node.nodeType == Node.ELEMENT_NODE:
        if node.hasAttribute('xmlns:'+prefix):
            return node.getAttribute('xmlns:'+prefix)
        node = node.parentNode
    raise ValueError('Unbound XML namespace prefix: '+prefix)


def _matches(node, qname, context):
    if ':' in qname:
        prefix, local = qname.split(':')
        return node.namespaceURI == _namespace(context, prefix) and node.localName == local
    return node.tagName == qname


def _find_path(root, path):
    if path in ('', '.'):
        return root
    tokens = path.removeprefix('/').split('/')
    if tokens and _matches(root, STEP.fullmatch(tokens[0]).group(1), root):
        first = STEP.fullmatch(tokens.pop(0))
        if first.group(2) not in (None, '0'):
            raise ValueError('Root index may only be zero')
    node = root
    for token in tokens:
        match = STEP.fullmatch(token)
        candidates = [c for c in _elements(node) if _matches(c, match.group(1), node)]
        if match.group(2) is None:
            if len(candidates) != 1:
                raise ValueError('Element path must match exactly one node: '+token)
            node = candidates[0]
        else:
            index = int(match.group(2))
            if index >= len(candidates):
                raise ValueError('Element path index is out of range')
            node = candidates[index]
    return node


def _select(target, docs, relationships):
    if 'part' in target:
        part = target['part']
        if part not in docs:
            raise ValueError('Target XML part does not exist')
        root = docs[part].documentElement
    else:
        slides = _slide_names(docs, relationships)
        if target['slide'] > len(slides):
            raise ValueError('Target slide does not exist')
        part = slides[target['slide']-1]
        prop, wanted = ('name', target['shape']) if 'shape' in target else ('id', target['shape_id'])
        matches = [n.parentNode.parentNode for n in docs[part].getElementsByTagNameNS(P,'cNvPr') if n.getAttribute(prop) == wanted]
        if len(matches) != 1:
            raise ValueError('Shape selector must match exactly one object')
        root = matches[0]
    return part, _find_path(root, target.get('path', ''))


def _fragment(xml, doc):
    fragment = _parse(xml)
    return doc.importNode(fragment.documentElement, deep=True)


def _insert(parent, child, index=None):
    children = _elements(parent)
    if index is None:
        parent.appendChild(child)
    elif index <= len(children):
        parent.insertBefore(child, children[index] if index < len(children) else None)
    else:
        raise ValueError('Insertion index exceeds child-element count')


def _node_path(node):
    tokens = []
    while node is not None and node.nodeType == Node.ELEMENT_NODE:
        siblings = [n for n in _elements(node.parentNode) if n.namespaceURI == node.namespaceURI and n.localName == node.localName]
        tokens.append(node.tagName+'['+str(siblings.index(node))+']')
        node = node.parentNode
    return '/'+('/'.join(reversed(tokens)))


def _attrs(node, attributes):
    for name, value in attributes.items():
        if ':' in name:
            prefix, local = name.split(':')
            uri = XMLNS if prefix == 'xmlns' else _namespace(node, prefix)
            if value is None:
                node.removeAttributeNS(uri, local) if node.hasAttributeNS(uri, local) else None
            else:
                node.setAttributeNS(uri, name, value)
        elif value is None:
            node.removeAttribute(name) if node.hasAttribute(name) else None
        else:
            node.setAttribute(name, value)


def _shape_text(node, text):
    body = node if node.localName == 'txBody' else next((n for n in _elements(node) if n.localName == 'txBody'), None)
    if body is None:
        raise ValueError('text convenience operation requires an existing txBody')
    paragraphs = [n for n in _elements(body) if n.namespaceURI == A and n.localName == 'p']
    first = paragraphs[0] if paragraphs else None
    ppr = next((n for n in _elements(first) if n.localName == 'pPr'), None) if first else None
    rpr = first.getElementsByTagNameNS(A,'rPr')[0] if first and first.getElementsByTagNameNS(A,'rPr') else None
    endpr = next((n for n in _elements(first) if n.localName == 'endParaRPr'), None) if first else None
    for paragraph in paragraphs:
        body.removeChild(paragraph)
    doc = node.ownerDocument
    for line in text.replace('\r\n','\n').replace('\r','\n').split('\n'):
        paragraph, run = doc.createElementNS(A,'a:p'), doc.createElementNS(A,'a:r')
        if ppr is not None:
            paragraph.appendChild(ppr.cloneNode(True))
        if rpr is not None:
            run.appendChild(rpr.cloneNode(True))
        content = doc.createElementNS(A,'a:t')
        content.setAttributeNS(NS['xml'],'xml:space','preserve')
        content.appendChild(doc.createTextNode(line))
        run.appendChild(content)
        paragraph.appendChild(run)
        if endpr is not None:
            paragraph.appendChild(endpr.cloneNode(True))
        body.appendChild(paragraph)


def _override(docs, part, content_type):
    root = docs['[Content_Types].xml'].documentElement
    matches = [n for n in _elements(root) if n.localName == 'Override' and n.getAttribute('PartName') == '/'+part]
    if content_type is None:
        for node in matches:
            root.removeChild(node)
    elif matches:
        matches[0].setAttribute('ContentType', content_type)
    else:
        node = root.ownerDocument.createElementNS(CT, (root.prefix+':' if root.prefix else '')+'Override')
        node.setAttribute('PartName', '/'+part)
        node.setAttribute('ContentType', content_type)
        root.appendChild(node)


def apply_program(input_path, output_path, program):
    """Apply generic operations atomically to a NEW candidate; source unchanged."""
    validation = validate_program(program)
    input_path, output_path = Path(input_path), Path(output_path)
    if not output_path.is_absolute() or output_path.suffix.lower() != '.pptx' or output_path.exists() or output_path.is_symlink():
        raise ValueError('Output must be a fresh absolute .pptx path')
    if output_path.resolve() == input_path.resolve():
        raise ValueError('Source must never be overwritten')
    original_raw, original = read_package(input_path)
    parts = dict(original)
    docs, relationships, types = _validate_parts(parts)
    dirty, changes = set(), []
    for index, operation in enumerate(program['operations']):
        op = operation['op']
        record = {'operation': index, 'op': op}
        if op == 'part_data':
            part, action = operation['part'], operation['action']
            exists = part in parts
            if (action == 'add' and exists) or (action in ('replace','delete') and not exists):
                raise ValueError('part_data existence precondition failed')
            before = _serialize(docs[part]) if part in dirty and part in docs else parts.get(part)
            record.update({'part': part, 'action': action, 'before_sha256': _sha(before) if before is not None else None})
            if action == 'delete':
                if part == '[Content_Types].xml':
                    raise ValueError('Cannot delete required content-types root')
                del parts[part]
                docs.pop(part, None)
                types.pop(part, None)
                dirty.discard(part)
                _override(docs, part, None)
                record['after_sha256'] = None
            else:
                content = operation['xml'].encode('utf-8') if 'xml' in operation else base64.b64decode(operation['data_base64'], validate=True)
                parts[part] = content
                if part.endswith(('.xml','.rels')):
                    docs[part] = _parse(content)
                else:
                    docs.pop(part, None)
                dirty.discard(part)
                record['after_sha256'] = _sha(content)
                if part != '[Content_Types].xml':
                    content_type = operation.get('content_type') or types.get(part)
                    if not content_type:
                        raise ValueError('Part requires a content type')
                    _override(docs, part, content_type)
                    types[part] = content_type
            if part != '[Content_Types].xml':
                dirty.add('[Content_Types].xml')
        else:
            part, node = _select(operation['target'], docs, relationships)
            before = node.toxml(encoding='utf-8')
            record.update({'part': part, 'target': copy.deepcopy(operation['target']),
                           'element': node.tagName, 'resolved_path':_node_path(node), 'before_sha256': _sha(before)})
            previous_location = (part,_node_path(node.parentNode),_elements(node.parentNode).index(node))
            if op == 'set':
                _attrs(node, operation.get('attrs', {}))
                if 'text' in operation:
                    for child in list(node.childNodes):
                        node.removeChild(child)
                    node.appendChild(node.ownerDocument.createTextNode(operation['text']))
            elif op == 'insert':
                _insert(node, _fragment(operation['xml'], node.ownerDocument), operation.get('index'))
            elif op == 'replace':
                replacement = _fragment(operation['xml'], node.ownerDocument)
                node.parentNode.replaceChild(replacement, node)
                node = replacement
            elif op == 'delete':
                if node.parentNode.nodeType == Node.DOCUMENT_NODE:
                    raise ValueError('Use part_data to remove a package part')
                node.parentNode.removeChild(node)
                node = None
            elif op == 'move':
                destination_part, destination = _select(operation['destination'], docs, relationships)
                ancestor = destination
                while ancestor is not None:
                    if ancestor is node:
                        raise ValueError('Cannot move a node into itself/its descendant')
                    ancestor = ancestor.parentNode
                if node.parentNode.nodeType == Node.DOCUMENT_NODE:
                    raise ValueError('Moving a document root is unsupported')
                node.parentNode.removeChild(node)
                if destination.ownerDocument is not node.ownerDocument:
                    node = destination.ownerDocument.importNode(node, deep=True)
                _insert(destination, node, operation.get('index'))
                dirty.add(destination_part)
                record['destination'] = copy.deepcopy(operation['destination'])
                record['destination_part'] = destination_part
                record['before_location'] = {'part':part,'parent_path':previous_location[1],'element_index':previous_location[2]}
                record['after_location'] = {'part':destination_part,'parent_path':_node_path(node.parentNode),'element_index':_elements(node.parentNode).index(node)}
            elif op == 'text':
                _shape_text(node, operation['text'])
            record['after_sha256'] = _sha(node.toxml(encoding='utf-8')) if node is not None else None
            record['actual_change'] = ((previous_location != (destination_part,_node_path(node.parentNode),_elements(node.parentNode).index(node))) if op == 'move' else record['before_sha256'] != record['after_sha256'])
            dirty.add(part)
        changes.append(record)
        # Keep relationship routing current without validating transient states:
        # a multi-op program may add a part before adding its relationship.
        relationships = {}
        for name, doc in docs.items():
            if name.endswith('.rels'):
                source = _relationship_source(name)
                relationships[source] = {r.getAttribute('Id'): {'target': _resolve(source,r.getAttribute('Target')), 'type':r.getAttribute('Type')} for r in _elements(doc.documentElement)}
    for part in dirty:
        if part in docs:
            parts[part] = _serialize(docs[part])
    if len(parts) > MAX_ENTRIES or sum(map(len,parts.values())) > MAX_EXPANDED:
        raise ValueError('Candidate exceeds package expansion budget')
    _validate_parts(parts)
    if parts == original:
        candidate = original_raw
    else:
        buffer = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(original_raw)) as old, zipfile.ZipFile(buffer,'w',compression=zipfile.ZIP_DEFLATED) as archive:
            old_infos = {i.filename: i for i in old.infolist()}
            for name, content in parts.items():
                archive.writestr(copy.copy(old_infos[name]) if name in old_infos else name, content)
        candidate = buffer.getvalue()
    budget = {'entries':0,'expanded':0}
    _validate_parts(_unzip(candidate,budget), budget)
    if input_path.read_bytes() != original_raw:
        raise ValueError('Source changed concurrently; candidate was not published')
    diffs = []
    for part in sorted(set(original)|set(parts)):
        before, after = original.get(part), parts.get(part)
        if before != after:
            diffs.append({'part':part,'action':'add' if before is None else 'delete' if after is None else 'replace',
                          'before_bytes':len(before) if before is not None else None,
                          'after_bytes':len(after) if after is not None else None,
                          'before_sha256':_sha(before) if before is not None else None,
                          'after_sha256':_sha(after) if after is not None else None})
    with output_path.open('xb') as stream:
        stream.write(candidate)
    return {'schema':'paper-studio-pptx-native-edit-v1','validation':validation,
            'input_path':str(input_path.absolute()),'input_bytes':len(original_raw),'input_sha256':_sha(original_raw),
            'output_path':str(output_path),'output_bytes':len(candidate),'output_sha256':_sha(candidate),
            'source_unchanged':True,'changes':changes,'part_changes':diffs,
            'changed_part_count':len(diffs),'slide_count':len(_slide_names(*_validate_parts(parts)[:2]))}


def describe_deck(path):
    """Read-only package/slide/object selector inventory, with part XML hashes."""
    raw, parts = read_package(path)
    docs, relationships, types = _validate_parts(parts)
    slides = []
    for index, part in enumerate(_slide_names(docs,relationships),1):
        shapes = []
        for prop in docs[part].getElementsByTagNameNS(P,'cNvPr'):
            node = prop.parentNode.parentNode
            if node.localName == 'spTree':
                continue
            shapes.append({'name':prop.getAttribute('name'),'id':prop.getAttribute('id'),'type':node.localName,
                           'xml_sha256':_sha(node.toxml(encoding='utf-8'))})
        slides.append({'slide':index,'part':part,'object_count':len(shapes),'objects':shapes})
    return {'input_bytes':len(raw),'input_sha256':_sha(raw),'slide_count':len(slides),'slides':slides,
            'parts':[{'part':name,'bytes':len(content),'sha256':_sha(content),
                      'content_type':types.get(name),'xml_available':name in docs} for name,content in sorted(parts.items())]}


def describe_parts(path, part_names=None, offset=0, limit=25, include_xml=False):
    """Read-only pages of parts; XML returned verbatim, binaries as metadata."""
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100 or type(include_xml) is not bool:
        raise ValueError('Invalid part page parameters')
    raw, parts = read_package(path)
    if part_names is not None and (not isinstance(part_names,list) or not all(isinstance(n,str) and n in parts for n in part_names)):
        raise ValueError('part_names must name existing internal parts')
    names = sorted(parts) if part_names is None else list(part_names)
    result = []
    for name in names[offset:offset+limit]:
        content = parts[name]
        record = {'part':name,'bytes':len(content),'sha256':_sha(content)}
        if include_xml and name.endswith(('.xml','.rels')):
            # All OOXML encodings are accepted by minidom; serialize the DOM
            # only when the original is not UTF-8, and disclose that encoding.
            try:
                record['xml'] = content.decode('utf-8-sig')
                record['xml_representation'] = 'original UTF-8'
            except UnicodeDecodeError:
                record['xml'] = _parse(content).toxml()
                record['xml_representation'] = 'DOM Unicode; original hash above'
        result.append(record)
    end = offset+len(result)
    return {'input_sha256':_sha(raw),'total':len(names),'offset':offset,'next_offset':end if end<len(names) else None,'parts':result}


def describe_part(path, part):
    return describe_parts(path,[part],limit=1,include_xml=True)['parts'][0]
