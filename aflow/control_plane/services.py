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

from .models import ContextBundle, RunControlRequest, StartupQuestionRecord
from .persistence import (
    ControlWriteResult,
    append_run_event,
    build_context_bundle,
    compare_and_swap_overrides,
    read_events,
)
from .repository import RunRepository


class ServiceAuthorizationError(PermissionError):
    """A transport-provided per-run authorization hook denied an operation."""


class ControlIdempotencyConflict(ValueError):
    """An idempotency key was reused for a different control operation."""


RunAuthorizer = Callable[[str, object], bool | None]


class ControlService:
    """Apply safe controls only through the CP01 CAS and journal primitives."""

    def __init__(self, repository: RunRepository, *, authorizer: RunAuthorizer | None = None) -> None:
        self._repository = repository
        self._authorizer = authorizer

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
    ) -> ContextBundle:
        status = self._repository.get_run_status(run_id)
        action = "context:full" if level == "full" else "context:lite"
        if self._authorizer is not None and self._authorizer(action, status) is False:
            raise ServiceAuthorizationError(f"not authorized for {action}")
        return build_context_bundle(
            self._repository.run_directory(run_id), level=level, full_scope=full_scope
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
