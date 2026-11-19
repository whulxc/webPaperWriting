"""Isolated immutable history checks; no real project or Drive writes."""
import json
from pathlib import Path

import pytest

from paper_studio.security.policy import StudioError,encoded,sha,file_sha,builds_root
from paper_studio.versions import history


@pytest.fixture
def cfg(tmp_path):
    project=tmp_path/'paper';project.mkdir()
    return {'project_id':'fixture','project_root':str(project),'artifact_root':str(project/'builds'),
            'artifact_layout':'project-builds-v1','state_root':str(tmp_path/'state'),
            'writable_roots':['candidates'],'readonly_roots':['builds'],'_config_revision':'fixture'}


def build(cfg,number,language,body='baseline',*,canonical=False,pages=2,kind='tex'):
    bid='rb_'+format(number,'024x');root=builds_root(cfg)/bid
    tex=(f'versions/r08/revisions/rev-001/main_{language}.tex' if canonical else
         f'manuscript/round5/main_{language}_candidate.tex')
    (root/'source'/Path(tex).parent).mkdir(parents=True)
    text=(r'\documentclass{article}'+'\n'+body+'\n'+
          r'\begin{equation}x=1\end{equation}\begin{table}values\end{table}\cite{Hunter2007}')
    (root/'source'/tex).write_text(text)
    (root/'source/figures').mkdir();(root/'source/figures/fig01.pdf').write_bytes(b'%PDF-original-figure')
    (root/'output').mkdir();pdf=Path(tex).stem+'.pdf';raw=b'%PDF-fixture-'+body.encode()
    (root/'output'/pdf).write_bytes(raw)
    members=[{'path':p.relative_to(root/'source').as_posix(),'bytes':p.stat().st_size,'sha256':file_sha(p)}
             for p in sorted((root/'source').rglob('*')) if p.is_file()]
    snapshot={'files':members,'sha256':sha(encoded(members)),'bytes':sum(row['bytes'] for row in members)}
    for row in members:
        current=Path(cfg['project_root'])/row['path'];current.parent.mkdir(parents=True,exist_ok=True)
        current.write_bytes((root/'source'/row['path']).read_bytes())
    from publication_fixture import write_evidence
    write_evidence(root,cfg,{'tex_path':tex},snapshot,pdf_path=pdf,pages=pages)
    (root/'worker.log').write_text('completed\n')
    return bid,root,tex


def test_bilingual_compare_detects_english_change_even_with_chinese_last(cfg):
    en,_,_=build(cfg,1,'en');zh,_,_=build(cfg,2,'zh')
    history.save_version(cfg,'r05',[en,zh],'baseline')
    new_en,_,_=build(cfg,3,'en','changed English',pages=3)
    history.save_version(cfg,'r07',[new_en,zh],'English changes')
    result=history.compare_versions(cfg,'r05','r07')
    assert {x['entry'] for x in result['changes']}=={'en'}
    assert any('changed English' in x.get('diff','') for x in result['changes'])
    assert [e['entry'] for e in result['entries']]==['en','zh']
    en_result=result['entries'][0]
    assert en_result['before']['pdf_pages']==2 and en_result['after']['pdf_pages']==3
    signatures=en_result['after']['tex_signatures']
    assert signatures['equations']['count']==1 and signatures['tables']['count']==1
    assert signatures['citations']['keys']==['Hunter2007']
    assert en_result['after']['figures'][0]['sha256']==sha(b'%PDF-original-figure')


def test_candidate_and_canonical_language_main_match_despite_build_ids(cfg):
    old_en,_,_=build(cfg,1,'en');old_zh,_,_=build(cfg,2,'zh')
    new_en,_,_=build(cfg,3,'en',canonical=True);new_zh,_,_=build(cfg,4,'zh',canonical=True)
    history.save_version(cfg,'r05',[old_en,old_zh],'baseline')
    history.save_version(cfg,'r08',[new_en,new_zh],'restore')
    result=history.compare_versions(cfg,'r05','r08')
    assert result['changes']==[] and result['output_changes']==[]
    assert {e['entry'] for e in result['entries']}=={'en','zh'}


