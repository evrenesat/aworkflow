from __future__ import annotations

import sys
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
)
from .config import WorkflowConfig, WorkflowStepConfig, WorkflowUserConfig, load_workflow_config

if TYPE_CHECKING:
    from .git_status import GitBaseline, GitSummary

_WORKFLOW_TERMINAL_TARGET = "END"

_DEFAULT_VALUE_LIMIT = 200
_DEFAULT_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


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


class BannerRenderer:
    """Plain append-only status renderer.

    Every meaningful state transition, turn finalization, and the final
    summary is written as one deterministic ``key=value`` record line to
    stderr.  Identical consecutive snapshots are deduplicated, output never
    depends on terminal size or input, and no ANSI, cursor, or alternate
    screen sequences are emitted.  The class name is retained because the
    ``banner`` call sites and the ``banner_files_limit`` configuration option
    are unchanged; the renderer itself owns no threads, terminal input, or
    cleanup handlers.
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
        self._last_body: str | None = None
        self._output_disabled = False

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
            self._last_body = None
            self._capture_baseline_locked()
        self._emit(state, event="start")

    def update(self, state: ControllerState) -> None:
        with self._lock:
            self._state = state
        self._emit(state, event="update")

    def stop(self, state: ControllerState) -> None:
        with self._lock:
            self._state = state
        self._emit(state, event="final", final=True)

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

    def _build_body(self, state: ControllerState, *, final: bool) -> str:
        fields: list[str] = []
        add = fields.append

        if state.run_id is not None:
            add(_record_field("run", state.run_id))
        if state.resumed_from_run_id is not None:
            add(_record_field("resumed_from", state.resumed_from_run_id))
        add(_record_field("status", _status_display(state)))
        if self._workflow_name is not None:
            add(_record_field("workflow", self._workflow_name))
        if state.frozen_run_identity is not None:
            add(_record_field(
                "frozen",
                state.frozen_run_identity.config_fingerprint[:12],
            ))

        step_name = self._current_step_name
        last_turn: TurnRecord | None = state.turn_history[-1] if state.turn_history else None
        if step_name is None and last_turn is not None:
            step_name = last_turn.step_name
        if step_name is not None:
            add(_record_field("step", step_name))

        add(_record_field("checkpoint", _checkpoint_display(state.last_snapshot)))
        if state.last_snapshot.current_checkpoint_name:
            add(_record_field("checkpoint_name", state.last_snapshot.current_checkpoint_name))
        add(_record_field(
            "turn",
            f"{state.active_turn}/{state.effective_max_turns or self._config_max_turns}",
        ))
        if state.selected_start_step is not None:
            add(_record_field("start_step", state.selected_start_step))
        skipped_names = _visual_start_skipped_step_names(
            declared_steps=self._workflow_graph_source.declared_steps,
            executable_steps=self._workflow_graph_source.executable_steps,
            excluded_step_names=self._workflow_graph_source.excluded_step_names,
            selected_start_step=state.selected_start_step,
        )
        if skipped_names:
            add(_record_field("skipped", ",".join(skipped_names)))

        team = state.current_team or state.current_team_override
        if team is not None:
            add(_record_field("team", team))
        if last_turn is not None:
            if last_turn.step_role is not None or last_turn.resolved_selector is not None:
                role_value = last_turn.step_role or "-"
                if last_turn.resolved_selector is not None:
                    add(_record_field("role", f"{role_value}:{last_turn.resolved_selector}"))
                else:
                    add(_record_field("role", role_value))
            add(_record_field("model", last_turn.resolved_model_display))
            if last_turn.chosen_transition is not None:
                transition = last_turn.chosen_transition
                if last_turn.chosen_transition_condition is not None:
                    transition += f" when {last_turn.chosen_transition_condition}"
                add(_record_field("transition", transition))
            add(_record_field("outcome", last_turn.outcome))
        elif self._config_harness is not None:
            add(_record_field("harness", self._config_harness))

        if state.override_file_present:
            add(_record_field("override_file", "present"))
        else:
            add(_record_field("override_file", "absent"))
        if state.override_result is not None:
            result = state.override_result
            add(_record_field("override_result", f"{result.status}: {result.message}"))
            if result.status == "rejected":
                add(_record_field("override_action", "correct overrides.toml and resume"))

        if state.manager_history:
            manager = state.manager_history[-1]
            add(_record_field(
                "manager",
                f"{manager.level}/{manager.trigger}/{manager.action}",
            ))
        if state.pending_manager_notes is not None:
            add(_record_field(
                "manager_notes",
                f"pending for {state.pending_manager_notes.target_step}",
            ))
        if state.pending_step_team_override is not None:
            override = state.pending_step_team_override
            add(_record_field(
                "manager_upgrade",
                f"{override.target_step}:{override.target_team}",
            ))
        if state.last_manager_report_path is not None:
            add(_record_field(
                "manager_report",
                state.last_manager_report_path,
                limit=None,
            ))
        rejections = _active_scope_rejections(state)
        if rejections:
            latest = rejections[-1]
            add(_record_field("review_rejection", latest.rejection_number))
            if latest.review_stdout_artifact_path is not None:
                add(_record_field(
                    "review_artifact",
                    latest.review_stdout_artifact_path,
                    limit=None,
                ))
        if state.scope_pressure_reason is not None:
            add(_record_field("scope_pressure", state.scope_pressure_reason))
        if state.pending_repartition is not None:
            pending = state.pending_repartition
            pending_value = pending.stage
            if pending.failed_stage is not None:
                pending_value += f" / failed: {pending.failed_stage}"
            add(_record_field("repartition", pending_value))
        if state.repartition_history:
            latest = state.repartition_history[-1]
            add(_record_field(
                "split",
                f"{latest.generation_id} / {len(latest.partition_ids)} parts / "
                f"{latest.current_disposition}",
            ))
            if latest.candidate_artifact_path:
                add(_record_field(
                    "split_artifact",
                    latest.candidate_artifact_path,
                    limit=None,
                ))
        scope = state.active_implementation_scope
        if (
            scope is not None
            and scope.current_partition_generation_id is not None
            and scope.current_partition_id is not None
        ):
            add(_record_field(
                "partition",
                f"{scope.current_partition_id}/{scope.current_partition_generation_id}",
            ))

        if self._original_plan_path is not None:
            add(_record_field("plan", self._original_plan_path.name))
        if (
            self._new_plan_path is not None
            and self._new_plan_path.is_file()
            and self._new_plan_path != self._active_plan_path
        ):
            add(_record_field("generated_plan", self._new_plan_path.name))
        if self._active_plan_path is not None:
            add(_record_field("active_plan", self._active_plan_path, limit=None))

        git_summary = self._current_git_summary()
        if git_summary is not None:
            add(_record_field("git", _git_row(git_summary)))
            files_text = _files_row(
                git_summary.changed_paths,
                limit=self._config_banner_files_limit,
            )
            if files_text is not None:
                add(_record_field("files", files_text))

        if last_turn is not None and last_turn.stdout_artifact_path is not None:
            add(_record_field(
                "artifact",
                last_turn.stdout_artifact_path,
                limit=None,
            ))
        if state.issues_summary_path is not None:
            add(_record_field("issues", state.issues_summary_path, limit=None))

        if final:
            add(_record_field("elapsed", _elapsed(state.run_started_at)))
        return " ".join(fields)

    def _emit(self, state: ControllerState, *, event: str, final: bool = False) -> None:
        try:
            body = self._build_body(state, final=final)
        except Exception:
            # Rendering must never alter workflow execution or finalization.
            return
        with self._lock:
            if event == "update" and body == self._last_body:
                return
            self._last_body = body
            timestamp = self._clock().strftime(_DEFAULT_TIME_FORMAT)
        line = " ".join((
            "aflow",
            _record_field("time", timestamp),
            _record_field("event", event),
            body,
        ))
        self._write_line(line)

    def _write_line(self, line: str) -> None:
        with self._lock:
            if self._output_disabled:
                return
            stream = self._stream if self._stream is not None else sys.stderr
            try:
                stream.write(line + "\n")
                stream.flush()
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
