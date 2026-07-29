from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from .plan import PlanSnapshot
from .run_state import (
    RUN_STATE_SCHEMA_VERSION,
    ControllerState,
    ReviewRejectionRecord,
    TurnRecord,
)
from .config import WorkflowConfig, WorkflowStepConfig, WorkflowUserConfig, load_workflow_config

if TYPE_CHECKING:
    from .git_status import GitSummary

_RICH_AVAILABLE = False
try:
    from rich.console import Console
    from rich.console import Group
    from rich.align import Align
    from rich.panel import Panel
    from rich.table import Table
    from rich.live import Live
    from rich.text import Text

    _RICH_AVAILABLE = True
except ImportError:
    Console = object  # type: ignore[assignment,misc]
    Live = object  # type: ignore[assignment,misc]

_STDERR_CONSOLE: Console | None = None
if _RICH_AVAILABLE:
    _STDERR_CONSOLE = Console(file=None, stderr=True)  # type: ignore[call-arg]

_UNSET = object()
_WORKFLOW_TERMINAL_TARGET = "END"
WorkflowStepVisualKind = Literal["active", "inactive", "excluded", "skipped"]
WorkflowTransitionTargetKind = Literal["active", "inactive", "excluded", "skipped", "terminal"]


@dataclass(frozen=True)
class WorkflowGraphSource:
    declared_steps: dict[str, WorkflowStepConfig]
    executable_steps: dict[str, WorkflowStepConfig]
    excluded_step_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkflowGraphContext:
    source: WorkflowGraphSource
    visual_start_step_skipped_step_names: tuple[str, ...] = ()
    current_step_name: str | None = None
    current_turn_is_running: bool = False


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


def _workflow_step_kind(
    *,
    step_name: str,
    context: WorkflowGraphContext,
) -> WorkflowStepVisualKind:
    if context.current_turn_is_running and step_name == context.current_step_name:
        return "active"
    if (
        step_name in context.source.excluded_step_names
        or step_name not in context.source.executable_steps
    ):
        return "excluded"
    if step_name in context.visual_start_step_skipped_step_names:
        return "skipped"
    return "inactive"


def _workflow_step_style(kind: WorkflowStepVisualKind) -> str:
    if kind == "active":
        return "bold green"
    if kind == "inactive":
        return "green"
    return "grey50"


def _workflow_transition_target_kind(
    *,
    target_name: str,
    context: WorkflowGraphContext,
) -> WorkflowTransitionTargetKind:
    if target_name == _WORKFLOW_TERMINAL_TARGET:
        return "terminal"
    return _workflow_step_kind(step_name=target_name, context=context)


def _workflow_transition_style(
    *,
    source_kind: WorkflowStepVisualKind,
    target_kind: WorkflowTransitionTargetKind,
) -> str:
    if source_kind == "active" and target_kind not in {"excluded", "skipped"}:
        return "white"
    if source_kind in {"excluded", "skipped"} or target_kind in {"excluded", "skipped"}:
        return "grey50"
    return "green"


def _turn_transition_text(record: TurnRecord) -> Text | None:
    if record.chosen_transition is None:
        return None
    text = Text()
    text.append("  ├─go→ ", style="dim")
    text.append(record.chosen_transition, style="bold")
    if record.chosen_transition_condition is not None:
        text.append(f" [{record.chosen_transition_condition}]", style="dim")
    return text


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
    return f"0/{snapshot.total_checkpoint_count}"


def _status_display(state: ControllerState) -> str:
    if state.status_message == "completed" and state.end_reason is not None:
        if state.end_reason == "already_complete":
            return "completed: already complete"
        if state.end_reason == "done":
            return "completed: done"
        if state.end_reason == "max_turns_reached":
            return "completed: max turns reached"
        return "completed: transition to END"
    return state.status_message


def _git_row(summary: GitSummary) -> str:
    if summary.modified_count == 0 and summary.added_count == 0 and summary.removed_count == 0:
        return f"clean since start | +{summary.lines_added}/-{summary.lines_removed} | {summary.commit_count} commits"
    return (
        f"M {summary.modified_count}, A {summary.added_count}, D {summary.removed_count}"
        f" | +{summary.lines_added}/-{summary.lines_removed}"
        f" | {summary.commit_count} commits"
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


def _duration_display(started_at: datetime, finished_at: datetime | None = None) -> str:
    end_at = finished_at or datetime.now(timezone.utc)
    delta = end_at - started_at
    total_seconds = max(0, int(delta.total_seconds()))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}h {minutes:02d}m {seconds:02d}s"
    if minutes > 0:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


