from __future__ import annotations

import atexit
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

if _RICH_AVAILABLE:
    from .terminal_viewport import (
        ScrollableViewport,
        TerminalInputSession,
        ViewportAction,
        ViewportEvent,
    )

_STDERR_CONSOLE: Console | None = None
if _RICH_AVAILABLE:
    _STDERR_CONSOLE = Console(file=None, stderr=True)  # type: ignore[call-arg]

_UNSET = object()
_WORKFLOW_TERMINAL_TARGET = "END"
WorkflowStepVisualKind = Literal["active", "inactive", "excluded", "skipped"]
WorkflowTransitionTargetKind = Literal["active", "inactive", "excluded", "skipped", "terminal"]
_WORKFLOW_STEP_STATE_LABELS: dict[WorkflowStepVisualKind, str] = {
    "active": "[active]",
    "inactive": "[inactive]",
    "excluded": "[excluded]",
    "skipped": "[skipped]",
}


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


def _literal_text(value: object, *, style: str | None = None) -> Text:
    """Render controller-owned values without allowing Rich markup parsing."""
    return Text(str(value), style=style)


def _labeled_text(label: str, value: object) -> Text:
    text = Text(label, style="bold cyan")
    text.append(": ")
    if isinstance(value, Text):
        text.append(value.plain)
    else:
        text.append(str(value))
    return text


def _add_labeled_row(table: Table, label: str, value: object) -> None:
    table.add_row(_literal_text(label, style="bold cyan"), _literal_text(value))


def _active_scope_rejections(state: ControllerState) -> tuple[ReviewRejectionRecord, ...]:
    scope = state.active_implementation_scope
    if scope is None:
        return ()
    return tuple(sorted(
        (item for item in state.review_rejection_history if item.scope_id == scope.scope_id),
        key=lambda item: item.rejection_number,
    ))


def _render_review_rejection_history(state: ControllerState) -> Group | None:
    records = _active_scope_rejections(state)
    if not records:
        return None
    items: list[object] = [Text("Current checkpoint review history", style="bold yellow")]
    current_run_id = state.run_id
    for record in records:
        items.append(Text(""))
        label = f"Rejection {record.rejection_number} · review turn {record.review_turn_number}"
        if current_run_id is not None and record.source_run_id != current_run_id:
            label += f" · run {record.source_run_id}"
        items.append(Text(label, style="bold yellow"))
        worker = f"turn {record.reviewed_implementation_turn_number}"
        if record.reviewed_worker_team:
            worker += f" · {record.reviewed_worker_team}"
        if record.reviewed_worker_selector:
            worker += f" · {record.reviewed_worker_selector}"
        items.append(_labeled_text("Reviewed worker", worker))
        items.append(_labeled_text("Review", record.review_summary))
        if record.repair_plan_summary:
            items.append(_labeled_text("Required fixes", record.repair_plan_summary))
        items.append(_labeled_text("Details", record.review_stdout_artifact_path))
        if record.repair_plan_path:
            items.append(_labeled_text("Fix plan", record.repair_plan_path))
    return Group(*items)


