"""Optional, isolated PyMuPDF 1.28.2 review backend; no OCR/network/source edits.

API provenance: official PyMuPDF Page / Annot documentation. PyMuPDF/MuPDF
are AGPL/commercial dual licensed; isolated installation is not an exemption.
https://pymupdf.readthedocs.io/en/latest/about.html#license-and-copyright
"""
from __future__ import annotations
import hashlib
import json
import math
from pathlib import Path
import re
import sys
import unicodedata

MAX_WORDS=50000
MAX_ANNOTATIONS=2000
MAX_IMAGES=2000
MAX_DRAWINGS=2000
MAX_TEX_FILES=32
MAX_TEX_BYTES=2*1024**2
MAX_SIDECAR_BYTES=8*1024**2
MAX_SIDECAR_FILE_BYTES=2*1024**2


def engine():
    if Path('/opt/addons').is_dir() and '/opt/addons' not in sys.path:
        sys.path.insert(0,'/opt/addons')
    try:import pymupdf
    except ImportError as error:
        raise RuntimeError('Explicit pymupdf backend requires PyMuPDF 1.28.2 in isolated python_addons; no fallback') from error
    if str(pymupdf.VersionBind)!='1.28.2':
        raise RuntimeError('Expected pinned PyMuPDF 1.28.2; incompatible dependency is not silently accepted')
    return pymupdf


def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def numbers(value):
    result=[float(v) for v in value]
    if not all(math.isfinite(v) for v in result):raise ValueError('Nonfinite PDF geometry')
    return result


def annotation_vertices(value):
    """Bound both nested ink strokes and ordinary point arrays."""
    budget=[1024];truncated=[False]
    def collect(item,depth=0):
        if depth>3 or budget[0]<=0:
            truncated[0]=True;return []
        if isinstance(item,(list,tuple)) and len(item)==2 and all(isinstance(v,(int,float)) for v in item):
            budget[0]-=1;return numbers(item)
        result=[]
        for child in item or ():
            if budget[0]<=0:truncated[0]=True;break
            result.append(collect(child,depth+1))
        return result
    return collect(value),truncated[0]


def geometry(pm,raw_page,page):
    """Normalize physical points from PDF dictionaries, not version quirks.

    PyMuPDF 1.28.2 can return unscaled rotation translations / annotation
    coordinates for rotated UserUnit pages. Retain those library values as
    diagnostic evidence; construct the public mapping from raw PDF geometry.
    """
    unit=float(raw_page.get('/UserUnit',1))
    if not math.isfinite(unit) or unit<=0:raise ValueError('Invalid PDF UserUnit')
    crop=pm.Rect(numbers(raw_page.cropbox))&pm.Rect(numbers(raw_page.mediabox))
    if crop.is_empty:raise ValueError('Empty visible PDF crop')
    width,height=crop.width*unit,crop.height*unit
    rotation=page.rotation
    matrices={0:(1,0,0,1,0,0),90:(0,1,-1,0,height,0),
              180:(-1,0,0,-1,width,height),270:(0,-1,1,0,0,width)}
    if rotation not in matrices:raise ValueError('Unsupported PDF rotation')
    rotate=pm.Matrix(*matrices[rotation])
    displayed=(height,width) if rotation in (90,270) else (width,height)
    if max(abs(page.rect.width-displayed[0]),abs(page.rect.height-displayed[1]))>0.01:
        raise ValueError('PDF render geometry differs from raw CropBox/UserUnit; coordinates unsupported')
    transform=pm.Matrix(unit,0,0,-unit,-crop.x0*unit,crop.y1*unit)
    discrepancy=max(abs(a-b) for a,b in zip(rotate,page.rotation_matrix))>0.01 or max(abs(a-b) for a,b in zip(transform,page.transformation_matrix))>0.01
    return {'unit':unit,'crop':crop,'width':width,'height':height,'rotation':rotate,'derotation':~rotate,
            'pdf_to_unrotated':transform,'library_discrepancy':discrepancy}


