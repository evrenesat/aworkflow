"""Lifecycle and failure-isolation registry for planning providers."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable

from .models import (
    PlanningError,
    PlanningErrorCode,
    ProviderCapabilities,
    ProviderReadiness,
    ProviderState,
    Session,
    SessionPage,
)
from .provider import PlanningProvider, ProviderOperationError

logger = logging.getLogger(__name__)


class ProviderRegistry:
    """Own exactly one instance of every configured planning provider."""

    def __init__(
        self,
        providers: Iterable[PlanningProvider],
        *,
        operation_timeout_seconds: float = 30.0,
    ) -> None:
        if operation_timeout_seconds <= 0:
            raise ValueError("operation_timeout_seconds must be greater than zero")
        self._timeout = operation_timeout_seconds
        self._providers: dict[str, PlanningProvider] = {}
        self._startup_errors: dict[str, PlanningError] = {}
        for provider in providers:
            if provider.provider_id in self._providers:
                raise ValueError(f"duplicate planning provider id: {provider.provider_id}")
            self._providers[provider.provider_id] = provider

    @property
    def provider_ids(self) -> tuple[str, ...]:
        return tuple(self._providers)

    def get(self, provider_id: str) -> PlanningProvider:
        try:
            return self._providers[provider_id]
        except KeyError as exc:
            raise ProviderOperationError(
                PlanningError(
                    code=PlanningErrorCode.PROVIDER_NOT_FOUND,
                    message="Planning provider was not found.",
                    provider_id=None,
                    retryable=False,
                )
            ) from exc

    def resolve_for_dispatch(self, provider_id: str) -> PlanningProvider:
        """Resolve a provider only when its application startup succeeded."""
        provider = self.get(provider_id)
        if startup_error := self._startup_errors.get(provider_id):
            raise ProviderOperationError(startup_error)
        return provider

    async def start(self) -> None:
        results = await asyncio.gather(
            *(self._start_provider(provider) for provider in self._providers.values())
        )
        self._startup_errors = {
            provider_id: error
            for provider_id, error in results
            if error is not None
        }

    async def _start_provider(
        self, provider: PlanningProvider
    ) -> tuple[str, PlanningError | None]:
        try:
            await asyncio.wait_for(provider.start(), timeout=self._timeout)
        except TimeoutError as exc:
            error = self._safe_error(
                provider.provider_id,
                PlanningErrorCode.PROVIDER_TIMEOUT,
                "Planning provider startup timed out.",
                retryable=True,
            )
            self._log_failure(provider.provider_id, "start", "timeout", exc)
            return provider.provider_id, error
        except ProviderOperationError as exc:
            self._log_failure(
                provider.provider_id, "start", "provider_failure", exc
            )
            return provider.provider_id, exc.error
        except Exception as exc:
            error = self._safe_error(
                provider.provider_id,
                PlanningErrorCode.PROVIDER_UNAVAILABLE,
                "Planning provider could not be started.",
                retryable=True,
            )
            self._log_failure(provider.provider_id, "start", "provider_failure", exc)
            return provider.provider_id, error
        return provider.provider_id, None

    async def close(self) -> None:
        results = await asyncio.gather(
            *(self._close_provider(provider) for provider in self._providers.values()),
            return_exceptions=True,
        )
        for provider_id, result in zip(self._providers, results, strict=True):
            if isinstance(result, BaseException):
                self._log_failure(provider_id, "close", "provider_failure", result)

    async def _close_provider(self, provider: PlanningProvider) -> None:
        await asyncio.wait_for(provider.close(), timeout=self._timeout)

    async def readiness(self) -> tuple[ProviderReadiness, ...]:
        return tuple(
            await asyncio.gather(
                *(self._provider_readiness(provider) for provider in self._providers.values())
            )
        )

    async def _provider_readiness(self, provider: PlanningProvider) -> ProviderReadiness:
        if error := self._startup_errors.get(provider.provider_id):
            return ProviderReadiness(
                provider_id=provider.provider_id,
                display_name=provider.display_name,
                state=ProviderState.UNAVAILABLE,
                capabilities=provider.capabilities,
                error=error,
            )
        try:
            return await asyncio.wait_for(provider.readiness(), timeout=self._timeout)
        except TimeoutError as exc:
            self._log_failure(provider.provider_id, "readiness", "timeout", exc)
            return ProviderReadiness(
                provider_id=provider.provider_id,
                display_name=provider.display_name,
                state=ProviderState.UNAVAILABLE,
                capabilities=provider.capabilities,
                error=self._safe_error(
                    provider.provider_id,
                    PlanningErrorCode.PROVIDER_TIMEOUT,
                    "Planning provider status timed out.",
                    retryable=True,
                ),
            )
        except Exception as exc:
            self._log_failure(
                provider.provider_id, "readiness", "provider_failure", exc
            )
            return ProviderReadiness(
                provider_id=provider.provider_id,
                display_name=provider.display_name,
                state=ProviderState.UNAVAILABLE,
                capabilities=provider.capabilities,
                error=self._safe_error(
                    provider.provider_id,
                    PlanningErrorCode.PROVIDER_UNAVAILABLE,
                    "Planning provider status is unavailable.",
                    retryable=True,
                ),
            )

    async def list_sessions(
        self,
        *,
        provider_id: str | None = None,
        cwd: str | None = None,
        archived: bool | None = None,
    ) -> tuple[tuple[Session, ...], tuple[ProviderReadiness, ...]]:
        """List sessions while isolating failures when enumerating all providers."""
        if provider_id is not None:
            provider = self.resolve_for_dispatch(provider_id)
            try:
                page = await asyncio.wait_for(
                    provider.list_sessions(cwd=cwd, archived=archived), timeout=self._timeout
                )
                self._validate_session_ownership(provider, page.sessions)
            except TimeoutError as exc:
                self._log_failure(
                    provider.provider_id, "list_sessions", "timeout", exc
                )
                raise ProviderOperationError(
                    self._safe_error(
                        provider.provider_id,
                        PlanningErrorCode.PROVIDER_TIMEOUT,
                        "Planning provider session listing timed out.",
                        retryable=True,
                    )
                ) from exc
            except ProviderOperationError:
                raise
            except Exception as exc:
                self._log_failure(
                    provider.provider_id,
                    "list_sessions",
                    "provider_failure",
                    exc,
                )
                raise ProviderOperationError(
                    self._safe_error(
                        provider.provider_id,
                        PlanningErrorCode.PROVIDER_UNAVAILABLE,
                        "Planning provider sessions are unavailable.",
                        retryable=True,
                    )
                ) from exc
            return page.sessions, (await self._provider_readiness(provider),)

        results = await asyncio.gather(
            *(self._list_provider_sessions(provider, cwd, archived) for provider in self._providers.values())
        )
        sessions: list[Session] = []
        statuses: list[ProviderReadiness] = []
        for provider_sessions, status in results:
            sessions.extend(provider_sessions)
            statuses.append(status)
        return tuple(sessions), tuple(statuses)

    async def _list_provider_sessions(
        self,
        provider: PlanningProvider,
        cwd: str | None,
        archived: bool | None,
    ) -> tuple[tuple[Session, ...], ProviderReadiness]:
        if error := self._startup_errors.get(provider.provider_id):
            return (), self._unavailable_readiness(provider, error)
        try:
            page = await asyncio.wait_for(
                provider.list_sessions(cwd=cwd, archived=archived), timeout=self._timeout
            )
            self._validate_session_ownership(provider, page.sessions)
            return page.sessions, await self._provider_readiness(provider)
        except TimeoutError as exc:
            self._log_failure(provider.provider_id, "list_sessions", "timeout", exc)
            code = PlanningErrorCode.PROVIDER_TIMEOUT
            message = "Planning provider session listing timed out."
        except Exception as exc:
            self._log_failure(
                provider.provider_id, "list_sessions", "provider_failure", exc
            )
            code = PlanningErrorCode.PROVIDER_UNAVAILABLE
            message = "Planning provider sessions are unavailable."
        error = self._safe_error(provider.provider_id, code, message, retryable=True)
        return (), self._unavailable_readiness(provider, error)

    @staticmethod
    def _unavailable_readiness(
        provider: PlanningProvider, error: PlanningError
    ) -> ProviderReadiness:
        return ProviderReadiness(
            provider_id=provider.provider_id,
            display_name=provider.display_name,
            state=ProviderState.UNAVAILABLE,
            capabilities=provider.capabilities,
            error=error,
        )

    @staticmethod
    def _log_failure(
        provider_id: str,
        operation: str,
        category: str,
        error: BaseException,
    ) -> None:
        """Log bounded diagnostics without exception messages or tracebacks."""
        logger.error(
            "planning_provider_failure provider_id=%s operation=%s category=%s "
            "exception_class=%s",
            provider_id,
            operation,
            category,
            type(error).__name__,
        )

    @staticmethod
    def _validate_session_ownership(
        provider: PlanningProvider, sessions: tuple[Session, ...]
    ) -> None:
        if any(session.provider_id != provider.provider_id for session in sessions):
            raise ValueError("provider returned a session owned by another provider")

    @staticmethod
    def _safe_error(
        provider_id: str,
        code: PlanningErrorCode,
        message: str,
        *,
        retryable: bool,
    ) -> PlanningError:
        return PlanningError(
            code=code,
            message=message,
            provider_id=provider_id,
            retryable=retryable,
        )


class UnavailablePlanningProvider(PlanningProvider):
    """Checkpoint-one placeholder for a configured adapter not installed yet."""

    def __init__(self, provider_id: str, display_name: str) -> None:
        self._provider_id = provider_id
        self._display_name = display_name

    @property
    def provider_id(self) -> str:
        return self._provider_id

    @property
    def display_name(self) -> str:
        return self._display_name

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities()

    async def start(self) -> None:
        raise ProviderOperationError(
            PlanningError(
                code=PlanningErrorCode.PROVIDER_UNAVAILABLE,
                message="Planning provider adapter is not available.",
                provider_id=self.provider_id,
                retryable=False,
            )
        )

    async def close(self) -> None:
        return None

    async def readiness(self) -> ProviderReadiness:
        return ProviderReadiness(
            provider_id=self.provider_id,
            display_name=self.display_name,
            state=ProviderState.UNAVAILABLE,
            capabilities=self.capabilities,
            error=PlanningError(
                code=PlanningErrorCode.PROVIDER_UNAVAILABLE,
                message="Planning provider adapter is not available.",
                provider_id=self.provider_id,
                retryable=False,
            ),
        )

    async def list_sessions(
        self,
        *,
        cwd: str | None = None,
        cursor: str | None = None,
        archived: bool | None = None,
    ) -> SessionPage:
        raise RuntimeError("planning provider adapter is not available")
