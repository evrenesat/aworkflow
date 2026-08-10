"""API models for the remote app server."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field

from aflow.control_plane import (
    CapabilitySet,
    ContextBundle,
    RunControlRequest,
    RunEvent,
    RunStatus,
    StartRunResult,
    StartupQuestionRecord,
)


class PlanStatus(str, Enum):
    """Status of a plan file."""

    DRAFT = "draft"
    IN_PROGRESS = "in_progress"


@dataclass
class RepoInfo:
    """Information about a registered repository."""

    id: str
    name: str
    path: Path
    is_git_root: bool
    registered_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "path": str(self.path),
            "is_git_root": self.is_git_root,
            "registered_at": self.registered_at.isoformat(),
        }


@dataclass(frozen=True)
class ProjectInfo:
    """Information about a discovered project."""

    id: str
    display_name: str
    current_path: Path
    historical_aliases: tuple[Path, ...]
    detection_source: str
    linked_session_count: int
    is_git_root: bool
    registered_at: datetime

    @property
    def name(self) -> str:
        """Backward-compatible alias for the display name."""
        return self.display_name

    @property
    def path(self) -> Path:
        """Backward-compatible alias for the current path."""
        return self.current_path

    def to_dict(self) -> dict[str, Any]:
        aliases = [str(alias) for alias in self.historical_aliases]
        payload = {
            "id": self.id,
            "display_name": self.display_name,
            "current_path": str(self.current_path),
            "historical_aliases": aliases,
            "detection_source": self.detection_source,
            "linked_session_count": self.linked_session_count,
            "is_git_root": self.is_git_root,
            "registered_at": self.registered_at.isoformat(),
        }
        payload.update(
            {
                "name": self.display_name,
                "path": str(self.current_path),
                "aliases": aliases,
            }
        )
        return payload


@dataclass
class PlanInfo:
    """Information about a plan file."""

    name: str
    path: Path
    status: PlanStatus
    checkpoint_count: int
    unchecked_count: int
    is_complete: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": str(self.path),
            "status": self.status.value,
            "checkpoint_count": self.checkpoint_count,
            "unchecked_count": self.unchecked_count,
            "is_complete": self.is_complete,
        }


@dataclass
class ExecutionRequest:
    """Request to start a workflow execution."""

    project_id: str
    plan_path: str
    workflow_name: str | None = None
    team: str | None = None
    start_step: str | None = None
    max_turns: int | None = None
    extra_instructions: str | None = None


@dataclass
class ExecutionStatus:
    """Status of a workflow execution."""

    run_id: str
    project_id: str
    plan_path: str
    workflow_name: str | None
    status: str
    turns_completed: int
    current_step: str | None
    started_at: datetime
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "project_id": self.project_id,
            "plan_path": self.plan_path,
            "workflow_name": self.workflow_name,
            "status": self.status,
            "turns_completed": self.turns_completed,
            "current_step": self.current_step,
            "started_at": self.started_at.isoformat(),
            "error": self.error,
        }


class CanonicalTransportModel(BaseModel):
    """Strict Pydantic view of a versioned control-plane value."""

    model_config = ConfigDict(extra="forbid")

    @classmethod
    def from_canonical(cls, value: Any):
        return cls.model_validate(value.to_dict())


class CapabilityResponse(CanonicalTransportModel):
    schema_version: int
    workflows: tuple[str, ...]
    teams: tuple[str, ...]
    roles: tuple[str, ...]
    controls: tuple[str, ...]
    context_levels: tuple[Literal["lite", "full"], ...]
    team_upgrade_chains: Mapping[str, tuple[str, ...]]
    control_safety: Mapping[str, Literal["safe", "restart_required"]]
    service_features: tuple[str, ...]


class RunStatusResponse(CanonicalTransportModel):
    run_id: str
    status: str
    schema_version: int
    ownership: Literal["control_plane", "legacy"]
    revision: int
    reason: str | None = None
    unit_name: str | None = None
    launch_phase: str | None = None
    workflow_name: str | None = None
    team: str | None = None
    current_step: str | None = None
    turns_completed: int | None = None
    max_turns: int | None = None
    evidence: Mapping[str, Any]


class StartRunResponse(CanonicalTransportModel):
    run_id: str
    created: bool
    status: str
    schema_version: int
    manifest_path: str | None = None
    reason: str | None = None


class StartupQuestionResponse(CanonicalTransportModel):
    question_id: str
    kind: str
    message: str
    options: Mapping[str, str]
    choices: tuple[str, ...]
    run_id: str | None = None
    schema_version: int


class StartResponse(CanonicalTransportModel):
    result: StartRunResponse | None = None
    startup_question: StartupQuestionResponse | None = None


class RunControlPayload(CanonicalTransportModel):
    expected_revision: int = Field(ge=0)
    schema_version: int = 1
    max_turns: int | None = Field(default=None, ge=1)
    owner_stop: bool | None = None
    team: str | None = None
    role_selectors: Mapping[str, str] = Field(default_factory=dict)
    unsafe_changes: Mapping[str, object] = Field(default_factory=dict)

    def to_canonical(self) -> RunControlRequest:
        return RunControlRequest(**self.model_dump())


class ControlResponse(CanonicalTransportModel):
    revision: int
    changed: bool
    owner_stop: bool
    run: RunStatusResponse


class ContextResponse(CanonicalTransportModel):
    run_id: str
    level: Literal["lite", "full"]
    data: Mapping[str, Any]
    schema_version: int


class EventResponse(CanonicalTransportModel):
    sequence: int
    event_type: str
    data: Mapping[str, Any]
    schema_version: int
    timestamp: str


class EventTailResponse(CanonicalTransportModel):
    events: tuple[EventResponse, ...]


class ProjectResponse(CanonicalTransportModel):
    project_id: str
    root: str
    schema_version: int


class ProjectListResponse(CanonicalTransportModel):
    projects: tuple[ProjectResponse, ...]


class GlobalCapabilitiesResponse(CanonicalTransportModel):
    projects: Mapping[str, CapabilityResponse]


class ReadinessResponse(CanonicalTransportModel):
    ready: bool
    projects: tuple[str, ...]


class PlanResponse(CanonicalTransportModel):
    path: str
    status: str
    modified_at: str
    schema_version: int


class PlanListResponse(CanonicalTransportModel):
    plans: tuple[PlanResponse, ...]


class RunListResponse(CanonicalTransportModel):
    runs: tuple[RunStatusResponse, ...]
    next_cursor: str | None = None
    schema_version: int


class StartRunPayload(CanonicalTransportModel):
    plan_path: str = Field(min_length=1, max_length=512)
    workflow_name: str | None = Field(default=None, max_length=128)
    team: str | None = Field(default=None, max_length=128)
    start_step: str | None = Field(default=None, max_length=128)
    max_turns: int | None = Field(default=None, ge=1)


class StartupAnswerPayload(CanonicalTransportModel):
    answer: str | int | bool


class OwnerStopPayload(CanonicalTransportModel):
    expected_revision: int = Field(ge=0)


def canonical_contract_payloads() -> dict[str, dict[str, Any]]:
    """Expose field names used by API tests to guard canonical-model drift."""
    samples = {
        "capability": CapabilitySet(),
        "run": RunStatus(run_id="sample", status="manifest_only"),
        "start": StartRunResult(run_id="sample", created=False, status="manifest_only"),
        "control": RunControlRequest(expected_revision=0),
        "context": ContextBundle(run_id="sample", level="lite", data={}),
        "event": RunEvent(sequence=1, event_type="sample", data={}),
        "question": StartupQuestionRecord(
            question_id="startup-sample-q1", kind="pick_step", message="Select a step"
        ),
    }
    transports: dict[str, type[CanonicalTransportModel]] = {
        "capability": CapabilityResponse,
        "run": RunStatusResponse,
        "start": StartRunResponse,
        "control": RunControlPayload,
        "context": ContextResponse,
        "event": EventResponse,
        "question": StartupQuestionResponse,
    }
    return {
        name: transports[name].from_canonical(value).model_dump(mode="json")
        for name, value in samples.items()
    }