def _turn_outcome_style(outcome: str, *, current: bool = False) -> str:
    if current:
        return "bold green"
    if outcome == "completed":
        return "green"
    if outcome == "retry-scheduled":
        return "magenta"
    if outcome in {"harness-failed", "plan-invalid", "transition-failed", "failed"}:
        return "red"
    if outcome == "running":
        return "yellow"
    return "cyan"


def _active_scope_rejections(state: ControllerState) -> tuple[ReviewRejectionRecord, ...]:
    scope = state.active_implementation_scope
    if scope is None:
        return ()
    return tuple(sorted(
        (item for item in state.review_rejection_history if item.scope_id == scope.scope_id),
        key=lambda item: item.rejection_number,
    ))


def _render_review_rejection_history(state: ControllerState) -> Panel | None:
    records = _active_scope_rejections(state)
    if not records:
        return None
    body = Table.grid(padding=(0, 1))
    body.add_column()
    current_run_id = state.run_id
    for record in records:
        label = f"Rejection {record.rejection_number} · review turn {record.review_turn_number}"
        if current_run_id is not None and record.source_run_id != current_run_id:
            label += f" · run {record.source_run_id}"
        body.add_row(Text(label, style="bold yellow"))
        worker = f"turn {record.reviewed_implementation_turn_number}"
        if record.reviewed_worker_team:
            worker += f" · {record.reviewed_worker_team}"
        if record.reviewed_worker_selector:
            worker += f" · {record.reviewed_worker_selector}"
        body.add_row(Text(f"Reviewed worker: {worker}"))
        body.add_row(Text("Review: ") + Text(record.review_summary))
        if record.repair_plan_summary:
            body.add_row(Text("Required fixes: ") + Text(record.repair_plan_summary))
        body.add_row(Text(f"Details: {record.review_stdout_artifact_path}"))
        if record.repair_plan_path:
            body.add_row(Text(f"Fix plan: {record.repair_plan_path}"))
    return Panel(body, title="Current checkpoint review history", border_style="yellow", padding=(0, 1))


def _render_turn_history(state: ControllerState) -> Group | Text | None:
    if not state.turn_history:
        return None
    panels: list[Panel] = []
    for record in state.turn_history:
        is_current = (
            state.current_turn_started_at is not None
            and record.turn_number == state.active_turn
            and record.outcome == "running"
        )
        border_style = _turn_outcome_style(record.outcome, current=is_current)
        body = Table.grid(padding=(0, 1))
        body.add_column(style="bold cyan", no_wrap=True)
        body.add_column()
        body.add_row("Step", record.step_name)
        if record.step_role is not None or record.resolved_selector is not None:
            role_value = record.step_role or "-"
            if record.resolved_selector is not None:
                body.add_row("Role/Selector", f"{role_value} -> {record.resolved_selector}")
            else:
                body.add_row("Role", role_value)
        body.add_row("Harness/Model", record.resolved_model_display)
        if record.active_plan_path is not None:
            body.add_row("Active Plan", record.active_plan_path)
        body.add_row("Duration", _duration_display(record.started_at, record.finished_at))
        body.add_row("Outcome", record.outcome)
        if record.triggering_rejection_number is not None:
            active_scope = state.active_implementation_scope
            rejection = next((
                item for item in _active_scope_rejections(state)
                if item.rejection_number == record.triggering_rejection_number
            ), None)
            if active_scope is not None and rejection is not None:
                body.add_row(
                    "Re-implementation",
                    Text(f"after rejection {record.triggering_rejection_number}"),
                )
                body.add_row(
                    "Why rejected",
                    Text(rejection.repair_plan_summary or rejection.review_summary),
                )
        transition_text = _turn_transition_text(record)
        if transition_text is not None:
            body.add_row("Transition", transition_text)
        if record.issues_summary_path is not None:
            body.add_row("Issues", record.issues_summary_path)
        if record.stdout_artifact_path is not None:
            body.add_row("Stdout", record.stdout_artifact_path)
        if record.stderr_artifact_path is not None:
            body.add_row("Stderr", record.stderr_artifact_path)
        panels.append(
            Panel(
                body,
                title=f"Turn {record.turn_number:03d}",
                border_style=border_style,
                padding=(0, 1),
            )
        )
    return Group(*panels)


