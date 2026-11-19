"""Project-only history lookups through the existing cached paper_read_files API."""
import base64
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sqlite3

import pytest
from paper_studio.gateway.main import Gateway
from paper_studio.gateway.history_lookup import PREFIX,read_locator
from paper_studio.security.policy import StudioError,encoded,sha
from paper_studio.versions import history_objects as history
from paper_studio.capabilities import legacy_release_blocked,visible_index_tools


@pytest.fixture
def cfg(tmp_path):
    for name in ('project','artifacts','state'):(tmp_path/name).mkdir()
    return {'project_id':'test','project_root':str(tmp_path/'project'),'artifact_root':str(tmp_path/'artifacts'),
            'state_root':str(tmp_path/'state'),'native_project':'agent:test:paper','native_artifact_project':'agent:test:artifacts',
            'admission_profile':'paper-editor.v1','_config_revision':'a'*64,'writable_roots':['draft'],'readonly_roots':['builds']}


def row(cfg,path='draft/paper.pdf',data=b'%PDF-fixture',**fields):
    p=Path(cfg['project_root'])/path;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
    return dict(schema=history.SCHEMA,project_id=cfg['project_id'],version='R10',revision='archive',language='zh',
                role='manuscript_pdf',source={'kind':'project','path':path},sha256=sha(data),bytes=len(data),**fields)


def lookup(cfg,uri='paper-history://R10/~/zh/manuscript_pdf',**extra):
    return Gateway(None)._call_locked(cfg,'paper_read_files',{'project_id':cfg['project_id'],'items':[{'path':uri}],**extra})['items'][0]


def test_cached_gateway_read_has_no_target_index_side_effect_and_no_native(cfg,monkeypatch):
    r=row(cfg);history.import_rows(cfg,[r],request_id='r1')
    monkeypatch.setattr(Gateway,'client',lambda *_:pytest.fail('history reads must not invoke generic Native routes'))
    value=lookup(cfg)
    assert value['status']=='found' and value['sha256_verified'] and value['sha256']==r['sha256']
    assert value['lookup_io']['directory_enumeration_attempts']==0
    chunk=lookup(cfg,'paper-history://R10/archive/zh/manuscript_pdf/content/2/4')
    assert base64.b64decode(chunk['chunk_base64'])==b'DF-f'
    assert not legacy_release_blocked(cfg,'paper_version_save',{}) and not visible_index_tools(cfg)
    assert 'target_index' not in cfg


def test_ambiguity_explicit_and_object_id_selects_only_same_selector(cfg):
    a=row(cfg,'draft/a.pdf');b=row(cfg,'draft/b.pdf',data=b'other')
    imported=history.import_rows(cfg,[a,b],request_id='r1')
    result=lookup(cfg);assert result['status']=='ambiguous' and len(result['candidates'])==2
    assert result['candidate_bytes_verified'] is False
    found=lookup(cfg,'paper-history://R10/~/zh/manuscript_pdf/object/'+imported['objects'][0])
    assert found['sha256']==a['sha256']
    assert lookup(cfg,'paper-history://R10/~/en/manuscript_pdf/object/'+imported['objects'][0])['status']=='not_found'
    assert lookup(cfg,'paper-history://R11/~/zh/manuscript_pdf')['status']=='not_found'


def test_numeric_named_and_unknown_are_not_guessed(cfg):
    a=row(cfg,'draft/figure.png',data=b'PNG',figure_number=12);a.update(role='figure_image')
    b=row(cfg,'draft/unknown.png',data=b'UNKNOWN',target_key='path-a');b.update(role='figure_image',language='unknown')
    history.import_rows(cfg,[a,b],request_id='r1')
    assert lookup(cfg,'paper-history://R10/~/zh/figure_image/12')['status']=='found'
    assert lookup(cfg,'paper-history://R10/~/en/figure_image/12')['status']=='not_found'
    assert lookup(cfg,'paper-history://R10/~/unknown/figure_image/key/path-a')['status']=='found'


def test_source_and_manifest_tampering_rejected(cfg):
    r=row(cfg);history.import_rows(cfg,[r],request_id='r1')
    (Path(cfg['project_root'])/r['source']['path']).write_bytes(b'changed')
    with pytest.raises(StudioError,match='bytes differ'):lookup(cfg)


def version_row(cfg,member='builds/rb_a/output/main.pdf'):
    root=Path(cfg['artifact_root'])/'versions/R10';root.mkdir(parents=True)
    p=root/member;p.parent.mkdir(parents=True);p.write_bytes(b'archive')
    manifest={'schema':'paper-studio.version.v1','project_id':cfg['project_id'],'version':'R10',
              'files':[{'path':member,'bytes':7,'sha256':sha(b'archive')}]}
    (root/'manifest.json').write_bytes(encoded(manifest))
    return dict(schema=history.SCHEMA,project_id=cfg['project_id'],version='R10',revision='archive',language='en',
                role='manuscript_pdf',source={'kind':'version','version':'R10','path':member},sha256=sha(b'archive'),bytes=7)