def _render_turn_history(state: ControllerState) -> Group | Text | None:
    if not state.turn_history:
        return None
    sections: list[object] = [Text("Turn history", style="bold cyan")]
    for record in state.turn_history:
        is_current = (
            state.current_turn_started_at is not None
            and record.turn_number == state.active_turn
            and record.outcome == "running"
        )
        sections.append(Text(""))
        sections.append(
            Text(
                f"Turn {record.turn_number:03d}",
                style=_turn_outcome_style(record.outcome, current=is_current),
            )
        )
        body = Table.grid(padding=(0, 1))
        body.add_column(style="bold cyan", no_wrap=True)
        body.add_column()
        _add_labeled_row(body, "Step", record.step_name)
        if record.step_role is not None or record.resolved_selector is not None:
            role_value = record.step_role or "-"
            if record.resolved_selector is not None:
                _add_labeled_row(body, "Role/Selector", f"{role_value} -> {record.resolved_selector}")
            else:
                _add_labeled_row(body, "Role", role_value)
        _add_labeled_row(body, "Harness/Model", record.resolved_model_display)
        if record.active_plan_path is not None:
            _add_labeled_row(body, "Active Plan", record.active_plan_path)
        _add_labeled_row(body, "Duration", _duration_display(record.started_at, record.finished_at))
        _add_labeled_row(body, "Outcome", record.outcome)
        if record.triggering_rejection_number is not None:
            active_scope = state.active_implementation_scope
            rejection = next((
                item for item in _active_scope_rejections(state)
                if item.rejection_number == record.triggering_rejection_number
            ), None)
            if active_scope is not None and rejection is not None:
                _add_labeled_row(
                    body,
                    "Re-implementation",
                    f"after rejection {record.triggering_rejection_number}",
                )
                _add_labeled_row(
                    body,
                    "Why rejected",
                    rejection.repair_plan_summary or rejection.review_summary,
                )
        transition_text = _turn_transition_text(record)
        if transition_text is not None:
            body.add_row(_literal_text("Transition", style="bold cyan"), transition_text)
        if record.issues_summary_path is not None:
            _add_labeled_row(body, "Issues", record.issues_summary_path)
        if record.stdout_artifact_path is not None:
            _add_labeled_row(body, "Stdout", record.stdout_artifact_path)
        if record.stderr_artifact_path is not None:
            _add_labeled_row(body, "Stderr", record.stderr_artifact_path)
        sections.append(body)
    return Group(*sections)


