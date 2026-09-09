"""Pure guided-settings projection and transform tests.

The guided form endpoint transforms only the submitted candidate pair: these
tests prove byte preservation, comment/unknown-field round trips, the closed
action set, syntax recovery, ZCode rejection, starter cleanup, and that no
function here ever touches a registered project.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from aflow.config import load_workflow_config, render_starter_documents

from aflow_app_server.guided_config import (
    GuidedConfigError,
    apply_action_batch,
    guided_form_response,
)
from aflow_app_server.models import (
    AddTeamAction,
    BuildStarterAction,
    ProjectConfigFormPayload,
    ProjectConfigFormResponse,
    RenamePromptAction,
    SetDefaultManagerEnabledAction,
    SetDefaultWorkflowAction,
    SetGlobalRoleAction,
    SetMaxTurnsAction,
    SetPromptAction,
    SetTeamRoleAction,
    SetTeamUpgradeAction,
    SetWorkflowDefaultTeamAction,
    SetWorkflowManagerEnabledAction,
    UpsertProfileAction,
)

AFLOW_TEXT = """# Top comment survives.
[aflow]
default_workflow = "deliver"

[harness.codex.profiles.fast]
model = "test-model"

[harness.codex.profiles.deep]
model = "big-model"
effort = "high"

[roles]
worker = "codex.fast"

# A team that overrides the worker role.
[teams.crew.roles]
worker = "codex.deep"

[prompts]
p = "Work."
"""

WORKFLOWS_TEXT = """[workflow]
main_branch = "main"

[workflow.deliver.steps.implement]
role = "worker"
prompts = ["p"]
go = [{ to = "END", when = "DONE" }]

