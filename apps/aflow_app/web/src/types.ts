export type ProjectReadiness = 'ready' | 'configuration_required' | 'blocked'

export interface ProjectInfo {
  id: string
  display_name: string
  current_path: string
  is_git_root: boolean
  registered_at: string
  readiness: ProjectReadiness
  parent_project_id?: string | null
}

export interface ProjectCreateRequest {
  mode: 'create' | 'register'
  path: string
  display_name?: string | null
  main_branch?: string
  initialize_git?: boolean
}

export interface ProjectCreateResult {
  id: string
  display_name: string
  relative_root: string
  root: string
  created_at: string
  readiness: ProjectReadiness
}

export interface ProjectDiscoveryCandidate {
  relative_path: string
  display_name: string
  registered_project_id: string | null
  addable: boolean
  add_blocker: string | null
}

export interface ProjectDiscovery {
  schema_version: number
  managed_root: string
  candidates: ProjectDiscoveryCandidate[]
  visited_entries: number
  skipped_unreadable: number
  truncated: boolean
  limits: {
    max_visited_entries: number
    max_candidates: number
  }
}

export interface ConfigValidationIssue {
  document: string | null
  line: number | null
  message: string
}

export interface ConfigValidation {
  state: 'ready' | 'configuration_required' | 'invalid'
  issues: ConfigValidationIssue[]
  placeholders: string[]
  workflows: string[]
  teams: string[]
  roles: string[]
}

export interface ProjectConfig {
  project_id: string
  revision: string
  documents: string[]
  aflow_toml: string
  workflows_toml: string
  validation: ConfigValidation
}

export interface ProjectConfigSaveRequest {
  aflow_toml: string
  workflows_toml: string
  expected_revision: string
}

export interface ProjectConfigValidateRequest {
  aflow_toml: string
  workflows_toml: string
}

/** Global transport settings (config.toml); the credential is write-only. */
export interface SettingsResponse {
  bind_host: string
  bind_port: number
  managed_projects_root: string
  password_set: boolean
  revision: string
  advanced_toml: string
  restart: {
    bind_host: boolean
    bind_port: boolean
    managed_projects_root: boolean
  }
}

export interface SettingsSaveRequest {
  expected_revision: string
  advanced_toml?: string | null
  managed_projects_root?: string | null
  bind_host?: string | null
  bind_port?: number | null
  password?: string | null
}

export interface ConfigBlockedRun {
  run_id: string
  status: string
}

/** Guided-config contract mirroring the server's pure form endpoint. */
export interface GuidedProfileSummary {
  model: string | null
  effort: string | null
}

export interface GuidedWorkflowStepSummaries {
  declared_steps: string[]
  first_step: string | null
  executable_steps: string[] | null
  first_executable_step: string | null
  /** Exact declared role per materialized executable step; null when unavailable. */
  step_roles?: Record<string, string> | null
  /** Declared manager_enabled override; null when omitted (inherits). */
  manager_enabled?: boolean | null
  /** Canonical resolved value: override → concrete base → defaults → false. */
  effective_manager_enabled?: boolean
  /** Where the effective value came from: "workflow", "base:<name>", or "defaults". */
  manager_enabled_source?: string
}

/**
 * One team's declared overrides plus the server-owned canonical projection.
 * The effective/provenance fields are optional for compatibility with older
 * saved responses and test fixtures; clients must never derive them locally.
 */
export interface GuidedTeamSummary {
  roles: Record<string, string>
  prompts?: Record<string, string>
  upgrade_to?: string | null
  extends?: string | null
  display_name?: string | null
  backup_team?: string | null
  effective_roles?: Record<string, string>
  effective_prompts?: Record<string, string>
  role_sources?: Record<string, string>
  prompt_sources?: Record<string, string>
}

export interface GuidedTemplateVariable {
  token: string
  description: string
  scope: string
  absent_value: string
  example: string
  applicable_prompt_types: string[]
}

