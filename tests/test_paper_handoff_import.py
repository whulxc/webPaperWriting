"""Explicit same-project migration preserves the complete frozen lineage."""
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from paper_studio.bootstrap import initialize, BootstrapError
from paper_studio.bootstrap import handoff
from paper_studio.security.policy import path_for, StudioError


def frozen_hashes(paper):
    return {p.relative_to(paper).as_posix(): handoff.digest(p.read_bytes())
            for root in [paper / handoff.BASE, paper / 'sync']
            for p in root.rglob('*') if p.is_file()}


def append(root, paper, label, previous='absent'):
    raw = (root / 'summary.json').read_bytes()
    spec = {'schema': handoff.INPUT_SCHEMA, 'project_id': 'same-project', 'change_id': label,
            'title': 'Synthetic handoff', 'summary': 'Migration fixture only.',
            'sources': [{'path': 'summary.json', 'sha256': handoff.digest(raw), 'role': 'summary'}],
            'evidence': [{'id': 'E1', 'kind': 'measured_fact', 'statement': 'Synthetic value.',
                          'sources': [{'path': 'summary.json', 'locator': {'json_pointer': '/value'}}]}],
            'impacts': {'claims': [], 'sections': [], 'figures': []}}
    return handoff.create_handoff(project_root=str(root), paper_root=str(paper),
                                  project_id='same-project', spec=spec, expected_previous=previous)


@pytest.fixture
def migration(tmp_path):
    source_root = tmp_path / 'source'; source_root.mkdir()
    (source_root / 'summary.json').write_text('{"value":1}')
    initialize(str(source_root), 'same-project', sources=['summary.json'])
    source = source_root / 'PaperWriting'
    first = append(source_root, source, 'r01')
    (source_root / 'summary.json').write_text('{"value":2}')
    append(source_root, source, 'r02', first['material_revision'])
    target_root = tmp_path / 'target'; target_root.mkdir()
    target = target_root / 'PaperWriting'; shutil.copytree(source, target)
    (target / 'PROJECT.yaml').write_text(json.dumps({'schema': 'paper-editor.project.v1',
        'project_id': 'same-project', 'paper_writing_root': str(target), 'fixture_origin': str(source)}))
    return {'source_project_root': str(source_root), 'source_paper_root': str(source),
            'target_project_root': str(target_root), 'target_paper_root': str(target),
            'project_id': 'same-project', 'expected_index_sha256': handoff.digest((source / handoff.INDEX_PATH).read_bytes())}


def apply(args):
    plan = handoff.plan_handoff_import(**args)
    return handoff.import_handoffs(**args, authorized=True, expected_plan_sha256=plan['plan_sha256'])


def test_explicit_import_preserves_history_replays_and_discovers_offline(migration):
    source = Path(migration['source_paper_root']); target = Path(migration['target_paper_root'])
    before_source = frozen_hashes(source); before_target = frozen_hashes(target)
    with pytest.raises(BootstrapError) as error:
        handoff.discover_handoff(target, 'same-project')
    assert error.value.code == 'HANDOFF_TAMPERED'
    plan = handoff.plan_handoff_import(**migration)
    assert plan['writes'] == 0 and not (target / handoff.IMPORT_BASE).exists()
    with pytest.raises(BootstrapError) as error:
        handoff.import_handoffs(**migration, expected_plan_sha256=plan['plan_sha256'])
    assert error.value.code == 'HANDOFF_IMPORT_AUTHORIZATION_REQUIRED'
    result = apply(migration)
    assert result['new_receipts'] == result['imported_manifests'] == 2
    assert frozen_hashes(source) == before_source and frozen_hashes(target) == before_target
    assert apply(migration)['replayed']
    # No reads of the old research/paper root are needed after local admission.
    Path(migration['source_project_root']).rename(source.parent.with_name('offline-source'))
    found = handoff.discover_handoff(target, 'same-project')
    assert found['sequence'] == 2 and found['evidence'][0]['sources'][0]['excerpt'] == '2'


