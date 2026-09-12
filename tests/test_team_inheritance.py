"""Focused tests for team declarations, inheritance, and runtime resolution."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from aflow.config import (
    ConfigError,
    HarnessProfileConfig,
    ManagerConfig,
    TeamConfig,
    WorkflowConfig,
    WorkflowHarnessConfig,
    WorkflowStepConfig,
    WorkflowUserConfig,
    load_workflow_config,
    resolve_team_config,
)
from aflow.control_plane.validation import validate_override_targets
from aflow.live_config import load_live_config, load_live_config_for_run
from aflow.manager import eligible_implementation_upgrade, resolve_manager_role
from aflow.run_config_snapshot import create_run_config_snapshot
from aflow.run_state import PendingTeamOverride
from aflow.workflow import resolve_role_prompt, resolve_role_selector
from tests._support import _write_config, _write_split_config


def _resolver_config() -> WorkflowUserConfig:
    return WorkflowUserConfig(
        roles={
            "worker": "codex.global-worker",
            "reviewer": "codex.global-reviewer",
            "manager_lite": "codex.global-manager",
        },
        role_prompts={
            "worker": "global worker guidance",
            "reviewer": "global reviewer guidance",
        },
        teams={
            "base": TeamConfig(
                roles={
                    "reviewer": "codex.base-reviewer",
                    "manager_lite": "codex.base-manager",
                },
                role_prompts={
                    "worker": "base worker guidance",
                    "reviewer": "base reviewer guidance",
                },
                backup_team="fallback",
                upgrade_to="child",
            ),
            "child": TeamConfig(
                roles={"worker": "codex.child-worker"},
                role_prompts={"reviewer": "child reviewer guidance"},
                extends="base",
            ),
            "fallback": TeamConfig(roles={"worker": "codex.fallback-worker"}),
        },
    )


def test_team_config_resolves_roles_prompts_and_provenance_without_materializing() -> None:
    config = _resolver_config()

    resolved = resolve_team_config(config, "child")

    assert resolved.effective_roles == {
        "worker": "codex.child-worker",
        "reviewer": "codex.base-reviewer",
        "manager_lite": "codex.base-manager",
    }
    assert resolved.role_sources == {
        "worker": "child",
        "reviewer": "base",
        "manager_lite": "base",
    }
    assert resolved.effective_prompts == {
        "worker": "base worker guidance",
        "reviewer": "child reviewer guidance",
    }
    assert resolved.prompt_sources == {"worker": "base", "reviewer": "child"}
    assert resolved.roles == resolved.effective_roles
    assert resolved.role_prompts == resolved.effective_prompts

    # The resolver only projects effective values; declarations stay sparse and
    # routes remain direct fields on their declaring teams.
    assert config.teams["child"].roles == {"worker": "codex.child-worker"}
    assert config.teams["child"].role_prompts == {"reviewer": "child reviewer guidance"}
    assert config.teams["child"].backup_team is None
    assert config.teams["child"].upgrade_to is None


def test_inherited_prompt_presence_beats_parent_even_when_explicitly_empty() -> None:
    config = _resolver_config()
    config.teams["child"] = replace(config.teams["child"], role_prompts={"worker": ""})

    resolved = resolve_team_config(config, "child")

    assert resolved.effective_prompts["worker"] == ""
    assert resolved.prompt_sources["worker"] == "child"


def test_toml_parses_metadata_and_reserves_it_from_inline_roles(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        '[harness.codex.profiles.default]\nmodel = "model"\n\n'
        '[roles]\nworker = "codex.default"\n\n'
        '[teams.base]\ndisplay_name = "  Product base  "\n\n'
        '[teams.child]\nextends = "base"\ndisplay_name = "  Stronger worker  "\n'
        'worker = "codex.default"\n',
    )

    config = load_workflow_config(config_path)

    assert config.teams["base"].display_name == "Product base"
    assert config.teams["child"].display_name == "Stronger worker"
    assert config.teams["child"].extends == "base"
    assert config.teams["child"].roles == {"worker": "codex.default"}


@pytest.mark.parametrize(
    ("team_text", "message"),
    [
        (
            '[teams.child]\nextends = "missing"\n',
            "teams.child.extends references unknown team 'missing'",
        ),
        (
            '[teams.child]\nextends = "child"\n',
            "teams.child.extends forms a cycle",
        ),
        (
            '[teams.base]\nextends = "child"\n\n[teams.child]\nextends = "base"\n',
            "teams.base.extends forms a cycle",
        ),
        (
            '[teams.base]\n\n[teams.child]\nextends = "base"\n\n'
            '[teams.grandchild]\nextends = "child"\n',
            "teams.grandchild.extends references child team 'child'",
        ),
    ],
)
def test_invalid_team_inheritance_is_rejected_before_runtime(
    tmp_path: Path, team_text: str, message: str
) -> None:
    config_path = _write_config(tmp_path, team_text)

    with pytest.raises(ConfigError, match=message):
        load_workflow_config(config_path)


@pytest.mark.parametrize(
    "display_name",
    ["", " ", "a" * 129],
)
def test_invalid_team_display_name_is_rejected(tmp_path: Path, display_name: str) -> None:
    config_path = _write_config(
        tmp_path,
        f'[teams.team]\ndisplay_name = "{display_name}"\n',
    )

    with pytest.raises(ConfigError, match="teams.team.display_name"):
        load_workflow_config(config_path)


def test_runtime_manager_and_control_validation_use_inherited_roles() -> None:
    config = _resolver_config()
    workflow = WorkflowConfig(
        steps={
            "implement": WorkflowStepConfig(
                role="worker",
                go=(),
            )
        },
        first_step="implement",
        manager_enabled=True,
    )
    config = replace(
        config,
        manager=ManagerConfig(lite_role="manager_lite"),
        workflows={"managed": workflow},
        harnesses={
            "codex": WorkflowHarnessConfig(
                profiles={
                    "global-worker": HarnessProfileConfig(model="worker"),
                    "global-reviewer": HarnessProfileConfig(model="reviewer"),
                    "global-manager": HarnessProfileConfig(model="manager"),
                    "base-reviewer": HarnessProfileConfig(model="base-reviewer"),
                    "base-manager": HarnessProfileConfig(model="base-manager"),
                    "child-worker": HarnessProfileConfig(model="child-worker"),
                    "fallback-worker": HarnessProfileConfig(model="fallback-worker"),
                }
            )
        },
    )

    assert resolve_role_selector("reviewer", "child", config) == "codex.base-reviewer"
    assert resolve_role_prompt("worker", "child", config) == "base worker guidance"
    assert resolve_manager_role(
        config, level="lite", baseline_team="child", workflow_name="managed"
    ).selector == "codex.base-manager"

    upgrade = eligible_implementation_upgrade(
        config, role="worker", baseline_team="base"
    )
    assert upgrade.available is True
    assert upgrade.source_selector == "codex.global-worker"
    assert upgrade.target_selector == "codex.child-worker"

    validate_override_targets(
        config,
        workflow_name="managed",
        step_name="implement",
        team="child",
    )


def test_run_local_and_pending_overrides_stay_above_team_inheritance() -> None:
    config = _resolver_config()
    pending = PendingTeamOverride(
        target_step="implement",
        role="worker",
        source_team="base",
        target_team="child",
        selector="codex.pending-worker",
        checkpoint_identity=None,
        decision_number=1,
    )

    assert (
        resolve_role_selector(
            "worker",
            "child",
            config,
            step_name="implement",
            run_local_role_selectors={"worker": "codex.run-local-worker"},
            pending_team_override=pending,
        )
        == "codex.pending-worker"
    )
    assert (
        resolve_role_selector(
            "worker",
            "child",
            config,
            step_name="implement",
            run_local_role_selectors={"worker": "codex.run-local-worker"},
            pending_team_override=replace(pending, consumed=True),
        )
        == "codex.run-local-worker"
    )


_INHERITED_AFLOW = """\
[harness.codex.profiles.global]
model = "global-worker"

