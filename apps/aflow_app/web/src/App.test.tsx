import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { App } from './App'
import * as api from './api'

vi.mock('./api', () => ({
  getAuthToken: vi.fn(), setAuthToken: vi.fn(), clearAuthToken: vi.fn(),
  listProjects: vi.fn(), updateProject: vi.fn(),
  listProjectSessions: vi.fn(), getProjectSession: vi.fn(), startProjectSession: vi.fn(),
  resumeProjectSession: vi.fn(), forkProjectSession: vi.fn(), setProjectSessionArchived: vi.fn(),
  startProjectTurn: vi.fn(), interruptProjectTurn: vi.fn(),
  listPendingApprovals: vi.fn(), respondToApproval: vi.fn(),
  listAttachments: vi.fn(), uploadAttachment: vi.fn(), deleteAttachment: vi.fn(),
  listProjectPlans: vi.fn(), listPlanDrafts: vi.fn(), loadPlanDraft: vi.fn(), savePlanDraft: vi.fn(),
  promotePlanDraft: vi.fn(), deletePlanDraft: vi.fn(),
  listControlPlaneProjects: vi.fn(), getControlPlaneReadiness: vi.fn(), getControlPlaneCapabilities: vi.fn(), listControlPlanePlans: vi.fn(),
  listControlPlaneRuns: vi.fn(), getControlPlaneRun: vi.fn(), listRunEvents: vi.fn(), getRunContext: vi.fn(),
  startControlPlaneRun: vi.fn(), answerStartupQuestion: vi.fn(), controlControlPlaneRun: vi.fn(),
  ownerStopControlPlaneRun: vi.fn(), resumeControlPlaneRun: vi.fn(), subscribeToRunEvents: vi.fn(),
}))

const project = {
  id: 'project-1', display_name: 'Alpha Project', current_path: '/workspace/alpha',
  historical_aliases: [], detection_source: 'local_git_root', linked_session_count: 1,
  is_git_root: true, registered_at: '2024-01-01T00:00:00Z',
}

const provider = {
  provider_id: 'codex', display_name: 'Codex', state: 'ready', error: null,
  capabilities: {
    models: ['gpt-5'], reasoning_levels: ['high'], reasoning_summaries: ['concise'],
    attachments: false, attachment_kinds: [], output_schema: false, fork: true, archive: true,
    approvals: false, interruption: true, compaction: false, rollback: false,
  },
}

const session = {
  key: { provider_id: 'codex', provider_session_id: 'session-1' }, project_id: 'project-1',
  cwd: '/workspace/alpha', title: 'Planning session', preview: '', status: 'idle', model: 'gpt-5',
  reasoning_level: 'high', archived: false, created_at: '2024-01-01T00:00:00Z',
  updated_at: '2024-01-01T00:00:00Z',
  turns: [{ turn_id: 'turn-1', status: 'completed', items: [{ type: 'text', text: '# Plan\n\n## Summary\nShip.' }], error: null, created_at: null, completed_at: null, attachment_ids: [] }],
}