export interface GuidedFormProjection {
  prompts?: Record<string, string>
  role_prompts?: Record<string, string>
  prompt_usages?: Record<string, string[]>
  /** Read-only help metadata; never included in guided save actions. */
  template_variables?: GuidedTemplateVariable[]
  default_workflow: string | null
  max_turns: number | null
  harnesses: Record<string, Record<string, GuidedProfileSummary>>
  roles: Record<string, string>
  teams: Record<string, GuidedTeamSummary>
  workflow_default_teams: Record<string, string | null>
  workflows: Record<string, GuidedWorkflowStepSummaries>
  /** Declared `[workflow].manager_enabled` default; null when omitted (disabled). */
  default_manager_enabled?: boolean | null
}

export interface GuidedConfiguredChoices {
  harnesses: string[]
  profiles: Record<string, string[]>
  selectors: string[]
  roles: string[]
  teams: string[]
  workflows: string[]
}

export interface GuidedHarnessSuggestion {
  name: string
  supports_effort: boolean
  custom_model_supported: boolean
}

export interface GuidedProfileSuggestion {
  harness: string
  profile: string
  model: string | null
  effort: string | null
}

export interface GuidedSuggestions {
  label: string
  harnesses: GuidedHarnessSuggestion[]
  profiles: GuidedProfileSuggestion[]
  note: string
}

export interface GuidedStarterDefaults {
  workflow: string
  team: null
  main_branch: string
  main_branch_source: 'git_head' | 'fallback'
}

export type GuidedConfigAction =
  | { type: 'set_prompt'; name: string; text: string | null }
  | { type: 'rename_prompt'; name: string; new_name: string }
  | { type: 'set_role_prompt'; role: string; team?: string | null; text: string | null }
  | { type: 'build_starter'; workflow: string; main_branch: string; team?: string | null }
  | { type: 'set_default_workflow'; value: string }
  | { type: 'set_max_turns'; value: number | null }
  | { type: 'upsert_profile'; harness: string; profile: string; model?: string | null; effort?: string | null }
  | { type: 'set_global_role'; role: string; selector: string }
  | { type: 'add_team'; team: string }
  | { type: 'set_team_role'; team: string; role: string; selector: string | null }
  | { type: 'set_team_base'; team: string; extends: string | null }
  | { type: 'set_team_display_name'; team: string; display_name: string | null }
  | { type: 'remove_team'; team: string }
  | { type: 'set_team_upgrade'; team: string; upgrade_to: string | null }
  | { type: 'set_workflow_default_team'; workflow: string; team: string | null }
  | { type: 'set_default_manager_enabled'; value: boolean | null }
  | { type: 'set_workflow_manager_enabled'; workflow: string; value: boolean | null }

export interface ProjectConfigFormRequest {
  aflow_toml: string
  workflows_toml: string
  action?: GuidedConfigAction | null
}

/** Bundled skill entries served by the account-local Skills API. */
export interface SkillLinkStatus {
  destination: string
  harnesses: string[]
  detected_harnesses: string[]
  linked: boolean
}

export interface SkillSummary {
  name: string
  default: boolean
  revision: string
  source: 'bundled' | 'saved'
  edited: boolean
  installed: boolean
  links: SkillLinkStatus[]
  detected_harnesses: string[]
}

export interface SkillDetail extends SkillSummary {
  content: string
}

export interface SkillSaveRequest {
  content: string
  expected_revision: string
}

export interface SkillValidateEntry {
  name: string
  content: string
  expected_revision: string
}

export interface SkillValidateEntryResult {
  name: string
  ok: boolean
  current_revision: string | null
  error_code: string | null
  error: string | null
}

export interface SkillRefreshEntry {
  name: string
  status: string
  changed: boolean
  edited: boolean
  error: string | null
}

export interface SkillInstallOperation {
  harness: string
  skill: string
  destination: string
  status: string
  error_code: string | null
  error: string | null
  displaced_path: string | null
}

