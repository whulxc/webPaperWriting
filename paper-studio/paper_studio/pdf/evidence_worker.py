"""Bounded PDF text extraction inside the existing registered PDF sandbox."""
import hashlib,io,json,math
from pathlib import Path

MAX_PDF_BYTES=32*1024*1024
MAX_PAGES=60


class EvidenceError(Exception):
    def __init__(self,code,message):self.code=code;self.message=message;super().__init__(message)


def fail(code,message):raise EvidenceError(code,message)
def digest(raw):return hashlib.sha256(raw).hexdigest()
def text_digest(pages):
    return digest(json.dumps([{'page':page['page'],'text':page['text']} for page in pages],ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8'))


def extract_pdf(path,request):
    pages=request.get('pages');max_chars=request.get('max_chars',8000);limit=request.get('_max_pdf_bytes',MAX_PDF_BYTES)
    if not isinstance(pages,list) or not 1<=len(pages)<=4 or any(type(page) is not int or page<1 for page in pages) or len(set(pages))!=len(pages):fail('EVIDENCE_PAGE_RANGE','One to four unique positive page numbers required')
    if type(max_chars) is not int or not 1<=max_chars<=20000:fail('EVIDENCE_TEXT_LIMIT','max_chars must be between one and 20000')
    if type(limit) is not int or not 1<=limit<=MAX_PDF_BYTES:fail('EVIDENCE_INPUT_LIMIT','PDF byte limit exceeds the trusted maximum')
    path=Path(path)
    if not path.is_file() or path.stat().st_size>limit:fail('EVIDENCE_INPUT_LIMIT','PDF input exceeds the configured byte limit')
    raw=path.read_bytes()
    if len(raw)>limit:fail('EVIDENCE_INPUT_LIMIT','PDF input exceeds the configured byte limit')
    identity=digest(raw)
    if identity!=request.get('_expected_sha256'):fail('REVISION_CONFLICT','Immutable PDF input differs from the approved SHA256')
    try:
        from pypdf import PdfReader
        reader=PdfReader(io.BytesIO(raw),strict=True)
        if reader.is_encrypted:fail('EVIDENCE_PDF_ENCRYPTED','Encrypted PDFs are not supported')
        count=len(reader.pages)
        if not 1<=count<=MAX_PAGES:fail('EVIDENCE_PAGE_LIMIT','PDF exceeds the existing 60-page parser limit')
        if any(number>count for number in pages):fail('EVIDENCE_PAGE_RANGE','A requested page is outside the actual PDF page count')
        remaining=max_chars;output=[]
        for number in pages:
            page=reader.pages[number-1];unit=float(page.get('/UserUnit',1));width=float(page.mediabox.width)*unit;height=float(page.mediabox.height)*unit
            if not all(math.isfinite(value) for value in [unit,width,height]) or min(unit,width,height)<=0 or max(width,height)>1200:fail('EVIDENCE_PAGE_SIZE','Selected PDF page physical dimensions exceed the existing limit')
            text=page.extract_text() or ''
            if not isinstance(text,str):fail('EVIDENCE_EXTRACTION_FAILED','PDF text extractor returned a non-text result')
            returned=text[:remaining];remaining-=len(returned)
            output.append({'page':number,'text':returned,'text_sha256':digest(returned.encode('utf-8')),
                           'extracted_text_sha256':digest(text.encode('utf-8')),'extracted_characters':len(text),
                           'returned_characters':len(returned),'truncated':len(returned)<len(text)})
    except EvidenceError:raise
    except Exception:fail('EVIDENCE_PDF_INVALID','The strict PDF parser could not extract text from this input')
    return {'schema':'paper-studio.pdf-evidence.v1','source_sha256':identity,'source_bytes':len(raw),'page_count':count,
            'pages':output,'max_chars':max_chars,'total_returned_characters':max_chars-remaining,'text_sha256':text_digest(output),
            'text_digest_format':'SHA256 of UTF-8 compact sorted-key JSON [{page,text}] in requested page order',
            'scientific_support':'unverified','claim_support':'unverified','visual_review':'not_performed','ocr_performed':False,
            'limitations':['Extracted text is not evidence that a scientific claim is supported.',
                           'No OCR, citation verification or page rendering is performed; scanned pages may return empty text.']}
