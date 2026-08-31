"""Provider-neutral interactive session contracts for capability-gated drivers."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Mapping, Protocol

from .base import HarnessInvocation


@dataclass(frozen=True)
class SessionCapabilities:
    session_identity: bool = False
    followup_turn: bool = False
    resume_with_model: bool = False
    mid_turn_steer: bool = False
    read_only_teardown: bool = False
    idempotent_turn_start: bool = False


NO_SESSION_CAPABILITIES = SessionCapabilities()


@dataclass(frozen=True)
class SessionRequest:
    repo_root: Path
    selector: str
    model: str | None
    effort: str | None
    system_prompt: str
    user_prompt: str
    session_id: str | None = None
    idempotency_key: str | None = None


@dataclass(frozen=True)
class SessionResult:
    session_id: str
    selector: str
    model: str | None
    effort: str | None
    final_output: str
    structured_events: tuple[Mapping[str, Any], ...] = ()
    provider_operation_id: str | None = None
    idempotency_key: str | None = None
    capabilities: SessionCapabilities = NO_SESSION_CAPABILITIES
    failure: str | None = None


@dataclass(frozen=True)
class SessionExecutionResult:
    """Result of an owned session turn, keeping semantic and raw channels separate."""

    result: SessionResult
    raw_transport: str
    events: tuple[Mapping[str, Any], ...] = ()


class SessionDriver(Protocol):
    capabilities: SessionCapabilities

    def build_invocation(self, request: SessionRequest) -> HarnessInvocation:
        ...

    def parse_result(
        self, request: SessionRequest, stdout: str, *, returncode: int = 0
    ) -> SessionResult:
        ...

    def execute_session(
        self,
        request: SessionRequest,
        invocation: HarnessInvocation,
        control_callback: Any | None = None,
    ) -> SessionExecutionResult:
        ...


class NoSessionDriver:
    capabilities = NO_SESSION_CAPABILITIES

    def build_invocation(self, request: SessionRequest) -> HarnessInvocation:
        raise RuntimeError("this harness does not advertise interactive sessions")

    def parse_result(
        self, request: SessionRequest, stdout: str, *, returncode: int = 0
    ) -> SessionResult:
        raise RuntimeError("this harness does not advertise interactive sessions")


def _event_session_id(event: Mapping[str, Any]) -> str | None:
    for key in ("thread_id", "threadId", "session_id", "sessionId", "conversation_id"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return value
    for key in ("thread", "session", "conversation"):
        nested = event.get(key)
        if isinstance(nested, Mapping):
            found = _event_session_id(nested)
            if found:
                return found
    return None


def _event_operation_id(event: Mapping[str, Any]) -> str | None:
    for key in ("operation_id", "operationId", "idempotency_key"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _event_text(event: Mapping[str, Any]) -> str | None:
    for key in ("text", "message", "content", "output", "final_output", "result", "item"):
        value = event.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, Mapping):
            nested = _event_text(value)
            if nested is not None:
                return nested
        if isinstance(value, list):
            parts = [item for item in value if isinstance(item, str)]
            if parts:
                return "".join(parts)
    return None


def parse_jsonl_events(stdout: str) -> tuple[Mapping[str, Any], ...]:
    events: list[Mapping[str, Any]] = []
    for line_number, line in enumerate(stdout.splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"session event line {line_number} is not JSON") from exc
        if not isinstance(value, Mapping):
            raise ValueError(f"session event line {line_number} is not an object")
        events.append(value)
    return tuple(events)


def parse_structured_result(
    request: SessionRequest,
    stdout: str,
    *,
    selector: str,
    capabilities: SessionCapabilities,
    returncode: int = 0,
) -> SessionResult:
    events = parse_jsonl_events(stdout)
    session_ids = {session_id for event in events if (session_id := _event_session_id(event))}
    if len(session_ids) != 1:
        raise ValueError("session output must contain exactly one invocation-specific session id")
    session_id = next(iter(session_ids))
    if request.session_id is not None and session_id != request.session_id:
        raise ValueError("session output id does not match the requested resumed session")
    final_parts = []
    for event in events:
        text = _event_text(event)
        event_type = event.get("type")
        if text and event_type in {
            "message.completed", "response.completed", "item.completed",
            "result", "final", "agent_message",
        }:
            final_parts.append(text)
    if not final_parts:
        raise ValueError("session output did not contain a structured final response")
    operation_ids = {_event_operation_id(event) for event in events if _event_operation_id(event)}
    if len(operation_ids) > 1:
        raise ValueError("session output contains conflicting provider operation ids")
    failure = None if returncode == 0 else f"session exited with return code {returncode}"
    return SessionResult(
        session_id=session_id,
        selector=selector,
        model=request.model,
        effort=request.effort,
        final_output="\n".join(final_parts),
        structured_events=events,
        provider_operation_id=next(iter(operation_ids)) if operation_ids else None,
        idempotency_key=request.idempotency_key,
        capabilities=capabilities,
        failure=failure,
    )
