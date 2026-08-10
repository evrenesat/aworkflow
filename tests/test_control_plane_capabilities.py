from __future__ import annotations

import pytest

from aflow.config import (
    HarnessProfileConfig,
    TeamConfig,
    WorkflowConfig,
    WorkflowHarnessConfig,
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
        workflows={"managed": WorkflowConfig(first_step="implement")},
    )

    capabilities = CapabilityService(config).get()

    assert capabilities.workflows == ("managed",)
    assert capabilities.roles == ("lead", "reviewer", "worker")
    assert capabilities.team_upgrade_chains["base"] == ("base", "strong", "max")
    assert capabilities.control_safety["workflow"] == "restart_required"
    assert "adapter:codex" in capabilities.service_features


def test_capabilities_reject_upgrade_cycles() -> None:
    config = WorkflowUserConfig(
        teams={"one": TeamConfig(upgrade_to="two"), "two": TeamConfig(upgrade_to="one")}
    )

    with pytest.raises(CapabilityError, match="cycle"):
        CapabilityService(config).get()
