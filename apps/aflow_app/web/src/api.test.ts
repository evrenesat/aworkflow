import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from './api'
import { markUserActivity, resetActivityMarker } from './activity'

function mockOkJson<T>(value: T, status = 200) {
  vi.mocked(global.fetch).mockResolvedValueOnce({ ok: true, status, json: async () => value } as Response)
}

describe('workflow control API client', () => {
  beforeEach(() => {
    global.fetch = vi.fn()
    api.clearAuthToken()
    api.setSessionExpiredHandler(null)
    resetActivityMarker()
    window.localStorage.clear()
    window.sessionStorage.clear()
  })

  it('exchanges the bearer for a cookie session without persisting the token', async () => {
    mockOkJson({ authenticated: true })
    await api.loginSession('secret-token')
    const [url, options] = vi.mocked(global.fetch).mock.calls.at(-1)!
    expect(url).toBe('/api/session')
    expect(options).toEqual(expect.objectContaining({
      method: 'POST',
      credentials: 'same-origin',
      headers: expect.objectContaining({ Authorization: 'Bearer secret-token' }),
    }))
    expect(window.localStorage.length).toBe(0)
    expect(window.sessionStorage.length).toBe(0)

    // Normal requests afterwards ride the same-origin cookie without any
    // Authorization header or stored credential.
    mockOkJson([])
    await api.listProjects()
    const [, listOptions] = vi.mocked(global.fetch).mock.calls.at(-1)!
    expect(listOptions.credentials).toBe('same-origin')
    expect((listOptions.headers as Record<string, string>).Authorization).toBeUndefined()

    mockOkJson({ authenticated: true })
    await api.checkSession()
    expect(vi.mocked(global.fetch).mock.calls.at(-1)![0]).toBe('/api/session')

    vi.mocked(global.fetch).mockResolvedValueOnce({ ok: true, status: 204 } as Response)
    await api.logoutSession()
    expect(vi.mocked(global.fetch).mock.calls.at(-1)).toEqual(['/api/session', expect.objectContaining({ method: 'DELETE', credentials: 'same-origin' })])
  })

  it('marks the next authenticated request with user activity at most once per minute', async () => {
    mockOkJson([])
    await api.listProjects()
    expect(vi.mocked(global.fetch).mock.calls.at(-1)![1].headers).not.toHaveProperty('X-AFlow-Activity')

    markUserActivity()
    markUserActivity()
    mockOkJson([])
    await api.listProjects()
    expect(vi.mocked(global.fetch).mock.calls.at(-1)![1].headers).toEqual(expect.objectContaining({ 'X-AFlow-Activity': '1' }))

    // The marker was consumed; the immediate next request is unmarked.
    markUserActivity(Date.now() + 1_000)
    mockOkJson([])
    await api.listProjects()
    expect(vi.mocked(global.fetch).mock.calls.at(-1)![1].headers).not.toHaveProperty('X-AFlow-Activity')
  })

  it('renews a visible page restore but leaves background restores unmarked', async () => {
    markUserActivity()
    mockOkJson({ authenticated: true })
    await api.checkSession()
    expect(vi.mocked(global.fetch).mock.calls.at(-1)![1].headers).toHaveProperty('X-AFlow-Activity', '1')
    mockOkJson({ authenticated: true })
    await api.checkSession()
    expect(vi.mocked(global.fetch).mock.calls.at(-1)![1].headers).not.toHaveProperty('X-AFlow-Activity')
  })

  it('aborts pending requests after confirmed logout and rejects late results', async () => {
    let finish!: (response: Response) => void
    vi.mocked(global.fetch).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
    const pending = api.listProjects()
    const rejected = expect(pending).rejects.toMatchObject({ name: 'AbortError' })
    const signal = vi.mocked(global.fetch).mock.calls[0][1].signal!
    vi.mocked(global.fetch).mockResolvedValueOnce({ ok: true, status: 204 } as Response)
    await api.logoutSession()
    expect(signal.aborted).toBe(true)
    finish({ ok: true, status: 200, json: async () => [{ id: 'late' }] } as Response)
    await rejected
    mockOkJson({ authenticated: true })
    await expect(api.checkSession()).resolves.toEqual({ authenticated: true })
  })

  it('does not abort the workspace or announce expiry when logout fails', async () => {
    const onExpired = vi.fn()
    api.setSessionExpiredHandler(onExpired)
    vi.mocked(global.fetch).mockResolvedValueOnce({ ok: false, status: 503, text: async () => 'unavailable' } as Response)
    await expect(api.logoutSession()).rejects.toMatchObject({ status: 503 })
    expect(vi.mocked(global.fetch).mock.calls[0][1].signal!.aborted).toBe(false)
    expect(onExpired).not.toHaveBeenCalled()
  })

  it('keeps background stream requests unmarked by user activity', async () => {
    markUserActivity()
    const encoder = new TextEncoder()
    vi.mocked(global.fetch).mockResolvedValueOnce({
      ok: true, status: 200,
      body: new ReadableStream<Uint8Array>({
        pull: (controller) => {
          controller.enqueue(encoder.encode('data: {"events":[]}\n\n'))
          controller.close()
        },
      }),
    } as unknown as Response)
    const unsubscribe = api.subscribeToRunEvents({
      projectId: 'project-1', runId: 'run-1', reconnectDelaysMs: [1],
      onEvents: () => {},
    })
    await new Promise((resolve) => setTimeout(resolve, 20))
    unsubscribe()
    const headers = vi.mocked(global.fetch).mock.calls.at(-1)![1].headers as Record<string, string>
    expect(headers['X-AFlow-Activity']).toBeUndefined()
    // The marker stays pending for the next ordinary REST request.
    mockOkJson([])
    await api.listProjects()
    expect(vi.mocked(global.fetch).mock.calls.at(-1)![1].headers).toEqual(expect.objectContaining({ 'X-AFlow-Activity': '1' }))
  })

  it('reports a 401 on ordinary requests as session expiry, but not on session endpoints', async () => {
    const onExpired = vi.fn()
    api.setSessionExpiredHandler(onExpired)
    vi.mocked(global.fetch).mockResolvedValueOnce({ ok: false, status: 401, text: async () => 'unauthorized' } as Response)
    await expect(api.listProjects()).rejects.toMatchObject({ status: 401 })
    expect(onExpired).toHaveBeenCalledTimes(1)

    vi.mocked(global.fetch).mockResolvedValueOnce({ ok: false, status: 401, text: async () => 'unauthorized' } as Response)
    await expect(api.checkSession()).rejects.toMatchObject({ status: 401 })
    expect(onExpired).toHaveBeenCalledTimes(1)
  })

  it('stops the run event stream on 401 and reports session expiry once', async () => {
    const onExpired = vi.fn()
    api.setSessionExpiredHandler(onExpired)
    vi.mocked(global.fetch).mockResolvedValue({ ok: false, status: 401, text: async () => 'unauthorized' } as Response)
    const states: api.StreamState[] = []
    const unsubscribe = api.subscribeToRunEvents({
      projectId: 'project-1', runId: 'run-1', reconnectDelaysMs: [1],
      onEvents: () => {},
      onStateChange: (state) => states.push(state),
    })
    await new Promise((resolve) => setTimeout(resolve, 30))
    unsubscribe()
    expect(onExpired).toHaveBeenCalledTimes(1)
    expect(vi.mocked(global.fetch)).toHaveBeenCalledTimes(1)
    expect(states).not.toContain('connected')
  })

  it('keeps bearer material in memory and sends it only as a header', async () => {
    api.setAuthToken('test-token')
    mockOkJson([{ id: 'project-1', display_name: 'Alpha' }])
    expect((await api.listProjects())[0].display_name).toBe('Alpha')
    expect(global.fetch).toHaveBeenCalledWith('/api/projects', expect.objectContaining({
      headers: expect.objectContaining({ Authorization: 'Bearer test-token' }),
    }))
    expect(window.localStorage.length).toBe(0)
  })

  it('reads bounded project discovery with authentication only as a header', async () => {
    api.setAuthToken('test-token')
    mockOkJson({
      schema_version: 1,
      managed_root: '/srv/code',
      candidates: [
        {
          relative_path: 'tools/kilo',
          display_name: 'Kilo',
          registered_project_id: null,
          addable: true,
          add_blocker: null,
        },
      ],
      visited_entries: 4,
      skipped_unreadable: 1,
      truncated: false,
      limits: { max_visited_entries: 500, max_candidates: 100 },
    })
    const discovery = await api.getProjectDiscovery()
    expect(global.fetch).toHaveBeenLastCalledWith('/api/project-discovery', expect.objectContaining({
      headers: expect.objectContaining({ Authorization: 'Bearer test-token' }),
    }))
    expect(discovery.candidates[0].relative_path).toBe('tools/kilo')
    expect(discovery.candidates[0].addable).toBe(true)
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

  it('transforms a candidate pair through the pure form endpoint without a revision', async () => {
    api.setAuthToken('test-token')
    const validation = {
      state: 'ready', issues: [], placeholders: [],
      workflows: ['starter'], teams: [], roles: ['worker'],
    }
    mockOkJson({
      aflow_toml: '# configured\n', workflows_toml: '# workflows\n', changed: true,
      validation,
      form: {
        default_workflow: 'starter', max_turns: null, harnesses: {}, roles: {},
        teams: {}, workflow_default_teams: {}, workflows: {},
      },
      syntax_issues: [],
      choices: { harnesses: [], profiles: {}, selectors: [], roles: ['worker'], teams: [], workflows: ['starter'] },
      suggestions: { label: 'suggestion', harnesses: [], profiles: [], note: 'Bundled values are suggestions.' },
      starter_defaults: null,
    })
    const response = await api.postProjectConfigForm('beta', {
      aflow_toml: '# aflow\n',
      workflows_toml: '# workflows\n',
      action: { type: 'set_global_role', role: 'worker', selector: 'starter.default' },
    })
    expect(response.changed).toBe(true)
    expect(response.form?.default_workflow).toBe('starter')
    expect(global.fetch).toHaveBeenLastCalledWith('/api/projects/beta/config/form', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({
        aflow_toml: '# aflow\n',
        workflows_toml: '# workflows\n',
        action: { type: 'set_global_role', role: 'worker', selector: 'starter.default' },
      }),
      headers: expect.objectContaining({ Authorization: 'Bearer test-token' }),
    }))
    // The pure transform never sends or receives a revision.
    expect((vi.mocked(global.fetch).mock.calls.at(-1)![1] as RequestInit).body).not.toContain('revision')
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

  it('resumes the run event stream from the last sequence and deduplicates replayed events', async () => {
    api.setAuthToken('test-token')
    const encoder = new TextEncoder()
    const streamFor = (frames: string[]) => {
      let sent = 0
      return new ReadableStream<Uint8Array>({
        pull(controller) {
          if (sent < frames.length) {
            controller.enqueue(encoder.encode(frames[sent]))
            sent += 1
          } else {
            controller.error(new Error('stream dropped'))
          }
        },
      })
    }
    const events = (sequences: number[]) => JSON.stringify({
      events: sequences.map((sequence) => ({
        sequence, event_type: `tick_${sequence}`, data: {}, schema_version: 1, timestamp: '2026-01-01T00:00:00Z',
      })),
    })
    vi.mocked(global.fetch)
      .mockResolvedValueOnce({ ok: true, status: 200, body: streamFor([`data: ${events([1, 2])}\n\n`]) } as unknown as Response)
      .mockResolvedValueOnce({ ok: true, status: 200, body: streamFor([`data: ${events([2, 3])}\n\n`]) } as unknown as Response)

    const batches: number[][] = []
    const states: api.StreamState[] = []
    const errors: string[] = []
    const unsubscribe = api.subscribeToRunEvents({
      projectId: 'project-1',
      runId: 'run-1',
      reconnectDelaysMs: [1],
      onEvents: (incoming) => batches.push(incoming.map((event) => event.sequence)),
      onStateChange: (state) => states.push(state),
      onError: (error) => errors.push(error.message),
    })

    await waitFor(() => expect(batches.flat()).toEqual([1, 2, 3]))
    unsubscribe()

    const firstUrl = vi.mocked(global.fetch).mock.calls[0][0] as string
    const secondUrl = vi.mocked(global.fetch).mock.calls[1][0] as string
    expect(firstUrl).not.toContain('after_sequence')
    expect(secondUrl).toContain('after_sequence=2')
    expect(secondUrl).not.toContain('test-token')
    // Sequence 2 was replayed by the server after reconnect but delivered once.
    expect(batches).toEqual([[1, 2], [3]])
    expect(states).toContain('connected')
    expect(states).toContain('reconnecting')
    expect(errors.length).toBeGreaterThan(0)
  })

  it('reports stream failures as connection state without inventing run state', async () => {
    api.setAuthToken('test-token')
    vi.mocked(global.fetch).mockResolvedValue({
      ok: false, status: 503, text: async () => 'control plane unavailable',
    } as Response)
    const states: api.StreamState[] = []
    const errors: string[] = []
    const unsubscribe = api.subscribeToRunEvents({
      projectId: 'project-1',
      runId: 'run-1',
      reconnectDelaysMs: [1],
      onEvents: () => {},
      onStateChange: (state) => states.push(state),
      onError: (error) => errors.push(error.message),
    })
    await waitFor(() => expect(errors.length).toBeGreaterThanOrEqual(2))
    unsubscribe()
    expect(errors[0]).toContain('control plane unavailable')
    expect(states[states.length - 1]).toBe('stopped')
    expect(states).not.toContain('connected')
  })
})

function waitFor(predicate: () => boolean, timeoutMs = 1_000): Promise<void> {
  const started = Date.now()
  return new Promise((resolve, reject) => {
    let lastError: unknown = null
    const tick = () => {
      let satisfied = false
      try {
        satisfied = predicate()
      } catch (error) {
        lastError = error
      }
      if (satisfied) {
        resolve()
        return
      }
      if (Date.now() - started > timeoutMs) {
        reject(lastError ?? new Error('waitFor timed out'))
        return
      }
      setTimeout(tick, 5)
    }
    tick()
  })
}
