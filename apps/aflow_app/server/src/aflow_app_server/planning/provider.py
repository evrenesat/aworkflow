"""Provider interface for planning-session backends."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from .models import (
    ApprovalDecision,
    PendingApproval,
    PlanningError,
    ProviderCapabilities,
    ProviderReadiness,
    Session,
    SessionKey,
    SessionPage,
    StartSessionRequest,
    StartTurnRequest,
    Turn,
)


class ProviderOperationError(Exception):
    """Provider-neutral exception carrying only a bounded client-safe error."""

    def __init__(self, error: PlanningError) -> None:
        super().__init__(error.message)
        self.error = error


@dataclass(frozen=True)
class AuthorizedProjectContext:
    """Server-owned project identity and catalog-authorized working directory."""

    project_id: str
    cwd: str


class PlanningProvider(ABC):
    """One long-lived backend adapter owned by the application registry."""

    @property
    @abstractmethod
    def provider_id(self) -> str:
        """Return the stable path-safe provider id."""

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Return the user-facing provider name."""

    @property
    @abstractmethod
    def capabilities(self) -> ProviderCapabilities:
        """Return capabilities without performing provider I/O."""

    @abstractmethod
    async def start(self) -> None:
        """Initialize provider resources once during application startup."""

    @abstractmethod
    async def close(self) -> None:
        """Close provider resources once during application shutdown."""

    @abstractmethod
    async def readiness(self) -> ProviderReadiness:
        """Report current provider readiness using the safe app contract."""

    async def list_models(self) -> tuple[str, ...]:
        raise NotImplementedError

    @property
    def pending_approvals(self) -> tuple[PendingApproval, ...]:
        return ()

    @abstractmethod
    async def list_sessions(
        self,
        *,
        cwd: str | None = None,
        cursor: str | None = None,
        archived: bool | None = None,
    ) -> SessionPage:
        """List sessions owned by this provider."""

    async def read_session(self, key: SessionKey, *, include_turns: bool = True) -> Session:
        raise NotImplementedError

    async def start_session(
        self,
        context: AuthorizedProjectContext,
        request: StartSessionRequest,
    ) -> Session:
        """Start a session in a server-authorized project location.

        Adapters must reversibly encode native session ids that are not safe as
        one URL path segment before constructing an app-facing ``SessionKey``.
        Route and storage layers treat the encoded id as opaque.
        """
        raise NotImplementedError

    async def resume_session(self, key: SessionKey, *, cwd: str) -> Session:
        raise NotImplementedError

    async def fork_session(self, key: SessionKey, *, cwd: str) -> Session:
        raise NotImplementedError

    async def start_turn(
        self,
        key: SessionKey,
        request: StartTurnRequest,
        *,
        context: AuthorizedProjectContext | None = None,
    ) -> Turn:
        raise NotImplementedError

    async def set_session_name(self, key: SessionKey, name: str) -> None:
        raise NotImplementedError

    async def set_archived(self, key: SessionKey, *, archived: bool) -> None:
        raise NotImplementedError

    async def interrupt_turn(self, key: SessionKey, turn_id: str) -> None:
        raise NotImplementedError

    async def respond_to_approval(self, key: SessionKey, decision: ApprovalDecision) -> None:
        raise NotImplementedError
