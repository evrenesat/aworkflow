import type {
  ConfigValidation,
  PlanDocument,
  PlanStatus,
  ProjectConfig,
  ProjectConfigFormRequest,
  ProjectConfigFormResponse,
  ProjectConfigSaveRequest,
  ProjectConfigValidateRequest,
  ProjectCreateRequest,
  ProjectCreateResult,
  ProjectDiscovery,
  ProjectInfo,
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
  StartRunRequest,
  StartRunResponse,
  StartRunResult,
} from './types'

import { consumeActivityMarker, resetActivityMarker } from './activity'

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

type SessionExpiredHandler = () => void

let sessionExpiredHandler: SessionExpiredHandler | null = null
let sessionRequests = new AbortController()

export function setSessionExpiredHandler(handler: SessionExpiredHandler | null): void {
  sessionExpiredHandler = handler
}

function isSessionUrl(url: string): boolean {
  return url === '/api/session' || url.startsWith('/api/session/')
}

async function fetchJson<T>(url: string, options: RequestInit = {}): Promise<T> {
  const includeJson = !(options.body instanceof FormData)
  const headers: Record<string, string> = {
    ...getHeaders(includeJson) as Record<string, string>,
    ...options.headers as Record<string, string>,
  }
  // Visible page restoration counts as use; login and logout do not renew.
  if ((!isSessionUrl(url) || !options.method || options.method === 'GET') && consumeActivityMarker()) {
    headers['X-AFlow-Activity'] = '1'
  }
  const signal = options.signal ?? sessionRequests.signal
  const response = await fetch(url, {
    credentials: 'same-origin',
    signal,
    ...options,
    headers,
  })

  if (signal.aborted) throw new DOMException('Session ended', 'AbortError')
  if (!response.ok) {
    if (response.status === 401 && !isSessionUrl(url)) {
      sessionExpiredHandler?.()
    }
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

  const result = await response.json()
  if (signal.aborted) throw new DOMException('Session ended', 'AbortError')
  return result
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

export async function checkSession(): Promise<{ authenticated: boolean }> {
  return fetchJson<{ authenticated: boolean }>('/api/session')
}

/**
 * Exchange the deployment bearer for a signed HttpOnly session cookie. The
 * token travels only in this request header and is never stored.
 */
export async function loginSession(token: string): Promise<{ authenticated: boolean }> {
  return fetchJson<{ authenticated: boolean }>('/api/session', {
    method: 'POST',
    headers: { Authorization: `Bearer ${token}` },
  })
}

export async function logoutSession(): Promise<void> {
  await fetchJson<void>('/api/session', { method: 'DELETE' })
  sessionRequests.abort()
  sessionRequests = new AbortController()
  resetActivityMarker()
}

export async function listProjects(): Promise<ProjectInfo[]> {
  return fetchJson<ProjectInfo[]>(`${API_BASE}/projects`)
}

export async function getProjectDiscovery(): Promise<ProjectDiscovery> {
  return fetchJson<ProjectDiscovery>(`${API_BASE}/project-discovery`)
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

export async function createProject(request: ProjectCreateRequest): Promise<ProjectCreateResult> {
  return fetchJson<ProjectCreateResult>(`${API_BASE}/projects`, {
    method: 'POST',
    body: JSON.stringify(request),
  })
}

export async function unregisterProject(projectId: string): Promise<void> {
  await fetchJson<void>(`${API_BASE}/projects/${encodeURIComponent(projectId)}`, {
    method: 'DELETE',
  })
}

export async function getProjectConfig(projectId: string): Promise<ProjectConfig> {
  return fetchJson<ProjectConfig>(`${API_BASE}/projects/${encodeURIComponent(projectId)}/config`)
}

export async function saveProjectConfig(
  projectId: string,
  request: ProjectConfigSaveRequest,
): Promise<ProjectConfig> {
  return fetchJson<ProjectConfig>(`${API_BASE}/projects/${encodeURIComponent(projectId)}/config`, {
    method: 'PUT',
    body: JSON.stringify(request),
  })
}

export async function validateProjectConfig(
  projectId: string,
  request: ProjectConfigValidateRequest,
): Promise<ConfigValidation> {
  return fetchJson<ConfigValidation>(
    `${API_BASE}/projects/${encodeURIComponent(projectId)}/config/validate`,
    { method: 'POST', body: JSON.stringify(request) },
  )
}

/**
 * Transform one candidate pair (plus at most one typed action) through the
 * pure guided form.  The endpoint never saves: no revision is sent.
 */
export async function postProjectConfigForm(
  projectId: string,
  request: ProjectConfigFormRequest,
  options: { signal?: AbortSignal } = {},
): Promise<ProjectConfigFormResponse> {
  return fetchJson<ProjectConfigFormResponse>(
    `${API_BASE}/projects/${encodeURIComponent(projectId)}/config/form`,
    { method: 'POST', body: JSON.stringify(request), signal: options.signal },
  )
}

export async function listProjectPlans(projectId: string, status?: PlanStatus): Promise<PlanDocument[]> {
  return fetchJson<PlanDocument[]>(`${API_BASE}/projects/${encodeURIComponent(projectId)}/plans${buildQuery({ status })}`)
}

export async function createProjectPlan(
  projectId: string,
  request: { name: string; content: string },
): Promise<PlanDocument> {
  return fetchJson<PlanDocument>(`${API_BASE}/projects/${encodeURIComponent(projectId)}/plans`, {
    method: 'POST',
    body: JSON.stringify(request),
  })
}

function planPath(projectId: string, status: PlanStatus, name: string): string {
  return `${API_BASE}/projects/${encodeURIComponent(projectId)}/plans/${status}/${encodeURIComponent(name)}`
}

export async function readProjectPlan(projectId: string, status: PlanStatus, name: string): Promise<PlanDocument> {
  return fetchJson<PlanDocument>(planPath(projectId, status, name))
}

export async function updateProjectPlan(
  projectId: string,
  status: PlanStatus,
  name: string,
  request: { content: string; expected_revision: string },
): Promise<PlanDocument> {
  return fetchJson<PlanDocument>(planPath(projectId, status, name), {
    method: 'PUT',
    body: JSON.stringify(request),
  })
}

export async function promoteProjectPlan(
  projectId: string,
  status: Exclude<PlanStatus, 'done'>,
  name: string,
  request: { expected_revision: string; target_name?: string | null },
): Promise<PlanDocument> {
  return fetchJson<PlanDocument>(`${planPath(projectId, status, name)}/promote`, {
    method: 'POST',
    body: JSON.stringify(request),
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
  request: StartRunRequest,
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
          { headers: getHeaders(false), credentials: 'same-origin', signal: controller.signal },
        )
        if (!response.ok) {
          if (response.status === 401) {
            // The browser session expired; stop reconnecting instead of
            // retrying the stream against a signed-out session.
            sessionExpiredHandler?.()
            return
          }
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
