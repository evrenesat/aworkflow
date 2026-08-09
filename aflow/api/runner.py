"""Public runner API for workflow execution."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from aflow.api.events import ExecutionEvent, ExecutionObserver
    from aflow.api.models import PreparedRun
    from aflow.run_state import ControllerRunResult, RetryContext, ResumeContext
    from aflow.status import BannerRenderer
    from aflow.harnesses.session import SessionDriver
from aflow.harnesses.preflight import HarnessPreflightProbe

from aflow.config import WorkflowUserConfig
from aflow.harnesses.base import HarnessAdapter
from aflow.plan import ParsedPlan
from aflow.run_state import ControllerConfig
from aflow.workflow import run_workflow


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


class WorkflowRunner:
    """Public runner for executing prepared workflows with event observation."""

    def __init__(self, config: RunnerConfig) -> None:
        self._config = config

    def run(self) -> ControllerRunResult:
        """Execute the workflow and return the result."""
        prepared = self._config.prepared_run

        from aflow.config import load_workflow_config

        workflow_config = load_workflow_config(prepared.config_path)

        config = ControllerConfig(
            repo_root=prepared.repo_root,
            plan_path=prepared.plan_path,
            max_turns=prepared.max_turns,
            keep_runs=workflow_config.aflow.keep_runs,
            team=prepared.team,
            extra_instructions=prepared.extra_instructions,
            start_step=prepared.start_step,
            reserved_run_id=prepared.reserved_run_id,
            idempotency_key=prepared.idempotency_key,
            caller_scope=prepared.caller_scope,
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
    )
    runner_obj = WorkflowRunner(config)
    return runner_obj.run()
