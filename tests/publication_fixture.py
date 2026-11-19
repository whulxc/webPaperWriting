"""Source-bound protocol fixtures used by formal-publication unit tests."""
import mimetypes
from pathlib import Path

from paper_studio.security.policy import encoded, sha


def write_evidence(job, cfg, request, snapshot, *, pdf_path, pages=2):
    raw_request={key:value for key,value in request.items() if not key.startswith('_')}
    prepared={**request,'_input_manifest_sha256':snapshot['sha256']}
    rows=snapshot['files'];main=prepared['tex_path']
    prepared['expected_revision']=next(row['sha256'] for row in rows if row['path']==main)
    record={'schema':'paper-studio.build-request.v1','project_id':cfg['project_id'],'build_id':job.name,
            'kind':'tex','request':prepared,'raw_request':raw_request,
            'prepared_request_sha256':sha(encoded(prepared)),
            'request_sha256':sha(encoded({'kind':'tex','request':raw_request,'project_id':cfg['project_id']})),
            'snapshot_complete':True,'snapshot':snapshot,'config_revision':'fixture','code_sha256':'fixture'}
    artifacts=[{'path':p.relative_to(job/'output').as_posix(),'bytes':p.stat().st_size,
                'sha256':sha(p.read_bytes()),'mime_type':mimetypes.guess_type(p.name)[0] or 'application/octet-stream'}
               for p in sorted((job/'output').rglob('*')) if p.is_file()]
    pdf=next(row for row in artifacts if row['path']==pdf_path)
    result={'status':'completed','pdf_path':pdf_path,'pdf_sha256':pdf['sha256'],'pages':pages,
            'execution':{'status':'completed','program':'latex','returncode':0,
                         'isolation':{'input':'read-only','host_fallback':False}},
            'source_binding':{'tex_path':main,'main_sha256':prepared['expected_revision'],
                'dependencies_sha256':snapshot['sha256'],'snapshot_sha256':snapshot['sha256'],'dependencies':rows}}
    manifest={'schema':'paper-studio.build-manifest.v1','project_id':cfg['project_id'],'build_id':job.name,
              'kind':'tex','request_sha256':record['request_sha256'],'config_revision':'fixture','code_sha256':'fixture',
              'input':snapshot,'artifacts':artifacts,'result':result}
    status={'project_id':cfg['project_id'],'build_id':job.name,'kind':'tex','status':'completed','generated':True,
            'manifest_sha256':sha(encoded(manifest)),'snapshot_sha256':snapshot['sha256'],'artifacts':artifacts,'result':result}
    for name,value in [('request.json',record),('manifest.json',manifest),('status.json',status)]:
        (job/name).write_bytes(encoded(value))
    return record, manifest, status
