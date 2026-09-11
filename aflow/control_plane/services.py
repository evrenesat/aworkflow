"""Transport-neutral control, context, and startup services over AFlow authority."""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
from typing import Callable, Literal
from uuid import uuid4

from aflow.api.models import PreparedRun, StartupQuestion, StartupRequest
from aflow.api.startup import prepare_startup, prepare_startup_with_answer
from aflow.config import ConfigError, WorkflowUserConfig
from aflow.live_config import load_live_config
from aflow.run_state import OverrideRequest, load_override_request

from .models import ContextBundle, RunControlRequest, RunStatus, StartupQuestionRecord
from .persistence import (
    ControlConflictError,
    ControlWriteResult,
    PersistenceError,
    append_run_event,
    build_context_bundle,
    compare_and_swap_overrides,
    read_events,
    validate_control_request,
)
from .repository import RunRepository
from .validation import ControlValidationError, validate_override_targets


class ServiceAuthorizationError(PermissionError):
    """A transport-provided per-run authorization hook denied an operation."""


class ControlIdempotencyConflict(ValueError):
    """An idempotency key was reused for a different control operation."""


RunAuthorizer = Callable[[str, object], bool | None]
ControlConfigLoader = Callable[[], WorkflowUserConfig]


class ControlService:
    """Apply safe controls only through the CP01 CAS and journal primitives."""

    def __init__(
        self,
        repository: RunRepository,
        *,
        authorizer: RunAuthorizer | None = None,
        config_loader: ControlConfigLoader | None = None,
        config_path: Path | None = None,
    ) -> None:
        if config_loader is not None and config_path is not None:
            raise TypeError("pass only one of config_loader and config_path")
        if config_path is not None:
            selected_path = Path(config_path)
            config_loader = lambda: load_live_config(selected_path).workflow_config
        self._repository = repository
        self._authorizer = authorizer
        self._config_loader = config_loader

    def apply(
        self,
        run_id: str,
        request: RunControlRequest,
        *,
        caller_scope: str = "local",
        idempotency_key: str | None = None,
    ) -> ControlWriteResult:
        status = self._repository.get_run_status(run_id)
        self._authorize("control", status)
        if status.ownership != "control_plane":
            raise ServiceAuthorizationError("legacy runs are read-only")
        run_dir = self._repository.run_directory(run_id)
        digest = _control_digest(request)
        if idempotency_key is not None:
            replay = self._idempotent_replay(
                run_dir,
                caller_scope=caller_scope,
                idempotency_key=idempotency_key,
                digest=digest,
            )
            if replay is not None:
                return replay
        validate_control_request(request)
        if request.expected_revision != status.revision:
            raise ControlConflictError(status.revision)
        if request.owner_stop is not True and self._config_loader is not None:
            self._validate_live_targets(status, run_dir, request)
        result = compare_and_swap_overrides(self._repository.repo_root, run_id, request)
        append_run_event(
            run_dir,
            "control_request",
            {
                "caller_scope": caller_scope,
                "idempotency_key": idempotency_key,
                "request_digest": digest,
                "revision": result.revision,
                "changed": result.changed,
                "owner_stop": result.owner_stop,
            },
        )
        return result

    def _validate_live_targets(
        self,
        status: object,
        run_dir: Path,
        request: RunControlRequest,
    ) -> None:
        """Validate a merged control proposal against one current config read."""
        assert self._config_loader is not None
        try:
            workflow_config = self._config_loader()
        except ConfigError as exc:
            raise ControlValidationError(
                field="configuration",
                target="live",
                message=f"current live workflow configuration is unusable: {exc}",
            ) from exc

        override_path = run_dir / "overrides.toml"
        if override_path.exists() and (
            override_path.is_symlink() or not override_path.is_file()
        ):
            raise PersistenceError("overrides.toml must be a regular file")
        loaded = load_override_request(override_path)
        if loaded.status == "invalid" or (
            loaded.request is None and loaded.status != "absent"
        ):
            raise ControlValidationError(
                field="overrides",
                target="overrides.toml",
                message=loaded.message or "the existing override file is invalid",
            )
        existing = loaded.request
        effective_team = self._effective_team(
            status, existing, request, workflow_config
        )
        effective_roles = self._effective_roles(existing, request)
        effective_max_turns = self._effective_max_turns(status, existing, request)
        pending_step = existing.next_step if existing is not None else None
        validate_override_targets(
            workflow_config,
            workflow_name=str(getattr(status, "workflow_name", "") or ""),
            step_name=pending_step
            or getattr(status, "current_step", None)
            or getattr(status, "selected_start_step", None),
            team=effective_team,
            role_selectors=effective_roles,
            max_turns=effective_max_turns,
            completed_turns=getattr(status, "turns_completed", None),
            step_field="next_step" if pending_step is not None else "current_step",
        )

    def _effective_team(
        self,
        status: object,
        existing: OverrideRequest | None,
        request: RunControlRequest,
        workflow_config: WorkflowUserConfig,
    ) -> str | None:
        if request.team is not None:
            return request.team.strip()
        if existing is not None and existing.team is not None:
            return existing.team
        workflow_name = str(getattr(status, "workflow_name", "") or "")
        manifest = self._repository.get_launch_manifest(
            str(getattr(status, "run_id", ""))
        )
        if manifest is not None and manifest.team is None:
            workflow = workflow_config.workflows.get(workflow_name)
            return workflow.team if workflow is not None else None
        return getattr(status, "team", None) or (
            manifest.team if manifest is not None else None
        )

    @staticmethod
    def _effective_roles(
        existing: OverrideRequest | None,
        request: RunControlRequest,
    ) -> dict[str, str]:
        roles = dict(existing.role_selectors) if existing is not None else {}
        roles.update(request.role_selectors)
        return roles

    @staticmethod
    def _effective_max_turns(
        status: object,
        existing: OverrideRequest | None,
        request: RunControlRequest,
    ) -> int | None:
        if request.max_turns is not None:
            return request.max_turns
        if existing is not None and existing.max_turns is not None:
            return existing.max_turns
        return getattr(status, "max_turns", None)

    def _idempotent_replay(
        self,
        run_dir: Path,
        *,
        caller_scope: str,
        idempotency_key: str,
        digest: str,
    ) -> ControlWriteResult | None:
        if not idempotency_key:
            raise ValueError("idempotency_key must be non-empty when provided")
        for event in reversed(read_events(run_dir, limit=1_000)):
            if event.event_type != "control_request":
                continue
            data = event.data
            if data.get("caller_scope") != caller_scope or data.get("idempotency_key") != idempotency_key:
                continue
            if data.get("request_digest") != digest:
                raise ControlIdempotencyConflict(
                    "control idempotency key was already used for a different request"
                )
            revision = data.get("revision")
            if not isinstance(revision, int) or isinstance(revision, bool):
                raise ControlIdempotencyConflict("stored control idempotency evidence is malformed")
            return ControlWriteResult(
                revision=revision,
                changed=bool(data.get("changed")),
                owner_stop=bool(data.get("owner_stop")),
                path=run_dir / "overrides.toml",
            )
        return None

    def _authorize(self, action: str, status: object) -> None:
        if self._authorizer is not None and self._authorizer(action, status) is False:
            raise ServiceAuthorizationError(f"not authorized for {action}")


