"""Verify discoverable style packaging using isolated portable plugin fixtures."""
from pathlib import Path, PurePosixPath
import hashlib
import json
import posixpath
import re
import shutil
import zipfile

import pytest
import yaml

from management import paper_editor_package as package


@pytest.fixture
def source(tmp_path, monkeypatch):
    destination=tmp_path/'skills/paper-editor'
    shutil.copytree(package.SOURCE,destination)
    monkeypatch.setattr(package,'SOURCE',destination)
    return destination


def snapshot(tmp_path, *, legacy=False, existing=None):
    interface={'displayName':'Existing App','defaultPrompt':['Preserve this private prompt']}
    overlay={'name':'fixture-app','version':'1.2.3','skills':'./skills',
             'apps':{'app-fixture':{}},'interface':interface}
    contents={'.codex-plugin/plugin.json':json.dumps(overlay),
              '.app.json':json.dumps({'apps':{'app-fixture':{'id':'fixed-app-id'}}})}
    if not legacy:
        contents['plugin.json']=json.dumps({'name':'fixture-app','version':'1.2.3',
            'extensions':{'com.openai':{'interface':interface}}})
    contents.update(existing or {})
    value={'plugin':{'plugin_id':'plugin_fixture','name':'fixture-app',
                    'version':'1.2.3','current_release_id':'pluginrel_'+'a'*32},
           'contents':contents,'files':[{'path':p,'size_bytes':len(raw.encode())} for p,raw in contents.items()]}
    value['files'].append({'path':'assets/private.png','size_bytes':100})
    path=tmp_path/'snapshot.json';path.write_text(json.dumps(value))
    return path,value


def test_standalone_contains_complete_discoverable_style_from_one_rulebook(source,tmp_path):
    receipt=package.package(tmp_path/'package')
    with zipfile.ZipFile(receipt['archive']) as archive:
        expected=(source/'references/anti-defensive-writing.md').read_bytes()
        assert archive.read('skills/anti-defensive-writing/SKILL.md')==expected
        assert archive.read('skills/paper-editor/references/anti-defensive-writing.md')==expected
        agent=yaml.safe_load(archive.read('skills/anti-defensive-writing/agents/openai.yaml'))
        assert agent['interface']['display_name']=='反防御写作'
        assert agent['policy']['allow_implicit_invocation'] is True
        assert (source/'references/anti-defensive-writing-LICENSE.md').read_bytes() in archive.read('skills/anti-defensive-writing/references/UPSTREAM-LICENSE.md')
        for path in archive.namelist():
            if not path.endswith('.md'):continue
            for target in re.findall(r'\]\(([^)]+)\)',archive.read(path).decode()):
                if '://' in target or target.startswith('#'):continue
                resolved=posixpath.normpath(str(PurePosixPath(path).parent/target.split('#')[0]))
                assert resolved in archive.namelist(),(path,target,resolved)
    assert receipt['style_component']['rulebook_sha256']==hashlib.sha256(expected).hexdigest()
    assert receipt['style_component']['mode']=='generated_from_shared_rulebook'
    assert len(receipt['style_component']['added_files'])==3


@pytest.mark.parametrize('legacy',[False,True])
def test_app_overlay_adds_component_and_keeps_existing_app_files(source,tmp_path,legacy):
    existing={'skills/unrelated/SKILL.md':'Keep my other skill',
              'skills/paper-editor/references/project-connection.md':'Keep project identity'}
    path,before=snapshot(tmp_path,legacy=legacy,existing=existing)
    receipt=package.app_skill_package(tmp_path/'package',snapshot=path,
        expected_release_id=before['plugin']['current_release_id'],plugin_version='1.2.4')
    with zipfile.ZipFile(receipt['archive']) as archive:
        assert archive.read('skills/anti-defensive-writing/SKILL.md')==(source/'references/anti-defensive-writing.md').read_bytes()
        assert '.app.json' not in archive.namelist()
        assert 'assets/private.png' not in archive.namelist()
        for member in existing:assert member not in archive.namelist()
        merged={**before['contents'],**{p:archive.read(p).decode() for p in archive.namelist()}}
        for member,value in existing.items():assert merged[member]==value
        assert merged['.app.json']==before['contents']['.app.json']
        for manifest in ['.codex-plugin/plugin.json']+([] if legacy else ['plugin.json']):
            parsed=json.loads(merged[manifest]);parsed['version']='1.2.3'
            assert parsed==json.loads(before['contents'][manifest])
    assert json.loads(path.read_text())==before


@pytest.mark.parametrize('app',[False,True])
def test_existing_independently_edited_component_is_preserved(source,tmp_path,app):
    custom={'skills/anti-defensive-writing/SKILL.md':'My author-edited rulebook',
            'skills/anti-defensive-writing/agents/openai.yaml':'private policy',
            'skills/unrelated/SKILL.md':'Other skill'}
    path,before=snapshot(tmp_path,existing=custom)
    if app:
        receipt=package.app_skill_package(tmp_path/'package',snapshot=path,
            expected_release_id=before['plugin']['current_release_id'],plugin_version='1.2.4')
    else:
        receipt=package.package(tmp_path/'package',migration_snapshot=path,
            expected_release_id=before['plugin']['current_release_id'],plugin_version='1.2.4',
            preserve_presentation=True)
    with zipfile.ZipFile(receipt['archive']) as archive:
        assert not any(p.startswith('skills/anti-defensive-writing/') for p in archive.namelist())
        merged={**before['contents'],**{p:archive.read(p).decode() for p in archive.namelist()}}
        assert all(merged[p]==raw for p,raw in custom.items())
    assert receipt['style_component']['mode']=='preserved_existing'
    assert receipt['style_component']['added_files']=={}
    assert receipt['style_component']['preserved_paths']==sorted(p for p in custom if '/anti-defensive-writing/' in p)


def test_inventory_only_sibling_is_preserved_without_guessing_its_contents(source):
    old={'files':[{'path':'skills/anti-defensive-writing/SKILL.md','size_bytes':2048}]}
    files,receipt=package.style_component(old)
    assert files=={} and receipt['mode']=='preserved_existing'


def test_rule_change_drives_both_new_component_and_embedded_reference(source,tmp_path):
    file=source/'references/anti-defensive-writing.md'
    changed=file.read_bytes()+b'\nShared-rule regression fixture.\n';file.write_bytes(changed)
    receipt=package.package(tmp_path/'package')
    with zipfile.ZipFile(receipt['archive']) as archive:
        assert archive.read('skills/anti-defensive-writing/SKILL.md')==changed
        assert archive.read('skills/paper-editor/references/anti-defensive-writing.md')==changed


@pytest.mark.parametrize('missing',['anti-defensive-writing.md','anti-defensive-writing-LICENSE.md'])
def test_missing_rule_or_license_fails_before_partial_package(source,tmp_path,missing):
    (source/'references'/missing).unlink()
    with pytest.raises(ValueError):package.package(tmp_path/'refused')
    assert not (tmp_path/'refused').exists()


def test_existing_incomplete_component_requires_reconciliation(source,tmp_path):
    path,before=snapshot(tmp_path,existing={
        'skills/anti-defensive-writing/references/private-note.md':'Preserve this note'})
    with pytest.raises(ValueError,match='has no SKILL.md'):
        package.app_skill_package(tmp_path/'refused',snapshot=path,
            expected_release_id=before['plugin']['current_release_id'],plugin_version='1.2.4')
    assert not (tmp_path/'refused').exists()
