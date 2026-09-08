from pathlib import Path

import pytest
from pydantic import ValidationError

from aflow_app_server.global_config_service import GlobalConfigService
from aflow_app_server.guided_config import guided_form_response
from aflow_app_server.models import GlobalConfigPatchPayload
from aflow_app_server.project_config_service import ProjectConfigError, ProjectConfigRevisionConflict

AFLOW = '''# preserve this comment
[aflow]
default_workflow = "demo"
[harness.codex.profiles.worker]
model = "test"
effort = "high"
[roles]
worker = "codex.worker"
[prompts]
work = "Do {task}."
'''
WORKFLOWS = '''[workflow]
merge_prompt = ["work"]
[workflow.demo]
merge_prompt = ["work", "work"]
[workflow.demo.steps.implement]
role = "worker"
prompts = ["work"]
go = [{to="END", when="DONE"}]
'''

@pytest.fixture
def service(tmp_path: Path):
    (tmp_path / 'aflow.toml').write_text(AFLOW)
    (tmp_path / 'workflows.toml').write_text(WORKFLOWS)
    return GlobalConfigService(config_dir=tmp_path, audit_path=tmp_path / 'audit.jsonl')

def patch(service, **fields):
    return service.patch(GlobalConfigPatchPayload(expected_revision=service.read().revision, **fields))

def test_effort_only_preserves_model_comments_and_sibling_inode(service):
    sibling = service.config_dir / 'workflows.toml'
    inode = sibling.stat().st_ino
    result = patch(service, actions=[dict(type='upsert_profile', harness='codex', profile='worker', effort='new-effort')])
    assert 'model = "test"' in result.aflow_toml
    assert '# preserve this comment' in result.aflow_toml
    assert result.workflows_toml == WORKFLOWS
    assert sibling.stat().st_ino == inode

def test_prompt_batch_creation_rename_and_override_roundtrip(service):
    text = 'Unicode λ\n  {literal} "quoted"\n'
    result = patch(service, actions=[
        dict(type='set_prompt', name='new', text=text),
        dict(type='rename_prompt', name='work', new_name='renamed'),
        dict(type='set_role_prompt', role='worker', text=text),
        dict(type='add_team', team='newteam'),
        dict(type='set_role_prompt', team='newteam', role='worker', text='team text'),
    ])
    form = guided_form_response(result.aflow_toml, result.workflows_toml)['form']
    assert form['prompts']['new'] == text
    assert form['role_prompts']['worker'] == text
    assert form['teams']['newteam']['prompts']['worker'] == 'team text'
    assert len(form['prompt_usages']['renamed']) == 3
    assert '["renamed", "renamed"]' in result.workflows_toml
    patch(service, actions=[dict(type='set_role_prompt', team='newteam', role='worker', text=None)])
    assert service.read().revision != result.revision

def test_referenced_delete_conflict_and_invalid_batch_preserve_bytes(service):
    before = service.read()
    with pytest.raises(ProjectConfigError):
        patch(service, actions=[dict(type='set_prompt', name='work', text=None)])
    with pytest.raises(ProjectConfigRevisionConflict):
        service.patch(GlobalConfigPatchPayload(expected_revision='0'*64, documents={'aflow.toml': 'invalid'}))
    assert service.read() == before

def test_final_batch_can_delete_then_recreate_reference(service):
    result = patch(service, actions=[dict(type='set_prompt', name='work', text=None), dict(type='set_prompt', name='work', text='Replacement')])
    assert 'Replacement' in result.aflow_toml

def test_noop_does_not_replace_files(service):
    path = service.config_dir / 'aflow.toml'
    before = path.stat()
    patch(service, documents={'aflow.toml': AFLOW})
    assert path.stat().st_ino == before.st_ino


def test_custom_profile_role_and_team_and_prompt_move(service):
    result = patch(service, actions=[
        dict(type='upsert_profile', harness='codex', profile='new-profile', model='new-model', effort='new-effort'),
        dict(type='set_global_role', role='new-role', selector='codex.new-profile'),
        dict(type='add_team', team='new-team'),
        dict(type='set_team_role', team='new-team', role='new-role', selector='codex.new-profile'),
        dict(type='set_role_prompt', role='new-role', text='Text {preserved}\nλ'),
        dict(type='move_role_prompt', role='new-role', target_role='worker', target_team='new-team'),
    ])
    form = guided_form_response(result.aflow_toml, result.workflows_toml)['form']
    assert form['harnesses']['codex']['new-profile'] == {'model': 'new-model', 'effort': 'new-effort'}
    assert form['teams']['new-team']['prompts']['worker'] == 'Text {preserved}\nλ'
    assert 'new-role' not in form['role_prompts']

@pytest.mark.parametrize('fields', [
    {'actions': [{'type': 'unknown'}]}, {'documents': {'other.toml': ''}},
    {'documents': {}}, {'actions': []},
    {'documents': {'aflow.toml': ''}, 'actions': [{'type': 'set_max_turns', 'value': 5}]},
])
def test_closed_patch_contract(fields):
    with pytest.raises(ValidationError):
        GlobalConfigPatchPayload(expected_revision='a'*64, **fields)


def test_team_chain_creation_links_and_reload_parity(service):
    """Two new teams plus their link survive one batch with byte fidelity."""
    result = patch(service, actions=[
        dict(type='add_team', team='stage-one'),
        dict(type='add_team', team='stage-two'),
        dict(type='set_team_role', team='stage-one', role='worker', selector='codex.worker'),
        dict(type='set_team_upgrade', team='stage-one', upgrade_to='stage-two'),
    ])
    assert '[teams.stage-one]' in result.aflow_toml
    assert 'upgrade_to = "stage-two"' in result.aflow_toml
    form = guided_form_response(result.aflow_toml, result.workflows_toml)['form']
    assert form['teams']['stage-one']['upgrade_to'] == 'stage-two'
    assert form['teams']['stage-one']['roles'] == {'worker': 'codex.worker'}
    # Save/reload parity: the reloaded pair projects the identical chain.
    reread = service.read()
    reprojected = guided_form_response(reread.aflow_toml, reread.workflows_toml)['form']
    assert reprojected['teams'] == form['teams']


def test_team_upgrade_cycle_is_rejected_without_writes(service):
    before = service.read()
    with pytest.raises(ProjectConfigError):
        patch(service, actions=[
            dict(type='add_team', team='a'),
            dict(type='add_team', team='b'),
            dict(type='set_team_upgrade', team='a', upgrade_to='b'),
            dict(type='set_team_upgrade', team='b', upgrade_to='a'),
        ])
    assert service.read() == before