[workflow.deliver.steps.verify]
role = "worker"
prompts = ["p"]
go = [{ to = "END", when = "DONE" }]
"""

STARTER_AFLOW, STARTER_WORKFLOWS = render_starter_documents()


def _call(
    aflow_text: str = AFLOW_TEXT,
    workflows_text: str = WORKFLOWS_TEXT,
    action=None,
) -> ProjectConfigFormResponse:
    result = guided_form_response(aflow_text, workflows_text, action)
    return ProjectConfigFormResponse.model_validate(result)


class TestNoOpAndPreservation:
    def test_no_action_returns_texts_byte_for_byte(self) -> None:
        response = _call()
        assert response.changed is False
        assert response.aflow_toml == AFLOW_TEXT
        assert response.workflows_toml == WORKFLOWS_TEXT
        assert response.form is not None
        assert response.form.default_workflow == "deliver"
        assert response.form.max_turns is None
        assert response.validation.state == "ready"

    def test_modified_document_preserves_comments_and_unknown_keys(self) -> None:
        response = _call(action=SetMaxTurnsAction(type="set_max_turns", value=30))
        assert response.changed is True
        assert "# Top comment survives." in response.aflow_toml
        assert "# A team that overrides the worker role." in response.aflow_toml
        assert 'default_workflow = "deliver"' in response.aflow_toml
        assert response.workflows_toml == WORKFLOWS_TEXT

    def test_unknown_fields_and_comments_survive_an_edit(self) -> None:
        # The engine rejects unknown fields at validation, but a guided edit
        # must still preserve them verbatim for Advanced TOML.
        carrying = AFLOW_TEXT.replace(
            "[roles]\n",
            '[roles]\nkeeper = "codex.fast"\n[custom_table]\ncustom_key = "keep-me"\n',
            1,
        )
        carrying = carrying.replace(
            'worker = "codex.fast"', 'worker = "codex.fast" # trailing comment'
        )
        response = _call(
            aflow_text=carrying,
            action=SetMaxTurnsAction(type="set_max_turns", value=7),
        )
        assert 'custom_key = "keep-me"' in response.aflow_toml
        assert "# trailing comment" in response.aflow_toml
        assert "max_turns = 7" in response.aflow_toml
        assert response.validation.state == "invalid"

    def test_unmodified_document_of_a_pair_is_byte_identical(self) -> None:
        response = _call(
            action=SetWorkflowDefaultTeamAction(
                type="set_workflow_default_team", workflow="deliver", team="crew"
            )
        )
        assert response.aflow_toml == AFLOW_TEXT
        assert 'team = "crew"' in response.workflows_toml
        assert response.form is not None
        assert response.form.workflow_default_teams["deliver"] == "crew"

    def test_projection_reports_profiles_roles_teams_and_steps(self) -> None:
        response = _call()
        form = response.form
        assert form is not None
        assert form.harnesses["codex"]["fast"].model == "test-model"
        assert form.harnesses["codex"]["deep"].effort == "high"
        assert form.roles == {"worker": "codex.fast"}
        assert form.teams["crew"].roles == {"worker": "codex.deep"}
        deliver = form.workflows["deliver"]
        assert deliver.declared_steps == ("implement", "verify")
        assert deliver.executable_steps == ("implement", "verify")
        assert deliver.first_executable_step == "implement"

    def test_semantically_incomplete_draft_stays_projectable(self) -> None:
        broken = AFLOW_TEXT.replace('worker = "codex.fast"', 'worker = "codex.missing"')
        response = _call(aflow_text=broken)
        assert response.validation.state == "invalid"
        assert any("codex.missing" in issue.message or "unknown profile 'missing'" in issue.message for issue in response.validation.issues)
        assert response.form is not None
        assert response.form.roles["worker"] == "codex.missing"

    def test_configured_choices_come_from_the_submitted_candidate(self) -> None:
        response = _call()
        assert response.choices.harnesses == ("codex",)
        assert response.choices.profiles == {"codex": ("deep", "fast")}
        assert response.choices.selectors == ("codex.deep", "codex.fast")
        assert response.choices.roles == ("worker",)
        assert response.choices.teams == ("crew",)
        assert response.choices.workflows == ("deliver",)

    def test_suggestions_are_labeled_and_bundled(self) -> None:
        response = _call()
        assert response.suggestions.label == "suggestion"
        names = {item.name for item in response.suggestions.harnesses}
        assert {"codex", "opencode", "zcode"} <= names
        zcode = next(
            item for item in response.suggestions.harnesses if item.name == "zcode"
        )
        assert zcode.custom_model_supported is False
        assert any(
            item.harness == "opencode" and item.profile == "glm-5.3"
            for item in response.suggestions.profiles
        )
        assert "suggestion" in response.suggestions.note.lower()


class TestActions:
    def test_set_default_workflow(self) -> None:
        response = _call(
            action=SetDefaultWorkflowAction(type="set_default_workflow", value="deliver")
        )
        assert 'default_workflow = "deliver"' in response.aflow_toml

    def test_set_default_workflow_rejects_unknown_workflow(self) -> None:
        with pytest.raises(GuidedConfigError) as exc_info:
            _call(action=SetDefaultWorkflowAction(type="set_default_workflow", value="ghost"))
        assert exc_info.value.code == "unknown_workflow"

    def test_set_max_turns_set_and_clear(self) -> None:
        set_response = _call(action=SetMaxTurnsAction(type="set_max_turns", value=12))
        assert "max_turns = 12" in set_response.aflow_toml
        cleared = _call(
            aflow_text=set_response.aflow_toml,
            action=SetMaxTurnsAction(type="set_max_turns", value=None),
        )
        assert "max_turns" not in cleared.aflow_toml
        assert "# Top comment survives." in cleared.aflow_toml

    def test_upsert_profile_create_update_and_partial_omission(self) -> None:
        created = _call(
            action=UpsertProfileAction(
                type="upsert_profile",
                harness="codex",
                profile="spare",
                model="custom-model",
            )
        )
        assert 'model = "custom-model"' in created.aflow_toml
        # Omitted fields stay unchanged.
        kept = _call(
            aflow_text=created.aflow_toml,
            action=UpsertProfileAction(
                type="upsert_profile", harness="codex", profile="spare", effort="low"
            ),
        )
        assert 'model = "custom-model"' in kept.aflow_toml
        assert 'effort = "low"' in kept.aflow_toml
        # Explicit null clears an engine-optional value.
        cleared = _call(
            aflow_text=kept.aflow_toml,
            action=UpsertProfileAction(
                type="upsert_profile", harness="codex", profile="spare", model=None
            ),
        )
        assert 'model = "custom-model"' not in cleared.aflow_toml

    def test_upsert_profile_rejects_unknown_harness_and_effort(self) -> None:
        with pytest.raises(GuidedConfigError) as harness_error:
            _call(
                action=UpsertProfileAction(
                    type="upsert_profile", harness="typo", profile="p", model="m"
                )
            )
        assert harness_error.value.code == "unknown_harness"
        with pytest.raises(GuidedConfigError) as effort_error:
            _call(
                action=UpsertProfileAction(
                    type="upsert_profile",
                    harness="opencode",
                    profile="p",
                    effort="high",
                )
            )
        assert effort_error.value.code == "effort_not_supported"

    def test_upsert_profile_rejects_zcode_model_with_explanation(self) -> None:
        with pytest.raises(GuidedConfigError) as exc_info:
            _call(
                action=UpsertProfileAction(
                    type="upsert_profile", harness="zcode", profile="p", model="gpt"
                )
            )
        assert exc_info.value.code == "zcode_managed_by_zcode"
        assert "ZCode" in str(exc_info.value)

    def test_set_global_role_requires_configured_profile(self) -> None:
        updated = _call(
            action=SetGlobalRoleAction(
                type="set_global_role", role="worker", selector="codex.deep"
            )
        )
        assert 'worker = "codex.deep"' in updated.aflow_toml
        with pytest.raises(GuidedConfigError) as exc_info:
            _call(
                action=SetGlobalRoleAction(
                    type="set_global_role", role="worker", selector="codex.ghost"
                )
            )
        assert exc_info.value.code == "unknown_profile"

    def test_add_team_creates_empty_roles_table(self) -> None:
        response = _call(action=AddTeamAction(type="add_team", team="reserves"))
        assert "[teams.reserves.roles]" in response.aflow_toml
        assert response.form is not None
        assert response.form.teams["reserves"].roles == {}
        with pytest.raises(GuidedConfigError) as exc_info:
            _call(
                aflow_text=response.aflow_toml,
                action=AddTeamAction(type="add_team", team="reserves"),
            )
        assert exc_info.value.code == "team_exists"

    def test_set_team_role(self) -> None:
        response = _call(
            action=SetTeamRoleAction(
                type="set_team_role", team="crew", role="worker", selector="codex.fast"
            )
        )
        assert response.form is not None
        assert response.form.teams["crew"].roles == {"worker": "codex.fast"}
        with pytest.raises(GuidedConfigError):
            _call(
                action=SetTeamRoleAction(
                    type="set_team_role", team="ghost", role="worker", selector="codex.fast"
                )
            )
        with pytest.raises(GuidedConfigError):
            _call(
                action=SetTeamRoleAction(
                    type="set_team_role", team="crew", role="worker", selector="codex.ghost"
                )
            )

    def test_set_workflow_default_team_set_clear_and_rejects_unknowns(self) -> None:
        with pytest.raises(GuidedConfigError):
            _call(
                action=SetWorkflowDefaultTeamAction(
                    type="set_workflow_default_team", workflow="ghost", team="crew"
                )
            )
        with pytest.raises(GuidedConfigError):
            _call(
                action=SetWorkflowDefaultTeamAction(
                    type="set_workflow_default_team", workflow="deliver", team="ghost"
                )
            )
        set_response = _call(
            action=SetWorkflowDefaultTeamAction(
                type="set_workflow_default_team", workflow="deliver", team="crew"
            )
        )
        assert 'team = "crew"' in set_response.workflows_toml
        cleared = _call(
            workflows_text=set_response.workflows_toml,
            action=SetWorkflowDefaultTeamAction(
                type="set_workflow_default_team", workflow="deliver", team=None
            ),
        )
        assert "team =" not in cleared.workflows_toml


class TestBuildStarter:
    def test_build_starter_from_empty_pair_matches_renderer(self) -> None:
        response = _call(
            aflow_text="",
            workflows_text="",
            action=BuildStarterAction(
                type="build_starter", workflow="build", main_branch="trunk", team=None
            ),
        )
        expected_aflow, expected_workflows = render_starter_documents(
            "build", None, "trunk"
        )
        assert response.changed is True
        assert response.aflow_toml == expected_aflow
        assert response.workflows_toml == expected_workflows
        assert response.validation.state == "configuration_required"
        assert "harness.starter.profiles.default.model" in response.validation.placeholders

    def test_build_starter_rejects_nonempty_pair(self) -> None:
        with pytest.raises(GuidedConfigError) as exc_info:
            _call(action=BuildStarterAction(
                type="build_starter", workflow="build", main_branch="main"
            ))
        assert exc_info.value.code == "build_starter_requires_empty_pair"

    def test_build_starter_with_team_records_team(self) -> None:
        response = _call(
            aflow_text="",
            workflows_text="",
            action=BuildStarterAction(
                type="build_starter", workflow="build", main_branch="main", team="crew"
            ),
        )
        assert '[teams."crew".roles]' in response.aflow_toml
        assert 'team = "crew"' in response.workflows_toml

    def test_empty_to_ready_flow_never_saves(self) -> None:
        built = _call(
            aflow_text="",
            workflows_text="",
            action=BuildStarterAction(
                type="build_starter", workflow="implement", main_branch="main"
            ),
        )
        with_profile = _call(
            aflow_text=built.aflow_toml,
            workflows_text=built.workflows_toml,
            action=UpsertProfileAction(
                type="upsert_profile",
                harness="codex",
                profile="default",
                model="real-model",
            ),
        )
        replaced = _call(
            aflow_text=with_profile.aflow_toml,
            workflows_text=with_profile.workflows_toml,
            action=SetGlobalRoleAction(
                type="set_global_role", role="worker", selector="codex.default"
            ),
        )
        assert replaced.validation.state == "ready"
        assert "[harness.starter" not in replaced.aflow_toml
        assert 'model = "FILL_IN_MODEL"' not in replaced.aflow_toml
        assert replaced.form is not None
        assert replaced.form.roles == {"worker": "codex.default"}


class TestProductionAlignedProfiles:
    def test_bundled_dotted_profile_can_be_created_and_assigned(self) -> None:
        created = _call(
            action=UpsertProfileAction(
                type="upsert_profile",
                harness="opencode",
                profile="glm-5.3",
                model="glm-5.3",
            )
        )
        assert created.form is not None
        assert created.form.harnesses["opencode"]["glm-5.3"].model == "glm-5.3"
        assigned = _call(
            aflow_text=created.aflow_toml,
            action=SetGlobalRoleAction(
                type="set_global_role", role="worker", selector="opencode.glm-5.3"
            ),
        )
        assert 'worker = "opencode.glm-5.3"' in assigned.aflow_toml
        assert assigned.form is not None
        assert assigned.form.roles == {"worker": "opencode.glm-5.3"}
        assert "opencode.glm-5.3" in assigned.choices.selectors

    def test_invalid_starter_input_returns_bounded_error_not_config_error(
        self,
    ) -> None:
        with pytest.raises(GuidedConfigError) as workflow_error:
            _call(
                action=SetDefaultWorkflowAction(
                    type="set_default_workflow", value="bad name!"
                )
            )
        assert workflow_error.value.code == "invalid_action_value"
        with pytest.raises(GuidedConfigError) as build_error:
            _call(
                aflow_text="",
                workflows_text="",
                action=BuildStarterAction(
                    type="build_starter", workflow="implement", main_branch="bad..name"
                ),
            )
        assert build_error.value.code == "invalid_action_value"

    def test_selector_splits_at_first_dot(self) -> None:
        seeded = _call(
            action=UpsertProfileAction(
                type="upsert_profile",
                harness="opencode",
                profile="glm-5.3",
                model="glm-5.3",
            )
        )
        assigned = _call(
            aflow_text=seeded.aflow_toml,
            action=SetGlobalRoleAction(
                type="set_global_role", role="worker", selector="opencode.glm-5.3"
            ),
        )
        assert assigned.changed is True


class TestAliasProjection:
    TEAM_WORKFLOWS = """[workflow]
