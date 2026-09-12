import json
import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError

from aflow_app_server.global_config_service import GlobalConfigService
from aflow_app_server.guided_config import GuidedConfigError, guided_form_response
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

INLINE_FAMILY_AFLOW = '''# preserve inline family metadata
[harness.codex.profiles.fast]
model = "test"
[harness.codex.profiles.deep]
model = "deep"
[roles]
worker = "codex.fast"
reviewer = "codex.fast"
[teams.base]
display_name = "Legacy base"
upgrade_to = "child"
worker = "codex.deep"
reviewer = "codex.fast"
[teams.child]
# Keep this comment through conversion.
display_name = "Legacy child"
backup_team = "fallback"
worker = "codex.fast"
reviewer = "codex.fast"
[teams.fallback]
[prompts]
work = "Do {task}."
'''

@pytest.fixture
def service(tmp_path: Path):
    (tmp_path / 'aflow.toml').write_text(AFLOW)
    (tmp_path / 'workflows.toml').write_text(WORKFLOWS)
    return GlobalConfigService(config_dir=tmp_path, audit_path=tmp_path / 'audit.jsonl')

@pytest.fixture
def inline_service(tmp_path: Path):
    (tmp_path / 'aflow.toml').write_text(INLINE_FAMILY_AFLOW)
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


def test_patch_audit_scope_is_explicit_and_defaults_to_rest(service):
    result = service.patch(
        GlobalConfigPatchPayload(
            expected_revision=service.read().revision,
            actions=[
                dict(
                    type="upsert_profile",
                    harness="codex",
                    profile="mcp",
                    model="mcp-model",
                )
            ],
        ),
        caller_scope="mcp",
    )
    record = json.loads((service.config_dir / "audit.jsonl").read_text().splitlines()[-1])
    assert result.revision == record["new_revision"]
    assert record["caller_scope"] == "mcp"
    assert record["outcome"] == "saved"

    service.patch(
        GlobalConfigPatchPayload(
            expected_revision=result.revision,
            actions=[
                dict(
                    type="upsert_profile",
                    harness="codex",
                    profile="rest",
                    model="rest-model",
                )
            ],
        )
    )
    record = json.loads((service.config_dir / "audit.jsonl").read_text().splitlines()[-1])
    assert record["caller_scope"] == "rest"


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


def test_oversized_family_batch_is_rejected_without_splitting():
    actions = [
        {'type': 'add_team', 'team': f'family-{index}'}
        for index in range(257)
    ]
    with pytest.raises(ValidationError):
        GlobalConfigPatchPayload(expected_revision='a' * 64, actions=actions)


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


def test_inline_legacy_conversion_persists_role_removal_and_reload_parity(inline_service):
    result = inline_service.patch(GlobalConfigPatchPayload(
        expected_revision=inline_service.read().revision,
        actions=[
            {'type': 'set_team_base', 'team': 'child', 'extends': 'base'},
            {'type': 'set_team_role', 'team': 'child', 'role': 'reviewer', 'selector': None},
        ],
    ))
    raw = tomllib.loads(result.aflow_toml)
    child = raw['teams']['child']
    assert child['extends'] == 'base'
    assert child['worker'] == 'codex.fast'
    assert 'reviewer' not in child
    assert 'roles' not in child
    assert '# Keep this comment through conversion.' in result.aflow_toml

    reread = inline_service.read()
    form = guided_form_response(reread.aflow_toml, reread.workflows_toml)['form']
    assert form['teams']['child']['extends'] == 'base'
    assert form['teams']['child']['roles'] == {'worker': 'codex.fast'}


