import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api'
import * as api from '../api'
import { RunDashboard } from './RunDashboard'
import { App } from '../App'

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
  workflows: ['managed', 'other'],
  teams: ['base', 'full'],
  roles: ['worker'],
  controls: ['max_turns', 'team', 'role_selectors', 'owner_stop'],
  workflow_details: {
    managed: {
      declared_steps: ['plan', 'implement', 'review'],
      executable_steps: ['plan', 'implement', 'review'],
      excluded_steps: ['legacy-step'],
      first_step: 'plan',
      default_team: 'base',
    },
    other: {
      declared_steps: ['research'],
      executable_steps: ['research'],
      excluded_steps: [],
      first_step: 'research',
      default_team: null,
    },
  },
  admitted_role_selectors: { worker: ['harness/impl-a', 'harness/impl-b'] },
  context_levels: ['lite', 'full'] as const,
  team_upgrade_chains: { base: ['base', 'full'], full: ['full'] },
  control_safety: { max_turns: 'safe' as const, team: 'safe' as const, role_selectors: 'safe' as const, owner_stop: 'safe' as const, workflow: 'restart_required' as const },
  service_features: ['controls'],
}

const ownedRun = {
  run_id: 'run-owned', status: 'running', schema_version: 1, ownership: 'control_plane' as const,
  revision: 1, reason: null, unit_name: 'aflow-run-run-owned.service', launch_phase: 'running',
  workflow_name: 'managed', team: 'base', current_step: 'implement', turns_completed: 2, max_turns: 8,
  selected_start_step: null, skipped_steps: [] as string[], restarted_from_run_id: null as string | null,
  evidence: { manifest_created_at: '2024-01-01T00:00:00Z', plan_path: 'plans/todo/demo.md', worktree_path: '/workspace/alpha', branch: 'feature/run' },
}