export interface SkillInstallResult {
  mode: string
  succeeded: boolean
  cancelled: boolean
  refresh: SkillRefreshEntry[]
  operations: SkillInstallOperation[]
}

export interface ProjectConfigFormResponse {
  aflow_toml: string
  workflows_toml: string
  changed: boolean
  validation: ConfigValidation
  form: GuidedFormProjection | null
  syntax_issues: ConfigValidationIssue[]
  choices: GuidedConfiguredChoices
  suggestions: GuidedSuggestions
  starter_defaults: GuidedStarterDefaults | null
}

export type PlanStatus = 'todo' | 'in_progress' | 'done'

export interface PlanDocument {
  project_id: string
  name: string
  path: string
  status: PlanStatus
  revision: string
  size_bytes: number
  content?: string
}

export type PlanBackupKind = 'snapshot' | 'follow_up'
export type PlanBackupBaselineStatus = 'known' | 'unknown'

export interface PlanBackupSummary {
  backup_filename: string
  kind: PlanBackupKind | string
  baseline_status: PlanBackupBaselineStatus | string
  capture_event: string | null
  timestamp: string | null
  run_id: string | null
  turn_number: number | null
  content_sha256: string | null
}

export interface PlanBackupPage {
  backups: PlanBackupSummary[]
  offset: number
  limit: number
  next_offset: number | null
  total_items: number
}

/** Versioned REST views returned by the daemon-backed control plane. */
export interface WorkflowCapability {
  declared_steps: string[]
  executable_steps: string[]
  excluded_steps: string[]
  first_step: string | null
  default_team: string | null
}

export interface ControlPlaneCapabilities {
  schema_version: number
  workflows: string[]
  teams: string[]
  roles: string[]
  controls: string[]
  workflow_details: Record<string, WorkflowCapability>
  admitted_role_selectors: Record<string, string[]>
  context_levels: Array<'lite' | 'full'>
  team_upgrade_chains: Record<string, string[]>
  control_safety: Record<string, 'safe' | 'restart_required'>
  service_features: string[]
}

export interface ControlPlaneProject {
  project_id: string
  root: string
  schema_version: number
}

export interface ControlPlaneReadiness {
  ready: boolean
  projects: string[]
}

export interface WorktreeStatusItem {
  path: string
  index_status: string
  worktree_status: string
  original_path: string | null
}

export interface WorktreePreflight {
  checkout_path: string
  execution_mode: 'same_checkout' | 'new_worktree'
  dirty: boolean
  requires_confirmation: boolean
  blockers: string[]
  total_items: number
  offset: number
  limit: number
  next_offset: number | null
  items: WorktreeStatusItem[]
}

export interface RunStatus {
  activity?: 'active' | 'inactive' | 'unknown'
  status_reason_code?: string
  history_state?: 'visible' | 'archived' | 'deleted'
  history_revision?: number
  worker_exit?: { stage: string; reason: string | null; exit_code: number | null; exited_at: string | null; diagnostic_unavailable: boolean } | null
  plan_path?: string | null
  started_at?: string | null
  ended_at?: string | null
  run_id: string
  status: string
  schema_version: number
  ownership: 'control_plane' | 'legacy'
  revision: number
  reason: string | null
  unit_name: string | null
  launch_phase: string | null
  workflow_name: string | null
  team: string | null
  current_step: string | null
  turns_completed: number | null
  max_turns: number | null
  selected_start_step: string | null
  skipped_steps: string[]
  restarted_from_run_id: string | null
  evidence: Record<string, unknown>
  /** Additive canonical progress; absent/null remains valid for legacy runs. */
  progress?: RunProgressSummary | null
}

export interface RecoveryWorkerEvidence {
  schema_version: 1
  target_run_id: string
  target_selector: string
  operation_state: 'pending' | 'in_flight' | 'consumed'
  operation_started: boolean
  session_started: boolean
  session_status: 'active' | 'handed_over' | 'closed' | null
}

