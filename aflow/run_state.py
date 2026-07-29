from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
from pathlib import Path
from typing import Any, Literal, Mapping
import tomllib

from .plan import PlanSnapshot


WorkflowEndReason = Literal[
    "already_complete",
    "done",
    "max_turns_reached",
    "transition_end",
]
RUN_STATE_SCHEMA_VERSION = 1
OverrideLoadStatus = Literal[
    "absent",
    "valid",
    "invalid",
    "already_consumed",
]


@dataclass(frozen=True)
class FrozenRunIdentity:
    workflow_name: str
    config_path: str
    config_fingerprint: str


@dataclass(frozen=True)
class OverrideRequest:
    digest: str
    source_text: str
    next_step: str | None = None
    team: str | None = None
    max_turns: int | None = None
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class OverrideLoadResult:
    status: OverrideLoadStatus
    request: OverrideRequest | None = None
    digest: str | None = None
    source_text: str | None = None
    message: str | None = None


@dataclass(frozen=True)
class OverrideResult:
    status: Literal["accepted", "rejected"]
    digest: str
    message: str
    source_text: str | None = None
    next_step: str | None = None
    team: str | None = None
    max_turns: int | None = None
    has_notes: bool = False
    applied: bool = False
    recorded_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


def load_override_request(
    path: Path,
    *,
    consumed_digest: str | None = None,
) -> OverrideLoadResult:
    """Load the narrow user override grammar without mutating the file."""
    if not path.is_file():
        return OverrideLoadResult(status="absent")
    try:
        source_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return OverrideLoadResult(
            status="invalid",
            message=f"cannot read override file: {exc}",
        )
    digest = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
    if digest == consumed_digest:
        return OverrideLoadResult(
            status="already_consumed",
            digest=digest,
            source_text=source_text,
        )
    try:
        raw = tomllib.loads(source_text)
    except tomllib.TOMLDecodeError as exc:
        return OverrideLoadResult(
            status="invalid",
            digest=digest,
            source_text=source_text,
            message=f"malformed TOML: {exc}",
        )
    allowed = {"next_step", "team", "max_turns", "notes"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        return OverrideLoadResult(
            status="invalid",
            digest=digest,
            source_text=source_text,
            message=f"unsupported override keys: {', '.join(unknown)}",
        )

    def optional_text(key: str) -> str | None:
        value = raw.get(key)
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{key} must be a non-empty string")
        return value.strip()

    try:
        next_step = optional_text("next_step")
        team = optional_text("team")
        max_turns = raw.get("max_turns")
        if max_turns is not None and (
            not isinstance(max_turns, int)
            or isinstance(max_turns, bool)
            or max_turns < 1
        ):
            raise ValueError("max_turns must be a positive integer")
        notes_value = raw.get("notes", [])
        if not isinstance(notes_value, list):
            raise ValueError("notes must be an array of non-empty strings")
        notes: list[str] = []
        for index, note in enumerate(notes_value):
            if not isinstance(note, str) or not note.strip():
                raise ValueError(f"notes[{index}] must be a non-empty string")
            notes.append(note.strip())
    except ValueError as exc:
        return OverrideLoadResult(
            status="invalid",
            digest=digest,
            source_text=source_text,
            message=str(exc),
        )
    if not raw:
        return OverrideLoadResult(
            status="invalid",
            digest=digest,
            source_text=source_text,
            message="override file must contain at least one supported key",
        )
    return OverrideLoadResult(
        status="valid",
        digest=digest,
        source_text=source_text,
        request=OverrideRequest(
            digest=digest,
            source_text=source_text,
            next_step=next_step,
            team=team,
            max_turns=max_turns,
            notes=tuple(notes),
        ),
    )

HarnessRecoverySource = Literal["deterministic", "team_lead"]
HarnessRecoveryAction = Literal[
    "retry_same_team_after_delay",
    "switch_to_backup_team_and_retry",
    "fail_immediately",
]


def describe_end_reason(end_reason: WorkflowEndReason) -> str:
    if end_reason == "already_complete":
        return "the original plan was already complete"
    if end_reason == "done":
        return "DONE evaluated true"
    if end_reason == "max_turns_reached":
        return "MAX_TURNS_REACHED matched"
    return "the workflow selected END"


@dataclass(frozen=True)
class ControllerConfig:
    repo_root: Path
    plan_path: Path
    max_turns: int = 15
    keep_runs: int = 20
    team: str | None = None
    extra_instructions: tuple[str, ...] = ()
    start_step: str | None = None


@dataclass(frozen=True)
class RetryContext:
    step_name: str
    step_role: str
    resolved_selector: str
    resolved_harness_name: str
    resolved_model: str | None
    resolved_effort: str | None
    snapshot_before: PlanSnapshot
    active_plan_path: Path
    new_plan_path: Path
    base_user_prompt: str
    parse_error_str: str
    attempt: int
    retry_limit: int


@dataclass(frozen=True)
class HarnessRecoveryContext:
    source: HarnessRecoverySource
    action: HarnessRecoveryAction
    reason: str
    match_terms: tuple[str, ...] = ()
    matched_terms: tuple[str, ...] = ()
    delay_seconds: int | None = 0
    from_team: str | None = None
    to_team: str | None = None
    consecutive_count: int = 0
    suggested_keywords: tuple[str, ...] = ()
    suggested_action: HarnessRecoveryAction | None = None
    executed: bool = True
    rejection_reason: str | None = None


@dataclass(frozen=True)
class ManagerDecisionSummary:
    decision_number: int
    level: str
    trigger: str
    action: str
    reason: str
    artifact_path: str


@dataclass(frozen=True)
class ActiveImplementationScope:
    """Stable lineage for all worker/reviewer attempts on one original checkpoint."""

    scope_id: str
    original_plan_path: str
    checkpoint_index: int | None
    checkpoint_name: str | None
    opened_turn_number: int
    awaiting_review: bool = False
    carried_reviewer_rejection_count: int = 0


@dataclass(frozen=True)
class PendingManagerNotes:
    target_step: str
    notes: tuple[str, ...]
    decision_number: int
    target_role: str | None = None
    target_selector: str | None = None
    checkpoint_identity: str | None = None
    consumed: bool = False
    scope_id: str | None = None
    target_plan_identity: str | None = None


@dataclass(frozen=True)
class PendingTeamOverride:
    target_step: str
    role: str
    source_team: str | None
    target_team: str
    selector: str
    checkpoint_identity: str | None
    decision_number: int
    consumed: bool = False
    scope_id: str | None = None
    target_plan_identity: str | None = None


@dataclass(frozen=True)
class ImplementationAttempt:
    """Durable routing evidence for one worker turn.

    Attempts deliberately carry the actual selector rather than inferring it
    from a team later: an upgrade must advance from what really ran.
    """

    turn_number: int
    step_name: str
    role: str
    team: str | None
    selector: str | None
    outcome: str
    manager_decision_number: int | None = None


@dataclass(frozen=True)
class ReviewRejectionRecord:
    """Durable, controller-derived evidence of one rejected implementation."""

    scope_id: str
    rejection_number: int
    source_run_id: str
    review_turn_number: int
    review_step_name: str
    reviewer_selector: str | None
    checkpoint_index: int | None
    checkpoint_name: str | None
    reviewed_implementation_turn_number: int
    reviewed_worker_team: str | None
    reviewed_worker_selector: str | None
    review_summary: str
    repair_plan_summary: str | None
    review_stdout_artifact_path: str
    repair_plan_path: str | None


@dataclass(frozen=True)
class PendingBoundaryDecision:
    """Accepted manager decision persisted before controller side effects."""

    finalized_turn_number: int
    decision_number: int
    action: str
    proposed_action: str
    proposed_transition: str | None
    resolved_next_step: str | None
    target_role: str | None = None
    target_team: str | None = None
    target_selector: str | None = None
    checkpoint_identity: str | None = None
    post_transition_active_plan_path: str | None = None
    post_transition_checkpoint_identity: str | None = None
    notes_reference: str | None = None
    applied: bool = False
    consumed: bool = False
    scope_id: str | None = None
    target_plan_identity: str | None = None


@dataclass(frozen=True)
class FinalizedTurnBoundary:
    """Controller-owned description of a finalized workflow turn or incident.

    This compact shape is persisted in manager context inputs so analysis can
    recreate the runtime context without relying on mutable controller state.
    """

    finalized_turn_number: int
    artifact_path: str
    trigger: str
    terminal: bool
    proposed_action: str
    proposed_transition: str | None
    current_step: str | None
    current_role: str | None
    baseline_team: str | None
    actual_team: str | None
    actual_selector: str | None
    original_plan_path: str | None
    active_plan_path: str | None
    checkpoint_identity: str | None
    context_schema_version: int = 2
    safely_retryable: bool = False
    operational_failure: bool = False
    backup_team: str | None = None
    backup_selector: str | None = None
    implementation_upgrade: dict[str, Any] | None = None
    active_implementation_scope: dict[str, Any] | None = None
    eligible_actions: list[str] = field(default_factory=list)
    evidence: str | None = None


@dataclass(frozen=True)
class PendingFinalizedTurn:
    """A completed harness turn whose interstep boundary was not persisted."""

    source_run_dir: Path
    turn_number: int
    step_name: str
    step_role: str
    selector: str
    active_plan_path: Path
    new_plan_path: Path
    snapshot_after: PlanSnapshot
    conditions: Mapping[str, bool]
    chosen_transition: str
    chosen_transition_condition: str | None = None


@dataclass(frozen=True)
class ResumeContext:
    resumed_from_run_id: str
    feature_branch: str
    worktree_path: Path
    main_branch: str
    setup: tuple[str, ...]
    teardown: tuple[str, ...]
    active_plan_path: Path | None = None
    interrupted_step_name: str | None = None
    manager_decision_number: int = 0
    manager_history: tuple[ManagerDecisionSummary, ...] = ()
    # Consecutive unchanged finalized turns in the latest workflow step.
    semantic_stall_count: int = 0
    reviewer_rejection_count: int = 0
    implementation_attempts: dict[str, tuple[ImplementationAttempt, ...]] = field(default_factory=dict)
    active_implementation_scope: ActiveImplementationScope | None = None
    review_rejection_history: tuple[ReviewRejectionRecord, ...] = ()
    pending_manager_notes: PendingManagerNotes | None = None
    pending_step_team_override: PendingTeamOverride | None = None
    pending_boundary_decision: PendingBoundaryDecision | None = None
    last_manager_report_path: str | None = None
    pending_finalized_turn: PendingFinalizedTurn | None = None
    frozen_run_identity: FrozenRunIdentity | None = None
    override_result: OverrideResult | None = None
    effective_max_turns: int | None = None
    pending_override_notes: tuple[str, ...] = ()
    override_source_run_dir: Path | None = None
    override_file_present: bool = False


@dataclass
class TurnRecord:
    turn_number: int
    step_name: str
    resolved_harness_name: str
    resolved_model_display: str
    turn_dir: Path | None = None
    step_role: str | None = None
    resolved_selector: str | None = None
    active_plan_path: str | None = None
    chosen_transition: str | None = None
    chosen_transition_condition: str | None = None
    issues_summary_path: str | None = None
    outcome: str = "running"
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    duration_seconds: float | None = None
    stdout_artifact_path: str | None = None
    stderr_artifact_path: str | None = None
    triggering_rejection_number: int | None = None


@dataclass(frozen=True)
class IssueRecord:
    issue_number: int
    kind: str
    message: str
    turn_number: int | None = None
    turn_dir: str | None = None
    result_artifact_path: str | None = None
    stdout_artifact_path: str | None = None
    stderr_artifact_path: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def format_harness_model_display(
    harness_name: str,
    model: str | None,
    effort: str | None = None,
) -> str:
    model_text = model or "default"
    if effort is not None:
        return f"{harness_name} / {model_text} / {effort}"
    return f"{harness_name} / {model_text}"


@dataclass
class ControllerState:
    last_snapshot: PlanSnapshot
    run_id: str | None = None
    resumed_from_run_id: str | None = None
    turns_completed: int = 0
    issues_accumulated: int = 0
    issues_summary_path: str | None = None
    run_started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    active_turn: int = 0
    current_turn_started_at: datetime | None = None
    status_message: str = "initializing"
    selected_start_step: str | None = None
    startup_recovery_used: bool = False
    startup_recovery_reason: str | None = None
    end_reason: WorkflowEndReason | None = None
    pending_retry: RetryContext | None = None
    current_team: str | None = None
    current_team_override: str | None = None
    current_harness_recovery: HarnessRecoveryContext | None = None
    harness_recovery_history: list[HarnessRecoveryContext] = field(default_factory=list)
    consecutive_harness_recoveries: int = 0
    turn_history: list[TurnRecord] = field(default_factory=list)
    issue_history: list[IssueRecord] = field(default_factory=list)
    consec_step_name: str | None = None
    consec_step_count: int = 0
    manager_decision_number: int = 0
    manager_history: list[ManagerDecisionSummary] = field(default_factory=list)
    semantic_stall_count: int = 0
    reviewer_rejection_count: int = 0
    implementation_attempts: dict[str, list[ImplementationAttempt]] = field(default_factory=dict)
    active_implementation_scope: ActiveImplementationScope | None = None
    review_rejection_history: list[ReviewRejectionRecord] = field(default_factory=list)
    pending_manager_notes: PendingManagerNotes | None = None
    pending_step_team_override: PendingTeamOverride | None = None
    pending_boundary_decision: PendingBoundaryDecision | None = None
    last_manager_report_path: str | None = None
    frozen_run_identity: FrozenRunIdentity | None = None
    override_result: OverrideResult | None = None
    effective_max_turns: int | None = None
    pending_override_notes: tuple[str, ...] = ()
    override_source_run_dir: Path | None = None
    override_file_present: bool = False


def manager_state_payload(state: ControllerState) -> dict[str, object]:
    """Return compact durable manager controller state for ``run.json``."""
    from dataclasses import asdict

    return {
        "manager_decision_number": state.manager_decision_number,
        "manager_history": [asdict(item) for item in state.manager_history],
        "semantic_stall_count": state.semantic_stall_count,
        "reviewer_rejection_count": state.reviewer_rejection_count,
        "implementation_attempts": {
            # Preserve old integer counters when a caller constructed legacy
            # state directly; on disk they are read as empty attempt histories.
            key: (attempts if isinstance(attempts, int) else [asdict(attempt) for attempt in attempts])
            for key, attempts in state.implementation_attempts.items()
        },
        "active_implementation_scope": (
            asdict(state.active_implementation_scope)
            if state.active_implementation_scope is not None
            else None
        ),
        "review_rejection_history": [asdict(item) for item in state.review_rejection_history],
        "pending_manager_notes": (
            asdict(state.pending_manager_notes)
            if state.pending_manager_notes is not None
            else None
        ),
        "pending_step_team_override": (
            asdict(state.pending_step_team_override)
            if state.pending_step_team_override is not None
            else None
        ),
        "pending_boundary_decision": (
            asdict(state.pending_boundary_decision)
            if state.pending_boundary_decision is not None
            else None
        ),
        "last_manager_report_path": state.last_manager_report_path,
    }


def restore_manager_state(state: ControllerState, payload: Mapping[str, Any]) -> None:
    """Restore manager-only durable state from a legacy-tolerant run payload."""
    state.manager_decision_number = int(payload.get("manager_decision_number", 0) or 0)
    history = payload.get("manager_history")
    if isinstance(history, list):
        state.manager_history = [
            ManagerDecisionSummary(
                decision_number=int(item["decision_number"]),
                level=str(item["level"]),
                trigger=str(item["trigger"]),
                action=str(item["action"]),
                reason=str(item["reason"]),
                artifact_path=str(item["artifact_path"]),
            )
            for item in history
            if isinstance(item, Mapping)
            and {"decision_number", "level", "trigger", "action", "reason", "artifact_path"} <= set(item)
        ]
    state.semantic_stall_count = int(payload.get("semantic_stall_count", 0) or 0)
    state.reviewer_rejection_count = int(payload.get("reviewer_rejection_count", 0) or 0)
    state.review_rejection_history = []
    rejection_history = payload.get("review_rejection_history")
    if isinstance(rejection_history, list):
        required = {
            "scope_id", "rejection_number", "source_run_id", "review_turn_number",
            "review_step_name", "reviewed_implementation_turn_number", "review_summary",
            "review_stdout_artifact_path",
        }
        for item in rejection_history:
            if not isinstance(item, Mapping) or not required <= set(item):
                continue
            try:
                state.review_rejection_history.append(ReviewRejectionRecord(
                    scope_id=str(item["scope_id"]), rejection_number=int(item["rejection_number"]),
                    source_run_id=str(item["source_run_id"]), review_turn_number=int(item["review_turn_number"]),
                    review_step_name=str(item["review_step_name"]),
                    reviewer_selector=str(item["reviewer_selector"]) if item.get("reviewer_selector") is not None else None,
                    checkpoint_index=int(item["checkpoint_index"]) if item.get("checkpoint_index") is not None else None,
                    checkpoint_name=str(item["checkpoint_name"]) if item.get("checkpoint_name") is not None else None,
                    reviewed_implementation_turn_number=int(item["reviewed_implementation_turn_number"]),
                    reviewed_worker_team=str(item["reviewed_worker_team"]) if item.get("reviewed_worker_team") is not None else None,
                    reviewed_worker_selector=str(item["reviewed_worker_selector"]) if item.get("reviewed_worker_selector") is not None else None,
                    review_summary=str(item["review_summary"]),
                    repair_plan_summary=str(item["repair_plan_summary"]) if item.get("repair_plan_summary") is not None else None,
                    review_stdout_artifact_path=str(item["review_stdout_artifact_path"]),
                    repair_plan_path=str(item["repair_plan_path"]) if item.get("repair_plan_path") is not None else None,
                ))
            except (TypeError, ValueError):
                continue
    attempts = payload.get("implementation_attempts")
    state.implementation_attempts = {}
    if isinstance(attempts, Mapping):
        for key, value in attempts.items():
            if isinstance(value, int) and not isinstance(value, bool):
                state.implementation_attempts[str(key)] = value  # type: ignore[assignment]
                continue
            if not isinstance(value, list):
                continue
            restored_attempts: list[ImplementationAttempt] = []
            for item in value:
                if not isinstance(item, Mapping):
                    continue
                required = {"turn_number", "step_name", "role", "outcome"}
                if not required <= set(item):
                    continue
                restored_attempts.append(ImplementationAttempt(
                    turn_number=int(item["turn_number"]), step_name=str(item["step_name"]),
                    role=str(item["role"]), team=str(item["team"]) if item.get("team") is not None else None,
                    selector=str(item["selector"]) if item.get("selector") is not None else None,
                    outcome=str(item["outcome"]),
                    manager_decision_number=(int(item["manager_decision_number"])
                        if item.get("manager_decision_number") is not None else None),
                ))
            state.implementation_attempts[str(key)] = restored_attempts
    scope = payload.get("active_implementation_scope")
    if isinstance(scope, Mapping) and {
        "scope_id", "original_plan_path", "opened_turn_number"
    } <= set(scope):
        has_scoped_rejection_count = (
            "carried_reviewer_rejection_count" in scope
        )
        state.active_implementation_scope = ActiveImplementationScope(
            scope_id=str(scope["scope_id"]),
            original_plan_path=str(scope["original_plan_path"]),
            checkpoint_index=(
                int(scope["checkpoint_index"])
                if scope.get("checkpoint_index") is not None else None
            ),
            checkpoint_name=(
                str(scope["checkpoint_name"])
                if scope.get("checkpoint_name") is not None else None
            ),
            opened_turn_number=int(scope["opened_turn_number"]),
            awaiting_review=bool(scope.get("awaiting_review", False)),
            carried_reviewer_rejection_count=int(
                scope.get("carried_reviewer_rejection_count", 0) or 0
            ),
        )
        if not has_scoped_rejection_count:
            # Pre-scoped run metadata may contain a poisoned whole-run total.
            # The compact run payload has no finalized-turn history from which
            # to reconstruct this scope, so resume it conservatively at zero.
            state.reviewer_rejection_count = 0
    notes = payload.get("pending_manager_notes")
    if isinstance(notes, Mapping) and isinstance(notes.get("notes"), (list, tuple)):
        state.pending_manager_notes = PendingManagerNotes(
            target_step=str(notes.get("target_step", "")),
            target_role=str(notes["target_role"]) if notes.get("target_role") is not None else None,
            target_selector=str(notes["target_selector"]) if notes.get("target_selector") is not None else None,
            checkpoint_identity=str(notes["checkpoint_identity"]) if notes.get("checkpoint_identity") is not None else None,
            notes=tuple(str(note) for note in notes["notes"]),
            decision_number=int(notes.get("decision_number", 0) or 0),
            consumed=bool(notes.get("consumed", False)),
            scope_id=str(notes["scope_id"]) if notes.get("scope_id") is not None else None,
            target_plan_identity=(
                str(notes["target_plan_identity"])
                if notes.get("target_plan_identity") is not None
                else None
            ),
        )
    override = payload.get("pending_step_team_override")
    if isinstance(override, Mapping):
        required = {"target_step", "role", "target_team", "selector", "decision_number"}
        if required <= set(override):
            state.pending_step_team_override = PendingTeamOverride(
                target_step=str(override["target_step"]),
                role=str(override["role"]),
                source_team=str(override["source_team"]) if override.get("source_team") is not None else None,
                target_team=str(override["target_team"]),
                selector=str(override["selector"]),
                checkpoint_identity=str(override["checkpoint_identity"]) if override.get("checkpoint_identity") is not None else None,
                decision_number=int(override["decision_number"]),
                consumed=bool(override.get("consumed", False)),
                scope_id=str(override["scope_id"]) if override.get("scope_id") is not None else None,
                target_plan_identity=(
                    str(override["target_plan_identity"])
                    if override.get("target_plan_identity") is not None
                    else None
                ),
            )
    boundary = payload.get("pending_boundary_decision")
    if isinstance(boundary, Mapping):
        required = {"finalized_turn_number", "decision_number", "action", "proposed_action"}
        if required <= set(boundary):
            state.pending_boundary_decision = PendingBoundaryDecision(
                finalized_turn_number=int(boundary["finalized_turn_number"]),
                decision_number=int(boundary["decision_number"]), action=str(boundary["action"]),
                proposed_action=str(boundary["proposed_action"]),
                proposed_transition=str(boundary["proposed_transition"]) if boundary.get("proposed_transition") is not None else None,
                resolved_next_step=str(boundary["resolved_next_step"]) if boundary.get("resolved_next_step") is not None else None,
                target_role=str(boundary["target_role"]) if boundary.get("target_role") is not None else None,
                target_team=str(boundary["target_team"]) if boundary.get("target_team") is not None else None,
                target_selector=str(boundary["target_selector"]) if boundary.get("target_selector") is not None else None,
                checkpoint_identity=str(boundary["checkpoint_identity"]) if boundary.get("checkpoint_identity") is not None else None,
                post_transition_active_plan_path=(str(boundary["post_transition_active_plan_path"])
                    if boundary.get("post_transition_active_plan_path") is not None else None),
                post_transition_checkpoint_identity=(str(boundary["post_transition_checkpoint_identity"])
                    if boundary.get("post_transition_checkpoint_identity") is not None else None),
                notes_reference=str(boundary["notes_reference"]) if boundary.get("notes_reference") is not None else None,
                applied=bool(boundary.get("applied", False)), consumed=bool(boundary.get("consumed", False)),
                scope_id=str(boundary["scope_id"]) if boundary.get("scope_id") is not None else None,
                target_plan_identity=(
                    str(boundary["target_plan_identity"])
                    if boundary.get("target_plan_identity") is not None
                    else None
                ),
            )
    report_path = payload.get("last_manager_report_path")
    state.last_manager_report_path = str(report_path) if report_path is not None else None


def manager_resume_fields(payload: Mapping[str, Any]) -> dict[str, object]:
    """Return legacy-tolerant manager state suitable for ``ResumeContext``."""
    restored = ControllerState(last_snapshot=PlanSnapshot(None, 0, 0, False))
    restore_manager_state(restored, payload)
    return {
        "manager_decision_number": restored.manager_decision_number,
        "manager_history": tuple(restored.manager_history),
        "semantic_stall_count": restored.semantic_stall_count,
        "reviewer_rejection_count": restored.reviewer_rejection_count,
        "implementation_attempts": {
            key: (() if isinstance(attempts, int) else tuple(attempts))
            for key, attempts in restored.implementation_attempts.items()
        },
        "active_implementation_scope": restored.active_implementation_scope,
        "review_rejection_history": tuple(restored.review_rejection_history),
        "pending_manager_notes": restored.pending_manager_notes,
        "pending_step_team_override": restored.pending_step_team_override,
        "pending_boundary_decision": restored.pending_boundary_decision,
        "last_manager_report_path": restored.last_manager_report_path,
    }


@dataclass(frozen=True)
class ExecutionContext:
    primary_repo_root: Path
    execution_repo_root: Path
    main_branch: str
    feature_branch: str
    worktree_path: Path | None
    setup: tuple[str, ...]
    teardown: tuple[str, ...]


@dataclass(frozen=True)
class ControllerRunResult:
    run_dir: Path
    turns_completed: int
    final_snapshot: PlanSnapshot
    status: str = "completed"
    issues_accumulated: int = 0
    end_reason: WorkflowEndReason = "transition_end"
    recovery_summary: HarnessRecoveryContext | None = None
    recovery_history: tuple[HarnessRecoveryContext, ...] = ()

    def to_dict(self) -> dict[str, object]:
        from dataclasses import asdict
        return {
            "run_dir": str(self.run_dir),
            "turns_completed": self.turns_completed,
            "final_snapshot": asdict(self.final_snapshot),
            "status": self.status,
            "issues_accumulated": self.issues_accumulated,
            "end_reason": self.end_reason,
            "recovery_summary": asdict(self.recovery_summary) if self.recovery_summary is not None else None,
            "recovery_history": [asdict(item) for item in self.recovery_history],
        }
