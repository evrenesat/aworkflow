"""API models for the remote app server."""

from __future__ import annotations

from typing import Annotated, Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator
from pydantic_core import core_schema

from aflow.control_plane import (
    CapabilitySet,
    ContextBundle,
    RunControlRequest,
    RunEvent,
    RunStatus,
    StartRunResult,
    StartupQuestionRecord,
    WorktreePreflightResult,
)
from aflow.skill_catalog import BUNDLED_SKILL_NAMES


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
    activity: Literal["active", "inactive", "unknown"] = "unknown"
    status_reason_code: str = "activity_unknown"
    history_state: Literal["visible", "archived", "deleted"] = "visible"
    history_revision: int = 0
    worker_exit: Mapping[str, Any] | None = None
    run_id: str
    status: str
    schema_version: int
    ownership: Literal["control_plane", "legacy"]
    revision: int
    reason: str | None = None
    plan_path: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
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


class WorktreeStatusItemResponse(CanonicalTransportModel):
    path: str
    index_status: str
    worktree_status: str
    original_path: str | None = None


class WorktreePreflightResponse(CanonicalTransportModel):
    checkout_path: str
    execution_mode: Literal["same_checkout", "new_worktree"]
    dirty: bool
    requires_confirmation: bool
    blockers: tuple[str, ...]
    total_items: int
    offset: int
    limit: int
    next_offset: int | None = None
    items: tuple[WorktreeStatusItemResponse, ...]


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
    dirty_worktree_confirmed: StrictBool = False


class ResumeRunPayload(CanonicalTransportModel):
    """Optional instruction replacement or explicit durable recovery request."""

    extra_instructions: tuple[str, ...] | None = Field(default=None, max_length=8)
    recovery: Mapping[str, object] | None = Field(
        default=None,
        description=(
            "Explicit durable-evidence recovery: mode must be "
            "'durable_evidence' and worker_selector must name a configured "
            "replacement worker."
        ),
    )


class PreflightRunPayload(StartRunPayload):
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=200, ge=1, le=1_000)


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


class SetTeamUpgradeAction(GuidedActionBase):
    """Link one team to the next quality-upgrade stage; null removes the link."""

    type: Literal["set_team_upgrade"]
    team: str = Field(min_length=1, max_length=64)
    upgrade_to: str | None = Field(default=None, min_length=1, max_length=64)


class SetWorkflowDefaultTeamAction(GuidedActionBase):
    type: Literal["set_workflow_default_team"]
    workflow: str = Field(min_length=1, max_length=64)
    team: str | None = Field(default=None, min_length=1, max_length=64)


class SetDefaultManagerEnabledAction(GuidedActionBase):
    """Set or delete the `[workflow].manager_enabled` default declaration.

    ``value=None`` deletes only that flag; ``True``/``False`` writes it at
    the exact table. ``StrictBool`` rejects non-boolean coercion (``1``,
    ``"true"``) at the contract boundary.
    """

    type: Literal["set_default_manager_enabled"]
    value: StrictBool | None


class SetWorkflowManagerEnabledAction(GuidedActionBase):
    """Set or delete one workflow's declared ``manager_enabled`` override.

    ``value=None`` deletes only that workflow's flag so it inherits again;
    inherited values are never written back into the workflow table.
    """

    type: Literal["set_workflow_manager_enabled"]
    workflow: str = Field(min_length=1, max_length=64)
    value: StrictBool | None


class SetPromptAction(GuidedActionBase):
    type: Literal["set_prompt"]
    name: str = Field(min_length=1, max_length=128)
    text: str | None = Field(max_length=262144)


class RenamePromptAction(GuidedActionBase):
    type: Literal["rename_prompt"]
    name: str = Field(min_length=1, max_length=128)
    new_name: str = Field(min_length=1, max_length=128)