def test_imported_lineage_accepts_new_local_handoff_and_reimport_replays(migration):
    apply(migration)
    root = Path(migration['target_project_root']); target = Path(migration['target_paper_root'])
    old = handoff.discover_handoff(target, 'same-project'); before = frozen_hashes(target)
    (root / 'summary.json').write_text('{"value":3}')
    new = append(root, target, 'r03', old['material_revision'])
    found = handoff.discover_handoff(target, 'same-project')
    assert found['sequence'] == 3 and found['previous_material_revision'] == old['material_revision']
    assert found['material_revision'] == new['material_revision']
    assert all(frozen_hashes(target)[k] == v for k, v in before.items() if k != handoff.INDEX_PATH)
    assert apply(migration)['replayed']


def test_minimal_target_contract_with_explicit_external_research_root_can_append(migration, tmp_path):
    external = tmp_path / 'external-research'; external.mkdir()
    migration['target_project_root'] = str(external)
    apply(migration)
    target = Path(migration['target_paper_root'])
    previous = handoff.discover_handoff(target, 'same-project')['material_revision']
    (external / 'summary.json').write_text('{"value":3}')
    append(external, target, 'r03', previous)
    assert handoff.discover_handoff(target, 'same-project')['sequence'] == 3


@pytest.mark.parametrize('side', ['source_paper_root', 'target_paper_root'])
@pytest.mark.parametrize('member', ['manifest.json', 'HANDOFF.md', 'sources/summary.json'])
def test_either_side_tamper_refuses_before_any_receipt(migration, side, member):
    target = Path(migration['target_paper_root'])
    path = Path(migration[side]) / handoff.BASE / 'r01' / member
    path.write_text('changed immutable bytes')
    with pytest.raises(BootstrapError):
        apply(migration)
    assert not (target / handoff.IMPORT_BASE).exists()


def test_cross_project_wrong_contract_stale_plan_and_escaping_source_rejected(migration):
    target = Path(migration['target_paper_root']); contract = target / 'PROJECT.yaml'
    original = contract.read_bytes(); metadata = json.loads(original)
    metadata['project_id'] = 'another-project'; contract.write_text(json.dumps(metadata))
    with pytest.raises(BootstrapError) as error:
        apply(migration)
    assert error.value.code == 'HANDOFF_PROJECT_MISMATCH'
    contract.write_bytes(original)
    with pytest.raises(BootstrapError) as error:
        handoff.import_handoffs(**migration, authorized=True, expected_plan_sha256='0' * 64)
    assert error.value.code == 'HANDOFF_IMPORT_PLAN_STALE'
    source = Path(migration['source_paper_root']) / handoff.BASE / 'r01/sources/summary.json'
    raw = source.read_bytes(); source.unlink(); outside = source.parents[5] / 'external.json'; outside.write_bytes(raw)
    source.symlink_to(outside)
    with pytest.raises(BootstrapError) as error:
        apply(migration)
    assert error.value.code == 'BOOTSTRAP_LINK_DENIED'
    assert not (target / handoff.IMPORT_BASE).exists()


def test_copied_local_receipts_do_not_authorize_another_physical_root(migration, tmp_path):
    apply(migration); target = Path(migration['target_paper_root'])
    other = tmp_path / 'unapproved' / 'PaperWriting'; shutil.copytree(target, other)
    metadata = json.loads((other / 'PROJECT.yaml').read_bytes())
    metadata['paper_writing_root'] = str(other); (other / 'PROJECT.yaml').write_text(json.dumps(metadata))
    with pytest.raises(BootstrapError) as error:
        handoff.discover_handoff(other, 'same-project')
    assert error.value.code == 'HANDOFF_TAMPERED'


def test_receipt_paths_cannot_be_forged_by_normal_project_edit(migration):
    cfg = {'project_root': migration['target_paper_root'], 'writable_roots': ['.config'], 'readonly_roots': []}
    with pytest.raises(StudioError) as error:
        path_for(cfg, handoff.IMPORT_BASE + '/' + '0' * 64 + '.json', True)
    assert error.value.code == 'PATH_DENIED'


