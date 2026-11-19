"""Evidence-bearing append/replay/discovery on isolated research fixtures."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from paper_studio.bootstrap import initialize, BootstrapError
from paper_studio.bootstrap.handoff import create_handoff, discover_handoff, INPUT_SCHEMA, INDEX_PATH
from paper_studio.evidence.index import context, inventory
from paper_studio.security.policy import StudioError


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def paper(tmp_path):
    root=tmp_path/'Research'; root.mkdir(); (root/'results').mkdir()
    (root/'results/summary.json').write_text('{"run_id":"fixture-r01","metrics":{"accuracy":0.73}}\n')
    initialize(str(root),'fixture-paper',sources=['results/summary.json'])
    paper=root/'PaperWriting'
    (paper/'manuscript/main.tex').write_text('Human method and results remain unchanged.\n')
    (paper/'HANDOFF.md').write_text('Human handoff: preserve exact bytes.\n')
    return root,paper


def spec(root, change_id='r01'):
    return {'schema':INPUT_SCHEMA,'project_id':'fixture-paper','change_id':change_id,
            'title':'Synthetic experiment handoff','summary':'Synthetic fixture only; no scientific acceptance.',
            'sources':[{'path':'results/summary.json','sha256':digest(root/'results/summary.json'),'role':'experiment_summary'}],
            'evidence':[{'id':'E1','kind':'measured_fact','statement':'The recorded synthetic accuracy is available at the pinned JSON location.',
                         'sources':[{'path':'results/summary.json','locator':{'json_pointer':'/metrics/accuracy'}}]}],
            'impacts':{'claims':[{'id':'C1','path':'manuscript/main.tex','reason':'Reassess the result claim against this new run.'}],
                       'figures':[{'id':'fig01','path':'figures/candidates/fig01.svg','reason':'New candidate will be derived separately.'}],
                       'sections':[{'id':'methods','path':'manuscript/main.tex','anchor':'Methods','reason':'Explain the evaluation unit.'}]}}


def append(root,paper,value=None,previous='absent'):
    return create_handoff(project_root=str(root),paper_root=str(paper),project_id='fixture-paper',
                          spec=value or spec(root),expected_previous=previous)


def hashes(root):
    return {str(p.relative_to(root)):digest(p) for p in root.rglob('*') if p.is_file()}


def test_handoff_copies_real_evidence_and_preserves_human_files(paper):
    root,p=paper; protected={rel:(p/rel).read_bytes() for rel in ['PROJECT.yaml','PROJECT_STATE.json','HANDOFF.md','manuscript/main.tex']}
    source_before=(root/'results/summary.json').read_bytes();result=append(root,p)
    manifest=json.loads(Path(result['manifest_path']).read_text())
    assert manifest['evidence'][0]['sources'][0]['excerpt']=='0.73'
    assert manifest['previous'] is None and manifest['sequence']==1
    assert manifest['scientific_acceptance']=='not_inferred'
    assert manifest['impacts']['figures'][0]['status']=='planned_missing'
    assert manifest['impacts']['sections'][0]['observed_sha256']==digest(p/'manuscript/main.tex')
    assert (p/'materials/handoffs/r01/sources/results/summary.json').read_bytes()==source_before
    assert digest(Path(result['manifest_path']))==result['material_revision']
    assert '0.73' in Path(result['chat_entry_path']).read_text()
    for rel,raw in protected.items():assert (p/rel).read_bytes()==raw
    assert (root/'results/summary.json').read_bytes()==source_before
    assert result['cloud_operations']==0 and not result['manuscript_modified']


def test_two_revisions_chain_and_replay_do_not_regress_latest(paper):
    root,p=paper;first_spec=spec(root);first=append(root,p,first_spec);first_hashes=hashes(p/'materials/handoffs/r01')
    (root/'results/summary.json').write_text('{"run_id":"fixture-r02","metrics":{"accuracy":0.78}}\n')
    second=append(root,p,spec(root,'r02'),first['material_revision'])
    latest=discover_handoff(p,'fixture-paper')
    assert latest['material_revision']==second['material_revision'] and latest['previous_material_revision']==first['material_revision']
    assert latest['evidence'][0]['sources'][0]['excerpt']=='0.78'
    assert hashes(p/'materials/handoffs/r01')==first_hashes
    before=hashes(p);replay=append(root,p,first_spec)
    assert replay['replayed'] and replay['material_revision']==first['material_revision']
    assert hashes(p)==before
    assert discover_handoff(p,'fixture-paper')['material_revision']==second['material_revision']


def test_predecessor_fence_and_change_id_conflicts_leave_bytes_untouched(paper):
    root,p=paper;append(root,p);before=hashes(p)
    with pytest.raises(BootstrapError) as error:append(root,p,spec(root,'r02'),'0'*64)
    assert error.value.code=='HANDOFF_PREDECESSOR_CONFLICT' and hashes(p)==before
    changed=spec(root);changed['summary']='Different input cannot replace the same revision.'
    with pytest.raises(BootstrapError) as error:append(root,p,changed)
    assert error.value.code=='HANDOFF_IDEMPOTENCY_CONFLICT' and hashes(p)==before


@pytest.mark.parametrize('path',['../outside.json','raw/summary.json','credentials/note.json','results/model.pt'])
def test_forbidden_source_paths_never_copy(paper,path):
    root,p=paper;value=spec(root);value['sources'][0]['path']=path;value['evidence'][0]['sources'][0]['path']=path
    before=hashes(p)
    with pytest.raises(BootstrapError):append(root,p,value)
    assert hashes(p)==before


def test_source_identity_and_locator_are_checked_before_commit(paper):
    root,p=paper;value=spec(root);value['sources'][0]['sha256']='0'*64
    with pytest.raises(BootstrapError) as error:append(root,p,value)
    assert error.value.code=='HANDOFF_SOURCE_CHANGED'
    assert not (p/INDEX_PATH).exists()
    value=spec(root);value['evidence'][0]['sources'][0]['locator']={'json_pointer':'/unknown'}
    with pytest.raises(BootstrapError) as error:append(root,p,value)
    assert error.value.code=='HANDOFF_LOCATOR_INVALID' and not (p/INDEX_PATH).exists()


def test_csv_and_line_excerpts_are_actual_pinned_content(paper):
    root,p=paper;(root/'results/metrics.csv').write_text('method,f1\nbaseline,0.71\ncandidate,0.76\n')
    value=spec(root);value['sources'].append({'path':'results/metrics.csv','sha256':digest(root/'results/metrics.csv'),'role':'aggregate_table'})
    value['evidence'][0]['sources']=[{'path':'results/metrics.csv','locator':{'row':2,'column':'f1'}},
                                     {'path':'results/metrics.csv','locator':{'start_line':1,'end_line':2}}]
    append(root,p,value);latest=discover_handoff(p,'fixture-paper')
    assert [r['excerpt'] for r in latest['evidence'][0]['sources']]==['0.76','method,f1\nbaseline,0.71']


def test_tampered_snapshot_or_index_is_rejected(paper):
    root,p=paper;append(root,p)
    (p/'materials/handoffs/r01/sources/results/summary.json').write_text('modified')
    with pytest.raises(BootstrapError) as error:discover_handoff(p,'fixture-paper')
    assert error.value.code=='HANDOFF_TAMPERED'


def test_context_reports_invalid_frozen_evidence_without_losing_entrypoints(paper):
    root,p=paper;append(root,p)
    (p/'materials/handoffs/r01/sources/results/summary.json').write_text('modified')
    cfg={'project_id':'fixture-paper','project_root':str(p),'artifact_root':str(root/'artifacts'),
         'writable_roots':['manuscript'],'readonly_roots':['materials'],'_config_revision':'fixture'}
    found=context(cfg)
    assert found['latest_material_handoff']['status']=='invalid'
    assert found['latest_material_handoff']['error_code']=='HANDOFF_TAMPERED'
    assert 'sync/handoff-index.json' in found['entrypoints']


def test_private_impact_paths_and_different_research_root_are_rejected(paper,tmp_path):
    root,p=paper;value=spec(root);value['impacts']['claims'][0]['path']='credentials/note.json'
    with pytest.raises(BootstrapError) as error:append(root,p,value)
    assert error.value.code=='HANDOFF_IMPACT_DENIED'
    other=tmp_path/'Other';other.mkdir()
    with pytest.raises(BootstrapError) as error:append(other,p,spec(root))
    assert error.value.code=='HANDOFF_PROJECT_MISMATCH'


def test_pointer_commit_failure_recovers_exact_orphan_without_rewriting(paper,monkeypatch):
    root,p=paper;value=spec(root)
    from paper_studio.bootstrap import handoff
    atomic=handoff._atomic
    monkeypatch.setattr(handoff,'_atomic',lambda *args: (_ for _ in ()).throw(OSError('fixture pointer failure')))
    with pytest.raises(OSError):append(root,p,value)
    frozen=hashes(p/'materials/handoffs/r01')
    assert not (p/INDEX_PATH).exists()
    monkeypatch.setattr(handoff,'_atomic',atomic)
    result=append(root,p,value)
    assert discover_handoff(p,'fixture-paper')['material_revision']==result['material_revision']
    assert hashes(p/'materials/handoffs/r01')==frozen


def test_big_project_context_still_discovers_latest_material_and_entrypoints(paper):
    root,p=paper;result=append(root,p)
    (p/'many').mkdir()
    for i in range(20):(p/'many'/('note-'+str(i)+'.txt')).write_text('unclassified note')
    cfg={'project_id':'fixture-paper','project_root':str(p),'artifact_root':str(root/'artifacts'),
         'writable_roots':['manuscript'],'readonly_roots':['materials','builds'],'_config_revision':'fixture','limits':{'max_files':10}}
    with pytest.raises(StudioError) as error:inventory(cfg)
    assert error.value.code=='INPUT_LIMIT'
    found=context(cfg)
    assert found['inventory']['truncated'] is True and found['inventory']['total'] is None
    assert found['latest_material_handoff']['material_revision']==result['material_revision']
    assert found['latest_material_handoff']['evidence'][0]['sources'][0]['excerpt']=='0.73'
    assert 'sync/handoff-index.json' in found['entrypoints']
    assert found['latest_material_handoff']['manifest_relative_path'].startswith('materials/handoffs/')


def test_context_hashes_have_aggregate_budget(paper):
    root,p=paper
    (p/'large.txt').write_bytes(b'x'*(16*1024*1024+1))
    cfg={'project_id':'fixture-paper','project_root':str(p),'artifact_root':str(root/'artifacts'),
         'writable_roots':['manuscript'],'readonly_roots':['materials'],'_config_revision':'fixture'}
    found=context(cfg)
    row=next(row for row in found['inventory']['files'] if row['path']=='large.txt')
    assert row['sha256'] is None and row['hash_status']=='not_read_context_budget'


def test_handoff_cli_is_callable_from_an_external_clean_project(paper,tmp_path):
    root,p=paper;path=tmp_path/'spec.json';path.write_text(json.dumps(spec(root)))
    result=subprocess.run([sys.executable,'-B','-m','paper_studio.bootstrap.handoff','--project-root',str(root),
        '--paper-root',str(p),'--project-id','fixture-paper','--spec',str(path),'--expected-previous','absent'],
        capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr
    data=json.loads(result.stdout);assert data['ok'] and Path(data['result']['manifest_path']).is_file()


def test_single_initialization_cli_can_also_publish_first_local_handoff(tmp_path):
    root=tmp_path/'clean';root.mkdir();(root/'results').mkdir();(root/'results/summary.json').write_text('{"metrics":{"accuracy":0.73}}')
    value=spec(root);path=tmp_path/'spec.json';path.write_text(json.dumps(value))
    result=subprocess.run([sys.executable,'-B','-m','paper_studio.bootstrap','--project-root',str(root),
        '--project-id','fixture-paper','--source','results/summary.json','--handoff-spec',str(path),
        '--expected-previous','absent'],capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr
    data=json.loads(result.stdout);assert data['result']['material_handoff']['material_revision']
    assert (root/'PaperWriting/sync/handoff-index.json').is_file()


def test_invalid_first_handoff_spec_does_not_create_initializer_files(tmp_path):
    root=tmp_path/'clean';root.mkdir();path=tmp_path/'invalid.json';path.write_text('{}')
    result=subprocess.run([sys.executable,'-B','-m','paper_studio.bootstrap','--project-root',str(root),
        '--project-id','fixture-paper','--handoff-spec',str(path),'--expected-previous','absent'],
        capture_output=True,text=True,timeout=30)
    assert result.returncode==1 and json.loads(result.stdout)['error']['code']=='HANDOFF_INPUT_INVALID'
    assert list(root.iterdir())==[]
