"""API models for the remote app server."""

from __future__ import annotations

from typing import Annotated, Any, Literal, Mapping

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


class GuidedActionBase(CanonicalTransportModel):
    """Base for the closed, discriminated guided-config action set."""

    type: str


class BuildStarterAction(GuidedActionBase):
    type: Literal["build_starter"]
    workflow: str = Field(min_length=1, max_length=64)
    main_branch: str = Field(min_length=1, max_length=128)
    team: str | None = Field(default=None, min_length=1, max_length=64)


class SetDefaultWorkflowAction(GuidedActionBase):
    type: Literal["set_default_workflow"]
    value: str = Field(min_length=1, max_length=64)


class SetMaxTurnsAction(GuidedActionBase):
    type: Literal["set_max_turns"]
    value: int | None = Field(default=None, ge=1)


class UpsertProfileAction(GuidedActionBase):
    type: Literal["upsert_profile"]
    harness: str = Field(min_length=1, max_length=64)
    profile: str = Field(min_length=1, max_length=64)
    model: str | None = Field(default=None, min_length=1, max_length=128)
    effort: str | None = Field(default=None, min_length=1, max_length=64)


class SetGlobalRoleAction(GuidedActionBase):
    type: Literal["set_global_role"]
    role: str = Field(min_length=1, max_length=64)
    selector: str = Field(min_length=1, max_length=192)


class AddTeamAction(GuidedActionBase):
    type: Literal["add_team"]
    team: str = Field(min_length=1, max_length=64)


class SetTeamRoleAction(GuidedActionBase):
    type: Literal["set_team_role"]
    team: str = Field(min_length=1, max_length=64)
    role: str = Field(min_length=1, max_length=64)
    selector: str = Field(min_length=1, max_length=192)


class SetWorkflowDefaultTeamAction(GuidedActionBase):
    type: Literal["set_workflow_default_team"]
    workflow: str = Field(min_length=1, max_length=64)
    team: str | None = Field(default=None, min_length=1, max_length=64)


GuidedConfigAction = Annotated[
    BuildStarterAction
    | SetDefaultWorkflowAction
    | SetMaxTurnsAction
    | UpsertProfileAction
    | SetGlobalRoleAction
    | AddTeamAction
    | SetTeamRoleAction
    | SetWorkflowDefaultTeamAction,
    Field(discriminator="type"),
]


class ProjectConfigFormPayload(CanonicalTransportModel):
    """Strict form request: candidate pair plus zero or one typed action.

    No ``expected_revision`` is accepted; the existing ``PUT /config`` remains
    the only save boundary.
    """

    aflow_toml: str
    workflows_toml: str
    action: GuidedConfigAction | None = None


class GuidedProfileSummary(CanonicalTransportModel):
    model: str | None = None
    effort: str | None = None


class GuidedWorkflowStepSummaries(CanonicalTransportModel):
    declared_steps: tuple[str, ...]
    first_step: str | None = None
    executable_steps: tuple[str, ...] | None = None
    first_executable_step: str | None = None


class GuidedTeamSummary(CanonicalTransportModel):
    roles: Mapping[str, str]


class GuidedFormProjection(CanonicalTransportModel):
    default_workflow: str | None = None
    max_turns: int | None = None
    harnesses: Mapping[str, Mapping[str, GuidedProfileSummary]]
    roles: Mapping[str, str]
    teams: Mapping[str, GuidedTeamSummary]
    workflow_default_teams: Mapping[str, str | None]
    workflows: Mapping[str, GuidedWorkflowStepSummaries]


class GuidedConfiguredChoices(CanonicalTransportModel):
    harnesses: tuple[str, ...]
    profiles: Mapping[str, tuple[str, ...]]
    selectors: tuple[str, ...]
    roles: tuple[str, ...]
    teams: tuple[str, ...]
    workflows: tuple[str, ...]


class GuidedHarnessSuggestion(CanonicalTransportModel):
    name: str
    supports_effort: bool
    custom_model_supported: bool


class GuidedProfileSuggestion(CanonicalTransportModel):
    harness: str
    profile: str
    model: str | None = None
    effort: str | None = None


class GuidedSuggestions(CanonicalTransportModel):
    label: str = "suggestion"
    harnesses: tuple[GuidedHarnessSuggestion, ...]
    profiles: tuple[GuidedProfileSuggestion, ...]
    note: str


class GuidedStarterDefaults(CanonicalTransportModel):
    workflow: str
    team: None = None
    main_branch: str
    main_branch_source: Literal["git_head", "fallback"]


class ProjectConfigFormResponse(CanonicalTransportModel):
    aflow_toml: str
    workflows_toml: str
    changed: bool
    validation: ConfigValidationModel
    form: GuidedFormProjection | None
    syntax_issues: tuple[ConfigValidationIssueModel, ...]
    choices: GuidedConfiguredChoices
    suggestions: GuidedSuggestions
    starter_defaults: GuidedStarterDefaults | None = None


class ProjectDiscoveryCandidateModel(CanonicalTransportModel):
    relative_path: str
    display_name: str
    registered_project_id: str | None = None
    addable: bool
    add_blocker: str | None = None


class ProjectDiscoveryResponse(CanonicalTransportModel):
    schema_version: int
    managed_root: str
    candidates: tuple[ProjectDiscoveryCandidateModel, ...]
    visited_entries: int
    skipped_unreadable: int
    truncated: bool
    limits: Mapping[str, int]


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
