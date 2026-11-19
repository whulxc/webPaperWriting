"""Create a small, explicitly synthetic two-revision handoff in a clean output root."""
import argparse
import json
from pathlib import Path

from .workspace import encoded, digest, initialize, no_links
from .handoff import INPUT_SCHEMA, create_handoff
from paper_studio.evidence.index import context


def run(output_root):
    root=no_links(Path(output_root))
    if root.exists() and any(root.iterdir()):
        raise ValueError('A new or empty explicit output root is required')
    root.mkdir(parents=True,exist_ok=True);(root/'results').mkdir()
    project_id='material-handoff-fixture'
    (root/'results/r01.json').write_bytes(encoded({'synthetic_fixture':True,'run_id':'fixture-r01','metrics':{'accuracy':0.73}}))
    initialize(str(root),project_id,project_name='Synthetic material handoff example',target_venue='Information Fusion',sources=['results/r01.json'])
    paper=root/'PaperWriting'
    (paper/'manuscript/main.tex').write_text('% Synthetic fixture only.\n\\section{Methods}\nAuthor text is preserved.\n')
    protected={name:(paper/name).read_bytes() for name in ['PROJECT.yaml','PROJECT_STATE.json','HANDOFF.md','manuscript/main.tex']}
    def spec(change_id):
        path='results/'+change_id+'.json'
        return {'schema':INPUT_SCHEMA,'project_id':project_id,'change_id':change_id,
                'title':'Synthetic '+change_id+' material handoff',
                'summary':'Synthetic interface fixture only; not a scientific experiment or accepted claim.',
                'sources':[{'path':path,'sha256':digest((root/path).read_bytes()),'role':'synthetic_experiment_summary'}],
                'evidence':[{'id':'E-accuracy','kind':'unverified_hypothesis',
                             'statement':'The synthetic source records an accuracy value; no scientific significance is inferred.',
                             'sources':[{'path':path,'locator':{'json_pointer':'/metrics/accuracy'}}]}],
                'impacts':{'claims':[{'id':'C-accuracy','path':'manuscript/main.tex','reason':'Reassess the candidate statement from this material revision.'}],
                           'figures':[{'id':'fig01','path':'figures/candidates/fig01.svg','reason':'A future candidate figure must cite this revision.'}],
                           'sections':[{'id':'methods','path':'manuscript/main.tex','anchor':'Methods','reason':'Describe the synthetic evaluation unit in a separate author edit.'}]}}
    first=create_handoff(project_root=root,paper_root=paper,project_id=project_id,spec=spec('r01'),expected_previous='absent')
    (root/'results/r02.json').write_bytes(encoded({'synthetic_fixture':True,'run_id':'fixture-r02','metrics':{'accuracy':0.78}}))
    second=create_handoff(project_root=root,paper_root=paper,project_id=project_id,spec=spec('r02'),expected_previous=first['material_revision'])
    assert all((paper/name).read_bytes()==raw for name,raw in protected.items())
    cfg={'project_id':project_id,'project_root':str(paper),'artifact_root':str(paper/'builds'),
         'artifact_layout':'project-builds-v1','writable_roots':['manuscript'],
         'readonly_roots':['materials','builds'],'_config_revision':'synthetic-fixture'}
    discovered=context(cfg)['latest_material_handoff']
    assert discovered['material_revision']==second['material_revision']
    return {'synthetic_fixture':True,'project_root':str(root),'paper_root':str(paper),
            'r01':first,'r02':second,'chat_discovered_revision':discovered['material_revision'],
            'latest_actual_excerpt':discovered['evidence'][0]['sources'][0]['excerpt'],
            'human_files_unchanged':True,'cloud_operations':0}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output-root',required=True)
    args=parser.parse_args();print(json.dumps(run(args.output_root),ensure_ascii=False,indent=2))


if __name__=='__main__':main()