def _render_workflow_graph(
    *,
    workflow_name: str | None,
    workflow_steps: dict[str, WorkflowStepConfig] | None,
    current_step_name: str | None,
    state: ControllerState,
    source: WorkflowGraphSource | None = None,
) -> Group | Text | None:
    source = source or _resolve_workflow_graph_source(
        workflow_name=workflow_name,
        workflow_steps=workflow_steps,
    )
    if not source.declared_steps:
        if workflow_name is None:
            return None
        return Text(workflow_name, style="bold magenta")

    visual_start_step_skipped_step_names = _visual_start_skipped_step_names(
        declared_steps=source.declared_steps,
        executable_steps=source.executable_steps,
        excluded_step_names=source.excluded_step_names,
        selected_start_step=state.selected_start_step,
    )
    context = WorkflowGraphContext(
        source=source,
        visual_start_step_skipped_step_names=visual_start_step_skipped_step_names,
        current_step_name=current_step_name,
        current_turn_is_running=(
            state.current_turn_started_at is not None
            and state.turn_history
            and state.turn_history[-1].turn_number == state.active_turn
            and state.turn_history[-1].outcome == "running"
        ),
    )
    graph_items: list[object] = []
    for step_name, step in source.declared_steps.items():
        kind = _workflow_step_kind(step_name=step_name, context=context)
        step_style = _workflow_step_style(kind)
        body = Text()
        body.append(step_name, style=step_style)
        body.append("\n")
        body.append(step.role, style="dim")
        graph_items.append(
            Panel(
                body,
                border_style=step_style,
                padding=(0, 1),
            )
        )
        arrows = Text()
        for transition in step.go:
            target_kind = _workflow_transition_target_kind(
                target_name=transition.to,
                context=context,
            )
            transition_style = _workflow_transition_style(
                source_kind=kind,
                target_kind=target_kind,
            )
            arrows.append("  ├─go→ ", style=transition_style)
            arrows.append(transition.to, style=f"bold {transition_style}" if transition_style != "white" else "bold")
            if transition.when is not None:
                arrows.append(f" [{transition.when}]", style=transition_style)
            arrows.append("\n")
        graph_items.append(arrows)
    return Align.right(Group(*graph_items))


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


def _render_roles_table(
    *,
    config: WorkflowUserConfig,
    role_names: tuple[str, ...],
) -> Table:
    table = Table(box=None, show_header=True, header_style="bold cyan", expand=True)
    table.add_column("Role", style="bold cyan", no_wrap=True)
    table.add_column("Selector")
    if not role_names:
        table.add_row("-", "none")
        return table
    for role_name in role_names:
        selector = config.roles.get(role_name)
        table.add_row(role_name, selector if selector is not None else "missing")
    return table


def _render_teams_table(
    *,
    config: WorkflowUserConfig,
    team_names: tuple[str, ...],
    role_names: tuple[str, ...],
    default_team_name: str | None = None,
) -> Table:
    table = Table(box=None, show_header=True, header_style="bold cyan", expand=True)
    table.add_column("Team", style="bold cyan", no_wrap=True)
    table.add_column("Overrides")
    if not team_names:
        table.add_row("-", "none")
        return table
    relevant_roles = set(role_names)
    for team_name in team_names:
        team_config = config.teams.get(team_name)
        team_label = team_name
        if default_team_name is not None and team_name == default_team_name:
            team_label = f"{team_name} (default)"
        if team_config is None:
            table.add_row(team_label, "missing")
            continue
        overrides = [
            f"{role_name} -> {selector}"
            for role_name, selector in team_config.roles.items()
            if role_name in relevant_roles
        ]
        if team_config.backup_team is not None:
            overrides.append(f"backup -> {team_config.backup_team}")
        table.add_row(team_label, ", ".join(overrides) if overrides else "-")
    return table


