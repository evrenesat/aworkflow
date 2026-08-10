"""Authenticated-transport adapter for the daemon-owned control plane.

This module intentionally composes daemon instances but never launches a
workflow itself.  All mutation and durable-state authority remains in the
checkpoint 1-3 control-plane and daemon services.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from aflow.api.models import StartupRequest
from aflow.config import WorkflowUserConfig
from aflow.control_plane import (
    ContextBundle,
    ControlWriteResult,
    PlanRecord,
    ProjectRecord,
    RunControlRequest,
    RunEvent,
    RunPage,
    RunStatus,
    StartRunResult,
    StartupQuestionRecord,
)
from aflow.daemon import AflowDaemon, DaemonConfig, DaemonError

from .config import ControlPlaneProjectConfig


class ProjectNotAllowedError(LookupError):
    """The request did not name a configured project allowlist entry."""


class ControlPlaneUnavailableError(RuntimeError):
    """A configured daemon has not completed safe startup reconciliation."""


@dataclass(frozen=True)
class _ProjectDaemon:
    config: ControlPlaneProjectConfig
    daemon: AflowDaemon


DaemonFactory = Callable[[DaemonConfig], AflowDaemon]


class ControlPlaneService:
    """Route allowlisted project requests to their one daemon instance."""

    def __init__(
        self,
        projects: tuple[ControlPlaneProjectConfig, ...],
        *,
        daemon_factory: DaemonFactory = AflowDaemon,
    ) -> None:
        normalized = tuple(project.normalized() for project in projects)
        if len({project.id for project in normalized}) != len(normalized):
            raise ValueError("control-plane project ids must be unique")
        self._configs = {project.id: project for project in normalized}
        self._daemon_factory = daemon_factory
        self._projects: dict[str, _ProjectDaemon] = {}
        self._unavailable: set[str] = set()

    def start(self) -> None:
        """Reconcile configured daemons without starting any workflow unit."""
        for project in self._configs.values():
            daemon = self._daemon_factory(
                DaemonConfig(
                    repo_root=project.root,
                    config_path=project.config_path,
                    aflow_executable=project.aflow_executable,
                    environment_file=project.environment_file,
                    release_identity=project.release_identity,
                    environment=project.environment,
                )
            )
            self._projects[project.id] = _ProjectDaemon(project, daemon)
            try:
                daemon.start()
            except Exception:
                # Do not expose startup/configuration details through a remote
                # readiness response.  The process remains live and can serve
                # legacy planning routes, while control-plane writes fail closed.
                self._unavailable.add(project.id)

    @property
    def ready(self) -> bool:
        return not self._unavailable and all(item.daemon.ready for item in self._projects.values())

    def projects(self) -> tuple[ProjectRecord, ...]:
        return tuple(
            ProjectRecord(project_id=project.id, root=str(project.root))
            for project in sorted(self._configs.values(), key=lambda item: item.id)
        )

    def capabilities(self, project_id: str):
        return self._project(project_id).daemon.application.capabilities.get()

    def list_plans(self, project_id: str, *, limit: int, cursor: str | None) -> tuple[PlanRecord, ...]:
        return self._project(project_id).daemon.application.repository.list_plans(
            limit=limit, cursor=cursor
        )

    def list_runs(self, project_id: str, *, limit: int, cursor: str | None) -> RunPage:
        return self._project(project_id).daemon.application.repository.list_runs(
            limit=limit, cursor=cursor
        )

    def run_status(self, project_id: str, run_id: str) -> RunStatus:
        return self._project(project_id).daemon.application.repository.get_run_status(run_id)

    def find_run(self, run_id: str) -> tuple[str, RunStatus]:
        """Compatibility lookup for the legacy unscoped execution endpoint."""
        for project_id in sorted(self._configs):
            try:
                return project_id, self.run_status(project_id, run_id)
            except Exception as exc:
                from aflow.control_plane import RepositoryNotFoundError, RunIdentityError

                if isinstance(exc, (RepositoryNotFoundError, RunIdentityError)):
                    continue
                raise
        raise ProjectNotAllowedError("run is not available through an allowed project")

    def events(
        self,
        project_id: str,
        run_id: str,
        *,
        after_sequence: int | None,
        limit: int,
    ) -> tuple[RunEvent, ...]:
        item = self._project(project_id)
        return item.daemon.service.poll_events(
            run_id,
            after_sequence=after_sequence,
            limit=limit,
            authorizer=lambda _action, _status: True,
        )

    def context(
        self,
        project_id: str,
        run_id: str,
        *,
        level: str,
        full_scope: bool,
    ) -> ContextBundle:
        return self._project(project_id).daemon.application.context.get(
            run_id,
            level=level,  # type: ignore[arg-type]
            full_scope=full_scope,
        )

    def start_run(
        self,
        project_id: str,
        *,
        plan_path: str,
        workflow_name: str | None,
        team: str | None,
        start_step: str | None,
        max_turns: int | None,
        idempotency_key: str | None,
        caller_scope: str = "rest",
    ) -> StartRunResult | StartupQuestionRecord:
        item = self._project(project_id)
        request = StartupRequest(
            repo_root=item.config.root,
            plan_path=self._plan_path(item.config.root, plan_path),
            config_path=item.config.config_path,
            workflow_config=WorkflowUserConfig(),
            workflow_name=workflow_name,
            start_step=start_step,
            max_turns=max_turns,
            team=team,
        )
        return item.daemon.service.start(
            request,
            caller_scope=self._caller_scope(project_id, caller_scope),
            idempotency_key=idempotency_key,
        )

    def answer_startup(
        self,
        project_id: str,
        question_id: str,
        answer: str | int | bool,
        *,
        idempotency_key: str | None,
        caller_scope: str = "rest",
    ) -> StartRunResult | StartupQuestionRecord:
        return self._project(project_id).daemon.service.answer_startup(
            question_id,
            answer,
            caller_scope=self._caller_scope(project_id, caller_scope),
            idempotency_key=idempotency_key,
        )

    def control(
        self,
        project_id: str,
        run_id: str,
        request: RunControlRequest,
        *,
        idempotency_key: str | None,
        caller_scope: str = "rest",
    ) -> tuple[ControlWriteResult, RunStatus]:
        item = self._project(project_id)
        result = item.daemon.application.controls.apply(
            run_id,
            request,
            caller_scope=self._caller_scope(project_id, caller_scope),
            idempotency_key=idempotency_key,
        )
        return result, item.daemon.application.repository.get_run_status(run_id)

    def owner_stop(
        self,
        project_id: str,
        run_id: str,
        *,
        expected_revision: int,
        idempotency_key: str | None,
        caller_scope: str = "rest",
    ) -> RunStatus:
        return self._project(project_id).daemon.service.owner_stop(
            run_id,
            expected_revision=expected_revision,
            caller_scope=self._caller_scope(project_id, caller_scope),
            idempotency_key=idempotency_key,
        )

    def resume(
        self,
        project_id: str,
        run_id: str,
        *,
        idempotency_key: str | None,
        caller_scope: str = "rest",
    ) -> StartRunResult:
        return self._project(project_id).daemon.service.resume(
            run_id,
            caller_scope=self._caller_scope(project_id, caller_scope),
            idempotency_key=idempotency_key,
        )

    def _project(self, project_id: str) -> _ProjectDaemon:
        item = self._projects.get(project_id)
        if item is None:
            if project_id in self._configs:
                raise ControlPlaneUnavailableError("project control plane is unavailable")
            raise ProjectNotAllowedError("project is not allowed")
        if project_id in self._unavailable or not item.daemon.ready:
            raise ControlPlaneUnavailableError("project control plane is unavailable")
        return item

    @staticmethod
    def _caller_scope(project_id: str, transport: str) -> str:
        if transport not in {"rest", "mcp"}:
            raise ValueError("unsupported control-plane transport")
        return f"{transport}:{project_id}"

    @staticmethod
    def _plan_path(root: Path, requested: str) -> Path:
        path = Path(requested)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise DaemonError("plan path must be a relative contained file")
        candidate = root / path
        if candidate.is_symlink() or not candidate.is_file():
            raise DaemonError("plan path must name an existing regular file")
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError) as exc:
            raise DaemonError("plan path is outside the allowed project") from exc
        return resolved
