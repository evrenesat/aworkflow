import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api'
import * as api from '../api'
import { RunDashboard, type RunSelectionChange } from './RunDashboard'
import { App } from '../App'

vi.mock('../api', async () => {
  const actual = await vi.importActual<typeof import('../api')>('../api')
  return {
    ...actual,
    checkSession: vi.fn(),
    getGlobalConfig: vi.fn(), postGlobalConfigForm: vi.fn(), getRestartOptions: vi.fn().mockResolvedValue(null),
    listControlPlaneProjects: vi.fn(), getControlPlaneReadiness: vi.fn(), getControlPlaneCapabilities: vi.fn(), listControlPlanePlans: vi.fn(),
    listControlPlaneRuns: vi.fn(), getControlPlaneRun: vi.fn(), listRunEvents: vi.fn(), getRunContext: vi.fn(),
    startControlPlaneRun: vi.fn(), answerStartupQuestion: vi.fn(), controlControlPlaneRun: vi.fn(),
    changeRunHistory: vi.fn(), ownerStopControlPlaneRun: vi.fn(), resumeControlPlaneRun: vi.fn(), subscribeToRunEvents: vi.fn(),
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
  started_at: '2024-01-01T00:00:00Z',
  evidence: { manifest_created_at: '2024-01-01T00:00:00Z', plan_path: 'plans/in-progress/demo.md', worktree_path: '/workspace/alpha', branch: 'feature/run' },
}

const committedConfig = {
  project_id: 'control-project',
  revision: 'a'.repeat(64),
  documents: ['aflow.toml', 'workflows.toml'],
  aflow_toml: '# committed aflow\n',
  workflows_toml: '# committed workflows\n',
  validation: {
    state: 'ready' as const, issues: [] as never[], placeholders: [] as string[],
    workflows: ['managed', 'other'], teams: ['base', 'full'], roles: ['worker'],
  },
}

/** A committed projection whose configured workflows carry exact step roles. */
const emptyProjection = {
  aflow_toml: committedConfig.aflow_toml,
  workflows_toml: committedConfig.workflows_toml,
  changed: false,
  validation: committedConfig.validation,
  form: {
    default_workflow: null as string | null,
    max_turns: null as number | null,
    harnesses: {} as Record<string, Record<string, { model: string | null; effort: string | null }>>,
    roles: {} as Record<string, string>,
    teams: {} as Record<string, { roles: Record<string, string> }>,
    workflow_default_teams: {} as Record<string, string | null>,
    workflows: {
      managed: {
        declared_steps: ['plan', 'implement', 'review'],
        first_step: 'plan',
        executable_steps: ['plan', 'implement', 'review'],
        first_executable_step: 'plan',
        step_roles: { plan: 'reviewer', implement: 'worker', review: 'reviewer' },
      },
      other: {
        declared_steps: ['research'],
        first_step: 'research',
        executable_steps: ['research'],
        first_executable_step: 'research',
        step_roles: { research: 'worker' },
      },
    } as Record<string, { declared_steps: string[]; first_step: string | null; executable_steps: string[] | null; first_executable_step: string | null; step_roles: Record<string, string> | null }>,
  },
  syntax_issues: [] as never[],
  choices: { harnesses: [] as string[], profiles: {} as Record<string, string[]>, selectors: [] as string[], roles: [] as string[], teams: [] as string[], workflows: [] as string[] },
  suggestions: { label: 'suggestion', harnesses: [], profiles: [], note: 'Bundled values are labeled suggestions.' },
  starter_defaults: null,
}

function renderDashboard(options: {
  restartPollIntervalMs?: number
  initialPlanPath?: string | null
  onInitialPlanHandled?: () => void
  requestedRunId?: string | null
  onRunSelectionChange?: (change: RunSelectionChange) => void
} = {}) {
  return render(
    <RunDashboard
      projectId="control-project"
      requestedRunId={options.requestedRunId ?? null}
      onRunSelectionChange={options.onRunSelectionChange}
      initialPlanPath={options.initialPlanPath ?? null}
      onInitialPlanHandled={options.onInitialPlanHandled ?? vi.fn()}
      restartPollIntervalMs={options.restartPollIntervalMs}
      onOpenSettings={vi.fn()}
    />,
  )
}

function renderDashboardNode(node: React.ReactElement) {
  return render(node)
}

function dashboardNode(options: { requestedRunId?: string | null } = {}) {
  return (
    <RunDashboard
      projectId="control-project"
      requestedRunId={options.requestedRunId ?? null}
      initialPlanPath={null}
      onInitialPlanHandled={vi.fn()}
      onOpenSettings={vi.fn()}
    />
  )
}

/** Commits an option in a searchable combobox control. */
function choose(label: string, value: string) {
  const input = screen.getByLabelText(label)
  fireEvent.focus(input)
  fireEvent.change(input, { target: { value } })
  fireEvent.keyDown(input, { key: 'Enter' })
}

async function openNewRun() {
  fireEvent.click(await screen.findByRole('button', { name: 'New run' }))
}

function openTechnicalDetails() {
  fireEvent.click(screen.getByRole('button', { name: 'Diagnostics' }))
}

function openAdvanced() {
  fireEvent.click(screen.getByRole('button', { name: 'Advanced options' }))
}

describe('RunDashboard', () => {
  beforeEach(() => {
    window.location.search = ''
    window.location.hash = ''
    vi.resetAllMocks()
    vi.mocked(api.getRestartOptions).mockResolvedValue(null as never)
    vi.mocked(api.checkSession).mockResolvedValue({ authenticated: true })
    vi.mocked(api.listControlPlaneProjects).mockResolvedValue([project])
    vi.mocked(api.getControlPlaneReadiness).mockResolvedValue({ ready: true, projects: ['control-project'] })
    vi.mocked(api.getControlPlaneCapabilities).mockResolvedValue(capabilities)
    vi.mocked(api.getGlobalConfig).mockResolvedValue(committedConfig)
    vi.mocked(api.postGlobalConfigForm).mockResolvedValue(emptyProjection)
    vi.mocked(api.listControlPlanePlans).mockResolvedValue([{ path: 'plans/in-progress/demo.md', status: 'in_progress', modified_at: '2024-01-01T00:00:00Z', schema_version: 1 }])
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [ownedRun], next_cursor: null, schema_version: 1 })
    vi.mocked(api.getControlPlaneRun).mockResolvedValue(ownedRun)
    vi.mocked(api.listRunEvents).mockResolvedValue([{ sequence: 1, event_type: 'run_started', data: {}, schema_version: 1, timestamp: '2024-01-01T00:00:00Z' }])
    vi.mocked(api.getRunContext).mockResolvedValue({ run_id: 'run-owned', level: 'lite', data: { status: 'running' }, schema_version: 1 })
    vi.mocked(api.subscribeToRunEvents).mockReturnValue(() => {})
  })

  it('suspends hidden streams and refreshes on visibility restoration', async () => {
    const close = vi.fn()
    vi.mocked(api.subscribeToRunEvents).mockReturnValue(close)
    const props = { projectId: project.project_id, initialPlanPath: null, onInitialPlanHandled: vi.fn() }
    const view = render(<RunDashboard {...props} visible />)
    await waitFor(() => expect(api.subscribeToRunEvents).toHaveBeenCalledTimes(1))
    view.rerender(<RunDashboard {...props} visible={false} />)
    expect(close).toHaveBeenCalledTimes(1)
    const count = vi.mocked(api.listControlPlaneRuns).mock.calls.length
    await new Promise(resolve => setTimeout(resolve, 10))
    expect(api.listControlPlaneRuns).toHaveBeenCalledTimes(count)
    view.rerender(<RunDashboard {...props} visible />)
    await waitFor(() => expect(api.subscribeToRunEvents).toHaveBeenCalledTimes(2))
    expect(api.listControlPlaneRuns).toHaveBeenCalledTimes(count + 1)
  })

  it('resolves a promoted-plan handoff against fresh plans and keeps it after a failed refresh', async () => {
    const handled = vi.fn()
    const props = { projectId: project.project_id, initialPlanPath: null as string | null, onInitialPlanHandled: handled }
    const view = render(<RunDashboard {...props} visible />)
    await screen.findByRole('button', { name: 'New run' })
    view.rerender(<RunDashboard {...props} visible={false} />)
    vi.mocked(api.listControlPlanePlans).mockRejectedValue(new Error('temporary fetch failure'))
    const path = 'plans/in-progress/newly-promoted.md'
    view.rerender(<RunDashboard {...props} initialPlanPath={path} visible page="new-run" />)
    await screen.findByText(/Refresh to retry the selected plan/)
    expect(handled).not.toHaveBeenCalled()
    vi.mocked(api.listControlPlanePlans).mockResolvedValue([{ path, status: 'in_progress', modified_at: '', schema_version: 1 }])
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    await waitFor(() => expect((screen.getByLabelText('Run plan') as HTMLInputElement).value).toBe(path))
    expect(handled).toHaveBeenCalledTimes(1)
  })

  it('restarts an inactive failed source with the same workflow and editable options without owner-stop', async () => {
    const failed = { ...ownedRun, status: 'failed', launch_phase: 'failed' }
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [failed], next_cursor: null, schema_version: 1 })
    vi.mocked(api.getControlPlaneRun).mockResolvedValue(failed)
    vi.mocked(api.getRestartOptions).mockResolvedValue({ eligible: true, reason: null, requires_stop: false, run_id: failed.run_id, extra_instructions_unavailable: false, options: { plan_path: 'plans/in-progress/demo.md', workflow_name: 'managed', max_turns: 8 } })
    vi.mocked(api.startControlPlaneRun).mockResolvedValue({ result: { run_id: 'successor', created: true, status: 'running', schema_version: 1, manifest_path: null, reason: null, restarted_from_run_id: failed.run_id }, startup_question: null })
    renderDashboard()
    fireEvent.click(await screen.findByRole('button', { name: 'Restart with changes' }))
    expect((await screen.findByLabelText('Run workflow') as HTMLInputElement).value).toBe('Managed')
    openAdvanced()
    fireEvent.change(screen.getByLabelText('Run max turns'), { target: { value: '12' } })
    fireEvent.click(screen.getByRole('button', { name: 'Confirm stop and start successor' }))
    await waitFor(() => expect(api.startControlPlaneRun).toHaveBeenCalledWith(project.project_id, expect.objectContaining({ workflow_name: 'managed', max_turns: 12, restarted_from_run_id: failed.run_id }), expect.any(String)))
    expect(api.ownerStopControlPlaneRun).not.toHaveBeenCalled()
  })

  it.each(['manifest', 'legacy id'] as const)('selects the newest returned run using %s and preserves an explicit selection', async (source) => {
    const oldest = { ...ownedRun, run_id: '20240101t000000z-11111111', evidence: source === 'manifest' ? { manifest_created_at: '2024-01-01T00:00:00Z' } : {} }
    const newest = { ...ownedRun, run_id: '20240102t000000z-22222222', evidence: source === 'manifest' ? { manifest_created_at: '2024-01-02T00:00:00Z' } : {} }
    const later = { ...ownedRun, run_id: '20240103t000000z-33333333', evidence: source === 'manifest' ? { manifest_created_at: '2024-01-03T00:00:00Z' } : {} }
    const byId = new Map([oldest, newest, later].map((run) => [run.run_id, run]))
    vi.mocked(api.listControlPlaneRuns)
      .mockResolvedValueOnce({ runs: [oldest, newest], next_cursor: null, schema_version: 1 })
      .mockResolvedValueOnce({ runs: [oldest, newest, later], next_cursor: null, schema_version: 1 })
    vi.mocked(api.getControlPlaneRun).mockImplementation(async (_projectId, runId) => byId.get(runId)!)
    vi.mocked(api.getRunContext).mockImplementation(async (_projectId, runId) => ({
      run_id: runId, level: 'lite', data: { status: 'running' }, schema_version: 1,
    }))
    renderDashboard()

    await screen.findByRole('button', { name: newest.run_id })
    fireEvent.click(screen.getByRole('button', { name: new RegExp(oldest.run_id) }))
    await screen.findByRole('button', { name: oldest.run_id })
    fireEvent.click(screen.getByRole('button', { name: 'Refresh', exact: true }))
    await waitFor(() => expect(api.listControlPlaneRuns).toHaveBeenCalledTimes(2))
    await screen.findByRole('button', { name: new RegExp(later.run_id) })
    expect(screen.getByRole('button', { name: oldest.run_id })).toBeDefined()
  })

  it('keeps the server snapshot visible through a failed daemon refresh', async () => {
    vi.mocked(api.listControlPlaneRuns).mockResolvedValueOnce({ runs: [ownedRun], next_cursor: null, schema_version: 1 }).mockRejectedValueOnce(new Error('daemon unavailable'))
    renderDashboard()

    await screen.findByRole('button', { name: 'run-owned' })
    openTechnicalDetails()
    await waitFor(() => expect(screen.getByText(/aflow-run-run-owned\.service/)).toBeDefined())
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))

    await waitFor(() => expect(screen.getByText(/Existing run data remains visible/)).toBeDefined())
    expect(screen.getByText(/aflow-run-run-owned\.service/)).toBeDefined()
    expect(screen.getAllByText('Running').length).toBeGreaterThan(0)
  })

  it('refreshes loaded pages and removes records that leave the history filter', async () => {
    let records = Array.from({ length: 130 }, (_, index) => ({ ...ownedRun, run_id: `history-${index}`, plan_path: `plans/history-${index}.md` }))
    vi.mocked(api.listControlPlaneRuns).mockImplementation(async (_project, request) => ({
      runs: request?.cursor ? records.slice(100) : records.slice(0, 100),
      next_cursor: request?.cursor ? null : 'page-two', schema_version: 1,
    }))
    vi.mocked(api.getControlPlaneRun).mockImplementation(async (_project, id) => records.find(run => run.run_id === id)!)
    const view = renderDashboard()
    const count = () => view.container.querySelectorAll('.run-list-item').length
    await waitFor(() => expect(count()).toBe(100))
    await waitFor(() => expect((screen.getByRole('button', { name: 'Load more runs' }) as HTMLButtonElement).disabled).toBe(false))
    fireEvent.click(screen.getByRole('button', { name: 'Load more runs' }))
    await waitFor(() => expect(count()).toBe(130))
    await waitFor(() => expect((screen.getByRole('button', { name: 'Refresh', exact: true }) as HTMLButtonElement).disabled).toBe(false))
    fireEvent.click(screen.getByRole('button', { name: 'Refresh', exact: true }))
    await waitFor(() => expect(api.listControlPlaneRuns).toHaveBeenCalledTimes(5))
    expect(count()).toBe(130)
    records = records.filter(run => run.run_id !== 'history-125')
    await waitFor(() => expect((screen.getByRole('button', { name: 'Refresh', exact: true }) as HTMLButtonElement).disabled).toBe(false))
    fireEvent.click(screen.getByRole('button', { name: 'Refresh', exact: true }))
    await waitFor(() => expect(count()).toBe(129))
    expect(view.container.querySelector('.run-list')?.textContent).not.toContain('history-125.md')
  })

  it('uses renewed acknowledgement after a definitive rejection', async () => {
    vi.mocked(api.changeRunHistory).mockRejectedValueOnce(new ApiError(422, 'Acknowledge active workflow')).mockResolvedValueOnce({ state: 'deleted', revision: 1 })
    renderDashboard()
    fireEvent.click(await screen.findByRole('button', { name: 'More run actions' }))
    fireEvent.click(screen.getByRole('menuitem', { name: 'Delete record…' }))
    vi.mocked(api.getControlPlaneRun).mockResolvedValue({ ...ownedRun, activity: 'active' })
    fireEvent.click(screen.getByRole('button', { name: 'Confirm delete' }))
    fireEvent.click(await screen.findByRole('checkbox', { name: /will not stop the active workflow/ }))
    await screen.findByText(/Acknowledge active workflow/)
    fireEvent.click(screen.getByRole('button', { name: 'Confirm delete' }))
    await screen.findByRole('heading', { name: 'Deleted record' })
    const calls = vi.mocked(api.changeRunHistory).mock.calls
    expect(calls[0][5]).toBe(false)
    expect(calls[1][5]).toBe(true)
    expect(calls[1][4]).not.toBe(calls[0][4])
  })

  it('requires active acknowledgement and keeps the history retry key after response loss', async () => {
    const active = { ...ownedRun, activity: 'active' as const, history_revision: 0 }
    vi.mocked(api.getControlPlaneRun).mockResolvedValue(active)
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [active], next_cursor: null, schema_version: 1 })
    vi.mocked(api.changeRunHistory).mockRejectedValueOnce(new Error('response lost')).mockResolvedValue({ state: 'deleted', revision: 1 })
    renderDashboard()
    fireEvent.click(await screen.findByRole('button', { name: 'More run actions' }))
    fireEvent.click(screen.getByRole('menuitem', { name: 'Delete record…' }))
    const confirm = screen.getByRole('button', { name: 'Confirm delete' }) as HTMLButtonElement
    expect(confirm.disabled).toBe(true)
    fireEvent.click(screen.getByRole('checkbox', { name: /will not stop the active workflow/ }))
    fireEvent.click(confirm)
    await screen.findByText(/response lost/)
    fireEvent.click(screen.getByRole('button', { name: 'Confirm delete' }))
    await screen.findByRole('heading', { name: 'Deleted record' })
    const calls = vi.mocked(api.changeRunHistory).mock.calls
    expect(calls[1]).toEqual(calls[0])
    expect(calls[0].slice(0, 4)).toEqual(['control-project', 'run-owned', 'delete', 0])
    expect(api.ownerStopControlPlaneRun).not.toHaveBeenCalled()
  })

  it('retains opened archived details and restores without workflow controls', async () => {
    vi.mocked(api.changeRunHistory).mockResolvedValueOnce({ state: 'archived', revision: 1 }).mockResolvedValueOnce({ state: 'visible', revision: 2 })
    renderDashboard()
    fireEvent.click(await screen.findByRole('button', { name: 'More run actions' }))
    fireEvent.click(screen.getByRole('menuitem', { name: 'Archive', exact: true }))
    fireEvent.click(await screen.findByRole('button', { name: 'Restore', exact: true }))
    await waitFor(() => expect(api.changeRunHistory).toHaveBeenCalledTimes(2))
    expect(api.changeRunHistory).toHaveBeenLastCalledWith('control-project', 'run-owned', 'restore', 1, expect.any(String), false)
  })

  it('renders the run overview with lineage, skipped steps, checkpoints, outcomes, and reconciliation evidence', async () => {
    const successorRun = { ...ownedRun, run_id: 'run-successor', restarted_from_run_id: 'run-owned', status: 'running' }
    const linkedSourceRun = { ...ownedRun, selected_start_step: 'implement', skipped_steps: ['plan'], restarted_from_run_id: 'run-source' }
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [linkedSourceRun, successorRun], next_cursor: null, schema_version: 1 })
    vi.mocked(api.getControlPlaneRun).mockImplementation(async (_projectId, runId) => runId === 'run-owned' ? linkedSourceRun : successorRun)
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
          manager_decisions: [],
          run_extract: [{ kind: 'manager_decision', number: 4, routing: { action: 'transition' }, semantic_summary: 'implementation finished' }],
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

    await screen.findByRole('button', { name: 'run-owned' })
    openTechnicalDetails()
    await waitFor(() => expect(screen.getByText('successor of run-source')).toBeDefined())
    expect(screen.getByText('source of run-successor')).toBeDefined()
    expect(screen.getByText('implement · plan')).toBeDefined()
    expect(screen.getByText('legacy-step')).toBeDefined()
    await waitFor(() => expect(screen.getByText('Build (2 of 2)')).toBeDefined())
    await waitFor(() => expect(screen.getByText('Decision #4: transition — implementation finished')).toBeDefined())
    expect(screen.getByText('Last finished turn: turn 3 · Implement · completed · exit 0')).toBeDefined()
    expect(screen.getByText('implemented the feature')).toBeDefined()
    expect(screen.getByText(/aflow-run-run-owned\.service · running · not reconciled/)).toBeDefined()
    expect(screen.getByText(/plans\/in-progress\/demo-2\.md/)).toBeDefined()
    expect(screen.getAllByText('Max turns')[0]).toBeDefined()
    expect(screen.getByText(/running for /)).toBeDefined()
    // The progress header names the plan file from canonical run evidence;
    // the demo-2 context fallback above stays under Diagnostics.
    expect(screen.getByRole('heading', { name: 'demo.md' })).toBeDefined()
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

    if (!screen.queryByLabelText('Run plan')) await openNewRun()
    await screen.findByLabelText('Run plan')
    choose('Run plan', 'plans/in-progress/demo.md')
    choose('Run workflow', 'managed')
    await waitFor(() => expect((screen.getByLabelText('Run plan') as HTMLInputElement).value).toBe('plans/in-progress/demo.md'))
    expect(screen.getByRole('button', { name: 'Start run' }).getAttribute('disabled')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))

    await waitFor(() => expect(screen.getByText(/Input needed/)).toBeDefined())
    expect(screen.getByText('No agent started. Answer to continue.')).toBeDefined()
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

    if (!screen.queryByLabelText('Run plan')) await openNewRun()
    await screen.findByLabelText('Run plan')
    choose('Run plan', 'plans/in-progress/demo.md')
    choose('Run workflow', 'managed')
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

    if (!screen.queryByLabelText('Run plan')) await openNewRun()
    await screen.findByLabelText('Run plan')
    choose('Run plan', 'plans/in-progress/demo.md')
    openAdvanced()
    expect((screen.getByLabelText('Run start step') as HTMLSelectElement).disabled).toBe(true)
    choose('Run workflow', 'managed')

    expect(screen.getByRole('option', { name: '1 · Plan' })).toBeDefined()
    expect(screen.getByRole('option', { name: '2 · Implement' })).toBeDefined()
    expect(screen.getByRole('option', { name: '3 · Review' })).toBeDefined()
    // The capability default team is inherited without an explicit selection.
    expect(screen.getByLabelText('Effective choices for this launch').textContent).toContain('Base — workflow default (Base)')
    fireEvent.change(screen.getByLabelText('Run start step'), { target: { value: 'implement' } })
    const skipNotice = screen.getByText(/skips the earlier executable steps:/)
    expect(skipNotice.textContent).toContain('Plan')

    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))
    await waitFor(() => expect(api.startControlPlaneRun).toHaveBeenCalledWith(
      'control-project',
      // The capability default team stays an omission in the request.
      expect.objectContaining({ plan_path: 'plans/in-progress/demo.md', workflow_name: 'managed', start_step: 'implement' }),
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

    if (!screen.queryByLabelText('Run plan')) await openNewRun()
    await screen.findByLabelText('Run plan')
    choose('Run plan', 'plans/in-progress/demo.md')
    choose('Run workflow', 'managed')
    openAdvanced()
    fireEvent.change(screen.getByLabelText('Run extra instructions'), { target: { value: '  Focus on tests.  \n\nKeep runs short. ' } })
    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))
    await waitFor(() => expect(api.startControlPlaneRun).toHaveBeenCalledWith(
      'control-project',
      expect.objectContaining({ extra_instructions: ['Focus on tests.', 'Keep runs short.'] }),
      expect.anything(),
    ))

    await openNewRun()
    fireEvent.change(screen.getByLabelText('Run extra instructions'), {
      target: { value: Array.from({ length: 9 }, (_, index) => `instruction ${index + 1}`).join('\n') },
    })
    await waitFor(() => expect(screen.getByText(/At most 8 instruction lines/)).toBeDefined())
    fireEvent.click(screen.getByRole('button', { name: 'Advanced options' }))
    expect(screen.getByRole('alert').textContent).toContain('Open Advanced options to edit')
    expect(screen.getByRole('button', { name: 'Start run' }).getAttribute('disabled')).not.toBeNull()
    expect(api.startControlPlaneRun).toHaveBeenCalledTimes(1)
  })

  it('reuses a start key after an uncertain failure and replaces it when the start intent changes', async () => {
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [], next_cursor: null, schema_version: 1 })
    vi.mocked(api.startControlPlaneRun).mockRejectedValue(new Error('connection lost'))
    renderDashboard()

    if (!screen.queryByLabelText('Run plan')) await openNewRun()
    await screen.findByLabelText('Run plan')
    choose('Run plan', 'plans/in-progress/demo.md')
    choose('Run workflow', 'managed')
    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))
    await waitFor(() => expect(screen.getByText('connection lost')).toBeDefined())

    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))
    await waitFor(() => expect(api.startControlPlaneRun).toHaveBeenCalledTimes(2))
    const unchangedRetryKey = vi.mocked(api.startControlPlaneRun).mock.calls[0][2]
    expect(vi.mocked(api.startControlPlaneRun).mock.calls[1][2]).toBe(unchangedRetryKey)

    if (!screen.queryByLabelText('Run max turns')) openAdvanced()
    fireEvent.change(screen.getByLabelText('Run max turns'), { target: { value: '9' } })
    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))
    await waitFor(() => expect(api.startControlPlaneRun).toHaveBeenCalledTimes(3))
    expect(vi.mocked(api.startControlPlaneRun).mock.calls[2][2]).not.toBe(unchangedRetryKey)
    expect(vi.mocked(api.startControlPlaneRun).mock.calls[2][1]).toEqual({ plan_path: 'plans/in-progress/demo.md', workflow_name: 'managed', max_turns: 9 })
  })

  it('refreshes a CAS conflict, keeps local control edits, and reports restart-required changes', async () => {
    vi.mocked(api.controlControlPlaneRun)
      .mockRejectedValueOnce(Object.assign(new Error('revision conflict'), { code: 'revision_conflict' }))
      .mockRejectedValueOnce(Object.assign(new Error('restart required'), { code: 'restart_required' }))
    vi.mocked(api.getControlPlaneRun).mockResolvedValue({ ...ownedRun, revision: 2 })
    renderDashboard()

    await waitFor(() => expect(screen.getByLabelText('Control max turns')).toBeDefined())
    openTechnicalDetails()
    await waitFor(() => expect(screen.getByText('Revision').parentElement?.textContent).toContain('2'))
    expect(screen.queryByText('workflow requires restart; it is not offered as a live control.')).toBeNull()
    fireEvent.change(screen.getByLabelText('Control max turns'), { target: { value: '9' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save run settings' }))
    await waitFor(() => expect(screen.getByText(/Another operator changed this run/)).toBeDefined())
    expect(api.getControlPlaneRun).toHaveBeenCalledWith('control-project', 'run-owned', expect.objectContaining({ signal: expect.any(AbortSignal) }))
    expect((screen.getByLabelText('Control max turns') as HTMLInputElement).value).toBe('9')

    fireEvent.change(screen.getByLabelText('Control max turns'), { target: { value: '10' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save run settings' }))
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

    await waitFor(() => expect(screen.getByLabelText('Selector for Worker')).toBeDefined())
    expect((screen.getByLabelText('Selector for Worker') as HTMLSelectElement).tagName).toBe('SELECT')
    expect(screen.getByRole('option', { name: 'harness/impl-a' })).toBeDefined()
    expect(screen.getByRole('option', { name: 'harness/impl-b' })).toBeDefined()
    expect(screen.getByText(/Changes are saved now and applied between turns/)).toBeDefined()

    await waitFor(() => expect((screen.getByLabelText('Control team') as HTMLSelectElement).value).toBe('base'))
    fireEvent.change(screen.getByLabelText('Control team'), { target: { value: 'full' } })
    fireEvent.change(screen.getByLabelText('Selector for Worker'), { target: { value: 'harness/impl-b' } })
    await waitFor(() => expect((screen.getByLabelText('Control team') as HTMLSelectElement).value).toBe('full'))
    fireEvent.click(screen.getByRole('button', { name: 'Save run settings' }))
    await waitFor(() => expect(api.controlControlPlaneRun).toHaveBeenCalledWith(
      'control-project',
      'run-owned',
      expect.objectContaining({ expected_revision: 1, team: 'full', role_selectors: { worker: 'harness/impl-b' } }),
      expect.stringMatching(/^control-/),
    ))
    await waitFor(() => expect(screen.getByText(/revision 2\. The engine applies them at the next safe boundary/)).toBeDefined())
  })

  it('disambiguates colliding live role controls and sends the exact raw role key', async () => {
    const collidingCapabilities = {
      ...capabilities,
      roles: ['code_review', 'code__review', 'unique_role'],
      admitted_role_selectors: {
        code_review: ['harness/impl-a', 'harness/impl-b'],
        code__review: ['harness/impl-a', 'harness/impl-b'],
        unique_role: ['harness/impl-a'],
      },
    }
    vi.mocked(api.getControlPlaneCapabilities).mockResolvedValue(collidingCapabilities)
    vi.mocked(api.controlControlPlaneRun).mockResolvedValue({
      revision: 2,
      changed: true,
      owner_stop: false,
      run: { ...ownedRun, revision: 2 },
    })
    renderDashboard()

    await screen.findByLabelText('Selector for Code review (code_review)')
    expect(screen.getByLabelText('Selector for Code review (code__review)')).toBeTruthy()
    expect(screen.getByLabelText('Selector for Unique role')).toBeTruthy()
    fireEvent.change(screen.getByLabelText('Selector for Code review (code__review)'), { target: { value: 'harness/impl-b' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save run settings' }))

    await waitFor(() => expect(api.controlControlPlaneRun).toHaveBeenCalledWith(
      'control-project',
      'run-owned',
      expect.objectContaining({ role_selectors: { code__review: 'harness/impl-b' } }),
      expect.stringMatching(/^control-/),
    ))
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

    fireEvent.click(await screen.findByRole('button', { name: 'Restart with changes' }))
    await screen.findByLabelText('Run plan')
    choose('Run plan', 'plans/in-progress/demo.md')
    choose('Run workflow', 'other')
    expect(screen.getByText(/and start successor workflow/)).toBeDefined()
    expect(screen.getAllByText('run-owned').length).toBeGreaterThanOrEqual(2)
    fireEvent.click(screen.getByRole('button', { name: 'Confirm stop and start successor' }))

    await waitFor(() => expect(api.startControlPlaneRun).toHaveBeenCalledWith(
      'control-project',
      expect.objectContaining({ plan_path: 'plans/in-progress/demo.md', workflow_name: 'other', restarted_from_run_id: 'run-owned' }),
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
    }, {
      id: 'control-other', display_name: 'Beta', current_path: '/workspace/beta',
      is_git_root: true, registered_at: '2026-01-01T00:00:00Z', readiness: 'ready',
    }])
    vi.mocked(api.listControlPlaneProjects).mockResolvedValue([project, { ...project, project_id: 'control-other', root: '/workspace/beta' }])
    render(<App />)
    fireEvent.click(await screen.findByRole('button', { name: 'Projects', exact: true }))
    fireEvent.click(await screen.findByText('Alpha'))
    fireEvent.click(screen.getByRole('button', { name: 'Runs' }))


    await openNewRun()
    if (!screen.queryByLabelText('Run plan')) await openNewRun()
    await screen.findByLabelText('Run plan')
    choose('Run plan', 'plans/in-progress/demo.md')
    choose('Run workflow', 'other')
    fireEvent.click(screen.getByRole('button', { name: 'More', exact: true }))
    fireEvent.click(screen.getByRole('menuitem', { name: 'Cancel', exact: true }))
    fireEvent.click(screen.getByRole('button', { name: 'Restart with changes' }))
    fireEvent.click(screen.getByRole('button', { name: 'Confirm stop and start successor' }))

    await screen.findByText(/Successor outcome is unknown/)
    expect(screen.getByLabelText('Run workflow').getAttribute('disabled')).not.toBeNull()
    expect(screen.getByRole('button', { name: 'Start run' }).getAttribute('disabled')).not.toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Runs', exact: true }))
    fireEvent.click(screen.getByRole('button', { name: /run-other Completed/ }))
    await screen.findByRole('button', { name: 'run-other' })
    expect(screen.getByRole('button', { name: 'Retry exact successor request' })).toBeDefined()
    expect(screen.queryByRole('button', { name: 'Start run' })).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Projects' }))
    await screen.findByText(/Its exact request remains preserved/)
    fireEvent.click(await screen.findByText('Beta'))
    fireEvent.click(screen.getByRole('button', { name: 'Runs' }))
    await screen.findByRole('button', { name: 'Retry exact successor request' })
    expect(screen.getByRole('button', { name: 'Retry exact successor request' }).getAttribute('disabled')).not.toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Retry exact successor request' }))
    expect(api.startControlPlaneRun).toHaveBeenCalledTimes(1)
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

    await openNewRun()
    if (!screen.queryByLabelText('Run plan')) await openNewRun()
    await screen.findByLabelText('Run plan')
    choose('Run plan', 'plans/in-progress/demo.md')
    choose('Run workflow', 'other')
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    fireEvent.click(screen.getByRole('button', { name: 'Restart with changes' }))
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

    await screen.findAllByText('Stopped')
    expect(screen.queryByRole('button', { name: /Change workflow/ })).toBeNull()
    rendered.unmount()

    const completed = { ...ownedRun, status: 'completed', launch_phase: 'completed' }
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [completed], next_cursor: null, schema_version: 1 })
    vi.mocked(api.getControlPlaneRun).mockResolvedValue(completed)
    renderDashboard()
    await screen.findAllByText('Completed')
    expect(screen.queryByRole('button', { name: /Change workflow/ })).toBeNull()
    expect(api.ownerStopControlPlaneRun).not.toHaveBeenCalled()
    expect(api.startControlPlaneRun).not.toHaveBeenCalled()
  })

  it('halts restart automation with the draft preserved when the owner stop fails', async () => {
    vi.mocked(api.ownerStopControlPlaneRun).mockRejectedValue(new Error('stop rejected by server'))
    renderDashboard({ restartPollIntervalMs: 1 })

    await openNewRun()
    if (!screen.queryByLabelText('Run plan')) await openNewRun()
    await screen.findByLabelText('Run plan')
    choose('Run plan', 'plans/in-progress/demo.md')
    choose('Run workflow', 'other')
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    fireEvent.click(screen.getByRole('button', { name: 'Restart with changes' }))
    fireEvent.click(screen.getByRole('button', { name: 'Confirm stop and start successor' }))

    await waitFor(() => expect(screen.getByText(/Restart stopped without a confirmed successor: stop rejected by server/)).toBeDefined())
    expect(api.startControlPlaneRun).not.toHaveBeenCalled()
    expect(screen.getByText(/source state may have changed/)).toBeDefined()
    expect((screen.getByLabelText('Run workflow') as HTMLInputElement).value).toBe('Managed')
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.getAllByText('Running').length).toBeGreaterThan(0)
  })

  it('never starts a successor when source inactivity cannot be proven after a disconnect', async () => {
    const stopped = { ...ownedRun, status: 'owner_stopped', launch_phase: 'owner_stopped' }
    vi.mocked(api.ownerStopControlPlaneRun).mockResolvedValue(stopped)
    vi.mocked(api.getControlPlaneRun).mockRejectedValue(new Error('network unreachable'))
    renderDashboard({ restartPollIntervalMs: 1 })

    await openNewRun()
    if (!screen.queryByLabelText('Run plan')) await openNewRun()
    await screen.findByLabelText('Run plan')
    choose('Run plan', 'plans/in-progress/demo.md')
    choose('Run workflow', 'other')
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    fireEvent.click(screen.getByRole('button', { name: 'Restart with changes' }))
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

    await openNewRun()
    if (!screen.queryByLabelText('Run plan')) await openNewRun()
    await screen.findByLabelText('Run plan')
    choose('Run plan', 'plans/in-progress/demo.md')
    choose('Run workflow', 'other')
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    fireEvent.click(screen.getByRole('button', { name: 'Restart with changes' }))
    fireEvent.click(screen.getByRole('button', { name: 'Confirm stop and start successor' }))

    await waitFor(() => expect(screen.getByText(/Source inactivity could not be confirmed/)).toBeDefined())
    expect(vi.mocked(api.getControlPlaneRun).mock.calls.length).toBeGreaterThanOrEqual(31)
    expect(api.startControlPlaneRun).not.toHaveBeenCalled()
  })

  it('refreshes canonical progress and terminal controls from streamed events without a manual refresh', async () => {
    let onEventsHook: ((events: api.RunEvent[]) => void) | null = null
    vi.mocked(api.subscribeToRunEvents).mockImplementation((subscription) => {
      onEventsHook = subscription.onEvents
      return () => {}
    })
    renderDashboard()
    await screen.findByText('Implement · 2')
    await waitFor(() => expect(api.subscribeToRunEvents).toHaveBeenCalled())
    vi.mocked(api.getControlPlaneRun).mockResolvedValue({
      ...ownedRun, status: 'completed', current_step: 'review', turns_completed: 3,
    })
    vi.mocked(api.getRunContext).mockResolvedValue({
      run_id: 'run-owned', level: 'lite', schema_version: 1,
      data: { manager_context: { manager_decisions: [], plan_state: { is_complete: true, checkpoints: [{ index: 1, name: 'Done' }], current_checkpoint: null }, run_extract: [
        { kind: 'manager_decision', number: 1, routing: { action: 'continue' }, semantic_summary: 'checkpoint complete' },
      ] } },
    })
    if (!onEventsHook) throw new Error('subscription hook was not registered')
    onEventsHook!([{ sequence: 2, event_type: 'run_completed', data: {}, schema_version: 1, timestamp: '2024-01-01T00:02:00Z' }])
    await screen.findByText('Review · 3')
    await screen.findByText('Decision #1: continue — checkpoint complete')
    await screen.findByText('All 1 checkpoints complete')
    expect(screen.getAllByText('Completed').length).toBeGreaterThan(0)
    expect(screen.queryByRole('heading', { name: 'Change workflow (guided restart)' })).toBeNull()
  })

  it('coalesces event bursts during a summary request and preserves the last snapshot on failure', async () => {
    let onEventsHook: ((events: api.RunEvent[]) => void) | null = null
    vi.mocked(api.subscribeToRunEvents).mockImplementation((subscription) => {
      onEventsHook = subscription.onEvents
      return () => {}
    })
    renderDashboard()
    await screen.findByText('Implement · 2')
    await waitFor(() => expect(api.subscribeToRunEvents).toHaveBeenCalled())
    let finishRefresh: ((run: Awaited<ReturnType<typeof api.getControlPlaneRun>>) => void) | undefined
    vi.mocked(api.getControlPlaneRun).mockImplementationOnce(() => new Promise((resolve) => { finishRefresh = resolve }))
    const emit = (sequence: number) => {
      if (!onEventsHook) throw new Error('subscription hook was not registered')
      onEventsHook([{ sequence, event_type: 'status_changed', data: {}, schema_version: 1, timestamp: '2024-01-01T00:02:00Z' }])
    }
    emit(2)
    await waitFor(() => expect(finishRefresh).toBeDefined())
    const callsInFlight = vi.mocked(api.getControlPlaneRun).mock.calls.length
    emit(3)
    emit(4)
    expect(vi.mocked(api.getControlPlaneRun).mock.calls.length).toBe(callsInFlight)
    vi.mocked(api.getControlPlaneRun).mockResolvedValue({ ...ownedRun, current_step: 'review', turns_completed: 4 })
    finishRefresh!({ ...ownedRun, turns_completed: 3 })
    await screen.findByText('Review · 4')
    expect(vi.mocked(api.getControlPlaneRun).mock.calls.length).toBe(callsInFlight + 1)
    expect(vi.mocked(api.listRunEvents).mock.calls.length).toBeGreaterThanOrEqual(1)
    vi.mocked(api.getControlPlaneRun).mockRejectedValue(new Error('summary temporarily unavailable'))
    emit(5)
    await screen.findByText('summary temporarily unavailable')
    expect(screen.getByText('Review · 4')).toBeDefined()
    expect(screen.getAllByText('Running').length).toBeGreaterThan(0)
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

    await screen.findByRole('button', { name: 'run-owned' })
    openTechnicalDetails()
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
    expect(await screen.findByText(/Step started/)).toBeDefined()
    expect(screen.getAllByText('Running').length).toBeGreaterThan(0)
  })

  it('requires confirmation for owner stop and explicit resume with separate source and continuation identities', async () => {
    const attentionRun = { ...ownedRun, run_id: 'run-needs-attention', status: 'needs_attention', revision: 3, evidence: { ...ownedRun.evidence, can_resume: true } }
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [attentionRun], next_cursor: null, schema_version: 1 })
    vi.mocked(api.getControlPlaneRun).mockResolvedValue(attentionRun)
    vi.mocked(api.ownerStopControlPlaneRun).mockResolvedValue({ ...attentionRun, status: 'owner_stopped' })
    vi.mocked(api.resumeControlPlaneRun).mockResolvedValue({ run_id: 'run-continuation', created: false, status: 'running', schema_version: 1, manifest_path: null, reason: null, restarted_from_run_id: null })
    const rendered = renderDashboard()

    await waitFor(() => expect(screen.getAllByText('Needs attention').length).toBeGreaterThan(0))
    expect(screen.queryByRole('button', { name: /Owner stop/ })).toBeNull()

    rendered.unmount()
    const onRunSelectionChange = vi.fn()
    renderDashboard({ onRunSelectionChange })
    await waitFor(() => expect(screen.getByRole('button', { name: /Resume as new run/ })).toBeDefined())
    fireEvent.click(screen.getByRole('button', { name: /Resume as new run/ }))
    expect(screen.getByText(/Source run-needs-attention remains visible/)).toBeDefined()
    fireEvent.click(screen.getByRole('button', { name: 'Confirm resume' }))
    await waitFor(() => expect(api.resumeControlPlaneRun).toHaveBeenCalledWith(
      'control-project', 'run-needs-attention', expect.stringMatching(/^resume-/),
    ))
    await waitFor(() => expect(screen.getByText(/Replay returned continuation run-continuation from source run run-needs-attention; no duplicate was created/)).toBeDefined())
    // The URL is updated to the returned continuation run.
    await waitFor(() => expect(onRunSelectionChange).toHaveBeenCalledWith({ runId: 'run-continuation', userInitiated: false }))
  })

  it('reuses a resume key after an uncertain failure', async () => {
    const attentionRun = { ...ownedRun, run_id: 'run-needs-attention', status: 'needs_attention', revision: 3, evidence: { ...ownedRun.evidence, can_resume: true } }
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
    const legacyRun = { ...ownedRun, run_id: 'legacy-run', ownership: 'legacy' as const, status: 'needs_attention', unit_name: null, reason: 'legacy run has no control-plane launch manifest' }
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [legacyRun], next_cursor: null, schema_version: 1 })
    vi.mocked(api.getControlPlaneRun).mockResolvedValue(legacyRun)
    renderDashboard()

    await waitFor(() => expect(screen.getAllByText('Needs attention').length).toBeGreaterThan(0))
    expect(screen.getByText(/Legacy execution record/)).toBeDefined()
    expect(screen.queryByLabelText('Control max turns')).toBeNull()
    expect(screen.queryByRole('button', { name: /Owner stop/ })).toBeNull()
    expect(screen.queryByRole('button', { name: /Resume as new run/ })).toBeNull()
    expect(screen.queryByRole('button', { name: /Change workflow/ })).toBeNull()
  })

  it('keeps New run collapsed behind existing runs and preserves the selected run', async () => {
    renderDashboard()
    await screen.findByRole('button', { name: 'run-owned' })
    const toggle = screen.getByRole('button', { name: 'New run' })
    expect(toggle).toBeDefined()
    expect(screen.queryByLabelText('Run plan')).toBeNull()

    await openNewRun()
    expect(screen.getByLabelText('Run plan')).toBeDefined()
    expect(screen.queryByLabelText('Run details')).toBeNull()
  })

  it('offers New run from the empty history', async () => {
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [], next_cursor: null, schema_version: 1 })
    renderDashboard()
    await screen.findByText('No runs yet')
    expect(screen.queryByLabelText('Run plan')).toBeNull()
    await openNewRun()
    expect(screen.getByLabelText('Run plan')).toBeDefined()
  })

  it('hands off the exact plan path from Run this plan and opens New run', async () => {
    const onInitialPlanHandled = vi.fn()
    renderDashboard({ initialPlanPath: 'plans/in-progress/demo.md', onInitialPlanHandled })
    await screen.findByRole('heading', { name: 'New run' })
    await waitFor(() => expect((screen.getByLabelText('Run plan') as HTMLInputElement).value).toBe('plans/in-progress/demo.md'))
    expect(onInitialPlanHandled).toHaveBeenCalled()
  })

  it('offers only Ready plans for launch and rejects Draft and Done handoffs with promotion guidance', async () => {
    const draft = { path: 'plans/todo/demo.md', status: 'todo', modified_at: '2024-01-01T00:00:00Z', schema_version: 1 }
    const done = { path: 'plans/done/demo.md', status: 'done', modified_at: '2024-01-01T00:00:00Z', schema_version: 1 }
    vi.mocked(api.listControlPlanePlans).mockResolvedValue([
      draft,
      { path: 'plans/in-progress/demo.md', status: 'in_progress', modified_at: '2024-01-01T00:00:00Z', schema_version: 1 },
      done,
    ])
    const admissionView = renderDashboard()

    await openNewRun()
    const planInput = screen.getByLabelText('Run plan')
    fireEvent.focus(planInput)
    // Only the Ready (in progress) plan is selectable; Draft and Done are
    // never offered, so their paths can never be submitted.
    expect(screen.getByRole('option', { name: /plans\/in-progress\/demo\.md/ })).toBeDefined()
    expect(screen.queryByRole('option', { name: /plans\/todo\/demo\.md/ })).toBeNull()
    expect(screen.queryByRole('option', { name: /plans\/done\/demo\.md/ })).toBeNull()
    admissionView.unmount()

    // A Draft handoff is cleared with the promotion guidance, not selected.
    const onDraftHandled = vi.fn()
    const draftRender = renderDashboard({ initialPlanPath: 'plans/todo/demo.md', onInitialPlanHandled: onDraftHandled })
    await screen.findByText(/is still a draft — move it to Ready \(in progress\) in Plans before running it\./)
    expect((screen.getByLabelText('Run plan') as HTMLInputElement).value).toBe('')
    expect(screen.getByRole('button', { name: 'Start run' }).getAttribute('disabled')).not.toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))
    expect(onDraftHandled).toHaveBeenCalled()
    expect(api.startControlPlaneRun).not.toHaveBeenCalled()
    draftRender.unmount()

    // A Done handoff explains the archival lifecycle instead of a launch.
    renderDashboard({ initialPlanPath: 'plans/done/demo.md' })
    await screen.findByText(/is done and kept for the record, so it cannot start a run\./)
    expect((screen.getByLabelText('Run plan') as HTMLInputElement).value).toBe('')
    expect(api.startControlPlaneRun).not.toHaveBeenCalled()
  })

  it('requires a workflow choice when no project default is configured before start or restart', async () => {
    renderDashboard()
    await screen.findByRole('button', { name: 'New run' })
    fireEvent.click(screen.getByRole('button', { name: 'New run' }))
    choose('Run plan', 'plans/in-progress/demo.md')
    expect(screen.getByText(/No default workflow configured — choose an available workflow, or set a global default in Settings/)).toBeDefined()
    expect(screen.getByRole('button', { name: 'Start run' }).getAttribute('disabled')).not.toBeNull()
    expect(screen.queryByRole('button', { name: 'Change workflow: stop and restart…' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))
    expect(api.startControlPlaneRun).not.toHaveBeenCalled()
    expect(api.ownerStopControlPlaneRun).not.toHaveBeenCalled()
    choose('Run workflow', 'other')
    expect(screen.getByRole('button', { name: 'Start run' }).getAttribute('disabled')).toBeNull()
    expect(screen.getByRole('button', { name: 'Start run' }).getAttribute('disabled')).toBeNull()
  })

  it('disables launch while an executable step lacks its exact role mapping', async () => {
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [], next_cursor: null, schema_version: 1 })
    vi.mocked(api.postGlobalConfigForm).mockResolvedValue({
      ...emptyProjection,
      form: {
        ...emptyProjection.form,
        workflows: {
          managed: {
            declared_steps: ['plan', 'implement', 'review'],
            first_step: 'plan',
            executable_steps: ['plan', 'implement', 'review'],
            first_executable_step: 'plan',
            step_roles: null,
          },
        },
      },
    })
    const first = renderDashboard()

    if (!screen.queryByLabelText('Run plan')) await openNewRun()
    await screen.findByLabelText('Run plan')
    choose('Run plan', 'plans/in-progress/demo.md')
    choose('Run workflow', 'managed')
    expect(screen.getByText(/exact role preview is unavailable/).textContent).toContain('Plan, Implement, Review')
    expect(screen.getByRole('button', { name: 'Start run' }).getAttribute('disabled')).not.toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))
    expect(api.startControlPlaneRun).not.toHaveBeenCalled()

    // A partially mapped workflow is equally unlaunchable: the unmapped step
    // is named instead of falling back to any configured role.
    first.unmount()
    vi.mocked(api.postGlobalConfigForm).mockResolvedValue({
      ...emptyProjection,
      form: {
        ...emptyProjection.form,
        workflows: {
          managed: {
            declared_steps: ['plan', 'implement', 'review'],
            first_step: 'plan',
            executable_steps: ['plan', 'implement', 'review'],
            first_executable_step: 'plan',
            step_roles: { plan: 'reviewer', implement: 'worker' },
          },
        },
      },
    })
    renderDashboard()
    if (!screen.queryByLabelText('Run plan')) await openNewRun()
    await screen.findByLabelText('Run plan')
    choose('Run plan', 'plans/in-progress/demo.md')
    choose('Run workflow', 'managed')
    expect(screen.getByText(/exact role preview is unavailable/).textContent).toContain('Review')
    expect(screen.getByRole('button', { name: 'Start run' }).getAttribute('disabled')).not.toBeNull()
    expect(api.startControlPlaneRun).not.toHaveBeenCalled()
  })

  it('applies one max-turns validation to preview and request, so an invalid override never launches', async () => {
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [], next_cursor: null, schema_version: 1 })
    vi.mocked(api.postGlobalConfigForm).mockResolvedValue({
      ...emptyProjection,
      form: { ...emptyProjection.form, default_workflow: 'managed', max_turns: 12 },
    })
    vi.mocked(api.startControlPlaneRun).mockResolvedValue({
      result: { run_id: 'run-started', created: true, status: 'running', schema_version: 1, manifest_path: null, reason: null, restarted_from_run_id: null },
      startup_question: null,
    })
    renderDashboard()

    if (!screen.queryByLabelText('Run plan')) await openNewRun()
    await screen.findByLabelText('Run plan')
    choose('Run plan', 'plans/in-progress/demo.md')

    // Zero and fractional overrides get corrective help and disable Start.
    if (!screen.queryByLabelText('Run max turns')) openAdvanced()
    fireEvent.change(screen.getByLabelText('Run max turns'), { target: { value: '0' } })
    expect(screen.getByText(/Max turns must be a whole number of 1 or greater/)).toBeDefined()
    expect(screen.getByLabelText('Effective choices for this launch').textContent).toContain('invalid override')
    expect(screen.getByRole('button', { name: 'Start run' }).getAttribute('disabled')).not.toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))
    expect(api.startControlPlaneRun).not.toHaveBeenCalled()

    if (!screen.queryByLabelText('Run max turns')) openAdvanced()
    fireEvent.change(screen.getByLabelText('Run max turns'), { target: { value: '1.5' } })
    expect(screen.getByText(/Max turns must be a whole number of 1 or greater/)).toBeDefined()
    expect(screen.getByRole('button', { name: 'Start run' }).getAttribute('disabled')).not.toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))
    expect(api.startControlPlaneRun).not.toHaveBeenCalled()

    // A valid explicit override is both displayed and sent.
    if (!screen.queryByLabelText('Run max turns')) openAdvanced()
    fireEvent.change(screen.getByLabelText('Run max turns'), { target: { value: '7' } })
    await waitFor(() => expect(screen.getByLabelText('Effective choices for this launch').textContent).toContain('7 — your override'))
    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))
    await waitFor(() => expect(api.startControlPlaneRun).toHaveBeenCalledWith(
      'control-project',
      expect.objectContaining({ plan_path: 'plans/in-progress/demo.md', max_turns: 7 }),
      expect.anything(),
    ))

    // Empty keeps the configured default in both preview and request.
    await openNewRun()
    if (!screen.queryByLabelText('Run max turns')) openAdvanced()
    fireEvent.change(screen.getByLabelText('Run max turns'), { target: { value: '' } })
    expect(screen.getByLabelText('Effective choices for this launch').textContent).toContain('12 — global default')
    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))
    await waitFor(() => expect(api.startControlPlaneRun).toHaveBeenCalledTimes(2))
    // An empty field stays an omission: the server applies the defaults.
    expect(vi.mocked(api.startControlPlaneRun).mock.calls[1][1]).toEqual({
      plan_path: 'plans/in-progress/demo.md', max_turns: 12,
    })
  })

  it('resolves launch values with user override, then committed defaults, and labels the sources', async () => {
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [], next_cursor: null, schema_version: 1 })
    vi.mocked(api.postGlobalConfigForm).mockResolvedValue({
      ...emptyProjection,
      form: {
        default_workflow: 'managed',
        max_turns: 12,
        harnesses: {
          codex: { fast: { model: 'glm-4.6', effort: 'high' } },
          zcode: { main: { model: null, effort: null } },
        },
        roles: { reviewer: 'zcode.main' },
        teams: { base: { roles: { worker: 'codex.fast' } } },
        workflow_default_teams: { managed: 'base' },
        workflows: {
          managed: {
            declared_steps: ['plan', 'implement', 'review'],
            first_step: 'plan',
            executable_steps: ['plan', 'implement', 'review'],
            first_executable_step: 'plan',
            step_roles: { plan: 'reviewer', implement: 'worker', review: 'reviewer' },
          },
          other: {
            declared_steps: ['research'],
            first_step: 'research',
            executable_steps: ['research'],
            first_executable_step: 'research',
            step_roles: { research: 'worker' },
          },
        },
      },
    })
    vi.mocked(api.startControlPlaneRun).mockResolvedValue({
      result: { run_id: 'run-started', created: true, status: 'running', schema_version: 1, manifest_path: null, reason: null, restarted_from_run_id: null },
      startup_question: null,
    })
    renderDashboard()

    if (!screen.queryByLabelText('Run plan')) await openNewRun()
    await screen.findByLabelText('Run plan')
    choose('Run plan', 'plans/in-progress/demo.md')

    // Without overrides, the committed defaults resolve and are labeled as defaults.
    const preview = screen.getByLabelText('Effective choices for this launch').textContent ?? ''
    expect(preview).toContain('Managed — global default')
    expect(preview).toContain('12 — global default')
    expect(preview).toContain('Base — workflow default (Base)')

    // Each executable step resolves exactly its own declared role — the other
    // configured roles are never repeated under a step.
    const implementRow = screen.getByRole('row', { name: /^Implement/ })
    expect(implementRow.textContent).toContain('Worker → codex.fast')
    expect(implementRow.textContent).toContain('glm-4.6 · effort high')
    expect(implementRow.textContent).toContain('— team override')
    expect(implementRow.textContent).not.toContain('zcode.main')
    const planRow = screen.getByRole('row', { name: /^Plan/ })
    expect(planRow.textContent).toContain('Reviewer → zcode.main')
    expect(planRow.textContent).toContain('model and effort configured in ZCode — global')
    expect(planRow.textContent).not.toContain('codex.fast')

    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))
    await waitFor(() => expect(api.startControlPlaneRun).toHaveBeenCalledWith(
      'control-project',
      // Following the defaults keeps the request an omission.
      { plan_path: 'plans/in-progress/demo.md', max_turns: 12 },
      expect.stringMatching(/^start-/),
    ))

    // Overrides win over committed values and recompute the exact step roles.
    await openNewRun()
    choose('Run workflow', 'other')
    choose('Run team', 'full')
    if (!screen.queryByLabelText('Run max turns')) openAdvanced()
    fireEvent.change(screen.getByLabelText('Run max turns'), { target: { value: '7' } })
    const overridden = screen.getByLabelText('Effective choices for this launch').textContent ?? ''
    expect(overridden).toContain('Other — your selection')
    expect(overridden).toContain('7 — your override')
    expect(overridden).toContain('Full — your selection')
    // Team full has no role overrides and worker has no global assignment, so
    // the research step's exact role is missing with the settings action named.
    expect(overridden).toContain('Worker — missing')
    expect(overridden).toContain('Settings → Roles')

    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))
    await waitFor(() => expect(api.startControlPlaneRun).toHaveBeenCalledTimes(2))
    expect(api.startControlPlaneRun).toHaveBeenLastCalledWith(
      'control-project',
      { plan_path: 'plans/in-progress/demo.md', workflow_name: 'other', team: 'full', max_turns: 7 },
      expect.stringMatching(/^start-/),
    )
  })

  it('clears a start step the newly chosen workflow does not contain', async () => {
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [], next_cursor: null, schema_version: 1 })
    vi.mocked(api.postGlobalConfigForm).mockResolvedValue({
      ...emptyProjection,
      form: {
        ...emptyProjection.form,
        workflows: {
          managed: {
            declared_steps: ['plan', 'implement', 'review'],
            first_step: 'plan',
            executable_steps: ['plan', 'implement', 'review'],
            first_executable_step: 'plan',
            step_roles: { plan: 'reviewer', implement: 'worker', review: 'reviewer' },
          },
          other: {
            declared_steps: ['research'],
            first_step: 'research',
            executable_steps: ['research'],
            first_executable_step: 'research',
            step_roles: { research: 'worker' },
          },
        },
      },
    })
    vi.mocked(api.startControlPlaneRun).mockResolvedValue({
      result: { run_id: 'run-started', created: true, status: 'running', schema_version: 1, manifest_path: null, reason: null, restarted_from_run_id: null },
      startup_question: null,
    })
    renderDashboard()

    if (!screen.queryByLabelText('Run plan')) await openNewRun()
    await screen.findByLabelText('Run plan')
    choose('Run plan', 'plans/in-progress/demo.md')
    openAdvanced()
    choose('Run workflow', 'managed')
    fireEvent.change(screen.getByLabelText('Run start step'), { target: { value: 'implement' } })
    expect(screen.getByText(/skips the earlier executable steps:/).textContent).toContain('Plan')

    choose('Run workflow', 'other')
    expect((screen.getByLabelText('Run start step') as HTMLSelectElement).selectedIndex).toBe(0)
    expect(screen.queryByText(/skips the earlier executable steps/)).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))
    await waitFor(() => expect(api.startControlPlaneRun).toHaveBeenCalledWith(
      'control-project',
      expect.objectContaining({ plan_path: 'plans/in-progress/demo.md', workflow_name: 'other' }),
      expect.anything(),
    ))
    expect(vi.mocked(api.startControlPlaneRun).mock.calls[0][1].start_step).toBeUndefined()
  })

  it('disables launch with exact settings guidance while the committed configuration is not ready', async () => {
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [], next_cursor: null, schema_version: 1 })
    vi.mocked(api.postGlobalConfigForm).mockResolvedValue({
      ...emptyProjection,
      validation: { ...committedConfig.validation, state: 'configuration_required' as const },
    })
    renderDashboard()

    if (!screen.queryByLabelText('Run plan')) await openNewRun()
    await screen.findByLabelText('Run plan')
    choose('Run plan', 'plans/in-progress/demo.md')
    expect(screen.getByRole('button', { name: 'Start run' }).getAttribute('disabled')).not.toBeNull()
    expect(screen.getByText(/still requires explicit model selectors/)).toBeDefined()
    expect(screen.getByRole('button', { name: 'Open settings' })).toBeDefined()
    expect(api.startControlPlaneRun).not.toHaveBeenCalled()
  })

  it('explains a committed TOML syntax error instead of offering launch', async () => {
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [], next_cursor: null, schema_version: 1 })
    vi.mocked(api.postGlobalConfigForm).mockResolvedValue({
      ...emptyProjection,
      form: null,
      syntax_issues: [{ document: 'workflows.toml', line: 3, message: 'unexpected token' }],
    })
    renderDashboard()

    if (!screen.queryByLabelText('Run plan')) await openNewRun()
    await screen.findByLabelText('Run plan')
    choose('Run plan', 'plans/in-progress/demo.md')
    expect(screen.getByRole('button', { name: 'Start run' }).getAttribute('disabled')).not.toBeNull()
    expect(screen.getByText(/TOML syntax error/)).toBeDefined()
    expect(screen.getByRole('button', { name: 'Open settings' })).toBeDefined()
    expect(api.startControlPlaneRun).not.toHaveBeenCalled()
  })

  it('keeps runs visible and blocks launch when the committed configuration cannot be read', async () => {
    vi.mocked(api.getGlobalConfig).mockRejectedValue(new Error('config unavailable'))
    renderDashboard()
    await screen.findByRole('button', { name: 'run-owned' })
    await openNewRun()
    choose('Run plan', 'plans/in-progress/demo.md')
    expect(screen.getByRole('button', { name: 'Start run' }).getAttribute('disabled')).not.toBeNull()
    expect(screen.getByText(/could not be read \(config unavailable\)/)).toBeDefined()
    expect(screen.queryByLabelText('Run details')).toBeNull()
    expect(api.startControlPlaneRun).not.toHaveBeenCalled()
  })

  it('shows control-plane guidance without other projects when the registered project is unavailable', async () => {
    vi.mocked(api.listControlPlaneProjects).mockResolvedValue([
      { project_id: 'another-project', root: '/workspace/other', schema_version: 1 },
    ])
    renderDashboard()

    await screen.findByRole('alert', { name: 'Project unavailable to the control plane' })
    expect(screen.queryByLabelText('Run plan')).toBeNull()
    expect(screen.queryByRole('button', { name: 'run-owned' })).toBeNull()
    expect(api.listControlPlaneRuns).not.toHaveBeenCalled()
    expect(api.listControlPlanePlans).not.toHaveBeenCalled()
    expect(api.getControlPlaneCapabilities).not.toHaveBeenCalled()
  })

  it('fetches a linked older run through the direct endpoint and inserts it without a substitute', async () => {
    const olderRun = { ...ownedRun, run_id: 'run-older', status: 'completed', current_step: 'review', turns_completed: 6 }
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [ownedRun], next_cursor: null, schema_version: 1 })
    vi.mocked(api.getControlPlaneRun).mockImplementation(async (_projectId, runId) => runId === 'run-older' ? olderRun : ownedRun)
    const onRunSelectionChange = vi.fn()
    renderDashboard({ requestedRunId: 'run-older', onRunSelectionChange })

    await screen.findByRole('button', { name: 'run-older' })
    // The exact linked run was validated through the project-scoped endpoint.
    expect(api.getControlPlaneRun).toHaveBeenCalledWith('control-project', 'run-older', expect.objectContaining({ signal: expect.any(AbortSignal) }))
    // It is inserted into the list without displacing anything, and the
    // newest run was never selected over the link.
    expect(screen.getByRole('button', { name: /run-owned Running/ })).toBeDefined()
    expect(screen.queryByRole('button', { name: 'run-owned' })).toBeNull()
    expect(onRunSelectionChange).not.toHaveBeenCalledWith(expect.objectContaining({ runId: 'run-owned' }))
  })

  it('keeps a selected older run outside the page when its URL request is cleared and the list refreshes', async () => {
    const olderRun = { ...ownedRun, run_id: 'run-older', status: 'completed' }
    vi.mocked(api.getControlPlaneRun).mockImplementation(async (_projectId, runId) => runId === 'run-older' ? olderRun : ownedRun)
    const rendered = renderDashboardNode(dashboardNode({ requestedRunId: 'run-older' }))
    await screen.findByRole('button', { name: 'run-older' })

    rendered.rerender(dashboardNode())
    fireEvent.click(screen.getByRole('button', { name: 'Refresh', exact: true }))
    await waitFor(() => expect(api.listControlPlaneRuns).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Refresh', exact: true })).toHaveProperty('disabled', false))
    expect(screen.getByRole('button', { name: 'run-older' })).toBeDefined()
    expect(screen.queryByRole('button', { name: 'run-owned' })).toBeNull()
  })

  it('follows a changed requested run id by refetching the direct endpoint', async () => {
    const olderRun = { ...ownedRun, run_id: 'run-older', status: 'completed' }
    vi.mocked(api.getControlPlaneRun).mockImplementation(async (_projectId, runId) => runId === 'run-older' ? olderRun : ownedRun)
    const rendered = renderDashboardNode(dashboardNode({ requestedRunId: 'run-owned' }))

    await screen.findByRole('button', { name: 'run-owned' })
    rendered.rerender(dashboardNode({ requestedRunId: 'run-older' }))
    await screen.findByRole('button', { name: 'run-older' })
    expect(api.getControlPlaneRun).toHaveBeenCalledWith('control-project', 'run-older', expect.objectContaining({ signal: expect.any(AbortSignal) }))
  })

  it('keeps the Runs view without a substitute when a linked run is missing', async () => {
    vi.mocked(api.getControlPlaneRun)
      .mockRejectedValueOnce(new ApiError(404, 'run not found', 'run_not_found'))
      .mockResolvedValue(ownedRun)
    const onRunSelectionChange = vi.fn()
    renderDashboard({ requestedRunId: 'run-gone', onRunSelectionChange })

    await screen.findByText(/is not recorded for this project/)
    // No substitute: the linked id is named, the list stays, and nothing is selected.
    expect(screen.queryByRole('heading', { name: /Run run-/ })).toBeNull()
    expect(screen.getByRole('button', { name: /run-owned Running/ })).toBeDefined()
    expect(screen.getByRole('button', { name: 'New run' })).toBeDefined()
    expect(onRunSelectionChange).toHaveBeenCalledWith({ runId: null, userInitiated: false, missingRunId: 'run-gone' })
    expect(api.getControlPlaneRun).toHaveBeenCalledWith('control-project', 'run-gone', expect.objectContaining({ signal: expect.any(AbortSignal) }))

    // An explicit pick clears the stale-link guidance.
    fireEvent.click(screen.getByRole('button', { name: /run-owned Running/ }))
    await screen.findByRole('button', { name: 'run-owned' })
    expect(screen.queryByText(/is not recorded for this project/)).toBeNull()
  })

  it('reports passive and user run selections so the URL can be synced', async () => {
    const otherRun = { ...ownedRun, run_id: 'run-other', status: 'completed' }
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [ownedRun, otherRun], next_cursor: null, schema_version: 1 })
    vi.mocked(api.getControlPlaneRun).mockImplementation(async (_projectId, runId) => runId === 'run-other' ? otherRun : ownedRun)
    const onRunSelectionChange = vi.fn()
    renderDashboard({ onRunSelectionChange })

    // The default newest selection is a passive sync, never a history push.
    await screen.findByRole('button', { name: 'run-owned' })
    await waitFor(() => expect(onRunSelectionChange).toHaveBeenCalledWith({ runId: 'run-owned', userInitiated: false }))
    expect(onRunSelectionChange).not.toHaveBeenCalledWith(expect.objectContaining({ userInitiated: true }))

    fireEvent.click(screen.getByRole('button', { name: /run-other Completed/ }))
    await waitFor(() => expect(onRunSelectionChange).toHaveBeenCalledWith({ runId: 'run-other', userInitiated: true }))
    expect(screen.getByRole('button', { name: 'run-other' })).toBeDefined()
  })

  it('reports the returned run after a launch so the URL can identify it', async () => {
    const startedRun = { ...ownedRun, run_id: 'run-started' }
    vi.mocked(api.listControlPlaneRuns)
      .mockResolvedValueOnce({ runs: [], next_cursor: null, schema_version: 1 })
      .mockResolvedValue({ runs: [startedRun], next_cursor: null, schema_version: 1 })
    vi.mocked(api.getControlPlaneRun).mockResolvedValue(startedRun)
    vi.mocked(api.startControlPlaneRun).mockResolvedValue({
      result: { run_id: 'run-started', created: true, status: 'running', schema_version: 1, manifest_path: null, reason: null, restarted_from_run_id: null },
      startup_question: null,
    })
    const onRunSelectionChange = vi.fn()
    renderDashboard({ onRunSelectionChange })

    if (!screen.queryByLabelText('Run plan')) await openNewRun()
    await screen.findByLabelText('Run plan')
    choose('Run plan', 'plans/in-progress/demo.md')
    choose('Run workflow', 'managed')
    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))

    await waitFor(() => expect(onRunSelectionChange).toHaveBeenCalledWith({ runId: 'run-started', userInitiated: false }))
    await screen.findByRole('button', { name: 'run-started' })
  })

  it('orders current-run progress and the run list before the New run form', async () => {
    const { container } = renderDashboard()
    await screen.findByRole('button', { name: 'run-owned' })

    const progressHeader = container.querySelector('.run-progress-header')
    const runList = container.querySelector('.run-list')
    const newRun = screen.getByRole('button', { name: 'New run' })
    expect(progressHeader).toBeDefined()
    expect(runList).toBeDefined()
    expect(newRun.compareDocumentPosition(runList!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(newRun.compareDocumentPosition(progressHeader!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    // The progress header carries the run's current step and turns.
    expect(screen.getByText('Implement · 2')).toBeDefined()
    expect(screen.queryByText(/stream connected|stream stopped/)).toBeNull()
  })

  it('copies the sanitized dashboard link and reports clipboard failure without hidden data', async () => {
    window.location.search = '?project=wrong&view=settings&extra=query-sentinel'
    window.location.hash = '#fragment-sentinel'
    const writeText = vi.fn()
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    renderDashboard()
    await screen.findByRole('button', { name: 'run-owned' })

    writeText.mockResolvedValueOnce(undefined)
    fireEvent.click(screen.getByRole('button', { name: 'Copy link' }))
    expect(await screen.findByText('Link copied to the clipboard.')).toBeDefined()
    expect(writeText).toHaveBeenCalledTimes(1)
    expect(writeText).toHaveBeenCalledWith(window.location.origin + window.location.pathname + '?project=control-project&view=runs&run=run-owned')

    writeText.mockRejectedValueOnce(new Error('clipboard denied'))
    fireEvent.click(screen.getByRole('button', { name: 'Copy link' }))
    const failure = await screen.findByText(/Clipboard access failed/)
    expect(failure.textContent).not.toContain('http')
    expect(failure.textContent).not.toContain('token')
  })

  it('keeps detailed context under one owner while Refresh updates list, status and events', async () => {
    vi.mocked(api.getRunContext).mockImplementation(async (_project, runId, level) => ({ run_id: runId, level, schema_version: 1, data: { marker: level === 'full' ? 'full-marker' : 'lite-marker' } }))
    renderDashboard()
    await screen.findByRole('button', { name: 'run-owned' })
    openTechnicalDetails()
    fireEvent.click(screen.getByText('Raw details'))
    await screen.findByText(/full-marker/)
    const before = [vi.mocked(api.listControlPlaneRuns).mock.calls.length, vi.mocked(api.getControlPlaneRun).mock.calls.length, vi.mocked(api.listRunEvents).mock.calls.length]
    expect(screen.getAllByRole('button', { name: 'Refresh' })).toHaveLength(1)
    expect(screen.queryByRole('button', { name: /Lite|Full|Refresh status|Refresh debugging/ })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    await waitFor(() => expect(vi.mocked(api.getRunContext).mock.calls.filter(call => call[2] === 'full')).toHaveLength(2))
    await screen.findByRole('button', { name: 'Refresh' })
    expect(screen.getByText(/full-marker/)).toBeTruthy()
    expect(screen.queryByText(/lite-marker/)).toBeNull()
    expect(vi.mocked(api.listControlPlaneRuns).mock.calls.length).toBeGreaterThan(before[0])
    expect(vi.mocked(api.getControlPlaneRun).mock.calls.length).toBeGreaterThan(before[1])
    expect(vi.mocked(api.listRunEvents).mock.calls.length).toBeGreaterThan(before[2])
  })
  it('summarizes a historical startup exit without raw JSON or invented execution timing', async () => {
    const failure = { ...ownedRun, status: 'failed', launch_phase: 'unit_started', started_at: null, evidence: { unit_active: false, has_run_metadata: false, can_resume: false }, worker_exit: { stage: 'startup', reason: null, exit_code: 1, exited_at: '2026-09-08T19:08:16Z', diagnostic_unavailable: true } }
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [failure], next_cursor: null, schema_version: 1 })
    vi.mocked(api.getControlPlaneRun).mockResolvedValue(failure)
    renderDashboard()
    await screen.findByRole('button', { name: 'run-owned' })
    openTechnicalDetails()
    expect(screen.getByText('Worker exited during startup (code 1)')).toBeTruthy()
    expect(screen.getByText('Original worker error was not retained.')).toBeTruthy()
    expect(screen.getByText(/Worker: inactive/)).toBeTruthy()
    expect(screen.getByText('Raw details').parentElement?.hasAttribute('open')).toBe(false)
  })
  it('loads full debugging context straight from Diagnostics without any acknowledgement', async () => {
    renderDashboard()
    await screen.findByRole('button', { name: 'run-owned' })
    // The bounded timeline is visible without its raw payloads.
    expect(screen.queryByText('run started')).toBeNull()
    openTechnicalDetails()
    fireEvent.click(screen.getByText('Raw details'))
    // One click: the intentional Diagnostics action sends full level and the
    // transport compatibility flag directly, with no consent ceremony.
    await waitFor(() => expect(api.getRunContext).toHaveBeenCalledWith('control-project', 'run-owned', 'full', true, expect.objectContaining({ signal: expect.any(AbortSignal) })))
    expect(screen.queryByLabelText(/I understand Full context may expose/)).toBeNull()
    expect(screen.getByRole('button', { name: 'Refresh' })).toBeDefined()
    await waitFor(() => expect(screen.getByText(/"status": "running"/)).toBeDefined())
    // The panel shows the current run identity and avoids refetching per render.
    expect(screen.getByText(/run run-owned/)).toBeDefined()
    const callsAfterOpen = vi.mocked(api.getRunContext).mock.calls.length
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    await waitFor(() => expect(vi.mocked(api.getRunContext).mock.calls.length).toBeGreaterThan(callsAfterOpen))
    expect(vi.mocked(api.getRunContext).mock.calls.at(-1)).toEqual(['control-project', 'run-owned', 'full', true, expect.objectContaining({ signal: expect.any(AbortSignal) })])
  })

  it('falls back to the supported summary when the server lacks full context', async () => {
    vi.mocked(api.getControlPlaneCapabilities).mockResolvedValue({ ...capabilities, context_levels: ['lite'] })
    renderDashboard()
    await screen.findByRole('button', { name: 'run-owned' })
    openTechnicalDetails()
    fireEvent.click(screen.getByText('Raw details'))
    await waitFor(() => expect(api.getRunContext).toHaveBeenCalledWith('control-project', 'run-owned', 'lite', false, expect.objectContaining({ signal: expect.any(AbortSignal) })))
    expect(screen.queryByRole('button', { name: 'Full debugging detail' })).toBeNull()
  })

  it('offers a retry after a failed context load without touching run status', async () => {
    let fullFailed = false
    vi.mocked(api.getRunContext).mockImplementation(async (_project, runId, level) => {
      if (level === 'full' && !fullFailed) {
        fullFailed = true
        throw new Error('context unavailable')
      }
      return { run_id: runId, level, data: { status: 'running' }, schema_version: 1 }
    })
    renderDashboard()
    await screen.findByRole('button', { name: 'run-owned' })
    openTechnicalDetails()
    fireEvent.click(screen.getByText('Raw details'))
    expect(await screen.findByText(/context unavailable/)).toBeDefined()
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    await waitFor(() => expect(screen.getByText(/"status": "running"/)).toBeDefined())
    expect(screen.queryByText(/context unavailable/)).toBeNull()
  })

  it('ignores a stale context response after the selected run changes', async () => {
    const other = { ...ownedRun, run_id: 'run-other', plan_path: 'plans/in-progress/other.md' }
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [ownedRun, other], next_cursor: null, schema_version: 1 })
    vi.mocked(api.getControlPlaneRun).mockImplementation(async (_project, id) => id === other.run_id ? other : ownedRun)
    let releaseOwned: ((value: unknown) => void) | null = null
    vi.mocked(api.getRunContext).mockImplementation(async (_project, runId, level, fullScope) => {
      if (runId === 'run-owned') {
        await new Promise((resolve) => { releaseOwned = resolve })
      }
      return { run_id: runId, level, data: runId === 'run-owned' ? { stale: true } : { status: 'fresh' }, schema_version: 1, ...(level === 'full' ? { fullScope } : {}) }
    })
    renderDashboard()
    await screen.findByRole('button', { name: 'run-owned' })
    openTechnicalDetails()
    fireEvent.click(screen.getByText('Raw details'))
    await waitFor(() => expect(screen.getByText(/run run-owned/)).toBeDefined())
    fireEvent.click(screen.getByRole('button', { name: /other\.md/ }))
    await screen.findByText(/run run-other/)
    await waitFor(() => expect(screen.getByText(/"status": "fresh"/)).toBeDefined())
    releaseOwned?.(null)
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(screen.queryByText(/"stale": true/)).toBeNull()
    expect(screen.getByText(/"status": "fresh"/)).toBeDefined()
  })
  it('keeps an uncertain creation key across Cancel, run selection and New run', async () => {
    const other = { ...ownedRun, run_id: 'run-other', status: 'completed' }
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [ownedRun, other], next_cursor: null, schema_version: 1 })
    vi.mocked(api.getControlPlaneRun).mockImplementation(async (_project, id) => id === other.run_id ? other : ownedRun)
    vi.mocked(api.startControlPlaneRun).mockRejectedValue(new Error('response lost'))
    renderDashboard()
    await openNewRun()
    choose('Run plan', 'plans/in-progress/demo.md')
    choose('Run workflow', 'managed')
    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))
    await screen.findByText('response lost')
    const original = vi.mocked(api.startControlPlaneRun).mock.calls[0]
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    fireEvent.click(screen.getByRole('button', { name: /run-other Completed/ }))
    await screen.findByRole('button', { name: 'run-other', exact: true })
    await openNewRun()
    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))
    await waitFor(() => expect(api.startControlPlaneRun).toHaveBeenCalledTimes(2))
    expect(vi.mocked(api.startControlPlaneRun).mock.calls[1]).toEqual(original)
  })

  it('keeps safe controls available while waiting without a running timer', async () => {
    const waiting = { ...ownedRun, status: 'waiting_for_valid_override' }
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [waiting], next_cursor: null, schema_version: 1 })
    vi.mocked(api.getControlPlaneRun).mockResolvedValue(waiting)
    renderDashboard()
    await screen.findByText('Adjust run')
    expect(screen.getAllByText('Waiting for valid override').length).toBe(2)
    expect(screen.getByRole('button', { name: 'Owner stop…' })).toBeDefined()
    expect(screen.queryByText(/running for/)).toBeNull()
  })

  it('disambiguates colliding live-run team choices without changing the submitted value', async () => {
    vi.mocked(api.getControlPlaneCapabilities).mockResolvedValue({
      ...capabilities,
      teams: ['fast_team', 'fast__team', 'slow_team'],
    })
    renderDashboard()
    const controlTeam = await screen.findByLabelText('Control team') as HTMLSelectElement
    expect(screen.getByRole('option', { name: 'Fast team (fast_team)', exact: true })).toBeTruthy()
    expect(screen.getByRole('option', { name: 'Fast team (fast__team)', exact: true })).toBeTruthy()
    expect(screen.getByRole('option', { name: 'Slow team', exact: true })).toBeTruthy()
    fireEvent.change(controlTeam, { target: { value: 'fast__team' } })
    expect(controlTeam.value).toBe('fast__team')
  })

  it('keeps a failed preparation inactive as time advances and links its durable reason', async () => {
    const failed = { ...ownedRun, status: 'needs_attention', started_at: null,
      current_step: null, turns_completed: null,
      reason: 'Untracked file: fixture.txt',
      evidence: { manifest_created_at: '2024-01-01T00:00:00Z', no_agent_started: true, startup_failure: { stage: 'preparation' } },
    }
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [failed], next_cursor: null, schema_version: 1 })
    vi.mocked(api.getControlPlaneRun).mockResolvedValue(failed)
    renderDashboard()
    await screen.findByText('No agent started.')
    expect(screen.getAllByText('Could not start').length).toBe(2)
    expect(screen.getByText('Untracked file: fixture.txt')).toBeDefined()
    expect(screen.queryByText('Latest progress')).toBeNull()
    vi.useFakeTimers()
    try {
      vi.advanceTimersByTime(3600000)
      expect(screen.queryByText(/running for/)).toBeNull()
    } finally { vi.useRealTimers() }
  })

  it('shows resolved defaults with a Default indicator before focus and a real default option', async () => {
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [], next_cursor: null, schema_version: 1 })
    vi.mocked(api.postGlobalConfigForm).mockResolvedValue({
      ...emptyProjection,
      form: {
        default_workflow: 'managed',
        max_turns: 12,
        harnesses: { codex: { fast: { model: 'glm-4.6', effort: 'high' } } },
        roles: { worker: 'codex.fast' },
        teams: { base: { roles: { worker: 'codex.fast' } }, full: { roles: { worker: 'codex.fast' } } },
        workflow_default_teams: { managed: 'base' },
        workflows: {
          managed: {
            declared_steps: ['plan', 'implement', 'review'],
            first_step: 'plan',
            executable_steps: ['plan', 'implement', 'review'],
            first_executable_step: 'plan',
            step_roles: { plan: 'reviewer', implement: 'worker', review: 'reviewer' },
          },
          other: {
            declared_steps: ['research'],
            first_step: 'research',
            executable_steps: ['research'],
            first_executable_step: 'research',
            step_roles: { research: 'worker' },
          },
        },
      },
    })
    renderDashboard()
    await openNewRun()
    // Before any focus the resolved names are visible with Default indicators.
    expect((screen.getByLabelText('Run workflow') as HTMLInputElement).value).toBe('Managed')
    expect((screen.getByLabelText('Run team') as HTMLInputElement).value).toBe('Base')
    expect(screen.getAllByText('Default').length).toBe(2)
    // The open list offers a real-text default row, never an empty entry.
    const workflowInput = screen.getByLabelText('Run workflow')
    fireEvent.focus(workflowInput)
    expect(screen.getByRole('option', { name: /Use default \(Managed\)/ })).toBeDefined()
    // Escape restores the resolved label instead of leaving a search query.
    fireEvent.change(workflowInput, { target: { value: 'man' } })
    fireEvent.keyDown(workflowInput, { key: 'Escape' })
    expect((screen.getByLabelText('Run workflow') as HTMLInputElement).value).toBe('Managed')
  })

  it('submits explicit selections, omits defaults, and follows the new workflow default team', async () => {
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [], next_cursor: null, schema_version: 1 })
    vi.mocked(api.postGlobalConfigForm).mockResolvedValue({
      ...emptyProjection,
      form: {
        default_workflow: 'managed',
        max_turns: 12,
        harnesses: { codex: { fast: { model: 'glm-4.6', effort: 'high' } } },
        roles: { worker: 'codex.fast' },
        teams: { base: { roles: { worker: 'codex.fast' } }, full: { roles: { worker: 'codex.fast' } } },
        workflow_default_teams: { managed: 'base' },
        workflows: {
          managed: {
            declared_steps: ['plan', 'implement', 'review'],
            first_step: 'plan',
            executable_steps: ['plan', 'implement', 'review'],
            first_executable_step: 'plan',
            step_roles: { plan: 'reviewer', implement: 'worker', review: 'reviewer' },
          },
          other: {
            declared_steps: ['research'],
            first_step: 'research',
            executable_steps: ['research'],
            first_executable_step: 'research',
            step_roles: { research: 'worker' },
          },
        },
      },
    })
    vi.mocked(api.startControlPlaneRun).mockResolvedValue({
      result: { run_id: 'run-started', created: true, status: 'running', schema_version: 1, manifest_path: null, reason: null, restarted_from_run_id: null },
      startup_question: null,
    })
    renderDashboard()
    await openNewRun()
    choose('Run plan', 'plans/in-progress/demo.md')
    // An explicit team selection is submitted verbatim.
    choose('Run team', 'full')
    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))
    await waitFor(() => expect(api.startControlPlaneRun).toHaveBeenCalledTimes(1))
    expect(vi.mocked(api.startControlPlaneRun).mock.calls[0][1]).toEqual({
      plan_path: 'plans/in-progress/demo.md', team: 'full', max_turns: 12,
    })

    // Returning to the default row restores omission in the request.
    await openNewRun()
    choose('Run plan', 'plans/in-progress/demo.md')
    const workflowInput = screen.getByLabelText('Run workflow')
    fireEvent.focus(workflowInput)
    fireEvent.click(screen.getByRole('option', { name: /Use default \(Managed\)/ }))
    const teamInput = screen.getByLabelText('Run team')
    fireEvent.focus(teamInput)
    // Clearing the search query reveals the default row again.
    fireEvent.change(teamInput, { target: { value: '' } })
    fireEvent.click(screen.getByRole('option', { name: /Use default \(Base\)/ }))
    // Changing the workflow to one without a default team updates the resolved team.
    choose('Run workflow', 'other')
    expect((screen.getByLabelText('Run team') as HTMLInputElement).value).toBe('No team — global roles')
    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))
    await waitFor(() => expect(api.startControlPlaneRun).toHaveBeenCalledTimes(2))
    expect(vi.mocked(api.startControlPlaneRun).mock.calls[1][1]).toEqual({
      plan_path: 'plans/in-progress/demo.md', workflow_name: 'other', max_turns: 12,
    })
  })

  it('names a missing default honestly and renders membership with global fallback and the full upgrade chain', async () => {
    renderDashboard()
    await openNewRun()
    const preview = screen.getByLabelText('Effective choices for this launch').textContent ?? ''
    // No configured defaults: the honest statements, not fabricated values.
    expect(preview).toContain('No default workflow configured')
    expect(preview).toContain('no team — global role assignments apply')
    // Choosing the workflow resolves its default team, the membership table
    // with override/fallback sources, and the full multi-stage chain.
    choose('Run workflow', 'managed')
    const resolved = screen.getByLabelText('Effective choices for this launch').textContent ?? ''
    expect(resolved).toContain('Base — workflow default (Base)')
    // The unassigned roles stay honest; the chain keeps its full stage order.
    expect(resolved).toContain('missing — assign it in Settings')
    expect(resolved).toContain('Base — Worker not assigned')
    expect(resolved).toContain('Full — Worker not assigned — no further upgrade configured')
    expect(resolved).toContain('no further upgrade configured')
    expect(resolved).toContain('configured escalation path')
  })

  it('reports a malformed team upgrade chain as an actionable error instead of a truncated chain', async () => {
    vi.mocked(api.getControlPlaneCapabilities).mockRejectedValue(new Error('team upgrade cycle detected at "base"'))
    renderDashboard()
    expect(await screen.findByText(/team upgrade cycle detected/)).toBeDefined()
  })
})
