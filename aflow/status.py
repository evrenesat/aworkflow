from __future__ import annotations

import os
import re
import sys
import textwrap
import threading
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from .plan import PlanSnapshot
from .run_state import (
    ControllerState,
    ReviewRejectionRecord,
    TurnRecord,
    describe_end_reason,
)
from .config import WorkflowConfig, WorkflowStepConfig, WorkflowUserConfig, load_workflow_config

if TYPE_CHECKING:
    from .git_status import GitBaseline, GitSummary

_WORKFLOW_TERMINAL_TARGET = "END"

_DEFAULT_VALUE_LIMIT = 200
_DEFAULT_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_DISPLAY_WRAP_WIDTH = 78
_SGR_BOLD = "\x1b[1m"
_SGR_RESET = "\x1b[0m"


@dataclass(frozen=True)
class WorkflowGraphSource:
    declared_steps: dict[str, WorkflowStepConfig]
    executable_steps: dict[str, WorkflowStepConfig]
    excluded_step_names: tuple[str, ...] = ()


def _plan_title(plan_path: Path) -> str:
    stem = plan_path.stem.replace("_", " ").replace("-", " ")
    return " ".join(stem.split()).title()


def _resolve_workflow_graph_source(
    *,
    workflow_name: str | None,
    workflow_steps: dict[str, WorkflowStepConfig] | None,
) -> WorkflowGraphSource:
    if workflow_name is None:
        steps = dict(workflow_steps or {})
        return WorkflowGraphSource(
            declared_steps=steps,
            executable_steps=steps,
        )

    try:
        workflow_config = load_workflow_config()
    except Exception:
        steps = dict(workflow_steps or {})
        return WorkflowGraphSource(
            declared_steps=steps,
            executable_steps=steps,
        )

    workflow = workflow_config.workflows.get(workflow_name)
    if workflow is None:
        steps = dict(workflow_steps or {})
        return WorkflowGraphSource(
            declared_steps=steps,
            executable_steps=steps,
        )

    return WorkflowGraphSource(
        declared_steps=dict(workflow.declared_steps),
        executable_steps=dict(workflow.steps),
        excluded_step_names=workflow.excluded_steps,
    )


def _visual_start_skipped_step_names(
    *,
    declared_steps: dict[str, WorkflowStepConfig],
    executable_steps: dict[str, WorkflowStepConfig],
    excluded_step_names: tuple[str, ...],
    selected_start_step: str | None,
) -> tuple[str, ...]:
    if selected_start_step is None:
        return ()
    skipped: list[str] = []
    excluded_set = set(excluded_step_names)
    for step_name in declared_steps:
        if step_name == selected_start_step:
            break
        if step_name in executable_steps and step_name not in excluded_set:
            skipped.append(step_name)
    return tuple(skipped)


def _elapsed(started_at: datetime) -> str:
    delta = datetime.now(timezone.utc) - started_at
    total_seconds = int(delta.total_seconds())
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}h {minutes:02d}m {seconds:02d}s"
    if minutes > 0:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


def _checkpoint_display(snapshot: PlanSnapshot) -> str:
    if snapshot.is_complete:
        return f"done ({snapshot.total_checkpoint_count}/{snapshot.total_checkpoint_count})"
    if snapshot.current_checkpoint_index is not None:
        return f"{snapshot.current_checkpoint_index}/{snapshot.total_checkpoint_count}"
    return "unknown"


def _status_display(state: ControllerState) -> str:
    display = state.status_message
    if state.status_message == "completed" and state.end_reason is not None:
        if state.end_reason == "already_complete":
            display = "completed: already complete"
        elif state.end_reason == "done":
            display = "completed: done"
        elif state.end_reason == "max_turns_reached":
            display = "completed: max turns reached"
        else:
            display = "completed: transition to END"
    transaction = state.current_hotplug_transaction or state.pending_hotplug_transaction
    if transaction is None and state.hotplug_history:
        transaction = state.hotplug_history[-1]
    if transaction is not None:
        capability = {
            "native_resume": "native session resume",
            "handover_required": "handover bootstrap",
        }.get(transaction.capability_path, "capability pending")
        active_selector = next(
            (
                item.selector for item in state.active_role_sessions
                if item.role == transaction.source_role and item.status == "active"
            ),
            None,
        )
        session_suffix = f" | active {active_selector}" if active_selector else ""
        return (
            f"{display} | hotplug {transaction.stage}: "
            f"{transaction.source_selector} -> {transaction.target_selector} ({capability})"
            + session_suffix
        )
    return display


def _git_row(summary: GitSummary) -> str:
    return _git_row_values(
        modified_count=summary.modified_count,
        added_count=summary.added_count,
        removed_count=summary.removed_count,
        lines_added=summary.lines_added,
        lines_removed=summary.lines_removed,
        commit_count=summary.commit_count,
    )


def _git_row_values(
    *,
    modified_count: int,
    added_count: int,
    removed_count: int,
    lines_added: int,
    lines_removed: int,
    commit_count: int,
) -> str:
    if modified_count == 0 and added_count == 0 and removed_count == 0:
        return f"clean since start | +{lines_added}/-{lines_removed} | {commit_count} commits"
    return (
        f"M {modified_count}, A {added_count}, D {removed_count}"
        f" | +{lines_added}/-{lines_removed}"
        f" | {commit_count} commits"
    )


def _files_row(changed_paths: tuple[str, ...], *, limit: int) -> str | None:
    if not changed_paths:
        return None
    shown = changed_paths[:limit]
    extra = len(changed_paths) - len(shown)
    text = ", ".join(shown)
    if extra > 0:
        text += f" +{extra} more"
    return text


def _active_scope_rejections(state: ControllerState) -> tuple[ReviewRejectionRecord, ...]:
    scope = state.active_implementation_scope
    if scope is None:
        return ()
    return tuple(sorted(
        (item for item in state.review_rejection_history if item.scope_id == scope.scope_id),
        key=lambda item: item.rejection_number,
    ))


def _clean_text(value: object, *, limit: int | None = _DEFAULT_VALUE_LIMIT) -> str:
    """Return printable text with newlines, tabs, and control bytes removed.

    Dynamic Unicode content is preserved so non-ASCII configuration or plan
    values remain readable; only control characters are flattened.  Bounds
    apply to display data only; callers pass ``limit=None`` for durable
    artifact references, which must never be truncated.
    """
    text = value if isinstance(value, str) else str(value)
    characters: list[str] = []
    for character in text:
        code = ord(character)
        if code < 0x20 or code == 0x7F or 0x80 <= code <= 0x9F:
            characters.append(" ")
        else:
            characters.append(character)
    cleaned = "".join(characters).strip()
    if limit is not None and len(cleaned) > limit:
        cleaned = cleaned[: limit - 3].rstrip() + "..."
    return cleaned


def _quote_value(text: str) -> str:
    if text != "" and not any(character in text for character in (" ", '"', "=")):
        return text
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _record_field(name: str, value: object, *, limit: int | None = _DEFAULT_VALUE_LIMIT) -> str:
    return f"{name}={_quote_value(_clean_text(value, limit=limit))}"


@dataclass(frozen=True)
class _CheckpointProjection:
    index: int | None
    total: int
    title: str | None
    complete: bool
    unchecked_checkpoints: int
    unchecked_steps: int


