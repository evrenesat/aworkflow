import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { App } from './App'
import * as api from './api'

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
  listProjects: vi.fn(), getProject: vi.fn(), createProject: vi.fn(), unregisterProject: vi.fn(),
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
    vi.mocked(api.getAuthToken).mockReturnValue('test-token')
    vi.mocked(api.listProjects).mockResolvedValue([])
    vi.mocked(api.getProjectConfig).mockResolvedValue(configPayload())
    vi.mocked(api.listProjectPlans).mockResolvedValue([])
    vi.mocked(api.listControlPlaneProjects).mockResolvedValue([])
    vi.mocked(api.getControlPlaneReadiness).mockResolvedValue({ ready: true, projects: [] })
    vi.mocked(api.getControlPlaneCapabilities).mockResolvedValue({
      schema_version: 1, workflows: [], teams: [], roles: [], controls: [],
      context_levels: ['lite'], team_upgrade_chains: {}, control_safety: {}, service_features: [],
    })
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [], next_cursor: null, schema_version: 1 })
    vi.mocked(api.subscribeToRunEvents).mockReturnValue(() => {})
  })

  it('shows login when unauthenticated', () => {
    vi.mocked(api.getAuthToken).mockReturnValue(null)
    render(<App />)
    expect(screen.getByPlaceholderText('Auth token')).toBeDefined()
  })

  it('guides an empty registry into the create form without filesystem choices', async () => {
    render(<App />)
    await waitFor(() => expect(api.listProjects).toHaveBeenCalled())
    await screen.findByText(/No registered projects/)
    expect(await screen.findByPlaceholderText('team/project')).toBeDefined()
    expect(screen.queryByText('/srv/code')).toBeNull()
    expect(screen.getByRole('button', { name: 'Configuration' }).getAttribute('disabled')).not.toBeNull()
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
    fireEvent.change(screen.getByLabelText('Initial workflow'), { target: { value: 'starter-flow' } })
    fireEvent.change(screen.getByLabelText('Initial team'), { target: { value: 'core' } })
    fireEvent.click(screen.getByRole('checkbox', { name: /Initialize a Git repository/ }))
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
      initialize_config: false,
    }))
    await screen.findByLabelText('aflow.toml contents')
    expect(screen.getByText(/needs explicit configuration/)).toBeDefined()
    expect(screen.getByText('Configuration required')).toBeDefined()
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

  it('guards navigation away from unsaved configuration edits', async () => {
    vi.mocked(api.listProjects).mockResolvedValue([readyProject])
    render(<App />)
    fireEvent.click(await screen.findByRole('button', { name: /Alpha Project/ }))
    fireEvent.click(await screen.findByRole('button', { name: 'Configuration' }))
    await screen.findByLabelText('aflow.toml contents')
    fireEvent.change(screen.getByLabelText('aflow.toml contents'), { target: { value: '# edited\n' } })
    await screen.findByText(/Unsaved edits/)

    fireEvent.click(screen.getByRole('button', { name: 'Plans' }))
    expect(screen.getByText(/You have unsaved configuration edits/)).toBeDefined()
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
})
