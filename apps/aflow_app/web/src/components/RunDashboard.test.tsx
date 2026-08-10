import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { RunDashboard } from './RunDashboard'

vi.mock('../api', async () => {
  const actual = await vi.importActual<typeof import('../api')>('../api')
  return {
    ...actual,
    listControlPlaneProjects: vi.fn(), getControlPlaneReadiness: vi.fn(), getControlPlaneCapabilities: vi.fn(), listControlPlanePlans: vi.fn(),
    listControlPlaneRuns: vi.fn(), getControlPlaneRun: vi.fn(), listRunEvents: vi.fn(), getRunContext: vi.fn(),
    startControlPlaneRun: vi.fn(), answerStartupQuestion: vi.fn(), controlControlPlaneRun: vi.fn(),
    ownerStopControlPlaneRun: vi.fn(), resumeControlPlaneRun: vi.fn(), subscribeToRunEvents: vi.fn(),
  }
})

const project = { project_id: 'control-project', root: '/workspace/alpha', schema_version: 1 }
const capabilities = {
  schema_version: 1,
  workflows: ['managed'],
  teams: ['base', 'full'],
  roles: ['worker'],
  controls: ['max_turns', 'team', 'role_selectors', 'owner_stop'],
  context_levels: ['lite', 'full'] as const,
  team_upgrade_chains: { base: ['base', 'full'], full: ['full'] },
  control_safety: { max_turns: 'safe' as const, team: 'safe' as const, role_selectors: 'safe' as const, owner_stop: 'safe' as const, workflow: 'restart_required' as const },
  service_features: ['controls'],
}

const ownedRun = {
  run_id: 'run-owned', status: 'running', schema_version: 1, ownership: 'control_plane' as const,
  revision: 1, reason: null, unit_name: 'aflow-run-run-owned.service', launch_phase: 'running',
  workflow_name: 'managed', team: 'base', current_step: 'implement', turns_completed: 2, max_turns: 8,
  evidence: { manifest_created_at: '2024-01-01T00:00:00Z', plan_path: 'plans/todo/demo.md', worktree_path: '/workspace/alpha', branch: 'feature/run' },
}

function renderDashboard() {
  return render(<RunDashboard initialProjectRoot="/workspace/alpha" initialPlanPath={null} onInitialPlanHandled={vi.fn()} />)
}

