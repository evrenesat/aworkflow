import type {
  Attachment,
  AttachmentKind,
  ExecutionEvent,
  ExecutionStatus,
  PendingApproval,
  PlanInfo,
  PlanningSession,
  PlanningSessionPage,
  ProjectInfo,
  ProviderModels,
  ProviderReadiness,
  ReasoningOptions,
  SessionKey,
  StartTurnRequest,
  PlanningTurn,
} from './types'

const API_BASE = '/api'

class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message)
    this.name = 'ApiError'
  }
}

let authToken: string | null = null

export function setAuthToken(token: string) {
  authToken = token
  if (typeof window !== 'undefined') {
    localStorage.setItem('aflow_auth_token', token)
  }
}

export function getAuthToken(): string | null {
  if (authToken) return authToken
  if (typeof window !== 'undefined') {
    authToken = localStorage.getItem('aflow_auth_token')
  }
  return authToken
}

export function clearAuthToken() {
  authToken = null
  if (typeof window !== 'undefined') {
    localStorage.removeItem('aflow_auth_token')
  }
}

function getHeaders(includeJson = true): HeadersInit {
  const headers: HeadersInit = {
    ...(includeJson ? { 'Content-Type': 'application/json' } : {}),
  }
  const token = getAuthToken()
  if (token) {
    headers['Authorization'] = `Bearer ${token}`
  }
  return headers
}

async function fetchJson<T>(url: string, options: RequestInit = {}): Promise<T> {
  const includeJson = !(options.body instanceof FormData)
  const response = await fetch(url, {
    ...options,
    headers: {
      ...getHeaders(includeJson),
      ...options.headers,
    },
  })

  if (!response.ok) {
    const text = await response.text()
    let message = text
    try {
      const json = JSON.parse(text)
      const detail = json.detail || json.message
      message = typeof detail === 'string' ? detail : detail?.message || text
    } catch {
      // Use text as-is
    }
    throw new ApiError(response.status, message)
  }

  if (response.status === 204) {
    return undefined as T
  }

  return response.json()
}

function buildQuery(params: Record<string, string | number | boolean | string[] | undefined>): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined) continue
    if (Array.isArray(value)) {
      for (const item of value) {
        search.append(key, item)
      }
      continue
    }
    search.set(key, String(value))
  }
  const query = search.toString()
  return query ? `?${query}` : ''
}

export async function listProjects(): Promise<ProjectInfo[]> {
  return fetchJson<ProjectInfo[]>(`${API_BASE}/projects`)
}

export async function getProject(projectId: string): Promise<ProjectInfo> {
  return fetchJson<ProjectInfo>(`${API_BASE}/projects/${projectId}`)
}

export async function updateProject(
  projectId: string,
  request: { display_name?: string | null; current_path?: string | null; alias?: string | null }
): Promise<ProjectInfo> {
  return fetchJson<ProjectInfo>(`${API_BASE}/projects/${projectId}`, {
    method: 'PATCH',
    body: JSON.stringify(request),
  })
}

export async function listProjectPlans(projectId: string): Promise<PlanInfo[]> {
  return fetchJson<PlanInfo[]>(`${API_BASE}/projects/${projectId}/plans`)
}

function sessionPath(projectId: string, key: SessionKey): string {
  return `${API_BASE}/projects/${projectId}/planning/providers/${encodeURIComponent(key.provider_id)}/sessions/${encodeURIComponent(key.provider_session_id)}`
}

export async function listPlanningProviders(): Promise<ProviderReadiness[]> {
  const response = await fetchJson<{ providers: ProviderReadiness[] }>(`${API_BASE}/planning/providers`)
  return response.providers
}

export async function listProviderModels(providerId: string): Promise<ProviderModels> {
  return fetchJson<ProviderModels>(`${API_BASE}/planning/providers/${encodeURIComponent(providerId)}/models`)
}

