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

from aflow.config import load_workflow_config, render_starter_documents

from aflow_app_server.guided_config import GuidedConfigError, guided_form_response
from aflow_app_server.models import (
    AddTeamAction,
    BuildStarterAction,
    ProjectConfigFormResponse,
    SetDefaultWorkflowAction,
    SetGlobalRoleAction,
    SetMaxTurnsAction,
    SetTeamRoleAction,
    SetWorkflowDefaultTeamAction,
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
