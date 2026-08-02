"""Stable app-facing models for planning providers and sessions."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


_URL_SEGMENT_CHARACTERS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~"
)


class PlanningModel(BaseModel):
    """Strict base model for the provider-neutral API boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class SessionKey(PlanningModel):
    """A collision-safe identity whose provider-local id is one URL segment."""

    provider_id: str = Field(min_length=1, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    provider_session_id: str = Field(min_length=1)

    @field_validator("provider_session_id")
    @classmethod
    def validate_provider_session_id(cls, value: str) -> str:
        if value in {".", ".."} or any(
            character not in _URL_SEGMENT_CHARACTERS for character in value
        ):
            raise ValueError(
                "provider_session_id must be a non-empty URL-safe path segment"
            )
        return value


class ProviderState(str, Enum):
    STARTING = "starting"
    READY = "ready"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    DISABLED = "disabled"


class PlanningErrorCode(str, Enum):
    INVALID_REQUEST = "invalid_request"
    PROVIDER_NOT_FOUND = "provider_not_found"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_TIMEOUT = "provider_timeout"
    SESSION_NOT_FOUND = "session_not_found"
    CAPABILITY_UNSUPPORTED = "capability_unsupported"
    CONFLICT = "conflict"
    ATTACHMENT_NOT_FOUND = "attachment_not_found"
    ATTACHMENT_LIMIT_EXCEEDED = "attachment_limit_exceeded"
    ATTACHMENT_IN_USE = "attachment_in_use"
    INTERNAL_ERROR = "internal_error"


class PlanningError(PlanningModel):
    """Bounded error safe to serialize to clients."""

    code: PlanningErrorCode
    message: str = Field(min_length=1, max_length=500)
    provider_id: str | None = Field(
        default=None, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
    )
    retryable: bool = False


class ProviderCapabilities(PlanningModel):
    """Features and controlled vocabularies advertised by one provider."""

    models: tuple[str, ...] = ()
    reasoning_levels: tuple[str, ...] = ()
    reasoning_summaries: tuple[str, ...] = ()
    attachments: bool = False
    attachment_kinds: tuple[AttachmentKind, ...] = ()
    output_schema: bool = False
    fork: bool = False
    archive: bool = False
    approvals: bool = False
    interruption: bool = False
    compaction: bool = False
    rollback: bool = False


class ProviderReadiness(PlanningModel):
    provider_id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    display_name: str
    state: ProviderState
    capabilities: ProviderCapabilities
    error: PlanningError | None = None


class SessionStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    FAILED = "failed"
    ARCHIVED = "archived"
    UNKNOWN = "unknown"


class TurnStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class AttachmentKind(str, Enum):
    FILE = "file"
    IMAGE = "image"


class Attachment(PlanningModel):
    attachment_id: str = Field(min_length=1)
    filename: str = Field(min_length=1)
    kind: AttachmentKind
    media_type: str | None = None
    size_bytes: int = Field(ge=0)
    created_at: datetime | None = None

    @field_validator("attachment_id")
    @classmethod
    def validate_attachment_id(cls, value: str) -> str:
        if value in {".", ".."} or any(
            character not in _URL_SEGMENT_CHARACTERS for character in value
        ):
            raise ValueError("attachment_id must be a URL-safe path segment")
        return value


class Turn(PlanningModel):
    turn_id: str = Field(min_length=1)
    status: TurnStatus
    items: tuple[dict[str, Any], ...] = ()
    error: PlanningError | None = None
    created_at: datetime | None = None
    completed_at: datetime | None = None
    attachment_ids: tuple[str, ...] = ()


class Session(PlanningModel):
    key: SessionKey
    project_id: str | None = Field(default=None, min_length=1)
    cwd: str
    title: str | None = None
    preview: str = ""
    status: SessionStatus = SessionStatus.UNKNOWN
    model: str | None = None
    reasoning_level: str | None = None
    archived: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None
    turns: tuple[Turn, ...] = ()

    @property
    def provider_id(self) -> str:
        return self.key.provider_id

    @property
    def provider_session_id(self) -> str:
        return self.key.provider_session_id


class SessionPage(PlanningModel):
    sessions: tuple[Session, ...] = ()
    next_cursor: str | None = None


class StartSessionRequest(PlanningModel):
    model: str | None = None
    reasoning_level: str | None = None


class StartTurnRequest(PlanningModel):
    text: str = Field(min_length=1)
    attachment_ids: tuple[str, ...] = ()
    model: str | None = None
    reasoning_level: str | None = None
    reasoning_summary: str | None = None
    output_schema: dict[str, Any] | None = None


class ApprovalDecision(PlanningModel):
    approval_id: str = Field(min_length=1)
    decision: Literal["accept", "decline", "cancel"]


class PendingApproval(PlanningModel):
    approval_id: str = Field(min_length=1)
    key: SessionKey
    turn_id: str = Field(min_length=1)
    kind: Literal["command", "file_change"]
    reason: str | None = None
