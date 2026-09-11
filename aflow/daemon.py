"""Durable lifecycle ownership for independent AFlow workflow units.

``aflowd`` owns launch intent and unit observation, never the lifetime of a
terminal connection.  The workflow controller remains the authority for its
``run.json`` and receives the normal installed ``aflow`` executable.
"""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field, replace
import fcntl
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import sys
import tempfile
from threading import Event, RLock
import time
from typing import Any, Callable, Iterator, Mapping, NoReturn

from aflow.api.models import (
    PreparedRun,
    StartupQuestion,
    StartupQuestionKind,
    StartupRequest,
)
from aflow.api.runner import execute_workflow
from aflow.api.startup import (
    PLAN_ADMISSION_ERROR_CODE,
    PLAN_ADMISSION_CHECKPOINT_KIND,
    PLAN_ADMISSION_TRACKING_KIND,
    PlanAdmissionError,
    StartupError,
    prepare_startup,
    prepare_startup_with_answer,
)
from aflow.control_plane.models import startup_failure
from aflow.control_plane.worker_diagnostics import confirmed_inactive
from aflow.config import ConfigError, WorkflowUserConfig, load_workflow_config
from aflow.live_config import load_live_config
from aflow.run_config_snapshot import (
    SnapshotError,
    create_run_config_snapshot,
)
from aflow.control_plane import (
    ControlConflictError,
    ControlPlaneApplication,
    RecoveryEvidenceReference,
    RecoveryIntent,
    RecoveryPersistenceError,
    RecoveryRequest,
    RecoveryValidationError,
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
    persist_recovery_intent,
    read_events,
    read_recovery_intent,
    recovery_intent_digest,
    recovery_artifact_digest,
    reserve_run_id,
    validate_run_id,
    write_launch_phase,
)
from aflow.control_plane.persistence import (
    PersistenceError,
    _contained_directory,
    normalized_request_digest,
)
from aflow.control_plane.units import UnitManager, UnitState
from aflow.git_status import WorktreePreflight
from aflow.harnesses import ADAPTERS
from aflow.hotplug import workspace_fingerprint
from aflow.recovery_runtime import (
    RecoveryRuntimeValidationError,
    validate_recovery_runtime,
)


_START_RECORD_SCHEMA_VERSION = 1
_START_RECORD_SUFFIX = ".json"
_REPLAYABLE_PHASES = frozenset({None, "manifest_only"})
_SAFE_ENVIRONMENT_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
_QUESTION_HISTORY_LIMIT = 64
_MAX_EXTRA_INSTRUCTION_ITEMS = 8
_MAX_EXTRA_INSTRUCTION_LENGTH = 512
_MAX_EXTRA_INSTRUCTIONS_LENGTH = 4096
_logger = logging.getLogger(__name__)


class DaemonError(RuntimeError):
    """The daemon cannot safely carry out a lifecycle operation."""


_RECOVERY_REJECTION_MESSAGES = {
    "recovery_source_state": (
        "Durable recovery requires a failed, interrupted, or owner-stopped "
        "source with exact terminal evidence."
    ),
    "recovery_source_activity": (
        "Durable recovery requires confirmed inactive source ownership; "
        "source activity is unknown or active."
    ),
    "recovery_operation_unresolved": (
        "Durable recovery has unknown liveness or prior turn evidence; "
        "reconcile the operation before retrying."
    ),
    "recovery_target_invalid": (
        "The explicitly selected durable-recovery target worker selector is "
        "not configured in the current settings."
    ),
    "recovery_evidence_unavailable": (
        "Durable recovery evidence bytes changed or are unavailable; "
        "refresh the source run before retrying."
    ),
}


class DurableRecoveryRejection(DaemonError):
    """A safe, actionable rejection of an explicit recovery admission."""

    def __init__(self, code: str) -> None:
        try:
            message = _RECOVERY_REJECTION_MESSAGES[code]
        except KeyError as exc:
            raise ValueError("unknown durable recovery rejection code") from exc
        self.code = code
        super().__init__(message)


def _reject_recovery(code: str) -> NoReturn:
    raise DurableRecoveryRejection(code)


def _recovery_fingerprint_plan_paths(paths: Iterable[Path]) -> tuple[Path, ...]:
    """Normalize the exact Markdown evidence inputs used by both boundaries."""
    normalized: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        candidate = Path(path).resolve()
        if candidate.suffix.lower() != ".md" or candidate in seen:
            continue
        seen.add(candidate)
        normalized.append(candidate)
    return tuple(normalized)


def _validate_recovery_runtime_shape(
    raw_runtime: object,
    *,
    expected_source_run_id: str | None = None,
    expected_target_run_id: str | None = None,
    expected_intent: RecoveryIntent | None = None,
    expected_intent_digest: str | None = None,
    allow_pending: bool = True,
) -> tuple[str, bool]:
    """Validate the shared marker and retain the daemon's typed error contract."""
    try:
        return validate_recovery_runtime(
            raw_runtime,
            expected_source_run_id=expected_source_run_id,
            expected_target_run_id=expected_target_run_id,
            expected_intent=expected_intent,
            expected_intent_digest=expected_intent_digest,
            allow_pending=allow_pending,
        )
    except RecoveryRuntimeValidationError as exc:
        raise DurableRecoveryRejection("recovery_operation_unresolved") from exc


class DaemonStartupError(DaemonError):
    """A startup/admission request failed with an already safe diagnostic."""

    def __init__(
        self,
        run_id: str | None,
        message: str,
        *,
        code: str = "startup_failed",
    ) -> None:
        super().__init__(message)
        self.run_id = run_id
        self.code = code


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
        if (
            Path(self.aflow_executable).is_symlink()
            or not executable.is_file()
            or not os.access(executable, os.X_OK)
        ):
            raise DaemonError(
                "aflow executable must be an installed executable from the selected release"
            )
        if Path(self.environment_file).is_symlink() or not environment_file.is_file():
            raise DaemonError(
                "daemon environment file must be a regular non-symlink file"
            )
        if (
            not isinstance(self.release_identity, str)
            or not self.release_identity.strip()
        ):
            raise DaemonError("daemon release identity must be a non-empty string")
        if (
            not math.isfinite(self.stop_timeout_seconds)
            or not math.isfinite(self.poll_interval_seconds)
            or self.stop_timeout_seconds < 0
            or self.poll_interval_seconds <= 0
        ):
            raise DaemonError("daemon timeout and polling values must be positive")
        environment = dict(self.environment or {})
        for key, value in environment.items():
            if (
                not isinstance(key, str)
                or _SAFE_ENVIRONMENT_NAME_RE.fullmatch(key) is None
                or not isinstance(value, str)
                or any(marker in value for marker in ("\x00", "\n", "\r"))
            ):
                raise DaemonError(
                    "daemon environment must be an explicit safe string allowlist"
                )
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

    def __init__(
        self, config: DaemonConfig, *, units: UnitManager | None = None
    ) -> None:
        self._config = config.validated()
        self._units = units
        self._application: ControlPlaneApplication | None = None
        self._service: DaemonService | None = None
        self._ready = False
        self._stop = Event()

    @property
    def config_path(self) -> Path:
        """The validated, read-only configuration path selected for this daemon."""
        return self._config.config_path

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

    def start(self, *, persist_reconciliation: bool = True) -> tuple[ReconciliationResult, ...]:
        """Load configuration and reconcile; this method never starts a workflow."""
        workflow_config = load_live_config(
            self._config.config_path,
            loader=load_workflow_config,
        ).workflow_config
        application = compose_control_plane(
            self._config.repo_root,
            config_path=self._config.config_path,
            units=self._units,
        )
        application.capabilities.get()
        reconciled = application.reconciliation.reconcile_all(persist=persist_reconciliation)
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

    def shutdown(self) -> tuple[UnitState, ...]:
        """Stop the poll loop and drain only locally owned subprocess units."""
        self.request_shutdown()
        shutdown = getattr(self._units, "shutdown", None)
        if shutdown is None:
            return ()
        return tuple(shutdown())


def _bootstrap_config_path(bootstrap: Any, config: "DaemonConfig") -> Path:
    """Return the daemon's current execution source for a prepared resume."""
    return config.config_path


