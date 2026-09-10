import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from './api'
import * as api from './api'
import { App } from './App'
import { resetActivityMarker } from './activity'

vi.mock('./api', async () => {
  const actual = await vi.importActual<typeof import('./api')>('./api')
  return {
    ...actual,
    checkSession: vi.fn(),
    loginSession: vi.fn(),
    logoutSession: vi.fn(),
    listProjects: vi.fn(),
    getProjectDiscovery: vi.fn(),
    createProject: vi.fn(),
    unregisterProject: vi.fn(),
    getSettings: vi.fn(),
    saveSettings: vi.fn(),
    projectSettingsText: vi.fn(),
    getGlobalConfig: vi.fn(),
    patchGlobalConfig: vi.fn(),
    validateGlobalConfig: vi.fn(),
    postGlobalConfigForm: vi.fn(),
    listSkills: vi.fn(),
    readSkill: vi.fn(),
    saveSkill: vi.fn(),
    validateSkills: vi.fn(),
    installSkills: vi.fn(),
    listProjectPlans: vi.fn(),
    createProjectPlan: vi.fn(),
    readProjectPlan: vi.fn(),
    updateProjectPlan: vi.fn(),
    promoteProjectPlan: vi.fn(),
    listControlPlaneProjects: vi.fn(),
    getControlPlaneReadiness: vi.fn(),
    getControlPlaneCapabilities: vi.fn(),
    listControlPlanePlans: vi.fn(),
    listControlPlaneRuns: vi.fn(),
    getControlPlaneRun: vi.fn(),
    getRestartOptions: vi.fn(),
    listRunEvents: vi.fn(),
    getRunContext: vi.fn(),
    startControlPlaneRun: vi.fn(),
    answerStartupQuestion: vi.fn(),
    controlControlPlaneRun: vi.fn(),
    ownerStopControlPlaneRun: vi.fn(),
    resumeControlPlaneRun: vi.fn(),
    subscribeToRunEvents: vi.fn(),
    setSessionExpiredHandler: vi.fn(),
  }
})

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

function guidedFormResponse(validation = configPayload().validation) {
  return {
    aflow_toml: '# aflow config\n',
    workflows_toml: '# workflows\n',
    changed: false,
    validation,
    form: {
      default_workflow: null,
      max_turns: null,
      harnesses: {},
      roles: {},
      teams: {},
      workflow_default_teams: {},
      workflows: {},
    },
    syntax_issues: [],
    choices: { harnesses: [], profiles: {}, selectors: [], roles: [], teams: [], workflows: [] },
    suggestions: {
      label: 'suggestion',
      harnesses: [{ name: 'codex', supports_effort: false, custom_model_supported: true }],
      profiles: [],
      note: 'Bundled values are labeled suggestions.',
    },
    starter_defaults: null,
  }
}

const controlPlaneProject = { project_id: 'alpha', root: '/srv/code/alpha', schema_version: 1 }

const dashboardRun = {
  run_id: 'run-9', status: 'running', schema_version: 1, ownership: 'control_plane' as const,
  revision: 1, reason: null, unit_name: 'aflow-run-run-9.service', launch_phase: 'running',
  workflow_name: 'starter', team: null, current_step: 'implement', turns_completed: 1, max_turns: 5,
  selected_start_step: null, skipped_steps: [] as string[], restarted_from_run_id: null as string | null,
  started_at: '2026-01-01T00:00:00Z',
  evidence: { manifest_created_at: '2026-01-01T00:00:00Z', plan_path: 'plans/in-progress/demo.md' },
}

const controlPlaneCapabilities = {
  schema_version: 1,
  workflows: ['starter'],
  teams: [] as string[],
  roles: ['worker'],
  controls: ['max_turns', 'team', 'role_selectors', 'owner_stop'],
  workflow_details: {
    starter: {
      declared_steps: ['implement'],
      executable_steps: ['implement'],
      excluded_steps: [] as string[],
      first_step: 'implement',
      default_team: null,
    },
  },
  admitted_role_selectors: { worker: ['codex.worker'] },
  context_levels: ['lite', 'full'] as const,
  team_upgrade_chains: {} as Record<string, string[]>,
  control_safety: { max_turns: 'safe' as const, team: 'safe' as const, role_selectors: 'safe' as const, owner_stop: 'safe' as const },
  service_features: ['controls'],
}

/** Mocks everything the run dashboard touches so runs/new-run views render. */
function mockRunDashboard(overrides: { runs?: typeof dashboardRun[] } = {}) {
  const runs = overrides.runs ?? [dashboardRun]
  vi.mocked(api.listControlPlaneProjects).mockResolvedValue([controlPlaneProject])
  vi.mocked(api.getControlPlaneReadiness).mockResolvedValue({ ready: true, projects: ['alpha'] })
  vi.mocked(api.getControlPlaneCapabilities).mockResolvedValue(controlPlaneCapabilities)
  vi.mocked(api.listControlPlanePlans).mockResolvedValue([{ path: 'plans/in-progress/demo.md', status: 'in_progress', modified_at: '2026-01-01T00:00:00Z', schema_version: 1 }])
  vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs, next_cursor: null, schema_version: 1 })
  vi.mocked(api.getControlPlaneRun).mockResolvedValue(dashboardRun)
  vi.mocked(api.listRunEvents).mockResolvedValue([])
  vi.mocked(api.getRunContext).mockResolvedValue({ run_id: 'run-9', level: 'lite', data: {}, schema_version: 1 })
  vi.mocked(api.subscribeToRunEvents).mockReturnValue(() => {})
}

function setUrl(search: string) {
  // happy-dom updates the location getter synchronously only through href.
  window.location.href = window.location.origin + search
}

function mockMatchMedia(initialMatches: boolean) {
  const original = window.matchMedia
  let matches = initialMatches
  const listeners = new Set<(event: MediaQueryListEvent) => void>()
  const media = {
    media: '(max-width: 959px), (max-height: 599px)',
    onchange: null,
    addEventListener: (_type: string, listener: (event: MediaQueryListEvent) => void) => listeners.add(listener),
    removeEventListener: (_type: string, listener: (event: MediaQueryListEvent) => void) => listeners.delete(listener),
    addListener: (listener: (event: MediaQueryListEvent) => void) => listeners.add(listener),
    removeListener: (listener: (event: MediaQueryListEvent) => void) => listeners.delete(listener),
    dispatchEvent: () => true,
  } as unknown as MediaQueryList
  Object.defineProperty(media, 'matches', { configurable: true, get: () => matches })
  Object.defineProperty(window, 'matchMedia', { configurable: true, writable: true, value: vi.fn(() => media) })
  return {
    setMatches(next: boolean) {
      matches = next
      const event = { matches: next, media: media.media } as MediaQueryListEvent
      listeners.forEach(listener => listener(event))
    },
    restore() {
      Object.defineProperty(window, 'matchMedia', { configurable: true, writable: true, value: original })
    },
  }
}