describe('RunDashboard', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(api.listControlPlaneProjects).mockResolvedValue([project])
    vi.mocked(api.getControlPlaneReadiness).mockResolvedValue({ ready: true, projects: ['control-project'] })
    vi.mocked(api.getControlPlaneCapabilities).mockResolvedValue(capabilities)
    vi.mocked(api.listControlPlanePlans).mockResolvedValue([{ path: 'plans/todo/demo.md', status: 'todo', modified_at: '2024-01-01T00:00:00Z', schema_version: 1 }])
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [ownedRun], next_cursor: null, schema_version: 1 })
    vi.mocked(api.getControlPlaneRun).mockResolvedValue(ownedRun)
    vi.mocked(api.listRunEvents).mockResolvedValue([{ sequence: 1, event_type: 'run_started', data: {}, schema_version: 1, timestamp: '2024-01-01T00:00:00Z' }])
    vi.mocked(api.getRunContext).mockResolvedValue({ run_id: 'run-owned', level: 'lite', data: { status: 'running' }, schema_version: 1 })
    vi.mocked(api.subscribeToRunEvents).mockReturnValue(() => {})
  })

  it('keeps the server snapshot visible through a failed daemon refresh', async () => {
    vi.mocked(api.listControlPlaneRuns).mockResolvedValueOnce({ runs: [ownedRun], next_cursor: null, schema_version: 1 }).mockRejectedValueOnce(new Error('daemon unavailable'))
    renderDashboard()

    await waitFor(() => expect(screen.getByText(/aflow-run-run-owned\.service/)).toBeDefined())
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))

    await waitFor(() => expect(screen.getByText(/Existing run data remains visible/)).toBeDefined())
    expect(screen.getByText(/aflow-run-run-owned\.service/)).toBeDefined()
    expect(screen.getAllByText('running').length).toBeGreaterThan(0)
  })

  it('keeps startup questions distinct from a running workflow and sends an answer idempotently', async () => {
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [], next_cursor: null, schema_version: 1 })
    vi.mocked(api.startControlPlaneRun).mockResolvedValue({
      result: null,
      startup_question: { question_id: 'question-1', kind: 'pick_step', message: 'Choose a step', options: {}, choices: ['implement'], run_id: 'pending-run', schema_version: 1 },
    })
    vi.mocked(api.answerStartupQuestion).mockResolvedValue({
      result: { run_id: 'run-started', created: true, status: 'running', schema_version: 1, manifest_path: null, reason: null },
      startup_question: null,
    })
    renderDashboard()

    await waitFor(() => expect(screen.getByRole('option', { name: 'plans/todo/demo.md' })).toBeDefined())
    fireEvent.change(screen.getByLabelText('Run plan'), { target: { value: 'plans/todo/demo.md' } })
    await waitFor(() => expect((screen.getByLabelText('Run plan') as HTMLSelectElement).value).toBe('plans/todo/demo.md'))
    expect(screen.getByRole('button', { name: 'Start run' }).getAttribute('disabled')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))

    await waitFor(() => expect(screen.getByText('Awaiting startup answer')).toBeDefined())
    expect(screen.getByText('No workflow unit exists until an answer is accepted.')).toBeDefined()
    expect(screen.queryByText('running')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'implement' }))
    await waitFor(() => expect(api.answerStartupQuestion).toHaveBeenCalledWith(
      'control-project', 'question-1', 'implement', expect.stringMatching(/^startup-answer-/),
    ))
  })

  it('reuses a start key after an uncertain failure and replaces it when the start intent changes', async () => {
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [], next_cursor: null, schema_version: 1 })
    vi.mocked(api.startControlPlaneRun).mockRejectedValue(new Error('connection lost'))
    renderDashboard()

    await waitFor(() => expect(screen.getByRole('option', { name: 'plans/todo/demo.md' })).toBeDefined())
    fireEvent.change(screen.getByLabelText('Run plan'), { target: { value: 'plans/todo/demo.md' } })
    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))
    await waitFor(() => expect(screen.getByText('connection lost')).toBeDefined())

    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))
    await waitFor(() => expect(api.startControlPlaneRun).toHaveBeenCalledTimes(2))
    const unchangedRetryKey = vi.mocked(api.startControlPlaneRun).mock.calls[0][2]
    expect(vi.mocked(api.startControlPlaneRun).mock.calls[1][2]).toBe(unchangedRetryKey)

    fireEvent.change(screen.getByLabelText('Run max turns'), { target: { value: '9' } })
    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))
    await waitFor(() => expect(api.startControlPlaneRun).toHaveBeenCalledTimes(3))
    expect(vi.mocked(api.startControlPlaneRun).mock.calls[2][2]).not.toBe(unchangedRetryKey)
    expect(vi.mocked(api.startControlPlaneRun).mock.calls[2][1]).toEqual({ plan_path: 'plans/todo/demo.md', max_turns: 9 })
  })

  it('refreshes a CAS conflict and reports restart-required changes without inventing live controls', async () => {
    vi.mocked(api.controlControlPlaneRun)
      .mockRejectedValueOnce(Object.assign(new Error('revision conflict'), { code: 'revision_conflict' }))
      .mockRejectedValueOnce(Object.assign(new Error('restart required'), { code: 'restart_required' }))
    vi.mocked(api.getControlPlaneRun).mockResolvedValue({ ...ownedRun, revision: 2 })
    renderDashboard()

    await waitFor(() => expect(screen.getByLabelText('Control max turns')).toBeDefined())
    await waitFor(() => expect(screen.getByText('Canonical identity · revision 2')).toBeDefined())
    expect(screen.getByText('workflow requires restart; it is not offered as a live control.')).toBeDefined()
    fireEvent.change(screen.getByLabelText('Control max turns'), { target: { value: '9' } })
    fireEvent.click(screen.getByRole('button', { name: 'Apply safe controls' }))
    await waitFor(() => expect(screen.getByText(/Another operator changed this run/)).toBeDefined())
    expect(api.getControlPlaneRun).toHaveBeenCalledWith('control-project', 'run-owned')

    fireEvent.change(screen.getByLabelText('Control max turns'), { target: { value: '10' } })
    fireEvent.click(screen.getByRole('button', { name: 'Apply safe controls' }))
    await waitFor(() => expect(screen.getByText(/server requires a restart/)).toBeDefined())
  })

  it('requires confirmation for owner stop and explicit resume with separate source and continuation identities', async () => {
    const attentionRun = { ...ownedRun, run_id: 'run-needs-attention', status: 'needs_attention', revision: 3 }
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [attentionRun], next_cursor: null, schema_version: 1 })
    vi.mocked(api.getControlPlaneRun).mockResolvedValue(attentionRun)
    vi.mocked(api.ownerStopControlPlaneRun).mockResolvedValue({ ...attentionRun, status: 'owner_stopped' })
    vi.mocked(api.resumeControlPlaneRun).mockResolvedValue({ run_id: 'run-continuation', created: false, status: 'running', schema_version: 1, manifest_path: null, reason: null })
    const rendered = renderDashboard()

    await waitFor(() => expect(screen.getByText('Needs attention — explicit resume required')).toBeDefined())
    fireEvent.click(screen.getByRole('button', { name: /Owner stop/ }))
    expect(screen.getByText(/Confirm owner stop for run-needs-attention/)).toBeDefined()
    fireEvent.click(screen.getByRole('button', { name: 'Confirm stop' }))
    await waitFor(() => expect(api.ownerStopControlPlaneRun).toHaveBeenCalledWith(
      'control-project', 'run-needs-attention', 3, expect.stringMatching(/^owner-stop-/),
    ))

    rendered.unmount()
    renderDashboard()
    await waitFor(() => expect(screen.getByRole('button', { name: /Resume as new run/ })).toBeDefined())
    fireEvent.click(screen.getByRole('button', { name: /Resume as new run/ }))
    expect(screen.getByText(/Source run-needs-attention remains visible/)).toBeDefined()
    fireEvent.click(screen.getByRole('button', { name: 'Confirm resume' }))
    await waitFor(() => expect(api.resumeControlPlaneRun).toHaveBeenCalledWith(
      'control-project', 'run-needs-attention', expect.stringMatching(/^resume-/),
    ))
    await waitFor(() => expect(screen.getByText(/Replay returned continuation run-continuation from source run run-needs-attention; no duplicate was created/)).toBeDefined())
  })

  it('reuses a resume key after an uncertain failure', async () => {
    const attentionRun = { ...ownedRun, run_id: 'run-needs-attention', status: 'needs_attention', revision: 3 }
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [attentionRun], next_cursor: null, schema_version: 1 })
    vi.mocked(api.getControlPlaneRun).mockResolvedValue(attentionRun)
    vi.mocked(api.resumeControlPlaneRun).mockRejectedValue(new Error('connection lost'))
    renderDashboard()

    await waitFor(() => expect(screen.getByRole('button', { name: /Resume as new run/ })).toBeDefined())
    fireEvent.click(screen.getByRole('button', { name: /Resume as new run/ }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Confirm resume' })).toBeDefined())
    fireEvent.click(screen.getByRole('button', { name: 'Confirm resume' }))
    await waitFor(() => expect(screen.getByText('connection lost')).toBeDefined())

    fireEvent.click(screen.getByRole('button', { name: 'Confirm resume' }))
    await waitFor(() => expect(api.resumeControlPlaneRun).toHaveBeenCalledTimes(2))
    expect(vi.mocked(api.resumeControlPlaneRun).mock.calls[1][2]).toBe(
      vi.mocked(api.resumeControlPlaneRun).mock.calls[0][2],
    )
  })

  it('classifies stale legacy runs as interrupted read-only records', async () => {
    const legacyRun = { ...ownedRun, run_id: 'legacy-run', ownership: 'legacy' as const, status: 'interrupted', unit_name: null, reason: 'legacy run has no control-plane launch manifest' }
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [legacyRun], next_cursor: null, schema_version: 1 })
    vi.mocked(api.getControlPlaneRun).mockResolvedValue(legacyRun)
    renderDashboard()

    await waitFor(() => expect(screen.getByText('Legacy interrupted (read-only)')).toBeDefined())
    expect(screen.getByText(/Legacy record classified as interrupted and read-only/)).toBeDefined()
    expect(screen.getByLabelText('Control max turns').getAttribute('disabled')).not.toBeNull()
    expect(screen.queryByRole('button', { name: /Owner stop/ })).toBeNull()
    expect(screen.queryByRole('button', { name: /Resume as new run/ })).toBeNull()
  })
})
