"""Authenticated-transport adapter for the daemon-owned control plane.

This module intentionally composes daemon instances but never launches a
workflow itself.  All mutation and durable-state authority remains in the
checkpoint 1-3 control-plane and daemon services.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import os
from pathlib import Path
import re
from threading import RLock

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
    RunRepository,
    RunStatus,
    StartRunResult,
    StartupQuestionRecord,
)
from aflow.daemon import AflowDaemon, DaemonConfig, DaemonError

from .project_registry import (
    ProjectRegistry,
    ProjectRegistryError,
    ProjectRegistryRecord,
)


class ProjectNotAllowedError(LookupError):
    """The request did not name a configured project allowlist entry."""


class ControlPlaneUnavailableError(RuntimeError):
    """A configured daemon has not completed safe startup reconciliation."""


@dataclass(frozen=True)
class _ProjectDaemon:
    record: ProjectRegistryRecord
    root: Path
    config_path: Path
    daemon: AflowDaemon


DaemonFactory = Callable[[DaemonConfig], AflowDaemon]
_ENVIRONMENT_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")


@dataclass(frozen=True)
class ControlPlaneServiceConfig:
    """Shared release inputs paired with the one project registry."""

    registry: ProjectRegistry
    aflow_executable: Path
    environment_file: Path
    release_identity: str
    environment: Mapping[str, str]


class ControlPlaneService:
    """Route allowlisted project requests to their one daemon instance."""

    def __init__(
        self,
        registry: ProjectRegistry | ControlPlaneServiceConfig | tuple[()],
        *,
        aflow_executable: Path | None = None,
        environment_file: Path | None = None,
        release_identity: str | None = None,
        environment: Mapping[str, str] | None = None,
        daemon_factory: DaemonFactory = AflowDaemon,
    ) -> None:
        if isinstance(registry, ControlPlaneServiceConfig):
            aflow_executable = registry.aflow_executable
            environment_file = registry.environment_file
            release_identity = registry.release_identity
            environment = registry.environment
            registry = registry.registry
        if isinstance(registry, tuple):
            if registry:
                raise ValueError("static control-plane projects are not supported")
            self._registry = None
        else:
            if (
                aflow_executable is None
                or environment_file is None
                or release_identity is None
            ):
                raise ValueError("shared daemon release inputs are required")
            self._registry = registry
        self._aflow_executable = aflow_executable
        self._environment_file = environment_file
        self._release_identity = release_identity
        self._environment = dict(environment or {})
        if self._registry is not None:
            self._validate_shared_release_inputs()
        self._daemon_factory = daemon_factory
        self._projects: dict[str, _ProjectDaemon] = {}
        self._unavailable: dict[str, str] = {}
        self._lock = RLock()
        self._project_locks_guard = RLock()
        self._project_locks: dict[str, RLock] = {}

    def project_lock(self, project_id: str) -> RLock:
        """Return a project lock before any control-plane or daemon lock."""
        with self._project_locks_guard:
            return self._project_locks.setdefault(project_id, RLock())

    def _validate_shared_release_inputs(self) -> None:
        """Canonicalize immutable process inputs once for all project daemons."""
        assert self._aflow_executable is not None
        assert self._environment_file is not None
        executable_source = Path(self._aflow_executable).expanduser()
        environment_source = Path(self._environment_file).expanduser()
        try:
            executable = executable_source.resolve(strict=True)
            environment_file = environment_source.resolve(strict=True)
        except OSError as exc:
            raise ValueError(
                "shared control-plane release inputs are unavailable"
            ) from exc
        if (
            executable_source.is_symlink()
            or not executable.is_file()
            or not os.access(executable, os.X_OK)
        ):
            raise ValueError("shared aflow executable must be a regular executable")
        if environment_source.is_symlink() or not environment_file.is_file():
            raise ValueError("shared environment file must be a regular file")
        if self._release_identity is None or not self._release_identity.strip():
            raise ValueError("shared release identity is required")
        for key, value in self._environment.items():
            if (
                not isinstance(key, str)
                or _ENVIRONMENT_NAME_RE.fullmatch(key) is None
                or not isinstance(value, str)
                or any(marker in value for marker in ("\x00", "\n", "\r"))
            ):
                raise ValueError("shared daemon environment is invalid")
        self._aflow_executable = executable
        self._environment_file = environment_file

    def start(self) -> None:
        """Reconcile configured daemons without starting any workflow unit."""
        # Dynamic projects are composed on first use. Reading validates every
        # registry record now without making one broken project suppress peers.
        for record in self._records():
            try:
                self._resolve(record.id)
            except Exception:
                self._unavailable[record.id] = "project_registration_invalid"
            else:
                if self._unavailable.get(record.id) == "project_registration_invalid":
                    self._unavailable.pop(record.id, None)

    @property
    def ready(self) -> bool:
        return all(error is None for error in self.readiness().values())

    def readiness(self) -> dict[str, str | None]:
        """Validate and return bounded readiness independently for every project."""
        result: dict[str, str | None] = {}
        for record in self._records():
            try:
                self._project(record.id)
            except Exception:
                result[record.id] = self._unavailable.get(
                    record.id, "project_control_plane_unavailable"
                )
                continue
            result[record.id] = None
        return result

    def projects(self) -> tuple[ProjectRecord, ...]:
        projects: list[ProjectRecord] = []
        for record in self._records():
            if self._registry is not None:
                root = self._registry.declared_root(record.id)
            else:
                raise ControlPlaneUnavailableError("project registry is unavailable")
            projects.append(ProjectRecord(project_id=record.id, root=str(root)))
        return tuple(projects)

    def ready_capabilities(self) -> dict[str, object]:
        """Return healthy capabilities while retaining errors in readiness."""
        capabilities: dict[str, object] = {}
        for record in self._records():
            try:
                capabilities[record.id] = self.capabilities(record.id)
            except ControlPlaneUnavailableError:
                continue
        return capabilities

    def capabilities(self, project_id: str):
        return self._project(project_id).daemon.application.capabilities.get()

    def list_plans(
        self, project_id: str, *, limit: int, cursor: str | None
    ) -> tuple[PlanRecord, ...]:
        return self._project(project_id).daemon.application.repository.list_plans(
            limit=limit, cursor=cursor
        )

    def list_runs(self, project_id: str, *, limit: int, cursor: str | None) -> RunPage:
        item = self._project(project_id)
        page = item.daemon.application.repository.list_runs(limit=limit, cursor=cursor)
        return RunPage(
            runs=tuple(item.daemon.service.run_status(run.run_id) for run in page.runs),
            next_cursor=page.next_cursor,
        )

    def run_status(self, project_id: str, run_id: str) -> RunStatus:
        return self._project(project_id).daemon.service.run_status(run_id)

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
        with self.project_lock(project_id):
            item = self._project(project_id)
            request = StartupRequest(
                repo_root=item.root,
                plan_path=self._plan_path(item.root, plan_path),
                config_path=item.config_path,
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
        with self.project_lock(project_id):
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
        with self.project_lock(project_id):
            return self._project(project_id).daemon.service.resume(
                run_id,
                caller_scope=self._caller_scope(project_id, caller_scope),
                idempotency_key=idempotency_key,
            )

    def _project(self, project_id: str) -> _ProjectDaemon:
        with self.project_lock(project_id), self._lock:
            try:
                record, root, daemon_config = self._resolve(project_id)
            except Exception as exc:
                try:
                    known_record = self._record(project_id)
                except ProjectRegistryError as registry_exc:
                    raise ControlPlaneUnavailableError(
                        "project registry is unavailable"
                    ) from registry_exc
                if known_record is None:
                    raise ProjectNotAllowedError("project is not allowed") from exc
                self._unavailable[project_id] = "project_registration_invalid"
                raise ControlPlaneUnavailableError(
                    "project control plane is unavailable"
                ) from exc
            item = self._projects.get(project_id)
            if item is not None and item.root != root:
                raise ControlPlaneUnavailableError(
                    "registered project identity changed"
                )
            if self._unavailable.get(project_id) == "project_registration_invalid":
                self._unavailable.pop(project_id, None)
            if item is None:
                try:
                    daemon = self._daemon_factory(daemon_config)
                    item = _ProjectDaemon(
                        record, root, daemon_config.config_path, daemon
                    )
                    daemon.start()
                except Exception as exc:
                    self._unavailable[project_id] = "project_daemon_start_failed"
                    raise ControlPlaneUnavailableError(
                        "project control plane is unavailable"
                    ) from exc
                self._projects[project_id] = item
                self._unavailable.pop(project_id, None)
            if project_id in self._unavailable or not item.daemon.ready:
                raise ControlPlaneUnavailableError(
                    "project control plane is unavailable"
                )
            return item

    def owned_run_snapshots(
        self, project_id: str
    ) -> tuple[tuple[str, str, str | None, str | None], ...]:
        """Return bounded (run_id, status, workflow, frozen fingerprint) per owned run.

        Legacy runs are excluded: they can never be resumed or launched by the
        control plane, so they never block a configuration save.  When no
        daemon can compose for the project (for example a project that has no
        configuration document yet), the durable run identities are enumerated
        through the production repository and any evidence fails closed.
        """
        try:
            item = self._project(project_id)
        except ControlPlaneUnavailableError:
            return self._uncomposable_run_snapshots(project_id)
        snapshots: list[tuple[str, str, str | None, str | None, str | None]] = []
        cursor: str | None = None
        while True:
            page = item.daemon.application.repository.list_runs(
                limit=1_000, cursor=cursor
            )
            for record in page.runs:
                status = item.daemon.service.run_status(record.run_id)
                if status.ownership != "control_plane":
                    continue
                manifest = item.daemon.application.repository.get_launch_manifest(
                    record.run_id
                )
                snapshots.append(
                    (
                        status.run_id,
                        status.status,
                        status.workflow_name
                        or (manifest.workflow_name if manifest is not None else None),
                        manifest.frozen_config_fingerprint
                        if manifest is not None
                        else None,
                        item.daemon.application.repository.get_frozen_config_path(status.run_id),
                    )
                )
            if page.next_cursor is None:
                return tuple(snapshots)
            cursor = page.next_cursor

    def _uncomposable_run_snapshots(
        self, project_id: str
    ) -> tuple[tuple[str, str, str | None, str | None], ...]:
        """Classify durable run state without a composed daemon.

        The repository can classify owned launch phases and legacy history
        independently.  Legacy runs remain read-only and therefore never
        block configuration, while any owned evidence is returned for the
        configuration service to classify conservatively.
        """
        if self._registry is None:
            raise ControlPlaneUnavailableError("project registry is unavailable")
        try:
            _, root = self._registry.resolve(project_id)
        except ProjectRegistryError as exc:
            raise ControlPlaneUnavailableError(
                "project registration is unavailable"
            ) from exc
        repository = RunRepository(root)
        snapshots: list[tuple[str, str, str | None, str | None, str | None]] = []
        cursor: str | None = None
        while True:
            page = repository.list_runs(limit=1_000, cursor=cursor)
            for status in page.runs:
                if status.ownership != "control_plane":
                    continue
                manifest = repository.get_launch_manifest(status.run_id)
                snapshots.append(
                    (
                        status.run_id,
                        status.status,
                        status.workflow_name
                        or (manifest.workflow_name if manifest is not None else None),
                        manifest.frozen_config_fingerprint
                        if manifest is not None
                        else None,
                    )
                )
            if page.next_cursor is None:
                return tuple(snapshots)
            cursor = page.next_cursor

    def reload_project_config(self, project_id: str) -> None:
        """Recompose one cached daemon from the committed configuration files.

        The previously composed daemon object and all durable run state are
        never mutated in place: a fresh daemon is built from the same shared
        release inputs and replaces only the cache entry, and only while the
        project proves it owns no active workflow unit.
        """
        with self.project_lock(project_id), self._lock:
            item = self._projects.get(project_id)
        if item is None:
            # Composed lazily on next use, directly from the committed files.
            return
        try:
            if self._has_active_unit(item):
                raise ControlPlaneUnavailableError(
                    "project owns an active workflow unit"
                )
            record, root, daemon_config = self._resolve(project_id)
            daemon = self._daemon_factory(daemon_config)
            daemon.start()
        except Exception as exc:
            with self._lock:
                self._projects.pop(project_id, None)
                self._unavailable[project_id] = "project_daemon_start_failed"
            raise ControlPlaneUnavailableError(
                "project control plane is unavailable"
            ) from exc
        with self.project_lock(project_id), self._lock:
            self._projects[project_id] = _ProjectDaemon(
                record, root, daemon_config.config_path, daemon
            )
            self._unavailable.pop(project_id, None)

    def unregister(self, project_id: str) -> bool:
        """Remove one registry record only after proving every exact unit inactive."""
        if self._registry is None:
            raise ProjectRegistryError(
                "static compatibility projects cannot be unregistered"
            )
        with self.project_lock(project_id), self._lock:
            if self._registry.get(project_id) is None:
                return False
            was_cached = project_id in self._projects
            try:
                item = self._project(project_id)
            except ControlPlaneUnavailableError as exc:
                raise ProjectRegistryError(
                    "project inactivity could not be proven"
                ) from exc
            try:
                active = self._has_active_unit(item)
            except Exception as exc:
                if not was_cached:
                    self._projects.pop(project_id, None)
                raise ProjectRegistryError(
                    "project inactivity could not be proven"
                ) from exc
            if active:
                raise ProjectRegistryError("project owns an active workflow unit")
            removed = self._registry.unregister(project_id)
            if removed is None:
                return False
            self._projects.pop(project_id, None)
            self._unavailable.pop(project_id, None)
            return True

    @staticmethod
    def _has_active_unit(item: _ProjectDaemon) -> bool:
        cursor: str | None = None
        while True:
            page = item.daemon.application.repository.list_runs(
                limit=1_000,
                cursor=cursor,
            )
            for record in page.runs:
                status = item.daemon.service.run_status(record.run_id)
                if status.unit_name:
                    observed = item.daemon.application.units.get(status.unit_name)
                    if observed is not None and observed.name != status.unit_name:
                        raise ProjectRegistryError(
                            "workflow unit identity is ambiguous"
                        )
                    if observed is not None and observed.is_active:
                        return True
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
        return False

    def _records(self) -> tuple[ProjectRegistryRecord, ...]:
        if self._registry is not None:
            try:
                return self._registry.list_records()
            except ProjectRegistryError as exc:
                raise ControlPlaneUnavailableError(
                    "project registry is unavailable"
                ) from exc
        return ()

    def _record(self, project_id: str) -> ProjectRegistryRecord | None:
        if self._registry is not None:
            return self._registry.get(project_id)
        return None

    def _resolve(
        self, project_id: str
    ) -> tuple[ProjectRegistryRecord, Path, DaemonConfig]:
        if self._registry is not None:
            record, root = self._registry.resolve(project_id)
            config_path = root / ".aflow" / "config" / "aflow.toml"
            return (
                record,
                root,
                DaemonConfig(
                    repo_root=root,
                    config_path=config_path,
                    aflow_executable=self._aflow_executable,
                    environment_file=self._environment_file,
                    release_identity=self._release_identity or "",
                    environment=self._environment,
                ).validated(),
            )
        raise ProjectRegistryError("project is not registered")

    @staticmethod
    def _caller_scope(project_id: str, transport: str) -> str:
        if transport not in {"rest", "mcp"}:
            raise ValueError("unsupported control-plane transport")
        return f"bearer:{project_id}"

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