def _bootstrap_workflow_config(
    bootstrap: Any, service: "DaemonService"
) -> WorkflowUserConfig:
    value = getattr(bootstrap, "workflow_config", None)
    return value if value is not None else service._workflow_config


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
        self._transient_extra_instructions: dict[str, tuple[str, ...]] = {}

    def _refresh_workflow_config(self) -> None:
        """Reload the workflow pair so global edits apply to new runs immediately.

        Existing runs keep their execution facts; current settings govern
        reservation, startup answers, and worker preparation.
        """
        try:
            self._workflow_config = load_live_config(
                self._config.config_path,
                loader=load_workflow_config,
            ).workflow_config
        except ConfigError as exc:
            raise DaemonError(f"workflow configuration is invalid: {exc}") from exc

    def start(
        self,
        request: StartupRequest,
        *,
        caller_scope: str = "local",
        idempotency_key: str | None = None,
    ) -> StartRunResult | StartupQuestionRecord:
        """Reserve durable intent before evaluating the interactive startup gate."""
        lineage_lock = self._durable_lock(".restart-locks", validate_run_id(request.restarted_from_run_id)) if request.restarted_from_run_id else nullcontext()
        with self._lock, lineage_lock, self._idempotency_lock("start", caller_scope, idempotency_key):
            self._refresh_workflow_config()
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
                self._transient_extra_instructions[
                    validate_run_id(str(pending["run_id"]))
                ] = normalized.extra_instructions
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
                request=normalized,
                candidate=candidate,
            )
            if existing is not None:
                self._transient_extra_instructions[existing.run_id] = normalized.extra_instructions
                return self._recover_start_manifest(existing, normalized, request_digest)

            self._prepare_required_git_tracking_before_reservation(normalized)
            run_id = reserve_run_id(self._config.repo_root)
            self._transient_extra_instructions[run_id] = normalized.extra_instructions
            effective_key = idempotency_key or f"daemon-{run_id}"
            manifest = self._initial_manifest_for(
                run_id=run_id,
                request=normalized,
                caller_scope=caller_scope,
                idempotency_key=effective_key,
            )
            try:
                manifest_result = create_launch_manifest(
                    self._config.repo_root, manifest
                )
            except (ValueError, RunIdentityConflict) as exc:
                raise DaemonError(f"cannot reserve launch intent: {exc}") from exc
            if not manifest_result.created:
                return self._recover_start_manifest(
                    manifest, normalized, request_digest
                )
            try:
                create_run_config_snapshot(
                    repo_root=self._config.repo_root,
                    run_id=run_id,
                    config_path=self._config.config_path,
                    workflow_name=manifest.workflow_name,
                    fingerprint=manifest.frozen_config_fingerprint,
                    loader=load_workflow_config,
                )
            except SnapshotError:
                # The copy is diagnostic compatibility only.  The live source
                # in the request remains the sole execution configuration.
                pass
            record = self._new_start_record(
                run_id=run_id,
                request=replace(
                    normalized,
                    reserved_run_id=run_id,
                    config_path=self._config.config_path,
                ),
                request_digest=request_digest,
                caller_scope=caller_scope,
                idempotency_key=idempotency_key,
                state="preparing",
                operation="start",
            )
            record["manifest_request_digest"] = normalized_request_digest(manifest)
            self._create_record(record)
            if normalized.restarted_from_run_id is not None:
                append_run_event(
                    self._application.repository.run_directory(
                        normalized.restarted_from_run_id
                    ),
                    "restart_successor_requested",
                    {"successor_run_id": run_id},
                )
            with self._startup_record_lock(run_id):
                return self._advance_start_preparation_locked(
                    self._read_record(run_id),
                    created=True,
                )

    def preflight(
        self,
        request: StartupRequest,
        *,
        caller_scope: str = "local",
    ) -> WorktreePreflight:
        """Inspect startup dirtiness without reserving or preparing a run.

        The request is normalized against the current live configuration and
        passed through the existing request-level selection validation.  The
        manifest is built only in memory; no launch, startup record, snapshot,
        or unit operation is performed here.
        """
        with self._lock:
            self._refresh_workflow_config()
            normalized = self._normalize_request(
                request,
                caller_scope=caller_scope,
                idempotency_key=None,
            )
            candidate = self._initial_manifest_for(
                run_id="preflight-preview",
                request=normalized,
                caller_scope=caller_scope,
                idempotency_key="preflight-preview",
            )
            from aflow.api.startup import _check_worktree_dirtiness

            try:
                return _check_worktree_dirtiness(
                    normalized,
                    candidate.workflow_name,
                    reject_blockers=False,
                )
            except StartupError as exc:
                raise DaemonError(str(exc)) from exc

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
            if (
                self._application.repository.get_run_status(run_id).status
                == "owner_stopped"
            ):
                return self._existing_start_result(run_id)
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
                prepared_or_question = prepare_startup_with_answer(
                    question, request, answer
                )
            except PlanAdmissionError as exc:
                _logger.warning(
                    "startup plan admission rejected for reserved run %s",
                    run_id,
                    exc_info=True,
                )
                updated = dict(record)
                updated["state"] = "needs_attention"
                updated["startup_failure"] = startup_failure(
                    "preparation",
                    exc.safe_message,
                    code=exc.code,
                    kind=exc.kind,
                )
                self._write_record(updated)
                raise DaemonStartupError(
                    run_id,
                    exc.safe_message,
                    code=exc.code,
                ) from exc
            except StartupError as exc:
                updated = dict(record)
                updated["state"] = "needs_attention"
                updated["startup_failure"] = startup_failure("preparation", str(exc))
                self._write_record(updated)
                raise DaemonStartupError(run_id, updated["startup_failure"]["message"]) from exc
            if isinstance(prepared_or_question, StartupQuestion):
                updated = dict(record)
                _record_answer(
                    updated, question_generation, answer_digest, idempotency_key
                )
                updated["question"] = _question_payload(prepared_or_question)
                updated["request"] = _request_payload(
                    prepared_or_question.continuation_request or request
                )
                updated["question_generation"] = question_generation + 1
                self._write_record(updated)
                return _question_record(
                    run_id, prepared_or_question, question_generation + 1
                )
            prepared = replace(
                prepared_or_question,
                reserved_run_id=run_id,
                idempotency_key=str(record["effective_idempotency_key"]),
                caller_scope=str(record["caller_scope"]),
                restarted_from_run_id=request.restarted_from_run_id,
                skipped_steps=_skipped_steps_for(
                    request.workflow_config,
                    prepared_or_question.workflow_name,
                    prepared_or_question.start_step,
                ),
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
                raise DaemonError(
                    "legacy runs are read-only and cannot be stopped by the daemon"
                )
            self._assert_manifest_caller(run_id, caller_scope)
            artifact_path = (
                self._config.repo_root
                / ".aflow"
                / "runs"
                / validate_run_id(run_id)
            )
            if artifact_path.is_symlink():
                raise DaemonError("run artifact path is unsafe")
            run_dir = self._application.repository.run_directory(run_id)
            if not run_dir.is_dir():
                if run_dir.exists():
                    raise DaemonError("run artifact path is unsafe")
                if expected_revision != 0:
                    raise ControlConflictError(0)
                run_dir.mkdir()
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
                    raise DaemonError(
                        "workflow unit did not stop before the bounded timeout"
                    )
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
        extra_instructions: tuple[str, ...] | None = None,
        recovery: Mapping[str, object] | RecoveryRequest | None = None,
    ) -> StartRunResult:
        """Launch one validated continuation; the source unit is never restarted."""
        if extra_instructions is not None:
            _validate_extra_instructions(extra_instructions)
        try:
            normalized_recovery = RecoveryRequest.from_value(recovery)
        except RecoveryValidationError as exc:
            if recovery is not None:
                raise DurableRecoveryRejection("recovery_target_invalid") from exc
            raise DaemonError(str(exc)) from exc
        normalized_source_run_id = validate_run_id(source_run_id)
        requested_extra_digest = (
            _extra_instructions_digest(extra_instructions)
            if extra_instructions is not None
            else None
        )
        with (
            self._lock,
            self._idempotency_lock("resume", caller_scope, idempotency_key),
        ):
            pending = self._find_pending_request(
                operation="resume",
                caller_scope=caller_scope,
                idempotency_key=idempotency_key,
            )
            if pending is not None:
                if pending.get("resumed_from_run_id") != normalized_source_run_id:
                    raise DaemonIdempotencyConflict(
                        "resume idempotency key was reused for a different source run"
                    )
                try:
                    stored_recovery = RecoveryRequest.from_value(
                        pending.get("recovery")
                    )
                except RecoveryValidationError as exc:
                    raise DaemonError(
                        "resume record has an invalid recovery request"
                    ) from exc
                if stored_recovery != normalized_recovery:
                    raise DaemonIdempotencyConflict(
                        "resume idempotency key was reused for a different recovery request"
                    )
                stored_extra_digest = _record_extra_instructions_digest(pending)
                provided = pending.get("resume_extra_instructions_provided", False)
                if not isinstance(provided, bool):
                    raise DaemonError(
                        "resume record has an invalid extra-instructions override flag"
                    )
                if (
                    extra_instructions is not None
                    and (
                        stored_extra_digest is None
                        or stored_extra_digest != requested_extra_digest
                    )
                ) or (extra_instructions is None and provided):
                    raise DaemonIdempotencyConflict(
                        "resume idempotency key was reused for a different request"
                    )
                if extra_instructions is not None:
                    self._transient_extra_instructions[
                        validate_run_id(str(pending["run_id"]))
                    ] = extra_instructions
                return self._recover_resume_record(pending)
            source = self._application.repository.get_run_status(normalized_source_run_id)
            _validate_persisted_recovery_runtime(
                self._application.repository.run_directory(normalized_source_run_id),
                expected_run_id=normalized_source_run_id,
                allow_pending=False,
                missing_ok=True,
            )
            if normalized_recovery is None:
                worker = source.evidence.get("worker")
                if isinstance(worker, Mapping) and not confirmed_inactive(worker):
                    raise DaemonError("source worker activity could not be confirmed inactive")
                if source.ownership != "control_plane":
                    raise DaemonError("legacy runs cannot be resumed by the control plane")
                unit_name = _unit_name(normalized_source_run_id)
                observed = self._application.units.get(unit_name)
                if observed is not None and observed.name != unit_name:
                    raise DaemonError("source workflow unit identity is ambiguous")
                if observed is not None and observed.is_active:
                    raise DaemonError("an active workflow unit cannot be resumed")
                if source.launch_phase in {"manifest_only", "launch_requested"}:
                    raise DaemonError(
                        "source run has an incomplete or ambiguous launch attempt"
                    )
                if (
                    source.launch_phase == "launch_started"
                    and source.status != "needs_attention"
                    and not source.evidence.get("controller_terminal")
                ):
                    raise DaemonError(
                        "a killed launched run must be reconciled before explicit resume"
                    )
                if source.status not in {
                    "running",
                    "failed",
                    "interrupted",
                    "needs_attention",
                    "waiting_for_valid_override",
                    "owner_stopped",
                }:
                    raise DaemonError(
                        "source run is incomplete, terminal, or lacks safe resume evidence"
                    )
            else:
                self._admit_recovery_source(
                    normalized_source_run_id,
                    source,
                )
                self._refresh_workflow_config()
                self._validate_recovery_target(normalized_recovery)
            bootstrap = self._resume_bootstrap(
                normalized_source_run_id,
                extra_instructions=extra_instructions or (),
                extra_instructions_provided=extra_instructions is not None,
            )
            source_manifest = self._application.repository.get_launch_manifest(
                normalized_source_run_id
            )
            if source_manifest is None:
                raise DaemonError("source run has no control-plane launch manifest")
            self._assert_manifest_caller(normalized_source_run_id, caller_scope)

            run_id = reserve_run_id(self._config.repo_root)
            self._transient_extra_instructions[run_id] = bootstrap.extra_instructions
            prepared = PreparedRun(
                workflow_name=bootstrap.workflow_name,
                repo_root=self._config.repo_root,
                plan_path=bootstrap.plan_path,
                config_path=_bootstrap_config_path(bootstrap, self._config),
                max_turns=bootstrap.max_turns,
                team=bootstrap.team,
                extra_instructions=bootstrap.extra_instructions,
                start_step=(
                    bootstrap.start_step
                    or _bootstrap_workflow_config(bootstrap, self).workflows[
                        bootstrap.workflow_name
                    ].first_step
                    or bootstrap.workflow_name
                ),
                reserved_run_id=run_id,
                idempotency_key=idempotency_key or f"daemon-{run_id}",
                caller_scope=caller_scope,
                team_explicit=getattr(bootstrap, "team_explicit", None),
                max_turns_explicit=getattr(bootstrap, "max_turns_explicit", None),
                start_step_explicit=True,
            )
            manifest = self._manifest_for(
                run_id=run_id,
                prepared=prepared,
                caller_scope=caller_scope,
                idempotency_key=prepared.idempotency_key or f"daemon-{run_id}",
                workflow_config=_bootstrap_workflow_config(bootstrap, self),
            )
            request_digest = normalized_request_digest(manifest)

            recovery_intent = (
                self._build_recovery_intent(
                    source_run_id=normalized_source_run_id,
                    target_run_id=run_id,
                    source=source,
                    source_manifest=source_manifest,
                    bootstrap=bootstrap,
                    target_request=normalized_recovery,
                )
                if normalized_recovery is not None
                else None
            )

            record = self._new_start_record(
                run_id=run_id,
                request=None,
                request_digest=request_digest,
                caller_scope=caller_scope,
                idempotency_key=idempotency_key,
                state="prepared",
                prepared=prepared,
                operation="resume",
                mode="resume",
                resumed_from_run_id=normalized_source_run_id,
                source_invocation_digest=source_manifest.request_digest,
                resume_extra_instructions_provided=extra_instructions is not None,
                resume_extra_instructions_digest=_extra_instructions_digest(
                    bootstrap.extra_instructions
                ),
                recovery=normalized_recovery,
            )
            record["manifest_request_digest"] = normalized_request_digest(manifest)
            if recovery_intent is not None:
                record["recovery_intent_digest"] = recovery_intent_digest(
                    recovery_intent
                )

                # The launch manifest is immutable and must exist before the
                # successor directory is created.  The intent itself is
                # published by the replay-safe recovery path below, after the
                # startup record has an identity that can be retried.
                try:
                    create_launch_manifest(self._config.repo_root, manifest)
                except (ValueError, RunIdentityConflict) as exc:
                    raise DaemonError(
                        f"cannot reserve recovery launch intent: {exc}"
                    ) from exc
                successor_dir = self._application.repository.run_directory(run_id)
                if successor_dir.exists() and not successor_dir.is_dir():
                    raise DaemonError("recovery successor run directory is unsafe")
                successor_dir.mkdir(parents=True, exist_ok=False)
            self._create_record(record)
            return self._recover_resume_record(
                record,
                prepared=prepared,
                created=True,
                recovery_intent=recovery_intent,
            )

    def run_status(self, run_id: str) -> RunStatus:
        """Project a persisted startup question into canonical run status."""
        from .control_plane.run_activity import project_activity
        status = self._application.repository.get_run_status(run_id)
        if status.status == "owner_stopped":
            return project_activity(
                replace(
                    status,
                    evidence={
                        **status.evidence,
                        "can_resume": self._can_resume(status),
                    },
                )
            )
        if status.ownership != "control_plane":
            return project_activity(status)
        status = replace(status, evidence={**status.evidence, "can_resume": self._can_resume(status)})
        try:
            observed = self._application.units.get(_unit_name(run_id))
            if observed is not None and observed.name == _unit_name(run_id) and status.evidence.get("worker") is None:
                status = replace(status, evidence={**status.evidence, "unit_active": observed.is_active, "unit_observation": "observed"})
            elif status.evidence.get("worker") is None:
                status = replace(status, evidence={**status.evidence, "unit_observation": "missing" if observed is None else "identity_mismatch"})
        except Exception:
            status = replace(status, evidence={**status.evidence, "unit_observation": "unavailable"})
        try:
            record = self._read_record(run_id)
        except DaemonError:
            return project_activity(status)
        if record.get("state") == "awaiting_startup_answer" and status.evidence.get("startup_question_valid") and status.status not in {
            "running", "completed", "failed", "interrupted",
        }:
            return project_activity(replace(
                status,
                status="awaiting_startup_answer",
                reason="startup answer required before workflow unit creation",
                evidence={
                    **status.evidence,
                    "startup_question_valid": True,
                    "startup_question": _question_record(
                        run_id, _question_from_record(record), _question_generation(record)
                    ).to_dict(),
                },
            ))
        prepared = record.get("prepared")
        if isinstance(prepared, Mapping):
            selected = _optional_string(prepared.get("start_step"))
            skipped = prepared.get("skipped_steps", ())
            if (
                selected is not None
                and isinstance(skipped, list)
                and all(isinstance(item, str) for item in skipped)
            ):
                return project_activity(replace(
                    status,
                    selected_start_step=selected,
                    skipped_steps=tuple(skipped),
                ))
        return project_activity(status)

    def _can_resume(self, status: RunStatus) -> bool:
        """Read-only admission preview; resume rechecks before any reservation."""
        worker = status.evidence.get("worker")
        if isinstance(worker, Mapping) and not confirmed_inactive(worker):
            return False
        if status.status not in {
            "failed",
            "interrupted",
            "needs_attention",
            "waiting_for_valid_override",
            "owner_stopped",
        }:
            return False
        if status.launch_phase in {None, "manifest_only", "launch_requested"}:
            return False
        if (
            status.launch_phase == "launch_started"
            and status.status != "needs_attention"
            and not status.evidence.get("controller_terminal")
        ):
            return False
        try:
            observed = self._application.units.get(_unit_name(status.run_id))
            if observed is not None and (observed.name != _unit_name(status.run_id) or observed.is_active):
                return False
            self._resume_bootstrap(status.run_id)
        except Exception:
            return False
        return True

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
        _validate_extra_instructions(request.extra_instructions)
        if request.restarted_from_run_id is not None:
            validate_run_id(request.restarted_from_run_id)
        if Path(request.repo_root).resolve() != self._config.repo_root:
            raise DaemonError("startup request repository does not match this daemon")
        if Path(request.config_path).resolve() != self._config.config_path:
            raise DaemonError(
                "startup request configuration does not match this daemon"
            )
        plan_path = Path(request.plan_path).resolve()
        try:
            plan_path.relative_to(self._config.repo_root)
        except ValueError as exc:
            raise DaemonError(
                "startup request plan is outside this daemon project"
            ) from exc
        return replace(
            request,
            repo_root=self._config.repo_root,
            plan_path=plan_path,
            config_path=self._config.config_path,
            workflow_config=self._workflow_config,
            caller_scope=caller_scope,
            idempotency_key=idempotency_key,
        )

    def _prepare_required_git_tracking_before_reservation(
        self,
        request: StartupRequest,
    ) -> None:
        """Normalize required plan metadata before daemon run allocation."""
        workflow_name = (
            request.workflow_name or self._workflow_config.aflow.default_workflow
        )
        if (
            workflow_name is None
            or workflow_name not in self._workflow_config.workflows
        ):
            raise DaemonError("startup request does not name a configured workflow")
        workflow = self._workflow_config.workflows[workflow_name]

        from aflow.git_status import probe_repo_state
        from aflow.plan import (
            GitTrackingMetadataError,
            MISSING_CHECKPOINT_SECTIONS,
            PlanParseError,
            is_handoff_pristine_for_base_refresh,
            load_plan,
            parse_git_tracking_metadata,
        )
        from aflow.workflow import (
            WorkflowError,
            _backup_original_plan,
            _lifecycle_is_bootstrap_eligible,
            _prepare_required_git_tracking_before_allocation,
            _workflow_requires_git_tracking,
        )

        if not _workflow_requires_git_tracking(workflow, self._workflow_config):
            return
        try:
            source_bytes = request.plan_path.read_bytes()
            plan_text = source_bytes.decode("utf-8")
            metadata = parse_git_tracking_metadata(plan_text)
            if metadata is not None:
                if metadata.plan_branch is None or metadata.pre_handoff_base_head is None:
                    raise PlanAdmissionError(PLAN_ADMISSION_TRACKING_KIND)
                try:
                    parsed_plan = load_plan(request.plan_path)
                except PlanParseError as exc:
                    if exc.error_kind == "inconsistent_checkpoint_state":
                        return
                    raise
                if (
                    metadata.plan_branch != ""
                    and metadata.pre_handoff_base_head != ""
                ):
                    return
                if not is_handoff_pristine_for_base_refresh(
                    metadata,
                    parsed_plan.sections,
                ):
                    return
            repo_state = probe_repo_state(self._config.repo_root)
            needs_bootstrap = _lifecycle_is_bootstrap_eligible(workflow, repo_state)
            _backup_original_plan(
                self._config.repo_root,
                request.plan_path,
                event="startup_preparation",
            )
            parsed_plan = load_plan(request.plan_path)
            _prepare_required_git_tracking_before_allocation(
                repo_root=self._config.repo_root,
                original_plan_path=request.plan_path,
                parsed_plan=(
                    parsed_plan
                    if metadata is not None
                    else load_plan(request.plan_path)
                ),
                wf=workflow,
                workflow_config=self._workflow_config,
                repo_state=repo_state,
                needs_bootstrap=needs_bootstrap,
                is_resume=request.resume_requested,
                startup_retry=None,
                expected_plan_bytes=source_bytes,
            )
        except PlanAdmissionError as exc:
            _logger.warning(
                "startup plan admission rejected before run reservation",
                exc_info=True,
            )
            raise DaemonStartupError(
                None,
                exc.safe_message,
                code=exc.code,
            ) from exc
        except GitTrackingMetadataError as exc:
            _logger.warning(
                "startup plan admission rejected before run reservation",
                exc_info=True,
            )
            raise DaemonStartupError(
                None,
                PlanAdmissionError.safe_message_for(PLAN_ADMISSION_TRACKING_KIND),
                code=PLAN_ADMISSION_ERROR_CODE,
            ) from exc
        except PlanParseError as exc:
            if exc.admission_kind == MISSING_CHECKPOINT_SECTIONS:
                _logger.warning(
                    "startup plan admission rejected before run reservation",
                    exc_info=True,
                )
                raise DaemonStartupError(
                    None,
                    PlanAdmissionError.safe_message_for(PLAN_ADMISSION_CHECKPOINT_KIND),
                    code=PLAN_ADMISSION_ERROR_CODE,
                ) from exc
            _logger.warning(
                "startup plan preflight failed before run reservation",
                exc_info=True,
            )
            raise DaemonError("startup plan preflight failed") from exc
        except WorkflowError as exc:
            if exc.failure_kind == "missing_git_tracking":
                _logger.warning(
                    "startup plan admission rejected before run reservation",
                    exc_info=True,
                )
                raise DaemonStartupError(
                    None,
                    PlanAdmissionError.safe_message_for(PLAN_ADMISSION_TRACKING_KIND),
                    code=PLAN_ADMISSION_ERROR_CODE,
                ) from exc
            _logger.warning(
                "startup plan preflight failed before run reservation",
                exc_info=True,
            )
            raise DaemonError("startup plan preflight failed") from exc
        except (OSError, UnicodeError, ValueError) as exc:
            _logger.warning(
                "startup plan preflight failed before run reservation",
                exc_info=True,
            )
            raise DaemonError("startup plan preflight failed") from exc

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
        resume_extra_instructions_provided: bool | None = None,
        resume_extra_instructions_digest: str | None = None,
        recovery: RecoveryRequest | None = None,
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
            record["live_config_path"] = str(request.config_path)
            for field_name in (
                "team_explicit",
                "max_turns_explicit",
                "start_step_explicit",
            ):
                value = getattr(request, field_name)
                if value is not None:
                    record[field_name] = value
        if prepared is not None:
            record["prepared"] = _prepared_payload(prepared)
            record["live_config_path"] = str(prepared.config_path)
            for field_name in (
                "team_explicit",
                "max_turns_explicit",
                "start_step_explicit",
            ):
                value = getattr(prepared, field_name)
                if value is not None:
                    record[field_name] = value
        if question is not None:
            record["question"] = _question_payload(question)
        if resumed_from_run_id is not None:
            record["resumed_from_run_id"] = validate_run_id(resumed_from_run_id)
        if source_invocation_digest is not None:
            record["source_invocation_digest"] = source_invocation_digest
        if resume_extra_instructions_provided is not None:
            if not isinstance(resume_extra_instructions_provided, bool):
                raise DaemonError(
                    "resume record extra-instructions override flag is invalid"
                )
            record["resume_extra_instructions_provided"] = (
                resume_extra_instructions_provided
            )
        if resume_extra_instructions_digest is not None:
            record["resume_extra_instructions_digest"] = (
                resume_extra_instructions_digest
            )
        if recovery is not None:
            record["recovery"] = recovery.to_dict()
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
        from .control_plane.run_activity import preparation_owner
        record = {**record, "preparation_owner": preparation_owner()}
        self._write_record(record)
        request = self._request_from_record(record)
        try:
            prepared_or_question = prepare_startup(request)
        except PlanAdmissionError as exc:
            _logger.warning(
                "startup plan admission rejected for reserved run %s",
                record["run_id"],
                exc_info=True,
            )
            updated = dict(record)
            updated["state"] = "needs_attention"
            updated["startup_failure"] = startup_failure(
                "preparation",
                exc.safe_message,
                code=exc.code,
                kind=exc.kind,
            )
            self._write_record(updated)
            raise DaemonStartupError(
                str(record["run_id"]),
                exc.safe_message,
                code=exc.code,
            ) from exc
        except StartupError as exc:
            updated = dict(record)
            updated["state"] = "needs_attention"
            updated["startup_failure"] = startup_failure("preparation", str(exc))
            self._write_record(updated)
            raise DaemonStartupError(str(record["run_id"]), updated["startup_failure"]["message"]) from exc
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
            restarted_from_run_id=request.restarted_from_run_id,
            skipped_steps=_skipped_steps_for(
                request.workflow_config,
                prepared_or_question.workflow_name,
                prepared_or_question.start_step,
            ),
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
            return StartRunResult(
                run_id=run_id,
                created=False,
                status="needs_attention",
                reason="unit identity is ambiguous",
            )
        if observed is not None and observed.is_active:
            return StartRunResult(
                run_id=run_id,
                created=False,
                status="running",
                restarted_from_run_id=persisted_manifest.restarted_from_run_id,
            )
        if status.launch_phase not in _REPLAYABLE_PHASES:
            return self._existing_start_result(run_id)

        extra_instructions = self._transient_extra_instructions.get(run_id, ())
        expected_extra_digest = _optional_string(record.get("extra_instructions_digest"))
        if expected_extra_digest is None:
            prepared_payload = record.get("prepared")
            if isinstance(prepared_payload, Mapping):
                expected_extra_digest = _optional_string(
                    prepared_payload.get("extra_instructions_digest")
                )
        if expected_extra_digest not in {None, _extra_instructions_digest(extra_instructions)}:
            raise DaemonError(
                "non-persistent extra instructions are unavailable; submit a new start request"
            )
        argv = self._worker_argv(run_id, extra_instructions)
        public_argv = self._worker_argv(run_id, ())
        environment_identity = _file_identity(self._config.environment_file)
        mutable = dict(record)
        mutable["state"] = "launch_requested"
        mutable["argv"] = list(public_argv)
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
                "argv": list(public_argv),
                "extra_instructions_digest": _extra_instructions_digest(extra_instructions),
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
                raise DaemonError(
                    "systemd did not prove the exact workflow unit is active"
                )
        except Exception as exc:
            mutable["state"] = "needs_attention"
            mutable["startup_failure"] = startup_failure("unit_launch", f"workflow unit failed to start: {exc}")
            self._write_record(mutable)
            raise DaemonStartupError(run_id, mutable["startup_failure"]["message"]) from exc
        write_launch_phase(self._config.repo_root, run_id, "unit_started")
        append_run_event(run_dir, "unit_started", {"unit_name": _unit_name(run_id)})
        mutable["state"] = "unit_started"
        self._write_record(mutable)
        self._transient_extra_instructions.pop(run_id, None)
        return StartRunResult(
            run_id=run_id,
            created=created,
            status="running",
            manifest_path=str(self._config.repo_root / ".aflow" / "launches" / f"{run_id}.json"),
            restarted_from_run_id=persisted_manifest.restarted_from_run_id,
        )

    def _replay_manifest(self, manifest: LaunchManifest) -> StartRunResult:
        run_id = manifest.run_id
        unit_name = _unit_name(run_id)
        status = self._application.repository.get_run_status(run_id)
        if status.status == "owner_stopped":
            return self._existing_start_result(run_id)
        observed = self._application.units.get(unit_name)
        if observed is not None and observed.name != unit_name:
            return StartRunResult(
                run_id=run_id,
                created=False,
                status="needs_attention",
                reason="unit identity is ambiguous",
            )
        if observed is not None and observed.is_active:
            return StartRunResult(
                run_id=run_id,
                created=False,
                status="running",
                restarted_from_run_id=manifest.restarted_from_run_id,
            )
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
        recovery_intent: RecoveryIntent | None = None,
    ) -> StartRunResult:
        run_id = validate_run_id(str(record["run_id"]))
        with self._startup_record_lock(run_id):
            return self._recover_resume_record_locked(
                self._read_record(run_id),
                prepared=prepared,
                created=created,
                recovery_intent=recovery_intent,
            )

    def _recover_resume_record_locked(
        self,
        record: Mapping[str, object],
        *,
        prepared: PreparedRun | None = None,
        created: bool = False,
        recovery_intent: RecoveryIntent | None = None,
    ) -> StartRunResult:
        """Finish only the manifest-only gap belonging to one resume record."""
        if record.get("operation") != "resume" or record.get("mode") != "resume":
            raise DaemonError("startup record is not a resumable continuation")
        run_id = validate_run_id(str(record["run_id"]))
        if record.get("state") == "unit_started":
            return self._existing_start_result(run_id)
        source_run_id = validate_run_id(str(record["resumed_from_run_id"]))
        recovery_request = _recovery_request_from_record(record)
        source: RunStatus | None = None
        if recovery_request is None:
            _validate_persisted_recovery_runtime(
                self._application.repository.run_directory(source_run_id),
                expected_run_id=source_run_id,
                allow_pending=False,
                missing_ok=True,
            )
        else:
            source = self._application.repository.get_run_status(source_run_id)
            self._admit_recovery_source(source_run_id, source)
            self._refresh_workflow_config()
            self._validate_recovery_target(recovery_request)
        resume_extra, resume_extra_provided = self._resume_extra_override_for_record(
            record
        )
        if prepared is None:
            bootstrap = self._resume_bootstrap(
                source_run_id,
                extra_instructions=resume_extra,
                extra_instructions_provided=resume_extra_provided,
            )
            prepared = PreparedRun(
                workflow_name=bootstrap.workflow_name,
                repo_root=self._config.repo_root,
                plan_path=bootstrap.plan_path,
                config_path=_bootstrap_config_path(bootstrap, self._config),
                max_turns=bootstrap.max_turns,
                team=bootstrap.team,
                extra_instructions=bootstrap.extra_instructions,
                start_step=(
                    bootstrap.start_step
                    or _bootstrap_workflow_config(bootstrap, self).workflows[
                        bootstrap.workflow_name
                    ].first_step
                    or bootstrap.workflow_name
                ),
                reserved_run_id=run_id,
                idempotency_key=str(record["effective_idempotency_key"]),
                caller_scope=str(record["caller_scope"]),
                team_explicit=getattr(bootstrap, "team_explicit", None),
                max_turns_explicit=getattr(bootstrap, "max_turns_explicit", None),
                start_step_explicit=True,
            )
        else:
            bootstrap = self._resume_bootstrap(
                validate_run_id(str(record["resumed_from_run_id"])),
                extra_instructions=resume_extra,
                extra_instructions_provided=resume_extra_provided,
            )
        if recovery_request is not None:
            if source is None:
                raise DaemonError("recovery source state disappeared during replay")
            if recovery_intent is None:
                source_manifest = self._application.repository.get_launch_manifest(
                    source_run_id
                )
                if source_manifest is None:
                    raise DaemonError(
                        "source run has no control-plane launch manifest"
                    )
                recovery_intent = self._build_recovery_intent(
                    source_run_id=source_run_id,
                    target_run_id=run_id,
                    source=source,
                    source_manifest=source_manifest,
                    bootstrap=bootstrap,
                    target_request=recovery_request,
                )
            expected_intent_digest = record.get("recovery_intent_digest")
            if expected_intent_digest is not None and (
                expected_intent_digest != recovery_intent_digest(recovery_intent)
            ):
                raise DaemonError(
                    "recovery record no longer matches its immutable evidence intent"
                )
        self._transient_extra_instructions[run_id] = prepared.extra_instructions
        manifest = self._manifest_for(
            run_id=run_id,
            prepared=prepared,
            caller_scope=str(record["caller_scope"]),
            idempotency_key=str(record["effective_idempotency_key"]),
            workflow_config=_bootstrap_workflow_config(bootstrap, self),
        )
        persisted_manifest = self._application.repository.get_launch_manifest(run_id)
        if persisted_manifest is None:
            try:
                create_launch_manifest(self._config.repo_root, manifest)
            except (ValueError, RunIdentityConflict) as exc:
                raise DaemonError(
                    f"cannot reserve continuation launch intent: {exc}"
                ) from exc
            persisted_manifest = self._application.repository.get_launch_manifest(run_id)
            if persisted_manifest is None:
                raise DaemonError("continuation manifest disappeared during replay")
            # A crash can leave the successor record without its manifest. The
            # current source is authoritative when rebuilding that optional
            # launch artifact, so align the record with the newly published
            # digest before replaying the worker.
            mutable_record = dict(record)
            mutable_record["manifest_request_digest"] = (
                persisted_manifest.request_digest
                or normalized_request_digest(manifest)
            )
            self._write_record(mutable_record)
            record = mutable_record
        else:
            if record.get("manifest_request_digest") != persisted_manifest.request_digest:
                raise DaemonError(
                    "resume record does not match its immutable continuation intent"
                )
        self._assert_manifest_accepts_prepared(persisted_manifest, record, prepared)
        mutable = dict(record)
        if recovery_request is not None:
            mutable = self._persist_recovery_provenance(
                mutable,
                recovery_intent,
            )
        elif not mutable.get("source_audited"):
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

    def _admit_recovery_source(
        self,
        source_run_id: str,
        source: RunStatus,
    ) -> None:
        """Fail closed unless durable evidence proves a recoverable source."""
        if source.ownership != "control_plane":
            _reject_recovery("recovery_source_state")
        if source.status not in {"failed", "interrupted", "owner_stopped"}:
            _reject_recovery("recovery_source_state")
        worker = source.evidence.get("worker")
        if not isinstance(worker, Mapping) or not confirmed_inactive(worker):
            _reject_recovery("recovery_source_activity")

        unit_name = _unit_name(source_run_id)
        observed = self._application.units.get(unit_name)
        if observed is not None and observed.name != unit_name:
            _reject_recovery("recovery_source_activity")
        if observed is not None and observed.is_active:
            _reject_recovery("recovery_source_activity")
        if source.evidence.get("unit_observation") in {
            "identity_mismatch",
            "unavailable",
        }:
            _reject_recovery("recovery_source_activity")

        if source.status == "owner_stopped":
            if source.launch_phase != "owner_stopped":
                _reject_recovery("recovery_source_state")
        else:
            if source.launch_phase in {None, "manifest_only", "launch_requested"}:
                _reject_recovery("recovery_source_state")
            if (
                source.launch_phase not in {"failed", "interrupted"}
                and source.evidence.get("controller_terminal") is not True
            ):
                _reject_recovery("recovery_source_state")
            if (
                source.launch_phase == "launch_started"
                and source.evidence.get("controller_terminal") is not True
            ):
                _reject_recovery("recovery_source_state")

        try:
            source_dir = self._application.repository.run_directory(source_run_id)
        except (ValueError, OSError) as exc:
            raise DurableRecoveryRejection("recovery_evidence_unavailable") from exc
        _validate_persisted_recovery_runtime(
            source_dir,
            expected_run_id=source_run_id,
            allow_pending=False,
            missing_ok=False,
        )
        if source.status == "owner_stopped" and not any(
            event.event_type == "owner_stopped"
            for event in read_events(source_dir, limit=1_000)
        ):
            _reject_recovery("recovery_source_state")

    def _validate_recovery_target(self, request: RecoveryRequest) -> None:
        """Validate only the explicitly selected current target profile."""
        harness, separator, _profile = request.worker_selector.partition(".")
        if not separator or harness not in ADAPTERS:
            _reject_recovery("recovery_target_invalid")
        from aflow.workflow import WorkflowError, resolve_profile

        try:
            resolve_profile(
                request.worker_selector,
                self._workflow_config,
                step_path="resume.recovery.worker",
            )
        except (WorkflowError, KeyError, TypeError, ValueError) as exc:
            raise DurableRecoveryRejection("recovery_target_invalid") from exc

    def _build_recovery_intent(
        self,
        *,
        source_run_id: str,
        target_run_id: str,
        source: RunStatus,
        source_manifest: LaunchManifest,
        bootstrap: Any,
        target_request: RecoveryRequest | None,
    ) -> RecoveryIntent:
        """Bind exact source evidence without consulting a provider."""
        if target_request is None:
            _reject_recovery("recovery_target_invalid")

        from aflow.run_state import ResumeContext
        from aflow.workflow import WorkflowError, _validate_branch_resume_context, _validate_worktree_resume_context

        context = getattr(bootstrap, "resume_context", None)
        if not isinstance(context, ResumeContext):
            _reject_recovery("recovery_evidence_unavailable")

        repo_root = self._config.repo_root.resolve()
        if Path(source_manifest.project_root).resolve() != repo_root:
            _reject_recovery("recovery_evidence_unavailable")
        if source_manifest.intended_unit not in {None, _unit_name(source_run_id)}:
            _reject_recovery("recovery_evidence_unavailable")
        source_dir = self._application.repository.run_directory(source_run_id)
        evidence: list[RecoveryEvidenceReference] = []
        evidence_file_paths: list[Path] = []

        def add_file(
            path: Path,
            *,
            expected: bytes | None = None,
        ) -> None:
            requested = Path(path)
            if requested.is_symlink():
                _reject_recovery("recovery_evidence_unavailable")
            try:
                resolved = requested.resolve(strict=True)
                resolved.relative_to(repo_root)
                data = resolved.read_bytes()
            except (OSError, ValueError) as exc:
                raise DurableRecoveryRejection("recovery_evidence_unavailable") from exc
            if not resolved.is_file():
                _reject_recovery("recovery_evidence_unavailable")
            if expected is not None and data != expected:
                _reject_recovery("recovery_evidence_unavailable")
            relative = resolved.relative_to(repo_root).as_posix()
            reference = RecoveryEvidenceReference(
                path=relative,
                sha256=hashlib.sha256(data).hexdigest(),
                size=len(data),
            )
            for existing in evidence:
                if existing.path == reference.path:
                    if existing != reference:
                        _reject_recovery("recovery_evidence_unavailable")
                    return
            evidence.append(reference)
            evidence_file_paths.append(resolved)

        source_run_json = source_dir / "run.json"
        add_file(source_run_json)
        add_file(
            self._config.repo_root
            / ".aflow"
            / "launches"
            / f"{validate_run_id(source_run_id)}.json"
        )

        plan_path = Path(getattr(bootstrap, "plan_path", ""))
        add_file(plan_path)
        active_plan_path = context.active_plan_path
        if active_plan_path is not None:
            add_file(Path(active_plan_path))

        setup = tuple(context.setup)
        if any(item not in {"branch", "worktree"} for item in setup):
            _reject_recovery("recovery_evidence_unavailable")
        if "worktree" in setup:
            try:
                _validate_worktree_resume_context(repo_root, context)
            except (WorkflowError, OSError, ValueError) as exc:
                raise DurableRecoveryRejection("recovery_evidence_unavailable") from exc
            execution_root = context.worktree_path
        elif "branch" in setup:
            try:
                _validate_branch_resume_context(repo_root, context)
            except (WorkflowError, OSError, ValueError) as exc:
                raise DurableRecoveryRejection("recovery_evidence_unavailable") from exc
            execution_root = repo_root
        else:
            execution_root = repo_root
        if execution_root is None or not Path(execution_root).is_dir():
            _reject_recovery("recovery_evidence_unavailable")

        for transaction in (
            context.current_hotplug_transaction,
            context.pending_hotplug_transaction,
        ):
            if transaction is not None and transaction.stage not in {"applied", "failed"}:
                _reject_recovery("recovery_operation_unresolved")

        scope = context.active_implementation_scope
        if scope is not None:
            envelope_bytes = context.scope_envelope_bytes
            envelope_path = context.scope_envelope_source_path
            if not isinstance(envelope_bytes, bytes) or not envelope_bytes:
                _reject_recovery("recovery_evidence_unavailable")
            if not isinstance(envelope_path, str) or not envelope_path.strip():
                _reject_recovery("recovery_evidence_unavailable")
            try:
                Path(envelope_path).resolve(strict=True).relative_to(source_dir.resolve())
            except (OSError, ValueError) as exc:
                raise DurableRecoveryRejection("recovery_evidence_unavailable") from exc
            add_file(Path(envelope_path), expected=envelope_bytes)
            for relative_path, artifact_bytes in context.scope_evidence_artifact_bytes.items():
                if not isinstance(relative_path, str) or not isinstance(artifact_bytes, bytes):
                    _reject_recovery("recovery_evidence_unavailable")
                candidate = (source_dir / relative_path).resolve()
                try:
                    candidate.relative_to(source_dir.resolve())
                except ValueError as exc:
                    raise DurableRecoveryRejection("recovery_evidence_unavailable") from exc
                add_file(candidate, expected=artifact_bytes)
            for relative_path, artifact_bytes in context.repartition_artifact_bytes.items():
                if not isinstance(relative_path, str) or not isinstance(artifact_bytes, bytes):
                    _reject_recovery("recovery_evidence_unavailable")
                candidate = (source_dir / relative_path).resolve()
                try:
                    candidate.relative_to(source_dir.resolve())
                except ValueError as exc:
                    raise DurableRecoveryRejection("recovery_evidence_unavailable") from exc
                add_file(candidate, expected=artifact_bytes)

        fingerprint = workspace_fingerprint(
            Path(execution_root),
            _recovery_fingerprint_plan_paths(evidence_file_paths),
        )
        fingerprint_digest = fingerprint.get("sha256")
        if not isinstance(fingerprint_digest, str) or len(fingerprint_digest) != 64:
            _reject_recovery("recovery_evidence_unavailable")
        workspace_path = Path(execution_root).resolve()
        evidence.append(
            RecoveryEvidenceReference(
                path=f"worktree:{workspace_path}",
                sha256=fingerprint_digest,
                kind="workspace",
            )
        )

        source_selector, selector_path = _source_worker_selector(
            source=source,
            run_json_path=source_run_json,
            # Read the source authority directly.  A bootstrap object is
            # useful for continuation state, but must not be able to smuggle
            # selector evidence from another run into this artifact.
            run_json=None,
            context=context,
            source_dir=source_dir,
        )
        if "." not in source_selector:
            _reject_recovery("recovery_evidence_unavailable")
        if selector_path is not None:
            add_file(selector_path)

        try:
            return RecoveryIntent(
                source_run_id=validate_run_id(source_run_id),
                target_run_id=validate_run_id(target_run_id),
                source_selector=source_selector,
                target_selector=target_request.worker_selector,
                evidence=tuple(evidence),
                source_session_context_transferred=False,
            )
        except RecoveryValidationError as exc:
            raise DurableRecoveryRejection("recovery_evidence_unavailable") from exc

    def _persist_recovery_provenance(
        self,
        record: Mapping[str, object],
        intent: RecoveryIntent | None,
    ) -> dict[str, object]:
        """Publish successor artifact and event exactly once."""
        if intent is None:
            raise DaemonError("recovery provenance is missing its intent")
        run_id = validate_run_id(str(record["run_id"]))
        run_dir = self._application.repository.run_directory(run_id)
        try:
            artifact_path, artifact_sha256 = persist_recovery_intent(run_dir, intent)
        except RecoveryPersistenceError as exc:
            raise DaemonError(str(exc)) from exc

        mutable = dict(record)
        for field_name, expected in (
            ("recovery_artifact_path", artifact_path),
            ("recovery_artifact_sha256", artifact_sha256),
        ):
            existing = mutable.get(field_name)
            if existing is not None and existing != expected:
                raise DaemonError("recovery record does not match its durable artifact")
            mutable[field_name] = expected

        event_payload = {
            "schema_version": intent.schema_version,
            "mode": intent.mode,
            "source_run_id": intent.source_run_id,
            "target_run_id": intent.target_run_id,
            "source_selector": intent.source_selector,
            "target_selector": intent.target_selector,
            "artifact_path": artifact_path,
            "artifact_sha256": artifact_sha256,
            "evidence": [item.to_dict() for item in intent.evidence],
            "source_session_context_transferred": False,
        }
        existing_events = read_events(run_dir, limit=1_000)
        recovery_events = [
            event for event in existing_events if event.event_type == "recovery_requested"
        ]
        if recovery_events:
            if any(event.data != event_payload for event in recovery_events):
                raise DaemonError(
                    "successor recovery event does not match its durable intent"
                )
        else:
            append_run_event(run_dir, "recovery_requested", event_payload)
        mutable["recovery_event_recorded"] = True
        self._write_record(mutable)
        return mutable

    def _initial_manifest_for(
        self,
        *,
        run_id: str,
        request: StartupRequest,
        caller_scope: str,
        idempotency_key: str,
    ) -> LaunchManifest:
        """Build request-level launch intent before plan-sensitive preparation.

        ``start_step`` deliberately remains ``None`` when the caller has not
        selected one.  A later persisted startup answer may choose a step, but
        it may never mutate this request-level launch intent.
        """
        workflow_name = (
            request.workflow_name or self._workflow_config.aflow.default_workflow
        )
        if (
            workflow_name is None
            or workflow_name not in self._workflow_config.workflows
        ):
            raise DaemonError("startup request does not name a configured workflow")
        workflow = self._workflow_config.workflows[workflow_name]
        start_step = _resolve_configured_start_step(
            request.start_step,
            workflow_name,
            workflow.steps,
            excluded_steps=workflow.excluded_steps,
        )
        selected_start_step = start_step or workflow.first_step
        executable_steps = tuple(workflow.steps)
        skipped_steps = (
            executable_steps[: executable_steps.index(selected_start_step)]
            if selected_start_step in executable_steps
            else ()
        )
        self._validate_restart_source(
            request.restarted_from_run_id,
            successor_run_id=run_id,
            caller_scope=caller_scope,
            successor_key=idempotency_key,
        )
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
            extra_instructions=request.extra_instructions,
            idempotency_key=idempotency_key,
            caller_scope=caller_scope,
            frozen_config_fingerprint=frozen.config_fingerprint,
            restarted_from_run_id=request.restarted_from_run_id,
            skipped_steps=skipped_steps,
        )

    def _validate_restart_source(
        self,
        source_run_id: str | None,
        *,
        successor_run_id: str,
        caller_scope: str,
        successor_key: str | None = None,
    ) -> None:
        if source_run_id is None:
            return
        source_run_id = validate_run_id(source_run_id)
        if source_run_id == successor_run_id:
            raise DaemonError("a successor run cannot restart itself")
        source = self._application.repository.get_run_status(source_run_id)
        worker = source.evidence.get("worker")
        if isinstance(worker, Mapping) and not confirmed_inactive(worker):
            raise DaemonError("restart predecessor worker activity could not be confirmed inactive")
        if source.ownership != "control_plane":
            raise DaemonError("legacy runs cannot be restart predecessors")
        manifest = self._application.repository.get_launch_manifest(source_run_id)
        if manifest is None:
            raise DaemonError("restart predecessor has no launch manifest")
        try:
            same_project = (
                Path(manifest.project_root).resolve() == self._config.repo_root
            )
        except (OSError, RuntimeError):
            same_project = False
        if not same_project:
            raise DaemonError("restart predecessor belongs to a different project")
        self._assert_manifest_caller(source_run_id, caller_scope)
        failure = source.evidence.get("startup_failure")
        confirmed_preparation_failure = (
            source.status == "needs_attention"
            and isinstance(failure, Mapping)
            and failure.get("stage") == "preparation"
            and source.evidence.get("no_agent_started") is True
        )
        failed_execution = source.status in {"failed", "interrupted"}
        owner_stopped = source.status == "owner_stopped" and source.launch_phase == "owner_stopped"
        if not (owner_stopped or failed_execution or confirmed_preparation_failure):
            raise DaemonError(
                "restart predecessor requires a confirmed failure or explicit owner stop"
            )
        source_events = self._application.repository.tail_events(
            source_run_id,
            limit=1_000,
        )
        if owner_stopped and not any(event.event_type == "owner_stopped" for event in source_events):
            raise DaemonError(
                "restart predecessor lacks explicit owner-stop evidence"
            )
        expected_unit = _unit_name(source_run_id)
        if manifest.intended_unit != expected_unit:
            raise DaemonError("restart predecessor unit identity is ambiguous")
        observed = self._application.units.get(expected_unit)
        if observed is not None and observed.name != expected_unit:
            raise DaemonError("restart predecessor unit identity is ambiguous")
        if observed is not None and observed.is_active:
            raise DaemonError("an active run cannot be a restart predecessor")
        ancestor = manifest
        seen = {successor_run_id}
        while ancestor is not None:
            if ancestor.run_id in seen:
                raise DaemonError("restart predecessor lineage is cyclic")
            seen.add(ancestor.run_id)
            if Path(ancestor.project_root).resolve() != self._config.repo_root:
                raise DaemonError("restart predecessor lineage belongs to a different project")
            self._assert_manifest_caller(ancestor.run_id, caller_scope)
            if ancestor.restarted_from_run_id is None:
                break
            ancestor = self._application.repository.get_launch_manifest(
                ancestor.restarted_from_run_id
            )
            if ancestor is None:
                raise DaemonError("restart predecessor lineage is incomplete")
        cursor = None
        while True:
            page = self._application.repository.list_runs(limit=100, cursor=cursor)
            for sibling in page.runs:
                if sibling.restarted_from_run_id != source_run_id or sibling.run_id == successor_run_id:
                    continue
                sibling_manifest = self._application.repository.get_launch_manifest(sibling.run_id)
                if successor_key and sibling_manifest and sibling_manifest.idempotency_key == successor_key and _equivalent_caller_scope(sibling_manifest.caller_scope, caller_scope):
                    continue  # Exact replay is verified against its request digest by start().
                unit = self._application.units.get(_unit_name(sibling.run_id))
                if unit is not None and unit.is_active:
                    raise DaemonError("restart predecessor already has an active successor")
                if sibling.status not in {"failed", "interrupted", "completed", "owner_stopped"} and not sibling.evidence.get("startup_failure"):
                    raise DaemonError("restart predecessor already has an unresolved successor")
            cursor = page.next_cursor
            if cursor is None:
                break

    def restart_options(self, run_id: str, *, caller_scope: str) -> dict[str, object]:
        """Bounded read-only admission and public launch fields for a new attempt."""
        run_id = validate_run_id(run_id)
        self._assert_manifest_caller(run_id, caller_scope)
        source = self._application.repository.get_run_status(run_id)
        manifest = self._application.repository.get_launch_manifest(run_id)
        if manifest is None:
            raise DaemonError("restart predecessor has no launch manifest")
        if Path(manifest.project_root).resolve() != self._config.repo_root:
            raise DaemonError("restart predecessor belongs to a different project")
        eligible = True
        reason = None
        try:
            self._validate_restart_source(run_id, successor_run_id="restart-preview", caller_scope=caller_scope)
        except DaemonError as exc:
            eligible, reason = False, str(exc)[:300]
        requires_stop = source.status in {"running", "paused", "waiting_for_input", "waiting_for_valid_override"}
        extra = self._transient_extra_instructions.get(run_id, manifest.extra_instructions)
        missing_extra = False
        try:
            record = self._read_record(run_id)
            request = record.get("request", {})
            digest = request.get("extra_instructions_digest") if isinstance(request, Mapping) else None
            if digest not in {None, _extra_instructions_digest(extra)}:
                try:
                    extra = self._resume_bootstrap(run_id).extra_instructions
                except Exception:
                    missing_extra = True
        except DaemonError:
            pass
        _validate_extra_instructions(extra)
        return {
            "eligible": eligible,
            "reason": reason,
            "requires_stop": requires_stop,
            "run_id": run_id,
            "extra_instructions_unavailable": missing_extra,
            "options": {
                "plan_path": str(Path(manifest.plan_path).resolve().relative_to(self._config.repo_root)),
                "workflow_name": manifest.workflow_name,
                "team": manifest.team,
                "max_turns": manifest.max_turns,
                "start_step": source.selected_start_step,
                "extra_instructions": list(extra),
            },
        }

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
            raise DaemonIdempotencyConflict(
                "durable start request does not match its launch manifest"
            )

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
            or (
                prepared.max_turns_explicit is not False
                and manifest.max_turns != prepared.max_turns
            )
            or (
                prepared.team_explicit is not False
                and manifest.team != prepared.team
            )
            or manifest.idempotency_key != record.get("effective_idempotency_key")
            or manifest.caller_scope != record.get("caller_scope")
            or manifest.intended_unit != _unit_name(manifest.run_id)
            or manifest.restarted_from_run_id != prepared.restarted_from_run_id
            or (
                manifest.start_step is not None
                and prepared.start_step_explicit is not False
                and manifest.skipped_steps != prepared.skipped_steps
            )
        ):
            raise DaemonError(
                "prepared startup state does not match immutable launch intent"
            )
        if (
            manifest.start_step is not None
            and prepared.start_step_explicit is not False
            and manifest.start_step != prepared.start_step
        ):
            raise DaemonError(
                "prepared startup step does not match immutable launch intent"
            )
    def _assert_record_runtime_identity(self, record: Mapping[str, object]) -> None:
        if (
            record.get("selected_executable") != str(self._config.aflow_executable)
            or record.get("selected_release_identity") != self._config.release_identity
            or record.get("selected_environment_file")
            != _file_identity(self._config.environment_file)
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
        workflow_config: WorkflowUserConfig | None = None,
    ) -> LaunchManifest:
        from aflow.workflow import _freeze_run_identity

        frozen = _freeze_run_identity(
            prepared.workflow_name,
            workflow_config if workflow_config is not None else self._workflow_config,
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
            restarted_from_run_id=prepared.restarted_from_run_id,
            skipped_steps=prepared.skipped_steps,
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
        request: StartupRequest,
        candidate: LaunchManifest,
    ) -> LaunchManifest | None:
        if idempotency_key is None:
            return None
        cursor: str | None = None
        while True:
            page = self._application.repository.list_runs(limit=1_000, cursor=cursor)
            for status in page.runs:
                if status.ownership != "control_plane":
                    continue
                manifest = self._application.repository.get_launch_manifest(
                    status.run_id
                )
                if (
                    manifest is None
                    or manifest.idempotency_key != idempotency_key
                    or not _equivalent_caller_scope(manifest.caller_scope, caller_scope)
                ):
                    continue
                if not self._manifest_matches_start_request(
                    manifest,
                    request=request,
                    candidate=candidate,
                ):
                    raise DaemonIdempotencyConflict(
                        "start idempotency key was reused for a different request"
                    )
                try:
                    record = self._read_record(manifest.run_id)
                except DaemonError:
                    record = None
                if isinstance(record, Mapping):
                    stored_request = record.get("request")
                    if isinstance(stored_request, Mapping):
                        stored_confirmation = stored_request.get(
                            "dirty_worktree_confirmed", False
                        )
                        if stored_confirmation != request.dirty_worktree_confirmed:
                            raise DaemonIdempotencyConflict(
                                "start idempotency key was reused for a different request"
                            )
                return manifest
            if page.next_cursor is None:
                return None
            cursor = page.next_cursor

    def _manifest_matches_start_request(
        self,
        manifest: LaunchManifest,
        *,
        request: StartupRequest,
        candidate: LaunchManifest,
    ) -> bool:
        """Match durable start intent without treating live defaults as identity."""
        if (
            manifest.project_root != candidate.project_root
            or manifest.plan_path != candidate.plan_path
            or manifest.workflow_name != candidate.workflow_name
            or manifest.idempotency_key != candidate.idempotency_key
            or manifest.caller_scope != candidate.caller_scope
            or manifest.restarted_from_run_id != candidate.restarted_from_run_id
            or manifest.intended_unit != _unit_name(manifest.run_id)
        ):
            return False

        max_turns_explicit = (
            request.max_turns_explicit
            if request.max_turns_explicit is not None
            else request.max_turns is not None
        )
        team_explicit = (
            request.team_explicit
            if request.team_explicit is not None
            else request.team is not None
        )
        start_step_explicit = (
            request.start_step_explicit
            if request.start_step_explicit is not None
            else request.start_step is not None
        )
        if max_turns_explicit and manifest.max_turns != candidate.max_turns:
            return False
        if team_explicit and manifest.team != candidate.team:
            return False
        if start_step_explicit:
            if (
                manifest.start_step != candidate.start_step
                or manifest.skipped_steps != candidate.skipped_steps
            ):
                return False
        elif manifest.start_step is not None:
            # The request-level manifest deliberately leaves an omitted
            # start-step unset; a populated value therefore represents a
            # different explicit request.
            return False

        # The persisted digest still protects prompt-like request intent. Use
        # the old values for omitted defaults and the old diagnostic
        # fingerprint only to reproduce the historical digest; neither value
        # authorizes loading the historical configuration.
        digest_candidate = replace(
            candidate,
            max_turns=(candidate.max_turns if max_turns_explicit else manifest.max_turns),
            team=candidate.team if team_explicit else manifest.team,
            start_step=(candidate.start_step if start_step_explicit else manifest.start_step),
            skipped_steps=(
                candidate.skipped_steps
                if start_step_explicit
                else manifest.skipped_steps
            ),
            frozen_config_fingerprint=manifest.frozen_config_fingerprint,
        )
        return manifest.request_digest == normalized_request_digest(digest_candidate)

    def _pending_response(
        self, record: Mapping[str, object]
    ) -> StartRunResult | StartupQuestionRecord:
        run_id = validate_run_id(str(record["run_id"]))
        with self._startup_record_lock(run_id):
            return self._pending_response_locked(self._read_record(run_id))

    def _pending_response_locked(
        self,
        record: Mapping[str, object],
    ) -> StartRunResult | StartupQuestionRecord:
        run_id = validate_run_id(str(record["run_id"]))
        if (
            self._application.repository.get_run_status(run_id).status
            == "owner_stopped"
        ):
            return self._existing_start_result(run_id)
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
            failure = record.get("startup_failure")
            if (
                state == "needs_attention"
                and isinstance(failure, Mapping)
                and failure.get("code") == PLAN_ADMISSION_ERROR_CODE
            ):
                raise DaemonStartupError(
                    run_id,
                    PlanAdmissionError.safe_message_for(failure.get("kind")),
                    code=PLAN_ADMISSION_ERROR_CODE,
                )
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
            extra_instructions=self._transient_extra_instructions.get(run_id, ()),
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
        manifest = self._application.repository.get_launch_manifest(run_id)
        if manifest is None:
            return StartRunResult(
                run_id=run_id,
                created=False,
                status="needs_attention",
                reason="durable startup record has no immutable launch manifest",
            )
        status = self._application.repository.get_run_status(run_id)
        if status.status == "owner_stopped":
            return StartRunResult(
                run_id=run_id,
                created=False,
                status=status.status,
                reason=status.reason,
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
            return StartRunResult(
                run_id=run_id,
                created=False,
                status="running",
                restarted_from_run_id=manifest.restarted_from_run_id,
            )
        if status.status in {"completed", "failed", "interrupted", "owner_stopped"}:
            return StartRunResult(
                run_id=run_id,
                created=False,
                status=status.status,
                reason=status.reason,
                restarted_from_run_id=manifest.restarted_from_run_id,
            )
        return StartRunResult(
            run_id=run_id,
            created=False,
            status="needs_attention",
            reason="existing launch has no active unit or terminal controller evidence",
        )

    def _worker_argv(
        self,
        run_id: str,
        extra_instructions: tuple[str, ...] = (),
    ) -> tuple[str, ...]:
        instruction_args = tuple(
            argument
            for instruction in extra_instructions
            for argument in (f"--extra-instruction={instruction}",)
        )
        return (
            str(self._config.aflow_executable),
            "daemon-worker",
            "--repo-root",
            str(self._config.repo_root),
            "--config",
            str(self._config.config_path),
            "--run-id",
            run_id,
            *instruction_args,
        )

    def _resume_extra_override_for_record(
        self, record: Mapping[str, object]
    ) -> tuple[tuple[str, ...], bool]:
        provided = record.get("resume_extra_instructions_provided", False)
        if not isinstance(provided, bool):
            raise DaemonError(
                "resume record has an invalid extra-instructions override flag"
            )
        if not provided:
            return (), False
        run_id = validate_run_id(str(record["run_id"]))
        extra = self._transient_extra_instructions.get(run_id)
        if extra is not None:
            _validate_extra_instructions(extra)
            return extra, True
        if _record_extra_instructions_digest(record) == _extra_instructions_digest(()):
            return (), True
        raise DaemonError(
            "non-persistent extra instructions are unavailable; submit a new resume request"
        )

    def _resume_bootstrap(
        self,
        source_run_id: str,
        *,
        extra_instructions: tuple[str, ...] = (),
        extra_instructions_provided: bool = False,
    ):
        from aflow.cli import _bootstrap_resume_invocation

        return _bootstrap_resume_invocation(
            repo_root=self._config.repo_root,
            config_path=self._config.config_path,
            default_config_path=self._config.config_path,
            config_path_is_explicit=True,
            workflow_config=self._workflow_config,
            requested_run_id=source_run_id,
            workflow_arg=None,
            plan_file_arg=None,
            team_arg=None,
            start_step_arg=None,
            max_turns_arg=None,
            extra_instructions_arg=extra_instructions,
            extra_instructions_provided=extra_instructions_provided,
            live_loader=load_workflow_config,
        )

    def _assert_record_caller(
        self, record: Mapping[str, object], caller_scope: str
    ) -> None:
        if not isinstance(caller_scope, str) or not caller_scope.strip():
            raise DaemonAuthorizationError("caller scope must be non-empty")
        if not _equivalent_caller_scope(record.get("caller_scope"), caller_scope):
            raise DaemonAuthorizationError(
                "caller scope is not authorized for this startup request"
            )

    def _assert_manifest_caller(self, run_id: str, caller_scope: str) -> None:
        if not isinstance(caller_scope, str) or not caller_scope.strip():
            raise DaemonAuthorizationError("caller scope must be non-empty")
        manifest = self._application.repository.get_launch_manifest(run_id)
        if manifest is None or not _equivalent_caller_scope(
            manifest.caller_scope, caller_scope
        ):
            raise DaemonAuthorizationError(
                "caller scope is not authorized for this run"
            )

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
            return _contained_directory(
                self._config.repo_root, ".aflow", "start-requests"
            )
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
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
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
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != _START_RECORD_SCHEMA_VERSION
        ):
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
        self._refresh_workflow_config()
        request = _request_from_payload(
            payload,
            workflow_config=self._workflow_config,
            extra_instructions=self._transient_extra_instructions.get(
                validate_run_id(str(record["run_id"])), ()
            ),
            reserved_run_id=str(record["run_id"]),
            caller_scope=str(record["caller_scope"]),
            idempotency_key=(
                str(record["idempotency_key"])
                if record.get("idempotency_key") is not None
                else None
            ),
        )
        return replace(
            request,
            config_path=self._config.config_path,
            workflow_config=self._workflow_config,
        )