export interface RunPage {
  runs: RunStatus[]
  next_cursor: string | null
  schema_version: number
}

export interface RunEvent {
  sequence: number
  event_type: string
  data: Record<string, unknown>
  schema_version: number
  timestamp: string
}

export interface RunEventTail {
  events: RunEvent[]
}

export type RunProgressAvailability = 'available' | 'partial' | 'unavailable'
export type RunProgressReason = 'missing_plan' | 'unreadable_plan' | 'non_checkpoint_plan' | 'missing_scope' | 'invalid_evidence'

export interface RunProgressCheckpoint {
  index: number | null
  name: string | null
}

export interface RunProgressTurn {
  turn_number: number | null
  step: string | null
  status: string | null
  summary: string | null
}

/** Read-only observer projection; nullable fields mean the server has no evidence. */
export interface RunProgress {
  availability: RunProgressAvailability
  checkpoint: RunProgressCheckpoint | null
  total: number | null
  complete: boolean | null
  repairing: boolean
  overlay_path: string | null
  reason: RunProgressReason | null
  last_finished_turn: RunProgressTurn | null
  current_turn: RunProgressTurn | null
}

export type CanonicalRunProgressAvailability = 'complete' | 'partial' | 'unavailable' | 'not_applicable'
export type RunProgressCoverage = 'complete' | 'partial' | 'unavailable'
export type CanonicalRunProgressCheckpointStatus =
  | 'pending'
  | 'implementing'
  | 'reviewing'
  | 'repairing'
  | 'approved'
  | 'recorded_complete'
  | 'blocked'
  | 'unknown'
export type RunProgressDeliveryStatus = 'pending' | 'running' | 'succeeded' | 'failed' | 'unknown' | 'not_applicable'
export type RunProgressChangeStatus = 'applied' | 'pending' | 'failed' | 'unknown'
export type RunProgressEventAssociation = 'checkpoint' | 'whole_plan' | 'outside_returned' | 'unassigned'

export interface RunProgressCount {
  value: number | null
  coverage: RunProgressCoverage
}

export interface RunProgressExecutor {
  role: string | null
  team: string | null
  selector: string | null
  harness: string | null
  model: string | null
  model_display: string | null
  effort: string | null
  source_run_id: string | null
  invocation_id: string | null
  turn_number: number | null
  started_at: string | null
  ended_at: string | null
  duration_seconds: number | null
}

export interface RunProgressDetailCheckpoint {
  checkpoint_id: string | null
  ordinal: number | null
  title: string | null
  status: CanonicalRunProgressCheckpointStatus
  awaiting_review: boolean
  worker_attempts: RunProgressCount
  repair_passes: RunProgressCount
  reviews: RunProgressCount
  runtime_retries: RunProgressCount
  applied_upgrades: RunProgressCount
  recorded_at: string | null
  duration_seconds: number | null
  scope_id: string | null
  generation_id: string | null
  parent_checkpoint_id: string | null
  source_run_id: string | null
}

export interface RunProgressDetailEvent {
  event_id: string
  checkpoint_id: string | null
  scope_id: string | null
  source_run_id: string | null
  turn_number: number | null
  decision_number: number | null
  kind: string
  outcome: string | null
  executor: RunProgressExecutor | null
  started_at: string | null
  ended_at: string | null
  duration_seconds: number | null
  reason: string | null
  source_reference: Record<string, unknown> | null
  /** Optional for compatibility with older detail payloads. */
  association?: RunProgressEventAssociation
}

export interface RunProgressChange {
  change_id: string
  status: RunProgressChangeStatus
  kind: string
  roles: string[]
  old_team: string | null
  new_team: string | null
  old_selector: string | null
  new_selector: string | null
  old_model: string | null
  new_model: string | null
  old_effort: string | null
  new_effort: string | null
  turn_number: number | null
  checkpoint_id: string | null
  generation_id: string | null
  reason: string | null
  recorded_at: string | null
  source_reference: Record<string, unknown> | null
}

