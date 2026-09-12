"""Versioned, bounded public models for control-plane persistence."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
import re
from typing import Any, Literal, Mapping


CONTROL_PLANE_SCHEMA_VERSION = 1
RUN_PROGRESS_SCHEMA_VERSION = 1
MAX_SERIALIZED_TEXT = 4_096
MAX_SERIALIZED_ITEMS = 128
WORKTREE_PREFLIGHT_DEFAULT_LIMIT = 200
WORKTREE_PREFLIGHT_MAX_LIMIT = 1_000
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


def startup_failure(
    stage: str,
    message: str,
    *,
    code: str | None = None,
    kind: str | None = None,
) -> dict[str, str]:
    """Bound diagnostics after removing credential assignments and bearer text."""
    message = re.sub(
        r"""(?i)((?:token|secret|password|api[_-]?key)["']?\s*[:=]\s*)(["'])(.*?)\2""",
        r"\1[redacted]", message, flags=re.DOTALL,
    )
    safe = re.sub(
        r"(?i)(authorization\s*[:=]\s*bearer\s+|(?:token|secret|password|api[_-]?key)\s*[:=]\s*[\"']?)([^\s\",'&]+)",
        r"\1[redacted]", message,
    )
    safe = re.sub(r"(?i)\bbearer\s+[^\s,]+", "Bearer [redacted]", safe)
    payload = {"stage": stage, "message": safe, "timestamp": utc_now()}
    if code is not None:
        payload["code"] = code
    if kind is not None:
        payload["kind"] = kind
    return bounded_redacted(payload)


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


ProgressAvailability = Literal[
    "complete", "partial", "unavailable", "not_applicable"
]
ProgressCoverage = Literal["complete", "partial", "unavailable"]
ProgressCheckpointStatus = Literal[
    "pending",
    "implementing",
    "reviewing",
    "repairing",
    "approved",
    "recorded_complete",
    "blocked",
    "unknown",
]
ProgressDeliveryStatus = Literal[
    "pending",
    "running",
    "succeeded",
    "failed",
    "unknown",
    "not_applicable",
]
ProgressChangeStatus = Literal["applied", "pending", "failed", "unknown"]


def _progress_safe(value: Any, *, key: str | None = None, depth: int = 0) -> Any:
    """Serialize progress without inheriting the smaller generic item cap.

    Progress detail deliberately has a larger, independently bounded history
    window than generic control-plane responses.  It still applies the same
    secret-field rule and never serializes arbitrary objects or raw artifacts.
    """
    if depth > 8:
        return "[truncated: nesting limit]"
    if key is not None and any(part in key.lower() for part in _SECRET_FIELD_PARTS):
        return "[redacted]"
    if isinstance(value, Enum):
        return _progress_safe(value.value, key=key, depth=depth + 1)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        return value if len(value) <= 4_096 else value[:4_096] + "[truncated]"
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for index, (child_key, child) in enumerate(value.items()):
            if index >= 512:
                result["_truncated"] = "mapping item limit"
                break
            name = str(child_key)
            result[name] = _progress_safe(child, key=name, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        result = [
            _progress_safe(item, depth=depth + 1)
            for item in items[:500]
        ]
        if len(items) > 500:
            result.append("[truncated: item limit]")
        return result
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _progress_safe(str(value), key=key, depth=depth + 1)


@dataclass(frozen=True)
class RunProgressCount:
    """A count whose value is explicitly qualified by evidence coverage."""

    value: int | None = None
    coverage: ProgressCoverage = "unavailable"

    def to_dict(self) -> dict[str, Any]:
        return _progress_safe(asdict(self))


@dataclass(frozen=True)
class RunProgressExecutor:
    """The actual invocation identity recorded at a worker/reviewer boundary."""

    role: str | None = None
    team: str | None = None
    selector: str | None = None
    harness: str | None = None
    model: str | None = None
    model_display: str | None = None
    effort: str | None = None
    source_run_id: str | None = None
    invocation_id: str | None = None
    turn_number: int | None = None
    started_at: str | None = None
    ended_at: str | None = None
    duration_seconds: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return _progress_safe(asdict(self))


@dataclass(frozen=True)
class RunProgressCheckpoint:
    """One original-plan checkpoint and its evidence-qualified counters."""

    checkpoint_id: str | None
    ordinal: int | None = None
    title: str | None = None
    status: ProgressCheckpointStatus = "unknown"
    awaiting_review: bool = False
    worker_attempts: RunProgressCount = field(default_factory=RunProgressCount)
    repair_passes: RunProgressCount = field(default_factory=RunProgressCount)
    reviews: RunProgressCount = field(default_factory=RunProgressCount)
    runtime_retries: RunProgressCount = field(default_factory=RunProgressCount)
    applied_upgrades: RunProgressCount = field(default_factory=RunProgressCount)
    recorded_at: str | None = None
    duration_seconds: float | None = None
    scope_id: str | None = None
    generation_id: str | None = None
    parent_checkpoint_id: str | None = None
    source_run_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return _progress_safe(asdict(self))


@dataclass(frozen=True)
class RunProgressEvent:
    """A safe, ordered history item; prompt and transcript bodies are excluded."""

    event_id: str
    checkpoint_id: str | None = None
    scope_id: str | None = None
    source_run_id: str | None = None
    turn_number: int | None = None
    decision_number: int | None = None
    kind: str = "unknown"
    outcome: str | None = None
    executor: RunProgressExecutor | None = None
    started_at: str | None = None
    ended_at: str | None = None
    duration_seconds: float | None = None
    reason: str | None = None
    source_reference: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return _progress_safe(asdict(self))


@dataclass(frozen=True)
class RunProgressChange:
    """An applied or pending team/profile change, kept separate from counts."""

    change_id: str
    status: ProgressChangeStatus = "unknown"
    kind: str = "configuration_change"
    roles: tuple[str, ...] = ()
    old_team: str | None = None
    new_team: str | None = None
    old_selector: str | None = None
    new_selector: str | None = None
    old_model: str | None = None
    new_model: str | None = None
    old_effort: str | None = None
    new_effort: str | None = None
    turn_number: int | None = None
    checkpoint_id: str | None = None
    generation_id: str | None = None
    reason: str | None = None
    recorded_at: str | None = None
    source_reference: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return _progress_safe(asdict(self))


@dataclass(frozen=True)
class RunProgressDeliveryStage:
    """One evidence-backed publication stage."""

    stage: str
    status: ProgressDeliveryStatus = "unknown"
    recorded_at: str | None = None
    reason: str | None = None
    source_reference: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return _progress_safe(asdict(self))


@dataclass(frozen=True)
class RunProgressTruncation:
    """Bounded-read accounting exposed so omitted history is never silent."""

    evidence_bytes: int = 0
    records_read: int = 0
    checkpoints_read: int = 0
    events_read: int = 0
    omitted_records: int = 0
    omitted_checkpoints: int = 0
    notices: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return _progress_safe(asdict(self))


@dataclass(frozen=True)
class RunProgressSummary:
    """Canonical summary projection shared by list and detail consumers."""

    schema_version: int = RUN_PROGRESS_SCHEMA_VERSION
    availability: ProgressAvailability = "unavailable"
    observed_at: str | None = None
    evidence_at: str | None = None
    reason_codes: tuple[str, ...] = ()
    original_plan_identity: str | None = None
    original_plan_display_name: str | None = None
    original_plan_path: str | None = None
    total_checkpoints: RunProgressCount = field(default_factory=RunProgressCount)
    approved_checkpoints: RunProgressCount = field(default_factory=RunProgressCount)
    recorded_complete_checkpoints: RunProgressCount = field(default_factory=RunProgressCount)
    checkpoint_states: Mapping[str, ProgressCheckpointStatus] = field(default_factory=dict)
    current_checkpoint_id: str | None = None
    current_checkpoint_ordinal: int | None = None
    current_checkpoint_title: str | None = None
    activity: str | None = None
    phase: str | None = None
    run_status: str | None = None
    current_executor: RunProgressExecutor | None = None
    last_executor: RunProgressExecutor | None = None
    worker_attempts: RunProgressCount = field(default_factory=RunProgressCount)
    repair_passes: RunProgressCount = field(default_factory=RunProgressCount)
    reviews: RunProgressCount = field(default_factory=RunProgressCount)
    runtime_retries: RunProgressCount = field(default_factory=RunProgressCount)
    applied_upgrades: RunProgressCount = field(default_factory=RunProgressCount)

    def to_dict(self) -> dict[str, Any]:
        return _progress_safe(asdict(self))


@dataclass(frozen=True)
class RunProgressDetail(RunProgressSummary):
    """Canonical summary plus bounded checkpoint and evidence history."""

    checkpoints: tuple[RunProgressCheckpoint, ...] = ()
    events: tuple[RunProgressEvent, ...] = ()
    applied_changes: tuple[RunProgressChange, ...] = ()
    pending_changes: tuple[RunProgressChange, ...] = ()
    delivery: tuple[RunProgressDeliveryStage, ...] = ()
    truncation: RunProgressTruncation = field(default_factory=RunProgressTruncation)

    def to_dict(self) -> dict[str, Any]:
        return _progress_safe(asdict(self))


@dataclass(frozen=True)
class WorkflowCapability:
    """Ordered workflow choices safe to expose before a launch."""

    declared_steps: tuple[str, ...] = ()
    executable_steps: tuple[str, ...] = ()
    excluded_steps: tuple[str, ...] = ()
    first_step: str | None = None
    default_team: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return bounded_redacted(asdict(self))


@dataclass(frozen=True)
class CapabilitySet:
    schema_version: int = CONTROL_PLANE_SCHEMA_VERSION
    workflows: tuple[str, ...] = ()
    teams: tuple[str, ...] = ()
    roles: tuple[str, ...] = ()
    controls: tuple[str, ...] = ()
    workflow_details: Mapping[str, WorkflowCapability] = field(default_factory=dict)
    admitted_role_selectors: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    status_values: tuple[str, ...] = ()
    context_levels: tuple[Literal["lite", "full"], ...] = ("lite", "full")
    team_upgrade_chains: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    control_safety: Mapping[str, Literal["safe", "restart_required"]] = field(
        default_factory=dict
    )
    service_features: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return bounded_redacted(asdict(self))


@dataclass(frozen=True)
class RunStatus:
    run_id: str
    status: str
    activity: Literal["active", "inactive", "unknown"] = "unknown"
    status_reason_code: str = "activity_unknown"
    history_state: Literal["visible", "archived", "deleted"] = "visible"
    history_revision: int = 0
    schema_version: int = CONTROL_PLANE_SCHEMA_VERSION
    ownership: Literal["control_plane", "legacy"] = "control_plane"
    revision: int = 0
    reason: str | None = None
    plan_path: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    unit_name: str | None = None
    launch_phase: str | None = None
    workflow_name: str | None = None
    team: str | None = None
    current_step: str | None = None
    turns_completed: int | None = None
    max_turns: int | None = None
    selected_start_step: str | None = None
    skipped_steps: tuple[str, ...] = ()
    restarted_from_run_id: str | None = None
    worker_exit: Mapping[str, Any] | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)
    progress: RunProgressSummary | None = None

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
    restarted_from_run_id: str | None = None

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
        # The generic control-plane serializer intentionally caps lists at 128
        # items.  Progress detail has its own, larger bounded history window,
        # so preserve that typed projection instead of silently turning a
        # valid detail payload into an invalid/truncated shape.
        data: dict[str, Any] = {}
        for index, (key, value) in enumerate(self.data.items()):
            if index >= MAX_SERIALIZED_ITEMS:
                data["_truncated"] = "mapping item limit"
                break
            name = str(key)
            if any(part in name.lower() for part in _SECRET_FIELD_PARTS):
                data[name] = "[redacted]"
            elif name == "progress":
                if isinstance(value, (RunProgressSummary, RunProgressDetail)):
                    data[name] = value.to_dict()
                elif isinstance(value, Mapping):
                    data[name] = _progress_safe(value)
                else:
                    data[name] = bounded_redacted(value)
            else:
                data[name] = bounded_redacted(value)
        return {
            "run_id": self.run_id,
            "level": self.level,
            "data": data,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class ProjectRecord:
    """A canonical project root exposed by the transport-neutral repository."""

    project_id: str
    root: str
    schema_version: int = CONTROL_PLANE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return bounded_redacted(asdict(self))


@dataclass(frozen=True)
class PlanRecord:
    """Bounded plan metadata; plan prose remains on disk until explicitly read."""

    path: str
    status: str
    modified_at: str
    schema_version: int = CONTROL_PLANE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return bounded_redacted(asdict(self))


@dataclass(frozen=True)
class RunPage:
    """Stable lexicographic run page with an opaque-by-convention cursor."""

    runs: tuple[RunStatus, ...]
    next_cursor: str | None = None
    schema_version: int = CONTROL_PLANE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return bounded_redacted(asdict(self))


@dataclass(frozen=True)
class WorktreeStatusItem:
    """One repository-relative path from a working-tree preflight."""

    path: str
    index_status: str
    worktree_status: str
    original_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return bounded_redacted(asdict(self))


@dataclass(frozen=True)
class WorktreePreflightResult:
    """Bounded, transport-neutral working-tree inspection output."""

    checkout_path: str
    execution_mode: Literal["same_checkout", "new_worktree"]
    dirty: bool
    requires_confirmation: bool
    blockers: tuple[str, ...]
    total_items: int
    offset: int = 0
    limit: int = WORKTREE_PREFLIGHT_DEFAULT_LIMIT
    next_offset: int | None = None
    items: tuple[WorktreeStatusItem, ...] = ()

    @staticmethod
    def validate_page(*, offset: int, limit: int) -> None:
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= WORKTREE_PREFLIGHT_MAX_LIMIT
        ):
            raise ValueError(
                "limit must be between 1 and "
                f"{WORKTREE_PREFLIGHT_MAX_LIMIT}"
            )

    @classmethod
    def from_domain(
        cls,
        result: Any,
        *,
        offset: int,
        limit: int,
    ) -> "WorktreePreflightResult":
        """Project the CP7 domain result into one bounded response page."""
        cls.validate_page(offset=offset, limit=limit)
        domain_items = tuple(getattr(result, "items", ()))
        items = tuple(
            WorktreeStatusItem(
                path=str(item.path),
                original_path=(
                    None
                    if item.original_path is None
                    else str(item.original_path)
                ),
                index_status=str(item.index_status),
                worktree_status=str(item.worktree_status),
            )
            for item in domain_items
        )
        total_items = int(getattr(result, "total_items", len(items)))
        if total_items < 0:
            raise ValueError("worktree preflight total_items must be non-negative")
        end = min(offset + limit, len(items))
        next_offset = end if end < total_items and end > offset else None
        return cls(
            checkout_path=str(getattr(result, "checkout_path")),
            execution_mode=getattr(result, "execution_mode"),
            dirty=bool(getattr(result, "dirty")),
            requires_confirmation=bool(getattr(result, "requires_confirmation")),
            blockers=tuple(str(blocker) for blocker in getattr(result, "blockers", ())),
            total_items=total_items,
            offset=offset,
            limit=limit,
            next_offset=next_offset,
            items=items[offset:end],
        )

    def to_dict(self) -> dict[str, Any]:
        # A preflight page may contain 1,000 items.  Keep the general
        # control-plane serializer bound while preserving this endpoint's
        # explicit page limit.
        payload = bounded_redacted(asdict(self))
        payload["items"] = [item.to_dict() for item in self.items]
        return payload


@dataclass(frozen=True)
class StartupQuestionRecord:
    """A transient, opaque handle for the existing startup-question protocol."""

    question_id: str
    kind: str
    message: str
    options: Mapping[str, str] = field(default_factory=dict)
    choices: tuple[str, ...] = ()
    run_id: str | None = None
    schema_version: int = CONTROL_PLANE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return bounded_redacted(asdict(self))


@dataclass(frozen=True)
class ReconciliationResult:
    """One fail-closed observation; it never authorizes a resume or launch."""

    run_id: str
    status: str
    reason: str
    ownership: Literal["control_plane", "legacy"] = "control_plane"
    unit_name: str | None = None
    observed_unit_state: str | None = None
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
    restarted_from_run_id: str | None = None
    skipped_steps: tuple[str, ...] = ()
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
            "restarted_from_run_id": self.restarted_from_run_id,
            "skipped_steps": list(self.skipped_steps),
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