def test_exact_archive_membership_not_whole_archive_scan(cfg):
    r=version_row(cfg);history.import_rows(cfg,[r],request_id='r1')
    value=lookup(cfg,'paper-history://R10/~/en/manuscript_pdf')
    assert value['status']=='found' and value['lookup_io']['directory_enumeration_attempts']==0
    root=Path(cfg['artifact_root'])/'versions/R10'
    (root/'manifest.json').write_bytes((root/'manifest.json').read_bytes()+b' ')
    with pytest.raises(StudioError,match='manifest identity'):lookup(cfg,'paper-history://R10/~/en/manuscript_pdf')


def test_import_wrong_archive_project_or_unregistered_member_rejected(cfg):
    r=version_row(cfg)
    wrong=dict(r);wrong['source']={**r['source'],'path':'unregistered.pdf'}
    with pytest.raises(StudioError,match='exactly once'):history.import_rows(cfg,[wrong],request_id='r1')
    p=Path(cfg['artifact_root'])/'versions/R10/manifest.json'
    m=json.loads(p.read_text());m['project_id']='other';p.write_bytes(encoded(m))
    with pytest.raises(StudioError) as err:history.import_rows(cfg,[r],request_id='r1')
    assert err.value.code=='PROJECT_DENIED'


def test_actual_build_output_and_frozen_source_members(cfg):
    bid='rb_'+'a'*24;root=Path(cfg['artifact_root'])/'builds'/bid;root.mkdir(parents=True)
    rows=[]
    for prefix,name,content in [('source','main.tex',b'tex'),('output','main.pdf',b'pdf')]:
        p=root/prefix/name;p.parent.mkdir();p.write_bytes(content)
        rows.append(dict(schema=history.SCHEMA,project_id=cfg['project_id'],version=bid,revision='archive',language='en',
                         role='manuscript_pdf' if prefix=='output' else 'manuscript_tex',source={'kind':'build','build_id':bid,'path':prefix+'/'+name},sha256=sha(content),bytes=3))
    (root/'request.json').write_bytes(encoded({'schema':'paper-studio.build-request.v1','project_id':cfg['project_id'],'build_id':bid,'snapshot':{'files':[{'path':'main.tex','sha256':sha(b'tex'),'bytes':3}]}}))
    (root/'manifest.json').write_bytes(encoded({'schema':'paper-studio.build-manifest.v1','project_id':cfg['project_id'],'build_id':bid,'artifacts':[{'path':'main.pdf','sha256':sha(b'pdf'),'bytes':3}]}))
    history.import_rows(cfg,rows,request_id='r1')
    assert lookup(cfg,f'paper-history://{bid}/~/en/manuscript_pdf')['status']=='found'
    assert lookup(cfg,f'paper-history://{bid}/~/en/manuscript_tex')['status']=='found'


def test_local_receipt_exact_id_hash_size_no_remote_claim(cfg):
    r=row(cfg);root=Path(cfg['project_root']);receipt={'files':[{'file_id':'drive123','sha256':r['sha256'],'bytes':r['bytes']}]}
    raw=encoded(receipt);(root/'receipt.json').write_bytes(raw)
    r['drive_ref']={'file_id':'drive123','receipt_source':{'kind':'project','path':'receipt.json'},'receipt_sha256':sha(raw),
                    'entry_pointer':'/files/0','id_pointer':'/file_id','sha256_pointer':'/sha256','bytes_pointer':'/bytes'}
    history.import_rows(cfg,[r],request_id='r1')
    drive=lookup(cfg)['drive_receipt']
    assert drive['file_id']=='drive123' and drive['remote_verification']=='not_probed'
    (root/'receipt.json').write_bytes(raw+b' ')
    with pytest.raises(StudioError):lookup(cfg)


def test_import_is_atomic_idempotent_and_conflicting_request_fails(cfg):
    r=row(cfg);a=history.import_rows(cfg,[r],request_id='r1')
    assert history.import_rows(cfg,[r],request_id='r1')['idempotent_replay'] is True
    bad=row(cfg,'draft/bad.pdf',data=b'new');bad['sha256']='f'*64
    with pytest.raises(StudioError):history.import_rows(cfg,[r,bad],request_id='r2')
    b=row(cfg,'draft/new.pdf',data=b'new')
    with pytest.raises(StudioError,match='Request ID'):history.import_rows(cfg,[b],request_id='r1')
    assert lookup(cfg)['object_id']==a['objects'][0]