def _render_roles_teams_section(
    *,
    config: WorkflowUserConfig,
    role_names: tuple[str, ...],
    team_names: tuple[str, ...],
    title: str,
    default_team_name: str | None = None,
) -> Panel:
    body = Group(
        _render_roles_table(config=config, role_names=role_names),
        _render_teams_table(
            config=config,
            team_names=team_names,
            role_names=role_names,
            default_team_name=default_team_name,
        ),
    )
    return Panel(body, title=title, border_style="blue")


def _render_workflow_show_section(
    *,
    workflow_name: str,
    workflow: WorkflowConfig,
) -> Panel:
    graph_source = WorkflowGraphSource(
        declared_steps=dict(workflow.declared_steps),
        executable_steps=dict(workflow.steps),
        excluded_step_names=workflow.excluded_steps,
    )
    graph = _render_workflow_graph(
        workflow_name=workflow_name,
        workflow_steps=workflow.steps,
        current_step_name=None,
        state=ControllerState(last_snapshot=PlanSnapshot(None, 0, 0, False)),
        source=graph_source,
    )
    return Panel(graph or Text(""), title=workflow_name, border_style="blue")


def build_workflow_show(
    *,
    config: WorkflowUserConfig,
    workflow_name: str | None = None,
) -> Group | Panel | Text | None:
    if not _RICH_AVAILABLE:
        return None

    sections: list[object] = []
    if workflow_name is None:
        if config.roles or config.teams:
            sections.append(
                _render_roles_teams_section(
                    config=config,
                    role_names=tuple(config.roles.keys()),
                    team_names=tuple(config.teams.keys()),
                    title="Roles / Teams",
                )
            )
        for name, workflow in config.workflows.items():
            sections.append(_render_workflow_show_section(workflow_name=name, workflow=workflow))
    else:
        workflow = config.workflows[workflow_name]
        role_names = _workflow_effective_role_names(workflow)
        team_names = _workflow_applicable_team_names(
            config=config,
            workflow=workflow,
            role_names=role_names,
        )
        sections.append(
            _render_roles_teams_section(
                config=config,
                role_names=role_names,
                team_names=team_names,
                title="Roles / Teams",
                default_team_name=workflow.team,
            )
        )
        sections.append(_render_workflow_show_section(workflow_name=workflow_name, workflow=workflow))

    if not sections:
        return Text("No workflows configured", style="yellow")
    return Group(*sections)


def _build_summary_table(
    *,
    workflow_name: str | None,
    config_harness: str | None,
    config_model: str | None,
    config_effort: str | None,
    config_max_turns: int,
    config_plan_path: Path,
    original_plan_path: Path | None,
    active_plan_path: Path | None,
    new_plan_path: Path | None,
    state: ControllerState,
    git_summary: GitSummary | None,
    banner_files_limit: int,
) -> Table:
    table = Table.grid(padding=(0, 1))
    table.add_column(style="bold cyan")
    table.add_column()

    table.add_row("Elapsed", _elapsed(state.run_started_at))

    if state.run_id is not None:
        table.add_row("Run ID", state.run_id)
    if state.resumed_from_run_id is not None:
        table.add_row("Resumed From", state.resumed_from_run_id)

    if workflow_name is not None:
        table.add_row("Workflow", workflow_name)
    if state.frozen_run_identity is not None:
        table.add_row("State Schema", str(RUN_STATE_SCHEMA_VERSION))
        table.add_row(
            "Frozen Config",
            state.frozen_run_identity.config_fingerprint[:12],
        )
    table.add_row(
        "Override File",
        "present" if state.override_file_present else "absent",
    )
    if state.override_result is not None:
        result = state.override_result
        table.add_row("Last Override", f"{result.status}: {result.message}")
        if result.status == "rejected":
            table.add_row("Override Action", "correct overrides.toml and resume")

    table.add_row("Checkpoint", _checkpoint_display(state.last_snapshot))
    name = state.last_snapshot.current_checkpoint_name or "-"
    table.add_row("Name", name)
    table.add_row(
        "Turn",
        f"{state.active_turn}/{state.effective_max_turns or config_max_turns}",
    )

    if original_plan_path is not None:
        table.add_row("Original Plan", original_plan_path.name)
    if new_plan_path is not None and new_plan_path.is_file() and new_plan_path != active_plan_path:
        table.add_row("Generated Plan", new_plan_path.name)

    if workflow_name is None:
        table.add_row("Plan", str(config_plan_path))

    if state.issues_summary_path is not None:
        table.add_row("Issues", state.issues_summary_path)

    if state.manager_history:
        manager = state.manager_history[-1]
        table.add_row("Manager", f"{manager.level} / {manager.trigger} / {manager.action}")
    if state.pending_manager_notes is not None:
        table.add_row("Manager Notes", f"pending for {state.pending_manager_notes.target_step}")
    if state.pending_step_team_override is not None:
        override = state.pending_step_team_override
        table.add_row("Manager Upgrade", f"{override.target_step}: {override.target_team}")
    if state.last_manager_report_path is not None:
        table.add_row("Manager Report", state.last_manager_report_path)

    if git_summary is not None:
        table.add_row("Git", _git_row(git_summary))
        files_text = _files_row(git_summary.changed_paths, limit=banner_files_limit)
        if files_text is not None:
            table.add_row("Files", files_text)

    table.add_row("Status", _status_display(state))
    return table


