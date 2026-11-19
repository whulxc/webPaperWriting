"""Hash-bound read-only PDF evidence facade; no rb or manuscript build record."""
import json,re,uuid
from pathlib import Path
from paper_studio.security.policy import StudioError,atomic_json,fail,no_links,path_for,project,safe_read,safe_write_new,sha
from paper_studio.security.sandbox import SandboxUserBusError,run_sandbox
from paper_studio.pdf.evidence_worker import MAX_PDF_BYTES,MAX_PAGES,text_digest


def dispatch(cfg,args):
    project(cfg,args.get('project_id'))
    policy=cfg.get('evidence_reader')
    if not isinstance(policy,dict) or policy.get('enabled') is not True:fail('EVIDENCE_READER_DISABLED','This project has not enabled bounded PDF evidence reads')
    limit=policy.get('max_pdf_bytes',MAX_PDF_BYTES)
    if type(limit) is not int or not 1<=limit<=MAX_PDF_BYTES:fail('CONFIG_INVALID','Trusted PDF read byte limit must not exceed 32 MiB')
    path=args.get('pdf_path');path_for(cfg,path)
    if not path.lower().endswith('.pdf'):fail('INVALID_ARGUMENT','pdf_path must identify a project-relative PDF')
    expected=args.get('expected_sha256')
    if not isinstance(expected,str) or not re.fullmatch('[0-9a-f]{64}',expected):fail('REVISION_REQUIRED','expected_sha256 must identify the actual source PDF bytes')
    pages=args.get('pages');max_chars=args.get('max_chars',8000)
    if not isinstance(pages,list) or not 1<=len(pages)<=4 or any(type(page) is not int or page<1 for page in pages) or len(set(pages))!=len(pages):fail('EVIDENCE_PAGE_RANGE','Select one to four distinct positive page numbers')
    if type(max_chars) is not int or not 1<=max_chars<=20000:fail('EVIDENCE_TEXT_LIMIT','max_chars is a total returned-text character budget of one to 20000')
    raw=safe_read(cfg['project_root'],path,limit);identity=sha(raw)
    if identity!=expected:fail('REVISION_CONFLICT','Source PDF differs from the expected SHA256',actual_sha256=identity)
    read_id='er_'+uuid.uuid4().hex
    root=no_links(Path(cfg['artifact_root'])/'evidence-reads'/read_id);snapshot=root/'input';output=root/'output'
    root.mkdir(mode=0o700,parents=True)
    audit={'schema':'paper-studio.pdf-evidence-attempt.v1','read_id':read_id,'project_id':cfg['project_id'],'pdf_path':path,
           'source_sha256':identity,'source_bytes':len(raw),'pages':pages,'max_chars':max_chars}
    atomic_json(root/'attempt.json',audit,True)
    try:
        snapshot.mkdir(mode=0o700)
        safe_write_new(snapshot,'source.pdf',raw);(snapshot/'source.pdf').chmod(0o400)
        request={'pdf_path':'source.pdf','pages':pages,'max_chars':max_chars,'_evidence_only':True,
                 '_expected_sha256':identity,'_max_pdf_bytes':limit}
        trusted={**cfg,'limits':{**cfg.get('limits',{}),'timeout_seconds':min(cfg.get('limits',{}).get('timeout_seconds',45),45),
                               'max_output_bytes':2*1024*1024,'max_files':16}}
        run_sandbox(snapshot,output,'pdf',request,trusted)
        result=json.loads(safe_read(output,'evidence.json',256*1024))
        returned=result.get('pages',[])
        if (result.get('source_sha256')!=identity or result.get('source_bytes')!=len(raw)
            or type(result.get('page_count')) is not int or not max(pages)<=result['page_count']<=MAX_PAGES
            or not isinstance(returned,list) or [item.get('page') for item in returned]!=pages):fail('EVIDENCE_OUTPUT_INVALID','Sandbox PDF evidence identities differ from the approved request')
        count=0;clean=[]
        for item in returned:
            text=item.get('text')
            if (not isinstance(text,str) or item.get('text_sha256')!=sha(text.encode('utf-8'))
                or item.get('returned_characters')!=len(text) or type(item.get('extracted_characters')) is not int
                or item['extracted_characters']<len(text) or type(item.get('truncated')) is not bool
                or item['truncated']!=(item['extracted_characters']>len(text))
                or not isinstance(item.get('extracted_text_sha256'),str)
                or not re.fullmatch('[0-9a-f]{64}',item['extracted_text_sha256'])):fail('EVIDENCE_OUTPUT_INVALID','Sandbox page text has an invalid bounded identity')
            count+=len(text)
            clean.append({key:item[key] for key in ['page','text','text_sha256','extracted_text_sha256','extracted_characters','returned_characters','truncated']})
        if count>max_chars or result.get('total_returned_characters')!=count or result.get('text_sha256')!=text_digest(clean):fail('EVIDENCE_OUTPUT_INVALID','Sandbox result exceeded its total text budget or text digest')
        if sha(safe_read(snapshot,'source.pdf',limit))!=identity:fail('REVISION_CONFLICT','PDF input snapshot changed during evidence extraction')
        value={**audit,'schema':'paper-studio.pdf-evidence.v1','page_count':result['page_count'],'pages':clean,
               'total_returned_characters':count,'text_sha256':result['text_sha256'],'text_digest_format':result.get('text_digest_format'),
               'scientific_support':'unverified','claim_support':'unverified','visual_review':'not_performed','ocr_performed':False,
               'limitations':['Extracted PDF text is not scientific claim-support verification.','Only the selected pages were extracted; no OCR or rendering was performed.'],
               'read_only':True}
        atomic_json(root/'result.json',{'status':'completed',**{key:value[key] for key in ['read_id','source_sha256','text_sha256','page_count','total_returned_characters']}},True)
        return value
    except Exception as error:
        if isinstance(error,StudioError):detail=error.as_dict()
        elif isinstance(error,SandboxUserBusError):
            detail={'code':'EVIDENCE_USER_BUS_UNAVAILABLE','message':'A verified current-user session bus is required for the isolated PDF reader; host fallback is disabled'}
        else:
            detail={'code':'EVIDENCE_READ_FAILED','message':'The isolated PDF evidence reader failed; inspect its retained audit'}
            try:
                worker_error=json.loads(safe_read(output,'evidence-error.json',4096))
                if (isinstance(worker_error,dict) and isinstance(worker_error.get('code'),str)
                    and re.fullmatch('[A-Z_]{1,64}',worker_error['code']) and isinstance(worker_error.get('message'),str) and len(worker_error['message'])<=256):detail=worker_error
            except (StudioError,ValueError,OSError):pass
        atomic_json(root/'result.json',{'status':'failed','read_id':read_id,'source_sha256':identity,'error':detail},True)
        fail(detail['code'],detail['message'],read_id=read_id)