def worker_main(
    *,
    repo_root: Path,
    config_path: Path,
    run_id: str,
    extra_instructions: tuple[str, ...] = (),
) -> int:
    """Run a single daemon-prepared controller through the installed ``aflow`` entry point."""
    stage = "configuration"
    try:
        root = Path(repo_root).resolve()
        config = Path(config_path).resolve()
        selected_run_id = validate_run_id(run_id)
        _validate_extra_instructions(extra_instructions)
        workflow_config = load_live_config(
            config,
            loader=load_workflow_config,
        ).workflow_config
        stage = "live_configuration_manifest"
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
        stage = "prepared_request"
        prepared, resume = _worker_prepared(
            record,
            manifest,
            root,
            config,
            workflow_config,
            extra_instructions=extra_instructions,
        )
        stage = "live_configuration_validation"
        stage = "controller_entry"
        execute_workflow(
            prepared,
            resume=resume,
            allow_existing_launch_manifest=True,
        )
    except Exception as exc:
        from aflow.control_plane.models import startup_failure
        from aflow.control_plane.persistent_units import _receipts_for, _write_receipt

        failure = startup_failure(stage, str(exc))
        try:
            receipts = _receipts_for(_unit_name(validate_run_id(run_id)), Path(repo_root).resolve())
            if receipts is not None and receipts.nonce == os.environ.get("AFLOW_WORKER_NONCE"):
                _write_receipt(receipts.directory / "worker-error.json", {"schema": 1, "nonce": receipts.nonce, **failure}, exclusive=False)
        except (OSError, ValueError):
            print("aflow daemon worker: diagnostic persistence failed", file=sys.stderr)
        print(f"aflow daemon worker: {failure['message']}", file=sys.stderr)
        return 1
    return 0


