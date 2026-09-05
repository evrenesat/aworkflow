import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { App } from './App'
import * as api from './api'

vi.mock('./api', () => ({
  getAuthToken: vi.fn(), setAuthToken: vi.fn(), clearAuthToken: vi.fn(),
  listProjects: vi.fn(), updateProject: vi.fn(), listProjectPlans: vi.fn(),
  createProjectPlan: vi.fn(), readProjectPlan: vi.fn(), updateProjectPlan: vi.fn(), promoteProjectPlan: vi.fn(),
  listControlPlaneProjects: vi.fn(), getControlPlaneReadiness: vi.fn(), getControlPlaneCapabilities: vi.fn(), listControlPlanePlans: vi.fn(),
  listControlPlaneRuns: vi.fn(), getControlPlaneRun: vi.fn(), listRunEvents: vi.fn(), getRunContext: vi.fn(),
  startControlPlaneRun: vi.fn(), answerStartupQuestion: vi.fn(), controlControlPlaneRun: vi.fn(),
  ownerStopControlPlaneRun: vi.fn(), resumeControlPlaneRun: vi.fn(), subscribeToRunEvents: vi.fn(),
}))

const project = {
  id: 'project-1', display_name: 'Alpha Project', current_path: '/workspace/alpha',
  is_git_root: true, registered_at: '2024-01-01T00:00:00Z', readiness: 'ready' as const,
}
const plan = {
  project_id: 'project-1', name: 'demo.md', path: 'plans/in-progress/demo.md',
  status: 'in_progress' as const, revision: 'a'.repeat(64), size_bytes: 7, content: '# Demo\n',
}

describe('App', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(api.getAuthToken).mockReturnValue('test-token')
    vi.mocked(api.listProjects).mockResolvedValue([project])
    vi.mocked(api.listProjectPlans).mockResolvedValue([plan])
    vi.mocked(api.readProjectPlan).mockResolvedValue(plan)
    vi.mocked(api.listControlPlaneProjects).mockResolvedValue([{ project_id: 'control-project', root: '/workspace/alpha', schema_version: 1 }])
    vi.mocked(api.getControlPlaneReadiness).mockResolvedValue({ ready: true, projects: ['control-project'] })
    vi.mocked(api.getControlPlaneCapabilities).mockResolvedValue({ schema_version: 1, workflows: ['managed'], teams: [], roles: [], controls: [], context_levels: ['lite'], team_upgrade_chains: {}, control_safety: {}, service_features: [] })
    vi.mocked(api.listControlPlanePlans).mockResolvedValue([])
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [], next_cursor: null, schema_version: 1 })
    vi.mocked(api.subscribeToRunEvents).mockReturnValue(() => {})
  })

  it('shows login when unauthenticated', () => {
    vi.mocked(api.getAuthToken).mockReturnValue(null)
    render(<App />)
    expect(screen.getByPlaceholderText('Auth token')).toBeDefined()
  })

  it('shows registered projects and filesystem plans', async () => {
    render(<App />)
    await waitFor(() => expect(screen.getByText('Alpha Project')).toBeDefined())
    fireEvent.click(screen.getByText('Open'))
    await waitFor(() => expect(screen.getByText('demo.md')).toBeDefined())
    expect(screen.getByText('Registered projects, plans, configuration, and daemon-owned runs')).toBeDefined()
  })

  it('opens and updates a revisioned plan', async () => {
    vi.mocked(api.updateProjectPlan).mockResolvedValue({ ...plan, revision: 'b'.repeat(64), content: '# Changed' })
    render(<App />)
    await waitFor(() => expect(screen.getByText('Alpha Project')).toBeDefined())
    fireEvent.click(screen.getByText('Open'))
    const planButton = await screen.findByRole('button', { name: /demo\.md/ })
    fireEvent.click(planButton)
    await waitFor(() => expect(api.readProjectPlan).toHaveBeenCalledWith('project-1', 'in_progress', 'demo.md'))
    await screen.findByLabelText('Plan content')
    fireEvent.change(screen.getByLabelText('Plan content'), { target: { value: '# Changed' } })
    fireEvent.click(screen.getByText('Save'))
    await waitFor(() => expect(api.updateProjectPlan).toHaveBeenCalledWith('project-1', 'in_progress', 'demo.md', {
      content: '# Changed', expected_revision: plan.revision,
    }))
  })

  it('opens the run dashboard from an in-progress plan', async () => {
    render(<App />)
    await waitFor(() => expect(screen.getByText('Alpha Project')).toBeDefined())
    fireEvent.click(screen.getByText('Open'))
    const planButton = await screen.findByRole('button', { name: /demo\.md/ })
    fireEvent.click(planButton)
    const openRun = await screen.findByText('Open run dashboard')
    fireEvent.click(openRun)
    await screen.findByText('Run dashboard')
  })
})