class ContextService:
    """Build bounded context from CP01 data and the established manager context."""

    def __init__(self, repository: RunRepository, *, authorizer: RunAuthorizer | None = None) -> None:
        self._repository = repository
        self._authorizer = authorizer

    def get(
        self,
        run_id: str,
        *,
        level: Literal["lite", "full"] = "lite",
        full_scope: bool = False,
        status: RunStatus | None = None,
    ) -> ContextBundle:
        if status is None:
            status = self._repository.get_run_status(run_id)
        elif status.run_id != run_id:
            raise ValueError("context status does not belong to the requested run")
        action = "context:full" if level == "full" else "context:lite"
        if self._authorizer is not None and self._authorizer(action, status) is False:
            raise ServiceAuthorizationError(f"not authorized for {action}")
        return build_context_bundle(
            self._repository.run_directory(run_id),
            level=level,
            full_scope=full_scope,
            progress_activity=status.activity,
            progress_phase=status.launch_phase,
            progress_run_status=status.status,
        )


class StartupQuestionService:
    """Keep opaque continuation handles around the existing startup protocol."""

    def __init__(self) -> None:
        self._questions: dict[str, tuple[StartupQuestion, StartupRequest]] = {}

    def prepare(self, request: StartupRequest) -> PreparedRun | StartupQuestionRecord:
        return self._record_or_prepared(prepare_startup(request), request)

    def answer(
        self,
        question_id: str,
        answer: str | int | bool,
    ) -> PreparedRun | StartupQuestionRecord:
        try:
            question, request = self._questions.pop(question_id)
        except KeyError as exc:
            raise KeyError("startup question is unknown or has expired") from exc
        return self._record_or_prepared(prepare_startup_with_answer(question, request, answer), request)

    def list_questions(self) -> tuple[StartupQuestionRecord, ...]:
        return tuple(
            _question_record(question_id, question)
            for question_id, (question, _) in sorted(self._questions.items())
        )

    def _record_or_prepared(
        self,
        result: PreparedRun | StartupQuestion,
        request: StartupRequest,
    ) -> PreparedRun | StartupQuestionRecord:
        if isinstance(result, PreparedRun):
            return result
        question_id = uuid4().hex
        self._questions[question_id] = (result, result.continuation_request or request)
        return _question_record(question_id, result)


def _question_record(question_id: str, question: StartupQuestion) -> StartupQuestionRecord:
    return StartupQuestionRecord(
        question_id=question_id,
        kind=question.kind.value,
        message=question.message,
        options=dict(question.options),
        choices=tuple(question.choices),
    )


def _control_digest(request: RunControlRequest) -> str:
    payload = asdict(request)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
