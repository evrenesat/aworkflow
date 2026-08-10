import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from './api'

function mockOkJson<T>(value: T, status = 200) {
  vi.mocked(global.fetch).mockResolvedValueOnce({ ok: true, status, json: async () => value } as Response)
}

const key = { provider_id: 'codex', provider_session_id: 'session-1' }

describe('provider-neutral API client', () => {
  beforeEach(() => {
    global.fetch = vi.fn()
    api.clearAuthToken()
  })

  it('keeps bearer material in memory and sends it only as an authorization header', async () => {
    api.setAuthToken('test-token')
    mockOkJson([{ id: 'project-1', display_name: 'Alpha', linked_session_count: 2 }])
    expect((await api.listProjects())[0].linked_session_count).toBe(2)
    expect(global.fetch).toHaveBeenCalledWith('/api/projects', expect.objectContaining({
      headers: expect.objectContaining({ Authorization: 'Bearer test-token' }),
    }))
    expect(window.localStorage.length).toBe(0)
    api.clearAuthToken()
    expect(api.getAuthToken()).toBeNull()
    expect(window.localStorage.length).toBe(0)
  })

  it('keeps idempotency and bearer material out of storage and mutation URLs', async () => {
    api.setAuthToken('test-token')
    window.localStorage.clear()
    window.sessionStorage.clear()
    const initialUrl = window.location.href
    mockOkJson({ result: { run_id: 'run-1', created: true, status: 'running', schema_version: 1, manifest_path: null, reason: null }, startup_question: null }, 201)

    await api.startControlPlaneRun('control-project', { plan_path: 'plans/todo/demo.md' }, 'start-retry-key')

    const [url, options] = vi.mocked(global.fetch).mock.calls.at(-1)!
    expect(url).not.toContain('test-token')
    expect(url).not.toContain('start-retry-key')
    expect(options.headers).toEqual(expect.objectContaining({
      Authorization: 'Bearer test-token',
      'Idempotency-Key': 'start-retry-key',
    }))
    expect(window.localStorage.length).toBe(0)
    expect(window.sessionStorage.length).toBe(0)
    expect(window.location.href).toBe(initialUrl)
  })

  it('uses canonical provider-qualified session routes without cwd payloads', async () => {
    mockOkJson({ sessions: [], providers: [], next_cursor: null })
    await api.listProjectSessions('project-1', { archived: false })
    expect(global.fetch).toHaveBeenLastCalledWith(
      '/api/projects/project-1/planning/sessions?archived=false', expect.any(Object)
    )

    mockOkJson({ key, turns: [] })
    await api.getProjectSession('project-1', key)
    expect(global.fetch).toHaveBeenLastCalledWith(
      '/api/projects/project-1/planning/providers/codex/sessions/session-1?include_turns=true', expect.any(Object)
    )

    mockOkJson({ key, turns: [] }, 201)
    await api.startProjectSession('project-1', { provider_id: 'codex', model: 'gpt-5' })
    expect(global.fetch).toHaveBeenLastCalledWith(
      '/api/projects/project-1/planning/sessions',
      expect.objectContaining({ method: 'POST', body: JSON.stringify({ provider_id: 'codex', model: 'gpt-5' }) })
    )

    mockOkJson({ key, turns: [] })
    await api.resumeProjectSession('project-1', key)
    expect(global.fetch).toHaveBeenLastCalledWith(
      '/api/projects/project-1/planning/providers/codex/sessions/session-1/resume',
      expect.objectContaining({ method: 'POST' })
    )
  })

  it('sends provider-neutral turn controls and actions', async () => {
    mockOkJson({ turn_id: 'turn-1', status: 'running', items: [] }, 201)
    await api.startProjectTurn('project-1', key, {
      text: 'hello', attachment_ids: ['attachment-1'], model: 'gpt-5',
      reasoning_level: 'high', reasoning_summary: 'concise',
    })
    expect(global.fetch).toHaveBeenLastCalledWith(
      '/api/projects/project-1/planning/providers/codex/sessions/session-1/turns',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          text: 'hello', attachment_ids: ['attachment-1'], model: 'gpt-5',
          reasoning_level: 'high', reasoning_summary: 'concise',
        }),
      })
    )

    mockOkJson({ status: 'interrupted' })
    await api.interruptProjectTurn('project-1', key, 'turn-1')
    expect(global.fetch).toHaveBeenLastCalledWith(
      '/api/projects/project-1/planning/providers/codex/sessions/session-1/turns/turn-1/interrupt',
      expect.objectContaining({ method: 'POST' })
    )

    mockOkJson({ status: 'recorded' })
    await api.respondToApproval('project-1', key, 'approval-1', 'accept')
    expect(global.fetch).toHaveBeenLastCalledWith(
      '/api/projects/project-1/planning/providers/codex/sessions/session-1/approvals/approval-1',
      expect.objectContaining({ method: 'POST', body: JSON.stringify({ decision: 'accept' }) })
    )
  })

  it('uploads multipart attachments without a JSON content type', async () => {
    api.setAuthToken('test-token')
    mockOkJson({ attachment_id: 'a-1', filename: 'diagram.png', kind: 'image', size_bytes: 3 }, 201)
    await api.uploadAttachment('project-1', key, new File(['abc'], 'diagram.png', { type: 'image/png' }), 'image')

    const [, options] = vi.mocked(global.fetch).mock.calls.at(-1)!
    expect(options.body).toBeInstanceOf(FormData)
    expect(options.headers.Authorization).toBe('Bearer test-token')
    expect(options.headers['Content-Type']).toBeUndefined()
  })

  it('surfaces bounded provider error messages', async () => {
    vi.mocked(global.fetch).mockResolvedValueOnce({
      ok: false,
      status: 503,
      text: async () => JSON.stringify({ detail: { code: 'provider_unavailable', message: 'Planning provider is unavailable.' } }),
    } as Response)
    await expect(api.listProjectSessions('project-1')).rejects.toThrow('Planning provider is unavailable.')
  })

  it('surfaces an authenticated control-plane failure and clears the in-memory bearer on logout', async () => {
    api.setAuthToken('test-token')
    vi.mocked(global.fetch).mockResolvedValueOnce({
      ok: false,
      status: 401,
      text: async () => JSON.stringify({ detail: { code: 'unauthorized' } }),
    } as Response)
    await expect(api.getControlPlaneReadiness()).rejects.toMatchObject({ status: 401, code: 'unauthorized' })

    api.clearAuthToken()
    mockOkJson({ ready: true, projects: [] })
    await api.getControlPlaneReadiness()
    expect(global.fetch).toHaveBeenLastCalledWith('/ready', expect.objectContaining({
      headers: expect.not.objectContaining({ Authorization: expect.anything() }),
    }))
    expect(window.localStorage.length).toBe(0)
  })

  it('keeps planning draft routes while using the control-plane run contract', async () => {
    mockOkJson({ name: 'plan-a', path: '/tmp/plan-a.md', status: 'draft' }, 201)
    await api.savePlanDraft('project-1', { name: 'plan-a', content: '# Plan' })
    expect(global.fetch).toHaveBeenLastCalledWith('/api/projects/project-1/plans/drafts', expect.objectContaining({ method: 'POST' }))

    mockOkJson({ projects: [{ project_id: 'control-project', root: '/workspace/alpha', schema_version: 1 }] })
    expect((await api.listControlPlaneProjects())[0].project_id).toBe('control-project')
    expect(global.fetch).toHaveBeenLastCalledWith('/api/control-plane/projects', expect.any(Object))

    mockOkJson({ ready: true, projects: ['control-project'] })
    await expect(api.getControlPlaneReadiness()).resolves.toEqual({ ready: true, projects: ['control-project'] })
    expect(global.fetch).toHaveBeenLastCalledWith('/ready', expect.any(Object))

    mockOkJson({ schema_version: 1, workflows: ['managed'], teams: ['review'], roles: ['worker'], controls: ['max_turns'], context_levels: ['lite'], team_upgrade_chains: {}, control_safety: { max_turns: 'safe' }, service_features: [] })
    await api.getControlPlaneCapabilities('control project')
    expect(global.fetch).toHaveBeenLastCalledWith('/api/control-plane/projects/control%20project/capabilities', expect.any(Object))

    mockOkJson({ runs: [], next_cursor: null, schema_version: 1 })
    await api.listControlPlaneRuns('control-project', { limit: 100 })
    expect(global.fetch).toHaveBeenLastCalledWith('/api/control-plane/projects/control-project/runs?limit=100', expect.any(Object))

    mockOkJson({ result: { run_id: 'run-1', created: true, status: 'running', schema_version: 1, manifest_path: null, reason: null }, startup_question: null }, 201)
    await api.startControlPlaneRun('control-project', { plan_path: 'plans/todo/demo.md' }, 'start-key')
    expect(global.fetch).toHaveBeenLastCalledWith(
      '/api/control-plane/projects/control-project/runs',
      expect.objectContaining({ method: 'POST', headers: expect.objectContaining({ 'Idempotency-Key': 'start-key' }) }),
    )

    mockOkJson({ result: { run_id: 'run-1', created: true, status: 'running', schema_version: 1, manifest_path: null, reason: null }, startup_question: null })
    await api.answerStartupQuestion('control-project', 'question-1', 'implement', 'answer-key')
    expect(global.fetch).toHaveBeenLastCalledWith('/api/control-plane/projects/control-project/startup-answers/question-1', expect.objectContaining({ method: 'POST' }))

    mockOkJson({ revision: 1, changed: true, owner_stop: false, run: { run_id: 'run-1' } })
    await api.controlControlPlaneRun('control-project', 'run-1', { expected_revision: 0, max_turns: 3 }, 'control-key')
    expect(global.fetch).toHaveBeenLastCalledWith('/api/control-plane/projects/control-project/runs/run-1/control', expect.objectContaining({ method: 'PATCH' }))

    mockOkJson({ run_id: 'run-1', status: 'owner_stopped' })
    await api.ownerStopControlPlaneRun('control-project', 'run-1', 1, 'stop-key')
    expect(global.fetch).toHaveBeenLastCalledWith('/api/control-plane/projects/control-project/runs/run-1/owner-stop', expect.objectContaining({ method: 'POST' }))

    mockOkJson({ run_id: 'run-2', created: true, status: 'running', schema_version: 1, manifest_path: null, reason: null }, 201)
    await api.resumeControlPlaneRun('control-project', 'run-1', 'resume-key')
    expect(global.fetch).toHaveBeenLastCalledWith('/api/control-plane/projects/control-project/runs/run-1/resume', expect.objectContaining({ method: 'POST' }))
  })

  it('reconnects authenticated run streams from the latest sequence without duplicate events or token URLs', async () => {
    api.setAuthToken('test-token')
    const encoder = new TextEncoder()
    const streamResponse = (events: Array<{ sequence: number; event_type: string; data: Record<string, unknown>; schema_version: number; timestamp: string }>) => ({
      ok: true,
      status: 200,
      body: new ReadableStream({
        start(controller) {
          controller.enqueue(encoder.encode(`event: events\ndata: ${JSON.stringify({ events })}\n\n`))
          controller.close()
        },
      }),
    } as Response)
    vi.mocked(global.fetch)
      .mockResolvedValueOnce(streamResponse([{ sequence: 1, event_type: 'started', data: {}, schema_version: 1, timestamp: '2024-01-01T00:00:00Z' }]))
      .mockResolvedValueOnce(streamResponse([
        { sequence: 1, event_type: 'started', data: {}, schema_version: 1, timestamp: '2024-01-01T00:00:00Z' },
        { sequence: 2, event_type: 'progress', data: {}, schema_version: 1, timestamp: '2024-01-01T00:00:01Z' },
      ]))
    const received: number[] = []
    let unsubscribe = () => {}
    unsubscribe = api.subscribeToRunEvents({
      projectId: 'control-project',
      runId: 'run-1',
      afterSequence: 0,
      reconnectDelaysMs: [0],
      onEvents: (events) => {
        received.push(...events.map((event) => event.sequence))
        if (received.includes(2)) unsubscribe()
      },
    })
    for (let attempt = 0; attempt < 20 && !received.includes(2); attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 10))
    }

    expect(received).toEqual([1, 2])
    const streamCalls = vi.mocked(global.fetch).mock.calls
    expect(streamCalls[0][0]).toBe('/api/control-plane/projects/control-project/runs/run-1/events/stream?after_sequence=0&limit=100')
    expect(streamCalls[1][0]).toBe('/api/control-plane/projects/control-project/runs/run-1/events/stream?after_sequence=1&limit=100')
    expect(streamCalls.every(([url]) => !String(url).includes('test-token'))).toBe(true)
    expect(streamCalls[0][1]?.headers).toEqual(expect.objectContaining({ Authorization: 'Bearer test-token' }))
  })
})
