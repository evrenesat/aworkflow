"""Versioned, bounded public models for control-plane persistence."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Mapping


CONTROL_PLANE_SCHEMA_VERSION = 1
MAX_SERIALIZED_TEXT = 4_096
MAX_SERIALIZED_ITEMS = 128
_SECRET_FIELD_PARTS = (
    "authorization",
    "credential",
    "cookie",
    "password",
    "secret",
    "session_id",
    "token",
    "api_key",
    "prompt",
)


def utc_now() -> str:
    """Return a timezone-aware, JSON-safe timestamp."""
    return datetime.now(timezone.utc).isoformat()


def bounded_redacted(value: Any, *, depth: int = 0) -> Any:
    """Return a JSON-safe, bounded representation without sensitive fields."""
    if depth > 8:
        return "[truncated: nesting limit]"
    if isinstance(value, Enum):
        return bounded_redacted(value.value, depth=depth + 1)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return bounded_redacted(asdict(value), depth=depth + 1)
    if isinstance(value, str):
        if len(value) <= MAX_SERIALIZED_TEXT:
            return value
        return value[:MAX_SERIALIZED_TEXT] + "[truncated]"
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= MAX_SERIALIZED_ITEMS:
                result["_truncated"] = "mapping item limit"
                break
            name = str(key)
            if any(part in name.lower() for part in _SECRET_FIELD_PARTS):
                result[name] = "[redacted]"
            else:
                result[name] = bounded_redacted(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        result = [
            bounded_redacted(item, depth=depth + 1)
            for item in list(value)[:MAX_SERIALIZED_ITEMS]
        ]
        if len(value) > MAX_SERIALIZED_ITEMS:
            result.append("[truncated: item limit]")
        return result
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return bounded_redacted(str(value), depth=depth + 1)


@dataclass(frozen=True)
class CapabilitySet:
    schema_version: int = CONTROL_PLANE_SCHEMA_VERSION
    workflows: tuple[str, ...] = ()
    teams: tuple[str, ...] = ()
    roles: tuple[str, ...] = ()
    controls: tuple[str, ...] = ()
    context_levels: tuple[Literal["lite", "full"], ...] = ("lite", "full")

    def to_dict(self) -> dict[str, Any]:
        return bounded_redacted(asdict(self))


@dataclass(frozen=True)
class RunStatus:
    run_id: str
    status: str
    schema_version: int = CONTROL_PLANE_SCHEMA_VERSION
    ownership: Literal["control_plane", "legacy"] = "control_plane"
    revision: int = 0
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return bounded_redacted(asdict(self))


@dataclass(frozen=True)
class StartRunResult:
    run_id: str
    created: bool
    status: str
    schema_version: int = CONTROL_PLANE_SCHEMA_VERSION
    manifest_path: str | None = None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return bounded_redacted(asdict(self))


@dataclass(frozen=True)
class RunControlRequest:
    """A compare-and-swap update to the existing run-owned override file."""

    expected_revision: int
    schema_version: int = CONTROL_PLANE_SCHEMA_VERSION
    max_turns: int | None = None
    owner_stop: bool | None = None
    team: str | None = None
    role_selectors: Mapping[str, str] = field(default_factory=dict)
    unsafe_changes: Mapping[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return bounded_redacted(asdict(self))


@dataclass(frozen=True)
class ContextBundle:
    run_id: str
    level: Literal["lite", "full"]
    data: Mapping[str, Any]
    schema_version: int = CONTROL_PLANE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return bounded_redacted(asdict(self))


@dataclass(frozen=True)
class LaunchManifest:
    """Immutable, pre-child launch intent for one canonical run identity."""

    run_id: str
    project_root: str
    plan_path: str
    workflow_name: str
    max_turns: int
    schema_version: int = CONTROL_PLANE_SCHEMA_VERSION
    team: str | None = None
    start_step: str | None = None
    extra_instructions: tuple[str, ...] = ()
    idempotency_key: str | None = None
    caller_scope: str | None = None
    request_digest: str | None = None
    frozen_config_fingerprint: str | None = None
    intended_unit: str | None = None
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        """Serialize only durable launch metadata, never launch prompt text.

        ``extra_instructions`` contribute to the engine-owned request digest but
        are intentionally omitted from the immutable manifest.  Keeping this
        representation as a narrow allowlist also prevents future prompt-like
        fields from becoming persistent merely because they were added to this
        dataclass.
        """
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "project_root": self.project_root,
            "plan_path": self.plan_path,
            "workflow_name": self.workflow_name,
            "max_turns": self.max_turns,
            "team": self.team,
            "start_step": self.start_step,
            "idempotency_key": self.idempotency_key,
            "caller_scope": self.caller_scope,
            "request_digest": self.request_digest,
            "frozen_config_fingerprint": self.frozen_config_fingerprint,
            "intended_unit": self.intended_unit,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class RunEvent:
    sequence: int
    event_type: str
    data: Mapping[str, Any]
    schema_version: int = CONTROL_PLANE_SCHEMA_VERSION
    timestamp: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return bounded_redacted(asdict(self))
