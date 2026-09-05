export interface ProjectInfo {
  id: string
  display_name: string
  current_path: string
  is_git_root: boolean
  registered_at: string
  readiness: 'ready' | 'configuration_required' | 'blocked'
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

/** Versioned REST views returned by the daemon-backed control plane. */
export interface ControlPlaneCapabilities {
  schema_version: number
  workflows: string[]
  teams: string[]
  roles: string[]
  controls: string[]
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

export interface RunStatus {
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
  evidence: Record<string, unknown>
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

export interface RunContext {
  run_id: string
  level: 'lite' | 'full'
  data: Record<string, unknown>
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
}

export interface StartRunResponse {
  result: StartRunResult | null
  startup_question: StartupQuestion | null
}

export interface RunControlRequest {
  expected_revision: number
  max_turns?: number
  team?: string
  role_selectors?: Record<string, string>
}

export interface ControlResponse {
  revision: number
  changed: boolean
  owner_stop: boolean
  run: RunStatus
}
