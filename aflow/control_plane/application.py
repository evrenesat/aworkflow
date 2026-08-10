"""One composition root for transports that need the shared control-plane layer."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .capabilities import CapabilityService
from .reconciliation import ReconciliationService
from .repository import RunRepository
from .services import ContextService, ControlService, StartupQuestionService
from .units import UnitManager
from .units import SystemdUnitManager


@dataclass(frozen=True)
class ControlPlaneApplication:
    """The transport-neutral services; handlers should depend on this, not disk."""

    repository: RunRepository
    capabilities: CapabilityService
    controls: ControlService
    context: ContextService
    startup_questions: StartupQuestionService
    reconciliation: ReconciliationService
    units: UnitManager


def compose_control_plane(
    repo_root: Path,
    *,
    config_path: Path,
    units: UnitManager | None = None,
) -> ControlPlaneApplication:
    """Create the one transport-neutral application graph for a project."""
    repository = RunRepository(repo_root)
    selected_units = units or SystemdUnitManager()
    capabilities = CapabilityService(
        config_path=config_path,
        service_features=(
            "run_repository",
            "capabilities",
            "controls",
            "context",
            "reconciliation",
            "unit_manager",
            "daemon_lifecycle",
            "journal_polling",
        ),
    )
    return ControlPlaneApplication(
        repository=repository,
        capabilities=capabilities,
        controls=ControlService(repository),
        context=ContextService(repository),
        startup_questions=StartupQuestionService(),
        reconciliation=ReconciliationService(repository, selected_units),
        units=selected_units,
    )