export interface RunProgressDeliveryStage {
  stage: string
  status: RunProgressDeliveryStatus
  recorded_at: string | null
  reason: string | null
  source_reference: Record<string, unknown> | null
}

export interface RunProgressTruncation {
  evidence_bytes: number
  records_read: number
  checkpoints_read: number
  events_read: number
  omitted_records: number
  omitted_checkpoints: number
  /** Exact portions omitted by the projection's response limits. */
  response_limit_records?: number
  response_limit_checkpoints?: number
  notices: string[]
}

export interface RunProgressSummary {
  schema_version: number
  availability: CanonicalRunProgressAvailability
  observed_at: string | null
  evidence_at: string | null
  reason_codes: string[]
  original_plan_identity: string | null
  original_plan_display_name: string | null
  original_plan_path: string | null
  total_checkpoints: RunProgressCount
  approved_checkpoints: RunProgressCount
  recorded_complete_checkpoints: RunProgressCount
  checkpoint_states?: Record<string, CanonicalRunProgressCheckpointStatus>
  current_checkpoint_id: string | null
  current_checkpoint_ordinal: number | null
  current_checkpoint_title: string | null
  activity: string | null
  phase: string | null
  run_status: string | null
  current_executor: RunProgressExecutor | null
  last_executor: RunProgressExecutor | null
  worker_attempts: RunProgressCount
  repair_passes: RunProgressCount
  reviews: RunProgressCount
  runtime_retries: RunProgressCount
  applied_upgrades: RunProgressCount
}

export interface RunProgressDetail extends RunProgressSummary {
  checkpoints: RunProgressDetailCheckpoint[]
  events: RunProgressDetailEvent[]
  applied_changes: RunProgressChange[]
  pending_changes: RunProgressChange[]
  delivery: RunProgressDeliveryStage[]
  truncation: RunProgressTruncation
}

export interface RunContextData extends Record<string, unknown> {
  /** Bounded compatibility projection for older execution-summary consumers. */
  execution_progress?: RunProgress | null
  /** Canonical history and approval projection. */
  progress?: RunProgressDetail | null
}

export interface RunContext {
  run_id: string
  level: 'lite' | 'full'
  data: RunContextData
  schema_version: number
}

export interface ControlPlanePlan {
  path: string
  status: string
  modified_at: string
  schema_version: number
}

export interface StartupQuestion {
  question_id: string
  kind: string
  message: string
  options: Record<string, string>
  choices: string[]
  run_id: string | null
  schema_version: number
}

export interface StartRunResult {
  run_id: string
  created: boolean
  status: string
  schema_version: number
  manifest_path: string | null
  reason: string | null
  restarted_from_run_id: string | null
}

/** Explicit replacement-worker recovery; omitted means ordinary resume. */
export interface RecoveryRequest {
  mode: 'durable_evidence'
  worker_selector: string
}

/** Optional body for the existing project-scoped resume route. */
export interface ResumeRunRequest {
  extra_instructions?: string[] | null
  recovery?: RecoveryRequest | null
}

export interface StartRunRequest {
  plan_path: string
  workflow_name?: string
  team?: string
  start_step?: string
  max_turns?: number
  extra_instructions?: string[]
  restarted_from_run_id?: string
  dirty_worktree_confirmed?: boolean
}

export interface StartRunResponse {
  result: StartRunResult | null
  startup_question: StartupQuestion | null
}

export interface RunControlRequest {
  expected_revision: number
  max_turns?: number
  owner_stop?: boolean
  team?: string
  role_selectors?: Record<string, string>
}

export interface ControlResponse {
  revision: number
  changed: boolean
  owner_stop: boolean
  run: RunStatus
}