class SetRolePromptAction(GuidedActionBase):
    type: Literal["set_role_prompt"]
    role: str = Field(min_length=1, max_length=64)
    team: str | None = Field(default=None, min_length=1, max_length=64)
    text: str | None = Field(max_length=262144)


class MoveRolePromptAction(GuidedActionBase):
    type: Literal["move_role_prompt"]
    role: str = Field(min_length=1, max_length=64)
    team: str | None = Field(default=None, min_length=1, max_length=64)
    target_role: str = Field(min_length=1, max_length=64)
    target_team: str | None = Field(default=None, min_length=1, max_length=64)


GuidedConfigAction = Annotated[
    BuildStarterAction
    | SetDefaultWorkflowAction
    | SetMaxTurnsAction
    | UpsertProfileAction
    | SetGlobalRoleAction
    | AddTeamAction
    | SetTeamRoleAction
    | SetTeamUpgradeAction
    | SetWorkflowDefaultTeamAction
    | SetDefaultManagerEnabledAction
    | SetWorkflowManagerEnabledAction
    | SetPromptAction
    | RenamePromptAction
    | SetRolePromptAction
    | MoveRolePromptAction,
    Field(discriminator="type"),
]


class GlobalConfigPatchPayload(CanonicalTransportModel):
    expected_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    actions: list[GuidedConfigAction] | None = Field(default=None, min_length=1, max_length=256)
    documents: dict[Literal["aflow.toml", "workflows.toml"], str] | None = None

    @model_validator(mode="after")
    def one_mode(self):
        if (self.actions is None) == (self.documents is None) or self.documents == {}:
            raise ValueError("provide actions or a nonempty documents map, exclusively")
        return self


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
    # Exact declared role per materialized executable step; None when the
    # production loader could not materialize the workflow.
    step_roles: Mapping[str, str] | None = None
    # Declared manager_enabled override (None when omitted/inherited).
    manager_enabled: bool | None = None
    # Canonical resolved value: declared override → concrete base → defaults
    # → False. Omitted defaults resolve to False, never None.
    effective_manager_enabled: bool = False
    # Where the effective value came from: "workflow" (explicit override),
    # "base:<name>" (alias inherits its concrete base), or "defaults".
    manager_enabled_source: str = "defaults"


class GuidedTeamSummary(CanonicalTransportModel):
    roles: Mapping[str, str]
    prompts: Mapping[str, str] = Field(default_factory=dict)
    upgrade_to: str | None = None


class GuidedTemplateVariable(CanonicalTransportModel):
    """Read-only editor help for one production prompt substitution."""

    token: str
    description: str
    scope: str
    absent_value: str
    example: str
    applicable_prompt_types: tuple[str, ...]


class GuidedFormProjection(CanonicalTransportModel):
    prompts: Mapping[str, str] = Field(default_factory=dict)
    role_prompts: Mapping[str, str] = Field(default_factory=dict)
    prompt_usages: Mapping[str, tuple[str, ...]] = Field(default_factory=dict)
    template_variables: tuple[GuidedTemplateVariable, ...] = Field(default_factory=tuple)
    default_workflow: str | None = None
    max_turns: int | None = None
    harnesses: Mapping[str, Mapping[str, GuidedProfileSummary]]
    roles: Mapping[str, str]
    teams: Mapping[str, GuidedTeamSummary]
    workflow_default_teams: Mapping[str, str | None]
    workflows: Mapping[str, GuidedWorkflowStepSummaries]
    # Declared `[workflow].manager_enabled` default (None when omitted).
    default_manager_enabled: bool | None = None


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


class SkillLinkStatusModel(CanonicalTransportModel):
    """Link state of one skill at one known mapped harness destination."""

    destination: str
    harnesses: tuple[str, ...]
    detected_harnesses: tuple[str, ...]
    linked: bool


