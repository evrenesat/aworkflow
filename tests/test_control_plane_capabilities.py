from __future__ import annotations

import subprocess

import pytest

from aflow.config import (
    HarnessProfileConfig,
    TeamConfig,
    WorkflowConfig,
    WorkflowHarnessConfig,
    WorkflowStepConfig,
    WorkflowUserConfig,
)
from aflow.control_plane import CapabilityError, CapabilityService


def test_capabilities_are_derived_from_config_with_full_upgrade_chains() -> None:
    config = WorkflowUserConfig(
        harnesses={"codex": WorkflowHarnessConfig(profiles={"high": HarnessProfileConfig(model="gpt")})},
        roles={"worker": "codex.high"},
        teams={
            "base": TeamConfig(roles={"worker": "codex.high"}, upgrade_to="strong"),
            "strong": TeamConfig(roles={"reviewer": "codex.high"}, upgrade_to="max"),
            "max": TeamConfig(roles={"lead": "codex.high"}),
        },
        workflows={
            "managed": WorkflowConfig(
                declared_steps={
                    "plan": WorkflowStepConfig(role="worker"),
                    "implement": WorkflowStepConfig(role="worker"),
                },
                steps={"implement": WorkflowStepConfig(role="worker")},
                first_step="implement",
                excluded_steps=("plan",),
                team="base",
            )
        },
    )

    capabilities = CapabilityService(config).get()

    assert capabilities.workflows == ("managed",)
    assert capabilities.roles == ("lead", "reviewer", "worker")
    assert capabilities.team_upgrade_chains["base"] == ("base", "strong", "max")
    assert capabilities.control_safety["workflow"] == "restart_required"
    details = capabilities.workflow_details["managed"]
    assert details.declared_steps == ("plan", "implement")
    assert details.executable_steps == ("implement",)
    assert details.excluded_steps == ("plan",)
    assert details.first_step == "implement"
    assert details.default_team == "base"
    assert capabilities.admitted_role_selectors["worker"] == ("codex.high",)
    assert "owner_stopped" in capabilities.status_values
    assert "adapter:codex" in capabilities.service_features
    assert capabilities.publication.available is False
    assert capabilities.publication.publish_remote is None
    assert capabilities.publication.publish_branch is None


def test_capabilities_expose_project_publication_settings(tmp_path) -> None:
    root = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "--initial-branch=main", str(root)], check=True)
    subprocess.run(
        ["git", "-C", str(root), "config", "aflow.publishRemote", "origin"], check=True
    )
    subprocess.run(
        ["git", "-C", str(root), "config", "aflow.publishBranch", "main"], check=True
    )
    config = WorkflowUserConfig()
    capabilities = CapabilityService(config, project_root=root).get()
    assert capabilities.publication.available is True
    assert capabilities.publication.publish_remote == "origin"
    assert capabilities.publication.publish_branch == "main"
    payload = capabilities.to_dict()
    assert payload["publication"] == {
        "available": True,
        "publish_remote": "origin",
        "publish_branch": "main",
    }


def test_capabilities_reject_upgrade_cycles() -> None:
    config = WorkflowUserConfig(
        teams={"one": TeamConfig(upgrade_to="two"), "two": TeamConfig(upgrade_to="one")}
    )

    with pytest.raises(CapabilityError, match="cycle"):
        CapabilityService(config).get()
