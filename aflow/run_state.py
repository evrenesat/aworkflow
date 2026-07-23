from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from .plan import PlanSnapshot


WorkflowEndReason = Literal[
    "already_complete",
    "done",
    "max_turns_reached",
    "transition_end",
]

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
class ResumeContext:
    resumed_from_run_id: str
    feature_branch: str
    worktree_path: Path
    main_branch: str
    setup: tuple[str, ...]
    teardown: tuple[str, ...]
    active_plan_path: Path | None = None


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