def _recovery_file_reference_path(repo_root: Path, reference: RecoveryEvidenceReference) -> Path:
    """Resolve one successor evidence reference without following unsafe paths."""
    relative = Path(reference.path)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        _reject_recovery("recovery_evidence_unavailable")
    if any(part in {"", "."} for part in relative.parts):
        _reject_recovery("recovery_evidence_unavailable")
    candidate = repo_root / relative
    if candidate.is_symlink():
        _reject_recovery("recovery_evidence_unavailable")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(repo_root.resolve())
        data = resolved.read_bytes()
    except (OSError, ValueError) as exc:
        raise DurableRecoveryRejection("recovery_evidence_unavailable") from exc
    if not resolved.is_file():
        _reject_recovery("recovery_evidence_unavailable")
    if len(data) != reference.size or hashlib.sha256(data).hexdigest() != reference.sha256:
        _reject_recovery("recovery_evidence_unavailable")
    return resolved


def _validate_recovery_evidence_for_worker(
    repo_root: Path,
    intent: RecoveryIntent,
) -> tuple[tuple[Path, ...], dict[str, object]]:
    """Recheck the immutable evidence immediately before target launch."""
    file_paths: list[Path] = []
    workspace_reference: RecoveryEvidenceReference | None = None
    for reference in intent.evidence:
        if reference.kind == "file":
            file_paths.append(_recovery_file_reference_path(repo_root, reference))
        elif workspace_reference is None:
            workspace_reference = reference
        else:
            _reject_recovery("recovery_evidence_unavailable")
    if workspace_reference is None or not workspace_reference.path.startswith("worktree:"):
        _reject_recovery("recovery_evidence_unavailable")
    workspace_text = workspace_reference.path.removeprefix("worktree:")
    if not workspace_text.strip():
        _reject_recovery("recovery_evidence_unavailable")
    workspace = Path(workspace_text)
    if workspace.is_symlink() or not workspace.is_dir():
        _reject_recovery("recovery_evidence_unavailable")
    plan_paths = _recovery_fingerprint_plan_paths(file_paths)
    observed = workspace_fingerprint(workspace, plan_paths)
    if observed.get("sha256") != workspace_reference.sha256:
        _reject_recovery("recovery_evidence_unavailable")
    if workspace_reference.size != 0:
        _reject_recovery("recovery_evidence_unavailable")
    return tuple(file_paths), {
        "workspace": workspace,
        "workspace_fingerprint": observed,
    }