export async function listReasoningOptions(providerId: string): Promise<ReasoningOptions> {
  return fetchJson<ReasoningOptions>(`${API_BASE}/planning/providers/${encodeURIComponent(providerId)}/reasoning-options`)
}

export async function listProjectSessions(
  projectId: string,
  request: { archived?: boolean } = {}
): Promise<PlanningSessionPage> {
  return fetchJson<PlanningSessionPage>(
    `${API_BASE}/projects/${projectId}/planning/sessions${buildQuery(request)}`
  )
}

export async function getProjectSession(
  projectId: string,
  key: SessionKey,
  includeTurns = true
): Promise<PlanningSession> {
  return fetchJson<PlanningSession>(`${sessionPath(projectId, key)}${buildQuery({ include_turns: includeTurns })}`)
}

export async function startProjectSession(
  projectId: string,
  request: { provider_id?: string; model?: string; reasoning_level?: string } = {}
): Promise<PlanningSession> {
  return fetchJson<PlanningSession>(`${API_BASE}/projects/${projectId}/planning/sessions`, {
    method: 'POST',
    body: JSON.stringify(request),
  })
}

export async function resumeProjectSession(projectId: string, key: SessionKey): Promise<PlanningSession> {
  return fetchJson<PlanningSession>(`${sessionPath(projectId, key)}/resume`, {
    method: 'POST',
  })
}

export async function forkProjectSession(projectId: string, key: SessionKey): Promise<PlanningSession> {
  return fetchJson<PlanningSession>(`${sessionPath(projectId, key)}/fork`, {
    method: 'POST',
  })
}

export async function setProjectSessionName(projectId: string, key: SessionKey, name: string): Promise<void> {
  return fetchJson<void>(sessionPath(projectId, key), {
    method: 'PATCH',
    body: JSON.stringify({ name }),
  })
}

export async function setProjectSessionArchived(
  projectId: string,
  key: SessionKey,
  archived: boolean
): Promise<{ archived: boolean }> {
  return fetchJson<{ archived: boolean }>(`${sessionPath(projectId, key)}/${archived ? 'archive' : 'unarchive'}`, {
    method: 'POST',
  })
}

export async function startProjectTurn(projectId: string, key: SessionKey, request: StartTurnRequest): Promise<PlanningTurn> {
  return fetchJson<PlanningTurn>(`${sessionPath(projectId, key)}/turns`, {
    method: 'POST',
    body: JSON.stringify(request),
  })
}

export async function interruptProjectTurn(projectId: string, key: SessionKey, turnId: string): Promise<void> {
  return fetchJson<void>(`${sessionPath(projectId, key)}/turns/${encodeURIComponent(turnId)}/interrupt`, { method: 'POST' })
}

export async function listPendingApprovals(projectId: string, key: SessionKey): Promise<PendingApproval[]> {
  const response = await fetchJson<{ approvals: PendingApproval[] }>(`${sessionPath(projectId, key)}/approvals`)
  return response.approvals
}

export async function respondToApproval(
  projectId: string,
  key: SessionKey,
  approvalId: string,
  decision: 'accept' | 'decline' | 'cancel'
): Promise<void> {
  return fetchJson<void>(`${sessionPath(projectId, key)}/approvals/${encodeURIComponent(approvalId)}`, {
    method: 'POST',
    body: JSON.stringify({ decision }),
  })
}

export async function listAttachments(projectId: string, key: SessionKey): Promise<Attachment[]> {
  const response = await fetchJson<{ attachments: Attachment[] }>(`${sessionPath(projectId, key)}/attachments`)
  return response.attachments
}

export async function uploadAttachment(
  projectId: string,
  key: SessionKey,
  file: File,
  kind: AttachmentKind
): Promise<Attachment> {
  const body = new FormData()
  body.append('file', file)
  body.append('kind', kind)
  return fetchJson<Attachment>(`${sessionPath(projectId, key)}/attachments`, {
    method: 'POST',
    body,
  })
}