main_branch = "main"

[workflow.deliver]
team = "crew"

[workflow.deliver.steps.implement]
role = "worker"
prompts = ["p"]
go = [{ to = "END", when = "DONE" }]

[workflow.deliver.steps.verify]
role = "worker"
prompts = ["p"]
go = [{ to = "END", when = "DONE" }]

[workflow.alias]
extends = "deliver"
"""

    def test_alias_projection_matches_load_workflow_config(self) -> None:
        response = _call(workflows_text=self.TEAM_WORKFLOWS)
        form = response.form
        assert form is not None
        alias = form.workflows["alias"]
        assert alias.declared_steps == ("implement", "verify")
        assert alias.first_step == "implement"
        assert alias.executable_steps == ("implement", "verify")
        assert alias.first_executable_step == "implement"
        assert form.workflow_default_teams["alias"] == "crew"

        with tempfile.TemporaryDirectory() as temporary:
            temp_dir = Path(temporary)
            (temp_dir / "aflow.toml").write_text(response.aflow_toml, encoding="utf-8")
            (temp_dir / "workflows.toml").write_text(
                response.workflows_toml, encoding="utf-8"
            )
            config = load_workflow_config(temp_dir / "aflow.toml")
        produced = config.workflows["alias"]
        assert alias.declared_steps == tuple(produced.declared_steps)
        assert alias.executable_steps == tuple(produced.steps)
        assert alias.first_executable_step == produced.first_step
        assert alias.step_roles == {
            name: step.role for name, step in produced.steps.items()
        }
        assert form.workflow_default_teams["alias"] == produced.team


class TestStepRoleProjection:
    def test_step_roles_use_each_steps_declared_role(self) -> None:
        aflow_text = AFLOW_TEXT.replace(
            '[roles]\nworker = "codex.fast"',
            '[roles]\nworker = "codex.fast"\nplanner = "codex.deep"',
        )
        workflows_text = WORKFLOWS_TEXT.replace(
            '[workflow.deliver.steps.verify]\nrole = "worker"',
            '[workflow.deliver.steps.verify]\nrole = "planner"',
        )
        response = _call(aflow_text=aflow_text, workflows_text=workflows_text)
        form = response.form
        assert form is not None
        deliver = form.workflows["deliver"]
        assert deliver.step_roles == {"implement": "worker", "verify": "planner"}

    def test_alias_and_excluded_step_roles_match_the_production_loader(self) -> None:
        workflows_text = TestAliasProjection.TEAM_WORKFLOWS + (
            "\n[workflow.narrow]\nextends = \"deliver\"\nexclude = [\"verify\"]\n"
        )
        response = _call(workflows_text=workflows_text)
        form = response.form
        assert form is not None
        # The alias inherits every executable step with its declared role.
        assert form.workflows["alias"].step_roles == {
            "implement": "worker",
            "verify": "worker",
        }
        # The excluded step is absent from both executable steps and roles.
        narrow = form.workflows["narrow"]
        assert narrow.executable_steps == ("implement",)
        assert narrow.step_roles == {"implement": "worker"}
        assert "verify" not in narrow.step_roles

        with tempfile.TemporaryDirectory() as temporary:
            temp_dir = Path(temporary)
            (temp_dir / "aflow.toml").write_text(response.aflow_toml, encoding="utf-8")
            (temp_dir / "workflows.toml").write_text(
                response.workflows_toml, encoding="utf-8"
            )
            config = load_workflow_config(temp_dir / "aflow.toml")
        produced = config.workflows["narrow"]
        assert narrow.step_roles == {
            name: step.role for name, step in produced.steps.items()
        }

    def test_step_roles_stay_null_when_materialization_is_unavailable(self) -> None:
        # A semantically invalid pair is still projectable from the raw
        # tables, but the production loader never ran, so there is no exact
        # per-step role evidence and the field stays None.
        broken = AFLOW_TEXT.replace('worker = "codex.fast"', 'worker = "codex.missing"')
        response = _call(aflow_text=broken)
        assert response.validation.state == "invalid"
        assert response.form is not None
        deliver = response.form.workflows["deliver"]
        assert deliver.step_roles is None
        assert deliver.executable_steps is None
        assert deliver.declared_steps == ("implement", "verify")


class TestRoleTargetValidation:
    def test_set_global_role_rejects_reserved_prompts_role(self) -> None:
        with pytest.raises(GuidedConfigError) as exc_info:
            _call(
                action=SetGlobalRoleAction(
                    type="set_global_role", role="prompts", selector="codex.fast"
                )
            )
        assert exc_info.value.code == "reserved_role"

    def test_set_team_role_requires_existing_global_role(self) -> None:
        with pytest.raises(GuidedConfigError) as exc_info:
            _call(
                action=SetTeamRoleAction(
                    type="set_team_role",
                    team="crew",
                    role="ghost",
                    selector="codex.fast",
                )
            )
        assert exc_info.value.code == "unknown_role"


class TestManagerEnabled:
    # Enabling supervision requires manager roles; pairs that enable a
    # workflow use this text so they stay valid and exercise the canonical
    # materialized path instead of the invalid-pair fallback.
    MANAGER_AFLOW = (
        AFLOW_TEXT + '\n[manager]\nlite_role = "worker"\nfull_role = "worker"\n'
    )
    ALIAS_WORKFLOWS = WORKFLOWS_TEXT + (
        "\n[workflow.quiet]\n"
        "manager_enabled = false\n"
        "\n"
        "[workflow.quiet.steps.implement]\n"
        "role = \"worker\"\n"
        "prompts = [\"p\"]\n"
        "go = [{ to = \"END\", when = \"DONE\" }]\n"
        "\n"
        "[workflow.child]\n"
        "extends = \"deliver\"\n"
        "\n"
        "[workflow.muted]\n"
        "extends = \"quiet\"\n"
    )

    def test_omitted_everywhere_is_disabled_with_declared_nulls(self) -> None:
        response = _call()
        assert response.validation.state == "ready"
        form = response.form
        assert form is not None
        assert form.default_manager_enabled is None
        deliver = form.workflows["deliver"]
        assert deliver.manager_enabled is None
        assert deliver.effective_manager_enabled is False
        assert deliver.manager_enabled_source == "defaults"

    def test_set_default_true_propagates_without_touching_workflows(self) -> None:
        response = _call(
            aflow_text=self.MANAGER_AFLOW,
            action=SetDefaultManagerEnabledAction(
                type="set_default_manager_enabled", value=True
            ),
        )
        assert response.changed is True
        assert response.validation.state == "ready"
        assert "manager_enabled = true" in response.workflows_toml
        # Only the default table gains the flag; no workflow table is written.
        assert response.workflows_toml.count("manager_enabled") == 1
        form = response.form
        assert form is not None
        assert form.default_manager_enabled is True
        deliver = form.workflows["deliver"]
        assert deliver.manager_enabled is None
        assert deliver.effective_manager_enabled is True
        assert deliver.manager_enabled_source == "defaults"

    def test_explicit_false_overrides_true_default_and_stays_declared(self) -> None:
        defaulted = _call(
            aflow_text=self.MANAGER_AFLOW,
            action=SetDefaultManagerEnabledAction(
                type="set_default_manager_enabled", value=True
            ),
        )
        assert defaulted.validation.state == "ready"
        response = _call(
            aflow_text=self.MANAGER_AFLOW,
            workflows_text=defaulted.workflows_toml,
            action=SetWorkflowManagerEnabledAction(
                type="set_workflow_manager_enabled", workflow="deliver", value=False
            ),
        )
        assert "manager_enabled = false" in response.workflows_toml
        form = response.form
        assert form is not None
        assert form.default_manager_enabled is True
        deliver = form.workflows["deliver"]
        # Explicit false is a real declaration: presence, not truthiness.
        assert deliver.manager_enabled is False
        assert deliver.effective_manager_enabled is False
        assert deliver.manager_enabled_source == "workflow"

    def test_alias_inherits_base_and_explicit_override_wins(self) -> None:
        response = _call(workflows_text=self.ALIAS_WORKFLOWS)
        assert response.validation.state == "ready"
        form = response.form
        assert form is not None
        assert form.default_manager_enabled is None
        assert form.workflows["quiet"].manager_enabled is False
        assert form.workflows["quiet"].effective_manager_enabled is False
        assert form.workflows["quiet"].manager_enabled_source == "workflow"
        # An alias with no flag inherits its concrete base, not the default.
        child = form.workflows["child"]
        assert child.manager_enabled is None
        assert child.effective_manager_enabled is False
        assert child.manager_enabled_source == "base:deliver"
        muted = form.workflows["muted"]
        assert muted.manager_enabled is None
        assert muted.effective_manager_enabled is False
        assert muted.manager_enabled_source == "base:quiet"

        enabled_base = _call(
            aflow_text=self.MANAGER_AFLOW,
            workflows_text=self.ALIAS_WORKFLOWS,
            action=SetWorkflowManagerEnabledAction(
                type="set_workflow_manager_enabled", workflow="deliver", value=True
            ),
        )
        assert enabled_base.validation.state == "ready"
        assert enabled_base.form is not None
        assert enabled_base.form.workflows["child"].effective_manager_enabled is True
        assert (
            enabled_base.form.workflows["child"].manager_enabled_source
            == "base:deliver"
        )
        # The quiet subtree is unaffected by the deliver change.
        assert enabled_base.form.workflows["muted"].effective_manager_enabled is False

        silenced = _call(
            aflow_text=self.MANAGER_AFLOW,
            workflows_text=enabled_base.workflows_toml,
            action=SetWorkflowManagerEnabledAction(
                type="set_workflow_manager_enabled", workflow="child", value=False
            ),
        )
        assert silenced.validation.state == "ready"
        assert silenced.form is not None
        assert silenced.form.workflows["child"].manager_enabled is False
        assert silenced.form.workflows["child"].effective_manager_enabled is False
        assert silenced.form.workflows["child"].manager_enabled_source == "workflow"

        # Every projected effective value matches the canonical resolver.
        with tempfile.TemporaryDirectory() as temporary:
            temp_dir = Path(temporary)
            (temp_dir / "aflow.toml").write_text(silenced.aflow_toml, encoding="utf-8")
            (temp_dir / "workflows.toml").write_text(
                silenced.workflows_toml, encoding="utf-8"
            )
            config = load_workflow_config(temp_dir / "aflow.toml")
        for name, wf_config in config.workflows.items():
            assert (
                silenced.form.workflows[name].effective_manager_enabled
                == wf_config.manager_enabled
            )

    def test_base_wins_over_default_for_aliases(self) -> None:
        defaulted = _call(
            aflow_text=self.MANAGER_AFLOW,
            workflows_text=self.ALIAS_WORKFLOWS,
            action=SetDefaultManagerEnabledAction(
                type="set_default_manager_enabled", value=True
            ),
        )
        assert defaulted.validation.state == "ready"
        form = defaulted.form
        assert form is not None
        # deliver inherits the true default; child follows deliver.
        assert form.workflows["child"].effective_manager_enabled is True
        assert form.workflows["child"].manager_enabled_source == "base:deliver"
        # quiet explicitly opts out, and muted follows quiet, not the default.
        assert form.workflows["muted"].effective_manager_enabled is False
        assert form.workflows["muted"].manager_enabled_source == "base:quiet"

    def test_delete_override_and_default_restore_inheritance(self) -> None:
        flagged = _call(
            aflow_text=self.MANAGER_AFLOW,
            workflows_text=self.ALIAS_WORKFLOWS,
            action=SetWorkflowManagerEnabledAction(
                type="set_workflow_manager_enabled", workflow="child", value=True
            ),
        )
        assert flagged.validation.state == "ready"
        assert flagged.form is not None
        assert flagged.form.workflows["child"].effective_manager_enabled is True
        cleared = _call(
            aflow_text=self.MANAGER_AFLOW,
            workflows_text=flagged.workflows_toml,
            action=SetWorkflowManagerEnabledAction(
                type="set_workflow_manager_enabled", workflow="child", value=None
            ),
        )
        assert "manager_enabled" not in cleared.workflows_toml.split("[workflow.child]")[1].split("[workflow.")[0]
        assert cleared.form is not None
        assert cleared.form.workflows["child"].manager_enabled is None
        assert cleared.form.workflows["child"].effective_manager_enabled is False
        assert cleared.form.workflows["child"].manager_enabled_source == "base:deliver"

        defaulted = _call(
            action=SetDefaultManagerEnabledAction(
                type="set_default_manager_enabled", value=True
            )
        )
        undefaulted = _call(
            workflows_text=defaulted.workflows_toml,
            action=SetDefaultManagerEnabledAction(
                type="set_default_manager_enabled", value=None
            ),
        )
        assert "manager_enabled" not in undefaulted.workflows_toml
        assert undefaulted.form is not None
        assert undefaulted.form.default_manager_enabled is None
        assert undefaulted.form.workflows["deliver"].effective_manager_enabled is False

    def test_delete_absent_default_is_a_net_noop(self) -> None:
        response = _call(
            action=SetDefaultManagerEnabledAction(
                type="set_default_manager_enabled", value=None
            )
        )
        assert response.changed is False
        assert response.workflows_toml == WORKFLOWS_TEXT
        assert response.form is not None
        assert response.form.default_manager_enabled is None

    def test_unknown_workflow_is_rejected(self) -> None:
        with pytest.raises(GuidedConfigError) as exc_info:
            _call(
                action=SetWorkflowManagerEnabledAction(
                    type="set_workflow_manager_enabled", workflow="ghost", value=True
                )
            )
        assert exc_info.value.code == "unknown_workflow"

    def test_nonboolean_values_are_rejected_without_coercion(self) -> None:
        with pytest.raises(ValidationError):
            SetDefaultManagerEnabledAction(
                type="set_default_manager_enabled", value=1  # type: ignore[arg-type]
            )
        with pytest.raises(ValidationError):
            SetWorkflowManagerEnabledAction(
                type="set_workflow_manager_enabled",
                workflow="deliver",
                value="true",  # type: ignore[arg-type]
            )
        with pytest.raises(ValidationError):
            ProjectConfigFormPayload(
                aflow_toml=AFLOW_TEXT,
                workflows_toml=WORKFLOWS_TEXT,
                action={
                    "type": "set_workflow_manager_enabled",
                    "workflow": "deliver",
                    "value": 0,
                },
            )

    def test_edits_preserve_comments_and_sibling_keys(self) -> None:
        workflows_text = WORKFLOWS_TEXT.replace(
            "[workflow]\n",
            "# Supervision default lives here.\n[workflow]\n",
            1,
        )
        response = _call(
            workflows_text=workflows_text,
            action=SetDefaultManagerEnabledAction(
                type="set_default_manager_enabled", value=False
            ),
        )
        assert "# Supervision default lives here." in response.workflows_toml
        assert 'main_branch = "main"' in response.workflows_toml
        # Explicit false is written, not dropped as falsy.
        assert "manager_enabled = false" in response.workflows_toml
        assert response.form is not None
        assert response.form.default_manager_enabled is False
        assert response.form.workflows["deliver"].effective_manager_enabled is False

    def test_raw_and_typed_edits_project_identically(self) -> None:
        raw_text = WORKFLOWS_TEXT.replace(
            "[workflow]\n",
            "[workflow]\nmanager_enabled = true\n",
            1,
        )
        raw_text = raw_text.replace(
            "[workflow.deliver.steps.implement]\n",
            "[workflow.deliver]\nmanager_enabled = false\n\n[workflow.deliver.steps.implement]\n",
            1,
        )
        raw = _call(workflows_text=raw_text)
        typed = _call(
            action=SetDefaultManagerEnabledAction(
                type="set_default_manager_enabled", value=True
            )
        )
        typed = _call(
            workflows_text=typed.workflows_toml,
            action=SetWorkflowManagerEnabledAction(
                type="set_workflow_manager_enabled", workflow="deliver", value=False
            ),
        )
        assert raw.form is not None and typed.form is not None
        assert raw.form.default_manager_enabled == typed.form.default_manager_enabled
        assert (
            raw.form.workflows["deliver"].model_dump()
            == typed.form.workflows["deliver"].model_dump()
        )

    def test_mixed_batch_applies_in_order_and_rejects_unknowns(self) -> None:
        aflow_text, workflows_text = apply_action_batch(
            AFLOW_TEXT,
            WORKFLOWS_TEXT,
            [
                SetDefaultManagerEnabledAction(
                    type="set_default_manager_enabled", value=True
                ),
                SetWorkflowManagerEnabledAction(
                    type="set_workflow_manager_enabled",
                    workflow="deliver",
                    value=False,
                ),
            ],
        )
        form = guided_form_response(aflow_text, workflows_text)["form"]
        assert form["default_manager_enabled"] is True
        assert form["workflows"]["deliver"]["manager_enabled"] is False
        assert form["workflows"]["deliver"]["effective_manager_enabled"] is False
        assert form["workflows"]["deliver"]["manager_enabled_source"] == "workflow"
        with pytest.raises(GuidedConfigError):
            apply_action_batch(
                AFLOW_TEXT,
                WORKFLOWS_TEXT,
                [
                    SetDefaultManagerEnabledAction(
                        type="set_default_manager_enabled", value=True
                    ),
                    SetWorkflowManagerEnabledAction(
                        type="set_workflow_manager_enabled",
                        workflow="ghost",
                        value=False,
                    ),
                ],
            )

    def test_invalid_pair_still_projects_declared_precedence(self) -> None:
        broken = AFLOW_TEXT.replace('worker = "codex.fast"', 'worker = "codex.missing"')
        workflows_text = self.ALIAS_WORKFLOWS.replace(
            "[workflow]\n",
            "[workflow]\nmanager_enabled = true\n",
            1,
        )
        response = _call(aflow_text=broken, workflows_text=workflows_text)
        assert response.validation.state == "invalid"
        assert response.form is not None
        assert response.form.default_manager_enabled is True
        deliver = response.form.workflows["deliver"]
        assert deliver.manager_enabled is None
        assert deliver.effective_manager_enabled is True
        assert deliver.manager_enabled_source == "defaults"
        muted = response.form.workflows["muted"]
        assert muted.effective_manager_enabled is False
        assert muted.manager_enabled_source == "base:quiet"


class TestZCodeFieldOwnership:
    def test_upsert_profile_rejects_zcode_model_and_effort_nulls(self) -> None:
        for field in ("model", "effort"):
            with pytest.raises(GuidedConfigError) as exc_info:
                _call(
                    action=UpsertProfileAction(
                        type="upsert_profile",
                        harness="zcode",
                        profile="p",
                        **{field: None},
                    )
                )
            assert exc_info.value.code == "zcode_managed_by_zcode"
            assert "ZCode" in str(exc_info.value)

    def test_field_free_zcode_profile_action_stays_valid(self) -> None:
        response = _call(
            action=UpsertProfileAction(
                type="upsert_profile", harness="zcode", profile="p"
            )
        )
        assert response.changed is True
        assert response.form is not None
        assert response.form.harnesses["zcode"]["p"].model is None


class TestSyntaxRecovery:
    def test_syntax_error_returns_unchanged_texts_without_projection(self) -> None:
        broken = AFLOW_TEXT.replace('default_workflow = "deliver"', "default_workflow = ")
        response = _call(aflow_text=broken)
        assert response.changed is False
        assert response.aflow_toml == broken
        assert response.form is None
        assert response.syntax_issues
        assert response.syntax_issues[0].document == "aflow.toml"
        assert response.syntax_issues[0].line is not None

    def test_syntax_error_in_workflows_document_is_reported_per_document(self) -> None:
        broken = WORKFLOWS_TEXT.replace('[workflow', '[workflow')
        broken = broken.replace('main_branch = "main"', 'main_branch = ')
        response = _call(workflows_text=broken)
        assert response.changed is False
        assert response.workflows_toml == broken
        assert response.form is None
        assert response.syntax_issues[0].document == "workflows.toml"


class TestMalformedPromptShapes:
    """Syntactically valid but mistyped prompt tables stay repairable.

    Each shape must return the untouched documents with the production
    validation diagnosis instead of a projection-time ValueError (HTTP 500).
    """

    def test_string_prompts_table_returns_diagnostics_without_projection(self) -> None:
        broken = 'prompts = "wrong"\n' + AFLOW_TEXT.replace('[prompts]\np = "Work."\n', '')
        response = _call(aflow_text=broken)
        assert response.aflow_toml == broken
        assert response.workflows_toml == WORKFLOWS_TEXT
        assert response.form is None
        assert response.syntax_issues == ()
        assert response.validation.state == "invalid"
        assert any("prompts" in issue.message for issue in response.validation.issues)

    def test_string_roles_prompts_table_returns_diagnostics(self) -> None:
        broken = AFLOW_TEXT.replace("[roles]\n", '[roles]\nprompts = "wrong"\n', 1)
        response = _call(aflow_text=broken)
        assert response.aflow_toml == broken
        assert response.form is None
        assert response.validation.state == "invalid"

    def test_mistyped_team_prompts_entry_returns_diagnostics(self) -> None:
        broken = AFLOW_TEXT.replace(
            "[teams.crew.roles]",
            "[teams.crew.prompts]\nworker = 5\n\n[teams.crew.roles]",
            1,
        )
        response = _call(aflow_text=broken)
        assert response.aflow_toml == broken
        assert response.form is None
        assert response.validation.state == "invalid"

    def test_malformed_step_prompt_array_is_skipped_not_fatal(self) -> None:
        broken = WORKFLOWS_TEXT.replace(
            'prompts = ["p"]', 'prompts = "not-an-array"', 1
        )
        response = _call(workflows_text=broken)
        assert response.workflows_toml == broken
        assert response.validation.state == "invalid"
        assert response.syntax_issues == ()

    def test_mistyped_merge_prompt_is_reported_without_projection_crash(self) -> None:
        broken = WORKFLOWS_TEXT + '[workflow.deliver.merge_prompt = 5]\n'
        response = _call(workflows_text=broken)
        assert response.workflows_toml == broken
        assert response.validation.state == "invalid"

    def test_rename_updates_merge_prompt_reference(self) -> None:
        carrying = WORKFLOWS_TEXT.replace(
            "[workflow.deliver.steps.implement]",
            '[workflow.deliver]\nmerge_prompt = "p"\n\n[workflow.deliver.steps.implement]',
            1,
        )
        response = _call(
            workflows_text=carrying,
            action=RenamePromptAction(type="rename_prompt", name="p", new_name="q"),
        )
        assert 'merge_prompt = "q"' in response.workflows_toml
        assert 'prompts = ["q"]' in response.workflows_toml
        assert response.form is not None
        assert response.form.prompt_usages["q"]

    def test_prompt_action_against_mistyped_table_is_a_bounded_rejection(self) -> None:
        broken = 'prompts = "wrong"\n' + AFLOW_TEXT.replace('[prompts]\np = "Work."\n', '')
        with pytest.raises(GuidedConfigError) as excinfo:
            guided_form_response(
                broken,
                WORKFLOWS_TEXT,
                SetPromptAction(type="set_prompt", name="x", text="y"),
            )
        assert excinfo.value.code == "invalid_field_type"


class TestStarterProfileCleanup:
    # A seeded real profile keeps every selector below valid.
    SEEDED_AFLOW = (
        STARTER_AFLOW
        + '\n[harness.codex.profiles.real]\nmodel = "m"\n'
    )

    def test_placeholder_profile_removed_after_last_reference_replaced(self) -> None:
        seeded = _call(
            aflow_text=self.SEEDED_AFLOW,
            workflows_text=STARTER_WORKFLOWS,
            action=SetGlobalRoleAction(
                type="set_global_role", role="worker", selector="codex.real"
            ),
        )
        assert "[harness.starter" not in seeded.aflow_toml
        assert 'model = "FILL_IN_MODEL"' not in seeded.aflow_toml
        # The explanatory comment above the removed table survives.
        assert "placeholder harness profile" in seeded.aflow_toml.lower()
        assert seeded.form is not None
        assert "starter" not in seeded.form.harnesses

    def test_placeholder_kept_while_still_referenced(self) -> None:
        seeded = _call(
            aflow_text=self.SEEDED_AFLOW,
            workflows_text=STARTER_WORKFLOWS,
            action=SetGlobalRoleAction(
                type="set_global_role", role="reviewer", selector="codex.real"
            ),
        )
        assert 'worker = "starter.default"' in seeded.aflow_toml
        assert "[harness.starter.profiles.default]" in seeded.aflow_toml

    def test_modified_placeholder_profile_is_never_deleted(self) -> None:
        modified_starter = STARTER_AFLOW.replace(
            'model = "FILL_IN_MODEL"', 'model = "FILL_IN_MODEL"\neffort = "high"'
        )
        seeded = _call(
            aflow_text=modified_starter
            + '\n[harness.codex.profiles.real]\nmodel = "m"\n',
            workflows_text=STARTER_WORKFLOWS,
            action=SetGlobalRoleAction(
                type="set_global_role", role="worker", selector="codex.real"
            ),
        )
        assert "[harness.starter.profiles.default]" in seeded.aflow_toml

    def test_unrelated_keys_keep_starter_parents(self) -> None:
        seeded = _call(
            aflow_text=STARTER_AFLOW
            + '\n[harness.starter.profiles.extra]\nmodel = "FILL_IN_MODEL"\n'
            + '\n[harness.codex.profiles.real]\nmodel = "m"\n',
            workflows_text=STARTER_WORKFLOWS,
            action=SetGlobalRoleAction(
                type="set_global_role", role="worker", selector="codex.real"
            ),
        )
        assert "[harness.starter.profiles.extra]" in seeded.aflow_toml


class TestTeamUpgradeChains:
    """Typed upgrade-link edits keep recovery links and graph checks intact."""

    TWO_TEAMS = AFLOW_TEXT + '\n[teams.second.roles]\nworker = "codex.fast"\n'
    LINKED = AFLOW_TEXT.replace(
        "[teams.crew.roles]",
        '[teams.crew]\nupgrade_to = "second"\nbackup_team = "second"\n\n[teams.crew.roles]',
        1,
    ) + '\n[teams.second.roles]\nworker = "codex.fast"\n'

    def test_set_upgrade_link_updates_projection_and_bytes(self) -> None:
        response = _call(
            aflow_text=self.TWO_TEAMS,
            action=SetTeamUpgradeAction(
                type="set_team_upgrade", team="crew", upgrade_to="second"
            ),
        )
        assert '[teams.crew]\nupgrade_to = "second"' in response.aflow_toml
        assert response.form is not None
        assert response.form.teams["crew"].upgrade_to == "second"
        assert response.form.teams["second"].upgrade_to is None
        assert response.validation.state == "ready"

    def test_unset_upgrade_link_removes_only_that_link(self) -> None:
        response = _call(
            aflow_text=self.LINKED,
            action=SetTeamUpgradeAction(
                type="set_team_upgrade", team="crew", upgrade_to=None
            ),
        )
        assert "upgrade_to" not in response.aflow_toml
        assert 'backup_team = "second"' in response.aflow_toml
        assert response.form is not None
        assert response.form.teams["crew"].upgrade_to is None

    def test_self_link_is_rejected(self) -> None:
        with pytest.raises(GuidedConfigError) as excinfo:
            _call(
                aflow_text=self.TWO_TEAMS,
                action=SetTeamUpgradeAction(
                    type="set_team_upgrade", team="crew", upgrade_to="crew"
                ),
            )
        assert excinfo.value.code == "invalid_action_value"

    def test_unknown_target_is_rejected(self) -> None:
        with pytest.raises(GuidedConfigError) as excinfo:
            _call(
                aflow_text=self.TWO_TEAMS,
                action=SetTeamUpgradeAction(
                    type="set_team_upgrade", team="crew", upgrade_to="ghost"
                ),
            )
        assert excinfo.value.code == "unknown_team"

    def test_cycle_is_reported_invalid_by_the_production_checks(self) -> None:
        # crew already upgrades to second; linking second back to crew closes
        # the cycle, which only the production graph checks may flag.
        response = _call(
            aflow_text=self.LINKED,
            action=SetTeamUpgradeAction(
                type="set_team_upgrade", team="second", upgrade_to="crew"
            ),
        )
        assert response.changed is True
        assert response.validation.state == "invalid"
        assert any(
            "cycle" in issue.message.lower() or "upgrade" in issue.message.lower()
            for issue in response.validation.issues
        )
