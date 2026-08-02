"""Tests for planning provider lifecycle and failure isolation."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from aflow_app_server.planning import (
    PlanningErrorCode,
    PlanningProvider,
    PlanningService,
    ProviderOperationError,
    ProviderCapabilities,
    ProviderReadiness,
    ProviderRegistry,
    ProviderState,
    Session,
    SessionKey,
    SessionPage,
    StartTurnRequest,
)
from aflow_app_server.planning.models import ApprovalDecision


@dataclass
class FakeProvider(PlanningProvider):
    _provider_id: str
    fail_listing: bool = False
    fail_starting: bool = False
    started: int = 0
    closed: int = 0
    listing_calls: int = 0
    model_calls: int = 0
    optional_calls: list[str] = field(default_factory=list)
    session_ids: tuple[str, ...] = ("shared-id",)
    _capabilities: ProviderCapabilities = field(
        default_factory=lambda: ProviderCapabilities(models=("test-model",))
    )

    @property
    def provider_id(self) -> str:
        return self._provider_id

    @property
    def display_name(self) -> str:
        return self.provider_id.title()

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    @property
    def pending_approvals(self):
        self.optional_calls.append("pending_approvals")
        return ()

    async def start(self) -> None:
        self.started += 1
        if self.fail_starting:
            raise RuntimeError("startup token=fake-secret /private/startup/path")

    async def close(self) -> None:
        self.closed += 1

    async def readiness(self) -> ProviderReadiness:
        return ProviderReadiness(
            provider_id=self.provider_id,
            display_name=self.display_name,
            state=ProviderState.READY,
            capabilities=self.capabilities,
        )

    async def list_sessions(self, **_: object) -> SessionPage:
        self.listing_calls += 1
        if self.fail_listing:
            raise RuntimeError("token=fake-secret /private/path")
        return SessionPage(
            sessions=tuple(
                Session(
                    key=SessionKey(
                        provider_id=self.provider_id,
                        provider_session_id=session_id,
                    ),
                    project_id="project-one",
                    cwd="/project",
                )
                for session_id in self.session_ids
            )
        )

    async def list_models(self) -> tuple[str, ...]:
        self.model_calls += 1
        return self.capabilities.models

    async def fork_session(self, *_: object, **__: object) -> Session:
        self.optional_calls.append("fork")
        raise AssertionError("unsupported fork was dispatched")

    async def start_turn(self, *_: object, **__: object):
        self.optional_calls.append("turn")
        raise AssertionError("unsupported turn feature was dispatched")

    async def set_archived(self, *_: object, **__: object) -> None:
        self.optional_calls.append("archive")
        raise AssertionError("unsupported archive was dispatched")

    async def interrupt_turn(self, *_: object, **__: object) -> None:
        self.optional_calls.append("interruption")
        raise AssertionError("unsupported interruption was dispatched")

    async def respond_to_approval(self, *_: object, **__: object) -> None:
        self.optional_calls.append("approvals")
        raise AssertionError("unsupported approval was dispatched")


def test_registry_owns_one_instance_for_application_lifespan() -> None:
    asyncio.run(_assert_registry_lifespan())


async def _assert_registry_lifespan() -> None:
    provider = FakeProvider("codex")
    registry = ProviderRegistry([provider])

    await registry.start()
    assert registry.get("codex") is provider
    assert provider.started == 1

    await registry.close()
    assert provider.closed == 1


def test_same_local_id_from_two_providers_does_not_collide() -> None:
    asyncio.run(_assert_provider_local_ids_do_not_collide())


async def _assert_provider_local_ids_do_not_collide() -> None:
    registry = ProviderRegistry([FakeProvider("codex"), FakeProvider("other")])
    await registry.start()

    sessions, statuses = await registry.list_sessions()

    assert {session.key for session in sessions} == {
        SessionKey(provider_id="codex", provider_session_id="shared-id"),
        SessionKey(provider_id="other", provider_session_id="shared-id"),
    }
    assert {status.state for status in statuses} == {ProviderState.READY}


def test_failed_provider_does_not_suppress_healthy_provider(caplog) -> None:
    asyncio.run(_assert_failed_provider_is_isolated(caplog))


async def _assert_failed_provider_is_isolated(caplog) -> None:
    registry = ProviderRegistry(
        [FakeProvider("healthy"), FakeProvider("failed", fail_listing=True)]
    )
    await registry.start()

    sessions, statuses = await registry.list_sessions()

    assert [session.provider_id for session in sessions] == ["healthy"]
    by_provider = {status.provider_id: status for status in statuses}
    assert by_provider["healthy"].state is ProviderState.READY
    assert by_provider["failed"].state is ProviderState.UNAVAILABLE
    assert by_provider["failed"].error is not None
    serialized = by_provider["failed"].error.model_dump_json()
    assert "fake-secret" not in serialized
    assert "/private/path" not in serialized
    assert "fake-secret" not in caplog.text
    assert "/private/path" not in caplog.text
    assert "exception_class=RuntimeError" in caplog.text


def test_startup_failure_blocks_dispatch_and_preserves_healthy_results(caplog) -> None:
    asyncio.run(_assert_startup_failure_blocks_dispatch(caplog))


async def _assert_startup_failure_blocks_dispatch(caplog) -> None:
    healthy = FakeProvider("healthy")
    failed = FakeProvider("failed", fail_starting=True)
    registry = ProviderRegistry([healthy, failed])
    await registry.start()

    sessions, statuses = await registry.list_sessions()

    assert [session.provider_id for session in sessions] == ["healthy"]
    assert healthy.listing_calls == 1
    assert failed.listing_calls == 0
    failed_status = {status.provider_id: status for status in statuses}["failed"]
    assert failed_status.state is ProviderState.UNAVAILABLE
    assert failed_status.error is not None
    assert failed_status.error.code is PlanningErrorCode.PROVIDER_UNAVAILABLE

    with pytest.raises(ProviderOperationError) as raised:
        await registry.list_sessions(provider_id="failed")
    assert raised.value.error == failed_status.error
    assert failed.listing_calls == 0
    assert "fake-secret" not in caplog.text
    assert "/private/startup/path" not in caplog.text
    assert "operation=start" in caplog.text
    assert "exception_class=RuntimeError" in caplog.text

    with pytest.raises(ProviderOperationError) as dispatch_error:
        registry.resolve_for_dispatch("failed")
    assert dispatch_error.value.error == failed_status.error

    service = PlanningService(registry, default_provider_id="healthy")
    with pytest.raises(ProviderOperationError) as model_error:
        await service.list_models("failed")
    assert model_error.value.error == failed_status.error
    assert failed.model_calls == 0
    with pytest.raises(ProviderOperationError) as capability_error:
        service.require_capability("failed", "fork")
    assert capability_error.value.error == failed_status.error


def test_service_rejects_optional_capabilities_before_provider_dispatch() -> None:
    asyncio.run(_assert_optional_capabilities_are_guarded())


async def _assert_optional_capabilities_are_guarded() -> None:
    provider = FakeProvider("limited")
    registry = ProviderRegistry([provider])
    await registry.start()
    service = PlanningService(registry, default_provider_id="limited")
    key = SessionKey(provider_id="limited", provider_session_id="shared-id")

    operations = (
        service.fork_session(key, cwd="/project"),
        service.set_archived(key, archived=True),
        service.interrupt_turn(key, "turn-1"),
        service.respond_to_approval(
            key, ApprovalDecision(approval_id="approval-1", decision="accept")
        ),
        service.start_turn(
            key, StartTurnRequest(text="hello", attachment_ids=("file-1",))
        ),
        service.start_turn(
            key, StartTurnRequest(text="hello", output_schema={"type": "object"})
        ),
    )
    for operation in operations:
        with pytest.raises(ProviderOperationError) as raised:
            await operation
        assert raised.value.error.code is PlanningErrorCode.CAPABILITY_UNSUPPORTED
        assert raised.value.error.provider_id == "limited"
        assert raised.value.error.retryable is False

    with pytest.raises(ProviderOperationError) as approvals_error:
        service.pending_approvals("limited")
    assert approvals_error.value.error.code is PlanningErrorCode.CAPABILITY_UNSUPPORTED
    with pytest.raises(ProviderOperationError) as attachments_error:
        service.require_capability("limited", "attachments")
    assert attachments_error.value.error.code is PlanningErrorCode.CAPABILITY_UNSUPPORTED
    assert provider.optional_calls == []


def test_registry_rejects_duplicate_provider_instances() -> None:
    with pytest.raises(ValueError, match="duplicate planning provider id"):
        ProviderRegistry([FakeProvider("codex"), FakeProvider("codex")])
