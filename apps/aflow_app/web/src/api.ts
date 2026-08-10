import type {
  Attachment,
  AttachmentKind,
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
  ControlPlaneCapabilities,
  ControlPlanePlan,
  ControlPlaneProject,
  ControlPlaneReadiness,
  ControlResponse,
  RunContext,
  RunControlRequest,
  RunEvent,
  RunEventTail,
  RunPage,
  RunStatus,
  StartRunResponse,
  StartRunResult,
} from './types'

const API_BASE = '/api'

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public readonly code: string | null = null,
    public readonly detail: Record<string, unknown> = {},
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

let authToken: string | null = null

export function setAuthToken(token: string) {
  authToken = token
}

export function getAuthToken(): string | null {
  return authToken
}

export function clearAuthToken() {
  authToken = null
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
    let code: string | null = null
    let detail: Record<string, unknown> = {}
    try {
      const json = JSON.parse(text)
      const errorDetail = json.detail || json.message
      if (typeof errorDetail === 'object' && errorDetail !== null) {
        detail = errorDetail as Record<string, unknown>
        code = typeof detail.code === 'string' ? detail.code : null
      }
      const detailMessage = typeof detail.message === 'string' ? detail.message : null
      message = typeof errorDetail === 'string' ? errorDetail : detailMessage || code || text
    } catch {
      // Use text as-is
    }
    throw new ApiError(response.status, message, code, detail)
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

const CONTROL_PLANE_BASE = `${API_BASE}/control-plane`

function controlProjectPath(projectId: string): string {
  return `${CONTROL_PLANE_BASE}/projects/${encodeURIComponent(projectId)}`
}

function withIdempotency(headers: HeadersInit | undefined, idempotencyKey: string): HeadersInit {
  return { ...headers, 'Idempotency-Key': idempotencyKey }
}

export async function listControlPlaneProjects(): Promise<ControlPlaneProject[]> {
  const response = await fetchJson<{ projects: ControlPlaneProject[] }>(`${CONTROL_PLANE_BASE}/projects`)
  return response.projects
}

export async function getControlPlaneReadiness(): Promise<ControlPlaneReadiness> {
  return fetchJson<ControlPlaneReadiness>('/ready')
}

export async function getControlPlaneCapabilities(projectId: string): Promise<ControlPlaneCapabilities> {
  return fetchJson<ControlPlaneCapabilities>(`${controlProjectPath(projectId)}/capabilities`)
}

export async function listControlPlanePlans(projectId: string): Promise<ControlPlanePlan[]> {
  const response = await fetchJson<{ plans: ControlPlanePlan[] }>(`${controlProjectPath(projectId)}/plans`)
  return response.plans
}

export async function listControlPlaneRuns(
  projectId: string,
  request: { cursor?: string; limit?: number } = {},
): Promise<RunPage> {
  return fetchJson<RunPage>(`${controlProjectPath(projectId)}/runs${buildQuery(request)}`)
}

export async function getControlPlaneRun(projectId: string, runId: string): Promise<RunStatus> {
  return fetchJson<RunStatus>(`${controlProjectPath(projectId)}/runs/${encodeURIComponent(runId)}`)
}

export async function listRunEvents(
  projectId: string,
  runId: string,
  request: { after_sequence?: number; limit?: number } = {},
): Promise<RunEvent[]> {
  const response = await fetchJson<RunEventTail>(
    `${controlProjectPath(projectId)}/runs/${encodeURIComponent(runId)}/events${buildQuery(request)}`,
  )
  return response.events
}

export async function getRunContext(
  projectId: string,
  runId: string,
  level: 'lite' | 'full',
  fullScope = false,
): Promise<RunContext> {
  return fetchJson<RunContext>(
    `${controlProjectPath(projectId)}/runs/${encodeURIComponent(runId)}/context${buildQuery({
      level,
      ...(level === 'full' ? { full_scope: fullScope } : {}),
    })}`,
  )
}

export async function startControlPlaneRun(
  projectId: string,
  request: {
    plan_path: string
    workflow_name?: string
    team?: string
    start_step?: string
    max_turns?: number
  },
  idempotencyKey: string,
): Promise<StartRunResponse> {
  return fetchJson<StartRunResponse>(`${controlProjectPath(projectId)}/runs`, {
    method: 'POST',
    headers: withIdempotency(undefined, idempotencyKey),
    body: JSON.stringify(request),
  })
}

export async function answerStartupQuestion(
  projectId: string,
  questionId: string,
  answer: string | number | boolean,
  idempotencyKey: string,
): Promise<StartRunResponse> {
  return fetchJson<StartRunResponse>(
    `${controlProjectPath(projectId)}/startup-answers/${encodeURIComponent(questionId)}`,
    {
      method: 'POST',
      headers: withIdempotency(undefined, idempotencyKey),
      body: JSON.stringify({ answer }),
    },
  )
}

export async function controlControlPlaneRun(
  projectId: string,
  runId: string,
  request: RunControlRequest,
  idempotencyKey: string,
): Promise<ControlResponse> {
  return fetchJson<ControlResponse>(
    `${controlProjectPath(projectId)}/runs/${encodeURIComponent(runId)}/control`,
    {
      method: 'PATCH',
      headers: withIdempotency(undefined, idempotencyKey),
      body: JSON.stringify(request),
    },
  )
}

export async function ownerStopControlPlaneRun(
  projectId: string,
  runId: string,
  expectedRevision: number,
  idempotencyKey: string,
): Promise<RunStatus> {
  return fetchJson<RunStatus>(
    `${controlProjectPath(projectId)}/runs/${encodeURIComponent(runId)}/owner-stop`,
    {
      method: 'POST',
      headers: withIdempotency(undefined, idempotencyKey),
      body: JSON.stringify({ expected_revision: expectedRevision }),
    },
  )
}

export async function resumeControlPlaneRun(
  projectId: string,
  runId: string,
  idempotencyKey: string,
): Promise<StartRunResult> {
  return fetchJson<StartRunResult>(
    `${controlProjectPath(projectId)}/runs/${encodeURIComponent(runId)}/resume`,
    { method: 'POST', headers: withIdempotency(undefined, idempotencyKey) },
  )
}

export type StreamState = 'connected' | 'reconnecting' | 'stopped'

export interface RunEventSubscription {
  projectId: string
  runId: string
  afterSequence?: number
  onEvents: (events: RunEvent[]) => void
  onError?: (error: Error) => void
  onStateChange?: (state: StreamState) => void
  /** Test-only override; production callers retain a capped 250ms–4s backoff. */
  reconnectDelaysMs?: number[]
}

function parseSseFrame(frame: string): RunEvent[] {
  const data = frame
    .split(/\r?\n/)
    .filter((line) => line.startsWith('data:'))
    .map((line) => line.slice(5).trimStart())
    .join('\n')
  if (!data) return []
  const parsed = JSON.parse(data) as RunEventTail
  return Array.isArray(parsed.events) ? parsed.events : []
}

async function readRunEventStream(response: Response, onEvents: (events: RunEvent[]) => void): Promise<void> {
  if (!response.body) throw new Error('Run event stream has no response body')
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  try {
    for (;;) {
      const { done, value } = await reader.read()
      buffer += decoder.decode(value, { stream: !done })
      const frames = buffer.split(/\r?\n\r?\n/)
      buffer = frames.pop() ?? ''
      for (const frame of frames) {
        const events = parseSseFrame(frame)
        if (events.length) onEvents(events)
      }
      if (done) {
        if (buffer.trim()) {
          const events = parseSseFrame(buffer)
          if (events.length) onEvents(events)
        }
        return
      }
    }
  } finally {
    reader.releaseLock()
  }
}

/**
 * Connect with fetch so Authorization remains in a header.  The only query
 * value is the public event cursor, never bearer material.
 */
export function subscribeToRunEvents(subscription: RunEventSubscription): () => void {
  const delays = subscription.reconnectDelaysMs?.length
    ? subscription.reconnectDelaysMs
    : [250, 500, 1_000, 2_000, 4_000]
  let cancelled = false
  let controller: AbortController | null = null
  let retryTimer: ReturnType<typeof setTimeout> | null = null
  let cursor = subscription.afterSequence
  let attempt = 0

  const waitForRetry = (delay: number) => new Promise<void>((resolve) => {
    retryTimer = setTimeout(() => {
      retryTimer = null
      resolve()
    }, delay)
  })

  const connect = async () => {
    while (!cancelled) {
      controller = new AbortController()
      try {
        const response = await fetch(
          `${controlProjectPath(subscription.projectId)}/runs/${encodeURIComponent(subscription.runId)}/events/stream${buildQuery({
            after_sequence: cursor,
            limit: 100,
          })}`,
          { headers: getHeaders(false), signal: controller.signal },
        )
        if (!response.ok) {
          const message = await response.text()
          throw new ApiError(response.status, message || 'Run event stream failed')
        }
        attempt = 0
        subscription.onStateChange?.('connected')
        await readRunEventStream(response, (events) => {
          const fresh = events.filter((event) => cursor === undefined || event.sequence > cursor!)
          if (!fresh.length) return
          cursor = fresh[fresh.length - 1].sequence
          subscription.onEvents(fresh)
        })
        if (cancelled) return
        subscription.onError?.(new Error('Run event stream ended; reconnecting without changing run status.'))
      } catch (error) {
        if (cancelled || (error instanceof DOMException && error.name === 'AbortError')) return
        subscription.onError?.(error instanceof Error ? error : new Error('Run event stream failed'))
      } finally {
        controller = null
      }
      if (cancelled) return
      subscription.onStateChange?.('reconnecting')
      const delay = delays[Math.min(attempt, delays.length - 1)]
      attempt += 1
      await waitForRetry(delay)
    }
  }

  void connect()
  return () => {
    cancelled = true
    controller?.abort()
    if (retryTimer !== null) clearTimeout(retryTimer)
    subscription.onStateChange?.('stopped')
  }
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