describe('App', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { configurable: true, value: vi.fn() })
    vi.mocked(api.getAuthToken).mockReturnValue('test-token')
    vi.mocked(api.listProjects).mockResolvedValue([project])
    vi.mocked(api.listProjectSessions).mockResolvedValue({ sessions: [session], providers: [provider], next_cursor: null } as never)
    vi.mocked(api.getProjectSession).mockResolvedValue(session as never)
    vi.mocked(api.listAttachments).mockResolvedValue([])
    vi.mocked(api.listPendingApprovals).mockResolvedValue([])
    vi.mocked(api.listProjectPlans).mockResolvedValue([])
    vi.mocked(api.listPlanDrafts).mockResolvedValue([])
    vi.mocked(api.listControlPlaneProjects).mockResolvedValue([{ project_id: 'control-project', root: '/workspace/alpha', schema_version: 1 }])
    vi.mocked(api.getControlPlaneReadiness).mockResolvedValue({ ready: true, projects: ['control-project'] })
    vi.mocked(api.getControlPlaneCapabilities).mockResolvedValue({
      schema_version: 1, workflows: ['managed'], teams: [], roles: [], controls: [], context_levels: ['lite'],
      team_upgrade_chains: {}, control_safety: {}, service_features: [],
    })
    vi.mocked(api.listControlPlanePlans).mockResolvedValue([])
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [], next_cursor: null, schema_version: 1 })
    vi.mocked(api.subscribeToRunEvents).mockReturnValue(() => {})
  })

  it('shows the login screen when not authenticated', () => {
    vi.mocked(api.getAuthToken).mockReturnValue(null)
    render(<App />)
    expect(screen.getByPlaceholderText('Auth token')).toBeDefined()
    expect(screen.getByText('Login')).toBeDefined()
  })

  it('shows provider-neutral project and session UX after authentication', async () => {
    render(<App />)
    await waitFor(() => expect(screen.getByText('Alpha Project')).toBeDefined())
    fireEvent.click(screen.getByText('Open'))
    await waitFor(() => expect(screen.getByText('Planning session')).toBeDefined())
    expect(screen.getByText('Projects, planning sessions, and daemon-owned runs')).toBeDefined()
    expect(screen.getByText('1 linked sessions')).toBeDefined()
    expect(api.listProjectSessions).toHaveBeenCalledWith('project-1', { archived: false })
  })

  it('lets the user edit a project path override', async () => {
    vi.mocked(api.updateProject).mockResolvedValue({
      ...project,
      current_path: '/workspace/alpha-renamed',
    })
    render(<App />)

    await waitFor(() => expect(screen.getByText('Alpha Project')).toBeDefined())
    fireEvent.click(screen.getByText('Open'))
    fireEvent.click(screen.getByText('Edit project'))
    fireEvent.change(screen.getByDisplayValue('/workspace/alpha'), { target: { value: '/workspace/alpha-renamed' } })
    fireEvent.click(screen.getByText('Save project'))

    await waitFor(() => expect(api.updateProject).toHaveBeenCalledWith('project-1', {
      display_name: 'Alpha Project',
      current_path: '/workspace/alpha-renamed',
    }))
  })

  it('saves a plan draft from a session turn', async () => {
    vi.mocked(api.savePlanDraft).mockResolvedValue({ name: 'plan', path: '/workspace/alpha/plans/drafts/plan.md', status: 'draft' })
    render(<App />)
    await waitFor(() => expect(screen.getByText('Alpha Project')).toBeDefined())
    fireEvent.click(screen.getByText('Open'))
    await waitFor(() => expect(screen.getByText('Save plan draft')).toBeDefined())
    fireEvent.click(screen.getByText('Save plan draft'))
    await waitFor(() => expect(api.savePlanDraft).toHaveBeenCalledWith('project-1', expect.objectContaining({ content: '# Plan\n\n## Summary\nShip.' })))
  })

  it('shows failure-isolated provider status without exposing raw details', async () => {
    vi.mocked(api.listProjectSessions).mockResolvedValue({
      sessions: [], next_cursor: null,
      providers: [{ ...provider, state: 'unavailable', error: { code: 'provider_unavailable', message: 'Planning provider is unavailable.', provider_id: 'codex', retryable: true } }],
    } as never)
    render(<App />)
    await waitFor(() => expect(screen.getByText('Alpha Project')).toBeDefined())
    fireEvent.click(screen.getByText('Open'))
    await waitFor(() => expect(screen.getByText(/Planning provider is unavailable/)).toBeDefined())
    expect(screen.getByText('No planning sessions found for this project yet.')).toBeDefined()
  })

  it('opens the daemon-owned run dashboard from a selected project plan', async () => {
    vi.mocked(api.listProjectPlans).mockResolvedValue([{
      name: 'demo',
      path: '/workspace/alpha/plans/in-progress/demo.md',
      status: 'in_progress',
      checkpoint_count: 3,
      unchecked_count: 1,
      is_complete: false,
    }])
    render(<App />)

    await waitFor(() => expect(screen.getByText('Alpha Project')).toBeDefined())
    fireEvent.click(screen.getByText('Open'))
    fireEvent.click(screen.getByText('Plans'))
    await waitFor(() => expect(screen.getByText('Open run dashboard')).toBeDefined())
    fireEvent.click(screen.getByText('Open run dashboard'))

    await waitFor(() => expect(screen.getByText('Run dashboard')).toBeDefined())
    await waitFor(() => expect(api.listControlPlaneRuns).toHaveBeenCalledWith('control-project', { limit: 100 }))
  })

  it('logs out and returns to the login form', async () => {
    render(<App />)
    await waitFor(() => expect(screen.getByText('Logout')).toBeDefined())
    fireEvent.click(screen.getByText('Logout'))
    expect(api.clearAuthToken).toHaveBeenCalled()
    expect(screen.getByPlaceholderText('Auth token')).toBeDefined()
  })
})
