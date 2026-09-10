"""Public runner API for workflow execution."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from aflow.api.events import ExecutionObserver
    from aflow.api.models import PreparedRun
    from aflow.run_state import ControllerRunResult, ResumeContext
    from aflow.status import BannerRenderer
    from aflow.harnesses.session import SessionDriver
from aflow.harnesses.preflight import HarnessPreflightProbe

from aflow.harnesses.base import HarnessAdapter
from aflow.plan import ParsedPlan
from aflow.run_state import ControllerConfig
from aflow.workflow import WorkflowError, run_workflow


@dataclass(frozen=True)
class RunnerConfig:
    """Configuration for the public runner."""

    prepared_run: PreparedRun
    observer: ExecutionObserver | None = None
    banner: BannerRenderer | None = None
    adapter: HarnessAdapter | None = None
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None
    preflight_probe: HarnessPreflightProbe | None = None
    resume: ResumeContext | None = None
    control_source: Callable[[], object] | None = None
    session_driver: SessionDriver | None = None
    source_session_driver: SessionDriver | None = None
    allow_existing_launch_manifest: bool = False


class WorkflowRunner:
    """Public runner for executing prepared workflows with event observation."""

    def __init__(self, config: RunnerConfig) -> None:
        self._config = config

    def run(self) -> ControllerRunResult:
        """Execute the workflow and return the result."""
        prepared = self._config.prepared_run

        from aflow.live_config import load_live_config

        workflow_config = load_live_config(prepared.config_path).workflow_config
        workflow = workflow_config.workflows.get(prepared.workflow_name)
        if workflow is None:
            raise WorkflowError(
                f"workflow '{prepared.workflow_name}' is not configured in the current source"
            )
        team_explicit = (
            prepared.team_explicit
            if prepared.team_explicit is not None
            else prepared.team is not None
        )
        max_turns_explicit = (
            prepared.max_turns_explicit
            if prepared.max_turns_explicit is not None
            else True
        )
        start_step_explicit = (
            prepared.start_step_explicit
            if prepared.start_step_explicit is not None
            else True
        )
        effective_start_step = (
            prepared.start_step if start_step_explicit else workflow.first_step
        )
        if effective_start_step not in workflow.steps:
            raise WorkflowError(
                f"start step '{effective_start_step}' is not configured in "
                f"workflow '{prepared.workflow_name}'"
            )
        if effective_start_step in workflow.excluded_steps:
            raise WorkflowError(
                f"start step '{effective_start_step}' is excluded from "
                f"workflow '{prepared.workflow_name}'"
            )
        effective_team = prepared.team if team_explicit else workflow.team
        if effective_team is not None and effective_team not in workflow_config.teams:
            raise WorkflowError(
                f"team '{effective_team}' is not configured in the current source"
            )
        effective_max_turns = (
            prepared.max_turns
            if max_turns_explicit
            else workflow_config.aflow.max_turns
        )
        if (
            not isinstance(effective_max_turns, int)
            or isinstance(effective_max_turns, bool)
            or effective_max_turns < 1
        ):
            raise WorkflowError("current [aflow].max_turns must be a positive integer")
        effective_skipped_steps = (
            prepared.skipped_steps
            if start_step_explicit
            else tuple(workflow.steps)[: tuple(workflow.steps).index(effective_start_step)]
        )
        prepared = replace(
            prepared,
            max_turns=effective_max_turns,
            team=effective_team,
            start_step=effective_start_step,
            skipped_steps=effective_skipped_steps,
            team_explicit=team_explicit,
            max_turns_explicit=max_turns_explicit,
            start_step_explicit=start_step_explicit,
        )

        config = ControllerConfig(
            repo_root=prepared.repo_root,
            plan_path=prepared.plan_path,
            max_turns=prepared.max_turns,
            keep_runs=workflow_config.aflow.keep_runs,
            team=prepared.team,
            extra_instructions=prepared.extra_instructions,
            start_step=prepared.start_step,
            dirty_worktree_confirmed=prepared.dirty_worktree_confirmed,
            continuation_from_branch=prepared.continuation_from_branch,
            continuation_from_head=prepared.continuation_from_head,
            continuation_mode=prepared.continuation_mode,
            reserved_run_id=prepared.reserved_run_id,
            idempotency_key=prepared.idempotency_key,
            caller_scope=prepared.caller_scope,
            restarted_from_run_id=prepared.restarted_from_run_id,
            skipped_steps=prepared.skipped_steps,
            team_explicit=prepared.team_explicit,
            max_turns_explicit=prepared.max_turns_explicit,
            start_step_explicit=prepared.start_step_explicit,
        )

        parsed_plan: ParsedPlan | None = None
        if prepared.parsed_plan is not None:
            parsed_plan = prepared.parsed_plan  # type: ignore[assignment]

        result = run_workflow(
            config=config,
            workflow_config=workflow_config,
            workflow_name=prepared.workflow_name,
            parsed_plan=parsed_plan,
            startup_retry=prepared.startup_retry,
            startup_base_head_refresh_sha=prepared.startup_base_head_refresh_sha,
            dirty_worktree_confirmed=prepared.dirty_worktree_confirmed,
            config_dir=prepared.config_path,
            adapter=self._config.adapter,
            runner=self._config.runner,
            preflight_probe=self._config.preflight_probe,
            banner=self._config.banner,
            resume=self._config.resume,
            observer=self._config.observer,
            control_source=self._config.control_source,
            session_driver=self._config.session_driver,
            source_session_driver=self._config.source_session_driver,
            allow_existing_launch_manifest=self._config.allow_existing_launch_manifest,
        )
        return result


def execute_workflow(
    prepared_run: PreparedRun,
    *,
    observer: ExecutionObserver | None = None,
    banner: BannerRenderer | None = None,
    adapter: HarnessAdapter | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    preflight_probe: HarnessPreflightProbe | None = None,
    resume: ResumeContext | None = None,
    control_source: Callable[[], object] | None = None,
    session_driver: SessionDriver | None = None,
    source_session_driver: SessionDriver | None = None,
    allow_existing_launch_manifest: bool = False,
) -> ControllerRunResult:
    """Execute a prepared workflow with optional event observation.

    Args:
        prepared_run: The prepared run configuration from startup.
        observer: Optional observer for execution events.
        banner: Optional banner renderer for terminal output.
        adapter: Optional harness adapter for testing.
        runner: Optional custom runner for subprocess execution.
        preflight_probe: Optional deterministic harness environment probe.
        resume: Optional resume context for resuming a previous run.

    Returns:
        ControllerRunResult: The execution result.

    Raises:
        WorkflowError: If the workflow execution fails.
    """
    config = RunnerConfig(
        prepared_run=prepared_run,
        observer=observer,
        banner=banner,
        adapter=adapter,
        runner=runner,
        preflight_probe=preflight_probe,
        resume=resume,
        control_source=control_source,
        session_driver=session_driver,
        source_session_driver=source_session_driver,
        allow_existing_launch_manifest=allow_existing_launch_manifest,
    )
    runner_obj = WorkflowRunner(config)
    return runner_obj.run()
