import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from './api'

function mockOkJson<T>(value: T, status = 200) {
  vi.mocked(global.fetch).mockResolvedValueOnce({ ok: true, status, json: async () => value } as Response)
}

describe('workflow control API client', () => {
  beforeEach(() => { global.fetch = vi.fn(); api.clearAuthToken() })

  it('keeps bearer material in memory and sends it only as a header', async () => {
    api.setAuthToken('test-token')
    mockOkJson([{ id: 'project-1', display_name: 'Alpha' }])
    expect((await api.listProjects())[0].display_name).toBe('Alpha')
    expect(global.fetch).toHaveBeenCalledWith('/api/projects', expect.objectContaining({
      headers: expect.objectContaining({ Authorization: 'Bearer test-token' }),
    }))
    expect(window.localStorage.length).toBe(0)
  })

  it('uses revisioned project plan routes', async () => {
    mockOkJson({ project_id: 'project-1', name: 'demo.md', path: 'plans/todo/demo.md', status: 'todo', revision: 'a'.repeat(64), size_bytes: 6 }, 201)
    await api.createProjectPlan('project-1', { name: 'demo.md', content: '# Plan' })
    expect(global.fetch).toHaveBeenLastCalledWith('/api/projects/project-1/plans', expect.objectContaining({ method: 'POST' }))

    mockOkJson({ project_id: 'project-1', name: 'demo.md', path: 'plans/todo/demo.md', status: 'todo', revision: 'b'.repeat(64), size_bytes: 9 })
    await api.updateProjectPlan('project-1', 'todo', 'demo.md', { content: '# Updated', expected_revision: 'a'.repeat(64) })
    expect(global.fetch).toHaveBeenLastCalledWith('/api/projects/project-1/plans/todo/demo.md', expect.objectContaining({ method: 'PUT' }))

    mockOkJson({ project_id: 'project-1', name: 'demo.md', path: 'plans/in-progress/demo.md', status: 'in_progress', revision: 'b'.repeat(64), size_bytes: 9 })
    await api.promoteProjectPlan('project-1', 'todo', 'demo.md', { expected_revision: 'b'.repeat(64) })
    expect(global.fetch).toHaveBeenLastCalledWith('/api/projects/project-1/plans/todo/demo.md/promote', expect.objectContaining({ method: 'POST' }))
  })

  it('keeps idempotency keys out of mutation URLs and storage', async () => {
    api.setAuthToken('test-token')
    window.localStorage.clear(); window.sessionStorage.clear()
    mockOkJson({ result: { run_id: 'run-1', created: true, status: 'running', schema_version: 1 }, startup_question: null }, 201)
    await api.startControlPlaneRun('control-project', { plan_path: 'plans/todo/demo.md' }, 'start-key')
    const [url, options] = vi.mocked(global.fetch).mock.calls.at(-1)!
    expect(url).not.toContain('test-token'); expect(url).not.toContain('start-key')
    expect(options.headers).toEqual(expect.objectContaining({ Authorization: 'Bearer test-token', 'Idempotency-Key': 'start-key' }))
    expect(window.localStorage.length).toBe(0); expect(window.sessionStorage.length).toBe(0)
  })

  it('surfaces structured API failures', async () => {
    vi.mocked(global.fetch).mockResolvedValueOnce({
      ok: false, status: 409,
      text: async () => JSON.stringify({ detail: { code: 'revision_conflict', current_revision: 'b'.repeat(64) } }),
    } as Response)
    await expect(api.updateProjectPlan('project-1', 'todo', 'demo.md', { content: 'x', expected_revision: 'a'.repeat(64) }))
      .rejects.toMatchObject({ status: 409, code: 'revision_conflict' })
  })
})
