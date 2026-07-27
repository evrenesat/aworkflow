"""Structured execution events for workflow runs."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Literal

if TYPE_CHECKING:
    from aflow.plan import PlanSnapshot
    from aflow.run_state import HarnessRecoveryContext, TurnRecord, WorkflowEndReason


class ExecutionEventType(str, Enum):
    """Types of execution events."""

    RUN_STARTED = "run_started"
    STATUS_CHANGED = "status_changed"
    TURN_STARTED = "turn_started"
    TURN_FINISHED = "turn_finished"
    MANAGER_STARTED = "manager_started"
    MANAGER_DECIDED = "manager_decided"
    QUESTION_REQUIRED = "question_required"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"


@dataclass(frozen=True)
class RunStartedEvent:
    """Event emitted when a workflow run starts."""

    event_type: Literal[ExecutionEventType.RUN_STARTED]
    timestamp: datetime
    workflow_name: str | None
    repo_root: Path | None
    plan_path: Path | None
    max_turns: int | None
    team: str | None
    start_step: str | None

    @classmethod
    def create(
        cls,
        *,
        workflow_name: str | None = None,
        repo_root: Path | None = None,
        plan_path: Path | None = None,
        max_turns: int | None = None,
        team: str | None = None,
        start_step: str | None = None,
    ) -> RunStartedEvent:
        return cls(
            event_type=ExecutionEventType.RUN_STARTED,
            timestamp=datetime.now(timezone.utc),
            workflow_name=workflow_name,
            repo_root=repo_root,
            plan_path=plan_path,
            max_turns=max_turns,
            team=team,
            start_step=start_step,
        )


@dataclass(frozen=True)
class StatusChangedEvent:
    """Event emitted when the workflow status changes."""

    event_type: Literal[ExecutionEventType.STATUS_CHANGED]
    timestamp: datetime
    status_message: str
    turns_completed: int
    active_turn: int | None
    current_step_name: str | None

    @classmethod
    def create(
        cls,
        status_message: str,
        turns_completed: int,
        active_turn: int | None = None,
        current_step_name: str | None = None,
    ) -> StatusChangedEvent:
        return cls(
            event_type=ExecutionEventType.STATUS_CHANGED,
            timestamp=datetime.now(timezone.utc),
            status_message=status_message,
            turns_completed=turns_completed,
            active_turn=active_turn,
            current_step_name=current_step_name,
        )


@dataclass(frozen=True)
class TurnStartedEvent:
    """Event emitted when a turn starts."""

    event_type: Literal[ExecutionEventType.TURN_STARTED]
    timestamp: datetime
    turn_number: int
    step_name: str
    step_role: str | None
    resolved_harness_name: str | None
    resolved_model_display: str | None

    @classmethod
    def create(
        cls,
        turn_number: int,
        step_name: str,
        step_role: str | None = None,
        resolved_harness_name: str | None = None,
        resolved_model_display: str | None = None,
    ) -> TurnStartedEvent:
        return cls(
            event_type=ExecutionEventType.TURN_STARTED,
            timestamp=datetime.now(timezone.utc),
            turn_number=turn_number,
            step_name=step_name,
            step_role=step_role,
            resolved_harness_name=resolved_harness_name,
            resolved_model_display=resolved_model_display,
        )


@dataclass(frozen=True)
class TurnFinishedEvent:
    """Event emitted when a turn finishes."""

    event_type: Literal[ExecutionEventType.TURN_FINISHED]
    timestamp: datetime
    turn_number: int
    step_name: str
    outcome: str
    duration_seconds: float | None
    stdout_artifact_path: str | None
    stderr_artifact_path: str | None
    returncode: int | None
    error: str | None
    recovery: HarnessRecoveryContext | None

    @classmethod
    def create(
        cls,
        turn_number: int,
        step_name: str,
        outcome: str,
        *,
        duration_seconds: float | None = None,
        stdout_artifact_path: str | None = None,
        stderr_artifact_path: str | None = None,
        returncode: int | None = None,
        error: str | None = None,
        recovery: HarnessRecoveryContext | None = None,
    ) -> TurnFinishedEvent:
        return cls(
            event_type=ExecutionEventType.TURN_FINISHED,
            timestamp=datetime.now(timezone.utc),
            turn_number=turn_number,
            step_name=step_name,
            outcome=outcome,
            duration_seconds=duration_seconds,
            stdout_artifact_path=stdout_artifact_path,
            stderr_artifact_path=stderr_artifact_path,
            returncode=returncode,
            error=error,
            recovery=recovery,
        )


@dataclass(frozen=True)
class ManagerStartedEvent:
    """Event emitted immediately before a manager decision is requested."""

    event_type: Literal[ExecutionEventType.MANAGER_STARTED]
    timestamp: datetime
    decision_number: int
    level: str
    trigger: str
    target_step: str | None
    target_team: str | None
    artifact_paths: dict[str, str]

    @classmethod
    def create(
        cls, *, decision_number: int, level: str, trigger: str,
        target_step: str | None, target_team: str | None,
        artifact_paths: dict[str, str],
    ) -> ManagerStartedEvent:
        return cls(
            event_type=ExecutionEventType.MANAGER_STARTED,
            timestamp=datetime.now(timezone.utc), decision_number=decision_number,
            level=level, trigger=trigger, target_step=target_step,
            target_team=target_team, artifact_paths=dict(artifact_paths),
        )


@dataclass(frozen=True)
class ManagerDecidedEvent:
    """Event emitted after a manager attempt is durably recorded."""

    event_type: Literal[ExecutionEventType.MANAGER_DECIDED]
    timestamp: datetime
    decision_number: int
    level: str
    trigger: str
    action: str
    target_step: str | None
    target_team: str | None
    report_path: str | None
    artifact_paths: dict[str, str]

    @classmethod
    def create(
        cls, *, decision_number: int, level: str, trigger: str, action: str,
        target_step: str | None, target_team: str | None,
        report_path: str | None, artifact_paths: dict[str, str],
    ) -> ManagerDecidedEvent:
        return cls(
            event_type=ExecutionEventType.MANAGER_DECIDED,
            timestamp=datetime.now(timezone.utc), decision_number=decision_number,
            level=level, trigger=trigger, action=action, target_step=target_step,
            target_team=target_team, report_path=report_path,
            artifact_paths=dict(artifact_paths),
        )


@dataclass(frozen=True)
class QuestionRequiredEvent:
    """Event emitted when a workflow requires a question to be answered."""

    event_type: Literal[ExecutionEventType.QUESTION_REQUIRED]
    timestamp: datetime
    question_kind: str
    question_message: str
    options: dict[str, str]
    choices: list[str]

    @classmethod
    def create(
        cls,
        question_kind: str,
        question_message: str,
        options: dict[str, str] | None = None,
        choices: list[str] | None = None,
    ) -> QuestionRequiredEvent:
        return cls(
            event_type=ExecutionEventType.QUESTION_REQUIRED,
            timestamp=datetime.now(timezone.utc),
            question_kind=question_kind,
            question_message=question_message,
            options=options or {},
            choices=choices or [],
        )


@dataclass(frozen=True)
class RunCompletedEvent:
    """Event emitted when a workflow run completes successfully."""

    event_type: Literal[ExecutionEventType.RUN_COMPLETED]
    timestamp: datetime
    run_dir: Path
    turns_completed: int
    final_snapshot: PlanSnapshot
    end_reason: WorkflowEndReason
    issues_accumulated: int
    recovery_summary: HarnessRecoveryContext | None
    recovery_history: tuple[HarnessRecoveryContext, ...]

    @classmethod
    def create(
        cls,
        run_dir: Path,
        turns_completed: int,
        final_snapshot: PlanSnapshot,
        end_reason: WorkflowEndReason,
        issues_accumulated: int = 0,
        recovery_summary: HarnessRecoveryContext | None = None,
        recovery_history: tuple[HarnessRecoveryContext, ...] = (),
    ) -> RunCompletedEvent:
        return cls(
            event_type=ExecutionEventType.RUN_COMPLETED,
            timestamp=datetime.now(timezone.utc),
            run_dir=run_dir,
            turns_completed=turns_completed,
            final_snapshot=final_snapshot,
            end_reason=end_reason,
            issues_accumulated=issues_accumulated,
            recovery_summary=recovery_summary,
            recovery_history=recovery_history,
        )


@dataclass(frozen=True)
class RunFailedEvent:
    """Event emitted when a workflow run fails."""

    event_type: Literal[ExecutionEventType.RUN_FAILED]
    timestamp: datetime
    run_dir: Path
    turns_completed: int
    failure_reason: str
    final_snapshot: PlanSnapshot | None
    issues_accumulated: int
    recovery_summary: HarnessRecoveryContext | None
    recovery_history: tuple[HarnessRecoveryContext, ...]

    @classmethod
    def create(
        cls,
        run_dir: Path,
        turns_completed: int,
        failure_reason: str,
        final_snapshot: PlanSnapshot | None = None,
        issues_accumulated: int = 0,
        recovery_summary: HarnessRecoveryContext | None = None,
        recovery_history: tuple[HarnessRecoveryContext, ...] = (),
    ) -> RunFailedEvent:
        return cls(
            event_type=ExecutionEventType.RUN_FAILED,
            timestamp=datetime.now(timezone.utc),
            run_dir=run_dir,
            turns_completed=turns_completed,
            failure_reason=failure_reason,
            final_snapshot=final_snapshot,
            issues_accumulated=issues_accumulated,
            recovery_summary=recovery_summary,
            recovery_history=recovery_history,
        )


ExecutionEvent = (
    RunStartedEvent
    | StatusChangedEvent
    | TurnStartedEvent
    | TurnFinishedEvent
    | ManagerStartedEvent
    | ManagerDecidedEvent
    | QuestionRequiredEvent
    | RunCompletedEvent
    | RunFailedEvent
)


class ExecutionObserver:
    """Observer interface for workflow execution events."""

    def on_event(self, event: ExecutionEvent) -> None:
        """Called when an execution event occurs."""
        pass


class CallbackObserver(ExecutionObserver):
    """Observer that calls a callback function for each event."""

    def __init__(self, callback: Callable[[ExecutionEvent], None]) -> None:
        self._callback = callback

    def on_event(self, event: ExecutionEvent) -> None:
        self._callback(event)


class CollectingObserver(ExecutionObserver):
    """Observer that collects all events in a list."""

    def __init__(self) -> None:
        self.events: list[ExecutionEvent] = []

    def on_event(self, event: ExecutionEvent) -> None:
        self.events.append(event)
