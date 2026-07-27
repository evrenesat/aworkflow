"""Models for startup preparation and run preparation."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Literal

from aflow.config import WorkflowConfig, WorkflowStepConfig
from aflow.plan import PlanSnapshot
from aflow.run_state import RetryContext


class StartupQuestionKind(str, Enum):
    """Types of questions that can be asked during startup."""

    CONFIRM_RECOVERY = "confirm_recovery"
    PICK_STEP = "pick_step"
    CONFIRM_BASE_HEAD_REFRESH = "confirm_base_head_refresh"
    CONFIRM_WORKTREE_DIRTY = "confirm_worktree_dirty"


@dataclass(frozen=True)
class StartupQuestion:
    """A structured question that requires user response during startup."""

    kind: StartupQuestionKind
    message: str
    options: dict[str, str] = field(default_factory=dict)
    choices: list[str] = field(default_factory=list)
    continuation_request: StartupRequest | None = None


@dataclass(frozen=True)
class StartupRequest:
    """Input request for startup preparation."""

    repo_root: Path
    plan_path: Path
    config_path: Path
    workflow_config: WorkflowConfig
    workflow_name: str | None
    start_step: str | None
    max_turns: int | None
    team: str | None
    extra_instructions: tuple[str, ...] = ()
    pre_recovered_plan: object | None = None
    startup_retry_error: str | None = None
    startup_base_head_refresh_sha: str | None = None
    dirty_worktree_confirmed: bool = False


@dataclass(frozen=True)
class AnalyzeRequest:
    """Input request for run analysis."""

    repo_root: Path
    run_id: str | None = None
    all: bool = False
    limit: int = 20
    include_noise: bool = False
    manager_context: Literal["lite", "full"] | None = None
    turn: int | None = None


@dataclass(frozen=True)
class StartupContext:
    """Intermediate state during startup preparation."""

    repo_root: Path
    plan_path: Path
    config_path: Path
    workflow_config: WorkflowConfig
    workflow_name: str
    start_step: str | None
    max_turns: int
    team: str | None
    extra_instructions: tuple[str, ...]
    parsed_plan: object
    startup_retry_error: str | None = None
    startup_base_head_refresh_sha: str | None = None
    selected_start_step: str | None = None


@dataclass(frozen=True)
class PreparedRun:
    """Ready-to-execute workflow configuration with all startup decisions made."""

    workflow_name: str
    repo_root: Path
    plan_path: Path
    config_path: Path
    max_turns: int
    team: str | None
    extra_instructions: tuple[str, ...]
    start_step: str
    startup_retry: RetryContext | None = None
    startup_base_head_refresh_sha: str | None = None
    move_completed_plan_to_done: bool = False
    parsed_plan: object | None = None
