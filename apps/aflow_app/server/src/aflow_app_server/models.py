"""API models for the remote app server."""

from __future__ import annotations

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


class CanonicalTransportModel(BaseModel):
    """Strict Pydantic view of a versioned control-plane value."""

    model_config = ConfigDict(extra="forbid")

    @classmethod
    def from_canonical(cls, value: Any):
        return cls.model_validate(value.to_dict())


class WorkflowCapabilityResponse(CanonicalTransportModel):
    declared_steps: tuple[str, ...]
    executable_steps: tuple[str, ...]
    excluded_steps: tuple[str, ...]
    first_step: str | None = None
    default_team: str | None = None


class CapabilityResponse(CanonicalTransportModel):
    schema_version: int
    workflows: tuple[str, ...]
    teams: tuple[str, ...]
    roles: tuple[str, ...]
    controls: tuple[str, ...]
    workflow_details: Mapping[str, WorkflowCapabilityResponse]
    admitted_role_selectors: Mapping[str, tuple[str, ...]]
    status_values: tuple[str, ...]
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
    selected_start_step: str | None = None
    skipped_steps: tuple[str, ...] = ()
    restarted_from_run_id: str | None = None
    evidence: Mapping[str, Any]


class StartRunResponse(CanonicalTransportModel):
    run_id: str
    created: bool
    status: str
    schema_version: int
    manifest_path: str | None = None
    reason: str | None = None
    restarted_from_run_id: str | None = None


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
    project_errors: Mapping[str, str] = Field(default_factory=dict)


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
    extra_instructions: tuple[str, ...] = Field(default=(), max_length=8)
    restarted_from_run_id: str | None = Field(default=None, max_length=64)


class StartupAnswerPayload(CanonicalTransportModel):
    answer: str | int | bool


class OwnerStopPayload(CanonicalTransportModel):
    expected_revision: int = Field(ge=0)


class ConfigValidationIssueModel(CanonicalTransportModel):
    document: str | None = None
    line: int | None = None
    message: str


class ConfigValidationModel(CanonicalTransportModel):
    state: Literal["ready", "configuration_required", "invalid"]
    issues: tuple[ConfigValidationIssueModel, ...]
    placeholders: tuple[str, ...]
    workflows: tuple[str, ...]
    teams: tuple[str, ...]
    roles: tuple[str, ...]


class ProjectConfigResponse(CanonicalTransportModel):
    project_id: str
    revision: str
    documents: tuple[str, ...]
    aflow_toml: str
    workflows_toml: str
    validation: ConfigValidationModel


class ProjectConfigSavePayload(CanonicalTransportModel):
    aflow_toml: str
    workflows_toml: str
    expected_revision: str = Field(pattern=r"^[0-9a-f]{64}$")


class ProjectConfigValidatePayload(CanonicalTransportModel):
    aflow_toml: str
    workflows_toml: str


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