def _render_workflow_graph(
    *,
    workflow_name: str | None,
    workflow_steps: dict[str, WorkflowStepConfig] | None,
    current_step_name: str | None,
    state: ControllerState,
    source: WorkflowGraphSource | None = None,
    live_dashboard: bool = False,
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
        if live_dashboard:
            step_line = Text()
            step_line.append(step_name, style=step_style)
            step_line.append(f" {_WORKFLOW_STEP_STATE_LABELS[kind]}", style=step_style)
            graph_items.append(step_line)
            role_line = Text("Role: ", style="bold cyan")
            role_line.append(step.role, style="dim")
            graph_items.append(role_line)
        else:
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
        if live_dashboard:
            for transition in step.go:
                target_kind = _workflow_transition_target_kind(
                    target_name=transition.to,
                    context=context,
                )
                transition_style = _workflow_transition_style(
                    source_kind=kind,
                    target_kind=target_kind,
                )
                arrows = Text()
                arrows.append("  ├─go→ ", style=transition_style)
                arrows.append(
                    transition.to,
                    style=f"bold {transition_style}" if transition_style != "white" else "bold",
                )
                if transition.when is not None:
                    arrows.append(f" [{transition.when}]", style=transition_style)
                graph_items.append(arrows)
        else:
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
                arrows.append(
                    transition.to,
                    style=f"bold {transition_style}" if transition_style != "white" else "bold",
                )
                if transition.when is not None:
                    arrows.append(f" [{transition.when}]", style=transition_style)
                arrows.append("\n")
            graph_items.append(arrows)
    if live_dashboard:
        return Group(*graph_items)
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
    return Panel(body, border_style="blue", title=title)


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
    table.add_column(style="bold cyan", no_wrap=True)
    table.add_column()

    _add_labeled_row(table, "Elapsed", _elapsed(state.run_started_at))

    if state.run_id is not None:
        _add_labeled_row(table, "Run ID", state.run_id)
    if state.resumed_from_run_id is not None:
        _add_labeled_row(table, "Resumed From", state.resumed_from_run_id)

    if workflow_name is not None:
        _add_labeled_row(table, "Workflow", workflow_name)
    if state.frozen_run_identity is not None:
        _add_labeled_row(table, "State Schema", RUN_STATE_SCHEMA_VERSION)
        _add_labeled_row(
            table,
            "Frozen Config",
            state.frozen_run_identity.config_fingerprint[:12],
        )
    _add_labeled_row(
        table,
        "Override File",
        "present" if state.override_file_present else "absent",
    )
    if state.override_result is not None:
        result = state.override_result
        _add_labeled_row(table, "Last Override", f"{result.status}: {result.message}")
        if result.status == "rejected":
            _add_labeled_row(table, "Override Action", "correct overrides.toml and resume")

    _add_labeled_row(table, "Checkpoint", _checkpoint_display(state.last_snapshot))
    name = state.last_snapshot.current_checkpoint_name or "-"
    _add_labeled_row(table, "Name", name)
    _add_labeled_row(
        table,
        "Turn",
        f"{state.active_turn}/{state.effective_max_turns or config_max_turns}",
    )

    if original_plan_path is not None:
        _add_labeled_row(table, "Original Plan", original_plan_path.name)
    if new_plan_path is not None and new_plan_path.is_file() and new_plan_path != active_plan_path:
        _add_labeled_row(table, "Generated Plan", new_plan_path.name)

    if workflow_name is None:
        _add_labeled_row(table, "Plan", config_plan_path)

    if state.issues_summary_path is not None:
        _add_labeled_row(table, "Issues", state.issues_summary_path)

    if state.manager_history:
        manager = state.manager_history[-1]
        _add_labeled_row(table, "Manager", f"{manager.level} / {manager.trigger} / {manager.action}")
    if state.pending_manager_notes is not None:
        _add_labeled_row(table, "Manager Notes", f"pending for {state.pending_manager_notes.target_step}")
    if state.pending_step_team_override is not None:
        override = state.pending_step_team_override
        _add_labeled_row(table, "Manager Upgrade", f"{override.target_step}: {override.target_team}")
    if state.last_manager_report_path is not None:
        _add_labeled_row(table, "Manager Report", state.last_manager_report_path)
    if state.scope_pressure_reason is not None:
        _add_labeled_row(
            table,
            "Scope Pressure",
            state.scope_pressure_reason,
        )
    if state.pending_repartition is not None:
        pending = state.pending_repartition
        pending_text = _literal_text(pending.stage)
        if pending.failed_stage is not None:
            pending_text.append(f" / failed: {pending.failed_stage}")
        _add_labeled_row(table, "Repartition", pending_text)
    if state.repartition_history:
        latest = state.repartition_history[-1]
        _add_labeled_row(
            table,
            "Latest Split",
            f"{latest.generation_id} / {len(latest.partition_ids)} parts / "
            f"{latest.current_disposition}",
        )
        _add_labeled_row(
            table,
            "Split Children",
            " | ".join(latest.child_summaries),
        )
        _add_labeled_row(
            table,
            "Split Target",
            f"{latest.resolved_target_step} ({latest.resolved_target_role}) / "
            f"current {latest.current_partition_id}",
        )
        _add_labeled_row(
            table,
            "Split Hashes",
            f"envelope {latest.envelope_sha256[:12]} / "
            f"proposal {latest.proposal_sha256[:12]} / "
            f"candidate {latest.candidate_plan_sha256[:12]}",
        )
        _add_labeled_row(
            table,
            "Split Evidence",
            f"{latest.envelope_artifact_path} / "
            f"{latest.proposal_artifact_path} / "
            f"{latest.candidate_artifact_path} / "
            f"{latest.mechanical_validation_artifact_path} / "
            f"{latest.semantic_verdict_artifact_path}",
        )
    scope = state.active_implementation_scope
    if (
        scope is not None
        and scope.current_partition_generation_id is not None
        and scope.current_partition_id is not None
    ):
        _add_labeled_row(
            table,
            "Current Partition",
            f"{scope.current_partition_id} / {scope.current_partition_generation_id}",
        )

    if git_summary is not None:
        _add_labeled_row(table, "Git", _git_row(git_summary))
        files_text = _files_row(git_summary.changed_paths, limit=banner_files_limit)
        if files_text is not None:
            _add_labeled_row(table, "Files", files_text)

    _add_labeled_row(table, "Status", _status_display(state))
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
) -> Group | Text | None:
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
        live_dashboard=True,
    )
    sections: list[object] = []
    title_source = original_plan_path or config_plan_path
    sections.append(Text(_plan_title(title_source), style="bold magenta"))
    if rejection_history is not None:
        sections.extend((Text(""), rejection_history))
    if turn_history is not None:
        sections.extend((Text(""), turn_history))
    if workflow_graph is not None:
        sections.extend((Text(""), Text("Workflow graph", style="bold cyan"), workflow_graph))
    sections.extend((Text(""), Text("Summary", style="bold cyan"), summary))
    return Group(*sections)


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
        refresh_interval_seconds: float = 3.0,
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
        self._render_wake = threading.Event()
        self._refresh_thread: threading.Thread | None = None
        self._input_session: TerminalInputSession | None = None
        self._viewport: ScrollableViewport | None = None
        self._interactive = False
        self._last_panel: Group | Text | None = None
        self._cleanup_lock = threading.Lock()
        self._cleanup_complete = threading.Event()
        self._cleanup_complete.set()
        self._cleanup_in_progress = False
        self._cleanup_owner: threading.Thread | None = None
        self._startup_complete = False
        self._emergency_cleanup_registered = False
        self._emergency_cleanup_callback: object | None = None

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

    def _build(self, state: ControllerState, git_summary: GitSummary | None = None) -> Group | Text | None:
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

        try:
            baseline = None
            if self._repo_root is not None:
                baseline = capture_baseline(self._repo_root)

            next_refresh_at = time.monotonic() + self._refresh_interval_seconds
            next_git_poll_at = None
            if self._repo_root is not None and baseline is not None:
                next_git_poll_at = time.monotonic() + self._git_poll_interval_seconds

            while True:
                now = time.monotonic()
                deadlines = [next_refresh_at]
                if next_git_poll_at is not None:
                    deadlines.append(next_git_poll_at)
                timeout = max(0.0, min(deadlines) - now)

                input_session = self._input_session
                events: tuple[ViewportAction | ViewportEvent, ...] = ()
                if input_session is not None:
                    woke = self._render_wake.wait(timeout=timeout)
                    if self._stop_event.is_set():
                        return
                    if woke:
                        events = input_session.drain_events()
                elif self._stop_event.wait(timeout=timeout):
                    return

                now = time.monotonic()
                if next_git_poll_at is not None and now >= next_git_poll_at:
                    if self._stop_event.is_set():
                        return
                    summary = summarize_since_baseline(self._repo_root, baseline)
                    if self._stop_event.is_set():
                        return
                    with self._lock:
                        if self._stop_event.is_set():
                            return
                        self._git_summary = summary
                    next_git_poll_at = time.monotonic() + self._git_poll_interval_seconds
                    now = time.monotonic()

                interaction_due = bool(events)
                periodic_due = now >= next_refresh_at
                if (interaction_due or periodic_due) and not self._stop_event.is_set():
                    self._refresh_live(events)
                    next_refresh_at = time.monotonic() + self._refresh_interval_seconds
        except Exception:
            # A background renderer failure must not strand a raw terminal,
            # Rich Live instance, or reader thread.  Once startup commits, the
            # renderer-owned path also leaves the last successfully built
            # document in scrollback.
            with self._lock:
                startup_complete = self._startup_complete
            self._cleanup_interactive(
                state=None,
                render_final=False,
                print_snapshot=startup_complete,
                suppress_errors=True,
            )

    def _refresh_live(
        self,
        events: tuple[ViewportAction | ViewportEvent, ...] = (),
    ) -> None:
        with self._lock:
            if self._stop_event.is_set():
                return
            state = self._state
            git_summary = self._git_summary
            live = self._live
            if state is None or live is None:
                return
            panel = self._build(state, git_summary)
            if panel is None or self._stop_event.is_set():
                return
            self._last_panel = panel
            viewport = self._viewport
            if viewport is not None:
                for event in events:
                    if isinstance(event, ViewportAction):
                        viewport.apply(event)
                viewport.renderable = panel
                renderable = viewport
            else:
                renderable = panel
            live.update(renderable, refresh=False)
            live.refresh()

    def _start_refresh_thread(self) -> None:
        self._stop_event.clear()
        self._render_wake.clear()
        t = threading.Thread(target=self._refresh_loop, daemon=True, name="aflow-banner-refresh")
        self._refresh_thread = t
        t.start()

    def _stop_refresh_thread(self) -> None:
        with self._lock:
            self._stop_event.set()
        self._render_wake.set()
        thread = self._refresh_thread
        if thread is not None:
            if thread is not threading.current_thread():
                try:
                    started = thread.is_alive()
                except RuntimeError:
                    started = False
                if started:
                    thread.join()
            self._refresh_thread = None

    def _new_input_session(self) -> TerminalInputSession | None:
        session: TerminalInputSession | None = None
        try:
            session = TerminalInputSession(
                self._console,
                wake_event=self._render_wake,
            )
            session.start()
            return session
        except Exception:
            if session is not None:
                try:
                    if not getattr(session, "is_restored", False):
                        session.close()
                except Exception:
                    pass
            return None
        except BaseException as exc:
            if session is not None:
                try:
                    if not getattr(session, "is_restored", False):
                        session.close()
                except BaseException as cleanup_error:
                    raise exc from cleanup_error
            raise

    def _close_unattached_start_attempt(
        self,
        *,
        live: Live | None,
        input_session: TerminalInputSession | None,
        interactive: bool,
    ) -> None:
        """Release resources that were acquired before renderer attachment."""

        pending_base: BaseException | None = None

        def attempt(operation: object) -> BaseException | None:
            nonlocal pending_base
            try:
                operation()  # type: ignore[operator]
            except Exception:
                return None
            except BaseException as exc:
                if pending_base is None:
                    pending_base = exc
                return exc
            return None

        if live is not None:
            live_stop_interrupted = attempt(live.stop) is not None
            if live_stop_interrupted and interactive:
                attempt(lambda: self._console.show_cursor(True))
                attempt(lambda: self._console.set_alt_screen(False))
        if input_session is not None:
            close_interrupted = attempt(input_session.close) is not None
            if close_interrupted and not getattr(input_session, "is_settled", False):
                attempt(input_session.close)

        if pending_base is not None:
            raise pending_base

    def _start_live_attempt(
        self,
        panel: Group | Text,
        *,
        input_session: TerminalInputSession | None,
        interactive: bool,
    ) -> None:
        """Start one fully transactional interactive or fallback attempt."""

        viewport = ScrollableViewport(panel) if interactive else None
        renderable = viewport if viewport is not None else panel
        live_kwargs = {
            "console": self._console,
            "auto_refresh": False,
            "screen": interactive,
            "vertical_overflow": "crop" if interactive else "visible",
        }
        live: Live | None = None
        attached = False
        try:
            live = Live(renderable, **live_kwargs)
            live.start()
            with self._lock:
                self._live = live
                self._input_session = input_session
                self._viewport = viewport
                self._interactive = interactive
                self._last_panel = panel
                self._startup_complete = False
            attached = True
            if interactive:
                self._register_emergency_cleanup()
            self._start_refresh_thread()
            with self._lock:
                if self._live is live:
                    self._startup_complete = True
        except BaseException as exc:
            if attached:
                try:
                    self._cleanup_interactive(
                        state=None,
                        render_final=False,
                        print_snapshot=False,
                        suppress_errors=True,
                    )
                except BaseException as cleanup_error:
                    if isinstance(exc, Exception):
                        raise cleanup_error from exc
                    raise exc from cleanup_error
            else:
                try:
                    self._close_unattached_start_attempt(
                        live=live,
                        input_session=input_session,
                        interactive=interactive,
                    )
                except BaseException as cleanup_error:
                    if isinstance(exc, Exception):
                        raise cleanup_error from exc
                    raise exc from cleanup_error
            raise

    def _start_live(self, panel: Group | Text) -> None:
        input_session = self._new_input_session()
        if input_session is None:
            self._start_live_attempt(
                panel,
                input_session=None,
                interactive=False,
            )
            return

        try:
            self._start_live_attempt(
                panel,
                input_session=input_session,
                interactive=True,
            )
        except Exception as interactive_error:
            try:
                self._start_live_attempt(
                    panel,
                    input_session=None,
                    interactive=False,
                )
            except BaseException as fallback_error:
                raise fallback_error from interactive_error

    def start(self, state: ControllerState) -> None:
        if not _RICH_AVAILABLE:
            return
        with self._lock:
            self._state = state
            panel = self._build(state)
        if panel is None:
            return
        self._start_live(panel)

    def update(self, state: ControllerState) -> None:
        if not _RICH_AVAILABLE:
            return
        with self._lock:
            self._state = state

    def pause(self) -> None:
        if not _RICH_AVAILABLE:
            return
        with self._lock:
            if self._live is None:
                return
            state = self._state
        if state is not None:
            self._cleanup_interactive(
                state=state,
                render_final=True,
                print_snapshot=True,
                suppress_errors=True,
            )

    def resume(self, state: ControllerState) -> None:
        if not _RICH_AVAILABLE:
            return
        with self._lock:
            self._state = state
            panel = self._build(state, self._git_summary)
        if panel is None:
            return
        self._start_live(panel)

    def stop(self, state: ControllerState) -> None:
        if not _RICH_AVAILABLE:
            return
        self._cleanup_interactive(
            state=state,
            render_final=True,
            print_snapshot=True,
            suppress_errors=True,
        )

    def _register_emergency_cleanup(self) -> None:
        with self._lock:
            if self._emergency_cleanup_registered:
                return
            callback = self._emergency_cleanup
            self._emergency_cleanup_callback = callback
            self._emergency_cleanup_registered = True
        atexit.register(callback)

    def _unregister_emergency_cleanup(self) -> None:
        with self._lock:
            if not self._emergency_cleanup_registered:
                return
            callback = self._emergency_cleanup_callback
        settled = callback is None
        if callback is not None:
            try:
                atexit.unregister(callback)  # type: ignore[arg-type]
            except Exception:
                settled = True
            except BaseException:
                # Keep ownership published so the cleanup transaction can
                # retry after an interrupted unregister attempt.
                raise
            else:
                settled = True
        if settled:
            with self._lock:
                if self._emergency_cleanup_callback is callback:
                    self._emergency_cleanup_callback = None
                    self._emergency_cleanup_registered = False

    def _emergency_cleanup(self) -> None:
        self._cleanup_interactive(
            state=None,
            render_final=False,
            print_snapshot=True,
            suppress_errors=True,
        )

    def _cleanup_interactive(
        self,
        *,
        state: ControllerState | None,
        render_final: bool,
        print_snapshot: bool,
        suppress_errors: bool,
    ) -> None:
        """Stop every renderer-owned resource exactly once.

        Cleanup ownership is elected under ``_cleanup_lock``, but the owner
        releases that lock before stopping or joining the render thread.  A
        render-thread failure that races with an external owner therefore
        returns immediately instead of waiting on the same gate that the
        external owner holds while it joins the thread.
        """

        current_thread = threading.current_thread()
        wait_for_cleanup: threading.Event | None = None
        owns_cleanup = False
        with self._cleanup_lock:
            if self._cleanup_in_progress:
                if self._cleanup_owner is current_thread:
                    return
                if self._refresh_thread is current_thread:
                    return
                wait_for_cleanup = self._cleanup_complete
            else:
                # Publish ownership before inspecting renderer state.  The
                # render thread may hold ``_lock`` inside a Rich call and
                # needs to observe this handoff from its exception path.
                self._cleanup_in_progress = True
                self._cleanup_owner = current_thread
                self._cleanup_complete.clear()
                owns_cleanup = True

        if wait_for_cleanup is not None:
            wait_for_cleanup.wait()
            return
        if not owns_cleanup:
            self._unregister_emergency_cleanup()
            return

        try:
            input_session = self._input_session
            errors: list[Exception] = []
            pending_base: BaseException | None = None

            def attempt(operation: object) -> BaseException | None:
                nonlocal pending_base
                try:
                    operation()  # type: ignore[operator]
                except Exception as exc:
                    errors.append(exc)
                    return None
                except BaseException as exc:
                    if pending_base is None:
                        pending_base = exc
                    return exc
                return None

            if input_session is not None:
                stop_reader_interrupted = attempt(input_session.stop_reader) is not None
                if stop_reader_interrupted:
                    # A signal can interrupt join() before it clears the
                    # renderer-owned thread reference.  Retry once while the
                    # cleanup owner still has exclusive teardown ownership.
                    attempt(input_session.stop_reader)
            refresh_thread_interrupted = attempt(self._stop_refresh_thread) is not None
            if refresh_thread_interrupted:
                attempt(self._stop_refresh_thread)

            with self._lock:
                live = self._live
                viewport = self._viewport
                interactive = self._interactive and input_session is not None
                if state is not None:
                    self._state = state
                panel = self._last_panel
                if live is not None and state is not None:
                    try:
                        built_panel = self._build(state, self._git_summary)
                    except Exception as exc:
                        errors.append(exc)
                    except BaseException as exc:
                        if pending_base is None:
                            pending_base = exc
                    else:
                        if built_panel is not None:
                            panel = built_panel
                            self._last_panel = built_panel

            if live is not None:
                if render_final and panel is not None:
                    def render_final_panel() -> None:
                        if interactive and viewport is not None:
                            viewport.renderable = panel
                            live.update(viewport, refresh=False)
                            live.refresh()
                        else:
                            live.update(panel, refresh=False)

                    attempt(render_final_panel)
                live_stop_interrupted = attempt(live.stop) is not None
                if live_stop_interrupted and interactive:
                    # Rich marks Live stopped near the beginning of stop().
                    # If an interrupt lands before its own finally block, use
                    # only public Console controls for the narrow recovery.
                    attempt(lambda: self._console.show_cursor(True))
                    attempt(lambda: self._console.set_alt_screen(False))

            if input_session is not None:
                close_interrupted = attempt(input_session.close) is not None
                if close_interrupted and not getattr(input_session, "is_settled", False):
                    attempt(input_session.close)

            unregister_interrupted = attempt(self._unregister_emergency_cleanup) is not None
            if unregister_interrupted and self._emergency_cleanup_registered:
                attempt(self._unregister_emergency_cleanup)

            if interactive and print_snapshot and panel is not None:
                attempt(lambda: self._console.print(panel))

            # Keep ownership visible until Live and terminal restoration have
            # both been attempted.  Cleanup callers are gated above, so they
            # cannot observe a detached-but-unrestored renderer.
            with self._lock:
                owns_live = self._live is live
                if owns_live:
                    self._live = None
                if self._viewport is viewport:
                    self._viewport = None
                if self._input_session is input_session:
                    self._input_session = None
                if owns_live or live is None:
                    self._interactive = False
                    self._startup_complete = False

            if pending_base is not None:
                raise pending_base
            if errors and not suppress_errors:
                raise errors[0]
        finally:
            with self._cleanup_lock:
                self._cleanup_in_progress = False
                self._cleanup_owner = None
                self._cleanup_complete.set()
