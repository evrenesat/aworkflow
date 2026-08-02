"""Application-facing planning service."""

from __future__ import annotations

from .models import ProviderReadiness, Session
from .registry import ProviderRegistry


class PlanningService:
    """Provider-neutral facade used by current and future HTTP routes."""

    def __init__(self, registry: ProviderRegistry, *, default_provider_id: str | None) -> None:
        self.registry = registry
        self.default_provider_id = default_provider_id

    async def provider_statuses(self) -> tuple[ProviderReadiness, ...]:
        return await self.registry.readiness()

    async def list_sessions(
        self,
        *,
        provider_id: str | None = None,
        cwd: str | None = None,
        archived: bool | None = None,
    ) -> tuple[tuple[Session, ...], tuple[ProviderReadiness, ...]]:
        return await self.registry.list_sessions(
            provider_id=provider_id, cwd=cwd, archived=archived
        )