/** Opens an added project from its compact row (the row itself is the open control). */
async function openAddedProject(name: RegExp) {
  const list = await screen.findByRole('list', { name: 'Added projects' })
  // Anchored so the row button matches without its More-actions trigger.
  fireEvent.click(within(list).getByRole('button', { name: new RegExp('^' + name.source, name.flags) }))
}

async function openAdvancedSettings() {
  fireEvent.click(screen.getByRole('button', { name: 'More', exact: true }))
  const advanced = await screen.findByRole('menuitem', { name: 'Advanced TOML', exact: true })
  await waitFor(() => expect((advanced as HTMLButtonElement).disabled).toBe(false))
  fireEvent.click(advanced)
  await screen.findByLabelText('aflow.toml contents')
}

async function clickHeaderMenuItem(name: string | RegExp) {
  fireEvent.click(screen.getByRole('button', { name: 'More', exact: true }))
  fireEvent.click(await screen.findByRole('menuitem', { name }))
}

describe('App workspace shell', () => {
  beforeEach(() => {
    window.location.href = window.location.origin + '/?view=projects'
    vi.clearAllMocks()
    resetActivityMarker()
    vi.mocked(api.checkSession).mockResolvedValue({ authenticated: true })
    vi.mocked(api.loginSession).mockResolvedValue({ authenticated: true })
    vi.mocked(api.logoutSession).mockResolvedValue(undefined)
    vi.mocked(api.getSettings).mockResolvedValue({ bind_host: 'localhost', bind_port: 8766, managed_projects_root: '/srv/code', password_set: true, revision: 'a'.repeat(64), advanced_toml: '', restart: { bind_host: false, bind_port: false, managed_projects_root: false } })
    vi.mocked(api.getRestartOptions).mockResolvedValue({ eligible: false, reason: null, requires_stop: false, run_id: '', extra_instructions_unavailable: false, options: { plan_path: '' } })
    vi.mocked(api.validateGlobalConfig).mockResolvedValue(configPayload('ready').validation)
    vi.mocked(api.listProjects).mockResolvedValue([])
    vi.mocked(api.listProjectPlans).mockResolvedValue([])
    vi.mocked(api.getProjectDiscovery).mockResolvedValue(discoveryBase)
    vi.mocked(api.getGlobalConfig).mockResolvedValue(configPayload())
    vi.mocked(api.postGlobalConfigForm).mockResolvedValue(guidedFormResponse())
    vi.mocked(api.listSkills).mockResolvedValue([])
    // Any test that reaches the runs dashboard needs these defaults.
    vi.mocked(api.listControlPlaneProjects).mockResolvedValue([controlPlaneProject])
    vi.mocked(api.getControlPlaneReadiness).mockResolvedValue({ ready: true, projects: ['alpha'] })
    vi.mocked(api.getControlPlaneCapabilities).mockResolvedValue(controlPlaneCapabilities)
    vi.mocked(api.listControlPlanePlans).mockResolvedValue([])
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [], next_cursor: null, schema_version: 1 })
    vi.mocked(api.getControlPlaneRun).mockResolvedValue(dashboardRun)
    vi.mocked(api.listRunEvents).mockResolvedValue([])
    vi.mocked(api.getRunContext).mockResolvedValue({ run_id: 'run-9', level: 'lite', data: {}, schema_version: 1 })
    vi.mocked(api.subscribeToRunEvents).mockReturnValue(() => {})
  })

  it('restores a valid server session on load without a token prompt', async () => {
    render(<App />)
    await screen.findByText(/No registered projects/)
    expect(screen.queryByPlaceholderText('Auth token')).toBeNull()
    expect(api.checkSession).toHaveBeenCalledTimes(1)
  })

  it('marks visible session restoration before requesting it and restores after remount', async () => {
    const view = render(<App />)
    await screen.findByText(/No registered projects/)
    expect(api.checkSession).toHaveBeenCalledTimes(1)
    view.unmount()
    render(<App />)
    await screen.findByText(/No registered projects/)
    expect(api.checkSession).toHaveBeenCalledTimes(2)
    expect(screen.queryByPlaceholderText('Auth token')).toBeNull()
  })

  it('shows login only after a definitive 401 from the session check', async () => {
    vi.mocked(api.checkSession).mockRejectedValueOnce(new ApiError(401, 'unauthorized'))
    render(<App />)
    expect(await screen.findByPlaceholderText('Auth token')).toBeDefined()
  })

  it('offers Retry instead of a signed-out message when the session check cannot reach the server', async () => {
    vi.mocked(api.checkSession).mockRejectedValueOnce(new TypeError('fetch failed'))
    render(<App />)
    expect(await screen.findByText(/could not be reached to check your session/)).toBeDefined()
    expect(screen.queryByPlaceholderText('Auth token')).toBeNull()
    vi.mocked(api.checkSession).mockResolvedValueOnce({ authenticated: true })
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await screen.findByText(/No registered projects/)
  })

  it('logs in through the server, clears the token entry, and never stores the bearer', async () => {
    vi.mocked(api.checkSession).mockRejectedValueOnce(new ApiError(401, 'unauthorized'))
    render(<App />)
    const input = await screen.findByPlaceholderText('Auth token')
    fireEvent.change(input, { target: { value: 'deployment-token' } })
    fireEvent.click(screen.getByRole('button', { name: 'Login' }))
    await screen.findByText(/No registered projects/)
    expect(api.loginSession).toHaveBeenCalledWith('deployment-token')
    expect(screen.queryByPlaceholderText('Auth token')).toBeNull()
    expect(api.getAuthToken()).toBeNull()
  })

  it('keeps the entered draft and explains a rejected login', async () => {
    vi.mocked(api.checkSession).mockRejectedValueOnce(new ApiError(401, 'unauthorized'))
    vi.mocked(api.loginSession).mockRejectedValueOnce(new ApiError(401, 'unauthorized'))
    render(<App />)
    const input = await screen.findByPlaceholderText('Auth token')
    fireEvent.change(input, { target: { value: 'wrong-token' } })
    fireEvent.click(screen.getByRole('button', { name: 'Login' }))
    expect(await screen.findByText(/did not accept that token/)).toBeDefined()
    expect((screen.getByPlaceholderText('Auth token') as HTMLInputElement).value).toBe('wrong-token')
  })

  it('keeps the entered draft through a transient login network failure', async () => {
    vi.mocked(api.checkSession).mockRejectedValueOnce(new ApiError(401, 'unauthorized'))
    vi.mocked(api.loginSession).mockRejectedValueOnce(new TypeError('fetch failed'))
    render(<App />)
    const input = await screen.findByPlaceholderText('Auth token')
    fireEvent.change(input, { target: { value: 'token' } })
    fireEvent.click(screen.getByRole('button', { name: 'Login' }))
    expect(await screen.findByText(/could not reach the server/)).toBeDefined()
    expect((screen.getByPlaceholderText('Auth token') as HTMLInputElement).value).toBe('token')
  })

  it('signs out only after the server confirms logout and returns to the login gate', async () => {
    vi.mocked(api.listProjects).mockResolvedValue([readyProject])
    render(<App />)
    await openAddedProject(/Alpha Project/)
    fireEvent.click(screen.getByRole('button', { name: 'Logout' }))
    await waitFor(() => expect(api.logoutSession).toHaveBeenCalledTimes(1))
    await screen.findByPlaceholderText('Auth token')
  })

  it('keeps the workspace and offers retryable feedback when logout cannot be confirmed', async () => {
    vi.mocked(api.listProjects).mockResolvedValue([readyProject])
    vi.mocked(api.logoutSession).mockRejectedValueOnce(new TypeError('fetch failed'))
    render(<App />)
    await openAddedProject(/Alpha Project/)
    fireEvent.click(screen.getByRole('button', { name: 'Logout' }))
    expect(await screen.findByText(/Logout could not be confirmed/)).toBeDefined()
    expect(screen.getByRole('button', { name: 'Retry logout' })).toBeDefined()
    expect(screen.getByRole('heading', { name: 'Runs' })).toBeDefined()
    fireEvent.click(screen.getByRole('button', { name: 'Retry logout' }))
    await waitFor(() => expect(api.logoutSession).toHaveBeenCalledTimes(2))
    await screen.findByPlaceholderText('Auth token')
  })

  it('treats a 401 during ordinary use as session expiry and preserves the workspace for re-login', async () => {
    vi.mocked(api.listProjects).mockResolvedValue([readyProject])
    const expiredHandler = vi.fn()
    vi.mocked(api.setSessionExpiredHandler).mockImplementation((handler) => {
      expiredHandler.mockImplementation(handler ?? (() => {}))
      return () => {}
    })
    render(<App />)
    await openAddedProject(/Alpha Project/)
    // Simulate a later authenticated request failing with 401.
    expiredHandler()
    expect(await screen.findByText(/Your session ended/)).toBeDefined()
    expect(screen.queryByRole('heading', { name: 'Runs' })).toBeNull()
    // Re-login returns to the preserved project view.
    vi.mocked(api.loginSession).mockResolvedValue({ authenticated: true })
    fireEvent.change(await screen.findByPlaceholderText('Auth token'), { target: { value: 'token' } })
    fireEvent.click(screen.getByRole('button', { name: 'Login' }))
    expect(await screen.findByRole('heading', { name: 'Runs' })).toBeDefined()
  })

  it('does not treat a server error as session expiry', async () => {
    vi.mocked(api.listProjects).mockRejectedValueOnce(new ApiError(503, 'control plane unavailable'))
    render(<App />)
    expect(await screen.findByText(/control plane unavailable/)).toBeDefined()
    expect(screen.getByRole('button', { name: 'Retry' })).toBeDefined()
    expect(screen.queryByText(/Your session ended/)).toBeNull()
    expect(screen.getByRole('button', { name: 'Logout' })).toBeDefined()
  })

  it('changes appearance without a project or dirtying server settings', async () => {
    render(<App />)
    await screen.findByText(/No registered projects/)
    fireEvent.click(screen.getByRole('button', { name: 'Settings' }))
    fireEvent.click(await screen.findByRole('tab', { name: 'General' }))
    fireEvent.change(await screen.findByLabelText('Color theme'), { target: { value: 'dark' } })
    expect(document.documentElement.dataset.theme).toBe('dark')
    expect(api.saveSettings).not.toHaveBeenCalled()
    expect(screen.queryByText('Choose a project first')).toBeNull()
  })

  it('keeps compact navigation and secondary Settings actions accessible in flow', async () => {
    const media = mockMatchMedia(true)
    try {
      render(<App />)
      await screen.findByText(/No registered projects/)

      const menuButton = screen.getByRole('button', { name: 'Menu', exact: true })
      fireEvent.click(menuButton)
      expect(menuButton.getAttribute('aria-expanded')).toBe('true')
      expect(screen.getByRole('menu', { name: 'Workspace navigation' })).toBeDefined()
      expect((screen.getByRole('menuitem', { name: 'Plans' }) as HTMLButtonElement).disabled).toBe(true)

      fireEvent.keyDown(document, { key: 'Escape' })
      expect(menuButton.getAttribute('aria-expanded')).toBe('false')
      expect(document.activeElement).toBe(menuButton)

      fireEvent.click(menuButton)
      fireEvent.click(screen.getByRole('menuitem', { name: 'Settings', exact: true }))
      await screen.findByRole('heading', { name: 'Settings' })
      const section = await screen.findByRole('combobox', { name: 'Settings section' })
      const sectionLabels = within(section).getAllByRole('option').map((option) => option.textContent?.trim())
      expect(sectionLabels).toEqual(expect.arrayContaining(['General', 'Agents & Roles', 'Skills']))
      expect(screen.queryAllByRole('tab')).toHaveLength(0)
      expect(screen.queryByRole('button', { name: 'Reload server settings' })).toBeNull()

      fireEvent.click(screen.getByRole('button', { name: 'More', exact: true }))
      expect(screen.getByRole('menu', { name: 'More settings actions' })).toBeDefined()
      fireEvent.click(screen.getByRole('menuitem', { name: 'Install skills', exact: true }))
      expect(await screen.findByRole('heading', { name: 'Install skills' })).toBeDefined()
      expect(screen.getByRole('button', { name: 'Hide', exact: true })).toBeDefined()
    } finally {
      media.restore()
    }
  })

  it('routes hosted Install skills through guided mode and retains raw drafts', async () => {
    render(<App />)
    await screen.findByText(/No registered projects/)
    fireEvent.click(screen.getByRole('button', { name: 'Settings', exact: true }))
    await openAdvancedSettings()
    const raw = screen.getByLabelText('aflow.toml contents') as HTMLTextAreaElement
    fireEvent.change(raw, { target: { value: '# retained raw draft\n' } })

    await clickHeaderMenuItem('Install skills')
    await screen.findByRole('heading', { name: 'Install skills' })
    expect(screen.queryByLabelText('aflow.toml contents')).toBeNull()
    expect(api.postGlobalConfigForm).toHaveBeenCalledWith(expect.objectContaining({ aflow_toml: '# retained raw draft\n' }))

    fireEvent.click(screen.getByRole('button', { name: 'Hide', exact: true }))
    expect(screen.queryByRole('heading', { name: 'Install skills' })).toBeNull()
    await clickHeaderMenuItem('Advanced TOML')
    expect((await screen.findByLabelText('aflow.toml contents') as HTMLTextAreaElement).value).toBe('# retained raw draft\n')
  })

  it('keeps invalid raw text and actionable errors when Install skills cannot switch modes', async () => {
    render(<App />)
    await screen.findByText(/No registered projects/)
    fireEvent.click(screen.getByRole('button', { name: 'Settings', exact: true }))
    await openAdvancedSettings()
    const raw = screen.getByLabelText('aflow.toml contents') as HTMLTextAreaElement
    fireEvent.change(raw, { target: { value: 'invalid = [' } })
    vi.mocked(api.postGlobalConfigForm).mockResolvedValueOnce({ ...guidedFormResponse(), form: null })

    await clickHeaderMenuItem('Install skills')
    await screen.findByText(/Correct the TOML syntax before switching to guided settings\./)
    expect((screen.getByLabelText('aflow.toml contents') as HTMLTextAreaElement).value).toBe('invalid = [')
    expect(screen.queryByRole('heading', { name: 'Install skills' })).toBeNull()
  })

  it('guides an empty registry into the create form and shows the server context', async () => {
    render(<App />)
    await waitFor(() => expect(api.listProjects).toHaveBeenCalled())
    await screen.findByText(/No registered projects/)
    expect(await screen.findByPlaceholderText('team/project')).toBeDefined()
    // Server context names the managed root without listing filesystem choices.
    expect(screen.getByText('/srv/code').className).toContain('mono')
    expect(screen.queryByText(/\/srv\/code\//)).toBeNull()
    expect(screen.getByRole('button', { name: 'Settings' }).getAttribute('disabled')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Plans' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Runs' })).toBeNull()
  })

  it('registers a project with initialize-Git confirmation and no local config', async () => {
    vi.mocked(api.createProject).mockResolvedValue({
      id: 'beta', display_name: 'Beta', relative_root: 'beta',
      root: '/srv/code/beta', created_at: '2026-01-01T00:00:00Z', readiness: 'ready',
    })
    render(<App />)
    fireEvent.click(await screen.findByRole('radio', { name: /Register an existing directory/ }))
    fireEvent.change(screen.getByLabelText('Relative project path'), { target: { value: 'beta' } })
    fireEvent.change(screen.getByLabelText('Display name'), { target: { value: 'Beta' } })
    // Project-level starter fields no longer exist: configuration is global.
    expect(screen.queryByLabelText('Initial workflow')).toBeNull()
    fireEvent.click(screen.getByRole('checkbox', { name: /Initialize a Git repository/ }))
    vi.mocked(api.listProjects).mockResolvedValue([configProject])
    fireEvent.click(screen.getByRole('button', { name: 'Register project' }))

    await waitFor(() => expect(api.createProject).toHaveBeenCalledWith({
      mode: 'register',
      path: 'beta',
      display_name: 'Beta',
      main_branch: 'main',
      initialize_git: true,
    }))
    // Readiness follows the shared global pair; a ready pair lands on Plans.
    await screen.findByText(/AFlow · Beta Project/)
  })

  it('never renders project-level starter fields in register mode', async () => {
    render(<App />)
    fireEvent.click(await screen.findByRole('radio', { name: /Register an existing directory/ }))
    fireEvent.change(screen.getByLabelText('Relative project path'), { target: { value: 'beta' } })
    expect(screen.queryByLabelText('Initial workflow')).toBeNull()
    expect(screen.queryByLabelText('Initial team')).toBeNull()
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
    }))
    await openAdvancedSettings()
    expect(screen.getByText(/The shared AFlow configuration needs explicit settings/)).toBeDefined()
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

    await openAdvancedSettings()
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
    await openAddedProject(/Alpha Project/)
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
    vi.mocked(api.patchGlobalConfig).mockResolvedValue(configPayload('ready'))
    // One server validates one pair one way: the projection of the committed
    // texts agrees with the ready save result.
    vi.mocked(api.postGlobalConfigForm).mockResolvedValue(guidedFormResponse(configPayload('ready').validation))
    render(<App />)
    await openAddedProject(/Beta Project/)
    fireEvent.click(screen.getByRole('button', { name: 'Settings' }))
    await openAdvancedSettings()
    fireEvent.change(screen.getByLabelText('aflow.toml contents'), { target: { value: '# ready\n' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save all changes' }))
    await waitFor(() => expect(screen.queryByText('Configuration required')).toBeNull())
    expect(screen.queryByText(/needs explicit configuration/)).toBeNull()
    expect(screen.getByLabelText('aflow.toml contents')).toBeDefined()
    fireEvent.click(await screen.findByRole('button', { name: 'Plans', exact: true }))

    await screen.findByLabelText('New plan filename')
    expect(screen.queryByText('Configuration required')).toBeNull()
    expect(screen.queryByText(/needs explicit configuration/)).toBeNull()
  })

  it('keeps a dirty guided draft behind the navigation guard until a ready save', async () => {
    vi.mocked(api.listProjects).mockResolvedValue([configProject])
    render(<App />)
    await openAddedProject(/Beta Project/)
    fireEvent.click(screen.getByRole('button', { name: 'Settings' }))
    await openAdvancedSettings()
    fireEvent.change(screen.getByLabelText('aflow.toml contents'), { target: { value: '# guided draft\n' } })
    await screen.findByText(/Unsaved changes/)
    fireEvent.click(screen.getByRole('button', { name: 'Plans' }))
    expect(screen.getByRole('alertdialog', { name: 'Unsaved editor edits' })).toBeDefined()
    fireEvent.click(screen.getByRole('button', { name: 'Stay' }))
    expect((screen.getByLabelText('aflow.toml contents') as HTMLTextAreaElement).value).toBe('# guided draft\n')

    // A ready save clears the guard and navigation proceeds without a dialog.
    vi.mocked(api.patchGlobalConfig).mockResolvedValue(configPayload('ready'))
    vi.mocked(api.postGlobalConfigForm).mockResolvedValue(guidedFormResponse(configPayload('ready').validation))
    fireEvent.click(screen.getByRole('button', { name: 'Save all changes' }))
    await waitFor(() => expect(api.patchGlobalConfig).toHaveBeenCalled())
    fireEvent.click(screen.getByRole('button', { name: 'Plans' }))
    await screen.findByLabelText('New plan filename')
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
    expect(screen.getByRole('button', { name: 'Projects' }).getAttribute('disabled')).toBeNull()
    expect(screen.getByRole('button', { name: 'Projects' }).getAttribute('aria-current')).toBe('page')
    expect(screen.queryByRole('button', { name: 'Runs' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Settings' }).getAttribute('disabled')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Plans' })).toBeNull()
    expect(await screen.findByPlaceholderText('team/project')).toBeDefined()
    expect(screen.getByRole('button', { name: 'More', exact: true })).toBeDefined()
  })

  it('returns a blocked project to Projects for re-checking instead of emphasizing Settings', async () => {
    vi.mocked(api.listProjects).mockResolvedValue([blockedProject])
    render(<App />)
    await openAddedProject(/Gamma Project/)
    expect(await screen.findByRole('heading', { name: 'Runs' })).toBeDefined()
    expect(screen.getByText(/not currently a usable Git project root/)).toBeDefined()

    fireEvent.click(screen.getByRole('button', { name: 'Projects' }))

    fireEvent.click(screen.getByRole('button', { name: 'More', exact: true }))
    const refresh = await screen.findByRole('menuitem', { name: 'Refresh', exact: true })
    expect(screen.getByRole('button', { name: 'Projects' }).getAttribute('aria-current')).toBe('page')
    expect(screen.queryByLabelText('aflow.toml contents')).toBeNull()
    fireEvent.click(refresh)
    await waitFor(() => expect(api.listProjects).toHaveBeenCalledTimes(2))
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
    fireEvent.click(await screen.findByRole('button', { name: 'More actions for project Alpha Project' }))
    fireEvent.click(screen.getByRole('menuitem', { name: 'Unregister…' }))
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
    await screen.findByRole('list', { name: 'Available on this server' })
    expect(within(screen.getByRole('list', { name: 'Available on this server' })).queryByText('alpha')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'More actions for project Alpha Project' }))
    fireEvent.click(screen.getByRole('menuitem', { name: 'Unregister…' }))
    fireEvent.click(screen.getByRole('button', { name: 'Unregister (keeps files)' }))
    await waitFor(() => expect(api.unregisterProject).toHaveBeenCalledWith('alpha'))
    // Both the registry list and discovery refresh; no second manual refresh.
    await waitFor(() => expect(api.listProjects).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(api.getProjectDiscovery).toHaveBeenCalledTimes(2))
    expect(screen.queryByText('Added')).toBeNull()
    await waitFor(() => expect(within(screen.getByRole('list', { name: 'Available on this server' })).getByRole('button', { name: 'Add', exact: true })).toBeDefined())
  })

  it('guards navigation away from unsaved configuration edits', async () => {
    vi.mocked(api.listProjects).mockResolvedValue([readyProject])
    render(<App />)
    await openAddedProject(/Alpha Project/)
    fireEvent.click(screen.getByRole('button', { name: 'Settings' }))
    await openAdvancedSettings()
    fireEvent.change(screen.getByLabelText('aflow.toml contents'), { target: { value: '# kept\n' } })

    fireEvent.click(screen.getByRole('button', { name: 'Plans' }))
    expect(screen.getByRole('alertdialog', { name: 'Unsaved editor edits' })).toBeDefined()
    fireEvent.click(screen.getByRole('button', { name: 'Leave anyway' }))
    await screen.findByLabelText('New plan filename')
    // Returning to Settings starts from the saved documents again.
    fireEvent.click(screen.getByRole('button', { name: 'Settings' }))
    await openAdvancedSettings()
  })

  it('hands an in-progress plan to a New run that opens with the exact plan selected', async () => {
    const readyPlan = {
      project_id: 'alpha', name: 'demo.md', path: 'plans/in-progress/demo.md',
      status: 'in_progress' as const, revision: 'b'.repeat(64), size_bytes: 9, content: '# Demo\n',
    }
    vi.mocked(api.listProjects).mockResolvedValue([readyProject])
    vi.mocked(api.listProjectPlans).mockResolvedValue([readyPlan])
    vi.mocked(api.readProjectPlan).mockResolvedValue(readyPlan)
    // The dashboard's handoff resolves the plan against control-plane plans.
    vi.mocked(api.listControlPlanePlans).mockResolvedValue([{ path: 'plans/in-progress/demo.md', status: 'in_progress', modified_at: '2026-01-01T00:00:00Z', schema_version: 1 }])
    const push = vi.spyOn(window.history, 'pushState')
    render(<App />)
    await openAddedProject(/Alpha Project/)
    fireEvent.click(screen.getByRole('button', { name: 'Plans' }))
    fireEvent.click(await screen.findByRole('button', { name: /demo\.md/ }))
    await clickHeaderMenuItem('Run this plan')

    await screen.findByRole('heading', { name: 'New run' })
    expect(push).toHaveBeenCalledWith(null, '', '/?project=alpha&view=new-run')
    await waitFor(() => expect((screen.getByLabelText('Run plan') as HTMLInputElement).value).toBe('plans/in-progress/demo.md'))
  })

  it('enters the same project workspace when Open is clicked twice with Projects between clicks', async () => {
    vi.mocked(api.listProjects).mockResolvedValue([readyProject])
    render(<App />)
    await openAddedProject(/Alpha Project/)
    expect(await screen.findByRole('heading', { name: 'Runs' })).toBeDefined()
    expect(screen.getByRole('button', { name: 'Runs' }).getAttribute('aria-current')).toBe('page')
    expect(screen.getByRole('heading', { name: /AFlow · Alpha Project/ })).toBeDefined()

    fireEvent.click(screen.getByRole('button', { name: 'Projects' }))
    await screen.findByText(/Registered beneath the server's managed root/)
    await openAddedProject(/Alpha Project/)
    expect(await screen.findByRole('heading', { name: 'Runs' })).toBeDefined()
    expect(screen.getByRole('button', { name: 'Runs' }).getAttribute('aria-current')).toBe('page')
  })

  it('guards reopening the already-selected project from Open with unsaved edits', async () => {
    vi.mocked(api.listProjects).mockResolvedValue([readyProject])
    render(<App />)
    await openAddedProject(/Alpha Project/)
    fireEvent.click(screen.getByRole('button', { name: 'Settings' }))
    await openAdvancedSettings()
    fireEvent.change(screen.getByLabelText('aflow.toml contents'), { target: { value: '# edited\n' } })
    await screen.findByText(/Unsaved changes/)

    fireEvent.click(screen.getByRole('button', { name: 'Projects' }))
    expect(screen.getByRole('alertdialog', { name: 'Unsaved editor edits' })).toBeDefined()
    fireEvent.click(screen.getByRole('button', { name: 'Stay' }))
    expect(screen.getByLabelText('aflow.toml contents')).toBeDefined()

    fireEvent.click(screen.getByRole('button', { name: 'Projects' }))
    fireEvent.click(screen.getByRole('button', { name: 'Leave anyway' }))
    await openAddedProject(/Alpha Project/)
    expect(await screen.findByRole('heading', { name: 'Runs' })).toBeDefined()
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
    expect(within(screen.getByRole('list', { name: 'Available on this server' })).queryByText('alpha')).toBeNull()

    fireEvent.change(screen.getByLabelText('Search projects and available candidates'), { target: { value: 'kilo' } })
    expect(screen.queryByText('limbo')).toBeNull()
    expect(screen.getByText('tools/kilo')).toBeDefined()

    fireEvent.click(within(kiloItem).getByRole('button', { name: 'Add', exact: true }))
    await waitFor(() => expect(api.createProject).toHaveBeenCalledWith({
      mode: 'register',
      path: 'tools/kilo',
      display_name: 'Kilo',
      main_branch: 'main',
      initialize_git: false,
    }))
  })

  it('preserves search and refreshes both lists when Add loses a race', async () => {
    vi.mocked(api.listProjects).mockResolvedValue([readyProject])
    vi.mocked(api.getProjectDiscovery)
      .mockResolvedValueOnce({
        ...discoveryBase,
        candidates: [
          { relative_path: 'tools/kilo', display_name: 'Kilo', registered_project_id: null, addable: true, add_blocker: null },
        ],
      })
      .mockResolvedValue({
        ...discoveryBase,
        candidates: [
          { relative_path: 'tools/kilo', display_name: 'Kilo', registered_project_id: 'kilo', addable: false, add_blocker: 'already registered' },
        ],
      })
    vi.mocked(api.createProject).mockRejectedValueOnce(new ApiError(409, 'already registered'))
    render(<App />)
    const kiloItem = (await screen.findByText('tools/kilo')).closest('[role="listitem"]') as HTMLElement
    fireEvent.change(screen.getByLabelText('Search projects and available candidates'), { target: { value: 'kilo' } })
    fireEvent.click(within(kiloItem).getByRole('button', { name: 'Add', exact: true }))

    await screen.findByText(/already registered/)
    // The typed search survives, and both lists refresh so the raced
    // registration becomes openable without a manual refresh.
    expect((screen.getByLabelText('Search projects and available candidates') as HTMLInputElement).value).toBe('kilo')
    await waitFor(() => expect(api.listProjects).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(api.getProjectDiscovery).toHaveBeenCalledTimes(2))
    expect(within(screen.getByRole('list', { name: 'Available on this server' })).queryByText('tools/kilo')).toBeNull()
  })

  it('recovers discovery failures and explains an empty server', async () => {
    vi.mocked(api.listProjects).mockResolvedValue([readyProject])
    vi.mocked(api.getProjectDiscovery).mockRejectedValueOnce(new Error('scan failed'))
    render(<App />)
    expect(await screen.findByText(/Existing projects could not be listed: scan failed/)).toBeDefined()
    vi.mocked(api.getProjectDiscovery).mockResolvedValue(discoveryBase)
    fireEvent.click(screen.getByRole('button', { name: 'Retry discovery' }))
    await screen.findByText(/No discoverable Git projects beneath the managed root yet/)
  })

  it('offers discovered candidates as register-form suggestions that fill the path', async () => {
    vi.mocked(api.getProjectDiscovery).mockResolvedValue({
      ...discoveryBase,
      candidates: [
        { relative_path: 'tools/kilo', display_name: 'Kilo', registered_project_id: null, addable: true, add_blocker: null },
      ],
    })
    render(<App />)
    await screen.findByText('tools/kilo')
    // With no registered projects the create form is already open; switch it
    // to register mode where discovered candidates act as suggestions.
    fireEvent.click(await screen.findByRole('radio', { name: /Register an existing directory/ }))
    const suggestionList = await screen.findByRole('list', { name: 'Discovered project candidates' })
    fireEvent.click(within(suggestionList).getByRole('button', { name: /Kilo/ }))
    expect((screen.getByLabelText('Relative project path') as HTMLInputElement).value).toBe('tools/kilo')
  })

  it('reopens exactly the linked project run after a reload', async () => {
    setUrl('/?project=alpha&view=runs&run=run-9')
    vi.mocked(api.listProjects).mockResolvedValue([readyProject])
    mockRunDashboard()
    render(<App />)
    await screen.findByRole('heading', { name: 'Runs' })
    await waitFor(() => expect(api.getControlPlaneRun).toHaveBeenCalledWith('alpha', 'run-9', expect.objectContaining({ signal: expect.any(AbortSignal) })))
  })

  it('removes a fragment from an otherwise canonical run link without pushing history', async () => {
    setUrl('/?project=alpha&view=runs&run=run-9#fragment-sentinel')
    const push = vi.spyOn(window.history, 'pushState')
    const replace = vi.spyOn(window.history, 'replaceState')
    vi.mocked(api.listProjects).mockResolvedValue([readyProject])
    mockRunDashboard()
    render(<App />)
    await screen.findByRole('heading', { name: 'Runs' })
    // The canonical rewrite drops the fragment and replaces, never pushes.
    await waitFor(() => expect(replace).toHaveBeenCalledWith(null, '', '/?project=alpha&view=runs&run=run-9'))
    expect(push).not.toHaveBeenCalled()
  })

  it('reflects the passively selected newest run in the Runs URL', async () => {
    setUrl('/?project=alpha&view=runs')
    const replace = vi.spyOn(window.history, 'replaceState')
    vi.mocked(api.listProjects).mockResolvedValue([readyProject])
    mockRunDashboard({ runs: [{ ...dashboardRun, run_id: 'run-older' }] })
    render(<App />)
    await screen.findByRole('heading', { name: 'Runs' })
    await waitFor(() => expect(replace).toHaveBeenCalledWith(null, '', '/?project=alpha&view=runs&run=run-older'))
  })

  it('keeps an ordinary compact Runs entry on history after passive URL sync', async () => {
    const media = mockMatchMedia(true)
    try {
      setUrl('/?project=alpha&view=runs')
      const replace = vi.spyOn(window.history, 'replaceState')
      vi.mocked(api.listProjects).mockResolvedValue([readyProject])
      mockRunDashboard()
      render(<App />)
      await screen.findByRole('heading', { name: 'Runs' })
      await waitFor(() => expect(replace).toHaveBeenCalledWith(null, '', '/?project=alpha&view=runs&run=run-9'))

      const navigation = document.querySelector('.sidebar-editor-navigation') as HTMLElement
      const detail = document.querySelector('.sidebar-editor-detail') as HTMLElement
      expect(navigation.hidden).toBe(false)
      expect(detail.hidden).toBe(true)
    } finally {
      media.restore()
    }
  })

  it('keeps a wide default Runs entry on history when resized compact', async () => {
    const media = mockMatchMedia(false)
    try {
      setUrl('/?project=alpha&view=runs')
      const replace = vi.spyOn(window.history, 'replaceState')
      vi.mocked(api.listProjects).mockResolvedValue([readyProject])
      mockRunDashboard()
      render(<App />)
      await screen.findByRole('heading', { name: 'Runs' })
      await waitFor(() => expect(replace).toHaveBeenCalledWith(null, '', '/?project=alpha&view=runs&run=run-9'))

      media.setMatches(true)
      await waitFor(() => expect((document.querySelector('.sidebar-editor-detail') as HTMLElement).hidden).toBe(true))
      expect((document.querySelector('.sidebar-editor-navigation') as HTMLElement).hidden).toBe(false)
    } finally {
      media.restore()
    }
  })

  it('opens explicit initial and later browser run URLs on compact screens', async () => {
    const media = mockMatchMedia(true)
    try {
      setUrl('/?project=alpha&view=runs&run=run-9')
      const other = { ...dashboardRun, run_id: 'run-other', plan_path: 'plans/in-progress/other.md' }
      vi.mocked(api.listProjects).mockResolvedValue([readyProject])
      mockRunDashboard({ runs: [dashboardRun, other] })
      vi.mocked(api.getControlPlaneRun).mockImplementation(async (_projectId, runId) => runId === 'run-other' ? other : dashboardRun)
      render(<App />)
      await screen.findByRole('heading', { name: 'Runs' })
      await screen.findByRole('heading', { name: 'demo.md' })
      expect((document.querySelector('.sidebar-editor-navigation') as HTMLElement).hidden).toBe(true)

      setUrl('/?project=alpha&view=runs&run=run-other')
      window.dispatchEvent(new PopStateEvent('popstate'))
      await waitFor(() => expect(api.getControlPlaneRun).toHaveBeenCalledWith('alpha', 'run-other', expect.objectContaining({ signal: expect.any(AbortSignal) })))
      await screen.findByRole('heading', { name: 'other.md' })
      expect((document.querySelector('.sidebar-editor-navigation') as HTMLElement).hidden).toBe(true)

      setUrl('/?project=alpha&view=runs&run=run-9')
      window.dispatchEvent(new PopStateEvent('popstate'))
      await waitFor(() => expect(api.getControlPlaneRun).toHaveBeenLastCalledWith('alpha', 'run-9', expect.objectContaining({ signal: expect.any(AbortSignal) })))
      await screen.findByRole('heading', { name: 'demo.md' })
      expect((document.querySelector('.sidebar-editor-navigation') as HTMLElement).hidden).toBe(true)
    } finally {
      media.restore()
    }
  })

  it('opens fresh and repeated compact All runs selections in the exact detail', async () => {
    const media = mockMatchMedia(true)
    try {
      setUrl('/?view=all-runs')
      const push = vi.spyOn(window.history, 'pushState')
      vi.mocked(api.listProjects).mockResolvedValue([readyProject])
      mockRunDashboard()
      render(<App />)

      const openFromAllRuns = async () => {
        const row = await screen.findByRole('button', { name: /Alpha Project.*run-9/ })
        fireEvent.click(row)
        await waitFor(() => expect(push).toHaveBeenLastCalledWith(null, '', '/?project=alpha&view=runs&run=run-9'))
        await screen.findByRole('heading', { name: 'demo.md' })
        expect((document.querySelector('.sidebar-editor-navigation') as HTMLElement).hidden).toBe(true)
        expect((document.querySelector('.sidebar-editor-detail') as HTMLElement).hidden).toBe(false)
      }

      await openFromAllRuns()
      fireEvent.click(screen.getByRole('button', { name: '← Back to Run history', exact: true }))
      await waitFor(() => expect((document.querySelector('.sidebar-editor-navigation') as HTMLElement).hidden).toBe(false))
      expect((document.querySelector('.sidebar-editor-detail') as HTMLElement).hidden).toBe(true)

      fireEvent.click(screen.getByRole('button', { name: 'Menu', exact: true }))
      fireEvent.click(screen.getByRole('menuitem', { name: 'All runs', exact: true }))
      await screen.findByRole('heading', { name: 'All runs' })
      await openFromAllRuns()
    } finally {
      media.restore()
    }
  })

  it('clears a stale project link with guidance instead of a substitute', async () => {
    setUrl('/?project=ghost&view=runs')
    const replace = vi.spyOn(window.history, 'replaceState')
    vi.mocked(api.listProjects).mockResolvedValue([readyProject])
    render(<App />)
    const notice = await screen.findByText(/not in the registered project list/)
    expect(notice.textContent).toContain('ghost')
    expect(screen.getByRole('button', { name: 'Projects' }).getAttribute('aria-current')).toBe('page')
    await waitFor(() => expect(replace).toHaveBeenCalledWith(null, '', '/?view=projects'))
    // No substitute project was opened silently.
    expect(screen.queryByRole('heading', { name: 'Runs' })).toBeNull()
  })

  it('normalizes an unknown view to Runs for a valid project and rewrites the URL', async () => {
    setUrl('/?project=alpha&view=bogus-view')
    const replace = vi.spyOn(window.history, 'replaceState')
    vi.mocked(api.listProjects).mockResolvedValue([readyProject])
    mockRunDashboard()
    render(<App />)
    await screen.findByRole('heading', { name: 'Runs' })
    await waitFor(() => expect(replace).toHaveBeenCalledWith(null, '', '/?project=alpha&view=runs'))
  })

  it('opens a view-less project link as Runs and normalizes the URL', async () => {
    setUrl('/?project=alpha')
    const replace = vi.spyOn(window.history, 'replaceState')
    vi.mocked(api.listProjects).mockResolvedValue([readyProject])
    mockRunDashboard()
    render(<App />)
    await screen.findByRole('heading', { name: 'Runs' })
    await waitFor(() => expect(replace).toHaveBeenCalledWith(null, '', '/?project=alpha&view=runs'))
  })

  it('keeps the Runs view and drops a missing run id without selecting a substitute', async () => {
    setUrl('/?project=alpha&view=runs&run=run-missing')
    const replace = vi.spyOn(window.history, 'replaceState')
    vi.mocked(api.listProjects).mockResolvedValue([readyProject])
    mockRunDashboard()
    vi.mocked(api.getControlPlaneRun).mockRejectedValue(new ApiError(404, 'not found'))
    render(<App />)
    await screen.findByRole('heading', { name: 'Runs' })
    expect(await screen.findByText(/is not recorded for this project/)).toBeDefined()
    await waitFor(() => expect(replace).toHaveBeenCalledWith(null, '', '/?project=alpha&view=runs'))
    // No other run was selected in place of the missing link target.
    expect(screen.queryByRole('heading', { name: 'demo.md' })).toBeNull()
  })

  it('pushes run selection into the URL and restores the prior run on browser back', async () => {
    setUrl('/?project=alpha&view=runs')
    const push = vi.spyOn(window.history, 'pushState')
    const other = { ...dashboardRun, run_id: 'run-other', plan_path: 'plans/in-progress/other.md' }
    vi.mocked(api.listProjects).mockResolvedValue([readyProject])
    mockRunDashboard({ runs: [dashboardRun, other] })
    render(<App />)
    await screen.findByRole('heading', { name: 'Runs' })
    // An explicit pick pushes; the passive newest selection never did.
    fireEvent.click(screen.getByRole('button', { name: /other\.md/ }))
    await waitFor(() => expect(push).toHaveBeenCalledWith(null, '', '/?project=alpha&view=runs&run=run-other'))

    // Simulate browser back to the prior entry.
    window.location.href = window.location.origin + '/?project=alpha&view=runs&run=run-9'
    window.dispatchEvent(new PopStateEvent('popstate'))
    await waitFor(() => expect(api.getControlPlaneRun).toHaveBeenLastCalledWith('alpha', 'run-9', expect.objectContaining({ signal: expect.any(AbortSignal) })))
  })

  it('guards browser back with unsaved edits and restores the URL on cancel', async () => {
    setUrl('/?project=alpha&view=runs')
    const push = vi.spyOn(window.history, 'pushState')
    vi.mocked(api.listProjects).mockResolvedValue([readyProject])
    mockRunDashboard()
    render(<App />)
    await screen.findByRole('heading', { name: 'Runs' })
    fireEvent.click(screen.getByRole('button', { name: 'Settings' }))
    await openAdvancedSettings()
    fireEvent.change(screen.getByLabelText('aflow.toml contents'), { target: { value: '# dirty\n' } })

    window.location.href = window.location.origin + '/?project=alpha&view=runs'
    window.dispatchEvent(new PopStateEvent('popstate'))
    expect(await screen.findByRole('alertdialog', { name: 'Unsaved editor edits' })).toBeDefined()
    fireEvent.click(screen.getByRole('button', { name: 'Stay' }))
    // Cancel restores the URL the visible state came from.
    await waitFor(() => expect(push).toHaveBeenCalledWith(null, '', '/?project=alpha&view=settings'))
    expect(screen.getByLabelText('aflow.toml contents')).toBeDefined()
  })

  it('completes discovery, guided save recovery, Ready-plan launch and exact-link reload', async () => {
    vi.mocked(api.listProjects).mockResolvedValueOnce([]).mockResolvedValue([readyProject])
    vi.mocked(api.getProjectDiscovery).mockResolvedValue({
      ...discoveryBase,
      candidates: [{ relative_path: 'alpha', display_name: 'Alpha Project', registered_project_id: null, addable: true, add_blocker: null }],
    })
    vi.mocked(api.createProject).mockResolvedValue({
      id: 'alpha', display_name: 'Alpha Project', relative_root: 'alpha',
      root: '/srv/code/alpha', created_at: '2026-01-01T00:00:00Z', readiness: 'configuration_required',
    })
    const readyPlan = {
      project_id: 'alpha', name: 'demo.md', path: 'plans/in-progress/demo.md',
      status: 'in_progress' as const, revision: 'b'.repeat(64), size_bytes: 9, content: '# Demo\n',
    }
    vi.mocked(api.listProjectPlans).mockResolvedValue([readyPlan])
    vi.mocked(api.readProjectPlan).mockResolvedValue(readyPlan)
    // The New run page resolves the handed-off plan from control-plane plans.
    vi.mocked(api.listControlPlanePlans).mockResolvedValue([{ path: 'plans/in-progress/demo.md', status: 'in_progress', modified_at: '2026-01-01T00:00:00Z', schema_version: 1 }])
    // First save fails validation, the corrected retry succeeds.
    vi.mocked(api.validateGlobalConfig)
      .mockResolvedValueOnce({ ...configPayload('invalid').validation, state: 'invalid' })
      .mockResolvedValue(configPayload('ready').validation)
    vi.mocked(api.patchGlobalConfig).mockResolvedValue(configPayload('ready'))
    vi.mocked(api.postGlobalConfigForm).mockResolvedValue(guidedFormResponse(configPayload('ready').validation))
    render(<App />)

    // Discover and add the project.
    const item = (await screen.findByText('alpha')).closest('[role="listitem"]') as HTMLElement
    fireEvent.click(within(item).getByRole('button', { name: 'Add', exact: true }))
    await waitFor(() => expect(api.createProject).toHaveBeenCalled())
    await screen.findByRole('heading', { name: /AFlow · Alpha Project/ })

    // Guided save: an invalid draft keeps the draft; the corrected retry saves.
    await openAdvancedSettings()
    fireEvent.change(screen.getByLabelText('aflow.toml contents'), { target: { value: '# still placeholder\n' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save all changes' }))
    await screen.findByText(/Replace placeholder selectors before saving/)
    expect((screen.getByLabelText('aflow.toml contents') as HTMLTextAreaElement).value).toBe('# still placeholder\n')
    fireEvent.change(screen.getByLabelText('aflow.toml contents'), { target: { value: '[roles]\nworker = "codex.worker"\n' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save all changes' }))
    await waitFor(() => expect(api.patchGlobalConfig).toHaveBeenCalled())

    // Ready-plan launch and an exact link reload.
    fireEvent.click(screen.getByRole('button', { name: 'Plans' }))
    fireEvent.click(await screen.findByRole('button', { name: /demo\.md/ }))
    await clickHeaderMenuItem('Run this plan')
    await screen.findByRole('heading', { name: 'New run' })
    await waitFor(() => expect((screen.getByLabelText('Run plan') as HTMLInputElement).value).toBe('plans/in-progress/demo.md'))
  })
})