function renderDashboard(options: { restartPollIntervalMs?: number } = {}) {
  return render(
    <RunDashboard
      initialProjectRoot="/workspace/alpha"
      initialPlanPath={null}
      onInitialPlanHandled={vi.fn()}
      restartPollIntervalMs={options.restartPollIntervalMs}
    />,
  )
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

  it('renders the run overview with lineage, skipped steps, checkpoints, outcomes, and reconciliation evidence', async () => {
    const successorRun = { ...ownedRun, run_id: 'run-successor', restarted_from_run_id: 'run-owned', status: 'running' }
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [{ ...ownedRun, selected_start_step: 'implement', skipped_steps: ['plan'], restarted_from_run_id: 'run-source' }, successorRun], next_cursor: null, schema_version: 1 })
    vi.mocked(api.getRunContext).mockResolvedValue({
      run_id: 'run-owned',
      level: 'lite',
      data: {
        run_metadata: { active_plan_path: 'plans/in-progress/demo-2.md', worktree_path: '/workspace/alpha', branch: 'feature/run' },
        manager_context: {
          plan_state: {
            checkpoints: [{ index: 1, name: 'Setup' }, { index: 2, name: 'Build' }],
            current_checkpoint: { index: 2, name: 'Build' },
          },
          manager_decisions: [{ decision_number: 4, action: 'transition', reason: 'implementation finished' }],
          finished_turn: { turn_number: 3, step_name: 'implement', status: 'completed', returncode: 0, semantic_result: { result: 'implemented the feature' } },
        },
      },
      schema_version: 1,
    })
    vi.mocked(api.listRunEvents).mockResolvedValue([
      { sequence: 1, event_type: 'run_started', data: {}, schema_version: 1, timestamp: '2024-01-01T00:00:00Z' },
      { sequence: 2, event_type: 'control_changed', data: { revision: 2, max_turns: 6, team: 'full', roles: { worker: 'harness/impl-a' } }, schema_version: 1, timestamp: '2024-01-01T00:01:00Z' },
    ])
    renderDashboard()

    await waitFor(() => expect(screen.getByText('successor of run-source')).toBeDefined())
    expect(screen.getByText('source of run-successor')).toBeDefined()
    expect(screen.getByText('implement · plan')).toBeDefined()
    expect(screen.getByText('legacy-step')).toBeDefined()
    await waitFor(() => expect(screen.getByText('Build (2 of 2)')).toBeDefined())
    await waitFor(() => expect(screen.getByText('Decision #4: transition — implementation finished')).toBeDefined())
    expect(screen.getByText('Last finalized turn: turn 3 · implement · completed · exit 0')).toBeDefined()
    expect(screen.getByText('implemented the feature')).toBeDefined()
    expect(screen.getByText(/aflow-run-run-owned\.service · running · not reconciled/)).toBeDefined()
    expect(screen.getByText(/plans\/in-progress\/demo-2\.md/)).toBeDefined()
    expect(screen.getByText(/max turns 6 · team full · selectors worker=harness\/impl-a/)).toBeDefined()
    expect(screen.getByText(/running for /)).toBeDefined()
  })

  it('keeps startup questions distinct from a running workflow and sends an answer idempotently', async () => {
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [], next_cursor: null, schema_version: 1 })
    vi.mocked(api.startControlPlaneRun).mockResolvedValue({
      result: null,
      startup_question: { question_id: 'question-1', kind: 'pick_step', message: 'Choose a step', options: {}, choices: ['implement'], run_id: 'pending-run', schema_version: 1 },
    })
    vi.mocked(api.answerStartupQuestion).mockResolvedValue({
      result: { run_id: 'run-started', created: true, status: 'running', schema_version: 1, manifest_path: null, reason: null, restarted_from_run_id: null },
      startup_question: null,
    })
    renderDashboard()

    await waitFor(() => expect(screen.getByRole('option', { name: 'plans/todo/demo.md' })).toBeDefined())
    fireEvent.change(screen.getByLabelText('Run plan'), { target: { value: 'plans/todo/demo.md' } })
    await waitFor(() => expect((screen.getByLabelText('Run plan') as HTMLSelectElement).value).toBe('plans/todo/demo.md'))
    expect(screen.getByRole('button', { name: 'Start run' }).getAttribute('disabled')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))

    await waitFor(() => expect(screen.getByText(/Awaiting startup answer/)).toBeDefined())
    expect(screen.getByText('No workflow unit exists until an answer is accepted.')).toBeDefined()
    fireEvent.click(screen.getByRole('button', { name: 'implement' }))
    await waitFor(() => expect(api.answerStartupQuestion).toHaveBeenCalledWith(
      'control-project', 'question-1', 'implement', expect.stringMatching(/^startup-answer-/),
    ))
  })

  it('answers confirmation startup questions with booleans for every question kind', async () => {
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [], next_cursor: null, schema_version: 1 })
    vi.mocked(api.startControlPlaneRun).mockResolvedValue({
      result: null,
      startup_question: { question_id: 'question-2', kind: 'confirm_worktree_dirty', message: 'Worktree is dirty. Start anyway?', options: {}, choices: [], run_id: 'pending-run', schema_version: 1 },
    })
    vi.mocked(api.answerStartupQuestion).mockResolvedValue({
      result: { run_id: 'run-started', created: true, status: 'running', schema_version: 1, manifest_path: null, reason: null, restarted_from_run_id: null },
      startup_question: null,
    })
    renderDashboard()

    await waitFor(() => expect(screen.getByRole('option', { name: 'plans/todo/demo.md' })).toBeDefined())
    fireEvent.change(screen.getByLabelText('Run plan'), { target: { value: 'plans/todo/demo.md' } })
    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))
    await screen.findByText('Worktree is dirty. Start anyway?')

    expect(screen.getByRole('button', { name: 'Confirm and continue' })).toBeDefined()
    fireEvent.click(screen.getByRole('button', { name: 'Decline and stop' }))
    await waitFor(() => expect(api.answerStartupQuestion).toHaveBeenCalledWith(
      'control-project', 'question-2', false, expect.stringMatching(/^startup-answer-/),
    ))
  })

  it('offers capability-admitted start steps, explains skips, and sends the canonical step name', async () => {
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [], next_cursor: null, schema_version: 1 })
    vi.mocked(api.startControlPlaneRun).mockResolvedValue({
      result: { run_id: 'run-started', created: true, status: 'running', schema_version: 1, manifest_path: null, reason: null, restarted_from_run_id: null },
      startup_question: null,
    })
    renderDashboard()

    await waitFor(() => expect(screen.getByRole('option', { name: 'plans/todo/demo.md' })).toBeDefined())
    fireEvent.change(screen.getByLabelText('Run plan'), { target: { value: 'plans/todo/demo.md' } })
    expect((screen.getByLabelText('Run start step') as HTMLSelectElement).disabled).toBe(true)
    fireEvent.change(screen.getByLabelText('Run workflow'), { target: { value: 'managed' } })

    expect(screen.getByRole('option', { name: '1 · plan' })).toBeDefined()
    expect(screen.getByRole('option', { name: '2 · implement' })).toBeDefined()
    expect(screen.getByRole('option', { name: '3 · review' })).toBeDefined()
    expect(screen.getByRole('option', { name: 'Workflow default (base)' })).toBeDefined()
    fireEvent.change(screen.getByLabelText('Run start step'), { target: { value: 'implement' } })
    const skipNotice = screen.getByText(/skips the earlier executable steps:/)
    expect(skipNotice.textContent).toContain('plan')

    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))
    await waitFor(() => expect(api.startControlPlaneRun).toHaveBeenCalledWith(
      'control-project',
      expect.objectContaining({ plan_path: 'plans/todo/demo.md', workflow_name: 'managed', start_step: 'implement' }),
      expect.stringMatching(/^start-/),
    ))
    const request = vi.mocked(api.startControlPlaneRun).mock.calls[0][1]
    expect(request.extra_instructions).toBeUndefined()
  })

  it('sends bounded extra instructions and blocks oversized drafts before the server does', async () => {
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [], next_cursor: null, schema_version: 1 })
    vi.mocked(api.startControlPlaneRun).mockResolvedValue({
      result: { run_id: 'run-started', created: true, status: 'running', schema_version: 1, manifest_path: null, reason: null, restarted_from_run_id: null },
      startup_question: null,
    })
    renderDashboard()

    await waitFor(() => expect(screen.getByRole('option', { name: 'plans/todo/demo.md' })).toBeDefined())
    fireEvent.change(screen.getByLabelText('Run plan'), { target: { value: 'plans/todo/demo.md' } })
    fireEvent.change(screen.getByLabelText('Run extra instructions'), { target: { value: '  Focus on tests.  \n\nKeep runs short. ' } })
    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))
    await waitFor(() => expect(api.startControlPlaneRun).toHaveBeenCalledWith(
      'control-project',
      expect.objectContaining({ extra_instructions: ['Focus on tests.', 'Keep runs short.'] }),
      expect.anything(),
    ))

    fireEvent.change(screen.getByLabelText('Run extra instructions'), {
      target: { value: Array.from({ length: 9 }, (_, index) => `instruction ${index + 1}`).join('\n') },
    })
    await waitFor(() => expect(screen.getByText(/At most 8 instruction lines/)).toBeDefined())
    expect(screen.getByRole('button', { name: 'Start run' }).getAttribute('disabled')).not.toBeNull()
    expect(api.startControlPlaneRun).toHaveBeenCalledTimes(1)
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

  it('refreshes a CAS conflict, keeps local control edits, and reports restart-required changes', async () => {
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
    expect((screen.getByLabelText('Control max turns') as HTMLInputElement).value).toBe('9')

    fireEvent.change(screen.getByLabelText('Control max turns'), { target: { value: '10' } })
    fireEvent.click(screen.getByRole('button', { name: 'Apply safe controls' }))
    await waitFor(() => expect(screen.getByText(/server requires a restart/)).toBeDefined())
    expect((screen.getByLabelText('Control max turns') as HTMLInputElement).value).toBe('10')
  })

  it('offers only capability-admitted selector values and explains the next safe boundary', async () => {
    vi.mocked(api.controlControlPlaneRun).mockResolvedValue({
      revision: 2,
      changed: true,
      owner_stop: false,
      run: { ...ownedRun, revision: 2 },
    })
    renderDashboard()

    await waitFor(() => expect(screen.getByLabelText('Selector for worker')).toBeDefined())
    expect((screen.getByLabelText('Selector for worker') as HTMLSelectElement).tagName).toBe('SELECT')
    expect(screen.getByRole('option', { name: 'harness/impl-a' })).toBeDefined()
    expect(screen.getByRole('option', { name: 'harness/impl-b' })).toBeDefined()
    expect(screen.getByText(/next safe boundary between turns/)).toBeDefined()

    fireEvent.change(screen.getByLabelText('Control team'), { target: { value: 'full' } })
    fireEvent.change(screen.getByLabelText('Selector for worker'), { target: { value: 'harness/impl-b' } })
    await waitFor(() => expect((screen.getByLabelText('Control team') as HTMLSelectElement).value).toBe('full'))
    fireEvent.click(screen.getByRole('button', { name: 'Apply safe controls' }))
    await waitFor(() => expect(api.controlControlPlaneRun).toHaveBeenCalledWith(
      'control-project',
      'run-owned',
      expect.objectContaining({ expected_revision: 1, team: 'full', role_selectors: { worker: 'harness/impl-b' } }),
      expect.stringMatching(/^control-/),
    ))
    await waitFor(() => expect(screen.getByText(/revision 2\. The engine applies them at the next safe boundary/)).toBeDefined())
  })

  it('restarts a workflow change through owner stop, exact inactive proof, and a lineage-linked successor', async () => {
    const stopped = { ...ownedRun, status: 'owner_stopped', launch_phase: 'owner_stopped' }
    vi.mocked(api.ownerStopControlPlaneRun).mockResolvedValue(stopped)
    vi.mocked(api.getControlPlaneRun)
      .mockResolvedValueOnce(ownedRun)
      .mockResolvedValue(stopped)
    vi.mocked(api.startControlPlaneRun).mockResolvedValue({
      result: { run_id: 'run-successor', created: true, status: 'running', schema_version: 1, manifest_path: null, reason: null, restarted_from_run_id: 'run-owned' },
      startup_question: null,
    })
    renderDashboard({ restartPollIntervalMs: 1 })

    await waitFor(() => expect(screen.getByRole('option', { name: 'plans/todo/demo.md' })).toBeDefined())
    fireEvent.change(screen.getByLabelText('Run plan'), { target: { value: 'plans/todo/demo.md' } })
    fireEvent.change(screen.getByLabelText('Run workflow'), { target: { value: 'other' } })
    fireEvent.click(screen.getByRole('button', { name: 'Change workflow: stop and restart…' }))
    expect(screen.getByText(/and start successor workflow/)).toBeDefined()
    expect(screen.getAllByText('run-owned').length).toBeGreaterThanOrEqual(2)
    fireEvent.click(screen.getByRole('button', { name: 'Confirm stop and start successor' }))

    await waitFor(() => expect(api.startControlPlaneRun).toHaveBeenCalledWith(
      'control-project',
      expect.objectContaining({ plan_path: 'plans/todo/demo.md', workflow_name: 'other', restarted_from_run_id: 'run-owned' }),
      expect.anything(),
    ))
    expect(vi.mocked(api.ownerStopControlPlaneRun).mock.invocationCallOrder[0])
      .toBeLessThan(vi.mocked(api.startControlPlaneRun).mock.invocationCallOrder[0])
    expect(vi.mocked(api.ownerStopControlPlaneRun)).toHaveBeenCalledWith(
      'control-project', 'run-owned', 1, expect.stringMatching(/^owner-stop-/),
    )
    await waitFor(() => expect(screen.getByText(/Successor start created run run-successor/)).toBeDefined())
    expect(screen.getByText(/records run-owned as its restart source/)).toBeDefined()
  })

  it('preserves exact successor recovery across run selection and workspace remount', async () => {
    const stopped = { ...ownedRun, status: 'owner_stopped', launch_phase: 'owner_stopped' }
    vi.mocked(api.ownerStopControlPlaneRun).mockResolvedValue(stopped)
    vi.mocked(api.getControlPlaneRun).mockResolvedValueOnce(ownedRun).mockResolvedValue(stopped)
    vi.mocked(api.startControlPlaneRun)
      .mockRejectedValueOnce(new Error('response lost after acceptance'))
      .mockResolvedValueOnce({
        result: { run_id: 'run-successor', created: false, status: 'running', schema_version: 1, manifest_path: null, reason: null, restarted_from_run_id: 'run-owned' },
        startup_question: null,
      })
    const otherRun = { ...ownedRun, run_id: 'run-other', status: 'completed' }
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [ownedRun, otherRun], next_cursor: null, schema_version: 1 })
    vi.mocked(api.getControlPlaneRun).mockImplementation(async (_projectId, runId) => runId === 'run-other'
      ? otherRun
      : api.ownerStopControlPlaneRun.mock.calls.length ? stopped : ownedRun)
    vi.spyOn(api, 'getAuthToken').mockReturnValue('test-token')
    vi.spyOn(api, 'listProjects').mockResolvedValue([{
      id: project.project_id, display_name: 'Alpha', current_path: project.root,
      is_git_root: true, registered_at: '2026-01-01T00:00:00Z', readiness: 'ready',
    }])
    render(<App />)
    fireEvent.click(await screen.findByText('Alpha'))
    fireEvent.click(screen.getByRole('button', { name: 'Runs' }))


    await waitFor(() => expect(screen.getByRole('option', { name: 'plans/todo/demo.md' })).toBeDefined())
    fireEvent.change(screen.getByLabelText('Run plan'), { target: { value: 'plans/todo/demo.md' } })
    fireEvent.change(screen.getByLabelText('Run workflow'), { target: { value: 'other' } })
    fireEvent.click(screen.getByRole('button', { name: 'Change workflow: stop and restart…' }))
    fireEvent.click(screen.getByRole('button', { name: 'Confirm stop and start successor' }))

    await screen.findByText(/Successor outcome is unknown/)
    expect(screen.getByLabelText('Run workflow').getAttribute('disabled')).not.toBeNull()
    expect(screen.getByRole('button', { name: 'Start run' }).getAttribute('disabled')).not.toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /run-other completed/ }))
    await screen.findByRole('heading', { name: 'Run run-other' })
    expect(screen.getByRole('button', { name: 'Retry exact successor request' })).toBeDefined()
    expect(screen.getByRole('button', { name: 'Start run' }).getAttribute('disabled')).not.toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Projects' }))
    await screen.findByText(/Its exact request remains preserved/)
    fireEvent.click(screen.getByRole('button', { name: 'Resolve pending successor' }))
    await screen.findByRole('button', { name: 'Retry exact successor request' })
    expect(screen.getByRole('button', { name: 'Start run' }).getAttribute('disabled')).not.toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Retry exact successor request' }))

    await waitFor(() => expect(api.startControlPlaneRun).toHaveBeenCalledTimes(2))
    expect(api.startControlPlaneRun.mock.calls[1][0]).toBe(api.startControlPlaneRun.mock.calls[0][0])
    expect(api.startControlPlaneRun.mock.calls[1][1]).toEqual(api.startControlPlaneRun.mock.calls[0][1])
    expect(api.startControlPlaneRun.mock.calls[1][2]).toBe(api.startControlPlaneRun.mock.calls[0][2])
    expect(api.ownerStopControlPlaneRun).toHaveBeenCalledTimes(1)
    await screen.findByText(/Successor retry replay returned existing run run-successor/)
  })

  it('clears an uncertain successor recovery only after a definitive rejection', async () => {
    const stopped = { ...ownedRun, status: 'owner_stopped', launch_phase: 'owner_stopped' }
    vi.mocked(api.ownerStopControlPlaneRun).mockResolvedValue(stopped)
    vi.mocked(api.getControlPlaneRun).mockResolvedValueOnce(ownedRun).mockResolvedValue(stopped)
    vi.mocked(api.startControlPlaneRun)
      .mockRejectedValueOnce(new Error('response lost'))
      .mockRejectedValueOnce(new ApiError(422, 'invalid successor', 'validation_error'))
    renderDashboard({ restartPollIntervalMs: 1 })

    await waitFor(() => expect(screen.getByRole('option', { name: 'plans/todo/demo.md' })).toBeDefined())
    fireEvent.change(screen.getByLabelText('Run plan'), { target: { value: 'plans/todo/demo.md' } })
    fireEvent.change(screen.getByLabelText('Run workflow'), { target: { value: 'other' } })
    fireEvent.click(screen.getByRole('button', { name: 'Change workflow: stop and restart…' }))
    fireEvent.click(screen.getByRole('button', { name: 'Confirm stop and start successor' }))
    await screen.findByRole('button', { name: 'Retry exact successor request' })
    fireEvent.click(screen.getByRole('button', { name: 'Retry exact successor request' }))

    await screen.findByText(/Successor start was rejected: invalid successor/)
    expect(screen.queryByRole('button', { name: 'Retry exact successor request' })).toBeNull()
    expect(api.ownerStopControlPlaneRun).toHaveBeenCalledTimes(1)
  })

  it('does not expose guided workflow restart for terminal owned runs', async () => {
    const ownerStopped = { ...ownedRun, status: 'owner_stopped', launch_phase: 'owner_stopped' }
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [ownerStopped], next_cursor: null, schema_version: 1 })
    vi.mocked(api.getControlPlaneRun).mockResolvedValue(ownerStopped)
    const rendered = renderDashboard()

    await screen.findByText('owner stopped')
    expect(screen.queryByRole('button', { name: /Change workflow/ })).toBeNull()
    rendered.unmount()

    const completed = { ...ownedRun, status: 'completed', launch_phase: 'completed' }
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [completed], next_cursor: null, schema_version: 1 })
    vi.mocked(api.getControlPlaneRun).mockResolvedValue(completed)
    renderDashboard()
    await screen.findByText('completed')
    expect(screen.queryByRole('button', { name: /Change workflow/ })).toBeNull()
    expect(api.ownerStopControlPlaneRun).not.toHaveBeenCalled()
    expect(api.startControlPlaneRun).not.toHaveBeenCalled()
  })

  it('halts restart automation with the draft preserved when the owner stop fails', async () => {
    vi.mocked(api.ownerStopControlPlaneRun).mockRejectedValue(new Error('stop rejected by server'))
    renderDashboard({ restartPollIntervalMs: 1 })

    await waitFor(() => expect(screen.getByRole('option', { name: 'plans/todo/demo.md' })).toBeDefined())
    fireEvent.change(screen.getByLabelText('Run plan'), { target: { value: 'plans/todo/demo.md' } })
    fireEvent.change(screen.getByLabelText('Run workflow'), { target: { value: 'other' } })
    fireEvent.click(screen.getByRole('button', { name: 'Change workflow: stop and restart…' }))
    fireEvent.click(screen.getByRole('button', { name: 'Confirm stop and start successor' }))

    await waitFor(() => expect(screen.getByText(/Restart stopped without a successor: stop rejected by server/)).toBeDefined())
    expect(api.startControlPlaneRun).not.toHaveBeenCalled()
    expect(screen.getByText(/source run state above is authoritative and your draft is preserved/)).toBeDefined()
    expect((screen.getByLabelText('Run workflow') as HTMLSelectElement).value).toBe('other')
    expect(screen.getAllByText('running').length).toBeGreaterThan(0)
  })

  it('never starts a successor when source inactivity cannot be proven after a disconnect', async () => {
    const stopped = { ...ownedRun, status: 'owner_stopped', launch_phase: 'owner_stopped' }
    vi.mocked(api.ownerStopControlPlaneRun).mockResolvedValue(stopped)
    vi.mocked(api.getControlPlaneRun).mockRejectedValue(new Error('network unreachable'))
    renderDashboard({ restartPollIntervalMs: 1 })

    await waitFor(() => expect(screen.getByRole('option', { name: 'plans/todo/demo.md' })).toBeDefined())
    fireEvent.change(screen.getByLabelText('Run plan'), { target: { value: 'plans/todo/demo.md' } })
    fireEvent.change(screen.getByLabelText('Run workflow'), { target: { value: 'other' } })
    fireEvent.click(screen.getByRole('button', { name: 'Change workflow: stop and restart…' }))
    fireEvent.click(screen.getByRole('button', { name: 'Confirm stop and start successor' }))

    await waitFor(() => expect(screen.getByText(/Source inactivity could not be confirmed/)).toBeDefined())
    expect(screen.getByText(/No successor was started/)).toBeDefined()
    expect(api.startControlPlaneRun).not.toHaveBeenCalled()
  })

  it('waits without overlap until the source is terminal and never starts a successor early', async () => {
    vi.mocked(api.ownerStopControlPlaneRun).mockResolvedValue({ ...ownedRun, status: 'owner_stopped', launch_phase: 'owner_stopped' })
    // The canonical status keeps reporting the source as not yet proven terminal.
    vi.mocked(api.getControlPlaneRun).mockResolvedValue(ownedRun)
    renderDashboard({ restartPollIntervalMs: 1 })

    await waitFor(() => expect(screen.getByRole('option', { name: 'plans/todo/demo.md' })).toBeDefined())
    fireEvent.change(screen.getByLabelText('Run plan'), { target: { value: 'plans/todo/demo.md' } })
    fireEvent.change(screen.getByLabelText('Run workflow'), { target: { value: 'other' } })
    fireEvent.click(screen.getByRole('button', { name: 'Change workflow: stop and restart…' }))
    fireEvent.click(screen.getByRole('button', { name: 'Confirm stop and start successor' }))

    await waitFor(() => expect(screen.getByText(/Source inactivity could not be confirmed/)).toBeDefined())
    expect(vi.mocked(api.getControlPlaneRun).mock.calls.length).toBeGreaterThanOrEqual(31)
    expect(api.startControlPlaneRun).not.toHaveBeenCalled()
  })

  it('keeps a stream failure distinct from run state and refreshes once after reconnect', async () => {
    let onEventsHook: ((events: api.RunEvent[]) => void) | null = null
    let onStateHook: ((state: api.StreamState) => void) | null = null
    vi.mocked(api.subscribeToRunEvents).mockImplementation((subscription) => {
      onEventsHook = subscription.onEvents
      onStateHook = subscription.onStateChange ?? null
      return () => {}
    })
    renderDashboard()

    await waitFor(() => expect(screen.getByText(/aflow-run-run-owned\.service/)).toBeDefined())
    if (!onStateHook || !onEventsHook) throw new Error('subscription hooks were not registered')

    onStateHook!('reconnecting')
    expect(await screen.findByText(/stream reconnecting — last snapshot shown/)).toBeDefined()
    const callsBefore = vi.mocked(api.getControlPlaneRun).mock.calls.length
    onStateHook!('connected')
    expect(await screen.findByText(/stream connected/)).toBeDefined()
    await waitFor(() => expect(vi.mocked(api.getControlPlaneRun).mock.calls.length).toBeGreaterThan(callsBefore))

    // A stream error never changes the displayed run status or timeline.
    onStateHook!('reconnecting')
    onEventsHook!([{ sequence: 2, event_type: 'step_started', data: {}, schema_version: 1, timestamp: '2024-01-01T00:02:00Z' }])
    expect(await screen.findByText(/step started/)).toBeDefined()
    expect(screen.getAllByText('running').length).toBeGreaterThan(0)
  })

  it('requires confirmation for owner stop and explicit resume with separate source and continuation identities', async () => {
    const attentionRun = { ...ownedRun, run_id: 'run-needs-attention', status: 'needs_attention', revision: 3 }
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [attentionRun], next_cursor: null, schema_version: 1 })
    vi.mocked(api.getControlPlaneRun).mockResolvedValue(attentionRun)
    vi.mocked(api.ownerStopControlPlaneRun).mockResolvedValue({ ...attentionRun, status: 'owner_stopped' })
    vi.mocked(api.resumeControlPlaneRun).mockResolvedValue({ run_id: 'run-continuation', created: false, status: 'running', schema_version: 1, manifest_path: null, reason: null, restarted_from_run_id: null })
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
    expect(screen.queryByRole('button', { name: /Change workflow/ })).toBeNull()
  })
})
