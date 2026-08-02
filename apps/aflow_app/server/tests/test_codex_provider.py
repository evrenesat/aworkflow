"""Focused contract tests for the SDK-backed Codex planning provider."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest
from codex_app_server_sdk import (
    CodexProtocolError,
    CommandApprovalRequest,
    ConversationStep,
)

from aflow_app_server.planning import (
    AuthorizedProjectContext,
    PlanningErrorCode,
    ProviderOperationError,
    ProviderState,
    SessionKey,
    StartSessionRequest,
    StartTurnRequest,
    TurnStatus,
)
from aflow_app_server.planning.models import ApprovalDecision
from aflow_app_server.planning.providers.codex import CodexProvider


@dataclass
class FakeThreadHandle:
    thread_id: str


class FakeCodexClient:
    def __init__(self) -> None:
        self.start_calls = 0
        self.initialize_calls = 0
        self.close_calls = 0
        self.handler = None
        self.thread_pages: dict[str | None, dict[str, Any]] = {
            None: {"data": [], "nextCursor": None}
        }
        self.model_pages: dict[str | None, dict[str, Any]] = {
            None: {"data": [{"id": "gpt-5.6"}], "nextCursor": None}
        }
        self.read_results: list[Any] = []
        self.started_configs: list[Any] = []
        self.resumed: list[tuple[str, Any]] = []
        self.forked: list[tuple[str, Any]] = []
        self.names: list[tuple[str, str]] = []
        self.archived: list[str] = []
        self.unarchived: list[str] = []
        self.interrupted: list[str] = []
        self.turn_overrides: list[Any] = []
        self.turn_started = asyncio.Event()
        self.release_turn = asyncio.Event()
        self.emit_first_step = True
        self.approval_before_step: CommandApprovalRequest | None = None
        self.approval_results: list[str] = []

    async def start(self):
        self.start_calls += 1
        return self

    async def initialize(self, *, timeout=None):
        self.initialize_calls += 1
        return object()

    async def close(self) -> None:
        self.close_calls += 1

    def set_approval_handler(self, handler) -> None:
        self.handler = handler

    async def list_models(self, *, cursor=None, **kwargs):
        return self.model_pages[cursor]

    async def list_threads(self, *, cursor=None, **kwargs):
        return self.thread_pages[cursor]

    async def read_thread(self, thread_id, *, include_turns=True):
        if self.read_results:
            result = self.read_results.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result
        return {"thread": _raw_thread(thread_id)}

    async def start_thread(self, config):
        self.started_configs.append(config)
        return FakeThreadHandle("thread-started")

    async def resume_thread(self, thread_id, *, overrides=None):
        self.resumed.append((thread_id, overrides))
        return FakeThreadHandle(thread_id)

    async def fork_thread(self, thread_id, *, overrides=None):
        self.forked.append((thread_id, overrides))
        return FakeThreadHandle("thread-forked")

    async def set_thread_name(self, thread_id, name):
        self.names.append((thread_id, name))

    async def archive_thread(self, thread_id):
        self.archived.append(thread_id)

    async def unarchive_thread(self, thread_id):
        self.unarchived.append(thread_id)

    async def interrupt_turn(self, turn_id, *, timeout=None):
        self.interrupted.append(turn_id)

    def chat(self, text, *, thread_id, turn_overrides, **kwargs):
        self.turn_overrides.append(turn_overrides)

        async def generate():
            self.turn_started.set()
            turn_id = "turn-1"
            if self.approval_before_step is not None:
                assert self.handler is not None
                self.approval_results.append(
                    await self.handler(self.approval_before_step)
                )
                turn_id = self.approval_before_step.turn_id
            if not self.emit_first_step:
                await self.release_turn.wait()
                return
            yield ConversationStep(
                thread_id=thread_id,
                turn_id=turn_id,
                step_type="thinking",
                text="working",
            )
            await self.release_turn.wait()
            yield ConversationStep(
                thread_id=thread_id,
                turn_id=turn_id,
                step_type="codex",
                item_type="agentMessage",
                text="done",
            )

        return generate()


def _raw_thread(thread_id: str, *, cwd: str = "/project", turns=None) -> dict[str, Any]:
    payload = {
        "id": thread_id,
        "cwd": cwd,
        "name": "Planning session",
        "preview": "hello",
        "status": {"type": "active", "activeFlags": []},
        "createdAt": "2026-08-02T10:00:00Z",
        "updatedAt": "2026-08-02T11:00:00Z",
    }
    if turns is not None:
        payload["turns"] = turns
    return payload


def _provider(client: FakeCodexClient, *, timeout: float = 1.0) -> CodexProvider:
    return CodexProvider(
        "codex",
        "Codex",
        server_url="ws://codex.example",
        server_token="secret",
        operation_timeout_seconds=timeout,
        client_factory=lambda _url, _token, _timeout: client,
    )


def test_provider_owns_one_sdk_client_for_its_lifespan() -> None:
    async def check() -> None:
        client = FakeCodexClient()
        provider = _provider(client)

        await asyncio.gather(provider.start(), provider.start())
        readiness = await provider.readiness()
        await provider.close()

        assert client.start_calls == 1
        assert client.initialize_calls == 1
        assert client.close_calls == 1
        assert readiness.state is ProviderState.READY
        assert readiness.capabilities.models == ("gpt-5.6",)
        assert readiness.capabilities.attachments is False
        assert readiness.capabilities.compaction is False
        assert readiness.capabilities.rollback is False
        assert client.handler is None

    asyncio.run(check())


def test_listing_paginates_and_encodes_unsafe_native_ids() -> None:
    async def check() -> None:
        client = FakeCodexClient()
        client.thread_pages = {
            None: {"data": [_raw_thread("native/id")], "nextCursor": "next"},
            "next": {"data": [_raw_thread("safe-id")], "nextCursor": None},
        }
        provider = _provider(client)
        await provider.start()

        page = await provider.list_sessions(cwd="/project")

        assert [session.provider_session_id for session in page.sessions] == [
            "b64.bmF0aXZlL2lk",
            "safe-id",
        ]
        unsafe = await provider.read_session(page.sessions[0].key, include_turns=False)
        assert unsafe.provider_session_id == "b64.bmF0aXZlL2lk"
        await provider.close()

    asyncio.run(check())


def test_read_normalizes_unmaterialized_session_to_empty_turns() -> None:
    async def check() -> None:
        client = FakeCodexClient()
        client.read_results = [
            CodexProtocolError("thread/read includeTurns unavailable until materialized"),
            {"thread": _raw_thread("new-thread")},
        ]
        provider = _provider(client)
        await provider.start()

        session = await provider.read_session(
            SessionKey(provider_id="codex", provider_session_id="new-thread")
        )

        assert session.turns == ()
        assert session.cwd == "/project"
        await provider.close()

    asyncio.run(check())


def test_session_lifecycle_uses_sdk_public_configuration() -> None:
    async def check() -> None:
        client = FakeCodexClient()
        provider = _provider(client)
        await provider.start()

        started = await provider.start_session(
            AuthorizedProjectContext(project_id="project-1", cwd="/project"),
            StartSessionRequest(model="gpt-5.6", reasoning_level="high"),
        )
        resumed = await provider.resume_session(started.key, cwd="/project")
        forked = await provider.fork_session(started.key, cwd="/project")
        await provider.set_session_name(started.key, "Named")
        await provider.set_archived(started.key, archived=True)
        await provider.set_archived(started.key, archived=False)

        config = client.started_configs[0]
        assert (config.cwd, config.model, config.approval_policy, config.sandbox) == (
            "/project",
            "gpt-5.6",
            "never",
            "danger-full-access",
        )
        assert started.project_id == "project-1"
        assert resumed.provider_session_id == "thread-started"
        assert forked.provider_session_id == "thread-forked"
        assert client.names == [("thread-started", "Named")]
        assert client.archived == ["thread-started"]
        assert client.unarchived == ["thread-started"]
        await provider.close()

    asyncio.run(check())


def test_turn_maps_reasoning_controls_and_interrupts_idempotently() -> None:
    async def check() -> None:
        client = FakeCodexClient()
        provider = _provider(client)
        await provider.start()
        key = SessionKey(provider_id="codex", provider_session_id="thread-1")

        turn = await provider.start_turn(
            key,
            StartTurnRequest(
                text="Plan this",
                model="gpt-5.6",
                reasoning_level="xhigh",
                reasoning_summary="detailed",
                output_schema={"type": "object"},
            ),
        )
        await provider.interrupt_turn(key, turn.turn_id)
        await provider.interrupt_turn(key, turn.turn_id)

        assert turn.status is TurnStatus.RUNNING
        overrides = client.turn_overrides[0]
        assert overrides.model == "gpt-5.6"
        assert overrides.effort == "xhigh"
        assert overrides.summary == "detailed"
        assert overrides.output_schema == {"type": "object"}
        assert client.interrupted == ["turn-1"]

        with pytest.raises(ProviderOperationError) as raised:
            await provider.interrupt_turn(
                SessionKey(provider_id="codex", provider_session_id="other"),
                turn.turn_id,
            )
        assert raised.value.error.code is PlanningErrorCode.CONFLICT
        await provider.close()

    asyncio.run(check())


def test_approval_decisions_reject_cross_session_and_replay() -> None:
    async def check() -> None:
        client = FakeCodexClient()
        client.approval_before_step = CommandApprovalRequest(
            request_id=7,
            approval_id="approval-1",
            thread_id="thread-1",
            turn_id="turn-1",
            item_id="item-1",
            reason="run command",
        )
        provider = _provider(client)
        await provider.start()
        assert client.handler is not None
        key = SessionKey(provider_id="codex", provider_session_id="thread-1")
        start_task = asyncio.create_task(
            provider.start_turn(key, StartTurnRequest(text="run it"))
        )
        for _ in range(10):
            if provider.pending_approvals:
                break
            await asyncio.sleep(0)
        assert provider.pending_approvals[0].approval_id == "approval-1"

        with pytest.raises(ProviderOperationError) as cross_session:
            await provider.respond_to_approval(
                SessionKey(provider_id="codex", provider_session_id="other"),
                ApprovalDecision(approval_id="approval-1", decision="accept"),
            )
        assert cross_session.value.error.code is PlanningErrorCode.CONFLICT
        with pytest.raises(ProviderOperationError) as cross_provider:
            await provider.respond_to_approval(
                SessionKey(provider_id="other", provider_session_id="thread-1"),
                ApprovalDecision(approval_id="approval-1", decision="accept"),
            )
        assert cross_provider.value.error.code is PlanningErrorCode.CONFLICT

        await provider.respond_to_approval(
            key, ApprovalDecision(approval_id="approval-1", decision="accept")
        )
        assert (await start_task).turn_id == "turn-1"
        assert client.approval_results == ["accept"]
        with pytest.raises(ProviderOperationError) as replay:
            await provider.respond_to_approval(
                key, ApprovalDecision(approval_id="approval-1", decision="decline")
            )
        assert replay.value.error.code is PlanningErrorCode.CONFLICT
        await provider.close()

    asyncio.run(check())


def test_unsolicited_approval_is_declined_without_becoming_pending() -> None:
    async def check() -> None:
        client = FakeCodexClient()
        provider = _provider(client)
        await provider.start()
        assert client.handler is not None

        result = await client.handler(
            CommandApprovalRequest(
                request_id=9,
                approval_id="approval-stale",
                thread_id="thread-1",
                turn_id="turn-stale",
                item_id="item-1",
            )
        )

        assert result == "decline"
        assert provider.pending_approvals == ()
        await provider.close()

    asyncio.run(check())


def test_approval_can_materialize_turn_before_first_conversation_step() -> None:
    async def check() -> None:
        client = FakeCodexClient()
        client.approval_before_step = CommandApprovalRequest(
            request_id=8,
            approval_id="approval-first",
            thread_id="thread-1",
            turn_id="turn-first",
            item_id="item-1",
        )
        provider = _provider(client)
        await provider.start()
        key = SessionKey(provider_id="codex", provider_session_id="thread-1")

        start_task = asyncio.create_task(
            provider.start_turn(key, StartTurnRequest(text="run it"))
        )
        for _ in range(10):
            if provider.pending_approvals:
                break
            await asyncio.sleep(0)
        turn = await start_task
        assert turn.turn_id == "turn-first"

        await provider.respond_to_approval(
            key,
            ApprovalDecision(approval_id="approval-first", decision="decline"),
        )
        await asyncio.sleep(0)
        assert client.approval_results == ["decline"]
        await provider.close()

    asyncio.run(check())


def test_invalid_provider_values_and_native_attachments_are_explicit() -> None:
    async def check() -> None:
        client = FakeCodexClient()
        provider = _provider(client)
        await provider.start()
        context = AuthorizedProjectContext(project_id="project-1", cwd="/project")

        with pytest.raises(ProviderOperationError) as model_error:
            await provider.start_session(context, StartSessionRequest(model="unknown"))
        assert model_error.value.error.code is PlanningErrorCode.INVALID_REQUEST

        key = SessionKey(provider_id="codex", provider_session_id="thread-1")
        with pytest.raises(ProviderOperationError) as reasoning_error:
            await provider.start_turn(
                key, StartTurnRequest(text="hello", reasoning_level="extreme")
            )
        assert reasoning_error.value.error.code is PlanningErrorCode.INVALID_REQUEST

        with pytest.raises(ProviderOperationError) as attachment_error:
            await provider.start_turn(
                key, StartTurnRequest(text="hello", attachment_ids=("file-1",))
            )
        assert attachment_error.value.error.code is PlanningErrorCode.CAPABILITY_UNSUPPORTED
        await provider.close()

    asyncio.run(check())


def test_timeout_and_shutdown_cancel_work_without_approving() -> None:
    class HangingStartClient(FakeCodexClient):
        async def start(self):
            await asyncio.Event().wait()

    async def check_timeout() -> None:
        provider = _provider(HangingStartClient(), timeout=0.01)
        with pytest.raises(TimeoutError):
            await provider.start()

    async def check_shutdown() -> None:
        client = FakeCodexClient()
        client.emit_first_step = False
        provider = _provider(client)
        await provider.start()
        key = SessionKey(provider_id="codex", provider_session_id="thread-1")
        start_task = asyncio.create_task(
            provider.start_turn(key, StartTurnRequest(text="hello"))
        )
        await client.turn_started.wait()
        chat_task = provider._starting_turns["thread-1"].task
        assert chat_task is not None
        await provider.close()
        with pytest.raises(ProviderOperationError) as closed:
            await start_task
        assert closed.value.error.code is PlanningErrorCode.PROVIDER_UNAVAILABLE
        assert chat_task.done()
        assert start_task.done()
        assert client.close_calls == 1
        assert provider._starting_turns == {}
        assert provider._tracked_turns == {}
        assert provider._interrupted_turns == set()
        assert provider.pending_approvals == ()
        await provider.close()
        assert client.close_calls == 1

    asyncio.run(check_timeout())
    asyncio.run(check_shutdown())


def test_completed_uninterrupted_turn_cannot_be_interrupted() -> None:
    async def check() -> None:
        client = FakeCodexClient()
        provider = _provider(client)
        await provider.start()
        key = SessionKey(provider_id="codex", provider_session_id="thread-1")

        turn = await provider.start_turn(key, StartTurnRequest(text="finish"))
        tracked = provider._tracked_turns[("thread-1", turn.turn_id)]
        client.release_turn.set()
        assert tracked.task is not None
        await tracked.task

        with pytest.raises(ProviderOperationError) as stale:
            await provider.interrupt_turn(key, turn.turn_id)
        assert stale.value.error.code is PlanningErrorCode.CONFLICT
        assert client.interrupted == []
        await provider.close()

    asyncio.run(check())