@dataclass(frozen=True)
class _IssueProjection:
    number: int
    kind: str
    message: str
    turn_number: int | None
    turn_dir: str | None
    result_artifact_path: str | None
    stdout_artifact_path: str | None
    stderr_artifact_path: str | None


@dataclass(frozen=True)
class _TurnProjection:
    number: int
    step_name: str
    role: str | None
    selector: str | None
    model: str
    outcome: str
    transition: str | None
    transition_condition: str | None
    active_plan_path: str | None
    stdout_artifact_path: str | None
    stderr_artifact_path: str | None
    issues_summary_path: str | None
    duration_seconds: float | None
    checkpoint: _CheckpointProjection | None
    issue: _IssueProjection | None


@dataclass(frozen=True)
class _GitProjection:
    modified_count: int
    added_count: int
    removed_count: int
    lines_added: int
    lines_removed: int
    commit_count: int
    changed_paths: tuple[str, ...]
    omitted_path_count: int


@dataclass(frozen=True)
class _DiagnosticsProjection:
    frozen_fingerprint: str | None
    override_file: bool | None
    override_result: tuple[object, ...] | None
    manager_decision: tuple[int, str, str, str, str] | None
    pending_manager_notes: tuple[str, int] | None
    pending_team_override: tuple[str, str, str, str, int] | None
    manager_report_path: str | None
    review_rejection: tuple[int, str, str | None, str | None] | None
    scope_pressure: str | None
    pending_repartition: tuple[str, ...] | None
    repartition: tuple[str, ...] | None
    partition: str | None
    hotplug: tuple[str, ...] | None
    recovery: tuple[str, ...] | None
    retry: tuple[str, ...] | None
    startup_recovery: tuple[str, ...] | None
    issues_summary_path: str | None


