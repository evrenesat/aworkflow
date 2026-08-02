"""Application-facing planning service."""

from __future__ import annotations

from .attachment_store import AttachmentStore
from .models import (
    ApprovalDecision,
    PendingApproval,
    ProviderReadiness,
    Session,
    SessionKey,
    StartSessionRequest,
    StartTurnRequest,
    Turn,
)
from .provider import AuthorizedProjectContext
from .registry import ProviderRegistry


class PlanningService:
    """Provider-neutral facade used by current and future HTTP routes."""

    def __init__(
        self,
        registry: ProviderRegistry,
        *,
        default_provider_id: str | None,
        attachment_store: AttachmentStore | None = None,
    ) -> None:
        self.registry = registry
        self.default_provider_id = default_provider_id
        self.attachment_store = attachment_store

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

    def _provider(self, provider_id: str | None = None):
        resolved = provider_id or self.default_provider_id
        if resolved is None:
            return self.registry.get("")
        return self.registry.get(resolved)

    async def list_models(self, provider_id: str | None = None) -> tuple[str, ...]:
        return await self._provider(provider_id).list_models()

    def pending_approvals(
        self, provider_id: str | None = None
    ) -> tuple[PendingApproval, ...]:
        return self._provider(provider_id).pending_approvals

    async def read_session(self, key: SessionKey, *, include_turns: bool = True) -> Session:
        return await self._provider(key.provider_id).read_session(
            key, include_turns=include_turns
        )

    async def start_session(
        self,
        context: AuthorizedProjectContext,
        request: StartSessionRequest,
        *,
        provider_id: str | None = None,
    ) -> Session:
        return await self._provider(provider_id).start_session(context, request)

    async def resume_session(self, key: SessionKey, *, cwd: str) -> Session:
        return await self._provider(key.provider_id).resume_session(key, cwd=cwd)

    async def fork_session(self, key: SessionKey, *, cwd: str) -> Session:
        return await self._provider(key.provider_id).fork_session(key, cwd=cwd)

    async def start_turn(
        self,
        key: SessionKey,
        request: StartTurnRequest,
        *,
        context: AuthorizedProjectContext | None = None,
    ) -> Turn:
        return await self._provider(key.provider_id).start_turn(
            key, request, context=context
        )

    async def set_session_name(self, key: SessionKey, name: str) -> None:
        await self._provider(key.provider_id).set_session_name(key, name)

    async def set_archived(self, key: SessionKey, *, archived: bool) -> None:
        await self._provider(key.provider_id).set_archived(key, archived=archived)

    async def interrupt_turn(self, key: SessionKey, turn_id: str) -> None:
        await self._provider(key.provider_id).interrupt_turn(key, turn_id)

    async def respond_to_approval(
        self, key: SessionKey, decision: ApprovalDecision
    ) -> None:
        await self._provider(key.provider_id).respond_to_approval(key, decision)
