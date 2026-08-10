"""Durable lifecycle ownership for independent AFlow workflow units.

``aflowd`` owns launch intent and unit observation, never the lifetime of a
terminal connection.  The workflow controller remains the authority for its
``run.json`` and receives the normal installed ``aflow`` executable.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import sys
import tempfile
from threading import Event, RLock
import time
from typing import Callable, Iterator, Mapping

from aflow.api.models import PreparedRun, StartupQuestion, StartupQuestionKind, StartupRequest
from aflow.api.runner import execute_workflow
from aflow.api.startup import StartupError, prepare_startup, prepare_startup_with_answer
from aflow.config import WorkflowUserConfig, load_workflow_config
from aflow.control_plane import (
    ControlPlaneApplication,
    LaunchManifest,
    ReconciliationResult,
    RunControlRequest,
    RunEvent,
    RunIdentityConflict,
    RunStatus,
    StartRunResult,
    StartupQuestionRecord,
    append_run_event,
    compose_control_plane,
    create_launch_manifest,
    reserve_run_id,
    validate_run_id,
    write_launch_phase,
)
from aflow.control_plane.persistence import (
    PersistenceError,
    _contained_directory,
    normalized_request_digest,
)
from aflow.control_plane.units import UnitManager


_START_RECORD_SCHEMA_VERSION = 1
_START_RECORD_SUFFIX = ".json"
_REPLAYABLE_PHASES = frozenset({None, "manifest_only"})
_SAFE_ENVIRONMENT_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
_QUESTION_HISTORY_LIMIT = 64


class DaemonError(RuntimeError):
    """The daemon cannot safely carry out a lifecycle operation."""


class DaemonNotReadyError(DaemonError):
    """A request arrived before configuration and startup reconciliation completed."""


class DaemonIdempotencyConflict(DaemonError):
    """One caller reused an idempotency key for a different start intent."""


class DaemonAuthorizationError(PermissionError):
    """An injected transport authorization hook denied journal access."""


@dataclass(frozen=True)
class DaemonConfig:
    """Immutable local configuration for one project-scoped daemon instance."""

    repo_root: Path
    config_path: Path
    aflow_executable: Path
    environment_file: Path
    release_identity: str
    environment: Mapping[str, str] = field(default_factory=dict)
    stop_timeout_seconds: float = 15.0
    poll_interval_seconds: float = 1.0

    def validated(self) -> "DaemonConfig":
        root = Path(self.repo_root).resolve()
        config_path = Path(self.config_path).resolve()
        executable = Path(self.aflow_executable).resolve()
        environment_file = Path(self.environment_file).resolve()
        if not root.is_dir():
            raise DaemonError(f"repository root does not exist: {root}")
        if Path(self.config_path).is_symlink() or not config_path.is_file():
            raise DaemonError("daemon configuration must be a regular non-symlink file")
        if Path(self.aflow_executable).is_symlink() or not executable.is_file() or not os.access(executable, os.X_OK):
            raise DaemonError("aflow executable must be an installed executable from the selected release")
        if Path(self.environment_file).is_symlink() or not environment_file.is_file():
            raise DaemonError("daemon environment file must be a regular non-symlink file")
        if not isinstance(self.release_identity, str) or not self.release_identity.strip():
            raise DaemonError("daemon release identity must be a non-empty string")
        if self.stop_timeout_seconds < 0 or self.poll_interval_seconds <= 0:
            raise DaemonError("daemon timeout and polling values must be positive")
        environment = dict(self.environment or {})
        for key, value in environment.items():
            if (
                not isinstance(key, str)
                or _SAFE_ENVIRONMENT_NAME_RE.fullmatch(key) is None
                or not isinstance(value, str)
                or any(marker in value for marker in ("\x00", "\n", "\r"))
            ):
                raise DaemonError("daemon environment must be an explicit safe string allowlist")
        return replace(
            self,
            repo_root=root,
            config_path=config_path,
            aflow_executable=executable,
            environment_file=environment_file,
            environment=environment,
        )


class AflowDaemon:
    """Lifespan owner that reconciles only; workflow units remain independent."""

    def __init__(self, config: DaemonConfig, *, units: UnitManager | None = None) -> None:
        self._config = config.validated()
        self._units = units
        self._application: ControlPlaneApplication | None = None
        self._service: DaemonService | None = None
        self._ready = False
        self._stop = Event()

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def service(self) -> "DaemonService":
        if self._service is None:
            raise DaemonNotReadyError("daemon lifecycle has not started")
        return self._service

    @property
    def application(self) -> ControlPlaneApplication:
        if self._application is None:
            raise DaemonNotReadyError("daemon lifecycle has not started")
        return self._application

    def start(self) -> tuple[ReconciliationResult, ...]:
        """Load configuration and reconcile; this method never starts a workflow."""
        workflow_config = load_workflow_config(self._config.config_path)
        application = compose_control_plane(
            self._config.repo_root,
            config_path=self._config.config_path,
            units=self._units,
        )
        application.capabilities.get()
        reconciled = application.reconciliation.reconcile_startup()
        self._application = application
        self._service = DaemonService(application, self._config, workflow_config)
        self._ready = True
        return reconciled

    def reconcile_periodic(self) -> tuple[ReconciliationResult, ...]:
        if not self._ready:
            raise DaemonNotReadyError("daemon is not ready")
        return self.application.reconciliation.reconcile_periodic()

    def serve_forever(self) -> None:
        if not self._ready:
            self.start()
        while not self._stop.wait(self._config.poll_interval_seconds):
            self.reconcile_periodic()

    def request_shutdown(self) -> None:
        self._stop.set()


class DaemonService:
    """Transport-neutral start, stop, resume, and journal polling operations."""

    def __init__(
        self,
        application: ControlPlaneApplication,
        config: DaemonConfig,
        workflow_config: WorkflowUserConfig,
    ) -> None:
        self._application = application
        self._config = config
        self._workflow_config = workflow_config
        self._lock = RLock()

    def start(
        self,
        request: StartupRequest,
        *,
        caller_scope: str = "local",
        idempotency_key: str | None = None,
    ) -> StartRunResult | StartupQuestionRecord:
        """Reserve durable intent before evaluating the interactive startup gate."""
        with self._lock, self._idempotency_lock("start", caller_scope, idempotency_key):
            normalized = self._normalize_request(
                request,
                caller_scope=caller_scope,
                idempotency_key=idempotency_key,
            )
            request_digest = _startup_request_digest(normalized)
            pending = self._find_pending_request(
                operation="start",
                caller_scope=caller_scope,
                idempotency_key=idempotency_key,
            )
            if pending is not None:
                if pending["request_digest"] != request_digest:
                    raise DaemonIdempotencyConflict("start idempotency key was reused for a different request")
                return self._pending_response(pending)

            candidate = self._initial_manifest_for(
                run_id="candidate-run",
                request=normalized,
                caller_scope=caller_scope,
                idempotency_key=idempotency_key or "candidate",
            )
            existing = self._find_existing_manifest(
                caller_scope=caller_scope,
                idempotency_key=idempotency_key,
                request_digest=normalized_request_digest(candidate),
            )
            if existing is not None:
                return self._recover_start_manifest(existing, normalized, request_digest)

            run_id = reserve_run_id(self._config.repo_root)
            effective_key = idempotency_key or f"daemon-{run_id}"
            manifest = self._initial_manifest_for(
                run_id=run_id,
                request=normalized,
                caller_scope=caller_scope,
                idempotency_key=effective_key,
            )
            try:
                manifest_result = create_launch_manifest(self._config.repo_root, manifest)
            except (ValueError, RunIdentityConflict) as exc:
                raise DaemonError(f"cannot reserve launch intent: {exc}") from exc
            if not manifest_result.created:
                return self._recover_start_manifest(manifest, normalized, request_digest)
            record = self._new_start_record(
                run_id=run_id,
                request=replace(normalized, reserved_run_id=run_id),
                request_digest=request_digest,
                caller_scope=caller_scope,
                idempotency_key=idempotency_key,
                state="preparing",
                operation="start",
            )
            record["manifest_request_digest"] = normalized_request_digest(manifest)
            self._create_record(record)
            with self._startup_record_lock(run_id):
                return self._advance_start_preparation_locked(
                    self._read_record(run_id),
                    created=True,
                )

    def answer_startup(
        self,
        question_id: str,
        answer: str | int | bool,
        *,
        caller_scope: str = "local",
        idempotency_key: str | None = None,
    ) -> StartRunResult | StartupQuestionRecord:
        """Replay a persisted startup question without a TTY-backed unit."""
        run_id, question_generation = _question_identity(question_id)
        with self._lock, self._startup_record_lock(run_id):
            record = self._read_record(run_id)
            self._assert_record_caller(record, caller_scope)
            answer_digest = _answer_digest(answer)
            prior_answer = _answered_question(record, question_generation)
            if prior_answer is not None:
                if (
                    prior_answer["answer_digest"] != answer_digest
                    or prior_answer["idempotency_key"] != idempotency_key
                ):
                    raise DaemonIdempotencyConflict(
                        "startup answer idempotency key was reused for a different answer"
                    )
                return self._pending_response_locked(record)
            if question_generation != _question_generation(record):
                raise DaemonError("startup question identity is stale")
            if record.get("state") != "awaiting_startup_answer":
                raise DaemonError("startup question is no longer awaiting an answer")
            question = _question_from_record(record)
            request = self._request_from_record(record)
            try:
                prepared_or_question = prepare_startup_with_answer(question, request, answer)
            except StartupError as exc:
                raise DaemonError(str(exc)) from exc
            if isinstance(prepared_or_question, StartupQuestion):
                updated = dict(record)
                _record_answer(updated, question_generation, answer_digest, idempotency_key)
                updated["question"] = _question_payload(prepared_or_question)
                updated["request"] = _request_payload(
                    prepared_or_question.continuation_request or request
                )
                updated["question_generation"] = question_generation + 1
                self._write_record(updated)
                return _question_record(run_id, prepared_or_question, question_generation + 1)
            prepared = replace(
                prepared_or_question,
                reserved_run_id=run_id,
                idempotency_key=str(record["effective_idempotency_key"]),
                caller_scope=str(record["caller_scope"]),
            )
            updated = dict(record)
            updated["state"] = "prepared"
            updated["prepared"] = _prepared_payload(prepared)
            _record_answer(updated, question_generation, answer_digest, idempotency_key)
            updated.pop("question", None)
            self._write_record(updated)
            return self._launch_prepared_locked(updated, prepared, created=False)

    def owner_stop(
        self,
        run_id: str,
        *,
        expected_revision: int,
        caller_scope: str = "local",
        idempotency_key: str | None = None,
    ) -> RunStatus:
        """Write owner-stop intent, stop the exact unit, and persist terminal evidence."""
        with self._lock:
            status = self._application.repository.get_run_status(run_id)
            if status.ownership != "control_plane":
                raise DaemonError("legacy runs are read-only and cannot be stopped by the daemon")
            self._assert_manifest_caller(run_id, caller_scope)
            self._application.controls.apply(
                run_id,
                RunControlRequest(expected_revision=expected_revision, owner_stop=True),
                caller_scope=caller_scope,
                idempotency_key=idempotency_key,
            )
            unit_name = _unit_name(run_id)
            observed = self._application.units.get(unit_name)
            if observed is not None and observed.is_active:
                self._application.units.stop(unit_name)
            deadline = time.monotonic() + self._config.stop_timeout_seconds
            while True:
                observed = self._application.units.get(unit_name)
                if observed is None or not observed.is_active:
                    break
                if time.monotonic() >= deadline:
                    raise DaemonError("workflow unit did not stop before the bounded timeout")
                time.sleep(min(0.1, max(0.01, self._config.poll_interval_seconds)))
            write_launch_phase(self._config.repo_root, run_id, "owner_stopped")
            append_run_event(
                self._application.repository.run_directory(run_id),
                "owner_stopped",
                {"source": "daemon", "unit_name": unit_name},
            )
            return self._application.repository.get_run_status(run_id)

    def resume(
        self,
        source_run_id: str,
        *,
        caller_scope: str = "local",
        idempotency_key: str | None = None,
    ) -> StartRunResult:
        """Launch one validated continuation; the source unit is never restarted."""
        with self._lock, self._idempotency_lock("resume", caller_scope, idempotency_key):
            pending = self._find_pending_request(
                operation="resume",
                caller_scope=caller_scope,
                idempotency_key=idempotency_key,
            )
            if pending is not None:
                if pending.get("resumed_from_run_id") != validate_run_id(source_run_id):
                    raise DaemonIdempotencyConflict("resume idempotency key was reused for a different source run")
                return self._recover_resume_record(pending)
            source = self._application.repository.get_run_status(source_run_id)
            if source.ownership != "control_plane":
                raise DaemonError("legacy runs cannot be resumed by the control plane")
            unit_name = _unit_name(source_run_id)
            observed = self._application.units.get(unit_name)
            if observed is not None and observed.name != unit_name:
                raise DaemonError("source workflow unit identity is ambiguous")
            if observed is not None and observed.is_active:
                raise DaemonError("an active workflow unit cannot be resumed")
            if source.launch_phase in {"manifest_only", "launch_requested"}:
                raise DaemonError("source run has an incomplete or ambiguous launch attempt")
            if source.launch_phase == "launch_started" and source.status != "needs_attention":
                raise DaemonError("a killed launched run must be reconciled before explicit resume")
            if source.status not in {"running", "failed", "interrupted", "needs_attention", "waiting_for_valid_override"}:
                raise DaemonError("source run is incomplete, terminal, or lacks safe resume evidence")
            bootstrap = self._resume_bootstrap(source_run_id)
            source_manifest = self._application.repository.get_launch_manifest(source_run_id)
            if source_manifest is None:
                raise DaemonError("source run has no control-plane launch manifest")
            self._assert_manifest_caller(source_run_id, caller_scope)

            run_id = reserve_run_id(self._config.repo_root)
            prepared = PreparedRun(
                workflow_name=bootstrap.workflow_name,
                repo_root=self._config.repo_root,
                plan_path=bootstrap.plan_path,
                config_path=self._config.config_path,
                max_turns=bootstrap.max_turns,
                team=bootstrap.team,
                extra_instructions=bootstrap.extra_instructions,
                start_step=(
                    bootstrap.start_step
                    or self._workflow_config.workflows[bootstrap.workflow_name].first_step
                    or bootstrap.workflow_name
                ),
                reserved_run_id=run_id,
                idempotency_key=idempotency_key or f"daemon-{run_id}",
                caller_scope=caller_scope,
            )
            manifest = self._manifest_for(
                run_id=run_id,
                prepared=prepared,
                caller_scope=caller_scope,
                idempotency_key=prepared.idempotency_key or f"daemon-{run_id}",
            )
            request_digest = normalized_request_digest(manifest)

            record = self._new_start_record(
                run_id=run_id,
                request=None,
                request_digest=request_digest,
                caller_scope=caller_scope,
                idempotency_key=idempotency_key,
                state="prepared",
                prepared=None,
                operation="resume",
                mode="resume",
                resumed_from_run_id=source_run_id,
                source_invocation_digest=source_manifest.request_digest,
            )
            record["manifest_request_digest"] = normalized_request_digest(manifest)
            self._create_record(record)
            return self._recover_resume_record(record, prepared=prepared, created=True)

    def run_status(self, run_id: str) -> RunStatus:
        """Project a persisted startup question into canonical run status."""
        status = self._application.repository.get_run_status(run_id)
        if status.ownership != "control_plane":
            return status
        try:
            record = self._read_record(run_id)
        except DaemonError:
            return status
        if record.get("state") != "awaiting_startup_answer":
            return status
        return replace(
            status,
            status="awaiting_startup_answer",
            reason="startup answer required before workflow unit creation",
        )

    def poll_events(
        self,
        run_id: str,
        *,
        after_sequence: int | None = None,
        limit: int = 100,
        authorizer: Callable[[str, RunStatus], bool | None] | None = None,
    ) -> tuple[RunEvent, ...]:
        """Return a cursorable journal slice after transport authentication."""
        status = self._application.repository.get_run_status(run_id)
        if authorizer is None or authorizer("events", status) is False:
            raise DaemonAuthorizationError("event stream access is not authorized")
        return tuple(
            self._application.repository.tail_events(
                run_id,
                after_sequence=after_sequence,
                limit=limit,
            )
        )  # type: ignore[return-value]

    def _normalize_request(
        self,
        request: StartupRequest,
        *,
        caller_scope: str,
        idempotency_key: str | None,
    ) -> StartupRequest:
        if not caller_scope.strip():
            raise DaemonError("caller scope must be non-empty")
        if idempotency_key is not None and not idempotency_key.strip():
            raise DaemonError("idempotency key must be non-empty when supplied")
        if request.extra_instructions:
            raise DaemonError(
                "daemon starts do not accept prompt-like extra instructions because they cannot be safely reconstructed"
            )
        if Path(request.repo_root).resolve() != self._config.repo_root:
            raise DaemonError("startup request repository does not match this daemon")
        if Path(request.config_path).resolve() != self._config.config_path:
            raise DaemonError("startup request configuration does not match this daemon")
        plan_path = Path(request.plan_path).resolve()
        try:
            plan_path.relative_to(self._config.repo_root)
        except ValueError as exc:
            raise DaemonError("startup request plan is outside this daemon project") from exc
        return replace(
            request,
            repo_root=self._config.repo_root,
            plan_path=plan_path,
            config_path=self._config.config_path,
            workflow_config=self._workflow_config,
            caller_scope=caller_scope,
            idempotency_key=idempotency_key,
        )

    def _new_start_record(
        self,
        *,
        run_id: str,
        request: StartupRequest | None,
        request_digest: str,
        caller_scope: str,
        idempotency_key: str | None,
        state: str,
        prepared: PreparedRun | None = None,
        question: StartupQuestion | None = None,
        operation: str = "start",
        mode: str = "start",
        resumed_from_run_id: str | None = None,
        source_invocation_digest: str | None = None,
    ) -> dict[str, object]:
        record: dict[str, object] = {
            "schema_version": _START_RECORD_SCHEMA_VERSION,
            "run_id": validate_run_id(run_id),
            "state": state,
            "operation": operation,
            "mode": mode,
            "caller_scope": caller_scope,
            "idempotency_key": idempotency_key,
            "effective_idempotency_key": (
                idempotency_key if idempotency_key is not None else f"daemon-{run_id}"
            ),
            "request_digest": request_digest,
            "config_path": str(self._config.config_path),
            "selected_executable": str(self._config.aflow_executable),
            "selected_release_identity": self._config.release_identity,
            "selected_environment_file": _file_identity(self._config.environment_file),
            "created_at": time.time_ns(),
        }
        if request is not None:
            record["request"] = _request_payload(request)
        if prepared is not None:
            record["prepared"] = _prepared_payload(prepared)
        if question is not None:
            record["question"] = _question_payload(question)
        if resumed_from_run_id is not None:
            record["resumed_from_run_id"] = validate_run_id(resumed_from_run_id)
        if source_invocation_digest is not None:
            record["source_invocation_digest"] = source_invocation_digest
        return record

    def _advance_start_preparation_locked(
        self,
        record: Mapping[str, object],
        *,
        created: bool,
    ) -> StartRunResult | StartupQuestionRecord:
        """Advance one already-reserved request without changing its identity."""
        if record.get("state") != "preparing":
            return self._pending_response_locked(record)
        request = self._request_from_record(record)
        try:
            prepared_or_question = prepare_startup(request)
        except StartupError as exc:
            updated = dict(record)
            updated["state"] = "needs_attention"
            self._write_record(updated)
            raise DaemonError(str(exc)) from exc
        if isinstance(prepared_or_question, StartupQuestion):
            updated = dict(record)
            updated["state"] = "awaiting_startup_answer"
            updated["question"] = _question_payload(prepared_or_question)
            updated["request"] = _request_payload(
                prepared_or_question.continuation_request or request
            )
            updated["question_generation"] = _next_question_generation(record)
            self._write_record(updated)
            return _question_record(
                validate_run_id(str(record["run_id"])),
                prepared_or_question,
                _question_generation(updated),
            )
        prepared = replace(
            prepared_or_question,
            reserved_run_id=validate_run_id(str(record["run_id"])),
            idempotency_key=str(record["effective_idempotency_key"]),
            caller_scope=str(record["caller_scope"]),
        )
        updated = dict(record)
        updated["state"] = "prepared"
        updated["prepared"] = _prepared_payload(prepared)
        self._write_record(updated)
        return self._launch_prepared_locked(updated, prepared, created=created)

    def _launch_prepared_locked(
        self,
        record: Mapping[str, object],
        prepared: PreparedRun,
        *,
        created: bool,
    ) -> StartRunResult:
        run_id = validate_run_id(str(record["run_id"]))
        record = self._read_record(run_id)
        if record.get("state") in {"unit_started", "needs_attention"}:
            return self._existing_start_result(run_id)
        if record.get("state") not in {"prepared", "launch_requested"}:
            raise DaemonError("persisted startup request cannot advance to a launch")
        self._assert_record_runtime_identity(record)
        persisted_manifest = self._application.repository.get_launch_manifest(run_id)
        if persisted_manifest is None:
            raise DaemonError("launch manifest disappeared before unit creation")
        self._assert_manifest_accepts_prepared(persisted_manifest, record, prepared)
        status = self._application.repository.get_run_status(run_id)
        observed = self._application.units.get(_unit_name(run_id))
        if observed is not None and observed.name != _unit_name(run_id):
            return StartRunResult(run_id=run_id, created=False, status="needs_attention", reason="unit identity is ambiguous")
        if observed is not None and observed.is_active:
            return StartRunResult(run_id=run_id, created=False, status="running")
        if status.launch_phase not in _REPLAYABLE_PHASES:
            return self._existing_start_result(run_id)

        argv = self._worker_argv(run_id)
        environment_identity = _file_identity(self._config.environment_file)
        mutable = dict(record)
        mutable["state"] = "launch_requested"
        mutable["argv"] = list(argv)
        mutable["executable"] = str(self._config.aflow_executable)
        mutable["cwd"] = str(self._config.repo_root)
        mutable["environment_file"] = environment_identity
        mutable["release_identity"] = self._config.release_identity
        self._write_record(mutable)
        run_dir = self._application.repository.run_directory(run_id)
        write_launch_phase(self._config.repo_root, run_id, "launch_requested")
        append_run_event(
            run_dir,
            "daemon_start_attempt",
            {
                "unit_name": _unit_name(run_id),
                "argv": list(argv),
                "cwd": str(self._config.repo_root),
                "executable": str(self._config.aflow_executable),
                "release_identity": self._config.release_identity,
                "environment_file": environment_identity,
                "manifest_digest": persisted_manifest.request_digest,
            },
        )
        try:
            started_unit = self._application.units.start(
                _unit_name(run_id),
                argv,
                cwd=self._config.repo_root,
                environment_file=self._config.environment_file,
                environment=self._config.environment,
            )
            if started_unit.name != _unit_name(run_id) or not started_unit.is_active:
                raise DaemonError("systemd did not prove the exact workflow unit is active")
        except Exception as exc:
            mutable["state"] = "needs_attention"
            self._write_record(mutable)
            raise DaemonError(f"workflow unit failed to start: {exc}") from exc
        write_launch_phase(self._config.repo_root, run_id, "unit_started")
        append_run_event(run_dir, "unit_started", {"unit_name": _unit_name(run_id)})
        mutable["state"] = "unit_started"
        self._write_record(mutable)
        return StartRunResult(
            run_id=run_id,
            created=created,
            status="running",
            manifest_path=str(self._config.repo_root / ".aflow" / "launches" / f"{run_id}.json"),
        )

    def _replay_manifest(self, manifest: LaunchManifest) -> StartRunResult:
        run_id = manifest.run_id
        unit_name = _unit_name(run_id)
        observed = self._application.units.get(unit_name)
        if observed is not None and observed.name != unit_name:
            return StartRunResult(run_id=run_id, created=False, status="needs_attention", reason="unit identity is ambiguous")
        if observed is not None and observed.is_active:
            return StartRunResult(run_id=run_id, created=False, status="running")
        status = self._application.repository.get_run_status(run_id)
        if status.launch_phase not in _REPLAYABLE_PHASES:
            return StartRunResult(
                run_id=run_id,
                created=False,
                status="needs_attention",
                reason="existing launch has child-start or terminal evidence",
            )
        try:
            record = self._read_record(run_id)
        except DaemonError:
            return StartRunResult(
                run_id=run_id,
                created=False,
                status="needs_attention",
                reason="launch manifest has no durable pre-child request record",
            )
        response = self._pending_response(record)
        if isinstance(response, StartupQuestionRecord):
            return StartRunResult(
                run_id=run_id,
                created=False,
                status="needs_attention",
                reason="startup answer is required before a unit can be replayed",
            )
        return response

    def _recover_resume_record(
        self,
        record: Mapping[str, object],
        *,
        prepared: PreparedRun | None = None,
        created: bool = False,
    ) -> StartRunResult:
        run_id = validate_run_id(str(record["run_id"]))
        with self._startup_record_lock(run_id):
            return self._recover_resume_record_locked(
                self._read_record(run_id),
                prepared=prepared,
                created=created,
            )

    def _recover_resume_record_locked(
        self,
        record: Mapping[str, object],
        *,
        prepared: PreparedRun | None = None,
        created: bool = False,
    ) -> StartRunResult:
        """Finish only the manifest-only gap belonging to one resume record."""
        if record.get("operation") != "resume" or record.get("mode") != "resume":
            raise DaemonError("startup record is not a resumable continuation")
        run_id = validate_run_id(str(record["run_id"]))
        if prepared is None:
            source_run_id = validate_run_id(str(record["resumed_from_run_id"]))
            bootstrap = self._resume_bootstrap(source_run_id)
            prepared = PreparedRun(
                workflow_name=bootstrap.workflow_name,
                repo_root=self._config.repo_root,
                plan_path=bootstrap.plan_path,
                config_path=self._config.config_path,
                max_turns=bootstrap.max_turns,
                team=bootstrap.team,
                extra_instructions=bootstrap.extra_instructions,
                start_step=(
                    bootstrap.start_step
                    or self._workflow_config.workflows[bootstrap.workflow_name].first_step
                    or bootstrap.workflow_name
                ),
                reserved_run_id=run_id,
                idempotency_key=str(record["effective_idempotency_key"]),
                caller_scope=str(record["caller_scope"]),
            )
        manifest = self._manifest_for(
            run_id=run_id,
            prepared=prepared,
            caller_scope=str(record["caller_scope"]),
            idempotency_key=str(record["effective_idempotency_key"]),
        )
        if record.get("manifest_request_digest") != normalized_request_digest(manifest):
            raise DaemonError("resume record does not match its immutable continuation intent")
        persisted_manifest = self._application.repository.get_launch_manifest(run_id)
        if persisted_manifest is None:
            try:
                result = create_launch_manifest(self._config.repo_root, manifest)
            except (ValueError, RunIdentityConflict) as exc:
                raise DaemonError(f"cannot reserve continuation launch intent: {exc}") from exc
            if not result.created:
                persisted_manifest = self._application.repository.get_launch_manifest(run_id)
                if persisted_manifest is None:
                    raise DaemonError("continuation manifest disappeared during replay")
        else:
            self._assert_manifest_accepts_prepared(persisted_manifest, record, prepared)
        mutable = dict(record)
        if not mutable.get("source_audited"):
            source_run_id = validate_run_id(str(mutable["resumed_from_run_id"]))
            append_run_event(
                self._application.repository.run_directory(source_run_id),
                "resume_requested",
                {
                    "continuation_run_id": run_id,
                    "source_invocation_digest": mutable.get("source_invocation_digest"),
                },
            )
            mutable["source_audited"] = True
            self._write_record(mutable)
        return self._launch_prepared_locked(mutable, prepared, created=created)

    def _initial_manifest_for(
        self,
        *,
        run_id: str,
        request: StartupRequest,
        caller_scope: str,
        idempotency_key: str,
    ) -> LaunchManifest:
        """Freeze only typed configuration defaults before plan-sensitive preparation.

        ``start_step`` deliberately remains ``None`` when the caller has not
        selected one.  A later persisted startup answer may choose a step, but
        it may never mutate this request-level launch intent.
        """
        workflow_name = request.workflow_name or self._workflow_config.aflow.default_workflow
        if workflow_name is None or workflow_name not in self._workflow_config.workflows:
            raise DaemonError("startup request does not name a configured workflow")
        workflow = self._workflow_config.workflows[workflow_name]
        start_step = _resolve_configured_start_step(request.start_step, workflow_name, workflow.steps)
        max_turns = request.max_turns if request.max_turns is not None else self._workflow_config.aflow.max_turns
        if not isinstance(max_turns, int) or isinstance(max_turns, bool) or max_turns < 1:
            raise DaemonError("startup request max_turns must be a positive integer")
        team = request.team if request.team is not None else workflow.team
        if team is not None and team not in self._workflow_config.teams:
            raise DaemonError("startup request names an unknown team")
        from aflow.workflow import _freeze_run_identity

        frozen = _freeze_run_identity(
            workflow_name,
            self._workflow_config,
            config_dir=self._config.config_path,
        )
        return LaunchManifest(
            run_id=run_id,
            project_root=str(self._config.repo_root),
            plan_path=str(Path(request.plan_path).resolve()),
            workflow_name=workflow_name,
            max_turns=max_turns,
            team=team,
            start_step=start_step,
            extra_instructions=(),
            idempotency_key=idempotency_key,
            caller_scope=caller_scope,
            frozen_config_fingerprint=frozen.config_fingerprint,
        )

    def _recover_start_manifest(
        self,
        manifest: LaunchManifest,
        request: StartupRequest,
        request_digest: str,
    ) -> StartRunResult | StartupQuestionRecord:
        """Recover only a manifest-only gap for the exact original request."""
        status = self._application.repository.get_run_status(manifest.run_id)
        if status.launch_phase not in _REPLAYABLE_PHASES:
            return self._replay_manifest(manifest)
        try:
            record = self._read_record(manifest.run_id)
        except DaemonError:
            record = self._new_start_record(
                run_id=manifest.run_id,
                request=replace(request, reserved_run_id=manifest.run_id),
                request_digest=request_digest,
                caller_scope=str(manifest.caller_scope),
                idempotency_key=manifest.idempotency_key,
                state="preparing",
                operation="start",
            )
            record["manifest_request_digest"] = manifest.request_digest or ""
            try:
                self._create_record(record)
            except DaemonError:
                record = self._read_record(manifest.run_id)
        self._assert_record_matches_manifest(record, manifest, request_digest)
        return self._pending_response(record)

    def _assert_record_matches_manifest(
        self,
        record: Mapping[str, object],
        manifest: LaunchManifest,
        request_digest: str,
    ) -> None:
        if (
            record.get("operation") != "start"
            or record.get("request_digest") != request_digest
            or record.get("caller_scope") != manifest.caller_scope
            or record.get("effective_idempotency_key") != manifest.idempotency_key
            or record.get("manifest_request_digest") != manifest.request_digest
        ):
            raise DaemonIdempotencyConflict("durable start request does not match its launch manifest")

    def _assert_manifest_accepts_prepared(
        self,
        manifest: LaunchManifest,
        record: Mapping[str, object],
        prepared: PreparedRun,
    ) -> None:
        if (
            manifest.run_id != record.get("run_id")
            or manifest.project_root != str(self._config.repo_root)
            or manifest.plan_path != str(Path(prepared.plan_path).resolve())
            or manifest.workflow_name != prepared.workflow_name
            or manifest.max_turns != prepared.max_turns
            or manifest.team != prepared.team
            or manifest.idempotency_key != record.get("effective_idempotency_key")
            or manifest.caller_scope != record.get("caller_scope")
            or manifest.intended_unit != _unit_name(manifest.run_id)
        ):
            raise DaemonError("prepared startup state does not match immutable launch intent")
        if manifest.start_step is not None and manifest.start_step != prepared.start_step:
            raise DaemonError("prepared startup step does not match immutable launch intent")
        from aflow.workflow import _freeze_run_identity

        frozen = _freeze_run_identity(
            prepared.workflow_name,
            self._workflow_config,
            config_dir=self._config.config_path,
        )
        if manifest.frozen_config_fingerprint != frozen.config_fingerprint:
            raise DaemonError("prepared startup state does not match frozen configuration")

    def _assert_record_runtime_identity(self, record: Mapping[str, object]) -> None:
        if (
            record.get("selected_executable") != str(self._config.aflow_executable)
            or record.get("selected_release_identity") != self._config.release_identity
            or record.get("selected_environment_file") != _file_identity(self._config.environment_file)
        ):
            raise DaemonError(
                "persisted launch record runtime identity differs from the active daemon; explicit recovery is required"
            )

    def _manifest_for(
        self,
        *,
        run_id: str,
        prepared: PreparedRun,
        caller_scope: str,
        idempotency_key: str,
    ) -> LaunchManifest:
        from aflow.workflow import _freeze_run_identity

        frozen = _freeze_run_identity(
            prepared.workflow_name,
            self._workflow_config,
            config_dir=self._config.config_path,
        )
        return LaunchManifest(
            run_id=run_id,
            project_root=str(self._config.repo_root),
            plan_path=str(prepared.plan_path.resolve()),
            workflow_name=prepared.workflow_name,
            max_turns=prepared.max_turns,
            team=prepared.team,
            start_step=prepared.start_step,
            extra_instructions=prepared.extra_instructions,
            idempotency_key=idempotency_key,
            caller_scope=caller_scope,
            frozen_config_fingerprint=frozen.config_fingerprint,
        )

    def _find_pending_request(
        self,
        *,
        operation: str,
        caller_scope: str,
        idempotency_key: str | None,
    ) -> dict[str, object] | None:
        if idempotency_key is None:
            return None
        for record in self._iter_records():
            if (
                record.get("operation") == operation
                and _equivalent_caller_scope(record.get("caller_scope"), caller_scope)
                and record.get("idempotency_key") == idempotency_key
            ):
                return record
        return None

    def _find_existing_manifest(
        self,
        *,
        caller_scope: str,
        idempotency_key: str | None,
        request_digest: str,
    ) -> LaunchManifest | None:
        if idempotency_key is None:
            return None
        cursor: str | None = None
        while True:
            page = self._application.repository.list_runs(limit=1_000, cursor=cursor)
            for status in page.runs:
                if status.ownership != "control_plane":
                    continue
                manifest = self._application.repository.get_launch_manifest(status.run_id)
                if manifest is None or manifest.idempotency_key != idempotency_key or not _equivalent_caller_scope(manifest.caller_scope, caller_scope):
                    continue
                if manifest.request_digest != request_digest:
                    raise DaemonIdempotencyConflict("start idempotency key was reused for a different request")
                return manifest
            if page.next_cursor is None:
                return None
            cursor = page.next_cursor

    def _pending_response(self, record: Mapping[str, object]) -> StartRunResult | StartupQuestionRecord:
        run_id = validate_run_id(str(record["run_id"]))
        with self._startup_record_lock(run_id):
            return self._pending_response_locked(self._read_record(run_id))

    def _pending_response_locked(
        self,
        record: Mapping[str, object],
    ) -> StartRunResult | StartupQuestionRecord:
        run_id = validate_run_id(str(record["run_id"]))
        state = record.get("state")
        if state == "preparing":
            return self._advance_start_preparation_locked(record, created=False)
        if state == "awaiting_startup_answer":
            return _question_record(
                run_id,
                _question_from_record(record),
                _question_generation(record),
            )
        if state in {"unit_started", "needs_attention"}:
            return self._existing_start_result(run_id)
        if state not in {"prepared", "launch_requested"}:
            raise DaemonError("persisted startup request has an unsupported state")

        manifest = self._application.repository.get_launch_manifest(run_id)
        if manifest is None:
            return StartRunResult(
                run_id=run_id,
                created=False,
                status="needs_attention",
                reason="durable startup record has no immutable launch manifest",
            )
        status = self._application.repository.get_run_status(run_id)
        if status.launch_phase not in _REPLAYABLE_PHASES:
            return self._existing_start_result(run_id)
        prepared_payload = record.get("prepared")
        if not isinstance(prepared_payload, Mapping):
            return StartRunResult(
                run_id=run_id,
                created=False,
                status="needs_attention",
                reason="durable startup record has no prepared launch request",
            )
        prepared = _prepared_from_payload(
            prepared_payload,
            repo_root=self._config.repo_root,
            config_path=self._config.config_path,
        )
        prepared = replace(
            prepared,
            reserved_run_id=run_id,
            idempotency_key=str(record["effective_idempotency_key"]),
            caller_scope=str(record["caller_scope"]),
        )
        return self._launch_prepared_locked(record, prepared, created=False)

    def _existing_start_result(self, run_id: str) -> StartRunResult:
        """Classify durable launch evidence without attempting another unit start."""
        unit_name = _unit_name(run_id)
        if self._application.repository.get_launch_manifest(run_id) is None:
            return StartRunResult(
                run_id=run_id,
                created=False,
                status="needs_attention",
                reason="durable startup record has no immutable launch manifest",
            )
        observed = self._application.units.get(unit_name)
        if observed is not None and observed.name != unit_name:
            return StartRunResult(
                run_id=run_id,
                created=False,
                status="needs_attention",
                reason="unit identity is ambiguous",
            )
        if observed is not None and observed.is_active:
            return StartRunResult(run_id=run_id, created=False, status="running")
        status = self._application.repository.get_run_status(run_id)
        if status.status in {"completed", "failed", "interrupted", "owner_stopped"}:
            return StartRunResult(
                run_id=run_id,
                created=False,
                status=status.status,
                reason=status.reason,
            )
        return StartRunResult(
            run_id=run_id,
            created=False,
            status="needs_attention",
            reason="existing launch has no active unit or terminal controller evidence",
        )

    def _worker_argv(self, run_id: str) -> tuple[str, ...]:
        return (
            str(self._config.aflow_executable),
            "daemon-worker",
            "--repo-root",
            str(self._config.repo_root),
            "--config",
            str(self._config.config_path),
            "--run-id",
            run_id,
        )

    def _resume_bootstrap(self, source_run_id: str):
        from aflow.cli import _bootstrap_resume_invocation

        return _bootstrap_resume_invocation(
            repo_root=self._config.repo_root,
            config_path=self._config.config_path,
            workflow_config=self._workflow_config,
            requested_run_id=source_run_id,
            workflow_arg=None,
            plan_file_arg=None,
            team_arg=None,
            start_step_arg=None,
            max_turns_arg=None,
            extra_instructions_arg=(),
            extra_instructions_provided=False,
        )

    def _assert_record_caller(self, record: Mapping[str, object], caller_scope: str) -> None:
        if not isinstance(caller_scope, str) or not caller_scope.strip():
            raise DaemonAuthorizationError("caller scope must be non-empty")
        if not _equivalent_caller_scope(record.get("caller_scope"), caller_scope):
            raise DaemonAuthorizationError("caller scope is not authorized for this startup request")

    def _assert_manifest_caller(self, run_id: str, caller_scope: str) -> None:
        if not isinstance(caller_scope, str) or not caller_scope.strip():
            raise DaemonAuthorizationError("caller scope must be non-empty")
        manifest = self._application.repository.get_launch_manifest(run_id)
        if manifest is None or not _equivalent_caller_scope(manifest.caller_scope, caller_scope):
            raise DaemonAuthorizationError("caller scope is not authorized for this run")

    @contextmanager
    def _idempotency_lock(
        self,
        operation: str,
        caller_scope: str,
        idempotency_key: str | None,
    ) -> Iterator[None]:
        """Serialize same-key requests even during a short daemon handover."""
        if idempotency_key is None:
            yield
            return
        material = f"{operation}\x00{caller_scope}\x00{idempotency_key}".encode("utf-8")
        with self._durable_lock(
            ".idempotency-locks",
            hashlib.sha256(material).hexdigest(),
        ):
            yield

    @contextmanager
    def _startup_record_lock(self, run_id: str) -> Iterator[None]:
        """Serialize one persisted startup record transition across daemon processes."""
        with self._durable_lock(".startup-answer-locks", validate_run_id(run_id)):
            yield

    @contextmanager
    def _durable_lock(self, directory: str, name: str) -> Iterator[None]:
        try:
            locks_root = _contained_directory(
                self._config.repo_root,
                ".aflow",
                "start-requests",
                directory,
            )
        except PersistenceError as exc:
            raise DaemonError("daemon lock directory is unsafe") from exc
        lock_path = locks_root / name
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(lock_path, flags, 0o600)
        except OSError as exc:
            raise DaemonError("daemon lock file is unsafe") from exc
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _records_root(self) -> Path:
        try:
            return _contained_directory(self._config.repo_root, ".aflow", "start-requests")
        except PersistenceError as exc:
            raise DaemonError("startup record directory is unsafe") from exc

    def _record_path(self, run_id: str) -> Path:
        return self._records_root() / f"{validate_run_id(run_id)}{_START_RECORD_SUFFIX}"

    def _create_record(self, record: Mapping[str, object]) -> None:
        path = self._record_path(str(record["run_id"]))
        if path.is_symlink():
            raise DaemonError("startup record may not be a symlink")
        encoded = _record_bytes(record)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise DaemonError("startup record identity is already reserved") from exc
        try:
            with os.fdopen(fd, "wb", closefd=False) as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            os.close(fd)
        _fsync_directory(path.parent)

    def _write_record(self, record: Mapping[str, object]) -> None:
        path = self._record_path(str(record["run_id"]))
        if path.is_symlink() or not path.exists():
            raise DaemonError("startup record is missing or unsafe")
        encoded = _record_bytes(record)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            _fsync_directory(path.parent)
        finally:
            if temporary.exists():
                temporary.unlink(missing_ok=True)

    def _read_record(self, run_id: str) -> dict[str, object]:
        path = self._record_path(run_id)
        if path.is_symlink() or not path.is_file():
            raise DaemonError("startup record does not exist")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DaemonError("startup record is unreadable") from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != _START_RECORD_SCHEMA_VERSION:
            raise DaemonError("startup record has an unsupported schema")
        if payload.get("run_id") != validate_run_id(run_id):
            raise DaemonError("startup record identity does not match its path")
        return payload

    def _iter_records(self) -> Iterator[dict[str, object]]:
        for path in sorted(self._records_root().glob(f"*{_START_RECORD_SUFFIX}")):
            if path.is_symlink() or not path.is_file():
                raise DaemonError("startup record directory contains an unsafe entry")
            yield self._read_record(path.stem)

    def _request_from_record(self, record: Mapping[str, object]) -> StartupRequest:
        payload = record.get("request")
        if not isinstance(payload, Mapping):
            raise DaemonError("startup record does not contain a replayable request")
        return _request_from_payload(
            payload,
            workflow_config=self._workflow_config,
            reserved_run_id=str(record["run_id"]),
            caller_scope=str(record["caller_scope"]),
            idempotency_key=(str(record["idempotency_key"]) if record.get("idempotency_key") is not None else None),
        )


def worker_main(*, repo_root: Path, config_path: Path, run_id: str) -> int:
    """Run a single daemon-prepared controller through the installed ``aflow`` entry point."""
    try:
        root = Path(repo_root).resolve()
        config = Path(config_path).resolve()
        selected_run_id = validate_run_id(run_id)
        workflow_config = load_workflow_config(config)
        daemon_config = DaemonConfig(
            repo_root=root,
            config_path=config,
            aflow_executable=Path(sys.argv[0]).resolve(),
            environment_file=config,
            release_identity="worker",
        )
        # The worker only needs safe record reads; its executable/environment
        # configuration was already validated by the parent before unit start.
        application = compose_control_plane(root, config_path=config)
        service = DaemonService.__new__(DaemonService)
        service._application = application
        service._config = daemon_config
        service._workflow_config = workflow_config
        service._lock = RLock()
        record = service._read_record(selected_run_id)
        manifest = application.repository.get_launch_manifest(selected_run_id)
        if manifest is None:
            raise DaemonError("daemon worker has no immutable launch manifest")
        if manifest.intended_unit != _unit_name(selected_run_id):
            raise DaemonError("daemon worker manifest unit identity is invalid")
        prepared, resume = _worker_prepared(record, manifest, root, config, workflow_config)
        from aflow.workflow import _freeze_run_identity

        frozen = _freeze_run_identity(prepared.workflow_name, workflow_config, config_dir=config)
        if manifest.frozen_config_fingerprint != frozen.config_fingerprint:
            raise DaemonError("daemon worker frozen configuration does not match launch intent")
        execute_workflow(
            prepared,
            resume=resume,
            allow_existing_launch_manifest=True,
        )
    except Exception as exc:
        print(f"aflow daemon worker: {exc}", file=sys.stderr)
        return 1
    return 0


def _worker_prepared(
    record: Mapping[str, object],
    manifest: LaunchManifest,
    repo_root: Path,
    config_path: Path,
    workflow_config: WorkflowUserConfig,
) -> tuple[PreparedRun, object | None]:
    run_id = validate_run_id(str(record["run_id"]))
    if record.get("mode") == "resume":
        source_run_id = validate_run_id(str(record["resumed_from_run_id"]))
        from aflow.cli import _bootstrap_resume_invocation

        bootstrap = _bootstrap_resume_invocation(
            repo_root=repo_root,
            config_path=config_path,
            workflow_config=workflow_config,
            requested_run_id=source_run_id,
            workflow_arg=None,
            plan_file_arg=None,
            team_arg=None,
            start_step_arg=None,
            max_turns_arg=None,
            extra_instructions_arg=(),
            extra_instructions_provided=False,
        )
        prepared = PreparedRun(
            workflow_name=bootstrap.workflow_name,
            repo_root=repo_root,
            plan_path=bootstrap.plan_path,
            config_path=config_path,
            max_turns=bootstrap.max_turns,
            team=bootstrap.team,
            extra_instructions=bootstrap.extra_instructions,
            start_step=(
                bootstrap.start_step
                or workflow_config.workflows[bootstrap.workflow_name].first_step
                or bootstrap.workflow_name
            ),
            reserved_run_id=run_id,
            idempotency_key=manifest.idempotency_key,
            caller_scope=manifest.caller_scope,
        )
        return prepared, bootstrap.resume_context
    payload = record.get("prepared")
    if not isinstance(payload, Mapping):
        raise DaemonError("daemon worker start record has no prepared request")
    prepared = _prepared_from_payload(payload, repo_root=repo_root, config_path=config_path)
    prepared = replace(
        prepared,
        reserved_run_id=run_id,
        idempotency_key=manifest.idempotency_key,
        caller_scope=manifest.caller_scope,
    )
    return prepared, None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the durable AFlow lifecycle daemon.")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--aflow-executable", type=Path)
    parser.add_argument("--environment-file", type=Path, required=True)
    parser.add_argument("--release-identity")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    executable = args.aflow_executable or Path(sys.argv[0]).resolve().with_name("aflow")
    release_identity = args.release_identity or str(Path(executable).resolve())
    daemon = AflowDaemon(
        DaemonConfig(
            repo_root=args.repo_root,
            config_path=args.config,
            aflow_executable=executable,
            environment_file=args.environment_file,
            release_identity=release_identity,
        )
    )
    try:
        daemon.start()
    except Exception as exc:
        print(f"aflowd: readiness failed: {exc}", file=sys.stderr)
        return 1
    if args.once:
        return 0
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda _signum, _frame: daemon.request_shutdown())
    daemon.serve_forever()
    return 0


def _resolve_configured_start_step(
    raw_start_step: str | None,
    workflow_name: str,
    steps: Mapping[str, object],
) -> str | None:
    if raw_start_step is None:
        return None
    value = str(raw_start_step)
    if value and value.isascii() and value.isdecimal():
        index = int(value)
        names = tuple(steps)
        if index < 1 or index > len(names):
            raise DaemonError(f"startup step index is out of range for workflow '{workflow_name}'")
        return names[index - 1]
    if value not in steps:
        raise DaemonError(f"startup step is not configured for workflow '{workflow_name}'")
    return value


def _answer_digest(answer: str | int | bool) -> str:
    if isinstance(answer, bool):
        payload: object = answer
    elif isinstance(answer, int) or isinstance(answer, str):
        payload = answer
    else:
        raise DaemonError("startup answer must be a string, integer, or boolean")
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _file_identity(path: Path) -> dict[str, object]:
    requested = Path(path)
    resolved = requested.resolve()
    if requested.is_symlink() or not resolved.is_file():
        raise DaemonError("daemon environment file identity is no longer safe")
    try:
        metadata = resolved.stat()
    except OSError as exc:
        raise DaemonError("daemon environment file identity cannot be read") from exc
    return {
        "path": str(resolved),
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "size": metadata.st_size,
        "mtime_ns": metadata.st_mtime_ns,
    }


def _startup_request_digest(request: StartupRequest) -> str:
    return hashlib.sha256(
        json.dumps(_request_payload(request), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _request_payload(request: StartupRequest) -> dict[str, object]:
    if request.extra_instructions:
        raise DaemonError("daemon startup records must not persist prompt-like extra instructions")
    return {
        "repo_root": str(Path(request.repo_root).resolve()),
        "plan_path": str(Path(request.plan_path).resolve()),
        "config_path": str(Path(request.config_path).resolve()),
        "workflow_name": request.workflow_name,
        "start_step": request.start_step,
        "max_turns": request.max_turns,
        "team": request.team,
        "resume_requested": request.resume_requested,
        "startup_base_head_refresh_sha": request.startup_base_head_refresh_sha,
        "dirty_worktree_confirmed": request.dirty_worktree_confirmed,
    }


def _request_from_payload(
    payload: Mapping[str, object],
    *,
    workflow_config: WorkflowUserConfig,
    reserved_run_id: str,
    caller_scope: str,
    idempotency_key: str | None,
) -> StartupRequest:
    for key in ("repo_root", "plan_path", "config_path"):
        if not isinstance(payload.get(key), str):
            raise DaemonError("startup record request has invalid path data")
    max_turns = payload.get("max_turns")
    if max_turns is not None and (not isinstance(max_turns, int) or isinstance(max_turns, bool)):
        raise DaemonError("startup record request has invalid max_turns")
    return StartupRequest(
        repo_root=Path(str(payload["repo_root"])),
        plan_path=Path(str(payload["plan_path"])),
        config_path=Path(str(payload["config_path"])),
        workflow_config=workflow_config,
        workflow_name=_optional_string(payload.get("workflow_name")),
        start_step=_optional_string(payload.get("start_step")),
        max_turns=max_turns,
        team=_optional_string(payload.get("team")),
        resume_requested=bool(payload.get("resume_requested", False)),
        startup_base_head_refresh_sha=_optional_string(payload.get("startup_base_head_refresh_sha")),
        dirty_worktree_confirmed=bool(payload.get("dirty_worktree_confirmed", False)),
        reserved_run_id=reserved_run_id,
        caller_scope=caller_scope,
        idempotency_key=idempotency_key,
    )


def _prepared_payload(prepared: PreparedRun) -> dict[str, object]:
    if prepared.extra_instructions:
        raise DaemonError("daemon prepared records must not persist prompt-like extra instructions")
    return {
        "workflow_name": prepared.workflow_name,
        "repo_root": str(prepared.repo_root),
        "plan_path": str(prepared.plan_path),
        "config_path": str(prepared.config_path),
        "max_turns": prepared.max_turns,
        "team": prepared.team,
        "start_step": prepared.start_step,
        "startup_base_head_refresh_sha": prepared.startup_base_head_refresh_sha,
        "move_completed_plan_to_done": prepared.move_completed_plan_to_done,
    }


def _prepared_from_payload(
    payload: Mapping[str, object],
    *,
    repo_root: Path,
    config_path: Path,
) -> PreparedRun:
    required_strings = ("workflow_name", "plan_path", "start_step")
    if any(not isinstance(payload.get(key), str) or not str(payload[key]).strip() for key in required_strings):
        raise DaemonError("prepared startup record has invalid required values")
    max_turns = payload.get("max_turns")
    if not isinstance(max_turns, int) or isinstance(max_turns, bool) or max_turns < 1:
        raise DaemonError("prepared startup record has invalid max_turns")
    return PreparedRun(
        workflow_name=str(payload["workflow_name"]),
        repo_root=repo_root,
        plan_path=Path(str(payload["plan_path"])),
        config_path=config_path,
        max_turns=max_turns,
        team=_optional_string(payload.get("team")),
        extra_instructions=(),
        start_step=str(payload["start_step"]),
        startup_base_head_refresh_sha=_optional_string(payload.get("startup_base_head_refresh_sha")),
        move_completed_plan_to_done=bool(payload.get("move_completed_plan_to_done", False)),
    )


def _question_payload(question: StartupQuestion) -> dict[str, object]:
    return {
        "kind": question.kind.value,
        "message": question.message,
        "options": dict(question.options),
        "choices": list(question.choices),
    }


def _question_from_record(record: Mapping[str, object]) -> StartupQuestion:
    raw = record.get("question")
    if not isinstance(raw, Mapping):
        raise DaemonError("startup record does not contain a question")
    try:
        kind = StartupQuestionKind(str(raw["kind"]))
        message = str(raw["message"])
        options = dict(raw.get("options", {}))
        choices = list(raw.get("choices", []))
    except (KeyError, TypeError, ValueError) as exc:
        raise DaemonError("startup record question has an invalid schema") from exc
    if not all(isinstance(key, str) and isinstance(value, str) for key, value in options.items()):
        raise DaemonError("startup record question options are invalid")
    if not all(isinstance(value, str) for value in choices):
        raise DaemonError("startup record question choices are invalid")
    return StartupQuestion(kind=kind, message=message, options=options, choices=choices)


def _question_generation(record: Mapping[str, object]) -> int:
    value = record.get("question_generation", 1)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise DaemonError("startup record question generation is invalid")
    return value


def _next_question_generation(record: Mapping[str, object]) -> int:
    if "question_generation" not in record:
        return 1
    return _question_generation(record) + 1


def _answered_question(
    record: Mapping[str, object],
    generation: int,
) -> Mapping[str, object] | None:
    history = record.get("answered_questions", [])
    if not isinstance(history, list):
        raise DaemonError("startup record answer history is invalid")
    for entry in history:
        if not isinstance(entry, Mapping):
            raise DaemonError("startup record answer history is invalid")
        if entry.get("generation") != generation:
            continue
        if not isinstance(entry.get("answer_digest"), str):
            raise DaemonError("startup record answer history is invalid")
        if entry.get("idempotency_key") is not None and not isinstance(entry.get("idempotency_key"), str):
            raise DaemonError("startup record answer history is invalid")
        return entry
    if generation == 1 and "answered_questions" not in record:
        answer_digest = record.get("answer_digest")
        idempotency_key = record.get("answer_idempotency_key")
        if isinstance(answer_digest, str) and (
            idempotency_key is None or isinstance(idempotency_key, str)
        ):
            return {
                "generation": generation,
                "answer_digest": answer_digest,
                "idempotency_key": idempotency_key,
            }
    return None


def _record_answer(
    record: dict[str, object],
    generation: int,
    answer_digest: str,
    idempotency_key: str | None,
) -> None:
    if _answered_question(record, generation) is not None:
        raise DaemonError("startup question answer was already recorded")
    history = list(record.get("answered_questions", ()))
    if len(history) >= _QUESTION_HISTORY_LIMIT:
        raise DaemonError("startup question answer history exceeded its bounded limit")
    history.append(
        {
            "generation": generation,
            "answer_digest": answer_digest,
            "idempotency_key": idempotency_key,
        }
    )
    record["answered_questions"] = history


def _question_record(
    run_id: str,
    question: StartupQuestion,
    generation: int,
) -> StartupQuestionRecord:
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
        raise DaemonError("startup question generation is invalid")
    return StartupQuestionRecord(
        question_id=f"startup-{run_id}-q{generation}",
        run_id=run_id,
        kind=question.kind.value,
        message=question.message,
        options=dict(question.options),
        choices=tuple(question.choices),
    )


def _question_identity(question_id: str) -> tuple[str, int]:
    prefix = "startup-"
    if not isinstance(question_id, str) or not question_id.startswith(prefix):
        raise DaemonError("startup question identity is invalid")
    raw = question_id.removeprefix(prefix)
    run_id, separator, raw_generation = raw.rpartition("-q")
    if not separator:
        return validate_run_id(raw), 1
    if not run_id or not raw_generation.isascii() or not raw_generation.isdecimal():
        raise DaemonError("startup question identity is invalid")
    generation = int(raw_generation)
    if generation < 1:
        raise DaemonError("startup question identity is invalid")
    return validate_run_id(run_id), generation


def _unit_name(run_id: str) -> str:
    return f"aflow-run-{validate_run_id(run_id)}.service"


def _equivalent_caller_scope(left: object, right: object) -> bool:
    """Treat REST and MCP as one bearer-owned project authorization scope."""
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    if left == right:
        return True
    left_prefix, left_separator, left_project = left.partition(":")
    right_prefix, right_separator, right_project = right.partition(":")
    compatible_prefixes = {"rest", "mcp", "bearer"}
    return (
        bool(left_separator and right_separator and left_project)
        and left_project == right_project
        and left_prefix in compatible_prefixes
        and right_prefix in compatible_prefixes
    )

def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _record_bytes(record: Mapping[str, object]) -> bytes:
    return (json.dumps(dict(record), sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