class SkillSummaryModel(CanonicalTransportModel):
    """One registered bundled skill: revision, edit state, and link status."""

    name: str
    default: bool
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    source: Literal["bundled", "saved"]
    edited: bool
    installed: bool
    links: tuple[SkillLinkStatusModel, ...]
    detected_harnesses: tuple[str, ...]


class SkillDetailModel(SkillSummaryModel):
    """One skill entry plus its effective SKILL.md content."""

    content: str = Field(max_length=2_000_000)


class SkillSavePayload(CanonicalTransportModel):
    """Save exactly one canonical SKILL.md; no paths, scripts, or homes."""

    content: str = Field(max_length=2_000_000)
    expected_revision: str = Field(pattern=r"^[0-9a-f]{64}$")


class SurrogateTolerantStr(str):
    """A `str` that validation accepts even with lone surrogates.

    Pydantic-core's default `str` validator rejects lone surrogates
    (`string_unicode`), which would keep unencodable skill content from ever
    reaching read-only validation. The skills validate endpoint must report
    such content as a bounded per-entry `skill_invalid` verdict instead, so
    this type preserves the `str` contract (including the 2_000_000-character
    limit) while deferring encodability checks to the service layer.
    """

    _MAX_LENGTH = 2_000_000

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type, handler):
        return core_schema.no_info_plain_validator_function(
            cls._validate,
            serialization=core_schema.plain_serializer_function_ser_schema(
                str, return_schema=core_schema.str_schema()
            ),
        )

    @classmethod
    def __get_pydantic_json_schema__(cls, schema, handler):
        return {"type": "string", "maxLength": cls._MAX_LENGTH}

    @classmethod
    def _validate(cls, value):
        if not isinstance(value, str):
            raise ValueError("Input should be a valid string")
        if len(value) > cls._MAX_LENGTH:
            raise ValueError(
                f"String should have at most {cls._MAX_LENGTH} characters"
            )
        return cls(value)


class SkillValidateEntryModel(CanonicalTransportModel):
    name: str = Field(min_length=1, max_length=255)
    content: SurrogateTolerantStr
    expected_revision: str = Field(min_length=1, max_length=64)


class SkillValidatePayload(CanonicalTransportModel):
    entries: list[SkillValidateEntryModel] = Field(
        min_length=1, max_length=len(BUNDLED_SKILL_NAMES)
    )

    @model_validator(mode="after")
    def no_duplicate_names(self):
        names = [entry.name for entry in self.entries]
        if len(set(names)) != len(names):
            raise ValueError("duplicate skill names are not allowed")
        return self


class SkillValidateEntryResult(CanonicalTransportModel):
    name: str
    ok: bool
    current_revision: str | None = None
    error_code: str | None = None
    error: str | None = None


class SkillValidateResponse(CanonicalTransportModel):
    entries: tuple[SkillValidateEntryResult, ...]


class SkillRefreshModel(CanonicalTransportModel):
    name: str
    status: str
    changed: bool
    edited: bool
    error: str | None = None


class SkillInstallOperationModel(CanonicalTransportModel):
    harness: str
    skill: str
    destination: str
    status: str
    error_code: str | None = None
    error: str | None = None
    displaced_path: str | None = None


class SkillInstallResponse(CanonicalTransportModel):
    mode: str
    succeeded: bool
    cancelled: bool
    refresh: tuple[SkillRefreshModel, ...]
    operations: tuple[SkillInstallOperationModel, ...]


class SkillInstallPayload(CanonicalTransportModel):
    """Empty install body: the shared default auto installer decides targets."""

    confirm: Literal[True] | None = None


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
        "preflight": WorktreePreflightResult(
            checkout_path="/tmp/project",
            execution_mode="same_checkout",
            dirty=False,
            requires_confirmation=False,
            blockers=(),
            total_items=0,
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
        "preflight": WorktreePreflightResponse,
    }
    return {
        name: transports[name].from_canonical(value).model_dump(mode="json")
        for name, value in samples.items()
    }
