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

  it('manages auth and lists projects with authorization', async () => {
    api.setAuthToken('test-token')
    mockOkJson([{ id: 'project-1', display_name: 'Alpha', linked_session_count: 2 }])
    expect((await api.listProjects())[0].linked_session_count).toBe(2)
    expect(global.fetch).toHaveBeenCalledWith('/api/projects', expect.objectContaining({
      headers: expect.objectContaining({ Authorization: 'Bearer test-token' }),
    }))
    api.clearAuthToken()
    expect(api.getAuthToken()).toBeNull()
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

  it('keeps plan draft and execution routes unchanged', async () => {
    mockOkJson({ name: 'plan-a', path: '/tmp/plan-a.md', status: 'draft' }, 201)
    await api.savePlanDraft('project-1', { name: 'plan-a', content: '# Plan' })
    expect(global.fetch).toHaveBeenLastCalledWith('/api/projects/project-1/plans/drafts', expect.objectContaining({ method: 'POST' }))

    mockOkJson({ run_id: 'run-1' })
    await api.startExecution({ project_id: 'project-1', plan_path: 'plans/in-progress/demo.md' })
    expect(global.fetch).toHaveBeenLastCalledWith('/api/executions', expect.objectContaining({ method: 'POST' }))
  })
})