def _read_recovery_target_runtime(
    target_dir: Path,
    *,
    intent: RecoveryIntent,
    intent_digest: str,
) -> tuple[dict[str, object] | None, str, bool]:
    """Read target operation state and reject an ambiguous prior invocation."""

    def reject_unmarked_turns() -> None:
        turn_root = target_dir / "turns"
        if turn_root.is_symlink():
            _reject_recovery("recovery_evidence_unavailable")
        if not turn_root.exists():
            return
        if not turn_root.is_dir():
            _reject_recovery("recovery_evidence_unavailable")
        for turn_path in sorted(turn_root.glob("turn-*")):
            if turn_path.is_symlink() or not turn_path.is_dir():
                _reject_recovery("recovery_evidence_unavailable")
            _reject_recovery("recovery_operation_unresolved")

    run_json_path = target_dir / "run.json"
    if run_json_path.is_symlink():
        _reject_recovery("recovery_evidence_unavailable")
    if not run_json_path.exists():
        reject_unmarked_turns()
        return None, "pending", False
    if not run_json_path.is_file():
        _reject_recovery("recovery_evidence_unavailable")
    try:
        payload = json.loads(run_json_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise DurableRecoveryRejection("recovery_evidence_unavailable") from exc
    if not isinstance(payload, Mapping):
        _reject_recovery("recovery_evidence_unavailable")
    raw_runtime = payload.get("recovery_runtime")
    if raw_runtime is None:
        reject_unmarked_turns()
        return dict(payload), "pending", False
    if not isinstance(raw_runtime, Mapping):
        _reject_recovery("recovery_operation_unresolved")
    operation_state, consumed = _validate_recovery_runtime_shape(
        raw_runtime,
        expected_intent=intent,
        expected_intent_digest=intent_digest,
    )
    if operation_state == "pending":
        # The marker is written immediately before the provider call. A turn
        # directory alongside a pending marker means that boundary was not
        # durably reconciled, so launching again could duplicate the target.
        reject_unmarked_turns()
    return dict(payload), operation_state, consumed


def _validate_persisted_recovery_runtime(
    run_dir: Path,
    *,
    expected_run_id: str,
    allow_pending: bool,
    missing_ok: bool,
) -> tuple[str, bool] | None:
    """Validate one source marker without requiring recovery request metadata."""
    if run_dir.is_symlink():
        _reject_recovery("recovery_evidence_unavailable")
    if not run_dir.exists():
        if missing_ok:
            return None
        _reject_recovery("recovery_evidence_unavailable")
    if not run_dir.is_dir():
        _reject_recovery("recovery_evidence_unavailable")
    run_json = run_dir / "run.json"
    if run_json.is_symlink():
        _reject_recovery("recovery_evidence_unavailable")
    if not run_json.exists():
        if missing_ok:
            return None
        _reject_recovery("recovery_evidence_unavailable")
    if not run_json.is_file():
        _reject_recovery("recovery_evidence_unavailable")
    try:
        payload = json.loads(run_json.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise DurableRecoveryRejection("recovery_evidence_unavailable") from exc
    if not isinstance(payload, Mapping):
        _reject_recovery("recovery_evidence_unavailable")
    raw_runtime = payload.get("recovery_runtime")
    if raw_runtime is None:
        return None
    return _validate_recovery_runtime_shape(
        raw_runtime,
        expected_target_run_id=expected_run_id,
        allow_pending=allow_pending,
    )


def _recovery_structural_turn(source_dir: Path) -> tuple[dict[str, object] | None, Path | None]:
    """Return only routing facts from the latest finalized source turn."""
    turns_root = source_dir / "turns"
    if turns_root.is_symlink() or not turns_root.is_dir():
        return None, None
    for turn_dir in reversed(sorted(turns_root.glob("turn-*"))):
        if turn_dir.is_symlink() or not turn_dir.is_dir():
            continue
        result_path = turn_dir / "result.json"
        if result_path.is_symlink() or not result_path.is_file():
            continue
        try:
            raw = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            continue
        if not isinstance(raw, Mapping) or raw.get("status") in {"starting", "running"}:
            continue
        keys = (
            "turn_number",
            "step_name",
            "step_role",
            "selector",
            "status",
            "returncode",
        )
        return (
            {key: raw[key] for key in keys if key in raw},
            result_path,
        )
    return None, None


def _build_durable_recovery_brief(
    *,
    repo_root: Path,
    source_dir: Path,
    source_run_json: Mapping[str, object],
    intent: RecoveryIntent,
    context: Any,
    plan_path: Path,
    evidence_paths: tuple[Path, ...],
    workspace_evidence: Mapping[str, object],
) -> str:
    """Build a compact, reference-only prompt for the replacement worker."""
    from aflow.control_plane.models import bounded_redacted
    from aflow.manager_context import build_manager_context
    from aflow.plan import load_plan_tolerant

    root = repo_root.resolve()

    def compact(value: object, *, byte_limit: int = 4096) -> str:
        encoded = json.dumps(
            bounded_redacted(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        if len(encoded.encode("utf-8")) <= byte_limit:
            return encoded
        return json.dumps(
            {
                "summary": (
                    "compact summary omitted because it exceeded its bound; "
                    "consult the referenced durable artifacts"
                )
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    def display(path: Path) -> str:
        try:
            return str(path.resolve().relative_to(root))
        except (OSError, ValueError):
            return str(path)

    active_plan = context.active_plan_path or plan_path
    plan_summaries: list[dict[str, object]] = []
    for label, path in (("original", plan_path), ("active", Path(active_plan))):
        if any(item["label"] == label for item in plan_summaries):
            continue
        try:
            tolerant = load_plan_tolerant(path)
            snapshot = tolerant.parsed_plan.snapshot.to_dict()
            checkpoints = [
                {
                    "name": section.name,
                    "checked": section.heading_checked,
                    "unchecked_steps": section.unchecked_step_count,
                    "checked_steps": section.checked_step_count,
                }
                for section in tolerant.parsed_plan.sections
            ]
            plan_summaries.append(
                {
                    "label": label,
                    "path": str(path),
                    "snapshot": snapshot,
                    "checkpoints": checkpoints,
                    "parse_recovery": tolerant.parse_error is not None,
                }
            )
        except (OSError, UnicodeError, ValueError) as exc:
            plan_summaries.append(
                {"label": label, "path": str(path), "unavailable": type(exc).__name__}
            )

    scope = context.active_implementation_scope
    if scope is None:
        scope_summary: object = "No open implementation scope was durably recorded."
    else:
        scope_summary = {
            key: getattr(scope, key)
            for key in (
                "scope_id",
                "original_plan_path",
                "checkpoint_index",
                "checkpoint_name",
                "opened_turn_number",
                "awaiting_review",
                "carried_reviewer_rejection_count",
                "current_partition_id",
            )
        }
    pending = context.pending_finalized_turn
    pending_summary: object = (
        {
            "turn_number": pending.turn_number,
            "step_name": pending.step_name,
            "step_role": pending.step_role,
            "selector": pending.selector,
            "active_plan_path": str(pending.active_plan_path),
        }
        if pending is not None
        else "No pending finalized interstep boundary was recorded."
    )
    latest_turn, latest_result_path = _recovery_structural_turn(source_dir)
    if latest_turn is None:
        outcome_summary: object = (
            "No finalized worker/reviewer turn artifact was available; consult the "
            "referenced run metadata and artifacts."
        )
    else:
        outcome_summary = latest_turn
        if latest_result_path is not None:
            outcome_summary = {
                **latest_turn,
                "result_artifact": display(latest_result_path),
            }
    source_state = {
        key: source_run_json[key]
        for key in ("status", "current_step_name", "turns_completed", "active_turn")
        if key in source_run_json
    }

    compact_context: object = "Existing compact evidence summary unavailable."
    if latest_turn is not None:
        try:
            manager_context = build_manager_context(
                source_dir,
                level="lite",
                trigger="durable_recovery",
            )
            plan_state = manager_context.get("plan_state")
            controller_state = manager_context.get("controller_state")
            compact_context = {
                "plan_state": plan_state if isinstance(plan_state, Mapping) else {},
                "controller_state": (
                    {
                        key: controller_state[key]
                        for key in (
                            "turns_completed",
                            "current_step_name",
                            "active_implementation_scope",
                            "pending_finalized_turn",
                        )
                        if isinstance(controller_state, Mapping) and key in controller_state
                    }
                    if isinstance(controller_state, Mapping)
                    else {}
                ),
            }
        except (OSError, UnicodeError, ValueError, KeyError, TypeError):
            pass

    fingerprint = workspace_evidence.get("workspace_fingerprint")
    if not isinstance(fingerprint, Mapping):
        fingerprint = {}
    pending_review = bool(
        scope is not None and scope.awaiting_review
    ) or pending is not None
    def bounded_evidence_lines(byte_limit: int = 4096) -> list[str]:
        selected: list[str] = []
        used = 0
        omitted = 0
        for reference in intent.evidence:
            line = (
                f"- {reference.kind}: {reference.path} "
                f"(sha256={reference.sha256}, size={reference.size})"
            )
            line_size = len(line.encode("utf-8"))
            separator_size = 1 if selected else 0
            if used + separator_size + line_size > byte_limit:
                omitted += 1
                continue
            selected.append(line)
            used += separator_size + line_size
        if omitted:
            omission = (
                f"- {omitted} additional evidence reference(s) omitted from this "
                "bounded brief; the immutable intent contains the complete list."
            )
            omission_size = len(omission.encode("utf-8"))
            while selected and used + 1 + omission_size > byte_limit:
                removed = selected.pop()
                used -= len(removed.encode("utf-8")) + (1 if selected else 0)
            if used + (1 if selected else 0) + omission_size <= byte_limit:
                selected.append(omission)
        return selected

    evidence_lines = bounded_evidence_lines()
    lines = [
        "## Durable provider-recovery evidence",
        f"Mode: {intent.mode}; source run: {intent.source_run_id}; replacement run: {intent.target_run_id}.",
        f"Source worker selector: {intent.source_selector}.",
        f"Explicit replacement worker selector: {intent.target_selector}.",
        "Start a fresh replacement session: no source session ID and no source handover inference.",
        "Hidden source-session context is unavailable; source_session_context_transferred=false.",
        "Missing durable evidence: provider-private session state, transcripts, secrets, and unreferenced context are unavailable.",
        f"Original/active plans: {compact(plan_summaries, byte_limit=4096)}",
        f"Open implementation scope: {compact(scope_summary, byte_limit=2048)}",
        f"Pending review or finalized boundary: {compact({'pending_review': pending_review, 'boundary': pending_summary}, byte_limit=2048)}",
        f"Last finalized worker/reviewer outcome: {compact(outcome_summary, byte_limit=2048)}",
        f"Source durable state: {compact(source_state, byte_limit=1024)}",
        f"Rechecked exact file evidence references: {len(evidence_paths)}.",
        f"Exact execution worktree: {workspace_evidence.get('workspace', '<unavailable>')}",
        f"Exact worktree HEAD: {fingerprint.get('head', '<unavailable>')}",
        f"Worktree dirty: {'yes' if fingerprint.get('status') else 'no'}.",
        f"Existing compact evidence summary: {compact(compact_context, byte_limit=3072)}",
        "Fuller durable artifacts (references only; read when needed):",
        *evidence_lines,
        "Do not reconstruct omitted provider context or infer a checkpoint/selector from current configuration.",
    ]
    brief = "\n".join(lines)
    if len(brief.encode("utf-8")) > 16 * 1024:
        lines = [
            lines[0],
            lines[1],
            lines[2],
            lines[3],
            lines[4],
            lines[5],
            lines[6],
            "Original/active plans: bounded structural summary unavailable; use exact plan evidence references.",
            "Open implementation scope: bounded structural summary unavailable; use exact run evidence references.",
            "Pending review or finalized boundary: bounded structural summary unavailable; use exact run evidence references.",
            "Last finalized worker/reviewer outcome: bounded structural summary unavailable; use exact run evidence references.",
            "Source durable state: bounded structural summary unavailable; use exact run evidence references.",
            lines[11],
            lines[12],
            lines[13],
            lines[14],
            lines[15],
            "Existing compact evidence summary: bounded structural summary unavailable; use exact run evidence references.",
            lines[17],
            *bounded_evidence_lines(4096),
            lines[-1],
        ]
        brief = "\n".join(lines)
    if len(brief.encode("utf-8")) > 16 * 1024:
        raise DaemonError("durable recovery brief exceeds its size limit")
    return brief


def _prepare_recovery_resume_context(
    *,
    record: Mapping[str, object],
    repo_root: Path,
    run_id: str,
    workflow_config: WorkflowUserConfig,
    bootstrap: Any,
) -> Any:
    """Bind a recovery request to a fresh, bounded replacement session."""
    recovery_request = _recovery_request_from_record(record)
    if recovery_request is None:
        source_run_id = validate_run_id(str(record.get("resumed_from_run_id")))
        _validate_persisted_recovery_runtime(
            repo_root.resolve() / ".aflow" / "runs" / source_run_id,
            expected_run_id=source_run_id,
            allow_pending=False,
            missing_ok=True,
        )
        return bootstrap.resume_context
    from dataclasses import replace as dataclass_replace
    from aflow.run_state import (
        RecoverySessionContext,
        ResumeContext,
        hotplug_resume_fields,
    )
    from aflow.workflow import resolve_profile

    context = getattr(bootstrap, "resume_context", None)
    if not isinstance(context, ResumeContext):
        raise DaemonError("durable-evidence recovery lacks exact resume context")
    source_run_id = validate_run_id(str(record.get("resumed_from_run_id")))
    target_run_id = validate_run_id(run_id)
    target_dir = repo_root.resolve() / ".aflow" / "runs" / target_run_id
    if target_dir.is_symlink() or not target_dir.is_dir():
        raise DaemonError("recovery target run directory is missing or unsafe")
    try:
        target_dir.resolve().relative_to((repo_root / ".aflow" / "runs").resolve())
    except (OSError, ValueError) as exc:
        raise DaemonError("recovery target run directory is outside the runs root") from exc
    try:
        resolve_profile(
            recovery_request.worker_selector,
            workflow_config,
            step_path="resume.recovery.worker",
        )
    except Exception as exc:
        raise DaemonError("durable-evidence recovery target worker selector is not configured") from exc

    try:
        intent = read_recovery_intent(target_dir)
    except RecoveryPersistenceError as exc:
        raise DaemonError(f"durable recovery intent is unavailable: {exc}") from exc
    if (
        intent.source_run_id != source_run_id
        or intent.target_run_id != target_run_id
        or intent.target_selector != recovery_request.worker_selector
        or intent.source_session_context_transferred is not False
    ):
        raise DaemonError("durable recovery intent does not match the launch record")
    intent_digest = recovery_intent_digest(intent)
    if record.get("recovery_intent_digest") != intent_digest:
        raise DaemonError("recovery record does not match its immutable intent")
    artifact_path = target_dir / "recovery" / "recovery-intent.json"
    if artifact_path.is_symlink() or not artifact_path.is_file():
        raise DaemonError("durable recovery intent artifact is missing or unsafe")
    try:
        artifact_bytes = artifact_path.read_bytes()
    except OSError as exc:
        raise DaemonError("durable recovery intent artifact cannot be read") from exc
    if hashlib.sha256(artifact_bytes).hexdigest() != recovery_artifact_digest(intent):
        raise DaemonError("durable recovery intent artifact bytes changed")
    if record.get("recovery_artifact_path") != "recovery/recovery-intent.json":
        raise DaemonError("recovery record has an invalid intent artifact path")
    if record.get("recovery_artifact_sha256") != hashlib.sha256(artifact_bytes).hexdigest():
        raise DaemonError("recovery record does not match its intent artifact")

    evidence_paths, workspace_evidence = _validate_recovery_evidence_for_worker(
        repo_root.resolve(), intent
    )
    source_dir = repo_root.resolve() / ".aflow" / "runs" / source_run_id
    source_run_json_path = source_dir / "run.json"
    if source_run_json_path.is_symlink() or not source_run_json_path.is_file():
        raise DaemonError("source run metadata evidence is missing or unsafe")
    try:
        source_run_json = json.loads(source_run_json_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise DaemonError("source run metadata evidence is unreadable") from exc
    if not isinstance(source_run_json, Mapping):
        raise DaemonError("source run metadata evidence is not an object")
    brief = _build_durable_recovery_brief(
        repo_root=repo_root,
        source_dir=source_dir,
        source_run_json=source_run_json,
        intent=intent,
        context=context,
        plan_path=Path(getattr(bootstrap, "plan_path", repo_root / "plan.md")),
        evidence_paths=evidence_paths,
        workspace_evidence=workspace_evidence,
    )
    target_payload, operation_state, consumed = _read_recovery_target_runtime(
        target_dir,
        intent=intent,
        intent_digest=intent_digest,
    )
    if target_payload is not None:
        raw_runtime = target_payload.get("recovery_runtime")
        if isinstance(raw_runtime, Mapping):
            expected_brief_sha256 = hashlib.sha256(
                brief.encode("utf-8")
            ).hexdigest()
            if raw_runtime.get("brief_sha256") != expected_brief_sha256:
                raise DaemonError(
                    "recovery target runtime state does not match its recovery brief"
                )
    if operation_state == "consumed" and target_payload is not None:
        try:
            context = dataclass_replace(context, **hotplug_resume_fields(target_payload))
            from aflow.cli import _reconstruct_resume_context

            target_workflow = workflow_config.workflows.get(
                getattr(bootstrap, "workflow_name", "")
            )
            target_context = _reconstruct_resume_context(
                resolved_run_id=target_dir,
                run_dir=target_dir,
                prev_run=target_payload,
                plan_path=Path(getattr(bootstrap, "plan_path", repo_root / "plan.md")),
                frozen_run_identity=context.frozen_run_identity,
                reset_scope=False,
                require_resume=True,
                workflow_steps=(
                    target_workflow.steps if target_workflow is not None else None
                ),
                effective_max_turns=(
                    target_payload.get("effective_max_turns")
                    if isinstance(target_payload.get("effective_max_turns"), int)
                    and not isinstance(target_payload.get("effective_max_turns"), bool)
                    else None
                ),
            )
            if target_context is not None:
                context = dataclass_replace(
                    target_context,
                    resumed_from_run_id=source_run_id,
                )
        except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
            raise DaemonError(
                "consumed recovery target has invalid persisted session state"
            ) from exc
    recovery_context = RecoverySessionContext(
        intent=intent,
        brief=brief,
        intent_digest=intent_digest,
        consumed=consumed,
        operation_state=operation_state,
    )
    return dataclass_replace(context, recovery_context=recovery_context)


def _worker_prepared(
    record: Mapping[str, object],
    manifest: LaunchManifest,
    repo_root: Path,
    config_path: Path,
    workflow_config: WorkflowUserConfig,
    *,
    extra_instructions: tuple[str, ...] = (),
) -> tuple[PreparedRun, object | None]:
    run_id = validate_run_id(str(record["run_id"]))
    if record.get("mode") == "resume":
        source_run_id = validate_run_id(str(record["resumed_from_run_id"]))
        from aflow.cli import _bootstrap_resume_invocation

        extra_instructions_provided = record.get(
            "resume_extra_instructions_provided", False
        )
        if not isinstance(extra_instructions_provided, bool):
            raise DaemonError(
                "resume record has an invalid extra-instructions override flag"
            )
        bootstrap = _bootstrap_resume_invocation(
            repo_root=repo_root,
            config_path=config_path,
            default_config_path=config_path,
            config_path_is_explicit=True,
            workflow_config=workflow_config,
            requested_run_id=source_run_id,
            workflow_arg=None,
            plan_file_arg=None,
            team_arg=None,
            start_step_arg=None,
            max_turns_arg=None,
            extra_instructions_arg=extra_instructions,
            extra_instructions_provided=extra_instructions_provided,
            live_loader=load_workflow_config,
        )
        prepared = PreparedRun(
            workflow_name=bootstrap.workflow_name,
            repo_root=repo_root,
            plan_path=bootstrap.plan_path,
            config_path=getattr(bootstrap, "config_path", config_path),
            max_turns=bootstrap.max_turns,
            team=bootstrap.team,
            extra_instructions=bootstrap.extra_instructions,
            start_step=(
                bootstrap.start_step
                or getattr(
                    bootstrap,
                    "workflow_config",
                    workflow_config,
                ).workflows[bootstrap.workflow_name].first_step
                or bootstrap.workflow_name
            ),
            reserved_run_id=run_id,
            idempotency_key=manifest.idempotency_key,
            caller_scope=manifest.caller_scope,
            team_explicit=getattr(bootstrap, "team_explicit", None),
            max_turns_explicit=getattr(bootstrap, "max_turns_explicit", None),
            start_step_explicit=True,
        )
        workflow_config = getattr(bootstrap, "workflow_config", workflow_config)
        _validate_worker_selection(prepared, workflow_config)
        resume_context = _prepare_recovery_resume_context(
            record=record,
            repo_root=repo_root,
            run_id=run_id,
            workflow_config=workflow_config,
            bootstrap=bootstrap,
        )
        return prepared, resume_context
    payload = record.get("prepared")
    if not isinstance(payload, Mapping):
        raise DaemonError("daemon worker start record has no prepared request")
    prepared = _prepared_from_payload(
        payload,
        repo_root=repo_root,
        config_path=config_path,
        extra_instructions=extra_instructions,
    )
    prepared = replace(
        prepared,
        config_path=config_path,
        reserved_run_id=run_id,
        idempotency_key=manifest.idempotency_key,
        caller_scope=manifest.caller_scope,
    )
    prepared = _apply_live_worker_defaults(prepared, workflow_config)
    _validate_worker_selection(prepared, workflow_config)
    if prepared.skipped_steps != _skipped_steps_for(
        workflow_config, prepared.workflow_name, prepared.start_step
    ):
        raise DaemonError("daemon worker skipped-step state is invalid")
    return prepared, None


def _apply_live_worker_defaults(
    prepared: PreparedRun,
    workflow_config: WorkflowUserConfig,
) -> PreparedRun:
    """Resolve non-explicit startup choices from the worker's current config."""
    workflow = workflow_config.workflows.get(prepared.workflow_name)
    if workflow is None:
        raise DaemonError(
            f"daemon worker workflow '{prepared.workflow_name}' is not configured"
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
    return replace(
        prepared,
        team=prepared.team if team_explicit else workflow.team,
        max_turns=(
            prepared.max_turns
            if max_turns_explicit
            else workflow_config.aflow.max_turns
        ),
        start_step=(
            prepared.start_step
            if start_step_explicit
            else workflow.first_step
        ),
        skipped_steps=(
            _skipped_steps_for(
                workflow_config,
                prepared.workflow_name,
                prepared.start_step
                if start_step_explicit
                else workflow.first_step,
            )
        ),
        team_explicit=team_explicit,
        max_turns_explicit=max_turns_explicit,
        start_step_explicit=start_step_explicit,
    )


def _validate_worker_selection(
    prepared: PreparedRun,
    workflow_config: WorkflowUserConfig,
) -> None:
    """Reject a stale worker selection before entering the controller."""
    workflow = workflow_config.workflows.get(prepared.workflow_name)
    if workflow is None:
        raise DaemonError(
            f"daemon worker workflow '{prepared.workflow_name}' is not configured"
        )
    if prepared.start_step in workflow.excluded_steps:
        raise DaemonError(
            f"daemon worker start step '{prepared.start_step}' is excluded "
            f"from workflow '{prepared.workflow_name}'"
        )
    if prepared.start_step not in workflow.steps:
        raise DaemonError(
            f"daemon worker start step '{prepared.start_step}' is not configured "
            f"for workflow '{prepared.workflow_name}'"
        )
    if prepared.team is not None and prepared.team not in workflow_config.teams:
        raise DaemonError(
            f"daemon worker team '{prepared.team}' is not configured"
        )
    if (
        not isinstance(prepared.max_turns, int)
        or isinstance(prepared.max_turns, bool)
        or prepared.max_turns < 1
    ):
        raise DaemonError("daemon worker max_turns must be a positive integer")


def _resolve_configured_start_step(
    raw_start_step: str | None,
    workflow_name: str,
    steps: Mapping[str, object],
    *,
    excluded_steps: tuple[str, ...] = (),
) -> str | None:
    if raw_start_step is None:
        return None
    value = str(raw_start_step)
    if value and value.isascii() and value.isdecimal():
        index = int(value)
        names = tuple(steps)
        if index < 1 or index > len(names):
            raise DaemonError(
                f"startup step index is out of range for workflow '{workflow_name}'"
            )
        return names[index - 1]
    if value in excluded_steps:
        raise DaemonError(f"startup step is excluded from workflow '{workflow_name}'")
    if value not in steps:
        raise DaemonError(
            f"startup step is not configured for workflow '{workflow_name}'"
        )
    return value


def _validated_skipped_steps(value: object) -> tuple[str, ...]:
    if (
        not isinstance(value, (list, tuple))
        or len(value) > 128
        or any(
            not isinstance(item, str) or not item or len(item) > 4_096
            for item in value
        )
        or len(set(value)) != len(value)
    ):
        raise DaemonError("prepared startup record has invalid skipped steps")
    return tuple(value)


def _skipped_steps_for(
    workflow_config: WorkflowUserConfig,
    workflow_name: str,
    start_step: str,
) -> tuple[str, ...]:
    workflow = workflow_config.workflows[workflow_name]
    executable_steps = tuple(workflow.steps)
    if start_step not in executable_steps:
        raise DaemonError(
            f"startup step is not configured for workflow '{workflow_name}'"
        )
    return executable_steps[: executable_steps.index(start_step)]


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


def _extra_instructions_digest(extra_instructions: tuple[str, ...]) -> str:
    return hashlib.sha256(
        json.dumps(
            list(extra_instructions),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _record_extra_instructions_digest(
    record: Mapping[str, object],
) -> str | None:
    value = record.get("resume_extra_instructions_digest")
    if value is None:
        prepared = record.get("prepared")
        if isinstance(prepared, Mapping):
            value = prepared.get("extra_instructions_digest")
    return value if isinstance(value, str) else None


def _recovery_request_from_record(
    record: Mapping[str, object],
) -> RecoveryRequest | None:
    try:
        return RecoveryRequest.from_value(record.get("recovery"))
    except RecoveryValidationError as exc:
        raise DaemonError("startup record has an invalid recovery request") from exc


def _source_worker_selector(
    *,
    source: RunStatus,
    run_json_path: Path,
    run_json: Mapping[str, object] | None,
    context: Any,
    source_dir: Path,
) -> tuple[str, Path | None]:
    """Return an explicitly recorded source selector, never a config default."""
    if run_json is None:
        try:
            raw = json.loads(run_json_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise DaemonError("source run metadata is not readable JSON") from exc
        run_json = raw if isinstance(raw, Mapping) else None
    if run_json is None:
        raise DaemonError("source run metadata is not a JSON object")

    candidates: set[str] = set()

    def add(value: object) -> None:
        if isinstance(value, str) and value and value == value.strip():
            candidates.add(value)

    status_evidence = source.evidence
    for key in ("source_selector", "worker_selector"):
        add(status_evidence.get(key))
    worker_evidence = status_evidence.get("worker")
    if isinstance(worker_evidence, Mapping):
        add(worker_evidence.get("selector"))

    context_selectors = getattr(context, "role_selectors", {})
    if isinstance(context_selectors, Mapping):
        add(context_selectors.get("worker"))
    raw_selectors = run_json.get("role_selectors")
    if isinstance(raw_selectors, Mapping):
        add(raw_selectors.get("worker"))
    for key in ("source_selector", "worker_selector"):
        add(run_json.get(key))

    def active_session_selectors(value: object) -> tuple[str, ...]:
        if not isinstance(value, (list, tuple)):
            return ()
        values: list[str] = []
        for item in value:
            if isinstance(item, Mapping):
                if item.get("role") == "worker" and item.get("status") == "active":
                    selector = item.get("selector")
                    if isinstance(selector, str) and selector:
                        values.append(selector)
            else:
                if (
                    getattr(item, "role", None) == "worker"
                    and getattr(item, "status", None) == "active"
                ):
                    selector = getattr(item, "selector", None)
                    if isinstance(selector, str) and selector:
                        values.append(selector)
        return tuple(values)

    for selector in active_session_selectors(
        getattr(context, "active_role_sessions", ())
    ):
        add(selector)
    for selector in active_session_selectors(run_json.get("active_role_sessions")):
        add(selector)

    if len(candidates) > 1:
        raise DaemonError(
            "source worker selector evidence is conflicting; no selector was inferred"
        )
    if candidates:
        return next(iter(candidates)), run_json_path

    # A finalized worker result is an exact source artifact fallback.  It is
    # only used when no current durable selector field exists; configuration
    # defaults are intentionally never consulted.
    result_paths = sorted(
        source_dir.glob("turns/*/result.json"),
        key=lambda path: path.as_posix(),
        reverse=True,
    )
    for result_path in result_paths:
        try:
            if result_path.is_symlink() or result_path.stat().st_size > 1_048_576:
                continue
            payload = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            continue
        if (
            isinstance(payload, Mapping)
            and payload.get("step_role") == "worker"
            and isinstance(payload.get("selector"), str)
            and payload["selector"]
        ):
            return payload["selector"], result_path
    raise DaemonError("source worker selector evidence is unavailable")


def _validate_extra_instructions(extra_instructions: tuple[str, ...]) -> None:
    if not isinstance(extra_instructions, tuple):
        raise DaemonError("extra instructions must be a tuple of strings")
    if len(extra_instructions) > _MAX_EXTRA_INSTRUCTION_ITEMS:
        raise DaemonError("too many extra instructions")
    if any(
        not isinstance(item, str)
        or not item.strip()
        or len(item) > _MAX_EXTRA_INSTRUCTION_LENGTH
        or "\x00" in item
        for item in extra_instructions
    ):
        raise DaemonError("extra instructions must be non-empty bounded strings")
    if sum(len(item) for item in extra_instructions) > _MAX_EXTRA_INSTRUCTIONS_LENGTH:
        raise DaemonError("extra instructions exceed the total length limit")


def _startup_request_digest(request: StartupRequest) -> str:
    return hashlib.sha256(
        json.dumps(
            _request_payload(request), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def _request_payload(request: StartupRequest) -> dict[str, object]:
    _validate_extra_instructions(request.extra_instructions)
    return {
        "repo_root": str(Path(request.repo_root).resolve()),
        "plan_path": str(Path(request.plan_path).resolve()),
        "config_path": str(Path(request.config_path).resolve()),
        "workflow_name": request.workflow_name,
        "start_step": request.start_step,
        "max_turns": request.max_turns,
        "team": request.team,
        "team_explicit": request.team_explicit,
        "max_turns_explicit": request.max_turns_explicit,
        "start_step_explicit": request.start_step_explicit,
        "resume_requested": request.resume_requested,
        "startup_base_head_refresh_sha": request.startup_base_head_refresh_sha,
        "dirty_worktree_confirmed": request.dirty_worktree_confirmed,
        "extra_instructions_digest": _extra_instructions_digest(
            request.extra_instructions
        ),
        "restarted_from_run_id": request.restarted_from_run_id,
    }


def _request_from_payload(
    payload: Mapping[str, object],
    *,
    workflow_config: WorkflowUserConfig,
    extra_instructions: tuple[str, ...],
    reserved_run_id: str,
    caller_scope: str,
    idempotency_key: str | None,
) -> StartupRequest:
    for key in ("repo_root", "plan_path", "config_path"):
        if not isinstance(payload.get(key), str):
            raise DaemonError("startup record request has invalid path data")
    _validate_extra_instructions(extra_instructions)
    if payload.get(
        "extra_instructions_digest", _extra_instructions_digest(())
    ) != _extra_instructions_digest(extra_instructions):
        raise DaemonError(
            "non-persistent extra instructions are unavailable; submit a new start request"
        )
    max_turns = payload.get("max_turns")
    if max_turns is not None and (
        not isinstance(max_turns, int) or isinstance(max_turns, bool)
    ):
        raise DaemonError("startup record request has invalid max_turns")
    choice_values = {}
    for field_name in (
        "team_explicit",
        "max_turns_explicit",
        "start_step_explicit",
    ):
        value = payload.get(field_name)
        if value is not None and not isinstance(value, bool):
            raise DaemonError(f"startup record request has invalid {field_name}")
        choice_values[field_name] = value
    dirty_worktree_confirmed = payload.get("dirty_worktree_confirmed", False)
    if not isinstance(dirty_worktree_confirmed, bool):
        raise DaemonError(
            "startup record request has invalid dirty_worktree_confirmed"
        )
    return StartupRequest(
        repo_root=Path(str(payload["repo_root"])),
        plan_path=Path(str(payload["plan_path"])),
        config_path=Path(str(payload["config_path"])),
        workflow_config=workflow_config,
        workflow_name=_optional_string(payload.get("workflow_name")),
        start_step=_optional_string(payload.get("start_step")),
        max_turns=max_turns,
        team=_optional_string(payload.get("team")),
        team_explicit=choice_values["team_explicit"],
        max_turns_explicit=choice_values["max_turns_explicit"],
        start_step_explicit=choice_values["start_step_explicit"],
        extra_instructions=extra_instructions,
        resume_requested=bool(payload.get("resume_requested", False)),
        startup_base_head_refresh_sha=_optional_string(
            payload.get("startup_base_head_refresh_sha")
        ),
        dirty_worktree_confirmed=dirty_worktree_confirmed,
        reserved_run_id=reserved_run_id,
        caller_scope=caller_scope,
        idempotency_key=idempotency_key,
        restarted_from_run_id=_optional_string(
            payload.get("restarted_from_run_id")
        ),
    )


def _prepared_payload(prepared: PreparedRun) -> dict[str, object]:
    _validate_extra_instructions(prepared.extra_instructions)
    return {
        "workflow_name": prepared.workflow_name,
        "repo_root": str(prepared.repo_root),
        "plan_path": str(prepared.plan_path),
        "config_path": str(prepared.config_path),
        "max_turns": prepared.max_turns,
        "team": prepared.team,
        "team_explicit": prepared.team_explicit,
        "max_turns_explicit": prepared.max_turns_explicit,
        "start_step_explicit": prepared.start_step_explicit,
        "start_step": prepared.start_step,
        "startup_base_head_refresh_sha": prepared.startup_base_head_refresh_sha,
        "dirty_worktree_confirmed": prepared.dirty_worktree_confirmed,
        "move_completed_plan_to_done": prepared.move_completed_plan_to_done,
        "extra_instructions_digest": _extra_instructions_digest(
            prepared.extra_instructions
        ),
        "restarted_from_run_id": prepared.restarted_from_run_id,
        "skipped_steps": list(prepared.skipped_steps),
    }


def _prepared_from_payload(
    payload: Mapping[str, object],
    *,
    repo_root: Path,
    config_path: Path,
    extra_instructions: tuple[str, ...] = (),
) -> PreparedRun:
    _validate_extra_instructions(extra_instructions)
    if payload.get(
        "extra_instructions_digest", _extra_instructions_digest(())
    ) != _extra_instructions_digest(extra_instructions):
        raise DaemonError(
            "non-persistent extra instructions are unavailable; submit a new start request"
        )
    required_strings = ("workflow_name", "plan_path", "start_step")
    if any(
        not isinstance(payload.get(key), str) or not str(payload[key]).strip()
        for key in required_strings
    ):
        raise DaemonError("prepared startup record has invalid required values")
    max_turns = payload.get("max_turns")
    if not isinstance(max_turns, int) or isinstance(max_turns, bool) or max_turns < 1:
        raise DaemonError("prepared startup record has invalid max_turns")
    choice_values = {}
    for field_name in (
        "team_explicit",
        "max_turns_explicit",
        "start_step_explicit",
    ):
        value = payload.get(field_name)
        if value is not None and not isinstance(value, bool):
            raise DaemonError(f"prepared startup record has invalid {field_name}")
        choice_values[field_name] = value
    dirty_worktree_confirmed = payload.get("dirty_worktree_confirmed", False)
    if not isinstance(dirty_worktree_confirmed, bool):
        raise DaemonError(
            "prepared startup record has invalid dirty_worktree_confirmed"
        )
    return PreparedRun(
        workflow_name=str(payload["workflow_name"]),
        repo_root=repo_root,
        plan_path=Path(str(payload["plan_path"])),
        config_path=config_path,
        max_turns=max_turns,
        team=_optional_string(payload.get("team")),
        team_explicit=choice_values["team_explicit"],
        max_turns_explicit=choice_values["max_turns_explicit"],
        start_step_explicit=choice_values["start_step_explicit"],
        extra_instructions=extra_instructions,
        start_step=str(payload["start_step"]),
        dirty_worktree_confirmed=dirty_worktree_confirmed,
        startup_base_head_refresh_sha=_optional_string(payload.get("startup_base_head_refresh_sha")),
        move_completed_plan_to_done=bool(payload.get("move_completed_plan_to_done", False)),
        restarted_from_run_id=_optional_string(
            payload.get("restarted_from_run_id")
        ),
        skipped_steps=_validated_skipped_steps(payload.get("skipped_steps", ())),
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
    if not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in options.items()
    ):
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
        if entry.get("idempotency_key") is not None and not isinstance(
            entry.get("idempotency_key"), str
        ):
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
    if (
        not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation < 1
    ):
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
    return (
        json.dumps(dict(record), sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
