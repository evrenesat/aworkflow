"""Provider-neutral interactive session contracts for capability-gated drivers."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Final, Mapping, Protocol

from .base import HarnessInvocation
from ..stop_marker import (
    FINAL_TEXT_OUTPUT_SOURCE,
    SemanticOutputSource,
    STRUCTURED_TRANSPORT_OUTPUT_SOURCE,
    resolve_semantic_output_source,
)


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
    for key in (
        "text", "message", "content", "output", "output_text", "final_output",
        "result", "item", "response",
    ):
        value = event.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, Mapping):
            nested = _event_text(value)
            if nested is not None:
                return nested
        if isinstance(value, list):
            parts: list[str] = []
            for item in value:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, Mapping):
                    nested = _event_text(item)
                    if nested is not None:
                        parts.append(nested)
            if parts:
                return "".join(parts)
    return None


_FINAL_EVENT_TYPES: Final[frozenset[str]] = frozenset(
    {
        "message.completed", "response.completed", "item.completed",
        "result", "final", "agent_message", "assistant_message",
    }
)
_ASSISTANT_ITEM_TYPES: Final[frozenset[str]] = frozenset(
    {"agent_message", "assistant_message"}
)
_ASSISTANT_ROLE_VALUES: Final[frozenset[str]] = frozenset({"assistant", "agent"})
_NON_ASSISTANT_PAYLOAD_TYPES: Final[frozenset[str]] = frozenset(
    {
        "command_execution", "function_call", "function_result", "tool_call",
        "tool_result", "user_message", "system_message", "developer_message",
        "human", "user", "system", "developer", "prompt", "input",
    }
)
_SEMANTIC_NESTED_KEYS: Final[tuple[str, ...]] = (
    "message", "content", "output", "output_text", "final_output", "result",
    "item", "response", "messages",
)


def _contains_non_assistant_role(value: Any) -> bool:
    if isinstance(value, Mapping):
        role = value.get("role")
        if (
            isinstance(role, str)
            and role.strip().lower() not in _ASSISTANT_ROLE_VALUES
        ):
            return True
        return any(
            _contains_non_assistant_role(value[key])
            for key in _SEMANTIC_NESTED_KEYS
            if key in value
        )
    if isinstance(value, list):
        return any(_contains_non_assistant_role(item) for item in value)
    return False


def _contains_non_assistant_payload(value: Any) -> bool:
    if isinstance(value, Mapping):
        payload_type = value.get("type")
        if (
            isinstance(payload_type, str)
            and payload_type.strip().lower() in _NON_ASSISTANT_PAYLOAD_TYPES
        ):
            return True
        return any(
            _contains_non_assistant_payload(value[key])
            for key in _SEMANTIC_NESTED_KEYS
            if key in value
        )
    if isinstance(value, list):
        return any(_contains_non_assistant_payload(item) for item in value)
    return False


def extract_final_assistant_text(
    value: Any,
    *,
    require_final_event: bool = False,
) -> str | None:
    """Extract assistant text without treating tool or prompt payloads as final."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        candidates = [
            extract_final_assistant_text(item, require_final_event=require_final_event)
            for item in value
        ]
        return next((item for item in reversed(candidates) if item), None)
    if not isinstance(value, Mapping):
        return None

    event_type = value.get("type")
    if isinstance(event_type, str):
        event_type = event_type.strip().lower()
        if event_type in _NON_ASSISTANT_PAYLOAD_TYPES:
            return None
        if (
            event_type in _FINAL_EVENT_TYPES
            or require_final_event
        ) and (
            _contains_non_assistant_role(value)
            or _contains_non_assistant_payload(value)
        ):
            return None
        if event_type == "item.completed":
            item = value.get("item")
            if not isinstance(item, Mapping):
                return None
            item_type = item.get("type")
            if (
                not isinstance(item_type, str)
                or item_type.strip().lower() not in _ASSISTANT_ITEM_TYPES
            ):
                return None
            return _event_text(item)
        if event_type in _FINAL_EVENT_TYPES:
            return _event_text(value)
        if require_final_event:
            return None
    elif require_final_event:
        return None

    if value.get("role") is not None:
        if _contains_non_assistant_role(value) or _contains_non_assistant_payload(value):
            return None
        return _event_text(value)
    for key in _SEMANTIC_NESTED_KEYS:
        if key in value:
            candidate = extract_final_assistant_text(
                value[key], require_final_event=require_final_event
            )
            if candidate:
                return candidate
    return None


def extract_structured_final_assistant_text(stdout: str) -> str | None:
    """Return the latest accepted assistant result from structured stdout.

    Callers must establish that the source is structured transport before
    using this parser.  ``None`` means no JSON event was recognized.  An empty
    string means structured output was recognized but contained no accepted
    assistant result, so its nested tool and prompt payloads remain
    non-semantic.
    """
    candidates: list[Any] = []
    try:
        candidates.append(json.loads(stdout))
    except json.JSONDecodeError:
        pass
    # JSONL records are delimited by LF; Unicode line separators can be payload data.
    for line in stdout.split("\n"):
        try:
            candidates.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if not candidates:
        return None
    for candidate in reversed(candidates):
        result = extract_final_assistant_text(candidate)
        if result:
            return result
    return ""


def select_agent_semantic_output(
    stdout: str,
    *,
    output_source: SemanticOutputSource = FINAL_TEXT_OUTPUT_SOURCE,
) -> str:
    """Select assistant-owned text without guessing that prose is transport."""
    output_source = resolve_semantic_output_source(output_source)
    if output_source == FINAL_TEXT_OUTPUT_SOURCE:
        return stdout
    if output_source != STRUCTURED_TRANSPORT_OUTPUT_SOURCE:
        raise ValueError(f"unknown semantic output source: {output_source!r}")
    structured = extract_structured_final_assistant_text(stdout)
    return structured if structured is not None else ""


def parse_jsonl_events(stdout: str) -> tuple[Mapping[str, Any], ...]:
    events: list[Mapping[str, Any]] = []
    # JSONL records are delimited by LF; Unicode line separators can be payload data.
    for line_number, line in enumerate(stdout.split("\n"), 1):
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
    final_parts: list[str] = []
    for event in events:
        text = extract_final_assistant_text(event, require_final_event=True)
        if text:
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