@dataclass(frozen=True)
class _PresentationProjection:
    run_id: str | None
    resumed_from_run_id: str | None
    workflow_name: str | None
    team: str | None
    status: str
    end_reason: str | None
    original_plan_path: str
    active_plan_path: str | None
    generated_plan_path: str | None
    selected_start_step: str | None
    skipped_steps: tuple[str, ...]
    current_step_name: str | None
    configured_runtime: str | None
    effective_max_turns: int
    active_turn: int
    turns_completed: int
    checkpoint: _CheckpointProjection
    preparation: bool
    running_turn: _TurnProjection | None
    latest_turn: _TurnProjection | None
    diagnostics: _DiagnosticsProjection
    git: _GitProjection | None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _format_duration(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds_value = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {seconds_value:02d}s"
    if minutes:
        return f"{minutes}m {seconds_value:02d}s"
    return f"{seconds_value}s"


def _format_elapsed(started_at: datetime, now: datetime) -> str:
    return _format_duration((_as_utc(now) - _as_utc(started_at)).total_seconds())


def _append_wrapped(lines: list[str], label: str | None, value: str) -> None:
    if not value:
        return
    if label is None:
        wrapper = textwrap.TextWrapper(
            width=_DISPLAY_WRAP_WIDTH,
            initial_indent="  ",
            subsequent_indent="    ",
            break_long_words=False,
            break_on_hyphens=False,
        )
    else:
        wrapper = textwrap.TextWrapper(
            width=_DISPLAY_WRAP_WIDTH,
            initial_indent=f"  {label}: ",
            subsequent_indent="    ",
            break_long_words=False,
            break_on_hyphens=False,
        )
    lines.extend(wrapper.wrap(value))


def _role_display(role: str) -> str:
    return " ".join(part.capitalize() for part in role.replace("_", " ").split())


def _turn_status_word(outcome: str) -> str:
    normalized = outcome.lower()
    if normalized == "running":
        return "Running"
    if normalized == "completed":
        return "Finished"
    if "owner" in normalized and "stop" in normalized:
        return "Owner stopped"
    if "wait" in normalized:
        return "Waiting"
    return "Failed"


def _turn_is_failure(outcome: str) -> bool:
    normalized = outcome.lower()
    return normalized not in {"running", "completed"} and "owner" not in normalized


def _final_status_heading(projection: _PresentationProjection) -> str:
    end_reason = projection.end_reason or ""
    normalized = projection.status.lower()
    if end_reason == "max_turns_reached" or "max turns" in normalized:
        return "Turn limit reached"
    if end_reason == "owner_stopped" or "owner_stopped" in normalized:
        return "Stopped by owner"
    if normalized == "failed" or "failed" in normalized:
        return "Failed"
    if normalized.startswith("waiting") or "waiting" in normalized:
        return "Waiting"
    if normalized.startswith("completed") or end_reason in {
        "already_complete",
        "done",
        "transition_end",
    }:
        return "Completed"
    if "finished" in normalized:
        return "Completed"
    if normalized in {"initializing", "preparing"}:
        return "Preparing"
    return _clean_text(projection.status).title() or "Stopped"


def _end_reason_text(end_reason: str) -> str:
    if end_reason in {
        "already_complete",
        "done",
        "max_turns_reached",
        "transition_end",
        "owner_stopped",
    }:
        return describe_end_reason(end_reason)  # type: ignore[arg-type]
    return _clean_text(end_reason)


def _path_text(value: object | None) -> str | None:
    if value is None:
        return None
    text = _clean_text(value, limit=None)
    return text or None


def _checkpoint_projection(snapshot: PlanSnapshot) -> _CheckpointProjection:
    return _CheckpointProjection(
        index=snapshot.current_checkpoint_index,
        total=snapshot.total_checkpoint_count,
        title=(
            _clean_text(snapshot.current_checkpoint_name)
            if snapshot.current_checkpoint_name is not None
            else None
        ),
        complete=snapshot.is_complete,
        unchecked_checkpoints=snapshot.unchecked_checkpoint_count,
        unchecked_steps=snapshot.current_checkpoint_unchecked_step_count,
    )


def _checkpoint_title(checkpoint: _CheckpointProjection) -> str | None:
    title = checkpoint.title
    if title is None or checkpoint.index is None:
        return title
    prefix = re.compile(rf"^Checkpoint\s+{checkpoint.index}\s*:\s*")
    match = prefix.match(title)
    if match is None:
        return title
    return title[match.end():].strip() or None


def _checkpoint_line(checkpoint: _CheckpointProjection) -> str | None:
    if checkpoint.complete:
        if checkpoint.total:
            return f"Plan complete ({checkpoint.total}/{checkpoint.total})"
        return "Plan complete"
    if checkpoint.index is None:
        return None
    return f"Checkpoint {checkpoint.index} of {checkpoint.total}"


def _safe_is_file(path: Path | None) -> bool:
    if path is None:
        return False
    try:
        return path.is_file()
    except OSError:
        return False


def _git_projection(summary: GitSummary, *, limit: int) -> _GitProjection | None:
    if (
        summary.modified_count == 0
        and summary.added_count == 0
        and summary.removed_count == 0
        and summary.lines_added == 0
        and summary.lines_removed == 0
        and summary.commit_count == 0
        and not summary.changed_paths
    ):
        return None
    shown = tuple(
        _clean_text(path, limit=None)
        for path in summary.changed_paths[:limit]
    )
    return _GitProjection(
        modified_count=summary.modified_count,
        added_count=summary.added_count,
        removed_count=summary.removed_count,
        lines_added=summary.lines_added,
        lines_removed=summary.lines_removed,
        commit_count=summary.commit_count,
        changed_paths=shown,
        omitted_path_count=max(0, len(summary.changed_paths) - len(shown)),
    )


def _issue_projection(issue: object) -> _IssueProjection:
    return _IssueProjection(
        number=int(getattr(issue, "issue_number")),
        kind=_clean_text(getattr(issue, "kind")),
        message=_clean_text(getattr(issue, "message"), limit=512),
        turn_number=getattr(issue, "turn_number"),
        turn_dir=_path_text(getattr(issue, "turn_dir", None)),
        result_artifact_path=_path_text(getattr(issue, "result_artifact_path", None)),
        stdout_artifact_path=_path_text(getattr(issue, "stdout_artifact_path", None)),
        stderr_artifact_path=_path_text(getattr(issue, "stderr_artifact_path", None)),
    )


class BannerRenderer:
    """Render append-only, human-readable workflow status blocks.

    The renderer owns presentation state only.  It snapshots mutable
    controller values into immutable projections before comparing or laying
    them out, so output selection cannot depend on later state mutation.
    """

    def __init__(
        self,
        *,
        config_harness: str | None = None,
        config_model: str | None = None,
        config_effort: str | None = None,
        workflow_steps: dict[str, WorkflowStepConfig] | None = None,
        config_max_turns: int,
        config_plan_path: Path,
        config_banner_files_limit: int = 10,
        workflow_name: str | None = None,
        current_step_name: str | None = None,
        original_plan_path: Path | None = None,
        active_plan_path: Path | None = None,
        new_plan_path: Path | None = None,
        workflow_graph_source: WorkflowGraphSource | None = None,
        repo_root: Path | None = None,
        stream: object | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._config_harness = config_harness
        self._config_model = config_model
        self._config_effort = config_effort
        self._workflow_steps = workflow_steps
        self._config_max_turns = config_max_turns
        self._config_plan_path = config_plan_path
        self._config_banner_files_limit = config_banner_files_limit
        self._workflow_name = workflow_name
        self._current_step_name = current_step_name
        self._original_plan_path = original_plan_path
        self._active_plan_path = active_plan_path
        self._new_plan_path = new_plan_path
        self._workflow_graph_source = workflow_graph_source or _resolve_workflow_graph_source(
            workflow_name=workflow_name,
            workflow_steps=workflow_steps,
        )
        self._repo_root = repo_root
        self._stream = stream
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._state: ControllerState | None = None
        self._baseline: GitBaseline | None = None
        self._last_projection: _PresentationProjection | None = None
        self._turn_checkpoint_cache: dict[int, PlanSnapshot] = {}
        self._last_emitted_final_turn_number: int | None = None
        self._output_disabled = False
        self._has_output = False
        self._style_stream: object | None = None
        self._style_enabled: bool | None = None

    def set_context(
        self,
        *,
        current_step_name: str | None = None,
        active_plan_path: Path | None = None,
        new_plan_path: Path | None = None,
        config_harness: str | None = None,
        config_model: str | None = None,
        config_effort: str | None = None,
    ) -> None:
        with self._lock:
            self._current_step_name = current_step_name
            self._active_plan_path = active_plan_path
            self._new_plan_path = new_plan_path
            self._config_harness = config_harness
            self._config_model = config_model
            self._config_effort = config_effort

    def start(self, state: ControllerState) -> None:
        with self._lock:
            self._state = state
            self._last_projection = None
            self._turn_checkpoint_cache = {}
            self._last_emitted_final_turn_number = None
            self._capture_baseline_locked()
        try:
            self._observe_running_turn(state)
            projection = self._build_projection(state)
            blocks = [self._format_header(projection)]
            finalized = self._new_finalized_records(state)
            if finalized:
                blocks.extend(
                    self._format_turn(
                        self._turn_projection(state, record),
                        projection,
                        previous=None,
                    )
                    for record in finalized
                )
            if projection.running_turn is not None:
                blocks.append(self._format_turn(
                    projection.running_turn, projection, previous=None
                ))
            elif not finalized:
                blocks.append(self._format_preparation(projection, update=False))
            self._last_projection = projection
            self._mark_finalized_records(finalized)
            self._write_blocks(blocks)
        except Exception:
            # Rendering must never alter workflow execution or finalization.
            return

    def update(self, state: ControllerState) -> None:
        self._emit_update(state, final=False)

    def stop(self, state: ControllerState) -> None:
        self._emit_update(state, final=True)

    def _capture_baseline_locked(self) -> None:
        self._baseline = None
        if self._repo_root is None:
            return
        try:
            from .git_status import capture_baseline

            self._baseline = capture_baseline(self._repo_root)
        except Exception:
            self._baseline = None

    def _current_git_summary(self) -> GitSummary | None:
        if self._repo_root is None or self._baseline is None:
            return None
        try:
            from .git_status import summarize_since_baseline

            return summarize_since_baseline(self._repo_root, self._baseline)
        except Exception:
            return None

    def _observe_running_turn(self, state: ControllerState) -> None:
        running = next(
            (record for record in reversed(state.turn_history) if record.outcome == "running"),
            None,
        )
        if running is None:
            return
        if running.turn_number not in self._turn_checkpoint_cache:
            self._turn_checkpoint_cache = {
                running.turn_number: state.last_snapshot,
            }

    def _new_finalized_records(self, state: ControllerState) -> tuple[TurnRecord, ...]:
        previous_number = self._last_emitted_final_turn_number
        records = tuple(
            record
            for record in state.turn_history
            if record.outcome != "running"
            and (previous_number is None or record.turn_number > previous_number)
        )
        return tuple(sorted(records, key=lambda record: record.turn_number))

    def _mark_finalized_records(self, records: tuple[TurnRecord, ...]) -> None:
        if records:
            self._last_emitted_final_turn_number = max(
                record.turn_number for record in records
            )

    def _turn_issue(self, state: ControllerState, turn_number: int) -> _IssueProjection | None:
        matching = [
            issue for issue in state.issue_history
            if issue.turn_number == turn_number
        ]
        if not matching:
            return None
        return _issue_projection(max(matching, key=lambda issue: issue.issue_number))

    def _turn_projection(
        self,
        state: ControllerState,
        record: TurnRecord,
    ) -> _TurnProjection:
        snapshot = self._turn_checkpoint_cache.get(record.turn_number)
        return _TurnProjection(
            number=record.turn_number,
            step_name=_clean_text(record.step_name),
            role=(
                _clean_text(record.step_role)
                if record.step_role is not None
                else None
            ),
            selector=(
                _clean_text(record.resolved_selector)
                if record.resolved_selector is not None
                else None
            ),
            model=_clean_text(record.resolved_model_display),
            outcome=_clean_text(record.outcome),
            transition=(
                _clean_text(record.chosen_transition)
                if record.chosen_transition is not None
                else None
            ),
            transition_condition=(
                _clean_text(record.chosen_transition_condition)
                if record.chosen_transition_condition is not None
                else None
            ),
            active_plan_path=_path_text(record.active_plan_path),
            stdout_artifact_path=_path_text(record.stdout_artifact_path),
            stderr_artifact_path=_path_text(record.stderr_artifact_path),
            issues_summary_path=_path_text(record.issues_summary_path),
            duration_seconds=record.duration_seconds,
            checkpoint=(
                _checkpoint_projection(snapshot)
                if snapshot is not None
                else None
            ),
            issue=(
                self._turn_issue(state, record.turn_number)
                if record.outcome != "running"
                else None
            ),
        )

    def _diagnostics_projection(self, state: ControllerState) -> _DiagnosticsProjection:
        override_result = None
        if state.override_result is not None:
            result = state.override_result
            override_result = (
                _clean_text(result.status),
                _clean_text(result.digest, limit=None),
                _clean_text(result.message),
                _clean_text(result.next_step) if result.next_step is not None else None,
                _clean_text(result.team) if result.team is not None else None,
                result.max_turns,
                bool(result.owner_stop),
                bool(result.has_notes),
                bool(result.applied),
            )

        manager_decision = None
        if state.manager_history:
            manager = state.manager_history[-1]
            manager_decision = (
                manager.decision_number,
                _clean_text(manager.level),
                _clean_text(manager.trigger),
                _clean_text(manager.action),
                _path_text(manager.artifact_path) or "",
            )

        pending_notes = None
        if state.pending_manager_notes is not None:
            pending_notes = (
                _clean_text(state.pending_manager_notes.target_step),
                state.pending_manager_notes.decision_number,
            )

        pending_team_override = None
        if state.pending_step_team_override is not None:
            override = state.pending_step_team_override
            pending_team_override = (
                _clean_text(override.target_step),
                _clean_text(override.role),
                _clean_text(override.target_team),
                _clean_text(override.selector),
                override.decision_number,
            )

        review_rejection = None
        rejections = _active_scope_rejections(state)
        if rejections:
            rejection = rejections[-1]
            review_rejection = (
                rejection.rejection_number,
                _path_text(rejection.review_stdout_artifact_path) or "",
                _path_text(rejection.repair_plan_path),
                _clean_text(rejection.scope_id),
            )

        pending_repartition = None
        if state.pending_repartition is not None:
            pending = state.pending_repartition
            pending_repartition = tuple(
                value
                for value in (
                    _clean_text(pending.stage),
                    _clean_text(pending.failed_stage) if pending.failed_stage else "",
                    _clean_text(pending.generation_id) if pending.generation_id else "",
                    _clean_text(pending.current_disposition)
                    if pending.current_disposition else "",
                    _path_text(pending.latest_attempt_path) or "",
                    _path_text(pending.proposal_artifact_path) or "",
                    _path_text(pending.candidate_artifact_path) or "",
                    _path_text(pending.mechanical_validation_artifact_path) or "",
                    _path_text(pending.semantic_verdict_artifact_path) or "",
                )
            )

        repartition = None
        if state.repartition_history:
            latest = state.repartition_history[-1]
            repartition = tuple(
                value
                for value in (
                    _clean_text(latest.generation_id),
                    str(len(latest.partition_ids)),
                    _clean_text(latest.current_disposition),
                    _path_text(latest.envelope_artifact_path) or "",
                    _path_text(latest.proposal_artifact_path) or "",
                    _path_text(latest.candidate_artifact_path) or "",
                    _path_text(latest.mechanical_validation_artifact_path) or "",
                    _path_text(latest.semantic_verdict_artifact_path) or "",
                )
            )

        transaction = (
            state.current_hotplug_transaction
            or state.pending_hotplug_transaction
            or (state.hotplug_history[-1] if state.hotplug_history else None)
        )
        hotplug = None
        if transaction is not None:
            capability = {
                "native_resume": "native session resume",
                "handover_required": "handover bootstrap",
            }.get(transaction.capability_path, "capability pending")
            hotplug = tuple(
                [
                    str(transaction.transaction_number),
                    _clean_text(transaction.stage),
                    _clean_text(transaction.source_selector),
                    _clean_text(transaction.target_selector),
                    capability,
                    _clean_text(transaction.failure)
                    if transaction.failure else "",
                    _clean_text(transaction.remediation)
                    if transaction.remediation else "",
                    *(
                        _path_text(path) or ""
                        for path in transaction.artifact_paths
                    ),
                ]
            )

        recovery = None
        if state.current_harness_recovery is not None:
            current = state.current_harness_recovery
            recovery = tuple(
                value
                for value in (
                    _clean_text(current.source),
                    _clean_text(current.action),
                    _clean_text(current.reason),
                    _clean_text(current.from_team) if current.from_team else "",
                    _clean_text(current.to_team) if current.to_team else "",
                    str(current.consecutive_count),
                    _clean_text(current.rejection_reason)
                    if current.rejection_reason else "",
                )
            )

        retry = None
        if state.pending_retry is not None:
            pending_retry = state.pending_retry
            retry = tuple(
                value
                for value in (
                    _clean_text(pending_retry.step_name),
                    str(pending_retry.attempt),
                    str(pending_retry.retry_limit),
                    _clean_text(pending_retry.parse_error_str),
                )
            )

        startup_recovery = None
        if state.startup_recovery_used or state.startup_recovery_reason is not None:
            startup_recovery = (
                "used" if state.startup_recovery_used else "not used",
                _clean_text(state.startup_recovery_reason)
                if state.startup_recovery_reason else "",
            )

        partition = None
        scope = state.active_implementation_scope
        if (
            scope is not None
            and scope.current_partition_generation_id is not None
            and scope.current_partition_id is not None
        ):
            partition = (
                f"{_clean_text(scope.current_partition_id)}"
                f"/{_clean_text(scope.current_partition_generation_id)}"
            )

        return _DiagnosticsProjection(
            frozen_fingerprint=(
                _clean_text(state.frozen_run_identity.config_fingerprint[:12], limit=None)
                if state.frozen_run_identity is not None
                else None
            ),
            override_file=(True if state.override_file_present else None),
            override_result=override_result,
            manager_decision=manager_decision,
            pending_manager_notes=pending_notes,
            pending_team_override=pending_team_override,
            manager_report_path=_path_text(state.last_manager_report_path),
            review_rejection=review_rejection,
            scope_pressure=(
                _clean_text(state.scope_pressure_reason)
                if state.scope_pressure_reason is not None
                else None
            ),
            pending_repartition=pending_repartition,
            repartition=repartition,
            partition=partition,
            hotplug=hotplug,
            recovery=recovery,
            retry=retry,
            startup_recovery=startup_recovery,
            issues_summary_path=_path_text(state.issues_summary_path),
        )

    def _build_projection(self, state: ControllerState) -> _PresentationProjection:
        latest_record = state.turn_history[-1] if state.turn_history else None
        running_record = (
            latest_record if latest_record is not None and latest_record.outcome == "running"
            else None
        )
        latest_turn = (
            self._turn_projection(state, latest_record)
            if latest_record is not None
            else None
        )
        running_turn = (
            self._turn_projection(state, running_record)
            if running_record is not None
            else None
        )
        current_step_name = self._current_step_name
        if current_step_name is None and latest_record is not None:
            current_step_name = latest_record.step_name
        configured_runtime = None
        runtime_parts = [
            item for item in (
                self._config_harness,
                self._config_model,
                self._config_effort,
            )
            if item is not None
        ]
        if runtime_parts:
            configured_runtime = " / ".join(_clean_text(item) for item in runtime_parts)

        original_plan_path = _path_text(self._original_plan_path or self._config_plan_path)
        assert original_plan_path is not None
        active_plan_path = _path_text(self._active_plan_path)
        generated_plan_path = None
        if (
            _safe_is_file(self._new_plan_path)
            and self._new_plan_path != self._active_plan_path
        ):
            generated_plan_path = _path_text(self._new_plan_path)
        skipped_names = _visual_start_skipped_step_names(
            declared_steps=self._workflow_graph_source.declared_steps,
            executable_steps=self._workflow_graph_source.executable_steps,
            excluded_step_names=self._workflow_graph_source.excluded_step_names,
            selected_start_step=state.selected_start_step,
        )
        max_turns = state.effective_max_turns or self._config_max_turns
        git_summary = self._current_git_summary()
        return _PresentationProjection(
            run_id=_path_text(state.run_id),
            resumed_from_run_id=_path_text(state.resumed_from_run_id),
            workflow_name=(
                _clean_text(self._workflow_name)
                if self._workflow_name is not None else None
            ),
            team=(
                _clean_text(state.current_team or state.current_team_override)
                if state.current_team or state.current_team_override else None
            ),
            status=_clean_text(_status_display(state)),
            end_reason=(
                _clean_text(state.end_reason)
                if state.end_reason is not None else None
            ),
            original_plan_path=original_plan_path,
            active_plan_path=active_plan_path,
            generated_plan_path=generated_plan_path,
            selected_start_step=(
                _clean_text(state.selected_start_step)
                if state.selected_start_step is not None else None
            ),
            skipped_steps=tuple(_clean_text(item) for item in skipped_names),
            current_step_name=(
                _clean_text(current_step_name)
                if current_step_name is not None else None
            ),
            configured_runtime=configured_runtime,
            effective_max_turns=max_turns,
            active_turn=state.active_turn,
            turns_completed=state.turns_completed,
            checkpoint=_checkpoint_projection(state.last_snapshot),
            preparation=running_turn is None and latest_record is None,
            running_turn=running_turn,
            latest_turn=latest_turn,
            diagnostics=self._diagnostics_projection(state),
            git=_git_projection(
                git_summary,
                limit=self._config_banner_files_limit,
            ) if git_summary is not None else None,
        )

    @staticmethod
    def _without_git(projection: _PresentationProjection) -> tuple[object, ...]:
        return (
            projection.run_id,
            projection.resumed_from_run_id,
            projection.workflow_name,
            projection.team,
            projection.status,
            projection.end_reason,
            projection.original_plan_path,
            projection.active_plan_path,
            projection.generated_plan_path,
            projection.selected_start_step,
            projection.skipped_steps,
            projection.current_step_name,
            projection.configured_runtime,
            projection.effective_max_turns,
            projection.active_turn,
            projection.turns_completed,
            projection.checkpoint,
            projection.preparation,
            projection.running_turn,
            projection.latest_turn,
            projection.diagnostics,
        )

    @classmethod
    def _meaningful_change(
        cls,
        previous: _PresentationProjection | None,
        current: _PresentationProjection,
    ) -> bool:
        return previous is None or cls._without_git(previous) != cls._without_git(current)

    def _format_header(self, projection: _PresentationProjection) -> str:
        lines = [f"AFlow run {projection.run_id or 'identity unavailable'}"]
        started_at = self._state.run_started_at if self._state is not None else self._clock()
        lines.append(
            f"  Started:  {_as_utc(started_at).strftime('%Y-%m-%d %H:%M:%S UTC')}"
        )
        if projection.workflow_name is not None:
            lines.append(f"  Workflow: {projection.workflow_name}")
        if projection.team is not None:
            lines.append(f"  Team:     {projection.team}")
        lines.append("  Plan:")
        lines.append(f"    {projection.original_plan_path}")
        if projection.resumed_from_run_id is not None:
            lines.append(f"  Resumed from: {projection.resumed_from_run_id}")
        if projection.diagnostics.frozen_fingerprint is not None:
            lines.append(
                f"  Config fingerprint: {projection.diagnostics.frozen_fingerprint}"
            )
        if projection.selected_start_step is not None:
            lines.append(f"  Start step: {projection.selected_start_step}")
        if projection.skipped_steps:
            lines.append(f"  Skipped steps: {', '.join(projection.skipped_steps)}")
        return "\n".join(lines)

    def _format_preparation(
        self,
        projection: _PresentationProjection,
        *,
        update: bool,
        previous: _PresentationProjection | None = None,
    ) -> str:
        if update:
            lines = [f"{self._timestamp()} - Preparation update"]
            if previous is not None and projection.status != previous.status:
                lines.append(f"  Status:   {projection.status}")
            if previous is not None and (
                projection.active_turn != previous.active_turn
                or projection.turns_completed != previous.turns_completed
            ):
                lines.append(
                    f"  Turn progress: {projection.turns_completed} completed; "
                    f"current {projection.active_turn} of {projection.effective_max_turns}"
                )
            previous_checkpoint = previous.checkpoint if previous is not None else None
            if previous is None or projection.checkpoint != previous_checkpoint:
                self._append_checkpoint(lines, projection.checkpoint)
            if previous is None or projection.team != previous.team:
                if projection.team is not None:
                    lines.append(f"  Team:     {projection.team}")
            if previous is None or projection.current_step_name != previous.current_step_name:
                if projection.current_step_name is not None:
                    lines.append(f"  Step:     {projection.current_step_name}")
            if previous is None or projection.configured_runtime != previous.configured_runtime:
                if projection.configured_runtime is not None:
                    lines.append(f"  Harness:  {projection.configured_runtime}")
            self._append_plan_identity_changes(
                lines,
                projection,
                previous,
            )
            self._append_changed_diagnostics(
                lines,
                projection.diagnostics,
                previous.diagnostics if previous is not None else None,
                include_all=previous is None,
            )
            return "\n".join(lines) if len(lines) > 1 else ""

        lines = ["Preparing run"]
        checkpoint_line = _checkpoint_line(projection.checkpoint)
        if checkpoint_line is None:
            lines.append("  Waiting for checkpoint metadata")
        else:
            lines.append(f"  {checkpoint_line}")
            self._append_checkpoint_title(lines, projection.checkpoint)
        if projection.team is not None:
            lines.append(f"  Team:     {projection.team}")
        if projection.current_step_name is not None:
            lines.append(f"  Step:     {projection.current_step_name}")
        if projection.configured_runtime is not None:
            lines.append(f"  Harness:  {projection.configured_runtime}")
        self._append_path(lines, "Active plan", projection.active_plan_path)
        self._append_path(lines, "Generated plan", projection.generated_plan_path)
        self._append_changed_diagnostics(
            lines,
            projection.diagnostics,
            None,
            include_all=True,
        )
        return "\n".join(lines)

    def _format_turn(
        self,
        turn: _TurnProjection,
        projection: _PresentationProjection,
        *,
        previous: _PresentationProjection | None,
    ) -> str:
        status_word = _turn_status_word(turn.outcome)
        lines = [
            f"{self._timestamp()} - Turn {turn.number} of "
            f"{projection.effective_max_turns} - {status_word}"
        ]
        if turn.checkpoint is not None:
            checkpoint_line = _checkpoint_line(turn.checkpoint)
            if checkpoint_line is not None:
                lines.append(f"  {checkpoint_line}")
                self._append_checkpoint_title(lines, turn.checkpoint)
        if projection.team is not None:
            lines.append(f"  Team:     {projection.team}")
        if turn.model:
            role_label = (
                _role_display(turn.role)
                if turn.role is not None else "Model"
            )
            lines.append(f"  {role_label}: {turn.model}")
        if turn.selector is not None:
            lines.append(f"  Selector: {turn.selector}")
        lines.append(f"  Step:     {turn.step_name}")

        if turn.outcome != "running":
            lines.append(f"  Outcome:  {turn.outcome}")
            if turn.transition is not None:
                next_value = turn.transition
                if turn.transition_condition is not None:
                    next_value += f" when {turn.transition_condition}"
                lines.append(f"  Next:     {next_value}")
            if turn.duration_seconds is not None:
                lines.append(f"  Duration: {_format_duration(turn.duration_seconds)}")
        self._append_plan_identity_changes(
            lines,
            projection,
            previous,
            turn_active_plan_path=turn.active_plan_path,
        )
        if turn.issue is not None and _turn_is_failure(turn.outcome):
            _append_wrapped(lines, "Reason", turn.issue.message)

        self._append_changed_diagnostics(
            lines,
            projection.diagnostics,
            previous.diagnostics if previous is not None else None,
            include_all=previous is None,
        )
        if turn.outcome != "running":
            self._append_git(lines, projection.git)
        self._append_turn_logs(lines, turn)
        return "\n".join(lines)

    def _format_status_update(
        self,
        previous: _PresentationProjection,
        projection: _PresentationProjection,
    ) -> str:
        lines = [f"{self._timestamp()} - Status update"]
        if projection.status != previous.status:
            lines.append(f"  Status:   {projection.status}")
        if (
            projection.active_turn != previous.active_turn
            or projection.turns_completed != previous.turns_completed
        ):
            lines.append(
                f"  Turn progress: {projection.turns_completed} completed; "
                f"current {projection.active_turn} of {projection.effective_max_turns}"
            )
        if projection.checkpoint != previous.checkpoint:
            self._append_checkpoint(lines, projection.checkpoint)
        if projection.team != previous.team and projection.team is not None:
            lines.append(f"  Team:     {projection.team}")
        if projection.current_step_name != previous.current_step_name:
            if projection.current_step_name is not None:
                lines.append(f"  Step:     {projection.current_step_name}")
        if projection.configured_runtime != previous.configured_runtime:
            if projection.configured_runtime is not None:
                lines.append(f"  Harness:  {projection.configured_runtime}")
        self._append_plan_identity_changes(lines, projection, previous)
        if projection.selected_start_step != previous.selected_start_step:
            if projection.selected_start_step is not None:
                lines.append(f"  Start step: {projection.selected_start_step}")
        if projection.skipped_steps != previous.skipped_steps and projection.skipped_steps:
            lines.append(f"  Skipped steps: {', '.join(projection.skipped_steps)}")
        self._append_changed_diagnostics(
            lines,
            projection.diagnostics,
            previous.diagnostics,
            include_all=False,
        )
        return "\n".join(lines) if len(lines) > 1 else ""

    def _format_final_summary(self, projection: _PresentationProjection) -> str:
        lines = [
            f"AFlow run {projection.run_id or 'identity unavailable'} - "
            f"{_final_status_heading(projection)}",
            f"  Status:   {projection.status}",
            f"  Turns:    {projection.turns_completed} of {projection.effective_max_turns}",
        ]
        self._append_checkpoint(lines, projection.checkpoint)
        if projection.end_reason is not None:
            lines.append(f"  End reason: {_end_reason_text(projection.end_reason)}")
        if projection.latest_turn is not None:
            lines.append(
                f"  Last turn: {projection.latest_turn.number} "
                f"({projection.latest_turn.step_name}, {projection.latest_turn.outcome})"
            )
            if (
                projection.latest_turn.issue is not None
                and _turn_is_failure(projection.latest_turn.outcome)
            ):
                _append_wrapped(
                    lines,
                    "Reason",
                    projection.latest_turn.issue.message,
                )
        started_at = self._state.run_started_at if self._state is not None else self._clock()
        lines.append(f"  Elapsed:  {_format_elapsed(started_at, self._clock())}")
        self._append_changed_diagnostics(
            lines,
            projection.diagnostics,
            None,
            include_all=True,
        )
        self._append_plan_summary(lines, projection)
        self._append_git(lines, projection.git)
        if projection.latest_turn is not None:
            self._append_turn_logs(lines, projection.latest_turn)
        return "\n".join(lines)

    def _emit_update(self, state: ControllerState, *, final: bool) -> None:
        try:
            with self._lock:
                self._state = state
            self._observe_running_turn(state)
            projection = self._build_projection(state)
            previous = self._last_projection
            blocks: list[str] = []
            finalized = self._new_finalized_records(state)
            if finalized:
                for record in finalized:
                    blocks.append(self._format_turn(
                        self._turn_projection(state, record),
                        projection,
                        previous=previous,
                    ))
                self._mark_finalized_records(finalized)
            elif projection.running_turn is not None:
                if (
                    previous is None
                    or previous.running_turn != projection.running_turn
                ):
                    blocks.append(self._format_turn(
                        projection.running_turn,
                        projection,
                        previous=previous,
                    ))
                elif self._meaningful_change(previous, projection):
                    blocks.append(self._format_status_update(previous, projection))
            elif previous is None:
                blocks.append(self._format_preparation(projection, update=False))
            elif self._meaningful_change(previous, projection):
                if previous.preparation and projection.preparation:
                    blocks.append(self._format_preparation(
                        projection,
                        update=True,
                        previous=previous,
                    ))
                else:
                    blocks.append(self._format_status_update(previous, projection))

            if final:
                blocks.append(self._format_final_summary(projection))
            self._last_projection = projection
            if blocks:
                self._write_blocks(blocks)
        except Exception:
            # Rendering must never alter workflow execution or finalization.
            return

    def _timestamp(self) -> str:
        return _as_utc(self._clock()).strftime("%H:%M:%S UTC")

    @staticmethod
    def _append_checkpoint(lines: list[str], checkpoint: _CheckpointProjection) -> None:
        checkpoint_line = _checkpoint_line(checkpoint)
        if checkpoint_line is not None:
            lines.append(f"  Checkpoint: {checkpoint_line}")
            BannerRenderer._append_checkpoint_title(lines, checkpoint)

    @staticmethod
    def _append_checkpoint_title(
        lines: list[str],
        checkpoint: _CheckpointProjection,
    ) -> None:
        title = _checkpoint_title(checkpoint)
        if title is not None:
            _append_wrapped(lines, None, title)

    @staticmethod
    def _append_path(lines: list[str], label: str, path: str | None) -> None:
        if path is not None:
            lines.append(f"  {label}:")
            lines.append(f"    {path}")

    @staticmethod
    def _append_plan_identity_changes(
        lines: list[str],
        projection: _PresentationProjection,
        previous: _PresentationProjection | None,
        *,
        turn_active_plan_path: str | None = None,
    ) -> None:
        """Show plan identities that changed in this emitted block.

        A finalized turn can still carry the old active plan while the
        renderer context already points at a newly selected repair plan.  In
        that case the current projection's path must be emitted with the
        finalization block.  If a turn already carries the changed path, use
        that single turn path instead of duplicating the label.
        """
        active_path = projection.active_plan_path
        if (
            turn_active_plan_path is not None
            and turn_active_plan_path != projection.original_plan_path
            and (
                previous is None
                or previous.active_plan_path != turn_active_plan_path
            )
        ):
            BannerRenderer._append_path(lines, "Active plan", turn_active_plan_path)

        if (
            active_path is not None
            and active_path != turn_active_plan_path
            and (
                previous is None
                or previous.active_plan_path != active_path
            )
        ):
            BannerRenderer._append_path(lines, "Active plan", active_path)

        generated_path = projection.generated_plan_path
        if (
            generated_path is not None
            and (
                previous is None
                or previous.generated_plan_path != generated_path
            )
            and generated_path != active_path
            and generated_path != turn_active_plan_path
        ):
            BannerRenderer._append_path(lines, "Generated plan", generated_path)

    @staticmethod
    def _append_changed_diagnostics(
        lines: list[str],
        diagnostics: _DiagnosticsProjection,
        previous: _DiagnosticsProjection | None,
        *,
        include_all: bool,
    ) -> None:
        def changed(name: str) -> bool:
            return include_all or previous is None or getattr(previous, name) != getattr(diagnostics, name)

        if diagnostics.frozen_fingerprint is not None and changed("frozen_fingerprint"):
            lines.append(f"  Config fingerprint: {diagnostics.frozen_fingerprint}")
        if diagnostics.override_file is not None and changed("override_file"):
            lines.append("  Override file: present")
        if diagnostics.override_result is not None and changed("override_result"):
            (
                status,
                digest,
                message,
                next_step,
                team,
                max_turns,
                owner_stop,
                has_notes,
                applied,
            ) = diagnostics.override_result
            lines.append(
                f"  Override result: {status} ({digest}): {message}"
            )
            if next_step is not None:
                lines.append(f"  Override next step: {next_step}")
            if team is not None:
                lines.append(f"  Override team: {team}")
            if max_turns is not None:
                lines.append(f"  Override turn limit: {max_turns}")
            if owner_stop:
                lines.append("  Override action: stop requested")
            elif status == "rejected":
                lines.append("  Override action: correct overrides.toml and resume")
            elif has_notes:
                lines.append("  Override notes: pending")
            if applied:
                lines.append("  Override state: applied")
        if diagnostics.manager_decision is not None and changed("manager_decision"):
            number, level, trigger, action, artifact_path = diagnostics.manager_decision
            lines.append(
                f"  Manager decision #{number}: {level}/{trigger}/{action}"
            )
            if artifact_path:
                BannerRenderer._append_path(lines, "Manager artifact", str(artifact_path))
        if diagnostics.pending_manager_notes is not None and changed("pending_manager_notes"):
            target, number = diagnostics.pending_manager_notes
            lines.append(f"  Pending manager notes: {target} (decision {number})")
        if diagnostics.pending_team_override is not None and changed("pending_team_override"):
            target, role, team, selector, number = diagnostics.pending_team_override
            lines.append(
                f"  Manager team override: {target} -> {team} "
                f"({role}, {selector}; decision {number})"
            )
        if diagnostics.manager_report_path is not None and changed("manager_report_path"):
            BannerRenderer._append_path(
                lines, "Manager report", diagnostics.manager_report_path
            )
        if diagnostics.review_rejection is not None and changed("review_rejection"):
            number, review_path, repair_path, scope_id = diagnostics.review_rejection
            lines.append(f"  Review rejection #{number} (scope {scope_id})")
            BannerRenderer._append_path(lines, "Review evidence", str(review_path))
            BannerRenderer._append_path(lines, "Repair plan", repair_path)
        if diagnostics.scope_pressure is not None and changed("scope_pressure"):
            _append_wrapped(lines, "Scope pressure", diagnostics.scope_pressure)
        if diagnostics.pending_repartition is not None and changed("pending_repartition"):
            values = diagnostics.pending_repartition
            lines.append(f"  Repartition: {values[0]}")
            if len(values) > 1 and values[1]:
                lines.append(f"  Repartition failed stage: {values[1]}")
            if len(values) > 2 and values[2]:
                lines.append(f"  Repartition generation: {values[2]}")
            if len(values) > 3 and values[3]:
                lines.append(f"  Repartition disposition: {values[3]}")
            for label, value in zip(
                (
                    "Repartition attempt",
                    "Repartition proposal",
                    "Repartition candidate",
                    "Repartition mechanical validation",
                    "Repartition semantic verdict",
                ),
                values[4:],
            ):
                BannerRenderer._append_path(lines, label, value or None)
        if diagnostics.repartition is not None and changed("repartition"):
            values = diagnostics.repartition
            lines.append(
                f"  Partition split: {values[0]} / {values[1]} parts / {values[2]}"
            )
            for label, value in zip(
                (
                    "Split envelope",
                    "Split proposal",
                    "Split candidate",
                    "Split mechanical validation",
                    "Split semantic verdict",
                ),
                values[3:],
            ):
                BannerRenderer._append_path(lines, label, value or None)
        if diagnostics.partition is not None and changed("partition"):
            lines.append(f"  Current partition: {diagnostics.partition}")
        if diagnostics.hotplug is not None and changed("hotplug"):
            values = diagnostics.hotplug
            capability = f"; {values[4]}" if values[4] else ""
            lines.append(
                f"  Hotplug transaction #{values[0]}: {values[1]} "
                f"({values[2]} -> {values[3]}{capability})"
            )
            if values[5]:
                _append_wrapped(lines, "Hotplug failure", values[5])
            if values[6]:
                _append_wrapped(lines, "Hotplug remediation", values[6])
            for path in values[7:]:
                BannerRenderer._append_path(lines, "Hotplug artifact", path or None)
        if diagnostics.recovery is not None and changed("recovery"):
            values = diagnostics.recovery
            lines.append(f"  Harness recovery: {values[0]} / {values[1]}")
            _append_wrapped(lines, "Recovery reason", values[2])
            if values[3] or values[4]:
                lines.append(f"  Recovery teams: {values[3]} -> {values[4]}")
            if values[5] != "0":
                lines.append(f"  Recovery count: {values[5]}")
            if values[6]:
                _append_wrapped(lines, "Recovery rejection", values[6])
        if diagnostics.retry is not None and changed("retry"):
            step, attempt, limit, reason = diagnostics.retry
            lines.append(f"  Retry scheduled: {step}, attempt {attempt} of {limit}")
            _append_wrapped(lines, "Retry reason", reason)
        if diagnostics.startup_recovery is not None and changed("startup_recovery"):
            used, reason = diagnostics.startup_recovery
            lines.append(f"  Startup recovery: {used}")
            if reason:
                _append_wrapped(lines, "Startup recovery reason", reason)
        if diagnostics.issues_summary_path is not None and changed("issues_summary_path"):
            BannerRenderer._append_path(
                lines, "Issues summary", diagnostics.issues_summary_path
            )

    @staticmethod
    def _append_git(lines: list[str], git: _GitProjection | None) -> None:
        if git is None:
            return
        lines.append(
            "  Git:      "
            + _git_row_values(
                modified_count=git.modified_count,
                added_count=git.added_count,
                removed_count=git.removed_count,
                lines_added=git.lines_added,
                lines_removed=git.lines_removed,
                commit_count=git.commit_count,
            )
        )
        if git.changed_paths or git.omitted_path_count:
            lines.append("  Files:")
            for path in git.changed_paths:
                lines.append(f"    {path}")
            if git.omitted_path_count:
                lines.append(f"    +{git.omitted_path_count} more")

    @staticmethod
    def _append_turn_logs(lines: list[str], turn: _TurnProjection) -> None:
        paths: list[tuple[str, str]] = []
        if turn.stdout_artifact_path is not None:
            paths.append(("stdout", turn.stdout_artifact_path))
        if turn.stderr_artifact_path is not None:
            paths.append(("stderr", turn.stderr_artifact_path))
        if turn.issues_summary_path is not None:
            paths.append(("issues", turn.issues_summary_path))
        if turn.issue is not None:
            issue_paths = (
                ("result", turn.issue.result_artifact_path),
                ("stdout", turn.issue.stdout_artifact_path),
                ("stderr", turn.issue.stderr_artifact_path),
            )
            paths.extend((label, path) for label, path in issue_paths if path is not None)
        unique: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for item in paths:
            if item not in seen:
                seen.add(item)
                unique.append(item)
        if not unique:
            return
        lines.append("  Logs:")
        for label, path in unique:
            lines.append(f"    {label}: {path}")

    @staticmethod
    def _append_plan_summary(
        lines: list[str],
        projection: _PresentationProjection,
    ) -> None:
        lines.append("  Plans:")
        lines.append(f"    original: {projection.original_plan_path}")
        if projection.active_plan_path is not None:
            lines.append(f"    active: {projection.active_plan_path}")
        if projection.generated_plan_path is not None:
            lines.append(f"    generated: {projection.generated_plan_path}")

    @staticmethod
    def _stream_supports_bold(stream: object) -> bool:
        try:
            isatty = getattr(stream, "isatty")
            if not callable(isatty) or not isatty():
                return False
        except Exception:
            return False
        term = os.environ.get("TERM")
        return bool(term) and term != "dumb" and "NO_COLOR" not in os.environ

    def _style_block(self, block: str) -> str:
        if not self._style_enabled:
            return block
        heading_end = block.find("\n")
        if heading_end < 0:
            heading_end = len(block)
        if heading_end == 0:
            return block
        return (
            _SGR_BOLD
            + block[:heading_end]
            + _SGR_RESET
            + block[heading_end:]
        )

    def _write_blocks(self, blocks: list[str]) -> None:
        with self._lock:
            if self._output_disabled:
                return
            stream = self._stream if self._stream is not None else sys.stderr
            try:
                if self._style_stream is not stream:
                    self._style_stream = stream
                    self._style_enabled = self._stream_supports_bold(stream)
                cleaned_blocks = [
                    self._style_block(block.strip("\n"))
                    for block in blocks
                    if block.strip("\n")
                ]
                if not cleaned_blocks:
                    return
                text = "\n\n".join(cleaned_blocks)
                prefix = "\n" if self._has_output else ""
                stream.write(prefix + text + "\n")
                stream.flush()
                self._has_output = True
            except Exception:
                # A broken stderr (for example a closed pipe) disables status
                # output for the rest of the run instead of failing the run.
                self._output_disabled = True


def _workflow_effective_role_names(workflow: WorkflowConfig) -> tuple[str, ...]:
    return tuple(dict.fromkeys(step.role for step in workflow.steps.values()))


def _workflow_applicable_team_names(
    *,
    config: WorkflowUserConfig,
    workflow: WorkflowConfig,
    role_names: tuple[str, ...],
) -> tuple[str, ...]:
    relevant_roles = set(role_names)
    team_names: list[str] = []
    if workflow.team is not None:
        team_names.append(workflow.team)
    for team_name, team_config in config.teams.items():
        if team_name == workflow.team:
            continue
        if any(role_name in team_config.roles for role_name in relevant_roles):
            team_names.append(team_name)
    return tuple(team_names)


def _append_roles_teams_lines(
    lines: list[str],
    *,
    config: WorkflowUserConfig,
    role_names: tuple[str, ...],
    team_names: tuple[str, ...],
    default_team_name: str | None = None,
) -> None:
    lines.append("Roles / Teams")
    if not role_names:
        lines.append("  roles: none")
    else:
        lines.append("  roles:")
        for role_name in role_names:
            selector = config.roles.get(role_name)
            selector_text = selector if selector is not None else "missing"
            lines.append(f"    role {role_name} -> {selector_text}")
    if not team_names:
        lines.append("  teams: none")
    else:
        lines.append("  teams:")
        relevant_roles = set(role_names)
        for team_name in team_names:
            team_config = config.teams.get(team_name)
            label = team_name
            if default_team_name is not None and team_name == default_team_name:
                label = f"{team_name} (default)"
            if team_config is None:
                lines.append(f"    team {label}: missing")
                continue
            overrides = [
                f"{role_name} -> {selector}"
                for role_name, selector in team_config.roles.items()
                if role_name in relevant_roles
            ]
            if team_config.backup_team is not None:
                overrides.append(f"backup -> {team_config.backup_team}")
            override_text = ", ".join(overrides) if overrides else "no applicable overrides"
            lines.append(f"    team {label}: {override_text}")


def _append_workflow_lines(
    lines: list[str],
    *,
    workflow_name: str,
    workflow: WorkflowConfig,
) -> None:
    lines.append(f"workflow {workflow_name}")
    if not workflow.declared_steps:
        lines.append("  (no steps declared)")
        return
    executable_names = set(workflow.steps)
    excluded_names = set(workflow.excluded_steps)
    for step_name, step in workflow.declared_steps.items():
        if step_name in excluded_names or step_name not in executable_names:
            state_word = "excluded"
        else:
            state_word = "executable"
        lines.append(f"  step {step_name} [{state_word}] role={step.role}")
        for transition in step.go:
            transition_text = f"    go -> {transition.to}"
            if transition.to == _WORKFLOW_TERMINAL_TARGET:
                transition_text += " [terminal]"
            if transition.when is not None:
                transition_text += f" when {transition.when}"
            lines.append(transition_text)


def build_workflow_show(
    *,
    config: WorkflowUserConfig,
    workflow_name: str | None = None,
) -> str:
    """Render workflow graphs, roles, and teams as plain ASCII text."""
    lines: list[str] = []
    if workflow_name is None:
        if config.roles or config.teams:
            _append_roles_teams_lines(
                lines,
                config=config,
                role_names=tuple(config.roles.keys()),
                team_names=tuple(config.teams.keys()),
            )
        for name, workflow in config.workflows.items():
            _append_workflow_lines(lines, workflow_name=name, workflow=workflow)
    else:
        workflow = config.workflows[workflow_name]
        role_names = _workflow_effective_role_names(workflow)
        team_names = _workflow_applicable_team_names(
            config=config,
            workflow=workflow,
            role_names=role_names,
        )
        _append_roles_teams_lines(
            lines,
            config=config,
            role_names=role_names,
            team_names=team_names,
            default_team_name=workflow.team,
        )
        _append_workflow_lines(lines, workflow_name=workflow_name, workflow=workflow)

    if not lines:
        return "No workflows configured"
    return "\n".join(lines)