def build_banner(
    *,
    workflow_name: str | None = None,
    current_step_name: str | None = None,
    workflow_steps: dict[str, WorkflowStepConfig] | None = None,
    workflow_graph_source: WorkflowGraphSource | None = None,
    config_harness: str | None = None,
    config_model: str | None = None,
    config_effort: str | None = None,
    config_max_turns: int,
    config_plan_path: Path,
    original_plan_path: Path | None = None,
    active_plan_path: Path | None = None,
    new_plan_path: Path | None = None,
    config_banner_files_limit: int = 10,
    state: ControllerState,
    git_summary: GitSummary | None = None,
) -> Panel | None:
    if not _RICH_AVAILABLE:
        return None
    source = workflow_graph_source or _resolve_workflow_graph_source(
        workflow_name=workflow_name,
        workflow_steps=workflow_steps,
    )
    summary = _build_summary_table(
        workflow_name=workflow_name,
        config_harness=config_harness,
        config_model=config_model,
        config_effort=config_effort,
        config_max_turns=config_max_turns,
        config_plan_path=config_plan_path,
        original_plan_path=original_plan_path,
        active_plan_path=active_plan_path,
        new_plan_path=new_plan_path,
        state=state,
        git_summary=git_summary,
        banner_files_limit=config_banner_files_limit,
    )
    turn_history = _render_turn_history(state)
    rejection_history = _render_review_rejection_history(state)
    workflow_graph = _render_workflow_graph(
        workflow_name=workflow_name,
        workflow_steps=workflow_steps,
        current_step_name=current_step_name,
        state=state,
        source=source,
    )
    left_items: list[object] = []
    if rejection_history is not None:
        left_items.append(rejection_history)
    if turn_history is not None:
        left_items.append(turn_history)
    left_items.append(summary)
    root = Table.grid(expand=True)
    root.add_column(ratio=3)
    root.add_column(ratio=2)
    root.add_row(Group(*left_items), workflow_graph or Text(""))
    title_source = original_plan_path or config_plan_path
    title = Text(_plan_title(title_source), style="bold magenta")
    return Panel(root, title=title, border_style="blue")