def test_family_actions_project_and_remove_after_batch_reference_rewrites(service):
    before = service.read()
    typed = GlobalConfigPatchPayload(
        expected_revision=before.revision,
        actions=[
            {'type': 'add_team', 'team': 'family-base'},
            {'type': 'add_team', 'team': 'family-stage'},
            {
                'type': 'set_team_display_name',
                'team': 'family-base',
                'display_name': 'Product development',
            },
            {
                'type': 'set_team_display_name',
                'team': 'family-stage',
                'display_name': 'Stronger worker',
            },
            {
                'type': 'set_team_base',
                'team': 'family-stage',
                'extends': 'family-base',
            },
            {
                'type': 'set_team_role',
                'team': 'family-stage',
                'role': 'worker',
                'selector': 'codex.worker',
            },
            {
                'type': 'set_team_upgrade',
                'team': 'family-base',
                'upgrade_to': 'family-stage',
            },
            {
                'type': 'set_workflow_default_team',
                'workflow': 'demo',
                'team': 'family-stage',
            },
        ],
    )
    assert typed.model_dump(mode='json')['actions'][4] == {
        'type': 'set_team_base',
        'team': 'family-stage',
        'extends': 'family-base',
    }

    created = service.patch(typed)
    form = guided_form_response(created.aflow_toml, created.workflows_toml)['form']
    assert form is not None
    stage = form['teams']['family-stage']
    assert stage['roles'] == {'worker': 'codex.worker'}
    assert stage['extends'] == 'family-base'
    assert stage['display_name'] == 'Stronger worker'
    assert stage['effective_roles'] == {'worker': 'codex.worker'}
    assert stage['role_sources'] == {'worker': 'family-stage'}
    assert form['teams']['family-base']['upgrade_to'] == 'family-stage'

    before_failed_remove = service.read()
    with pytest.raises(GuidedConfigError) as excinfo:
        service.patch(
            GlobalConfigPatchPayload(
                expected_revision=before_failed_remove.revision,
                actions=[{'type': 'remove_team', 'team': 'family-stage'}],
            )
        )
    assert excinfo.value.code == 'team_in_use'
    assert 'workflow.demo.team' in str(excinfo.value)
    assert service.read() == before_failed_remove

    removed = service.patch(
        GlobalConfigPatchPayload(
            expected_revision=before_failed_remove.revision,
            actions=[
                {
                    'type': 'set_workflow_default_team',
                    'workflow': 'demo',
                    'team': None,
                },
                {
                    'type': 'set_team_upgrade',
                    'team': 'family-base',
                    'upgrade_to': None,
                },
                {'type': 'remove_team', 'team': 'family-stage'},
            ],
        )
    )
    assert 'family-stage' not in removed.aflow_toml
    assert 'team = "family-stage"' not in removed.workflows_toml
    assert 'upgrade_to = "family-stage"' not in removed.aflow_toml


def test_family_batch_rejects_invalid_inheritance_without_writing_either_document(service):
    before = service.read()
    with pytest.raises(ProjectConfigError) as excinfo:
        service.patch(
            GlobalConfigPatchPayload(
                expected_revision=before.revision,
                actions=[
                    {'type': 'add_team', 'team': 'family-a'},
                    {'type': 'add_team', 'team': 'family-b'},
                    {
                        'type': 'set_team_base',
                        'team': 'family-a',
                        'extends': 'family-b',
                    },
                    {
                        'type': 'set_team_base',
                        'team': 'family-b',
                        'extends': 'family-a',
                    },
                ],
            )
        )
    assert 'cycle' in str(excinfo.value).lower()
    assert service.read() == before


def test_manager_enabled_default_and_override_roundtrip(service):
    # Enabling supervision requires manager roles; seed them with a raw save
    # before exercising the typed supervision actions.
    patch(service, documents={
        'aflow.toml': AFLOW + '\n[manager]\nlite_role = "worker"\nfull_role = "worker"\n',
        'workflows.toml': WORKFLOWS,
    })
    result = patch(service, actions=[
        dict(type='set_default_manager_enabled', value=True),
        dict(type='set_workflow_manager_enabled', workflow='demo', value=False),
    ])
    assert 'manager_enabled = true' in result.workflows_toml
    assert 'manager_enabled = false' in result.workflows_toml
    # The seeded aflow document keeps its comment and manager roles.
    assert '# preserve this comment' in result.aflow_toml
    assert 'lite_role = "worker"' in result.aflow_toml
    form = guided_form_response(result.aflow_toml, result.workflows_toml)['form']
    assert form['default_manager_enabled'] is True
    demo = form['workflows']['demo']
    assert demo['manager_enabled'] is False
    assert demo['effective_manager_enabled'] is False
    assert demo['manager_enabled_source'] == 'workflow'
    # Save/reload parity: the reloaded pair projects identically.
    reprojected = guided_form_response(service.read().aflow_toml, service.read().workflows_toml)['form']
    assert reprojected['default_manager_enabled'] is True
    assert reprojected['workflows']['demo'] == demo
    # Deleting the override restores inheritance from the saved default.
    cleared = patch(service, actions=[dict(type='set_workflow_manager_enabled', workflow='demo', value=None)])
    cleared_form = guided_form_response(cleared.aflow_toml, cleared.workflows_toml)['form']
    assert cleared_form['workflows']['demo']['manager_enabled'] is None
    assert cleared_form['workflows']['demo']['effective_manager_enabled'] is True
    assert cleared_form['workflows']['demo']['manager_enabled_source'] == 'defaults'
    assert cleared_form['default_manager_enabled'] is True

def test_manager_enabled_rejects_unknowns_coercion_and_stale_revisions(service):
    from aflow_app_server.guided_config import GuidedConfigError
    before = service.read()
    with pytest.raises(GuidedConfigError):
        patch(service, actions=[dict(type='set_workflow_manager_enabled', workflow='ghost', value=True)])
    with pytest.raises(ValidationError):
        patch(service, actions=[dict(type='set_default_manager_enabled', value=1)])
    with pytest.raises(ProjectConfigRevisionConflict):
        service.patch(GlobalConfigPatchPayload(
            expected_revision='0' * 64,
            actions=[dict(type='set_default_manager_enabled', value=True)],
        ))
    assert service.read() == before


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