def test_last_build_of_same_language_wins_without_losing_other_language(cfg):
    en,_,_=build(cfg,1,'en');zh,_,_=build(cfg,2,'zh');en2,_,_=build(cfg,3,'en')
    history.save_version(cfg,'multi',[en,zh,en2],'two EN selections')
    result=history.compare_versions(cfg,'multi','multi')
    assert result['changes']==[]
    assert {e['entry']:e['before']['build_id'] for e in result['entries']}=={'en':en2,'zh':zh}


def test_save_extra_members_are_in_verified_manifest_and_byte_budget(cfg):
    bid,_,_=build(cfg,1,'en')
    extras={'VERSION.json':b'{"version":"r08"}\n','release/main_en.tex':b'English',
            'revisions/rev-001/main_zh.tex':b'Chinese','figures/fig01/v001/selection.json':b'{}'}
    result=history.save_version(cfg,'r08',[bid],'restore',extra_files=extras)
    root,manifest=history.verify_version(cfg,'r08')
    members={f['path']:f for f in manifest['files']}
    assert result['bytes']==sum(f['bytes'] for f in members.values())
    for path,raw in extras.items():
        assert (root/path).read_bytes()==raw and members[path]['sha256']==sha(raw)
    original=file_sha(root/'manifest.json')
    with pytest.raises(StudioError,match='cannot be overwritten'):
        history.save_version(cfg,'r08',[bid],'second',extra_files=extras)
    assert file_sha(root/'manifest.json')==original


@pytest.mark.parametrize('path',['../escape','/absolute','manifest.json','builds/injected/source.tex',
                                  'CHANGELOG.md','release','figures/../../escape'])
def test_extra_members_reject_unsafe_or_unmanaged_paths_before_creating_stage(cfg,path):
    bid,_,_=build(cfg,1,'en')
    with pytest.raises(StudioError):history.save_version(cfg,'unsafe',[bid],'no',extra_files={path:b'bad'})
    assert not history.version_path(cfg,'unsafe').exists()
    assert not (Path(cfg['artifact_root'])/'versions').exists()


def test_failed_save_removes_only_its_stage_and_keeps_other_history(cfg):
    bid,root,_=build(cfg,1,'en');history.save_version(cfg,'safe',[bid],'keep')
    baseline=file_sha(history.version_path(cfg,'safe')/'manifest.json')
    status=json.loads((root/'status.json').read_text());status['status']='failed'
    (root/'status.json').write_bytes(encoded(status))
    unrelated=Path(cfg['artifact_root'])/'versions/.stage-other';unrelated.mkdir()
    with pytest.raises(StudioError) as caught:history.save_version(cfg,'failed',[bid],'fail')
    assert caught.value.code=='BUILD_NOT_COMPLETE'
    assert not history.version_path(cfg,'failed').exists()
    assert sorted(p.name for p in unrelated.parent.iterdir())==['.stage-other','safe']
    assert file_sha(history.version_path(cfg,'safe')/'manifest.json')==baseline


def test_extra_member_parent_collision_rejected_before_writes(cfg):
    bid,_,_=build(cfg,1,'en')
    with pytest.raises(StudioError) as caught:
        history.save_version(cfg,'bad',[bid],'no',extra_files={'release/file':b'x','release/file/child':b'y'})
    assert caught.value.code=='INVALID_ARGUMENT'
    assert not (Path(cfg['artifact_root'])/'versions').exists()


def test_comparison_refuses_tampered_archived_source(cfg):
    bid,_,_=build(cfg,1,'en');history.save_version(cfg,'old',[bid],'baseline')
    root,manifest=history.verify_version(cfg,'old')
    tex=next(f['path'] for f in manifest['files'] if f['path'].endswith('.tex'))
    (root/tex).write_bytes(b'tampered')
    with pytest.raises(StudioError) as caught:history.compare_versions(cfg,'old','old')
    assert caught.value.code=='VERSION_TAMPERED'


@pytest.mark.parametrize('failure',['changed','extra','removed'])
def test_archive_compares_source_bytes_to_original_build_snapshot(cfg,failure):
    bid,root,tex=build(cfg,7,'en')
    source=root/'source'
    if failure=='changed':
        (source/tex).write_text('different source')
    elif failure=='extra':
        (source/'extra.tex').write_text('unlisted source')
    else:
        (source/tex).unlink()
    with pytest.raises(StudioError) as problem:
        history.save_version(cfg,'bad-source',[bid],'must match frozen inputs')
    assert problem.value.code=='SNAPSHOT_TAMPERED'
    assert not history.version_path(cfg,'bad-source').exists()
