import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { App } from './App'
import * as api from './api'
import { consumeActivityMarker, resetActivityMarker } from './activity'

vi.mock('./api', () => ({
  ApiError: class ApiError extends Error {
    constructor(
      public status: number,
      message: string,
      public readonly code: string | null = null,
      public readonly detail: Record<string, unknown> = {},
    ) {
      super(message)
    }
  },
  getAuthToken: vi.fn(), setAuthToken: vi.fn(), clearAuthToken: vi.fn(),
  checkSession: vi.fn(), loginSession: vi.fn(), logoutSession: vi.fn(), setSessionExpiredHandler: vi.fn(),
  listProjects: vi.fn(), getProjectDiscovery: vi.fn(), getProject: vi.fn(), createProject: vi.fn(), unregisterProject: vi.fn(),
  getProjectConfig: vi.fn(), saveProjectConfig: vi.fn(), validateProjectConfig: vi.fn(),
  listProjectPlans: vi.fn(), createProjectPlan: vi.fn(), readProjectPlan: vi.fn(),
  updateProjectPlan: vi.fn(), promoteProjectPlan: vi.fn(),
  listControlPlaneProjects: vi.fn(), getControlPlaneReadiness: vi.fn(), getControlPlaneCapabilities: vi.fn(),
  listControlPlanePlans: vi.fn(), listControlPlaneRuns: vi.fn(), getControlPlaneRun: vi.fn(),
  listRunEvents: vi.fn(), getRunContext: vi.fn(), startControlPlaneRun: vi.fn(),
  answerStartupQuestion: vi.fn(), controlControlPlaneRun: vi.fn(), ownerStopControlPlaneRun: vi.fn(),
  resumeControlPlaneRun: vi.fn(), subscribeToRunEvents: vi.fn(),
}))

const readyProject = {
  id: 'alpha', display_name: 'Alpha Project', current_path: '/srv/code/alpha',
  is_git_root: true, registered_at: '2026-01-01T00:00:00Z', readiness: 'ready' as const,
}
const configProject = {
  id: 'beta', display_name: 'Beta Project', current_path: '/srv/code/beta',
  is_git_root: true, registered_at: '2026-01-01T00:00:00Z', readiness: 'configuration_required' as const,
}
const blockedProject = {
  id: 'gamma', display_name: 'Gamma Project', current_path: '/srv/code/gamma',
  is_git_root: false, registered_at: '2026-01-01T00:00:00Z', readiness: 'blocked' as const,
}

const discoveryBase = {
  schema_version: 1,
  managed_root: '/srv/code',
  candidates: [] as never[],
  visited_entries: 0,
  skipped_unreadable: 0,
  truncated: false,
  limits: { max_visited_entries: 500, max_candidates: 100 },
}

const configPayload = (state: 'ready' | 'configuration_required' | 'invalid' = 'configuration_required') => ({
  project_id: 'beta',
  revision: 'a'.repeat(64),
  documents: ['aflow.toml', 'workflows.toml'],
  aflow_toml: '# aflow config\n',
  workflows_toml: '# workflows\n',
  validation: {
    state,
    issues: [],
    placeholders: state === 'ready' ? [] : ['harness.starter.profiles.default.model'],
    workflows: ['starter'],
    teams: [],
    roles: ['worker'],
  },
})

