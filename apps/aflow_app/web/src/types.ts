export interface ProjectInfo {
  id: string
  display_name: string
  current_path: string
  historical_aliases: string[]
  detection_source: string
  linked_session_count: number
  is_git_root: boolean
  registered_at: string
  name?: string
  path?: string
  aliases?: string[]
}

export interface PlanInfo {
  name: string
  path: string
  status: 'draft' | 'in_progress'
  checkpoint_count: number
  unchecked_count: number
  is_complete: boolean
}

export type ProviderState = 'starting' | 'ready' | 'degraded' | 'unavailable' | 'disabled'
export type SessionStatus = 'idle' | 'running' | 'waiting_for_approval' | 'failed' | 'archived' | 'unknown'
export type TurnStatus = 'pending' | 'running' | 'waiting_for_approval' | 'completed' | 'failed' | 'interrupted'
export type AttachmentKind = 'file' | 'image'

export interface PlanningError {
  code: string
  message: string
  provider_id: string | null
  retryable: boolean
}

export interface ProviderCapabilities {
  models: string[]
  reasoning_levels: string[]
  reasoning_summaries: string[]
  attachments: boolean
  attachment_kinds: AttachmentKind[]
  output_schema: boolean
  fork: boolean
  archive: boolean
  approvals: boolean
  interruption: boolean
  compaction: boolean
  rollback: boolean
}

export interface ProviderReadiness {
  provider_id: string
  display_name: string
  state: ProviderState
  capabilities: ProviderCapabilities
  error: PlanningError | null
}

export interface SessionKey {
  provider_id: string
  provider_session_id: string
}

export interface TurnItem {
  type?: string
  [key: string]: unknown
}

export interface PlanningTurn {
  turn_id: string
  status: TurnStatus
  items: TurnItem[]
  error: PlanningError | null
  created_at: string | null
  completed_at: string | null
  attachment_ids: string[]
}

export interface PlanningSession {
  key: SessionKey
  project_id: string | null
  cwd: string
  title: string | null
  preview: string
  status: SessionStatus
  model: string | null
  reasoning_level: string | null
  archived: boolean
  created_at: string | null
  updated_at: string | null
  turns: PlanningTurn[]
}

export interface PlanningSessionPage {
  sessions: PlanningSession[]
  providers: ProviderReadiness[]
  next_cursor: string | null
}

export interface ProviderModels {
  provider_id: string
  models: string[]
}

export interface ReasoningOptions {
  provider_id: string
  reasoning_levels: string[]
  reasoning_summaries: string[]
}

export interface Attachment {
  attachment_id: string
  filename: string
  kind: AttachmentKind
  media_type: string | null
  size_bytes: number
  created_at: string | null
}

export interface PendingApproval {
  approval_id: string
  key: SessionKey
  turn_id: string
  kind: 'command' | 'file_change'
  reason: string | null
}

export interface StartTurnRequest {
  text: string
  attachment_ids?: string[]
  model?: string
  reasoning_level?: string
  reasoning_summary?: string
}

export interface ExecutionStatus {
  run_id: string
  project_id: string
  plan_path: string
  workflow_name: string | null
  status: string
  turns_completed: number
  current_step: string | null
  started_at: string
  error: string | null
}

export interface ExecutionEvent {
  type: 'run_started' | 'turn_started' | 'turn_finished' | 'status_update' | 'run_completed' | 'run_failed'
  data: Record<string, unknown>
  timestamp: string
}