def test_concurrent_append_and_readonly_no_db_writes(cfg):
    rows=[row(cfg,f'draft/{n}.pdf',data=str(n).encode(),target_key=f'k{n}') for n in range(4)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        values=list(pool.map(lambda pair:history.import_rows(cfg,[pair[1]],request_id='r'+str(pair[0])),enumerate(rows)))
    assert all(v['imported']==1 for v in values)
    p=history.path(cfg);before=p.stat().st_mtime_ns
    assert lookup(cfg)['status']=='ambiguous'
    assert p.stat().st_mtime_ns==before
    assert p.stat().st_mode&0o077==0


def test_project_binding_db_permissions_and_symlink_guard(cfg,tmp_path):
    r=row(cfg);history.import_rows(cfg,[r],request_id='r1')
    with pytest.raises(StudioError) as e:lookup({**cfg,'project_id':'other'})
    assert e.value.code=='PROJECT_DENIED'
    p=history.path(cfg);os.chmod(p,0o644)
    with pytest.raises(StudioError,match='private'):lookup(cfg)
    os.chmod(p,0o600)
    file=Path(cfg['project_root'])/r['source']['path'];file.unlink();file.symlink_to(tmp_path/'outside')
    with pytest.raises(StudioError):lookup(cfg)


@pytest.mark.parametrize('uri',['paper-history://../~/zh/manuscript_pdf','paper-history://R10/~/zh/manuscript_pdf/../x','paper-history://R10/~/zh/manuscript_pdf?global=true'])
def test_bad_uris_never_native_fallback(cfg,uri):
    with pytest.raises(StudioError):lookup(cfg,uri)


def test_budget_and_mixed_locator_validation_happen_before_reads(cfg):
    for items in [[{'path':'paper-history://R10/~/zh/manuscript_pdf'},{'path':'PROJECT.yaml'}],
                  [{'path':'paper-history://R10/~/zh/manuscript_pdf','start_line':1}],
                  [{'path':'paper-history://R10/~/zh/manuscript_pdf/content/0/100000'}]]:
        with pytest.raises(StudioError):read_locator(cfg,{'items':items,'max_result_bytes':1024})
    assert not history.path(cfg).exists()


def test_no_directory_enumeration_even_if_reader_regresses(cfg,monkeypatch):
    monkeypatch.setattr(history,'resolve',lambda cfg,**kw:os.listdir(cfg['project_root']))
    with pytest.raises(StudioError) as error:read_locator(cfg,{'items':[{'path':'paper-history://R10/~/zh/manuscript_pdf'}]})
    assert error.value.code=='INDEX_SCAN_DENIED'


def test_db_document_identity_corruption_is_not_silently_reinterpreted(cfg):
    r=row(cfg);history.import_rows(cfg,[r],request_id='r1')
    with sqlite3.connect(history.path(cfg)) as db:
        old=db.execute('SELECT object_id,document FROM objects').fetchone();changed=json.loads(old[1]);changed['source']['path']='draft/other.pdf'
        db.execute('UPDATE objects SET document=? WHERE object_id=?',(json.dumps(changed),old[0]))
    with pytest.raises(StudioError) as err:lookup(cfg)
    assert err.value.code=='HISTORY_DATABASE_INVALID'


@pytest.mark.parametrize('bad', [{'language':{}},{'role':[]},{'provenance':float('nan')}])
def test_malformed_import_rows_have_structured_errors(cfg,bad):
    r=row(cfg);r.update(bad)
    with pytest.raises(StudioError):history.import_rows(cfg,[r],request_id='r1')


def test_frozen_archive_version_cannot_be_mislabeled(cfg):
    r=version_row(cfg);r['version']='R11'
    with pytest.raises(StudioError,match='exact archive'):history.import_rows(cfg,[r],request_id='r1')


def test_catalogue_parent_must_also_be_private(cfg):
    r=row(cfg);history.import_rows(cfg,[r],request_id='r1')
    os.chmod(history.path(cfg).parent,0o755)
    with pytest.raises(StudioError,match='directory must be private'):lookup(cfg)


def test_public_local_cli_import_and_cached_uri_read(cfg,tmp_path):
    import subprocess,sys
    r=row(cfg)
    config={**cfg,'schema_version':1,'software_id':'webcodex-paper-studio','instance_id':'test','client_id':'test'}
    cp=tmp_path/'cfg.json';cp.write_text(json.dumps(config))
    source=tmp_path/'objects.jsonl';source.write_text(json.dumps(r)+'\n')
    command=[sys.executable,str(Path(__file__).resolve().parents[1]/'management/paper_history_index.py'),'--config',str(cp),'--project-id','test']
    denied=subprocess.run(command+['import-jsonl','--input',str(source),'--request-id','r1'],capture_output=True,text=True)
    assert denied.returncode==2 and json.loads(denied.stdout)['error']['code']=='AUTHORIZATION_REQUIRED'
    done=subprocess.run(command+['import-jsonl','--input',str(source),'--request-id','r1','--authorized'],capture_output=True,text=True)
    assert done.returncode==0,done.stderr+done.stdout
    got=subprocess.run(command+['get','--uri','paper-history://R10/~/zh/manuscript_pdf'],capture_output=True,text=True)
    assert got.returncode==0,got.stderr+got.stdout
    assert json.loads(got.stdout)['items'][0]['status']=='found'


def test_readonly_cli_route_respects_original_tool_admission(cfg):
    r=row(cfg);history.import_rows(cfg,[r],request_id='r1')
    with pytest.raises(StudioError,match='not admitted'):
        read_locator({**cfg,'admitted_tools':[]},{'items':[{'path':'paper-history://R10/~/zh/manuscript_pdf'}]})
