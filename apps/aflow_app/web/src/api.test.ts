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

  it('creates and unregisters projects through the registry-scoped contract', async () => {
    api.setAuthToken('test-token')
    mockOkJson({
      id: 'beta', display_name: 'Beta', relative_root: 'beta', root: '/srv/code/beta',
      created_at: '2026-01-01T00:00:00Z', readiness: 'configuration_required',
    }, 201)
    const created = await api.createProject({
      mode: 'register', path: 'beta', display_name: 'Beta', main_branch: 'main',
      initial_workflow: 'starter', initial_team: null, initialize_git: true, initialize_config: false,
    })
    expect(created.id).toBe('beta')
    expect(global.fetch).toHaveBeenLastCalledWith('/api/projects', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({
        mode: 'register', path: 'beta', display_name: 'Beta', main_branch: 'main',
        initial_workflow: 'starter', initial_team: null, initialize_git: true, initialize_config: false,
      }),
    }))

    mockOkJson(undefined, 204)
    await api.unregisterProject('beta')
    expect(global.fetch).toHaveBeenLastCalledWith('/api/projects/beta', expect.objectContaining({ method: 'DELETE' }))
  })

  it('reads, validates, and revision-saves the canonical configuration pair', async () => {
    api.setAuthToken('test-token')
    const validation = {
      state: 'configuration_required', issues: [], placeholders: ['harness.starter.profiles.default.model'],
      workflows: ['starter'], teams: [], roles: [],
    }
    mockOkJson({
      project_id: 'beta', revision: 'a'.repeat(64), documents: ['aflow.toml', 'workflows.toml'],
      aflow_toml: '# aflow\n', workflows_toml: '# workflows\n', validation,
    })
    const config = await api.getProjectConfig('beta')
    expect(global.fetch).toHaveBeenLastCalledWith('/api/projects/beta/config', expect.objectContaining({
      headers: expect.objectContaining({ Authorization: 'Bearer test-token' }),
    }))
    expect(config.revision).toBe('a'.repeat(64))

    mockOkJson(validation)
    await api.validateProjectConfig('beta', { aflow_toml: '# aflow\n', workflows_toml: '# workflows\n' })
    expect(global.fetch).toHaveBeenLastCalledWith('/api/projects/beta/config/validate', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ aflow_toml: '# aflow\n', workflows_toml: '# workflows\n' }),
    }))

    mockOkJson({ ...config, revision: 'b'.repeat(64) })
    await api.saveProjectConfig('beta', {
      aflow_toml: '# aflow\n', workflows_toml: '# workflows\n', expected_revision: 'a'.repeat(64),
    })
    expect(global.fetch).toHaveBeenLastCalledWith('/api/projects/beta/config', expect.objectContaining({
      method: 'PUT',
      body: JSON.stringify({
        aflow_toml: '# aflow\n', workflows_toml: '# workflows\n', expected_revision: 'a'.repeat(64),
      }),
    }))
  })

  it('carries config conflict and blocker detail through ApiError', async () => {
    vi.mocked(global.fetch).mockResolvedValueOnce({
      ok: false, status: 409,
      text: async () => JSON.stringify({
        detail: {
          code: 'config_save_blocked',
          blocking_runs: [{ run_id: 'run-9', status: 'running' }],
        },
      }),
    } as Response)
    await expect(api.saveProjectConfig('beta', {
      aflow_toml: 'x', workflows_toml: 'y', expected_revision: 'a'.repeat(64),
    })).rejects.toMatchObject({
      status: 409,
      code: 'config_save_blocked',
      detail: { blocking_runs: [{ run_id: 'run-9', status: 'running' }] },
    })
  })
})
