"""Verify a Drive annotation derivative inside the existing read-only PDF worker."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


ACTIVE_KEYS = {'/AA','/JS','/JavaScript','/Launch','/RichMediaContent',
               '/EmbeddedFiles','/EmbeddedFile','/XFA','/AcroForm'}
ANNOTATIONS = {'/Text','/Popup','/Highlight','/Underline','/StrikeOut','/Squiggly',
               '/Ink','/Square','/Circle','/Caret','/Polygon','/PolyLine','/Stamp','/Link'}


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _annotations(page):
    value=page.get('/Annots',[])
    return value.get_object() if hasattr(value,'get_object') else value


def _canonical(value, budget, active=None, depth=0):
    """Resolve resource references into deterministic content, never execute actions."""
    from pypdf.generic import IndirectObject, StreamObject
    if depth > 48 or budget[0] <= 0:
        raise ValueError('PDF object structure exceeds its bounded review budget')
    budget[0] -= 1
    if isinstance(value, IndirectObject):
        identity=(value.idnum,value.generation)
        if active is not None and identity in active:
            raise ValueError('Cyclic resource references are unsupported')
        return _canonical(value.get_object(),budget,(active or set())|{identity},depth+1)
    if isinstance(value,dict):
        if ACTIVE_KEYS.intersection(map(str,value)):
            raise ValueError('Active PDF content is forbidden in review derivatives')
        result={str(key):_canonical(item,budget,active,depth+1) for key,item in sorted(value.items(),key=lambda pair:str(pair[0]))
                if str(key) not in {'/Length','/Filter','/DecodeParms'}}
        if isinstance(value,StreamObject):
            data=value.get_data()
            if len(data)>64*1024**2:
                raise ValueError('PDF stream exceeds its bounded review budget')
            result['decoded_stream_sha256']=_digest(data)
        return result
    if isinstance(value,(list,tuple)):
        return [_canonical(item,budget,active,depth+1) for item in value]
    if isinstance(value,(str,int,float,bool)) or value is None:
        return value
    return str(value)


def _screen(reader):
    """Check the object graph for script, attachment and launch entry points."""
    from pypdf.generic import IndirectObject
    seen=set();remaining=[100000]
    def walk(value,depth=0):
        if depth>64 or remaining[0]<=0:
            raise ValueError('PDF graph exceeds its bounded review budget')
        remaining[0]-=1
        if isinstance(value,IndirectObject):
            key=(value.idnum,value.generation)
            if key in seen:return
            seen.add(key);walk(value.get_object(),depth+1)
        elif isinstance(value,dict):
            if ACTIVE_KEYS.intersection(map(str,value)):
                raise ValueError('Active PDF content is forbidden in review derivatives')
            if '/OpenAction' in value:
                action=value['/OpenAction'].get_object()
                destination=action.get('/D') if isinstance(action,dict) and str(action.get('/S',''))=='/GoTo' else action
                if not isinstance(destination,(list,tuple)) or not 2<=len(destination)<=6:
                    raise ValueError('Automatic PDF actions must be a static internal view destination')
            if str(value.get('/S','')) in {'/JavaScript','/Launch','/SubmitForm','/ImportData'}:
                raise ValueError('Executable or form-submission PDF actions are forbidden')
            for item in value.values():walk(item,depth+1)
        elif isinstance(value,(list,tuple)):
            for item in value:walk(item,depth+1)
    walk(reader.trailer['/Root'])


def inspect(baseline_path, reviewed_path):
    from pypdf import PdfReader
    import sys
    if Path('/opt/addons').is_dir() and '/opt/addons' not in sys.path:
        sys.path.insert(0,'/opt/addons')
    import pymupdf
    original, reviewed=Path(baseline_path).read_bytes(),Path(reviewed_path).read_bytes()
    before,after=PdfReader(baseline_path,strict=True),PdfReader(reviewed_path,strict=True)
    if before.is_encrypted or after.is_encrypted:
        raise ValueError('Encrypted review derivatives are unsupported')
    if not 1<=len(before.pages)==len(after.pages)<=2000:
        raise ValueError('Review derivative page count changed or exceeds the review budget')
    _screen(before);_screen(after)
    rows=[]
    with pymupdf.open(stream=original,filetype='pdf') as pm_before, pymupdf.open(stream=reviewed,filetype='pdf') as pm_after:
        for index,(left,right) in enumerate(zip(before.pages,after.pages)):
            for reference in _annotations(right):
                if str(reference.get_object().get('/Subtype','')) not in ANNOTATIONS:
                    raise ValueError('Only ordinary review annotations and unchanged links are supported')
            left_stream=left.get_contents();right_stream=right.get_contents()
            geometry=lambda page: {'media':list(map(float,page.mediabox)),'crop':list(map(float,page.cropbox)),
                                   'rotation':page.rotation,'unit':float(page.get('/UserUnit',1))}
            canonical=lambda page: _digest(json.dumps(_canonical(page.get('/Resources',{}),[100000]),
                                                      sort_keys=True,separators=(',',':')).encode())
            a,b=pm_before[index],pm_after[index]
            same_stream=_digest(left_stream.get_data() if left_stream else b'')==_digest(right_stream.get_data() if right_stream else b'')
            same_resources=canonical(left)==canonical(right)
            same_text=a.get_text('rawdict')==b.get_text('rawdict')
            links=lambda page: [{key:str(value) for key,value in row.items() if key not in {'xref','id'}}
                                for row in page.get_links()]
            same_links=links(a)==links(b)
            pix_a=a.get_pixmap(matrix=pymupdf.Matrix(1,1),alpha=False,annots=False)
            pix_b=b.get_pixmap(matrix=pymupdf.Matrix(1,1),alpha=False,annots=False)
            same_render=(pix_a.width,pix_a.height,_digest(pix_a.samples))==(pix_b.width,pix_b.height,_digest(pix_b.samples))
            row={'page':index+1,'geometry_identical':geometry(left)==geometry(right),'content_stream_identical':same_stream,
                 'resources_identical':same_resources,'text_geometry_identical':same_text,'render_without_annotations_identical':same_render,
                 'links_identical':same_links,
                 'baseline_annotation_count':len(_annotations(left)), 'reviewed_annotation_count':len(_annotations(right))}
            if not all(row[key] for key in ('geometry_identical','content_stream_identical','resources_identical','links_identical',
                                            'text_geometry_identical','render_without_annotations_identical')):
                raise ValueError('Review derivative changes page content, resources or unannotated rendering')
            rows.append(row)
    return {'baseline_sha256':_digest(original),'baseline_bytes':len(original),
            'reviewed_sha256':_digest(reviewed),'reviewed_bytes':len(reviewed),
            'baseline_prefix_preserved':reviewed.startswith(original),'pages':rows,
            'page_content_identity_verified':True,'active_content_rejected':True,
            'unchanged_original_pdf_bytes':original==reviewed}


def main(request):
    def selected(name):
        value=request[name]
        if not isinstance(value,str) or Path(value).name!=value or not value.endswith('.pdf'):
            raise ValueError('Select one fixed PDF basename for identity review')
        return Path('/input')/value
    result=inspect(selected('baseline_pdf'),selected('reviewed_pdf'))
    Path('/work/annotation-identity.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