describe('App workspace shell', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    resetActivityMarker()
    vi.mocked(api.checkSession).mockResolvedValue({ authenticated: true })
    vi.mocked(api.loginSession).mockResolvedValue({ authenticated: true })
    vi.mocked(api.logoutSession).mockResolvedValue(undefined)
    vi.mocked(api.listProjects).mockResolvedValue([])
    vi.mocked(api.getProjectDiscovery).mockResolvedValue(discoveryBase)
    vi.mocked(api.getProjectConfig).mockResolvedValue(configPayload())
    vi.mocked(api.listProjectPlans).mockResolvedValue([])
    vi.mocked(api.listControlPlaneProjects).mockResolvedValue([])
    vi.mocked(api.getControlPlaneReadiness).mockResolvedValue({ ready: true, projects: [] })
    vi.mocked(api.getControlPlaneCapabilities).mockResolvedValue({
      schema_version: 1, workflows: [], teams: [], roles: [], controls: [],
      workflow_details: {}, admitted_role_selectors: {},
      context_levels: ['lite'], team_upgrade_chains: {}, control_safety: {}, service_features: [],
    })
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [], next_cursor: null, schema_version: 1 })
    vi.mocked(api.subscribeToRunEvents).mockReturnValue(() => {})
  })

  it('restores a valid server session on load without a token prompt', async () => {
    render(<App />)
    await waitFor(() => expect(api.listProjects).toHaveBeenCalled())
    expect(screen.queryByPlaceholderText('Auth token')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Login' })).toBeNull()
  })

  it('marks visible session restoration before requesting it and restores after remount', async () => {
    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' })
    vi.mocked(api.checkSession).mockImplementation(async () => {
      expect(consumeActivityMarker()).toBe(true)
      return { authenticated: true }
    })
    const first = render(<App />)
    await screen.findByRole('button', { name: 'Logout' })
    first.unmount()
    resetActivityMarker()
    render(<App />)
    await screen.findByRole('button', { name: 'Logout' })
    expect(api.checkSession).toHaveBeenCalledTimes(2)
    expect(screen.queryByPlaceholderText('Auth token')).toBeNull()
  })

  it('shows login only after a definitive 401 from the session check', async () => {
    vi.mocked(api.checkSession).mockRejectedValueOnce(new api.ApiError(401, 'unauthorized'))
    render(<App />)
    expect(await screen.findByPlaceholderText('Auth token')).toBeDefined()
    // The signed-out state is never announced for a connection failure.
    expect(screen.queryByText(/could not be reached/)).toBeNull()
  })

  it('offers Retry instead of a signed-out message when the session check cannot reach the server', async () => {
    vi.mocked(api.checkSession).mockRejectedValueOnce(new TypeError('fetch failed'))
    render(<App />)
    expect(await screen.findByText(/server could not be reached/)).toBeDefined()
    expect(screen.queryByPlaceholderText('Auth token')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(api.listProjects).toHaveBeenCalled())
    expect(screen.queryByText(/could not be reached/)).toBeNull()
  })

  it('logs in through the server, clears the token entry, and never stores the bearer', async () => {
    vi.mocked(api.checkSession).mockRejectedValueOnce(new api.ApiError(401, 'unauthorized'))
    render(<App />)
    const input = await screen.findByPlaceholderText('Auth token')
    expect(screen.getByText(/30 days of inactivity/)).toBeDefined()
    fireEvent.change(input, { target: { value: '  secret-token  ' } })
    fireEvent.click(screen.getByRole('button', { name: 'Login' }))
    await waitFor(() => expect(api.loginSession).toHaveBeenCalledWith('secret-token'))
    await waitFor(() => expect(api.listProjects).toHaveBeenCalled())
    expect((screen.queryByPlaceholderText('Auth token') as HTMLInputElement | null)?.value ?? '').toBe('')
    expect(api.setAuthToken).not.toHaveBeenCalled()
    expect(window.localStorage.length).toBe(0)
    expect(window.sessionStorage.length).toBe(0)
  })

  it('keeps the entered draft and explains a rejected login', async () => {
    vi.mocked(api.checkSession).mockRejectedValueOnce(new api.ApiError(401, 'unauthorized'))
    vi.mocked(api.loginSession).mockRejectedValueOnce(new api.ApiError(401, 'unauthorized'))
    render(<App />)
    const input = await screen.findByPlaceholderText('Auth token')
    fireEvent.change(input, { target: { value: 'wrong-token' } })
    fireEvent.click(screen.getByRole('button', { name: 'Login' }))
    expect(await screen.findByText(/did not accept that token/)).toBeDefined()
    expect((screen.getByPlaceholderText('Auth token') as HTMLInputElement).value).toBe('wrong-token')
  })

  it('keeps the entered draft through a transient login network failure', async () => {
    vi.mocked(api.checkSession).mockRejectedValueOnce(new api.ApiError(401, 'unauthorized'))
    vi.mocked(api.loginSession).mockRejectedValueOnce(new TypeError('fetch failed'))
    render(<App />)
    fireEvent.change(await screen.findByPlaceholderText('Auth token'), { target: { value: 'token' } })
    fireEvent.click(screen.getByRole('button', { name: 'Login' }))
    expect(await screen.findByText(/could not reach the server/)).toBeDefined()
    expect((screen.getByPlaceholderText('Auth token') as HTMLInputElement).value).toBe('token')
  })

  it('signs out only after the server confirms logout and returns to the login gate', async () => {
    vi.mocked(api.listProjects).mockResolvedValue([readyProject])
    render(<App />)
    fireEvent.click(await screen.findByRole('button', { name: 'Open' }))
    fireEvent.click(screen.getByRole('button', { name: 'Logout' }))
    await waitFor(() => expect(api.logoutSession).toHaveBeenCalledTimes(1))
    await screen.findByPlaceholderText('Auth token')
  })

  it('keeps the workspace and offers retryable feedback when logout cannot be confirmed', async () => {
    vi.mocked(api.listProjects).mockResolvedValue([readyProject])
    vi.mocked(api.logoutSession).mockRejectedValueOnce(new TypeError('fetch failed'))
    render(<App />)
    fireEvent.click(await screen.findByRole('button', { name: 'Open' }))
    fireEvent.click(screen.getByRole('button', { name: 'Logout' }))
    expect(await screen.findByText(/Logout could not be confirmed/)).toBeDefined()
    expect(screen.getByRole('button', { name: 'Retry logout' })).toBeDefined()
    expect(screen.getByRole('heading', { name: /How Alpha Project fits together/ })).toBeDefined()
    fireEvent.click(screen.getByRole('button', { name: 'Retry logout' }))
    await waitFor(() => expect(api.logoutSession).toHaveBeenCalledTimes(2))
    await screen.findByPlaceholderText('Auth token')
  })

  it('treats a 401 during ordinary use as session expiry and preserves the workspace for re-login', async () => {
    vi.mocked(api.listProjects).mockResolvedValue([readyProject])
    const expiredHandler = vi.fn()
    vi.mocked(api.setSessionExpiredHandler).mockImplementation((handler) => expiredHandler.mockImplementation(handler ?? (() => {})))
    render(<App />)
    fireEvent.click(await screen.findByRole('button', { name: 'Open' }))
    // Simulate a later authenticated request failing with 401.
    expiredHandler()
    expect(await screen.findByText(/Your session ended/)).toBeDefined()
    expect(screen.queryByRole('heading', { name: /How Alpha Project fits together/ })).toBeNull()
    // Re-login returns to the preserved project view.
    vi.mocked(api.loginSession).mockResolvedValue({ authenticated: true })
    fireEvent.change(await screen.findByPlaceholderText('Auth token'), { target: { value: 'token' } })
    fireEvent.click(screen.getByRole('button', { name: 'Login' }))
    expect(await screen.findByRole('heading', { name: /How Alpha Project fits together/ })).toBeDefined()
  })

  it('does not treat a server error as session expiry', async () => {
    vi.mocked(api.listProjects).mockRejectedValueOnce(new api.ApiError(503, 'control plane unavailable'))
    render(<App />)
    expect(await screen.findByText(/control plane unavailable/)).toBeDefined()
    expect(screen.getByRole('button', { name: 'Retry' })).toBeDefined()
    expect(screen.queryByText(/Your session ended/)).toBeNull()
    expect(screen.getByRole('button', { name: 'Logout' })).toBeDefined()
  })

  it('guides an empty registry into the create form and shows the server context', async () => {
    render(<App />)
    await waitFor(() => expect(api.listProjects).toHaveBeenCalled())
    await screen.findByText(/No registered projects/)
    expect(await screen.findByPlaceholderText('team/project')).toBeDefined()
    // Server context names the managed root without listing filesystem choices.
    expect(screen.getByText('/srv/code').className).toContain('mono')
    expect(screen.queryByText(/\/srv\/code\//)).toBeNull()
    expect(screen.getByRole('button', { name: 'Settings' }).getAttribute('disabled')).not.toBeNull()
    expect(screen.getByRole('button', { name: 'Plans' }).getAttribute('disabled')).not.toBeNull()
    expect(screen.getByRole('button', { name: 'Runs' }).getAttribute('disabled')).not.toBeNull()
  })

  it('registers a project with initialize-Git confirmation, initial workflow, and team', async () => {
    vi.mocked(api.createProject).mockResolvedValue({
      id: 'beta', display_name: 'Beta', relative_root: 'beta',
      root: '/srv/code/beta', created_at: '2026-01-01T00:00:00Z', readiness: 'configuration_required',
    })
    render(<App />)
    fireEvent.click(await screen.findByRole('radio', { name: /Register an existing directory/ }))
    fireEvent.change(screen.getByLabelText('Relative project path'), { target: { value: 'beta' } })
    fireEvent.change(screen.getByLabelText('Display name'), { target: { value: 'Beta' } })
    // Starter settings stay hidden until initialization is explicitly chosen.
    expect(screen.queryByLabelText('Initial workflow')).toBeNull()
    fireEvent.click(screen.getByRole('checkbox', { name: /Initialize a Git repository/ }))
    fireEvent.click(screen.getByRole('checkbox', { name: /Write the starter configuration pair/ }))
    fireEvent.change(screen.getByLabelText('Initial workflow'), { target: { value: 'starter-flow' } })
    fireEvent.change(screen.getByLabelText('Initial team'), { target: { value: 'core' } })
    vi.mocked(api.listProjects).mockResolvedValue([configProject])
    fireEvent.click(screen.getByRole('button', { name: 'Register project' }))

    await waitFor(() => expect(api.createProject).toHaveBeenCalledWith({
      mode: 'register',
      path: 'beta',
      display_name: 'Beta',
      main_branch: 'main',
      initial_workflow: 'starter-flow',
      initial_team: 'core',
      initialize_git: true,
      initialize_config: true,
    }))
    await screen.findByLabelText('aflow.toml contents')
    expect(screen.getByText(/needs explicit configuration/)).toBeDefined()
    expect(screen.getByText('Configuration required')).toBeDefined()
  })

  it('does not submit hidden starter values when register-mode initialization is off', async () => {
    vi.mocked(api.createProject).mockRejectedValueOnce(new Error('operation_rejected'))
    render(<App />)
    fireEvent.click(await screen.findByRole('radio', { name: /Register an existing directory/ }))
    fireEvent.change(screen.getByLabelText('Relative project path'), { target: { value: 'beta' } })
    fireEvent.click(screen.getByRole('checkbox', { name: /Write the starter configuration pair/ }))
    fireEvent.change(screen.getByLabelText('Initial workflow'), { target: { value: 'not a valid workflow' } })
    fireEvent.change(screen.getByLabelText('Initial team'), { target: { value: 'ghost-team' } })
    fireEvent.click(screen.getByRole('checkbox', { name: /Write the starter configuration pair/ }))
    expect(screen.queryByLabelText('Initial workflow')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Register project' }))

    await waitFor(() => expect(api.createProject).toHaveBeenCalledWith({
      mode: 'register',
      path: 'beta',
      display_name: null,
      main_branch: 'main',
      initial_workflow: null,
      initial_team: null,
      initialize_git: false,
      initialize_config: false,
    }))
    // Re-enabling initialization preserves the entered values.
    fireEvent.click(screen.getByRole('checkbox', { name: /Write the starter configuration pair/ }))
    expect((screen.getByLabelText('Initial workflow') as HTMLInputElement).value).toBe('not a valid workflow')
    expect((screen.getByLabelText('Initial team') as HTMLInputElement).value).toBe('ghost-team')
  })

  it('creates a new project and lands in configuration guidance', async () => {
    vi.mocked(api.createProject).mockResolvedValue({
      id: 'beta', display_name: 'Beta', relative_root: 'beta',
      root: '/srv/code/beta', created_at: '2026-01-01T00:00:00Z', readiness: 'configuration_required',
    })
    render(<App />)
    fireEvent.change(await screen.findByLabelText('Relative project path'), { target: { value: 'beta' } })
    vi.mocked(api.listProjects).mockResolvedValue([configProject])
    fireEvent.click(screen.getByRole('button', { name: 'Create project' }))

    await waitFor(() => expect(api.createProject).toHaveBeenCalledWith({
      mode: 'create',
      path: 'beta',
      display_name: null,
      main_branch: 'main',
      initial_workflow: null,
      initial_team: null,
    }))
    await screen.findByLabelText('aflow.toml contents')
    expect(screen.getByText(/needs explicit configuration/)).toBeDefined()
  })

  it('keeps a successful creation selected when the refresh read fails', async () => {
    vi.mocked(api.createProject).mockResolvedValue({
      id: 'beta', display_name: 'Beta', relative_root: 'beta',
      root: '/srv/code/beta', created_at: '2026-01-01T00:00:00Z', readiness: 'configuration_required',
    })
    vi.mocked(api.listProjects).mockResolvedValueOnce([]).mockRejectedValueOnce(new Error('refresh unavailable'))
    render(<App />)
    await screen.findByLabelText('Relative project path')
    fireEvent.change(screen.getByLabelText('Relative project path'), { target: { value: 'beta' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create project' }))

    await screen.findByLabelText('aflow.toml contents')
    expect(screen.queryByText(/Failed to create or register/)).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Projects' }))
    await screen.findByText(/Project Beta was created, but the project list could not refresh/)
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(api.listProjects).toHaveBeenCalledTimes(3))
    expect(api.createProject).toHaveBeenCalledTimes(1)
  })

  it('guards shell navigation away from an unsaved plan draft', async () => {
    const todoPlan = {
      project_id: 'alpha', name: 'draft.md', path: 'plans/todo/draft.md',
      status: 'todo' as const, revision: 'c'.repeat(64), size_bytes: 7, content: '# Draft\n',
    }
    vi.mocked(api.listProjects).mockResolvedValue([readyProject])
    vi.mocked(api.listProjectPlans).mockResolvedValue([todoPlan])
    vi.mocked(api.readProjectPlan).mockResolvedValue(todoPlan)
    render(<App />)
    fireEvent.click(await screen.findByRole('button', { name: /Alpha Project/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Plans' }))
    fireEvent.click(await screen.findByRole('button', { name: /draft\.md/ }))
    fireEvent.change(await screen.findByLabelText('Plan content'), { target: { value: '# Unsaved\n' } })
    fireEvent.click(screen.getByRole('button', { name: 'Settings' }))

    expect(screen.getByRole('alertdialog', { name: 'Unsaved editor edits' })).toBeDefined()
    fireEvent.click(screen.getByRole('button', { name: 'Stay' }))
    expect(screen.getByLabelText('Plan content')).toBeDefined()
  })

  it('updates selected readiness from a successful ready configuration save', async () => {
    vi.mocked(api.listProjects).mockResolvedValue([configProject])
    vi.mocked(api.saveProjectConfig).mockResolvedValue(configPayload('ready'))
    render(<App />)
    fireEvent.click(await screen.findByRole('button', { name: /Beta Project/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Settings' }))
    await screen.findByLabelText('aflow.toml contents')
    fireEvent.change(screen.getByLabelText('aflow.toml contents'), { target: { value: '# ready\n' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save both files' }))
    await waitFor(() => expect(screen.getByText('Ready')).toBeDefined())
    expect(screen.queryByText(/needs explicit configuration/)).toBeNull()
    expect(screen.getByLabelText('aflow.toml contents')).toBeDefined()
    fireEvent.click(await screen.findByRole('button', { name: 'Go to plans' }))

    await screen.findByLabelText('New plan filename')
    expect(screen.getByText('Ready')).toBeDefined()
    expect(screen.queryByText(/needs explicit configuration/)).toBeNull()
  })

  it('shows project load errors and offers retry without inventing choices', async () => {
    vi.mocked(api.listProjects).mockRejectedValue(new Error('connection refused'))
    render(<App />)
    await screen.findByText(/connection refused/)
    expect(screen.getByRole('button', { name: 'Retry' })).toBeDefined()
    expect(screen.queryByPlaceholderText('team/project')).toBeNull()
    vi.mocked(api.listProjects).mockResolvedValue([])
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await screen.findByPlaceholderText('team/project')
  })

  it('visibly explains the no-project state and keeps the create flow reachable while scoped views are disabled', async () => {
    render(<App />)
    await screen.findByText(/No registered projects/)
    expect(screen.getByRole('note').textContent)
      .toMatch(/become available after you open a project/)
    expect(screen.getByRole('button', { name: 'Projects' }).getAttribute('disabled')).toBeNull()
    expect(screen.getByRole('button', { name: 'Projects' }).getAttribute('aria-current')).toBe('page')
    expect(screen.getByRole('button', { name: 'Overview' }).getAttribute('disabled')).not.toBeNull()
    expect(screen.getByRole('button', { name: 'Settings' }).getAttribute('disabled')).not.toBeNull()
    expect(screen.getByRole('button', { name: 'Plans' }).getAttribute('disabled')).not.toBeNull()
    expect(screen.getByRole('button', { name: 'Runs' }).getAttribute('disabled')).not.toBeNull()
    expect(await screen.findByPlaceholderText('team/project')).toBeDefined()
    expect(screen.getByRole('button', { name: 'Refresh' })).toBeDefined()
  })

  it('returns a blocked project to Projects for re-checking instead of emphasizing Settings', async () => {
    vi.mocked(api.listProjects).mockResolvedValue([blockedProject])
    render(<App />)
    fireEvent.click(await screen.findByRole('button', { name: 'Open' }))
    expect(await screen.findByRole('heading', { name: /How Gamma Project fits together/ })).toBeDefined()
    expect(screen.getByText(/not a usable Git root yet/)).toBeDefined()

    const back = screen.getByRole('button', { name: 'Back to Projects (re-check after repair)' })
    expect(back.className).toContain('btn-primary')
    expect(screen.getByRole('button', { name: 'Open settings' }).className).not.toContain('btn-primary')
    fireEvent.click(back)

    const refresh = await screen.findByRole('button', { name: 'Refresh' })
    expect(screen.getByRole('button', { name: 'Projects' }).getAttribute('aria-current')).toBe('page')
    expect(screen.queryByLabelText('aflow.toml contents')).toBeNull()
    fireEvent.click(refresh)
    await waitFor(() => expect(api.listProjects).toHaveBeenCalledTimes(2))
    expect(api.getProjectConfig).not.toHaveBeenCalled()
  })

  it('renders readiness states accurately across registered projects', async () => {
    vi.mocked(api.listProjects).mockResolvedValue([readyProject, configProject, blockedProject])
    render(<App />)
    await screen.findByText('Alpha Project')
    expect(screen.getByText('Ready')).toBeDefined()
    expect(screen.getByText('Configuration required')).toBeDefined()
    expect(screen.getByText('Blocked')).toBeDefined()
    expect(screen.getByRole('button', { name: 'Projects' }).getAttribute('aria-current')).toBe('page')
  })

  it('unregisters only from the registry and states that files are preserved', async () => {
    vi.mocked(api.listProjects).mockResolvedValue([readyProject])
    vi.mocked(api.unregisterProject).mockResolvedValue(undefined)
    render(<App />)
    fireEvent.click(await screen.findByRole('button', { name: 'Unregister…' }))
    expect(screen.getByText(/Files, Git history, and plans on disk are preserved/)).toBeDefined()
    fireEvent.click(screen.getByRole('button', { name: 'Unregister (keeps files)' }))
    await waitFor(() => expect(api.unregisterProject).toHaveBeenCalledWith('alpha'))
    await waitFor(() => expect(api.listProjects).toHaveBeenCalledTimes(2))
  })

  it('refreshes discovery after unregister so the candidate is immediately addable again', async () => {
    vi.mocked(api.listProjects).mockResolvedValueOnce([readyProject]).mockResolvedValue([])
    vi.mocked(api.unregisterProject).mockResolvedValue(undefined)
    vi.mocked(api.getProjectDiscovery)
      .mockResolvedValueOnce({
        ...discoveryBase,
        candidates: [
          { relative_path: 'alpha', display_name: 'Alpha Project', registered_project_id: 'alpha', addable: false, add_blocker: 'already registered' },
        ],
      })
      .mockResolvedValue({
        ...discoveryBase,
        candidates: [
          { relative_path: 'alpha', display_name: 'Alpha', registered_project_id: null, addable: true, add_blocker: null },
        ],
      })
    render(<App />)
    const candidateItem = (await screen.findByText('alpha')).closest('[role="listitem"]') as HTMLElement
    expect(within(candidateItem).getByText('Added')).toBeDefined()
    expect(within(candidateItem).queryByRole('button', { name: 'Add', exact: true })).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Unregister…' }))
    fireEvent.click(screen.getByRole('button', { name: 'Unregister (keeps files)' }))
    await waitFor(() => expect(api.unregisterProject).toHaveBeenCalledWith('alpha'))
    // Both the registry list and discovery refresh; no second manual refresh.
    await waitFor(() => expect(api.listProjects).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(api.getProjectDiscovery).toHaveBeenCalledTimes(2))
    expect(screen.queryByText('Added')).toBeNull()
    expect(screen.queryByText(/Refresh to open/)).toBeNull()
    expect(screen.getByRole('button', { name: 'Add', exact: true })).toBeDefined()
  })

  it('guards navigation away from unsaved configuration edits', async () => {
    vi.mocked(api.listProjects).mockResolvedValue([readyProject])
    render(<App />)
    fireEvent.click(await screen.findByRole('button', { name: /Alpha Project/ }))
    fireEvent.click(await screen.findByRole('button', { name: 'Settings' }))
    await screen.findByLabelText('aflow.toml contents')
    fireEvent.change(screen.getByLabelText('aflow.toml contents'), { target: { value: '# edited\n' } })
    await screen.findByText(/Unsaved edits/)

    fireEvent.click(screen.getByRole('button', { name: 'Plans' }))
    expect(screen.getByText(/You have unsaved editor edits/)).toBeDefined()
    fireEvent.click(screen.getByRole('button', { name: 'Stay' }))
    expect(screen.getByLabelText('aflow.toml contents')).toBeDefined()

    fireEvent.click(screen.getByRole('button', { name: 'Plans' }))
    fireEvent.click(screen.getByRole('button', { name: 'Leave anyway' }))
    await screen.findByLabelText('New plan filename')
  })

  it('opens the run dashboard from an in-progress plan', async () => {
    const inProgressPlan = {
      project_id: 'alpha', name: 'demo.md', path: 'plans/in-progress/demo.md',
      status: 'in_progress' as const, revision: 'c'.repeat(64), size_bytes: 7, content: '# Demo\n',
    }
    vi.mocked(api.listProjects).mockResolvedValue([readyProject])
    vi.mocked(api.listProjectPlans).mockResolvedValue([inProgressPlan])
    vi.mocked(api.readProjectPlan).mockResolvedValue(inProgressPlan)
    render(<App />)
    fireEvent.click(await screen.findByRole('button', { name: /Alpha Project/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Plans' }))
    const planButton = await screen.findByRole('button', { name: /demo\.md/ })
    fireEvent.click(planButton)
    fireEvent.click(await screen.findByText('Open run dashboard'))
    await screen.findByText('Run dashboard')
  })

  it('enters the same project workspace when Open is clicked twice with Projects between clicks', async () => {
    vi.mocked(api.listProjects).mockResolvedValue([readyProject])
    render(<App />)
    fireEvent.click(await screen.findByRole('button', { name: 'Open' }))
    expect(await screen.findByRole('heading', { name: /How Alpha Project fits together/ })).toBeDefined()
    expect(screen.getByRole('button', { name: 'Overview' }).getAttribute('aria-current')).toBe('page')
    expect(screen.getByText('/srv/code/alpha')).toBeDefined()

    fireEvent.click(screen.getByRole('button', { name: 'Projects' }))
    await screen.findByText(/Registered beneath the server's managed root/)
    fireEvent.click(screen.getByRole('button', { name: 'Open' }))
    expect(await screen.findByRole('heading', { name: /How Alpha Project fits together/ })).toBeDefined()
    expect(screen.getByRole('button', { name: 'Overview' }).getAttribute('aria-current')).toBe('page')
  })

  it('guards reopening the already-selected project from Open with unsaved edits', async () => {
    vi.mocked(api.listProjects).mockResolvedValue([readyProject])
    render(<App />)
    fireEvent.click(await screen.findByRole('button', { name: 'Open' }))
    fireEvent.click(screen.getByRole('button', { name: 'Settings' }))
    await screen.findByLabelText('aflow.toml contents')
    fireEvent.change(screen.getByLabelText('aflow.toml contents'), { target: { value: '# edited\n' } })
    await screen.findByText(/Unsaved edits/)

    fireEvent.click(screen.getByRole('button', { name: 'Change project' }))
    expect(screen.getByRole('alertdialog', { name: 'Unsaved editor edits' })).toBeDefined()
    fireEvent.click(screen.getByRole('button', { name: 'Stay' }))
    expect(screen.getByLabelText('aflow.toml contents')).toBeDefined()

    fireEvent.click(screen.getByRole('button', { name: 'Change project' }))
    fireEvent.click(screen.getByRole('button', { name: 'Leave anyway' }))
    await screen.findByRole('button', { name: 'Open' })
    fireEvent.click(screen.getByRole('button', { name: 'Open' }))
    expect(await screen.findByRole('heading', { name: /How Alpha Project fits together/ })).toBeDefined()
  })

  it('shows discovery context, filters as typed, and adds an addable candidate', async () => {
    vi.mocked(api.listProjects).mockResolvedValue([readyProject])
    vi.mocked(api.getProjectDiscovery).mockResolvedValue({
      ...discoveryBase,
      truncated: true,
      candidates: [
        { relative_path: 'alpha', display_name: 'Alpha Project', registered_project_id: 'alpha', addable: false, add_blocker: 'already registered' },
        { relative_path: 'tools/kilo', display_name: 'Kilo', registered_project_id: null, addable: true, add_blocker: null },
        { relative_path: 'limbo', display_name: 'Limbo', registered_project_id: null, addable: false, add_blocker: 'repository HEAD does not point to a commit' },
      ],
    })
    vi.mocked(api.createProject).mockResolvedValue({
      id: 'kilo', display_name: 'Kilo', relative_root: 'tools/kilo',
      root: '/srv/code/tools/kilo', created_at: '2026-01-01T00:00:00Z', readiness: 'configuration_required',
    })
    render(<App />)
    expect(await screen.findByText('Available on this server')).toBeDefined()
    expect(screen.getByText('Added projects')).toBeDefined()
    expect(screen.getByText('tools/kilo')).toBeDefined()
    expect(screen.getByText(/Cannot be added: repository HEAD does not point to a commit/)).toBeDefined()
    expect(screen.getByText(/Results were limited/)).toBeDefined()

    // Unregistered candidates never expose a no-op content control: an
    // addable candidate has only its real Add action and a blocked candidate
    // has no button at all, so nothing extra is keyboard-focusable.
    const kiloItem = screen.getByText('tools/kilo').closest('[role="listitem"]') as HTMLElement
    expect(within(kiloItem).getAllByRole('button')).toHaveLength(1)
    expect(kiloItem.querySelector('.content-button')).toBeNull()
    const limboItem = screen.getByText('limbo').closest('[role="listitem"]') as HTMLElement
    expect(within(limboItem).queryAllByRole('button')).toHaveLength(0)
    // A registered candidate's content remains an interactive open control.
    const alphaItem = screen.getByText('alpha').closest('[role="listitem"]') as HTMLElement
    expect(within(alphaItem).getByRole('button', { name: /Alpha Project/ })).toBeDefined()

    fireEvent.change(screen.getByLabelText('Search projects and available candidates'), { target: { value: 'KIL' } })
    expect(await screen.findByText(/No added projects match/)).toBeDefined()
    expect(screen.queryByText('Limbo')).toBeNull()
    expect(screen.getByText('tools/kilo')).toBeDefined()

    fireEvent.click(screen.getByRole('button', { name: 'Add', exact: true }))
    await waitFor(() => expect(api.createProject).toHaveBeenCalledWith({
      mode: 'register',
      path: 'tools/kilo',
      display_name: 'Kilo',
      main_branch: 'main',
      initial_workflow: null,
      initial_team: null,
      initialize_git: false,
      initialize_config: false,
    }))
    await screen.findByLabelText('aflow.toml contents')
  })

  it('preserves search and refreshes both lists when Add loses a race', async () => {
    const kiloProject = {
      id: 'kilo', display_name: 'Kilo', current_path: '/srv/code/kilo',
      is_git_root: true, registered_at: '2026-01-01T00:00:00Z', readiness: 'configuration_required' as const,
    }
    vi.mocked(api.listProjects).mockResolvedValueOnce([]).mockResolvedValue([kiloProject])
    vi.mocked(api.getProjectDiscovery)
      .mockResolvedValueOnce({
        ...discoveryBase,
        candidates: [
          { relative_path: 'kilo', display_name: 'Kilo', registered_project_id: null, addable: true, add_blocker: null },
        ],
      })
      .mockResolvedValue({
        ...discoveryBase,
        candidates: [
          { relative_path: 'kilo', display_name: 'Kilo', registered_project_id: 'kilo', addable: false, add_blocker: 'already registered' },
        ],
      })
    vi.mocked(api.createProject).mockRejectedValueOnce(new Error('operation_rejected'))
    render(<App />)
    fireEvent.change(await screen.findByLabelText('Search projects and available candidates'), { target: { value: 'kilo' } })
    fireEvent.click(await screen.findByRole('button', { name: 'Add', exact: true }))

    expect(await screen.findByText('operation_rejected')).toBeDefined()
    const search = screen.getByLabelText('Search projects and available candidates') as HTMLInputElement
    expect(search.value).toBe('kilo')
    await waitFor(() => expect(api.listProjects).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(api.getProjectDiscovery).toHaveBeenCalledTimes(2))
    // The raced registration is openable without a second manual refresh.
    const candidateItem = screen.getByText('kilo').closest('[role="listitem"]') as HTMLElement
    expect(within(candidateItem).getByRole('button', { name: 'Open' })).toBeDefined()
  })

  it('recovers discovery failures and explains an empty server', async () => {
    vi.mocked(api.getProjectDiscovery)
      .mockRejectedValueOnce(new Error('scan refused'))
      .mockResolvedValueOnce(discoveryBase)
    render(<App />)
    expect(await screen.findByText(/Existing projects could not be listed: scan refused/)).toBeDefined()
    fireEvent.click(screen.getByRole('button', { name: 'Retry discovery' }))
    expect(await screen.findByText(/No discoverable Git projects/)).toBeDefined()
    expect(screen.getByText(/two levels/)).toBeDefined()
  })

  it('offers discovered candidates as register-form suggestions that fill the path', async () => {
    vi.mocked(api.getProjectDiscovery).mockResolvedValue({
      ...discoveryBase,
      candidates: [
        { relative_path: 'tools/kilo', display_name: 'Kilo', registered_project_id: null, addable: true, add_blocker: null },
      ],
    })
    render(<App />)
    fireEvent.click(await screen.findByRole('radio', { name: /Register an existing directory/ }))
    const suggestionList = await screen.findByRole('list', { name: 'Discovered project candidates' })
    fireEvent.click(within(suggestionList).getByRole('button', { name: /Kilo/ }))
    expect((screen.getByLabelText('Relative project path') as HTMLInputElement).value).toBe('tools/kilo')
  })
})
