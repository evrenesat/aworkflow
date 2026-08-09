"""One composition root for transports that need the shared control-plane layer."""

from __future__ import annotations

from dataclasses import dataclass

from .capabilities import CapabilityService
from .reconciliation import ReconciliationService
from .repository import RunRepository
from .services import ContextService, ControlService, StartupQuestionService
from .units import UnitManager


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