def raw_annotation_geometry(pm,raw,mapping,annotation):
    """Use native PDF Rect/QuadPoints so all annotation types share text space."""
    if raw is None:
        if mapping['library_discrepancy']:raise ValueError('Raw annotation geometry unavailable on nonstandard coordinate page')
        vertices,truncated=annotation_vertices(annotation.vertices)
        return annotation.rect,vertices,truncated
    transform=mapping['pdf_to_unrotated']
    rect=pm.Rect(numbers(raw['/Rect']))*transform
    truncated=False
    def points(values):
        nonlocal truncated
        if len(values)>2048:truncated=True
        values=values[:2048]
        if len(values)%2:raise ValueError('Invalid native annotation point array')
        return [numbers(pm.Point(float(values[i]),float(values[i+1]))*transform) for i in range(0,len(values),2)]
    if '/InkList' in raw:
        strokes=raw['/InkList']
        if len(strokes)>32:truncated=True
        vertices=[points(stroke) for stroke in strokes[:32]]
    else:
        values=raw.get('/QuadPoints',raw.get('/Vertices',raw.get('/L',[])))
        vertices=points(values)
    return rect,vertices,truncated


def normalize(text):
    return ' '.join(unicodedata.normalize('NFKC',text).casefold().split())


def tex_text(text):
    text=re.sub(r'(?<!\\)%[^\n]*','',text)
    text=re.sub(r'\\([%&_#$])',r'\1',text)
    text=re.sub(r'\\[A-Za-z@]+\*?(?:\[[^\]]*\])?',' ',text)
    return normalize(text.replace('{',' ').replace('}',' '))


def sources(root):
    records=[];total=0;truncated=False
    for path in sorted(root.rglob('*.tex')):
        size=path.stat().st_size
        if len(records)>=MAX_TEX_FILES or size>MAX_TEX_BYTES-total:
            truncated=True;continue
        raw=path.read_bytes();total+=len(raw)
        records.append({'path':path.relative_to(root).as_posix(),'sha256':hashlib.sha256(raw).hexdigest(),
                        'lines':raw.decode('utf-8',errors='replace').splitlines()})
    return records,truncated


def locate(quote,source_files,pdf_hash,*,approximate=False,source_scan_truncated=False):
    needle=normalize(quote)
    base={'source_binding':'unbound','pdf_sha256':pdf_hash,'quote':quote[:2048],
          'normalization':'NFKC/casefold/whitespace; TeX commands/comments/braces stripped; no macro execution',
          'exact_source_line':False,'source_scan_truncated':source_scan_truncated}
    if len(needle)<8:
        return {**base,'status':'unbound','confidence':'none','reason':'insufficient PDF text anchor','candidates':[]}
    matches=[]
    for width in (1,2,3):
        for source in source_files:
            for index in range(max(0,len(source['lines'])-width+1)):
                fragment=' '.join(source['lines'][index:index+width])
                if needle in tex_text(fragment):
                    matches.append({'path':source['path'],'source_sha256':source['sha256'],'line_start':index+1,
                                    'line_end':index+width,'snippet':fragment[:2048]})
                    if len(matches)>=64:break
            if len(matches)>=64:break
        if matches:break
    if len(matches)==1 and not source_scan_truncated:
        return {**base,'status':'unbound','match_status':'unique_text_candidate','confidence':'low' if approximate else 'medium',
                'reason':'unique textual match is not evidence of the PDF compilation source','candidates':matches}
    return {**base,'status':'ambiguous' if matches else 'unbound','confidence':'low' if matches else 'none',
            'reason':'multiple or incomplete source candidates' if matches else 'no source text match','candidates':matches,
            'candidate_limit_reached':len(matches)>=64}