[harness.codex.profiles.base-reviewer]
model = "base-reviewer"

[harness.codex.profiles.child-worker]
model = "child-worker"

[roles]
worker = "codex.global"
reviewer = "codex.global"

[roles.prompts]
worker = "Global worker guidance."

[teams.base]
display_name = "Product base"

[teams.base.roles]
reviewer = "codex.base-reviewer"

[teams.child]
display_name = "Stronger worker"
extends = "base"

[teams.child.roles]
worker = "codex.child-worker"

[prompts]
p = "Work."
"""

_INHERITED_WORKFLOWS = """\
[workflow.simple.steps.implement]
role = "worker"
prompts = ["p"]
go = [{ to = "END" }]
"""


def test_live_load_and_diagnostic_snapshot_keep_family_metadata_and_fresh_source(
    tmp_path: Path,
) -> None:
    config_path, _ = _write_split_config(
        tmp_path / "source", _INHERITED_AFLOW, _INHERITED_WORKFLOWS
    )

    loaded = load_live_config(config_path)
    assert loaded.workflow_config.teams["child"].extends == "base"
    assert loaded.workflow_config.teams["child"].display_name == "Stronger worker"
    assert resolve_role_selector("reviewer", "child", loaded.workflow_config) == (
        "codex.base-reviewer"
    )

    repo = tmp_path / "repo"
    snapshot = create_run_config_snapshot(
        repo_root=repo,
        run_id="family-run",
        config_path=config_path,
        workflow_name="simple",
    )
    snapshot_config = load_workflow_config(snapshot.config_path)
    assert snapshot_config.teams["child"].extends == "base"
    assert snapshot_config.teams["child"].display_name == "Stronger worker"

    updated = _INHERITED_AFLOW.replace("Stronger worker", "Updated worker")
    config_path.write_text(updated, encoding="utf-8")
    refreshed = load_live_config_for_run(repo, "family-run")
    assert refreshed.source.kind == "legacy_snapshot_origin"
    assert refreshed.workflow_config.teams["child"].display_name == "Updated worker"
    assert resolve_role_selector("reviewer", "child", refreshed.workflow_config) == (
        "codex.base-reviewer"
    )
