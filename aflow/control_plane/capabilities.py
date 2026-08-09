"""Frozen capability discovery derived from the existing AFlow configuration."""

from __future__ import annotations

from pathlib import Path

from aflow.config import WorkflowUserConfig, load_workflow_config
from aflow.harnesses import ADAPTERS

from .models import CapabilitySet


class CapabilityError(ValueError):
    """The loaded configuration cannot produce a safe capability response."""


class CapabilityService:
    """Translate loaded configuration into one stable, transport-neutral model."""

    def __init__(
        self,
        config: WorkflowUserConfig | None = None,
        *,
        config_path: Path | None = None,
        service_features: tuple[str, ...] = (
            "run_repository",
            "capabilities",
            "controls",
            "context",
            "reconciliation",
            "unit_manager",
        ),
    ) -> None:
        if config is None and config_path is None:
            raise CapabilityError("config or config_path is required")
        self._config = config
        self._config_path = Path(config_path) if config_path is not None else None
        self._service_features = tuple(sorted(set(service_features)))

    def get(self) -> CapabilitySet:
        config = self._config or load_workflow_config(self._config_path)  # type: ignore[arg-type]
        team_chains = {
            team: self._team_chain(config, team)
            for team in sorted(config.teams)
        }
        roles = set(config.roles)
        for team in config.teams.values():
            roles.update(team.roles)
        controls = ("max_turns", "owner_stop", "team", "role_selectors")
        return CapabilitySet(
            workflows=tuple(sorted(config.workflows)),
            teams=tuple(sorted(config.teams)),
            roles=tuple(sorted(roles)),
            controls=controls,
            team_upgrade_chains=team_chains,
            control_safety={
                "max_turns": "safe",
                "owner_stop": "safe",
                "team": "safe",
                "role_selectors": "safe",
                "workflow": "restart_required",
                "plan": "restart_required",
                "repository": "restart_required",
                "lifecycle": "restart_required",
            },
            service_features=tuple(
                sorted(
                    set(self._service_features)
                    | {f"adapter:{name}" for name in config.harnesses if name in ADAPTERS}
                )
            ),
        )

    def _team_chain(self, config: WorkflowUserConfig, team: str) -> tuple[str, ...]:
        chain: list[str] = []
        seen: set[str] = set()
        current: str | None = team
        while current is not None:
            if current in seen:
                raise CapabilityError(f"team upgrade cycle detected at '{current}'")
            current_config = config.teams.get(current)
            if current_config is None:
                raise CapabilityError(f"team '{current}' referenced by upgrade chain is unknown")
            seen.add(current)
            chain.append(current)
            current = current_config.upgrade_to
        return tuple(chain)