def write_sidecar(work,name,records,*,truncated=False):
    # Serialize incrementally under a strict byte budget; never collect unlimited
    # data in the public build result. Native parser memory is cgroup bounded.
    kept=[];used=0
    for record in records:
        size=len(json.dumps(record,ensure_ascii=False,allow_nan=False).encode('utf-8'))+2
        if used+size>MAX_SIDECAR_FILE_BYTES-1024:
            truncated=True;break
        kept.append(record);used+=size
    payload={'records':kept,'truncated':truncated}
    path=work/name
    raw=(json.dumps(payload,ensure_ascii=False,allow_nan=False)+'\n').encode('utf-8')
    if len(raw)>MAX_SIDECAR_FILE_BYTES:raise ValueError('PDF sidecar byte limit exceeded')
    path.write_bytes(raw)
    return {'path':name,'records':len(kept),'truncated':truncated,'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}


def apply_annotations(source,output,operations):
    """Trusted internal writer for fixtures/adapters; not a public MCP action.

    Coordinates are PyMuPDF unrotated page points. Save a fresh derived file;
    never flatten, execute actions, redact or modify the input document.
    """
    pm=engine();source=Path(source);output=Path(output)
    if not source.is_absolute() or source.resolve()!=source or not output.is_absolute() or output.resolve()!=output:
        raise ValueError('Annotation writer requires canonical absolute paths')
    if output.exists() or output==source or output.suffix.lower()!='.pdf':raise ValueError('Fresh derived PDF output required')
    if not isinstance(operations,list) or len(operations)>MAX_ANNOTATIONS:raise ValueError('Bounded annotation operation list required')
    original=digest(source)
    from pypdf import PdfReader
    raw_document=PdfReader(source,strict=True)
    with pm.open(source) as document:
        for operation in operations:
            kind=operation.get('type');number=operation.get('page')
            if kind not in {'note','highlight','rect'} or isinstance(number,bool) or not isinstance(number,int) or not 1<=number<=len(document):
                raise ValueError('Unsupported annotation type/page')
            text=operation.get('text','')
            if not isinstance(text,str) or len(text)>4096:raise ValueError('Bounded plain annotation text required')
            page=document[number-1]
            mapping=geometry(pm,raw_document.pages[number-1],page)
            bounds=pm.Rect(0,0,mapping['width'],mapping['height'])
            if kind=='note':
                point=pm.Point(numbers(operation.get('point',[])))
                if not bounds.contains(point):raise ValueError('Annotation point outside visible page')
                annotation=page.add_text_annot(point,text)
            else:
                rect=pm.Rect(numbers(operation.get('rect',[])))
                if rect.is_empty or not bounds.contains(rect):raise ValueError('Annotation rectangle outside visible page')
                annotation=page.add_highlight_annot(rect) if kind=='highlight' else page.add_rect_annot(rect)
                annotation.set_info(content=text)
            annotation.update()
        document.save(output,garbage=0,deflate=True)
    if digest(source)!=original:raise RuntimeError('Input PDF changed during annotation writing')
    return {'source_sha256':original,'output_sha256':digest(output),'path':str(output),'operations':len(operations),
            'source_unchanged':True,'coordinate_system':'pymupdf_unrotated_points'}


def review(path,work,legacy_info):
    pm=engine();root=Path('/input');pdf_hash=digest(path)
    tex,source_truncated=sources(root)
    layout=[];annotations=[];locations=[];diagnostics=[];rendered=[];thumbnails=[]
    words_count=images_count=drawings_count=0;layout_truncated=False;annotation_truncated=False
    from pypdf import PdfReader
    raw_document=PdfReader(path,strict=True)
    with pm.open(path) as document:
        if not document.is_pdf or document.is_encrypted:raise ValueError('Unencrypted PDF required')
        if len(document)!=legacy_info.get('page_count',len(legacy_info['pages'])):raise ValueError('PDF parsers disagree on page count')
        if document.is_repaired:diagnostics.append({'code':'PDF_REPAIRED_BY_MUPDF','severity':'warning'})
        for font in legacy_info['fonts']:
            if not font['embedded']:diagnostics.append({'code':'UNEMBEDDED_FONT','page':font['page'],'font':font['base_font'],'severity':'warning'})
        for number in legacy_info.get('selected_pages',list(range(1,len(document)+1))):
            page=document[number-1]
            raw_page=raw_document.pages[number-1]
            mapping=geometry(pm,raw_page,page)
            if mapping['library_discrepancy']:
                diagnostics.append({'code':'LIBRARY_GEOMETRY_NORMALIZED','page':number,'severity':'warning','user_unit':mapping['unit'],
                    'evidence':'raw PDF CropBox/UserUnit-derived matrices differ from library matrices; native annotation coordinates normalized from PDF Rect/QuadPoints'})
            if max(page.rect.width,page.rect.height)>1200:raise ValueError('Rendered PDF physical size exceeds review limit')
            pix=page.get_pixmap(dpi=150,alpha=False,annots=True)
            destination=work/f'page_{number:03}.png';pix.save(destination)
            rendered.append({'page':number,'path':destination.name,'pixels':[pix.width,pix.height],'dpi':150,'sha256':digest(destination)})
            pixel_matrix=mapping['rotation']*pm.Matrix(150/72,150/72)*pm.Matrix(1,0,0,1,-pix.x,-pix.y)
            del pix
            scale=min(1,512/max(page.rect.width,page.rect.height))
            thumb=page.get_pixmap(matrix=pm.Matrix(scale,scale),alpha=False,annots=True)
            thumb_path=work/f'thumb_{number:03}.png';thumb.save(thumb_path)
            thumbnails.append({'page':number,'path':thumb_path.name,'pixels':[thumb.width,thumb.height],'sha256':digest(thumb_path)})
            del thumb
            words=page.get_text('words',sort=True)
            valid_words=[]
            for word in words:
                if words_count>=MAX_WORDS:layout_truncated=True;break
                bbox=numbers(word[:4]);text=str(word[4])[:512]
                valid_words.append((bbox,text,word[5:]))
                layout.append({'kind':'word','page':number,'bbox':bbox,'text':text,'block_line_word':list(word[5:])});words_count+=1
            layout.append({'kind':'page_geometry','page':number,'coordinate_system':'pymupdf_unrotated_points',
                'mediabox':numbers(page.mediabox),'cropbox':numbers(page.cropbox),'page_rect':numbers(page.rect),'rotation':page.rotation,
                'user_unit':mapping['unit'],'pdf_cropbox':numbers(mapping['crop']),
                'matrix_source':'native_pdf_cropbox_userunit',
                'pdf_to_mupdf':numbers(mapping['pdf_to_unrotated']),'rotation_matrix':numbers(mapping['rotation']),
                'derotation_matrix':numbers(mapping['derotation']),'unrotated_to_pixels':numbers(pixel_matrix),
                'library_pdf_to_mupdf':numbers(page.transformation_matrix),'library_rotation_matrix':numbers(page.rotation_matrix),
                'library_geometry_discrepancy':mapping['library_discrepancy']})
            for image in page.get_image_info(hashes=False,xrefs=False):
                if images_count>=MAX_IMAGES:layout_truncated=True;break
                layout.append({'kind':'image_instance','page':number,'bbox':numbers(image['bbox']),
                    'width':image['width'],'height':image['height'],'transform':numbers(image['transform'])});images_count+=1
                bounds=pm.Rect(image['bbox'])
                if bounds.width>0 and bounds.height>0:
                    estimated_dpi=min(72*image['width']/bounds.width,72*image['height']/bounds.height)
                    if estimated_dpi<100:diagnostics.append({'code':'LOW_ESTIMATED_IMAGE_DPI','page':number,'severity':'information',
                        'estimated_dpi':estimated_dpi,'bbox':numbers(bounds),'limitation':'geometric estimate; no publisher-compliance claim'})
            for drawing in page.get_drawings():
                if drawings_count>=MAX_DRAWINGS:layout_truncated=True;break
                layout.append({'kind':'vector_region','page':number,'bbox':numbers(drawing['rect']),
                    'semantic_status':'unclassified; not evidence of a figure or table'});drawings_count+=1
            if not words:diagnostics.append({'code':'NO_EXTRACTABLE_TEXT','page':number,'severity':'information','ocr_performed':False})
            references=raw_page.get('/Annots',[])
            if hasattr(references,'get_object'):references=references.get_object()
            raw_annotations={entry.idnum:entry.get_object() for entry in references if hasattr(entry,'idnum')}
            for annotation in page.annots() or ():
                if len(annotations)>=MAX_ANNOTATIONS:annotation_truncated=True;break
                rect,vertices,vertices_truncated=raw_annotation_geometry(pm,raw_annotations.get(annotation.xref),mapping,annotation)
                regions=[]
                if annotation.type[0] in (8,9,10,11) and vertices:
                    regions=[pm.Quad(vertices[i:i+4]).rect for i in range(0,len(vertices),4) if len(vertices[i:i+4])==4]
                if not regions:regions=[rect]
                selected=[text for bbox,text,_ in valid_words if any(region.contains(pm.Rect(bbox).tl+(pm.Rect(bbox).br-pm.Rect(bbox).tl)*0.5) for region in regions)]
                approximate=False
                if not selected and valid_words:
                    center=(rect.tl+rect.br)*0.5
                    nearest=min(valid_words,key=lambda word:abs(pm.Rect(word[0]).tl.x-center.x)+abs(pm.Rect(word[0]).tl.y-center.y))
                    delta=pm.Point(nearest[0][:2])-center
                    if math.hypot(delta.x,delta.y)<72:
                        selected=[text for _,text,indices in valid_words if tuple(indices[:2])==tuple(nearest[2][:2])];approximate=True
                quote=' '.join(selected)[:2048]
                identity=f'page-{number}-xref-{annotation.xref}'
                info={k:str(v)[:4096] for k,v in annotation.info.items() if k in {'content','title','subject','id','creationDate','modDate'}}
                item={'id':identity,'page':number,'xref':annotation.xref,'type':list(annotation.type),'bbox':numbers(rect),
                    'vertices':vertices,'vertices_truncated':vertices_truncated,'info':info,'quote':quote,'quote_method':'nearby_line_heuristic' if approximate else 'geometry_word_intersection'}
                item['library_bbox']=numbers(annotation.rect)
                item['coordinate_source']='native_pdf_rect_quadpoints' if annotation.xref in raw_annotations else 'library_fallback'
                annotations.append(item)
                locations.append({'annotation_id':identity,'page':number,'bbox':numbers(rect),
                    **locate(quote,tex,pdf_hash,approximate=approximate,source_scan_truncated=source_truncated)})
    # Keep every page transform ahead of truncatable word/object records.
    layout.sort(key=lambda item:item['kind']!='page_geometry')
    sidecars={'layout':write_sidecar(work,'layout.json',layout,truncated=layout_truncated),
              'annotations':write_sidecar(work,'annotations.json',annotations,truncated=annotation_truncated),
              'source_locations':write_sidecar(work,'source_locations.json',locations,truncated=annotation_truncated or source_truncated),
              'diagnostics':write_sidecar(work,'diagnostics.json',diagnostics)}
    if sum(item['bytes'] for item in sidecars.values())>MAX_SIDECAR_BYTES:raise ValueError('Combined sidecar limit exceeded')
    return {'status':'completed','source_sha256':pdf_hash,**legacy_info,'rendered_pages':rendered,
        'visual_status':'not_reviewed','overflow_status':'not_determined_by_structural_checks',
        'backend':{'name':'pymupdf','version':str(pm.VersionBind),'mupdf_version':str(pm.VersionFitz),'license':'AGPL/commercial'},
        'thumbnails':thumbnails,'sidecars':sidecars,'source_mapping':{'binding':'unbound','synctex_used':False,'source_files':len(tex),'source_scan_truncated':source_truncated},
        'limits':{'words':MAX_WORDS,'annotations':MAX_ANNOTATIONS,'images':MAX_IMAGES,'vector_regions':MAX_DRAWINGS,'sidecar_bytes':MAX_SIDECAR_BYTES},
        'limitations':['Visual inspection remains required; diagnostics do not prove layout correctness.',
            'Textual TeX locations are unbound candidates; no verified build/SyncTeX mapping or automatic source edits.',
            'Image instances and vector regions are geometric objects, not semantic figure identities.']}