class BannerRenderer:
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
        console: Console | None = None,
        repo_root: Path | None = None,
        refresh_interval_seconds: float = 1.0,
        git_poll_interval_seconds: float = 10.0,
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
        self._console = console or _STDERR_CONSOLE
        self._repo_root = repo_root
        self._refresh_interval_seconds = refresh_interval_seconds
        self._git_poll_interval_seconds = git_poll_interval_seconds
        self._live: Live | None = None
        self._lock = threading.Lock()
        self._state: ControllerState | None = None
        self._git_summary: GitSummary | None = None
        self._stop_event = threading.Event()
        self._refresh_thread: threading.Thread | None = None

    def set_context(
        self,
        *,
        current_step_name: str | object = _UNSET,
        active_plan_path: Path | object = _UNSET,
        new_plan_path: Path | object = _UNSET,
        config_harness: str | object = _UNSET,
        config_model: str | object = _UNSET,
        config_effort: str | object = _UNSET,
    ) -> None:
        with self._lock:
            if current_step_name is not _UNSET:
                self._current_step_name = current_step_name
            if active_plan_path is not _UNSET:
                self._active_plan_path = active_plan_path
            if new_plan_path is not _UNSET:
                self._new_plan_path = new_plan_path
            if config_harness is not _UNSET:
                self._config_harness = config_harness
            if config_model is not _UNSET:
                self._config_model = config_model
            if config_effort is not _UNSET:
                self._config_effort = config_effort

    def _build(self, state: ControllerState, git_summary: GitSummary | None = None) -> Panel | None:
        return build_banner(
            workflow_name=self._workflow_name,
            current_step_name=self._current_step_name,
            workflow_steps=self._workflow_steps,
            workflow_graph_source=self._workflow_graph_source,
            config_harness=self._config_harness,
            config_model=self._config_model,
            config_effort=self._config_effort,
            config_max_turns=self._config_max_turns,
            config_plan_path=self._config_plan_path,
            config_banner_files_limit=self._config_banner_files_limit,
            original_plan_path=self._original_plan_path,
            active_plan_path=self._active_plan_path,
            new_plan_path=self._new_plan_path,
            state=state,
            git_summary=git_summary,
        )

    def _refresh_loop(self) -> None:
        from .git_status import capture_baseline, summarize_since_baseline

        baseline = None
        if self._repo_root is not None:
            baseline = capture_baseline(self._repo_root)

        last_git_poll = 0.0

        while not self._stop_event.is_set():
            now = time.monotonic()

            if self._repo_root is not None and baseline is not None:
                if now - last_git_poll >= self._git_poll_interval_seconds:
                    summary = summarize_since_baseline(self._repo_root, baseline)
                    with self._lock:
                        self._git_summary = summary
                    last_git_poll = now

            with self._lock:
                state = self._state
                git_summary = self._git_summary
                live = self._live

            if state is not None and live is not None:
                with self._lock:
                    panel = self._build(state, git_summary)
                if panel is not None:
                    live.update(panel)

            self._stop_event.wait(timeout=self._refresh_interval_seconds)

    def _start_refresh_thread(self) -> None:
        self._stop_event.clear()
        t = threading.Thread(target=self._refresh_loop, daemon=True, name="aflow-banner-refresh")
        self._refresh_thread = t
        t.start()

    def _stop_refresh_thread(self) -> None:
        self._stop_event.set()
        if self._refresh_thread is not None:
            self._refresh_thread.join(timeout=2.0)
            self._refresh_thread = None

    def start(self, state: ControllerState) -> None:
        if not _RICH_AVAILABLE:
            return
        with self._lock:
            self._state = state
        panel = self._build(state)
        if panel is None:
            return
        self._live = Live(panel, console=self._console, refresh_per_second=4, vertical_overflow="visible")
        self._live.start()
        self._start_refresh_thread()

    def update(self, state: ControllerState) -> None:
        if not _RICH_AVAILABLE:
            return
        with self._lock:
            self._state = state
            git_summary = self._git_summary
            live = self._live
        if live is None:
            return
        panel = self._build(state, git_summary)
        if panel is None:
            return
        live.update(panel)

    def pause(self) -> None:
        if self._live is None or not _RICH_AVAILABLE:
            return
        self._stop_refresh_thread()
        self._live.stop()
        self._live = None

    def resume(self, state: ControllerState) -> None:
        if not _RICH_AVAILABLE:
            return
        with self._lock:
            self._state = state
            git_summary = self._git_summary
        panel = self._build(state, git_summary)
        if panel is None:
            return
        self._live = Live(panel, console=self._console, refresh_per_second=4, vertical_overflow="visible")
        self._live.start()
        self._start_refresh_thread()

    def stop(self, state: ControllerState) -> None:
        if not _RICH_AVAILABLE:
            return
        self._stop_refresh_thread()
        if self._live is None:
            return
        with self._lock:
            git_summary = self._git_summary
        panel = self._build(state, git_summary)
        if panel is None:
            return
        self._live.update(panel)
        self._live.stop()
        self._live = None