def test_full_predecessor_object_is_verified_even_with_rehashed_indexes(migration):
    source = Path(migration['source_paper_root']); target = Path(migration['target_paper_root'])
    index = json.loads((source / handoff.INDEX_PATH).read_bytes())
    row = index['entries'][1]
    path = source / row['manifest_path']; manifest = json.loads(path.read_bytes())
    manifest['previous']['change_id'] = 'forged-predecessor'
    raw = handoff.encoded(manifest); path.write_bytes(raw); (target / row['manifest_path']).write_bytes(raw)
    row['manifest_sha256'] = handoff.digest(raw); index['latest'] = row
    raw_index = handoff.encoded(index)
    for paper in (source, target): (paper / handoff.INDEX_PATH).write_bytes(raw_index)
    migration['expected_index_sha256'] = handoff.digest(raw_index)
    with pytest.raises(BootstrapError) as error:
        apply(migration)
    assert error.value.code == 'HANDOFF_TAMPERED' and not (target / handoff.IMPORT_BASE).exists()


def test_mutation_during_second_full_verification_leaves_no_receipts(migration, monkeypatch):
    target = Path(migration['target_paper_root']); plan = handoff.plan_handoff_import(**migration)
    original = handoff.plan_handoff_import; calls = []

    def changing(**kwargs):
        calls.append(True)
        if len(calls) == 2:
            (target / handoff.BASE / 'r01/HANDOFF.md').write_text('concurrent target mutation')
        return original(**kwargs)

    monkeypatch.setattr(handoff, 'plan_handoff_import', changing)
    with pytest.raises(BootstrapError):
        handoff.import_handoffs(**migration, authorized=True, expected_plan_sha256=plan['plan_sha256'])
    assert not (target / handoff.IMPORT_BASE).exists()


def test_public_verifier_accepts_exact_history_and_requires_import_admission(migration):
    source = Path(migration['source_paper_root']); target = Path(migration['target_paper_root'])
    index = json.loads((source / handoff.INDEX_PATH).read_bytes())
    first, second = index['entries']
    assert handoff.verify_indexed_handoff(source, 'same-project', first['manifest_path'],
                                          first['manifest_sha256'])['sequence'] == 1
    with pytest.raises(BootstrapError) as error:
        handoff.verify_indexed_handoff(target, 'same-project', second['manifest_path'], second['manifest_sha256'])
    assert error.value.code == 'HANDOFF_TAMPERED'
    apply(migration)
    assert handoff.verify_indexed_handoff(target, 'same-project', second['manifest_path'],
                                          second['manifest_sha256'])['sequence'] == 2
    for path, sha in [(first['manifest_path'], '0' * 64), ('../manifest.json', first['manifest_sha256'])]:
        with pytest.raises(BootstrapError) as error:
            handoff.verify_indexed_handoff(target, 'same-project', path, sha)
        assert error.value.code == 'HANDOFF_NOT_INDEXED'
    (target / handoff.BASE / 'r01/HANDOFF.md').write_text('tampered predecessor')
    with pytest.raises(BootstrapError) as error:
        handoff.verify_indexed_handoff(target, 'same-project', second['manifest_path'], second['manifest_sha256'])
    assert error.value.code == 'HANDOFF_TAMPERED'


def test_local_cli_plans_and_requires_authorization_for_apply(migration, tmp_path):
    cli = Path(__file__).resolve().parents[1] / 'management/paper_handoff_import.py'
    flags = [part for key, value in migration.items() for part in ['--' + key.replace('_', '-'), value]]
    command = [sys.executable, '-B', str(cli)]
    plan = subprocess.run([*command, 'plan', *flags], cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert plan.returncode == 0, plan.stdout + plan.stderr
    identity = json.loads(plan.stdout)['result']['plan_sha256']
    rejected = subprocess.run([*command, 'apply', *flags, '--expected-plan-sha256', identity],
                              cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert json.loads(rejected.stdout)['error']['code'] == 'HANDOFF_IMPORT_AUTHORIZATION_REQUIRED'
    applied = subprocess.run([*command, 'apply', *flags, '--expected-plan-sha256', identity, '--authorized'],
                             cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert json.loads(applied.stdout)['result']['imported_manifests'] == 2