export async function deleteAttachment(projectId: string, key: SessionKey, attachmentId: string): Promise<void> {
  return fetchJson<void>(`${sessionPath(projectId, key)}/attachments/${encodeURIComponent(attachmentId)}`, {
    method: 'DELETE',
  })
}

export async function listPlanDrafts(projectId: string): Promise<string[]> {
  return fetchJson<string[]>(`${API_BASE}/projects/${projectId}/plans/drafts`)
}

export async function savePlanDraft(
  projectId: string,
  request: { name: string; content: string }
): Promise<{ name: string; path: string; status: 'draft' }> {
  return fetchJson<{ name: string; path: string; status: 'draft' }>(`${API_BASE}/projects/${projectId}/plans/drafts`, {
    method: 'POST',
    body: JSON.stringify(request),
  })
}

export async function loadPlanDraft(projectId: string, name: string): Promise<{ name: string; content: string }> {
  return fetchJson<{ name: string; content: string }>(`${API_BASE}/projects/${projectId}/plans/drafts/${name}`)
}

export async function promotePlanDraft(
  projectId: string,
  request: { draft_name: string; target_name?: string | null }
): Promise<{ name: string; path: string; status: 'in_progress' }> {
  return fetchJson<{ name: string; path: string; status: 'in_progress' }>(`${API_BASE}/projects/${projectId}/plans/promote`, {
    method: 'POST',
    body: JSON.stringify(request),
  })
}

export async function deletePlanDraft(projectId: string, name: string): Promise<void> {
  return fetchJson<void>(`${API_BASE}/projects/${projectId}/plans/drafts/${name}`, {
    method: 'DELETE',
  })
}

export async function startExecution(request: {
  project_id: string
  plan_path: string
  workflow_name?: string
  team?: string
  start_step?: string
  max_turns?: number
  extra_instructions?: string
}): Promise<{ run_id: string }> {
  return fetchJson<{ run_id: string }>(`${API_BASE}/executions`, {
    method: 'POST',
    body: JSON.stringify(request),
  })
}

export async function getExecutionStatus(runId: string): Promise<ExecutionStatus> {
  return fetchJson<ExecutionStatus>(`${API_BASE}/executions/${runId}`)
}

export function subscribeToExecutionEvents(
  runId: string,
  onEvent: (event: ExecutionEvent) => void,
  onError?: (error: Error) => void
): () => void {
  const token = getAuthToken()
  const url = `${API_BASE}/executions/${runId}/events${token ? `?token=${encodeURIComponent(token)}` : ''}`
  const eventSource = new EventSource(url)

  eventSource.onmessage = (e) => {
    try {
      const event = JSON.parse(e.data) as ExecutionEvent
      onEvent(event)
    } catch (err) {
      console.error('Failed to parse event:', err)
    }
  }

  eventSource.onerror = (err) => {
    console.error('SSE error:', err)
    if (onError) {
      onError(new Error('Connection lost'))
    }
    eventSource.close()
  }

  return () => eventSource.close()
}

export async function checkHealth(): Promise<{ status: string }> {
  const response = await fetch('/health')
  if (!response.ok) {
    throw new Error('Health check failed')
  }
  return response.json()
}

export async function transcribeAudio(audioFile: File): Promise<{ text: string }> {
  const formData = new FormData()
  formData.append('file', audioFile)

  const token = getAuthToken()
  const headers: HeadersInit = {}
  if (token) {
    headers['Authorization'] = `Bearer ${token}`
  }

  const response = await fetch(`${API_BASE}/transcribe`, {
    method: 'POST',
    headers,
    body: formData,
  })

  if (!response.ok) {
    const text = await response.text()
    let message = text
    try {
      const json = JSON.parse(text)
      message = json.detail || json.message || text
    } catch {
      // Use text as-is
    }
    throw new ApiError(response.status, message)
  }

  return response.json()
}
