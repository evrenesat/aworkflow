import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api'
import * as api from '../api'
import type { RunContext, RunProgress, RunProgressDetail, RunProgressSummary, WorktreePreflight } from '../types'
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
    startControlPlaneRun: vi.fn(), answerStartupQuestion: vi.fn(), preflightControlPlaneRun: vi.fn(), controlControlPlaneRun: vi.fn(),
    changeRunHistory: vi.fn(), ownerStopControlPlaneRun: vi.fn(), resumeControlPlaneRun: vi.fn(), createProjectPlanFromRun: vi.fn(), subscribeToRunEvents: vi.fn(),
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

function preflightResult(overrides: Partial<WorktreePreflight> = {}): WorktreePreflight {
  return {
    checkout_path: '/workspace/alpha',
    execution_mode: 'same_checkout',
    dirty: false,
    requires_confirmation: false,
    blockers: [],
    total_items: 0,
    offset: 0,
    limit: 200,
    next_offset: null,
    items: [],
    ...overrides,
  }
}

const ownedRun = {
  run_id: 'run-owned', status: 'running', schema_version: 1, ownership: 'control_plane' as const,
  revision: 1, reason: null, unit_name: 'aflow-run-run-owned.service', launch_phase: 'running',
  workflow_name: 'managed', team: 'base', current_step: 'implement', turns_completed: 2, max_turns: 8,
  selected_start_step: null, skipped_steps: [] as string[], restarted_from_run_id: null as string | null,
  started_at: '2024-01-01T00:00:00Z',
  evidence: { manifest_created_at: '2024-01-01T00:00:00Z', plan_path: 'plans/in-progress/demo.md', worktree_path: '/workspace/alpha', branch: 'feature/run' },
}

function canonicalListProgress(): RunProgressSummary {
  const known = (value: number) => ({ value, coverage: 'complete' as const })
  return {
    schema_version: 1, availability: 'complete', observed_at: '2026-09-11T12:00:00Z', evidence_at: '2026-09-11T11:59:00Z',
    reason_codes: [], original_plan_identity: 'run-owned-plan', original_plan_display_name: 'run-owned.md', original_plan_path: '/workspace/alpha/plans/run-owned.md',
    total_checkpoints: known(11), approved_checkpoints: known(4), recorded_complete_checkpoints: known(4),
    current_checkpoint_id: 'cp-5', current_checkpoint_ordinal: 5, current_checkpoint_title: 'Checkpoint 5: Active',
    activity: 'active', phase: 'implementing', run_status: 'running', current_executor: null, last_executor: null,
    worker_attempts: known(2), repair_passes: known(0), reviews: known(1), runtime_retries: known(0), applied_upgrades: known(0),
  }
}

function canonicalDetailProgress(overrides: Partial<RunProgressDetail> = {}): RunProgressDetail {
  const known = (value: number) => ({ value, coverage: 'complete' as const })
  const worker = {
    role: 'worker', team: 'base', selector: 'codex.worker', harness: 'codex', model: 'gpt-5', model_display: 'GPT-5', effort: 'high',
    source_run_id: 'run-owned', invocation_id: 'invoke-worker-5', turn_number: 5,
    started_at: '2026-09-11T10:00:00Z', ended_at: null, duration_seconds: null,
  }
  const reviewer = {
    role: 'reviewer', team: 'base', selector: 'codex.reviewer', harness: 'codex', model: 'gpt-5', model_display: 'GPT-5', effort: 'high',
    source_run_id: 'run-owned', invocation_id: 'invoke-review-4', turn_number: 4,
    started_at: '2026-09-11T09:50:00Z', ended_at: '2026-09-11T09:55:00Z', duration_seconds: 300,
  }
  return {
    ...canonicalListProgress(),
    current_executor: worker,
    last_executor: reviewer,
    checkpoints: [
      {
        checkpoint_id: 'cp-4', ordinal: 4, title: 'Checkpoint 4: Reviewed', status: 'approved',
        worker_attempts: known(1), repair_passes: known(1), reviews: known(2), runtime_retries: known(0), applied_upgrades: known(0),
        recorded_at: '2026-09-11T09:55:00Z', duration_seconds: 900, scope_id: 'scope-4', generation_id: 'generation-4', parent_checkpoint_id: null, source_run_id: 'run-owned',
      },
      {
        checkpoint_id: 'cp-5', ordinal: 5, title: 'Checkpoint 5: Active', status: 'implementing',
        worker_attempts: known(1), repair_passes: known(0), reviews: known(0), runtime_retries: known(0), applied_upgrades: known(0),
        recorded_at: null, duration_seconds: null, scope_id: 'scope-5', generation_id: 'generation-5', parent_checkpoint_id: 'cp-4', source_run_id: 'run-owned',
      },
      {
        checkpoint_id: 'cp-6', ordinal: 6, title: 'Checkpoint 6: Pending', status: 'pending',
        worker_attempts: known(0), repair_passes: known(0), reviews: known(0), runtime_retries: known(0), applied_upgrades: known(0),
        recorded_at: null, duration_seconds: null, scope_id: 'scope-6', generation_id: 'generation-6', parent_checkpoint_id: null, source_run_id: 'run-owned',
      },
    ],
    events: [
      {
        event_id: 'attempt-4', checkpoint_id: 'cp-4', scope_id: 'scope-4', source_run_id: 'run-owned', turn_number: 2, decision_number: null,
        kind: 'worker_attempt', outcome: 'completed', executor: reviewer, started_at: '2026-09-11T09:30:00Z', ended_at: '2026-09-11T09:40:00Z', duration_seconds: 600,
        reason: 'implemented the checkpoint', source_reference: { run_id: 'run-owned', turn_number: 2 },
      },
      {
        event_id: 'review-4', checkpoint_id: 'cp-4', scope_id: 'scope-4', source_run_id: 'run-owned', turn_number: 3, decision_number: 1,
        kind: 'review_rejection', outcome: 'rejected', executor: reviewer, started_at: '2026-09-11T09:41:00Z', ended_at: '2026-09-11T09:42:00Z', duration_seconds: 60,
        reason: 'review requested a repair', source_reference: { run_id: 'run-owned', decision_number: 1 },
      },
      {
        event_id: 'repair-4', checkpoint_id: 'cp-4', scope_id: 'scope-4', source_run_id: 'run-owned', turn_number: 4, decision_number: null,
        kind: 'repair_attempt', outcome: 'completed', executor: worker, started_at: '2026-09-11T09:43:00Z', ended_at: '2026-09-11T09:50:00Z', duration_seconds: 420,
        reason: 'addressed review rejection', source_reference: { run_id: 'run-owned', turn_number: 4 },
      },
      {
        event_id: 'attempt-5', checkpoint_id: 'cp-5', scope_id: 'scope-5', source_run_id: 'run-owned', turn_number: 5, decision_number: null,
        kind: 'worker_attempt', outcome: 'running', executor: worker, started_at: '2026-09-11T10:00:00Z', ended_at: null, duration_seconds: null,
        reason: null, source_reference: { run_id: 'run-owned', turn_number: 5 },
      },
    ],
    applied_changes: [{
      change_id: 'upgrade-4', status: 'applied', kind: 'worker_upgrade', roles: ['worker'], old_team: 'base', new_team: 'full', old_selector: 'codex.worker', new_selector: 'codex.repair',
      old_model: 'gpt-5', new_model: 'gpt-5-repair', old_effort: 'medium', new_effort: 'high', turn_number: 4, checkpoint_id: 'cp-4', reason: 'repair team applied', recorded_at: '2026-09-11T09:43:00Z', source_reference: { run_id: 'run-owned' },
    }],
    pending_changes: [{
      change_id: 'pending-5', status: 'pending', kind: 'team_change', roles: ['reviewer'], old_team: 'base', new_team: 'full', old_selector: null, new_selector: 'codex.reviewer', old_model: null, new_model: null, old_effort: null, new_effort: null,
      turn_number: null, checkpoint_id: 'cp-5', reason: 'applies on next safe turn', recorded_at: null, source_reference: { run_id: 'run-owned' },
    }],
    delivery: [
      { stage: 'final_review', status: 'succeeded', recorded_at: '2026-09-11T09:55:00Z', reason: null, source_reference: null },
      { stage: 'ci', status: 'unknown', recorded_at: null, reason: 'No CI receipt', source_reference: null },
    ],
    truncation: { evidence_bytes: 100, records_read: 12, checkpoints_read: 3, events_read: 4, omitted_records: 0, omitted_checkpoints: 0, notices: [] },
    ...overrides,
  }
}

function observerProgress(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    availability: 'available',
    checkpoint: { index: 4, name: 'Checkpoint 4: Repair' },
    total: 14,
    complete: false,
    repairing: false,
    overlay_path: null,
    reason: null,
    last_finished_turn: null,
    current_turn: null,
    ...overrides,
  }
}

function compatibilityProgress(overrides: Partial<RunProgress> = {}): RunProgress {
  return {
    availability: 'available',
    checkpoint: { index: 4, name: 'Checkpoint 4: Repair' },
    total: 14,
    complete: false,
    repairing: false,
    overlay_path: null,
    reason: null,
    last_finished_turn: null,
    current_turn: null,
    ...overrides,
  }
}

function progressContext(progress: Record<string, unknown>, managerContext?: Record<string, unknown>) {
  return {
    run_id: 'run-owned',
    level: 'lite' as const,
    data: { progress, ...(managerContext ? { manager_context: managerContext } : {}) },
    schema_version: 1,
  }
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
  onOpenPlan?: (planPath: string) => void
} = {}) {
  return render(
    <RunDashboard
      projectId="control-project"
      requestedRunId={options.requestedRunId ?? null}
      onRunSelectionChange={options.onRunSelectionChange}
      initialPlanPath={options.initialPlanPath ?? null}
      onInitialPlanHandled={options.onInitialPlanHandled ?? vi.fn()}
      onOpenPlan={options.onOpenPlan}
      restartPollIntervalMs={options.restartPollIntervalMs}
      onOpenSettings={vi.fn()}
    />,
  )
}

function renderDashboardNode(node: React.ReactElement) {
  return render(node)
}

function dashboardNode(options: { requestedRunId?: string | null; visible?: boolean } = {}) {
  return (
    <RunDashboard
      projectId="control-project"
      requestedRunId={options.requestedRunId ?? null}
      visible={options.visible ?? true}
      initialPlanPath={null}
      onInitialPlanHandled={vi.fn()}
      onOpenPlan={vi.fn()}
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
  await screen.findByLabelText('Run plan')
}

async function waitForPreflightReady(expectedRequest: Record<string, unknown> = {}) {
  await waitFor(() => {
    expect(screen.getByText('No uncommitted changes detected.')).toBeDefined()
    const successorButton = screen.queryByRole('button', { name: 'Confirm stop and start successor' })
    const actionButton = successorButton ?? screen.getByRole('button', { name: 'Start run' })
    expect(actionButton.getAttribute('disabled')).toBeNull()
    if (Object.keys(expectedRequest).length > 0) {
      expect(api.preflightControlPlaneRun).toHaveBeenLastCalledWith(
        'control-project',
        expect.objectContaining(expectedRequest),
        expect.objectContaining({ offset: 0, limit: 200, signal: expect.any(AbortSignal) }),
      )
    }
  })
}

async function waitForControlAdmission(selectorLabel?: string) {
  await waitFor(() => {
    const team = screen.getByLabelText('Control team') as HTMLSelectElement
    expect(team.value).toBe('base')
    expect(team.disabled).toBe(false)
    if (selectorLabel) {
      expect((screen.getByLabelText(selectorLabel) as HTMLSelectElement).disabled).toBe(false)
    }
  })
}

function openTechnicalDetails() {
  fireEvent.click(screen.getByRole('button', { name: 'Diagnostics' }))
}

function openAdvanced() {
  fireEvent.click(screen.getByRole('button', { name: 'Advanced options' }))
}

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

type ResumeComparisonSetup = 'accepted-baseline' | 'pending'
type ResumeComparisonTiming = 'before-detail-settlement' | 'after-detail-settlement'

function installResumeComparisonSetup(setup: ResumeComparisonSetup) {
  const locationBefore = window.location.href
  const clipboardBefore = Object.getOwnPropertyDescriptor(navigator, 'clipboard')
  const writeText = vi.fn()
  window.history.replaceState(null, '', `${window.location.pathname}?project=wrong&view=settings&extra=query-sentinel#fragment-sentinel`)
  if (setup === 'accepted-baseline') {
    window.location.search = ''
    window.location.hash = ''
  } else {
    window.history.replaceState(null, '', window.location.pathname)
  }
  Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
  return () => {
    window.history.replaceState(null, '', locationBefore)
    if (clipboardBefore) Object.defineProperty(navigator, 'clipboard', clipboardBefore)
    else Reflect.deleteProperty(navigator, 'clipboard')
  }
}

describe('RunDashboard', () => {
  let locationBeforeTest = ''
  let clipboardDescriptorBeforeTest: PropertyDescriptor | undefined

  beforeEach(() => {
    locationBeforeTest = window.location.href
    clipboardDescriptorBeforeTest = Object.getOwnPropertyDescriptor(navigator, 'clipboard')
    window.history.replaceState(null, '', window.location.pathname)
    vi.resetAllMocks()
    vi.mocked(api.getRestartOptions).mockResolvedValue(null as never)
    vi.mocked(api.checkSession).mockResolvedValue({ authenticated: true })
    vi.mocked(api.listControlPlaneProjects).mockResolvedValue([project])
    vi.mocked(api.getControlPlaneReadiness).mockResolvedValue({ ready: true, projects: ['control-project'] })
    vi.mocked(api.getControlPlaneCapabilities).mockResolvedValue(capabilities)
    vi.mocked(api.getGlobalConfig).mockResolvedValue(committedConfig)
    vi.mocked(api.postGlobalConfigForm).mockResolvedValue(emptyProjection)
    vi.mocked(api.listControlPlanePlans).mockResolvedValue([{ path: 'plans/in-progress/demo.md', status: 'in_progress', modified_at: '2024-01-01T00:00:00Z', schema_version: 1 }])
    vi.mocked(api.preflightControlPlaneRun).mockResolvedValue({
      checkout_path: '/workspace/alpha', execution_mode: 'same_checkout', dirty: false,
      requires_confirmation: false, blockers: [], total_items: 0, offset: 0, limit: 200,
      next_offset: null, items: [],
    })
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [ownedRun], next_cursor: null, schema_version: 1 })
    vi.mocked(api.getControlPlaneRun).mockResolvedValue(ownedRun)
    vi.mocked(api.listRunEvents).mockResolvedValue([{ sequence: 1, event_type: 'run_started', data: {}, schema_version: 1, timestamp: '2024-01-01T00:00:00Z' }])
    vi.mocked(api.getRunContext).mockResolvedValue({ run_id: 'run-owned', level: 'lite', data: { status: 'running' }, schema_version: 1 })
    vi.mocked(api.subscribeToRunEvents).mockReturnValue(() => {})
  })

  afterEach(() => {
    window.history.replaceState(null, '', locationBeforeTest)
    if (clipboardDescriptorBeforeTest) Object.defineProperty(navigator, 'clipboard', clipboardDescriptorBeforeTest)
    else Reflect.deleteProperty(navigator, 'clipboard')
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

  it('refreshes saved configuration options without losing the selected run or control draft', async () => {
    const initialRuns = deferred<{ runs: typeof ownedRun[]; next_cursor: null; schema_version: number }>()
    const initialCapabilities = deferred<typeof capabilities>()
    const initialRunDetail = deferred<typeof ownedRun>()
    vi.mocked(api.listControlPlaneRuns).mockImplementationOnce(() => initialRuns.promise)
    vi.mocked(api.getControlPlaneCapabilities).mockImplementationOnce(() => initialCapabilities.promise)
    vi.mocked(api.getControlPlaneRun).mockImplementationOnce(() => initialRunDetail.promise)
    const view = renderDashboardNode(dashboardNode())

    await waitFor(() => {
      expect(api.listControlPlaneRuns).toHaveBeenCalled()
      expect(api.getControlPlaneCapabilities).toHaveBeenCalled()
    })
    expect(screen.queryByLabelText('Control max turns')).toBeNull()

    await act(async () => {
      initialRuns.resolve({ runs: [ownedRun], next_cursor: null, schema_version: 1 })
      await initialRuns.promise
    })
    expect(screen.queryByLabelText('Control max turns')).toBeNull()

    await act(async () => {
      initialCapabilities.resolve(capabilities)
      await initialCapabilities.promise
    })
    await waitFor(() => {
      expect(api.getControlPlaneRun).toHaveBeenCalledWith(
        'control-project',
        'run-owned',
        expect.objectContaining({ signal: expect.any(AbortSignal) }),
      )
      const maxTurns = screen.getByLabelText('Control max turns') as HTMLInputElement
      expect(maxTurns.disabled).toBe(false)
      expect(maxTurns.value).toBe('8')
      expect(screen.getByRole('button', { name: 'run-owned' })).toBeDefined()
    })

    await act(async () => {
      initialRunDetail.resolve(ownedRun)
      await initialRunDetail.promise
    })
    expect((screen.getByLabelText('Control max turns') as HTMLInputElement).value).toBe('8')
    fireEvent.change(screen.getByLabelText('Control max turns'), { target: { value: '17' } })
    vi.mocked(api.getControlPlaneCapabilities).mockResolvedValue({
      ...capabilities,
      teams: [...capabilities.teams, 'saved-team'],
      admitted_role_selectors: { worker: [...capabilities.admitted_role_selectors.worker, 'harness/saved'] },
    })

    view.rerender(dashboardNode({ visible: false }))
    view.rerender(dashboardNode({ visible: true }))
    await screen.findByRole('option', { name: 'saved-team' })
    await screen.findByRole('option', { name: 'harness/saved' })
    expect((screen.getByLabelText('Control max turns') as HTMLInputElement).value).toBe('17')
    expect(screen.getByRole('button', { name: 'run-owned' })).toBeDefined()
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
    await waitForPreflightReady()
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

  it('keeps destructive history unavailable while restore is pending', async () => {
    const archived = { ...ownedRun, history_state: 'archived' as const, history_revision: 1 }
    const restoreReady = deferred<{ state: 'visible'; revision: number }>()
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [archived], next_cursor: null, schema_version: 1 })
    vi.mocked(api.getControlPlaneRun).mockResolvedValue(archived)
    vi.mocked(api.changeRunHistory).mockImplementation(async (_project, _run, action) => {
      if (action === 'restore') return restoreReady.promise
      return { state: 'deleted', revision: 3 }
    })
    renderDashboard()

    const restore = await screen.findByRole('button', { name: 'Restore', exact: true })
    fireEvent.click(restore)
    await waitFor(() => expect(api.changeRunHistory).toHaveBeenCalledWith(
      'control-project', 'run-owned', 'restore', 1, expect.any(String), false,
    ))
    expect((restore as HTMLButtonElement).disabled).toBe(true)

    fireEvent.click(screen.getByRole('button', { name: 'More run actions' }))
    const deleteItem = screen.getByRole('menuitem', { name: 'Delete record…' })
    expect((deleteItem as HTMLButtonElement).disabled).toBe(true)

    await act(async () => {
      restoreReady.resolve({ state: 'visible', revision: 2 })
      await restoreReady.promise
    })
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Restore', exact: true })).toBeNull())
    expect((deleteItem as HTMLButtonElement).disabled).toBe(false)
    fireEvent.click(deleteItem)
    const confirm = screen.getByRole('button', { name: 'Confirm delete' })
    expect((confirm as HTMLButtonElement).disabled).toBe(false)
    fireEvent.click(confirm)
    await screen.findByRole('heading', { name: 'Deleted record' })
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
    expect(screen.getByRole('heading', { name: 'Demo' })).toBeDefined()
  })

  it('renders verified repair progress and separates current from last finished turns', async () => {
    vi.mocked(api.getRunContext).mockResolvedValue(progressContext(observerProgress({
      repairing: true,
      overlay_path: '/workspace/alpha/execution/repair-overlay.md',
      current_turn: { turn_number: 4, step: 'implement', status: 'starting', summary: null },
      last_finished_turn: { turn_number: 3, step: 'review', status: 'completed', summary: 'exit 0: review approved' },
    })))

    renderDashboard()

    expect(await screen.findByText('Checkpoint 4: Repair (4 of 14)')).toBeDefined()
    expect(screen.getByText('Repairing')).toBeDefined()
    expect(screen.getByText(/repair-overlay\.md/)).toBeDefined()
    expect(screen.getByText('Current turn: turn 4 · Implement · starting')).toBeDefined()
    expect(screen.getByText('Last finished turn: turn 3 · Review · completed')).toBeDefined()
    expect(screen.getByText('Last finished summary: exit 0: review approved')).toBeDefined()
    expect(screen.queryByText(/\? of 0/)).toBeNull()
    expect(screen.queryByText(/All 0 checkpoints complete/)).toBeNull()
  })

  it('keeps the bounded execution summary beside canonical history', async () => {
    vi.mocked(api.getRunContext).mockResolvedValue({
      run_id: 'run-owned',
      level: 'lite',
      schema_version: 1,
      data: {
        progress: canonicalDetailProgress(),
        execution_progress: compatibilityProgress({
          checkpoint: null,
          total: 4,
          complete: true,
          current_turn: { turn_number: 4, step: 'implement', status: 'starting', summary: null },
          last_finished_turn: { turn_number: 3, step: 'review', status: 'completed', summary: 'exit 0: recovery approved' },
        }),
      },
    })

    renderDashboard()

    expect(await screen.findByRole('button', { name: /Checkpoint 5: Active/ })).toBeDefined()
    expect((await screen.findAllByText('4 / 11 approved')).length).toBeGreaterThan(0)
    expect(screen.getByText('All 4 checkpoints complete')).toBeDefined()
    expect(screen.getByText('Current turn: turn 4 · Implement · starting')).toBeDefined()
    expect(screen.getByText('Last finished turn: turn 3 · Review · completed')).toBeDefined()
    expect(screen.getByText('Last finished summary: exit 0: recovery approved')).toBeDefined()
  })

  it('does not reinterpret canonical-only progress as the legacy summary', async () => {
    vi.mocked(api.getRunContext).mockResolvedValue({
      run_id: 'run-owned',
      level: 'lite',
      schema_version: 1,
      data: { progress: canonicalDetailProgress() },
    })

    renderDashboard()

    expect(await screen.findByRole('button', { name: /Checkpoint 5: Active/ })).toBeDefined()
    expect(screen.queryByText(/Progress available — current checkpoint not reported/)).toBeNull()
    expect(screen.queryByText(/All \d+ checkpoints complete/)).toBeNull()
  })

  it('renders a known partial scope without inventing a denominator', async () => {
    vi.mocked(api.getRunContext).mockResolvedValue(progressContext(observerProgress({
      availability: 'partial',
      total: null,
      complete: null,
      checkpoint: { index: 4, name: 'Checkpoint 4: Scope only' },
      reason: 'missing_plan',
    })))

    renderDashboard()

    expect(await screen.findByText('Checkpoint 4: Scope only (4)')).toBeDefined()
    expect(screen.queryByText(/Checkpoint 4: Scope only \(4 of/)).toBeNull()
    expect(screen.queryByText(/\? of 0/)).toBeNull()
  })

  it('renders unavailable evidence and refuses empty legacy completion', async () => {
    vi.mocked(api.getRunContext).mockResolvedValue(progressContext(observerProgress({
      availability: 'unavailable',
      checkpoint: null,
      total: 0,
      complete: true,
      reason: 'non_checkpoint_plan',
    })))

    renderDashboard()

    expect(await screen.findByText('Progress unavailable — the source is not a checkpoint plan.')).toBeDefined()
    expect(screen.queryByText(/All 0 checkpoints complete/)).toBeNull()
    expect(screen.queryByText(/\? of 0/)).toBeNull()
  })

  it('keeps a legacy controller scope partial when its checkpoint list is empty', async () => {
    vi.mocked(api.getRunContext).mockResolvedValue({
      run_id: 'run-owned',
      level: 'lite',
      data: {
        manager_context: {
          plan_state: { checkpoints: [], is_complete: true, current_checkpoint: null },
          controller_state: {
            active_implementation_scope: {
              scope_id: 'original::checkpoint-4',
              checkpoint_index: 4,
              checkpoint_name: 'Checkpoint 4: Legacy scope',
            },
          },
        },
      },
      schema_version: 1,
    })

    renderDashboard()

    expect(await screen.findByText('Checkpoint 4: Legacy scope (4)')).toBeDefined()
    expect(screen.queryByText(/All 0 checkpoints complete/)).toBeNull()
  })

  it('advances from a repairing scope to the next checkpoint after refresh', async () => {
    let emit: ((events: api.RunEvent[]) => void) | null = null
    vi.mocked(api.subscribeToRunEvents).mockImplementation((subscription) => {
      emit = subscription.onEvents
      return () => {}
    })
    const repairing = progressContext(observerProgress({
      repairing: true,
      overlay_path: '/workspace/alpha/execution/repair-overlay.md',
    }))
    const next = progressContext(observerProgress({
      checkpoint: { index: 5, name: 'Checkpoint 5: Next' },
    }))
    vi.mocked(api.getRunContext).mockResolvedValueOnce(repairing).mockResolvedValue(next)

    renderDashboard()

    expect(await screen.findByText('Checkpoint 4: Repair (4 of 14)')).toBeDefined()
    expect(screen.getByText('Repairing')).toBeDefined()
    if (!emit) throw new Error('event subscription was not registered')
    await act(async () => {
      emit!([{ sequence: 2, event_type: 'scope_closed', data: {}, schema_version: 1, timestamp: '2024-01-01T00:02:00Z' }])
      await new Promise((resolve) => setTimeout(resolve, 150))
    })

    expect(await screen.findByText('Checkpoint 5: Next (5 of 14)')).toBeDefined()
    expect(screen.queryByText('Repairing')).toBeNull()
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

    await openNewRun()
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

    await openNewRun()
    await screen.findByLabelText('Run plan')
    choose('Run plan', 'plans/in-progress/demo.md')
    choose('Run workflow', 'managed')
    await waitForPreflightReady()
    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))
    await screen.findByText('Worktree is dirty. Start anyway?')

    expect(screen.queryByRole('button', { name: 'Confirm and continue' })).toBeNull()
    const confirmation = screen.getByRole('checkbox', { name: 'Continue despite uncommitted changes' }) as HTMLInputElement
    expect(confirmation.checked).toBe(false)
    fireEvent.click(confirmation)
    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))
    await waitFor(() => expect(api.answerStartupQuestion).toHaveBeenCalledWith(
      'control-project', 'question-2', true, expect.stringMatching(/^startup-answer-/),
    ))
  })

  it('restores a pending dirty question on Runs and answers it without a new start', async () => {
    const pendingRun = {
      ...ownedRun,
      run_id: 'run-dirty-question',
      status: 'awaiting_startup_answer',
      launch_phase: 'awaiting_startup_answer',
      evidence: {
        ...ownedRun.evidence,
        startup_question: {
          question_id: 'question-reopened',
          kind: 'confirm_worktree_dirty',
          message: 'The reopened checkout is dirty. Continue?',
          options: {},
          choices: [],
          run_id: 'run-dirty-question',
          schema_version: 1,
        },
      },
    }
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [pendingRun], next_cursor: null, schema_version: 1 })
    vi.mocked(api.getControlPlaneRun).mockImplementation(async (_projectId, runId) => runId === pendingRun.run_id ? pendingRun : ownedRun)
    vi.mocked(api.answerStartupQuestion).mockResolvedValue({ result: null, startup_question: null })
    renderDashboard({ requestedRunId: pendingRun.run_id })

    await screen.findByText('The reopened checkout is dirty. Continue?')
    expect(screen.getByRole('button', { name: 'Confirm and continue' })).toBeDefined()
    expect(screen.getByRole('button', { name: 'Decline and stop' })).toBeDefined()
    fireEvent.click(screen.getByRole('button', { name: 'Decline and stop' }))
    await waitFor(() => expect(api.answerStartupQuestion).toHaveBeenCalledWith(
      'control-project', 'question-reopened', false, expect.stringMatching(/^startup-answer-/),
    ))
    expect(api.startControlPlaneRun).not.toHaveBeenCalled()
  })

  it('shows every dirty status and requires the explicit continuation checkbox', async () => {
    vi.mocked(api.preflightControlPlaneRun).mockResolvedValue(preflightResult({
      dirty: true,
      requires_confirmation: true,
      total_items: 4,
      items: [
        { path: 'src/staged.ts', index_status: 'M', worktree_status: ' ', original_path: null },
        { path: 'src/changed.ts', index_status: ' ', worktree_status: 'M', original_path: null },
        { path: 'notes.txt', index_status: '?', worktree_status: '?', original_path: null },
        { path: 'src/new.ts', index_status: 'R', worktree_status: ' ', original_path: 'src/old.ts' },
      ],
    }))
    vi.mocked(api.startControlPlaneRun).mockResolvedValue({
      result: { run_id: 'run-started', created: true, status: 'running', schema_version: 1, manifest_path: null, reason: null, restarted_from_run_id: null },
      startup_question: null,
    })
    renderDashboard()
    await openNewRun()
    choose('Run plan', 'plans/in-progress/demo.md')
    choose('Run workflow', 'managed')

    await screen.findByText('src/staged.ts')
    expect(screen.getByText('staged modified')).toBeDefined()
    expect(screen.getByText('unstaged modified')).toBeDefined()
    expect(screen.getByText('untracked')).toBeDefined()
    expect(screen.getByText('staged renamed')).toBeDefined()
    expect(screen.getByText('src/old.ts')).toBeDefined()
    const checkbox = screen.getByRole('checkbox', { name: 'Continue despite uncommitted changes' }) as HTMLInputElement
    expect(checkbox.checked).toBe(false)
    expect(screen.getByRole('button', { name: 'Start run' }).getAttribute('disabled')).not.toBeNull()

    fireEvent.click(checkbox)
    expect(checkbox.checked).toBe(true)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Start run' }).getAttribute('disabled')).toBeNull())
    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))
    await waitFor(() => expect(api.startControlPlaneRun).toHaveBeenCalledWith(
      'control-project',
      expect.objectContaining({ dirty_worktree_confirmed: true }),
      expect.any(String),
    ))
  })

  it('appends the next preflight page only through Show more', async () => {
    vi.mocked(api.preflightControlPlaneRun)
      .mockResolvedValueOnce(preflightResult({
        dirty: true,
        total_items: 2,
        next_offset: 1,
        items: [{ path: 'first.ts', index_status: ' ', worktree_status: 'M', original_path: null }],
      }))
      .mockResolvedValueOnce(preflightResult({
        dirty: true,
        offset: 1,
        total_items: 2,
        items: [{ path: 'second.ts', index_status: ' ', worktree_status: 'D', original_path: null }],
      }))
    renderDashboard()
    await openNewRun()
    choose('Run plan', 'plans/in-progress/demo.md')
    choose('Run workflow', 'managed')

    await screen.findByText('first.ts')
    const showMore = screen.getByRole('button', { name: 'Show more (1 remaining)' })
    fireEvent.click(showMore)
    await screen.findByText('second.ts')
    expect(screen.getByText('first.ts')).toBeDefined()
    expect(screen.queryByRole('button', { name: /Show more/ })).toBeNull()
    expect(api.preflightControlPlaneRun).toHaveBeenNthCalledWith(
      2,
      'control-project',
      expect.objectContaining({ plan_path: 'plans/in-progress/demo.md', dirty_worktree_confirmed: false }),
      expect.objectContaining({ offset: 1, limit: 200, signal: expect.any(AbortSignal) }),
    )
  })

  it('explains new-worktree dirt without asking for an unnecessary confirmation', async () => {
    vi.mocked(api.preflightControlPlaneRun).mockResolvedValue(preflightResult({
      execution_mode: 'new_worktree',
      dirty: true,
      requires_confirmation: false,
      total_items: 1,
      items: [{ path: 'plans/in-progress/demo.md', index_status: ' ', worktree_status: 'M', original_path: null }],
    }))
    renderDashboard()
    await openNewRun()
    choose('Run plan', 'plans/in-progress/demo.md')
    choose('Run workflow', 'managed')

    await screen.findByText('plans/in-progress/demo.md')
    expect(screen.getByText(/A new worktree starts from the selected commit/)).toBeDefined()
    expect(screen.getByText(/leaves these changes in the current checkout/)).toBeDefined()
    expect(screen.queryByRole('checkbox', { name: 'Continue despite uncommitted changes' })).toBeNull()
    await waitFor(() => expect(screen.getByRole('button', { name: 'Start run' }).getAttribute('disabled')).toBeNull())
  })

  it('keeps inspection failure distinct from a clean checkout and blocks Start', async () => {
    vi.mocked(api.preflightControlPlaneRun).mockRejectedValue(new Error('git status unavailable'))
    renderDashboard()
    await openNewRun()
    choose('Run plan', 'plans/in-progress/demo.md')
    choose('Run workflow', 'managed')

    await screen.findByText(/Working tree inspection failed: git status unavailable/)
    expect(screen.queryByText('No uncommitted changes detected.')).toBeNull()
    expect(screen.getByRole('button', { name: 'Start run' }).getAttribute('disabled')).not.toBeNull()
    expect(api.startControlPlaneRun).not.toHaveBeenCalled()
  })

  it('discards a late preflight response after the workflow selection changes', async () => {
    let releaseManaged!: (result: WorktreePreflight) => void
    const managedResult = new Promise<WorktreePreflight>((resolve) => { releaseManaged = resolve })
    vi.mocked(api.preflightControlPlaneRun).mockImplementation((_projectId, request) => request.workflow_name === 'managed'
      ? managedResult
      : Promise.resolve(preflightResult({ dirty: true, total_items: 1, items: [{ path: 'other.ts', index_status: ' ', worktree_status: 'M', original_path: null }] })))
    renderDashboard()
    await openNewRun()
    choose('Run plan', 'plans/in-progress/demo.md')
    choose('Run workflow', 'managed')
    await waitFor(() => expect(api.preflightControlPlaneRun).toHaveBeenCalledTimes(1))
    choose('Run workflow', 'other')
    await screen.findByText('other.ts')
    releaseManaged(preflightResult({ dirty: true, total_items: 1, items: [{ path: 'stale.ts', index_status: ' ', worktree_status: 'M', original_path: null }] }))
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(screen.queryByText('stale.ts')).toBeNull()
    expect(screen.getByText('other.ts')).toBeDefined()
  })

  it('waits for the current inspection across default-to-explicit selection and refresh', async () => {
    const defaultInspection = deferred<WorktreePreflight>()
    const explicitInspection = deferred<WorktreePreflight>()
    const refreshInspection = deferred<WorktreePreflight>()
    let explicitRequestCount = 0
    vi.mocked(api.postGlobalConfigForm).mockResolvedValue({
      ...emptyProjection,
      form: { ...emptyProjection.form, default_workflow: 'managed' },
    })
    vi.mocked(api.preflightControlPlaneRun).mockImplementation((_projectId, request) => {
      if (request.workflow_name === 'managed') {
        explicitRequestCount += 1
        return (explicitRequestCount === 1 ? explicitInspection : refreshInspection).promise
      }
      return defaultInspection.promise
    })
    vi.mocked(api.startControlPlaneRun).mockResolvedValue({
      result: { run_id: 'run-started', created: true, status: 'running', schema_version: 1, manifest_path: null, reason: null, restarted_from_run_id: null },
      startup_question: null,
    })
    renderDashboard()

    await openNewRun()
    choose('Run plan', 'plans/in-progress/demo.md')
    await waitFor(() => {
      expect(api.preflightControlPlaneRun).toHaveBeenCalledTimes(1)
      expect(api.preflightControlPlaneRun.mock.calls[0][1]).not.toHaveProperty('workflow_name')
    })

    choose('Run workflow', 'managed')
    await waitFor(() => expect(api.preflightControlPlaneRun).toHaveBeenCalledTimes(2))
    const panel = () => screen.getByRole('region', { name: 'Working tree preflight' })
    expect((screen.getByLabelText('Run workflow') as HTMLInputElement).value).toBe('Managed')
    expect(panel().getAttribute('data-preflight-status')).toBe('loading')
    expect(screen.getByRole('button', { name: 'Start run' }).getAttribute('disabled')).not.toBeNull()

    explicitInspection.resolve(preflightResult({
      dirty: true,
      requires_confirmation: true,
      total_items: 1,
      items: [{ path: 'current.ts', index_status: ' ', worktree_status: 'M', original_path: null }],
    }))
    await screen.findByText('current.ts')
    const confirmation = screen.getByRole('checkbox', { name: 'Continue despite uncommitted changes' }) as HTMLInputElement
    fireEvent.click(confirmation)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Start run' }).getAttribute('disabled')).toBeNull())

    await act(async () => {
      defaultInspection.resolve(preflightResult({
        dirty: true,
        requires_confirmation: true,
        total_items: 1,
        items: [{ path: 'stale-default.ts', index_status: ' ', worktree_status: 'M', original_path: null }],
      }))
      await defaultInspection.promise
    })
    expect(screen.queryByText('stale-default.ts')).toBeNull()
    expect(screen.getByText('current.ts')).toBeDefined()

    fireEvent.click(screen.getByRole('button', { name: 'Refresh worktree inspection' }))
    await waitFor(() => expect(api.preflightControlPlaneRun).toHaveBeenCalledTimes(3))
    expect(panel().getAttribute('data-preflight-status')).toBe('loading')
    expect(screen.queryByRole('checkbox', { name: 'Continue despite uncommitted changes' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Start run' }).getAttribute('disabled')).not.toBeNull()

    refreshInspection.resolve(preflightResult({
      dirty: true,
      requires_confirmation: true,
      total_items: 1,
      items: [{ path: 'refreshed.ts', index_status: ' ', worktree_status: 'M', original_path: null }],
    }))
    await screen.findByText('refreshed.ts')
    expect((screen.getByRole('checkbox', { name: 'Continue despite uncommitted changes' }) as HTMLInputElement).checked).toBe(true)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Start run' }).getAttribute('disabled')).toBeNull())
    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))
    await waitFor(() => expect(api.startControlPlaneRun).toHaveBeenCalledWith(
      'control-project',
      expect.objectContaining({ workflow_name: 'managed', dirty_worktree_confirmed: true }),
      expect.any(String),
    ))
  })

  it('preserves acknowledgment through unrelated edits but resets it for a new launch selection', async () => {
    vi.mocked(api.preflightControlPlaneRun).mockResolvedValue(preflightResult({
      dirty: true,
      requires_confirmation: true,
      total_items: 1,
      items: [{ path: 'draft.ts', index_status: ' ', worktree_status: 'M', original_path: null }],
    }))
    renderDashboard()
    await openNewRun()
    choose('Run plan', 'plans/in-progress/demo.md')
    choose('Run workflow', 'managed')
    await screen.findByText('draft.ts')
    const checkbox = screen.getByRole('checkbox', { name: 'Continue despite uncommitted changes' }) as HTMLInputElement
    fireEvent.click(checkbox)
    openAdvanced()
    fireEvent.change(screen.getByLabelText('Run max turns'), { target: { value: '7' } })
    await waitFor(() => expect(api.preflightControlPlaneRun).toHaveBeenCalledTimes(2))
    expect(checkbox.checked).toBe(true)

    choose('Run workflow', 'other')
    await waitFor(() => expect((screen.getByRole('checkbox', { name: 'Continue despite uncommitted changes' }) as HTMLInputElement).checked).toBe(false))
  })

  it('rechecks dirt at launch and answers the returned question without starting another run', async () => {
    vi.mocked(api.preflightControlPlaneRun)
      .mockResolvedValueOnce(preflightResult())
      .mockResolvedValueOnce(preflightResult({
        dirty: true,
        requires_confirmation: true,
        total_items: 1,
        items: [{ path: 'appeared-at-launch.ts', index_status: ' ', worktree_status: 'M', original_path: null }],
      }))
    vi.mocked(api.startControlPlaneRun).mockResolvedValue({
      result: null,
      startup_question: { question_id: 'question-race', kind: 'confirm_worktree_dirty', message: 'Worktree became dirty at launch.', options: {}, choices: [], run_id: 'pending-run', schema_version: 1 },
    })
    vi.mocked(api.answerStartupQuestion).mockResolvedValue({
      result: { run_id: 'run-started', created: true, status: 'running', schema_version: 1, manifest_path: null, reason: null, restarted_from_run_id: null },
      startup_question: null,
    })
    renderDashboard()
    await openNewRun()
    choose('Run plan', 'plans/in-progress/demo.md')
    choose('Run workflow', 'managed')
    await waitForPreflightReady()
    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))

    await screen.findByText('appeared-at-launch.ts')
    expect(screen.getByText('Worktree became dirty at launch.')).toBeDefined()
    expect(screen.queryByRole('button', { name: 'Confirm and continue' })).toBeNull()
    const checkbox = screen.getByRole('checkbox', { name: 'Continue despite uncommitted changes' }) as HTMLInputElement
    expect(checkbox.checked).toBe(false)
    expect(screen.getByRole('button', { name: 'Start run' }).getAttribute('disabled')).not.toBeNull()
    fireEvent.click(checkbox)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Start run' }).getAttribute('disabled')).toBeNull())
    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))
    await waitFor(() => expect(api.answerStartupQuestion).toHaveBeenCalledWith(
      'control-project', 'question-race', true, expect.stringMatching(/^startup-answer-/),
    ))
    expect(api.startControlPlaneRun).toHaveBeenCalledTimes(1)
  })

  it('offers capability-admitted start steps, explains skips, and sends the canonical step name', async () => {
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [], next_cursor: null, schema_version: 1 })
    vi.mocked(api.startControlPlaneRun).mockResolvedValue({
      result: { run_id: 'run-started', created: true, status: 'running', schema_version: 1, manifest_path: null, reason: null, restarted_from_run_id: null },
      startup_question: null,
    })
    renderDashboard()

    await openNewRun()
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
    await waitForPreflightReady()
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

    await openNewRun()
    await screen.findByLabelText('Run plan')
    choose('Run plan', 'plans/in-progress/demo.md')
    choose('Run workflow', 'managed')
    openAdvanced()
    fireEvent.change(screen.getByLabelText('Run extra instructions'), { target: { value: '  Focus on tests.  \n\nKeep runs short. ' } })
    await waitForPreflightReady()
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

    await openNewRun()
    await screen.findByLabelText('Run plan')
    choose('Run plan', 'plans/in-progress/demo.md')
    choose('Run workflow', 'managed')
    await waitForPreflightReady()
    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))
    await waitFor(() => expect(screen.getByText('connection lost')).toBeDefined())

    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))
    await waitFor(() => expect(api.startControlPlaneRun).toHaveBeenCalledTimes(2))
    const unchangedRetryKey = vi.mocked(api.startControlPlaneRun).mock.calls[0][2]
    expect(vi.mocked(api.startControlPlaneRun).mock.calls[1][2]).toBe(unchangedRetryKey)

    if (!screen.queryByLabelText('Run max turns')) openAdvanced()
    fireEvent.change(screen.getByLabelText('Run max turns'), { target: { value: '9' } })
    await waitForPreflightReady()
    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))
    await waitFor(() => expect(api.startControlPlaneRun).toHaveBeenCalledTimes(3))
    expect(vi.mocked(api.startControlPlaneRun).mock.calls[2][2]).not.toBe(unchangedRetryKey)
    expect(vi.mocked(api.startControlPlaneRun).mock.calls[2][1]).toEqual({ plan_path: 'plans/in-progress/demo.md', workflow_name: 'managed', max_turns: 9, dirty_worktree_confirmed: false })
  })

  it('keeps launch selections after a safe rejection and clears an old reserved link on retry', async () => {
    const message = 'Plan validation failed. Add or correct exactly one Git Tracking section, then retry.'
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [], next_cursor: null, schema_version: 1 })
    vi.mocked(api.startControlPlaneRun)
      .mockRejectedValueOnce(new ApiError(422, message, 'plan_validation_failed', {
        code: 'plan_validation_failed', message, run_id: 'run-reserved',
      }))
      .mockRejectedValueOnce(new ApiError(422, 'operation_rejected', 'operation_rejected'))
    renderDashboard()

    await openNewRun()
    choose('Run plan', 'plans/in-progress/demo.md')
    choose('Run workflow', 'managed')
    await waitForPreflightReady()
    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))

    await screen.findByText(message)
    expect((screen.getByLabelText('Run plan') as HTMLInputElement).value).toBe('plans/in-progress/demo.md')
    expect((screen.getByLabelText('Run workflow') as HTMLInputElement).value).toBe('Managed')
    expect(screen.getByRole('button', { name: 'View failed request' })).toBeDefined()

    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))
    await screen.findByText('operation_rejected')
    expect(screen.queryByRole('button', { name: 'View failed request' })).toBeNull()
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

  it('keeps a rejected control draft and run status while showing the server error', async () => {
    vi.mocked(api.getControlPlaneCapabilities).mockResolvedValue({
      ...capabilities,
      teams: ['base', 'fast_team', 'fast__team'],
    })
    vi.mocked(api.controlControlPlaneRun).mockRejectedValueOnce(
      new ApiError(422, 'fast__team is not admitted for this run', 'invalid_control'),
    )
    renderDashboard()

    await waitForControlAdmission()
    const team = screen.getByLabelText('Control team') as HTMLSelectElement
    expect(screen.getByRole('option', { name: 'No team', exact: true })).toBeDefined()
    expect(screen.getByRole('option', { name: 'Fast team (fast_team)', exact: true })).toBeDefined()
    expect(screen.getByRole('option', { name: 'Fast team (fast__team)', exact: true })).toBeDefined()
    fireEvent.change(team, { target: { value: 'fast__team' } })
    await waitFor(() => expect(team.value).toBe('fast__team'))
    const save = screen.getByRole('button', { name: 'Save run settings' })
    await waitFor(() => expect(save.getAttribute('disabled')).toBeNull())
    fireEvent.click(save)
    await waitFor(() => expect(api.controlControlPlaneRun).toHaveBeenCalledWith(
      'control-project',
      'run-owned',
      { expected_revision: 1, team: 'fast__team' },
      expect.stringMatching(/^control-/),
    ))
    await waitFor(() => expect(screen.getByText('fast__team is not admitted for this run')).toBeDefined())
    expect(team.value).toBe('fast__team')
    expect(screen.getByRole('button', { name: 'run-owned' })).toBeDefined()
    expect(screen.getAllByText('Running').length).toBeGreaterThan(0)
  })

  it('recovers a failed project discovery through Refresh', async () => {
    const discoveryError = 'project discovery temporarily offline'
    const retryProjects = deferred<Array<typeof project>>()
    vi.mocked(api.listControlPlaneProjects)
      .mockRejectedValueOnce(new Error(discoveryError))
      .mockImplementationOnce(() => retryProjects.promise)
    renderDashboard()

    await screen.findByText(discoveryError)
    const refresh = screen.getByRole('button', { name: 'Refresh', exact: true }) as HTMLButtonElement
    expect(refresh.disabled).toBe(false)
    expect(api.listControlPlaneProjects).toHaveBeenCalledTimes(1)

    fireEvent.click(refresh)
    await waitFor(() => expect(api.listControlPlaneProjects).toHaveBeenCalledTimes(2))
    expect(screen.queryByRole('button', { name: 'run-owned', exact: true })).toBeNull()

    await act(async () => {
      retryProjects.resolve([project])
      await retryProjects.promise
    })
    await screen.findByRole('button', { name: 'run-owned', exact: true })
    await waitFor(() => expect((screen.getByRole('button', { name: 'Refresh', exact: true }) as HTMLButtonElement).disabled).toBe(false))
    expect(screen.queryByText(discoveryError)).toBeNull()
    expect(screen.getByRole('button', { name: 'run-owned', exact: true })).toBeDefined()
  })

  it('keeps a rejected action visible when a project discovery retry fails', async () => {
    const actionError = 'control action remains pending review'
    vi.mocked(api.controlControlPlaneRun).mockRejectedValueOnce(
      new ApiError(422, actionError, 'invalid_control'),
    )
    const view = renderDashboard()

    await waitForControlAdmission()
    const maxTurns = screen.getByLabelText('Control max turns') as HTMLInputElement
    fireEvent.change(maxTurns, { target: { value: '9' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save run settings' }))
    await screen.findByText(actionError)

    view.rerender(dashboardNode({ visible: false }))
    const initialDiscoveryError = 'discovery retry temporarily offline'
    const repeatedDiscoveryError = 'discovery retry still offline'
    vi.mocked(api.listControlPlaneProjects)
      .mockRejectedValueOnce(new Error(initialDiscoveryError))
      .mockRejectedValueOnce(new Error(repeatedDiscoveryError))
    view.rerender(dashboardNode({ visible: true }))

    await screen.findByText(initialDiscoveryError)
    expect(screen.getByText(actionError)).toBeDefined()
    expect(api.listControlPlaneProjects).toHaveBeenCalledTimes(2)

    fireEvent.click(screen.getByRole('button', { name: 'Refresh', exact: true }))
    await waitFor(() => expect(api.listControlPlaneProjects).toHaveBeenCalledTimes(3))
    await screen.findByText(repeatedDiscoveryError)
    expect(screen.getByText(actionError)).toBeDefined()
  })

  it('keeps a rejected control draft through a late passive snapshot refresh', async () => {
    let emit: ((events: api.RunEvent[]) => void) | null = null
    vi.mocked(api.subscribeToRunEvents).mockImplementation((subscription) => {
      emit = subscription.onEvents
      return () => {}
    })
    const acceptedRun = { ...ownedRun, revision: 2, max_turns: 12 }
    const passiveRefresh = deferred<typeof acceptedRun>()
    vi.mocked(api.getControlPlaneRun).mockResolvedValue(ownedRun)
    const rejection = 'responsive fixture rejected this control draft'
    vi.mocked(api.controlControlPlaneRun)
      .mockResolvedValueOnce({ revision: 2, changed: true, owner_stop: false, run: acceptedRun })
      .mockRejectedValueOnce(new ApiError(422, rejection, 'invalid_control'))
      .mockResolvedValueOnce({ revision: 3, changed: true, owner_stop: false, run: { ...acceptedRun, revision: 3, max_turns: 13 } })
    renderDashboard()

    await waitForControlAdmission()
    const maxTurns = screen.getByLabelText('Control max turns') as HTMLInputElement
    const save = screen.getByRole('button', { name: 'Save run settings' })
    fireEvent.change(maxTurns, { target: { value: '12' } })
    fireEvent.click(save)
    await waitFor(() => expect(api.controlControlPlaneRun).toHaveBeenCalledTimes(1))
    await screen.findByText(/Safe controls recorded at revision 2/)
    expect(api.controlControlPlaneRun).toHaveBeenNthCalledWith(
      1,
      'control-project',
      'run-owned',
      { expected_revision: 1, max_turns: 12 },
      expect.stringMatching(/^control-/),
    )

    vi.mocked(api.getControlPlaneRun).mockImplementationOnce(() => passiveRefresh.promise)
    if (!emit) throw new Error('subscription hook was not registered')
    act(() => {
      emit!([{ sequence: 2, event_type: 'status_changed', data: {}, schema_version: 1, timestamp: '2024-01-01T00:02:00Z' }])
    })
    fireEvent.change(maxTurns, { target: { value: '13' } })
    fireEvent.click(save)
    await waitFor(() => expect(api.controlControlPlaneRun).toHaveBeenCalledTimes(2))
    await screen.findByText(rejection)
    expect(api.controlControlPlaneRun).toHaveBeenNthCalledWith(
      2,
      'control-project',
      'run-owned',
      { expected_revision: 2, max_turns: 13 },
      expect.stringMatching(/^control-/),
    )

    await waitFor(() => expect(api.getControlPlaneRun.mock.calls.length).toBeGreaterThan(1))
    await act(async () => {
      passiveRefresh.resolve(acceptedRun)
      await passiveRefresh.promise
    })
    expect(screen.getByText(rejection)).toBeDefined()
    expect(maxTurns.value).toBe('13')
    expect(screen.getByRole('button', { name: 'run-owned' })).toBeDefined()

    const readFailure = 'passive snapshot temporarily unavailable'
    vi.mocked(api.getControlPlaneRun).mockRejectedValueOnce(new Error(readFailure))
    act(() => {
      emit!([{ sequence: 3, event_type: 'status_changed', data: {}, schema_version: 1, timestamp: '2024-01-01T00:03:00Z' }])
    })
    await screen.findByText(readFailure)
    expect(screen.getByText(rejection)).toBeDefined()

    vi.mocked(api.getControlPlaneRun).mockResolvedValueOnce(acceptedRun)
    act(() => {
      emit!([{ sequence: 4, event_type: 'status_changed', data: {}, schema_version: 1, timestamp: '2024-01-01T00:04:00Z' }])
    })
    await waitFor(() => expect(screen.queryByText(readFailure)).toBeNull())
    expect(screen.getByText(rejection)).toBeDefined()

    fireEvent.click(save)
    await screen.findByText(/Safe controls recorded at revision 3/)
    expect(screen.queryByText(rejection)).toBeNull()
  })

  it('does not let an older selected-run read replace an acknowledged control revision', async () => {
    const staleRun = { ...ownedRun, revision: 0, team: 'base', max_turns: 8 }
    const acknowledgedRun = { ...ownedRun, revision: 1, team: 'full', max_turns: 12 }
    const detailReady = deferred<typeof staleRun>()
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [staleRun], next_cursor: null, schema_version: 1 })
    vi.mocked(api.getControlPlaneRun).mockImplementationOnce(() => detailReady.promise)
    vi.mocked(api.controlControlPlaneRun).mockResolvedValue({
      revision: 1,
      changed: true,
      owner_stop: false,
      run: acknowledgedRun,
    })
    renderDashboard({ requestedRunId: staleRun.run_id })

    await waitForControlAdmission('Selector for Worker')
    fireEvent.change(screen.getByLabelText('Control max turns'), { target: { value: '12' } })
    fireEvent.change(screen.getByLabelText('Control team'), { target: { value: 'full' } })
    fireEvent.change(screen.getByLabelText('Selector for Worker'), { target: { value: 'harness/impl-b' } })
    const save = screen.getByRole('button', { name: 'Save run settings' })
    await waitFor(() => expect(save.getAttribute('disabled')).toBeNull())
    fireEvent.click(save)
    await waitFor(() => expect(api.controlControlPlaneRun).toHaveBeenCalledWith(
      'control-project',
      'run-owned',
      { expected_revision: 0, max_turns: 12, team: 'full', role_selectors: { worker: 'harness/impl-b' } },
      expect.stringMatching(/^control-/),
    ))
    await waitFor(() => expect(screen.getByText(/revision 1\. The engine applies them at the next safe boundary/)).toBeDefined())

    // This direct read began before the write acknowledgement and settles after it.
    await act(async () => {
      detailReady.resolve(staleRun)
      await detailReady.promise
    })

    fireEvent.change(screen.getByLabelText('Control max turns'), { target: { value: '13' } })
    fireEvent.click(save)
    await waitFor(() => expect(api.controlControlPlaneRun).toHaveBeenCalledTimes(2))
    expect(api.controlControlPlaneRun.mock.calls[1][2]).toEqual({ expected_revision: 1, max_turns: 13 })
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
    expect(screen.getByText(/Changes are saved now and apply at the next safe turn/)).toBeDefined()

    await waitForControlAdmission('Selector for Worker')
    const team = screen.getByLabelText('Control team') as HTMLSelectElement
    const selector = screen.getByLabelText('Selector for Worker') as HTMLSelectElement
    fireEvent.change(team, { target: { value: 'full' } })
    fireEvent.change(selector, { target: { value: 'harness/impl-b' } })
    await waitFor(() => expect((screen.getByLabelText('Control team') as HTMLSelectElement).value).toBe('full'))
    const save = screen.getByRole('button', { name: 'Save run settings' })
    await waitFor(() => expect(save.getAttribute('disabled')).toBeNull())
    fireEvent.click(save)
    await waitFor(() => expect(api.controlControlPlaneRun).toHaveBeenCalledWith(
      'control-project',
      'run-owned',
      expect.objectContaining({ expected_revision: 1, team: 'full', role_selectors: { worker: 'harness/impl-b' } }),
      expect.stringMatching(/^control-/),
    ))
    await waitFor(() => expect(screen.getByText(/revision 2\. The engine applies them at the next safe boundary/)).toBeDefined())
  })

  it('requests a boundary stop through revisioned control and keeps the immediate stop separate', async () => {
    const pending = {
      ...ownedRun,
      revision: 2,
      evidence: {
        ...ownedRun.evidence,
        overrides: { state: 'pending', revision: 2, owner_stop: true },
      },
    }
    vi.mocked(api.controlControlPlaneRun).mockResolvedValue({
      revision: 2,
      changed: true,
      owner_stop: true,
      run: pending,
    })
    renderDashboard()

    const boundary = await screen.findByRole('button', { name: 'Stop after current turn', exact: true })
    fireEvent.click(boundary)
    await waitFor(() => expect(api.controlControlPlaneRun).toHaveBeenCalledWith(
      'control-project',
      'run-owned',
      { expected_revision: 1, owner_stop: true },
      expect.stringMatching(/^boundary-stop-/),
    ))
    await screen.findByText(/Stop after current turn requested for run-owned/)
    expect(api.ownerStopControlPlaneRun).not.toHaveBeenCalled()
    await screen.findByText(/Stop requested — finishing current turn/)
    expect(screen.getByRole('button', { name: 'Stop now…', exact: true })).toBeDefined()
  })

  it('shows a saved boundary-stop intent after a fresh run read and leaves terminal runs without stop controls', async () => {
    const pending = {
      ...ownedRun,
      evidence: {
        ...ownedRun.evidence,
        overrides: { state: 'pending', revision: 1, owner_stop: true },
      },
    }
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [pending], next_cursor: null, schema_version: 1 })
    vi.mocked(api.getControlPlaneRun).mockResolvedValue(pending)
    const rendered = renderDashboard()
    await screen.findByText(/Stop requested — finishing current turn/)
    expect(screen.getByRole('button', { name: 'Stop now…', exact: true })).toBeDefined()

    rendered.unmount()
    const stopped = { ...pending, status: 'owner_stopped', launch_phase: 'owner_stopped' }
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [stopped], next_cursor: null, schema_version: 1 })
    vi.mocked(api.getControlPlaneRun).mockResolvedValue(stopped)
    renderDashboard()
    await screen.findAllByText('Stopped')
    expect(screen.queryByRole('button', { name: 'Stop after current turn', exact: true })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Stop now…', exact: true })).toBeNull()
  })

  it('refreshes a stale boundary-stop revision without retrying the write', async () => {
    vi.mocked(api.controlControlPlaneRun).mockRejectedValue(
      new ApiError(409, 'revision changed', 'revision_conflict'),
    )
    renderDashboard()
    fireEvent.click(await screen.findByRole('button', { name: 'Stop after current turn', exact: true }))
    await screen.findByText(/stop request was not retried/)
    expect(api.controlControlPlaneRun).toHaveBeenCalledTimes(1)
    expect(api.getControlPlaneRun.mock.calls.length).toBeGreaterThan(1)
  })

  it('keeps Stop now on the immediate endpoint and confirms its terminal response', async () => {
    const stopped = { ...ownedRun, status: 'owner_stopped', launch_phase: 'owner_stopped' }
    const capabilitiesReady = deferred<typeof capabilities>()
    vi.mocked(api.getControlPlaneCapabilities).mockImplementationOnce(() => capabilitiesReady.promise)
    vi.mocked(api.ownerStopControlPlaneRun).mockResolvedValue(stopped)
    renderDashboard()

    await waitFor(() => expect(api.getControlPlaneCapabilities).toHaveBeenCalledWith('control-project'))
    expect(screen.queryByRole('button', { name: 'Stop now…', exact: true })).toBeNull()
    expect(api.ownerStopControlPlaneRun).not.toHaveBeenCalled()
    await act(async () => {
      capabilitiesReady.resolve(capabilities)
      await capabilitiesReady.promise
    })
    await waitForControlAdmission()

    fireEvent.click(await screen.findByRole('button', { name: 'Stop now…', exact: true }))
    await screen.findByText(/interrupts the active worker\/reviewer call/)
    expect(api.ownerStopControlPlaneRun).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Stop now', exact: true }))
    await waitFor(() => expect(api.ownerStopControlPlaneRun).toHaveBeenCalledWith(
      'control-project', 'run-owned', 1, expect.stringMatching(/^owner-stop-/),
    ))
    expect(api.ownerStopControlPlaneRun).toHaveBeenCalledTimes(1)
    await screen.findByText(/Stop now recorded for run-owned/)
    expect(api.controlControlPlaneRun).not.toHaveBeenCalled()
    expect(screen.queryByRole('button', { name: 'Stop after current turn', exact: true })).toBeNull()
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
    await waitForControlAdmission('Selector for Code review (code__review)')
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
    await waitForPreflightReady({
      plan_path: 'plans/in-progress/demo.md',
      workflow_name: 'other',
      restarted_from_run_id: 'run-owned',
    })
    const confirmation = screen
      .getByRole('button', { name: 'Confirm stop and start successor', exact: true })
      .closest('.confirmation')
    expect(confirmation?.textContent).toContain('and start')
    expect(confirmation?.textContent).toContain('successor workflow')
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
    await screen.findByLabelText('Run plan')
    choose('Run plan', 'plans/in-progress/demo.md')
    choose('Run workflow', 'other')
    fireEvent.click(screen.getByRole('button', { name: 'More', exact: true }))
    fireEvent.click(screen.getByRole('menuitem', { name: 'Cancel', exact: true }))
    fireEvent.click(screen.getByRole('button', { name: 'Restart with changes' }))
    await waitForPreflightReady()
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
    const destinationStart = await screen.findByRole('button', { name: 'Start run' })
    expect(destinationStart.getAttribute('disabled')).not.toBeNull()
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
    await screen.findByLabelText('Run plan')
    choose('Run plan', 'plans/in-progress/demo.md')
    choose('Run workflow', 'other')
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    fireEvent.click(screen.getByRole('button', { name: 'Restart with changes' }))
    await waitForPreflightReady()
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
    expect(screen.queryByRole('button', { name: 'Stop after current turn', exact: true })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Stop now…', exact: true })).toBeNull()
    rendered.unmount()

    const completed = { ...ownedRun, status: 'completed', launch_phase: 'completed' }
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [completed], next_cursor: null, schema_version: 1 })
    vi.mocked(api.getControlPlaneRun).mockResolvedValue(completed)
    renderDashboard()
    await screen.findAllByText('Completed')
    expect(screen.queryByRole('button', { name: /Change workflow/ })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Stop after current turn', exact: true })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Stop now…', exact: true })).toBeNull()
    expect(api.ownerStopControlPlaneRun).not.toHaveBeenCalled()
    expect(api.startControlPlaneRun).not.toHaveBeenCalled()
  })

  it('halts restart automation with the draft preserved when the owner stop fails', async () => {
    vi.mocked(api.ownerStopControlPlaneRun).mockRejectedValue(new Error('stop rejected by server'))
    renderDashboard({ restartPollIntervalMs: 1 })

    await openNewRun()
    await screen.findByLabelText('Run plan')
    choose('Run plan', 'plans/in-progress/demo.md')
    choose('Run workflow', 'other')
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    fireEvent.click(screen.getByRole('button', { name: 'Restart with changes' }))
    await waitForPreflightReady()
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
    await screen.findByLabelText('Run plan')
    choose('Run plan', 'plans/in-progress/demo.md')
    choose('Run workflow', 'other')
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    fireEvent.click(screen.getByRole('button', { name: 'Restart with changes' }))
    await waitForPreflightReady()
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
    await screen.findByLabelText('Run plan')
    choose('Run plan', 'plans/in-progress/demo.md')
    choose('Run workflow', 'other')
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    fireEvent.click(screen.getByRole('button', { name: 'Restart with changes' }))
    await waitForPreflightReady()
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

  it('creates one editable follow-up draft for the selected failed run and opens its exact path', async () => {
    const failed = {
      ...ownedRun,
      run_id: 'run-failed',
      status: 'failed',
      activity: 'inactive' as const,
      launch_phase: 'failed',
      reason: 'worker stopped before the checkpoint boundary',
    }
    const created = {
      project_id: project.project_id,
      name: 'corrected-followup.md',
      path: 'plans/todo/corrected-followup.md',
      status: 'todo' as const,
      revision: 'c'.repeat(64),
      size_bytes: 20,
    }
    const pending = deferred<typeof created>()
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [failed], next_cursor: null, schema_version: 1 })
    vi.mocked(api.getControlPlaneRun).mockResolvedValue(failed)
    vi.mocked(api.createProjectPlanFromRun).mockReturnValue(pending.promise)
    const onOpenPlan = vi.fn()
    renderDashboard({ onOpenPlan })

    const button = await screen.findByRole('button', { name: 'Create follow-up draft', exact: true })
    const filename = screen.getByLabelText('Follow-up draft filename') as HTMLInputElement
    expect(filename.value).toBe('followup-run-failed.md')
    fireEvent.change(filename, { target: { value: created.name } })
    fireEvent.click(button)
    await waitFor(() => expect(api.createProjectPlanFromRun).toHaveBeenCalledTimes(1))
    expect(api.createProjectPlanFromRun).toHaveBeenCalledWith(project.project_id, failed.run_id, created.name)
    expect(button).toHaveProperty('disabled', true)
    fireEvent.click(button)
    expect(api.createProjectPlanFromRun).toHaveBeenCalledTimes(1)

    await act(async () => {
      pending.resolve(created)
      await pending.promise
    })
    await waitFor(() => expect(onOpenPlan).toHaveBeenCalledWith(created.path))
  })

  it('keeps a corrected follow-up filename after a collision and maps the error to an action', async () => {
    const failed = { ...ownedRun, run_id: 'run-collision', status: 'failed', activity: 'inactive' as const, launch_phase: 'failed' }
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [failed], next_cursor: null, schema_version: 1 })
    vi.mocked(api.getControlPlaneRun).mockResolvedValue(failed)
    vi.mocked(api.createProjectPlanFromRun).mockRejectedValue(new ApiError(409, 'plan_exists', 'plan_exists'))
    renderDashboard()

    await screen.findByRole('button', { name: 'Create follow-up draft', exact: true })
    const filename = screen.getByLabelText('Follow-up draft filename') as HTMLInputElement
    fireEvent.change(filename, { target: { value: 'existing-followup.md' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create follow-up draft', exact: true }))

    await screen.findByText('A draft named existing-followup.md already exists. Choose a different filename and try again.', { exact: true })
    expect(filename.value).toBe('existing-followup.md')
  })

  it.each(['resolution', 'rejection'] as const)('releases follow-up busy state after stale %s', async outcome => {
    const first = { ...ownedRun, run_id: 'run-first', status: 'failed', activity: 'inactive' as const, launch_phase: 'failed' }
    const second = { ...ownedRun, run_id: 'run-second', status: 'failed', activity: 'inactive' as const, launch_phase: 'failed' }
    const firstCreated = {
      project_id: project.project_id,
      name: 'followup-run-first.md',
      path: 'plans/todo/followup-run-first.md',
      status: 'todo' as const,
      revision: 'd'.repeat(64),
      size_bytes: 20,
    }
    const secondCreated = {
      ...firstCreated,
      name: 'followup-run-second.md',
      path: 'plans/todo/followup-run-second.md',
      revision: 'e'.repeat(64),
    }
    const pending = deferred<typeof firstCreated>()
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [first, second], next_cursor: null, schema_version: 1 })
    vi.mocked(api.getControlPlaneRun).mockImplementation(async (_projectId, runId) => runId === first.run_id ? first : second)
    vi.mocked(api.createProjectPlanFromRun)
      .mockReturnValueOnce(pending.promise)
      .mockResolvedValueOnce(secondCreated)
    const onOpenPlan = vi.fn()
    renderDashboard({ onOpenPlan })

    await screen.findByRole('button', { name: 'Create follow-up draft', exact: true })
    fireEvent.click(screen.getByRole('button', { name: 'Create follow-up draft', exact: true }))
    await waitFor(() => expect(api.createProjectPlanFromRun).toHaveBeenCalledTimes(1))
    fireEvent.click(screen.getByRole('button', { name: /run-second/ }))
    await act(async () => {
      if (outcome === 'resolution') {
        pending.resolve(firstCreated)
        await pending.promise
      } else {
        pending.reject(new ApiError(503, 'control_plane_unavailable', 'control plane unavailable'))
        await pending.promise.catch(() => undefined)
      }
    })
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Create follow-up draft', exact: true })).toHaveProperty('disabled', false)
      expect(screen.getByRole('button', { name: 'New run', exact: true })).toHaveProperty('disabled', false)
    })
    expect(onOpenPlan).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Create follow-up draft', exact: true }))
    await waitFor(() => expect(api.createProjectPlanFromRun).toHaveBeenCalledTimes(2))
    expect(api.createProjectPlanFromRun).toHaveBeenLastCalledWith(project.project_id, second.run_id, 'followup-run-second.md')
  })

  it('keeps ordinary Resume primary and submits explicit durable recovery to a selected worker', async () => {
    const source = {
      ...ownedRun,
      run_id: 'recovery-source',
      status: 'failed',
      activity: 'inactive' as const,
      launch_phase: 'failed',
      reason: 'The original provider session is unavailable.',
      evidence: { ...ownedRun.evidence, can_resume: true },
    }
    const successor = {
      ...ownedRun,
      run_id: 'recovery-successor',
      status: 'running',
      activity: 'active' as const,
      launch_phase: 'unit_started',
      evidence: {
        ...ownedRun.evidence,
        unit_active: true,
        recovery_worker: {
          schema_version: 1,
          target_run_id: 'recovery-successor',
          target_selector: 'harness/impl-b',
          operation_state: 'consumed',
          operation_started: true,
          session_started: true,
          session_status: 'active',
        },
      },
    }
    vi.mocked(api.listControlPlaneRuns)
      .mockResolvedValueOnce({ runs: [source], next_cursor: null, schema_version: 1 })
      .mockResolvedValue({ runs: [source, successor], next_cursor: null, schema_version: 1 })
    vi.mocked(api.getControlPlaneRun).mockImplementation(async (_projectId, runId) => runId === source.run_id ? source : successor)
    vi.mocked(api.getRestartOptions).mockImplementation(async (_projectId, runId) => runId === source.run_id
      ? { eligible: true, reason: null, requires_stop: false, run_id: source.run_id, extra_instructions_unavailable: false, options: { plan_path: source.plan_path ?? 'plans/in-progress/demo.md' } }
      : null as never)
    vi.mocked(api.listRunEvents).mockImplementation(async (_projectId, runId) => runId === successor.run_id
      ? [{ sequence: 1, event_type: 'recovery_requested', data: {
        mode: 'durable_evidence', source_run_id: source.run_id, target_run_id: successor.run_id,
        source_selector: 'harness/impl-a', target_selector: 'harness/impl-b',
        artifact_path: 'recovery/recovery-intent.json', evidence: ['scope:checkpoint-4'],
        source_session_context_transferred: false,
      }, schema_version: 1, timestamp: '2024-01-01T00:02:00Z' }]
      : [{ sequence: 1, event_type: 'run_started', data: {}, schema_version: 1, timestamp: '2024-01-01T00:00:00Z' }])
    vi.mocked(api.resumeControlPlaneRun).mockResolvedValue({ run_id: successor.run_id, created: true, status: 'running', schema_version: 1, manifest_path: null, reason: null, restarted_from_run_id: null })
    const onRunSelectionChange = vi.fn()
    renderDashboard({ requestedRunId: source.run_id, onRunSelectionChange })

    await screen.findByRole('button', { name: /Resume as new run/ })
    const recoveryButton = await screen.findByRole('button', { name: 'Recover with another worker…' })
    expect(recoveryButton.className).toContain('btn-secondary')
    fireEvent.click(recoveryButton)
    const selector = await screen.findByLabelText('Recovery worker') as HTMLInputElement
    fireEvent.focus(selector)
    fireEvent.change(selector, { target: { value: 'harness/impl-b' } })
    fireEvent.click(await screen.findByRole('option', { name: /harness\/impl-b/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Recover with selected worker' }))

    await waitFor(() => expect(api.resumeControlPlaneRun).toHaveBeenCalledWith(
      'control-project', source.run_id, expect.stringMatching(/^recovery-/),
      { recovery: { mode: 'durable_evidence', worker_selector: 'harness/impl-b' } },
    ))
    await screen.findByText('Replacement worker started')
    expect(screen.getByText('Source run')).toBeDefined()
    expect(screen.getByText(source.run_id)).toBeDefined()
    expect(screen.getAllByText('harness/impl-b').length).toBeGreaterThan(0)
    expect(screen.getByText(/private context from the old provider session was unavailable/i)).toBeDefined()
    expect(screen.getByRole('link', { name: /open the recorded event and artifact details/i })).toBeDefined()
    await waitFor(() => expect(onRunSelectionChange).toHaveBeenCalledWith({ runId: successor.run_id, userInitiated: false }))
  })

  it('keeps the recovery draft and source selected when admission rejects the replacement', async () => {
    const source = {
      ...ownedRun,
      run_id: 'recovery-rejected-source',
      status: 'failed',
      activity: 'inactive' as const,
      launch_phase: 'failed',
      evidence: { ...ownedRun.evidence, can_resume: true },
    }
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [source], next_cursor: null, schema_version: 1 })
    vi.mocked(api.getControlPlaneRun).mockResolvedValue(source)
    vi.mocked(api.getRestartOptions).mockResolvedValue({ eligible: true, reason: null, requires_stop: false, run_id: source.run_id, extra_instructions_unavailable: false, options: { plan_path: source.plan_path ?? 'plans/in-progress/demo.md' } })
    vi.mocked(api.resumeControlPlaneRun).mockRejectedValue(new ApiError(
      422,
      'Durable recovery requires confirmed inactive source ownership; source activity is unknown or active.',
      'recovery_source_activity',
      {
        code: 'recovery_source_activity',
        message: 'Durable recovery requires confirmed inactive source ownership; source activity is unknown or active.',
      },
    ))
    renderDashboard({ requestedRunId: source.run_id })

    await screen.findByRole('button', { name: 'Resume as new run…' })
    fireEvent.click(await screen.findByRole('button', { name: 'Recover with another worker…' }))
    const selector = await screen.findByLabelText('Recovery worker') as HTMLInputElement
    fireEvent.focus(selector)
    fireEvent.change(selector, { target: { value: 'harness/impl-b' } })
    fireEvent.click(await screen.findByRole('option', { name: /harness\/impl-b/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Recover with selected worker' }))

    await screen.findByText('Recovery was rejected: Durable recovery requires confirmed inactive source ownership; source activity is unknown or active.')
    expect(screen.getByTitle('Copy run ID').textContent).toBe(source.run_id)
    expect((screen.getByLabelText('Recovery worker') as HTMLInputElement).value).toBe('harness/impl-b')
    expect(screen.getByRole('button', { name: 'Recover with selected worker' })).toBeDefined()
    expect(api.resumeControlPlaneRun).toHaveBeenCalledTimes(1)
  })

  it('does not offer replacement recovery for an active source', async () => {
    const active = {
      ...ownedRun,
      run_id: 'active-recovery-source',
      status: 'failed',
      activity: 'active' as const,
      launch_phase: 'launch_started',
      reason: 'The source operation is still active.',
    }
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [active], next_cursor: null, schema_version: 1 })
    vi.mocked(api.getControlPlaneRun).mockResolvedValue(active)
    vi.mocked(api.getRestartOptions).mockResolvedValue({
      eligible: false,
      reason: 'source worker activity is active or unknown',
      requires_stop: true,
      run_id: active.run_id,
      extra_instructions_unavailable: false,
      options: { plan_path: active.plan_path ?? 'plans/in-progress/demo.md' },
    })
    renderDashboard({ requestedRunId: active.run_id })

    await screen.findByText(/Restart unavailable: source worker activity is active or unknown/)
    expect(screen.queryByRole('button', { name: /Recover with another worker/ })).toBeNull()
    expect(api.resumeControlPlaneRun).not.toHaveBeenCalled()
  })

  it('reuses a resume key after an uncertain failure', async () => {
    const attentionRun = { ...ownedRun, run_id: 'run-needs-attention', status: 'needs_attention', revision: 3, evidence: { ...ownedRun.evidence, can_resume: true } }
    const listReady = deferred<{ runs: (typeof attentionRun)[]; next_cursor: null; schema_version: number }>()
    const detailReady = deferred<typeof attentionRun>()
    const confirmedRun = { ...attentionRun, current_step: 'review' }
    vi.mocked(api.listControlPlaneRuns).mockImplementationOnce(() => listReady.promise)
    vi.mocked(api.getControlPlaneRun).mockImplementationOnce(() => detailReady.promise)
    vi.mocked(api.resumeControlPlaneRun).mockRejectedValue(new Error('connection lost'))
    renderDashboard()

    await act(async () => {
      listReady.resolve({ runs: [attentionRun], next_cursor: null, schema_version: 1 })
      await listReady.promise
    })
    await waitFor(() => expect(screen.getByRole('button', { name: /Resume as new run/ })).toBeDefined())
    await waitFor(() => expect(api.getControlPlaneRun).toHaveBeenCalledTimes(1))
    await act(async () => {
      detailReady.resolve(confirmedRun)
      await detailReady.promise
    })
    await screen.findByText('Review · 2')
    const resumeButton = screen.getByRole('button', { name: /Resume as new run/ }) as HTMLButtonElement
    expect(resumeButton.disabled).toBe(false)
    fireEvent.click(resumeButton)
    expect(await screen.findByRole('button', { name: 'Confirm resume' })).toBeDefined()
    fireEvent.click(screen.getByRole('button', { name: 'Confirm resume' }))
    await waitFor(() => expect(screen.getByText('connection lost')).toBeDefined())

    fireEvent.click(screen.getByRole('button', { name: 'Confirm resume' }))
    await waitFor(() => expect(api.resumeControlPlaneRun).toHaveBeenCalledTimes(2))
    expect(vi.mocked(api.resumeControlPlaneRun).mock.calls[1][2]).toBe(
      vi.mocked(api.resumeControlPlaneRun).mock.calls[0][2],
    )
  })

  it.each([
    ['accepted-baseline', 'before-detail-settlement'],
    ['accepted-baseline', 'after-detail-settlement'],
    ['pending', 'before-detail-settlement'],
    ['pending', 'after-detail-settlement'],
  ] as const)('retains confirmed resume readiness for %s setup with %s interaction', async (setup, timing) => {
    const restoreSetup = installResumeComparisonSetup(setup)
    const attentionRun = { ...ownedRun, run_id: 'run-needs-attention', status: 'needs_attention', revision: 3, evidence: { ...ownedRun.evidence, can_resume: true } }
    const listReady = deferred<{ runs: (typeof attentionRun)[]; next_cursor: null; schema_version: number }>()
    const detailReady = deferred<typeof attentionRun>()
    const confirmedRun = { ...attentionRun, current_step: 'review' }
    vi.mocked(api.listControlPlaneRuns).mockImplementationOnce(() => listReady.promise)
    vi.mocked(api.getControlPlaneRun).mockImplementationOnce(() => detailReady.promise)
    const rendered = renderDashboard()

    try {
      await act(async () => {
        listReady.resolve({ runs: [attentionRun], next_cursor: null, schema_version: 1 })
        await listReady.promise
      })
      const listResumeButton = await screen.findByRole('button', { name: /Resume as new run/ }) as HTMLButtonElement
      expect(listResumeButton.disabled).toBe(false)
      expect(screen.queryByText('Review · 2')).toBeNull()
      await waitFor(() => expect(api.getControlPlaneRun).toHaveBeenCalledTimes(1))

      if (timing === 'before-detail-settlement') {
        fireEvent.click(listResumeButton)
        expect(await screen.findByRole('button', { name: 'Confirm resume' })).toBeDefined()
        await act(async () => {
          detailReady.resolve(confirmedRun)
          await detailReady.promise
        })
        await screen.findByText('Review · 2')
        expect(screen.getByRole('button', { name: 'Confirm resume' })).toBeDefined()
      } else {
        await act(async () => {
          detailReady.resolve(confirmedRun)
          await detailReady.promise
        })
        await screen.findByText('Review · 2')
        const settledResumeButton = screen.getByRole('button', { name: /Resume as new run/ }) as HTMLButtonElement
        expect(settledResumeButton.disabled).toBe(false)
        fireEvent.click(settledResumeButton)
        expect(await screen.findByRole('button', { name: 'Confirm resume' })).toBeDefined()
      }
    } finally {
      rendered.unmount()
      restoreSetup()
    }
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
    await waitForPreflightReady()
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

    await openNewRun()
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
    await openNewRun()
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

    await openNewRun()
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
    await waitForPreflightReady()
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
    await waitForPreflightReady()
    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))
    await waitFor(() => expect(api.startControlPlaneRun).toHaveBeenCalledTimes(2))
    // An empty field stays an omission: the server applies the defaults.
    expect(vi.mocked(api.startControlPlaneRun).mock.calls[1][1]).toEqual({
      plan_path: 'plans/in-progress/demo.md', max_turns: 12, dirty_worktree_confirmed: false,
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

    await openNewRun()
    await screen.findByLabelText('Run plan')
    choose('Run plan', 'plans/in-progress/demo.md')

    // Without overrides, the committed defaults resolve and are labeled as defaults.
    const preview = screen.getByLabelText('Effective choices for this launch').textContent ?? ''
    expect(preview).toContain('Managed — global default')
    expect(preview).toContain('12 — global default')
    expect(preview).toContain('Base — workflow default (Base)')
    expect(preview).toContain('Worker upgrade chain')
    expect(preview).toContain('Reviewer')
    const launchDetails = screen.getByText('Details').closest('details')
    expect(launchDetails?.hasAttribute('open')).toBe(false)
    fireEvent.click(launchDetails?.querySelector('summary') as HTMLElement)
    // jsdom does not toggle the native disclosure on every supported
    // version, so mirror the browser's open state before querying its table.
    launchDetails?.setAttribute('open', '')
    expect(launchDetails?.hasAttribute('open')).toBe(true)

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

    await waitForPreflightReady()
    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))
    await waitFor(() => expect(api.startControlPlaneRun).toHaveBeenCalledWith(
      'control-project',
      // Following the defaults keeps the request an omission.
      { plan_path: 'plans/in-progress/demo.md', max_turns: 12, dirty_worktree_confirmed: false },
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

    await waitForPreflightReady()
    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))
    await waitFor(() => expect(api.startControlPlaneRun).toHaveBeenCalledTimes(2))
    expect(api.startControlPlaneRun).toHaveBeenLastCalledWith(
      'control-project',
      { plan_path: 'plans/in-progress/demo.md', workflow_name: 'other', team: 'full', max_turns: 7, dirty_worktree_confirmed: false },
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

    await openNewRun()
    await screen.findByLabelText('Run plan')
    choose('Run plan', 'plans/in-progress/demo.md')
    openAdvanced()
    choose('Run workflow', 'managed')
    fireEvent.change(screen.getByLabelText('Run start step'), { target: { value: 'implement' } })
    expect(screen.getByText(/skips the earlier executable steps:/).textContent).toContain('Plan')

    choose('Run workflow', 'other')
    expect((screen.getByLabelText('Run start step') as HTMLSelectElement).selectedIndex).toBe(0)
    expect(screen.queryByText(/skips the earlier executable steps/)).toBeNull()

    await waitForPreflightReady()
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

    await openNewRun()
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

    await openNewRun()
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

    await openNewRun()
    await screen.findByLabelText('Run plan')
    choose('Run plan', 'plans/in-progress/demo.md')
    choose('Run workflow', 'managed')
    await waitForPreflightReady()
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

  it('renders canonical list progress without adding a context request for the row', async () => {
    const listed = { ...ownedRun, evidence: {}, plan_path: null, progress: canonicalListProgress() }
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [listed], next_cursor: null, schema_version: 1 })
    vi.mocked(api.getControlPlaneRun).mockResolvedValue(listed)

    renderDashboard()

    expect((await screen.findAllByText('4 / 11 approved')).length).toBeGreaterThan(0)
    expect(screen.getByText(/Implementing CP5 of 11/)).toBeDefined()
    expect(screen.getByText('Plan: /workspace/alpha/plans/run-owned.md · Run: run-owned')).toBeDefined()
    expect(api.getRunContext).toHaveBeenCalledTimes(1)
    expect(api.getRunContext).toHaveBeenCalledWith('control-project', 'run-owned', 'lite', false, expect.objectContaining({ signal: expect.any(AbortSignal) }))
  })

  it('inspects selected checkpoint history while preserving full diagnostics on refresh', async () => {
    const listed = { ...ownedRun, evidence: {}, plan_path: null, progress: canonicalListProgress() }
    const canonicalDetail = canonicalDetailProgress()
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [listed], next_cursor: null, schema_version: 1 })
    vi.mocked(api.getControlPlaneRun).mockResolvedValue(listed)
    vi.mocked(api.getRunContext).mockImplementation(async (_projectId, runId, level) => ({
      run_id: runId,
      level,
      schema_version: 1,
      data: { progress: canonicalDetail, ...(level === 'full' ? { marker: 'full-marker' } : {}) },
    }))

    renderDashboard()

    await screen.findByRole('button', { name: /Checkpoint 5: Active/ })
    fireEvent.click(screen.getByRole('button', { name: /Checkpoint 4: Reviewed/ }))
    expect(screen.getAllByRole('heading', { name: 'Checkpoint 4: Reviewed' }).length).toBeGreaterThan(0)
    expect(screen.getByText('Review rejection')).toBeDefined()

    openTechnicalDetails()
    fireEvent.click(screen.getByText('Raw details'))
    await screen.findByText(/full-marker/)
    const fullCallsBeforeRefresh = vi.mocked(api.getRunContext).mock.calls.filter(call => call[2] === 'full').length

    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    await waitFor(() => expect(vi.mocked(api.getRunContext).mock.calls.filter(call => call[2] === 'full').length).toBeGreaterThan(fullCallsBeforeRefresh))
    expect(screen.getByText(/full-marker/)).toBeDefined()
    expect(screen.getAllByRole('heading', { name: 'Checkpoint 4: Reviewed' }).length).toBeGreaterThan(0)
    expect(screen.getByRole('button', { name: 'Stop now…', exact: true })).toBeDefined()
    expect(screen.getByRole('button', { name: 'Stop after current turn', exact: true })).toBeDefined()
    expect(screen.getByRole('button', { name: 'Restart with changes' })).toBeDefined()
  })

  it('rejects a late checkpoint context response after selecting a different run', async () => {
    const otherRun = { ...ownedRun, run_id: 'run-other', status: 'completed', started_at: '2024-01-02T00:00:00Z' }
    const oldContext = deferred<RunContext>()
    const newContext = deferred<RunContext>()
    const oldProgress = canonicalDetailProgress({ events: [canonicalDetailProgress().events[3]] })
    const newProgress = canonicalDetailProgress({ events: [{ ...canonicalDetailProgress().events[3], reason: 'new context evidence' }] })
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [ownedRun, otherRun], next_cursor: null, schema_version: 1 })
    vi.mocked(api.getControlPlaneRun).mockImplementation(async (_projectId, runId) => runId === otherRun.run_id ? otherRun : ownedRun)
    vi.mocked(api.getRunContext).mockImplementation(async (_projectId, runId, _level) => {
      if (runId === otherRun.run_id) return newContext.promise
      return oldContext.promise
    })

    renderDashboard({ requestedRunId: ownedRun.run_id })
    await screen.findByRole('button', { name: /run-owned Running/ })
    await waitFor(() => expect(api.getRunContext).toHaveBeenCalledWith('control-project', 'run-owned', 'lite', false, expect.objectContaining({ signal: expect.any(AbortSignal) })))

    fireEvent.click(screen.getByRole('button', { name: /run-other Completed/ }))
    await screen.findByRole('button', { name: /run-other Completed/ })
    await waitFor(() => expect(api.getRunContext).toHaveBeenCalledWith('control-project', 'run-other', 'lite', false, expect.objectContaining({ signal: expect.any(AbortSignal) })))

    await act(async () => {
      newContext.resolve({ run_id: 'run-other', level: 'lite', schema_version: 1, data: { progress: newProgress } })
      await newContext.promise
    })
    await screen.findByText(/new context evidence/)

    await act(async () => {
      oldContext.resolve({ run_id: 'run-owned', level: 'lite', schema_version: 1, data: { progress: oldProgress } })
      await oldContext.promise
    })
    expect(screen.getByText(/new context evidence/)).toBeDefined()
    expect(screen.queryByText(/old context evidence/)).toBeNull()
  })

  it('copies the sanitized dashboard link and reports clipboard failure without hidden data', async () => {
    window.location.search = '?project=wrong&view=settings&extra=query-sentinel'
    window.location.hash = '#fragment-sentinel'
    const listReady = deferred<{ runs: (typeof ownedRun)[]; next_cursor: null; schema_version: number }>()
    const detailReady = deferred<typeof ownedRun>()
    const confirmedRun = { ...ownedRun, current_step: 'review' }
    vi.mocked(api.listControlPlaneRuns).mockImplementationOnce(() => listReady.promise)
    vi.mocked(api.getControlPlaneRun).mockImplementationOnce(() => detailReady.promise)
    const onRunSelectionChange = vi.fn()
    const writeText = vi.fn()
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    renderDashboard({ onRunSelectionChange })

    await act(async () => {
      listReady.resolve({ runs: [ownedRun], next_cursor: null, schema_version: 1 })
      await listReady.promise
    })
    await screen.findByRole('button', { name: /run-owned Running/ })
    expect(screen.queryByText('Review · 2')).toBeNull()
    await waitFor(() => expect(onRunSelectionChange).toHaveBeenCalledWith({ runId: 'run-owned', userInitiated: false }))
    await waitFor(() => expect(api.getControlPlaneRun).toHaveBeenCalledTimes(1))
    await act(async () => {
      detailReady.resolve(confirmedRun)
      await detailReady.promise
    })
    await screen.findByText('Review · 2')

    const copyReady = deferred<void>()
    writeText.mockImplementationOnce(() => copyReady.promise)
    fireEvent.click(screen.getByRole('button', { name: 'Copy link' }))
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1))
    expect(screen.queryByText('Link copied to the clipboard.')).toBeNull()
    await act(async () => {
      copyReady.resolve()
      await copyReady.promise
    })
    expect(await screen.findByText('Link copied to the clipboard.')).toBeDefined()
    expect(writeText).toHaveBeenCalledTimes(1)
    expect(writeText).toHaveBeenCalledWith(window.location.origin + window.location.pathname + '?project=control-project&view=runs&run=run-owned')

    writeText.mockRejectedValueOnce(new Error('clipboard denied'))
    fireEvent.click(screen.getByRole('button', { name: 'Copy link' }))
    const failure = await screen.findByText(/Clipboard access failed/)
    expect(failure.textContent).not.toContain('http')
    expect(failure.textContent).not.toContain('token')
  })

  it('does not report an older copy after the selected run changes', async () => {
    const otherRun = {
      ...ownedRun,
      run_id: 'run-other',
      status: 'completed',
      plan_path: 'plans/in-progress/other.md',
      current_step: 'review',
      evidence: { ...ownedRun.evidence, manifest_created_at: '2023-12-31T00:00:00Z' },
    }
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [ownedRun, otherRun], next_cursor: null, schema_version: 1 })
    vi.mocked(api.getControlPlaneRun).mockImplementation(async (_projectId, runId) => runId === otherRun.run_id ? otherRun : ownedRun)
    const copyReady = deferred<void>()
    const writeText = vi.fn().mockImplementation(() => copyReady.promise)
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    renderDashboard()
    await screen.findByRole('button', { name: 'run-owned' })

    fireEvent.click(screen.getByRole('button', { name: 'Copy link' }))
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1))
    fireEvent.click(screen.getByRole('button', { name: /other\.md/ }))
    await screen.findByRole('button', { name: 'run-other' })
    expect(screen.queryByText('Link copied to the clipboard.')).toBeNull()

    await act(async () => {
      copyReady.resolve()
      await copyReady.promise
    })
    expect(screen.queryByText('Link copied to the clipboard.')).toBeNull()
    expect(screen.queryByText(/Clipboard access failed/)).toBeNull()
    expect(writeText).toHaveBeenCalledTimes(1)
    expect(writeText).toHaveBeenCalledWith(window.location.origin + window.location.pathname + '?project=control-project&view=runs&run=run-owned')
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

  it('separates pending control model details from the last executed turn evidence', async () => {
    const pendingRun = {
      ...ownedRun,
      evidence: {
        ...ownedRun.evidence,
        overrides: { state: 'pending', revision: 2, role_selectors: { worker: 'codex.pending' } },
      },
    }
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [pendingRun], next_cursor: null, schema_version: 1 })
    vi.mocked(api.getControlPlaneRun).mockResolvedValue(pendingRun)
    vi.mocked(api.listRunEvents).mockResolvedValue([{
      sequence: 1,
      event_type: 'turn_started',
      data: {
        turn_number: 4,
        step_name: 'implement',
        step_role: 'worker',
        resolved_model_display: 'executed-model',
      },
      schema_version: 1,
      timestamp: '2024-01-01T00:04:00Z',
    }])
    vi.mocked(api.postGlobalConfigForm).mockResolvedValue({
      ...emptyProjection,
      form: {
        ...emptyProjection.form,
        harnesses: { codex: { pending: { model: 'pending-model', effort: 'low' } } },
      },
    })

    renderDashboard()
    await waitFor(() => expect(screen.getByText(/Last executed/)).toBeDefined())
    expect(screen.getByText(/executed-model/)).toBeDefined()
    expect(screen.getByText(/codex\.pending · pending-model · effort low/)).toBeDefined()
    expect(screen.getByText(/applies at the next turn or on resume/)).toBeDefined()
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
      const stale = runId === 'run-owned'
      return {
        run_id: runId,
        level,
        data: {
          progress: observerProgress({
            checkpoint: { index: stale ? 4 : 5, name: stale ? 'Checkpoint 4: Stale' : 'Checkpoint 5: Fresh' },
          }),
          ...(stale ? { stale: true } : { status: 'fresh' }),
        },
        schema_version: 1,
        ...(level === 'full' ? { fullScope } : {}),
      }
    })
    renderDashboard()
    await screen.findByRole('button', { name: 'run-owned' })
    openTechnicalDetails()
    fireEvent.click(screen.getByText('Raw details'))
    await waitFor(() => expect(screen.getByText(/run run-owned/)).toBeDefined())
    fireEvent.click(screen.getByRole('button', { name: /other\.md/ }))
    await screen.findByText(/run run-other/)
    await screen.findByText('Checkpoint 5: Fresh (5 of 14)')
    expect(screen.queryByText('Checkpoint 4: Stale (4 of 14)')).toBeNull()
    await waitFor(() => expect(screen.getByText(/"status": "fresh"/)).toBeDefined())
    releaseOwned?.(null)
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(screen.queryByText(/"stale": true/)).toBeNull()
    expect(screen.queryByText('Checkpoint 4: Stale (4 of 14)')).toBeNull()
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
    await waitForPreflightReady()
    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))
    await screen.findByText('response lost')
    const original = vi.mocked(api.startControlPlaneRun).mock.calls[0]
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    fireEvent.click(screen.getByRole('button', { name: /run-other Completed/ }))
    await screen.findByRole('button', { name: 'run-other', exact: true })
    await openNewRun()
    await waitForPreflightReady()
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
    expect(screen.getByRole('button', { name: 'Stop after current turn', exact: true })).toBeDefined()
    expect(screen.getByRole('button', { name: 'Stop now…', exact: true })).toBeDefined()
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
    await waitFor(() => expect(api.getRunContext).toHaveBeenCalled())
    expect(await screen.findByText(/Progress unavailable/)).toBeDefined()
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
    await waitForPreflightReady()
    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))
    await waitFor(() => expect(api.startControlPlaneRun).toHaveBeenCalledTimes(1))
    expect(vi.mocked(api.startControlPlaneRun).mock.calls[0][1]).toEqual({
      plan_path: 'plans/in-progress/demo.md', team: 'full', max_turns: 12, dirty_worktree_confirmed: false,
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
    await waitForPreflightReady()
    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))
    await waitFor(() => expect(api.startControlPlaneRun).toHaveBeenCalledTimes(2))
    expect(vi.mocked(api.startControlPlaneRun).mock.calls[1][1]).toEqual({
      plan_path: 'plans/in-progress/demo.md', workflow_name: 'other', max_turns: 12, dirty_worktree_confirmed: false,
    })
  })

  it('uses the exact child default in the family/stage preview and preserves default omission', async () => {
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [], next_cursor: null, schema_version: 1 })
    vi.mocked(api.postGlobalConfigForm).mockResolvedValue({
      ...emptyProjection,
      form: {
        ...emptyProjection.form,
        default_workflow: 'managed',
        roles: { worker: 'codex.global', reviewer: 'codex.review' },
        teams: {
          product: {
            roles: { worker: 'codex.base' },
            display_name: 'Product',
            extends: null,
            upgrade_to: 'product_fast',
            effective_roles: { worker: 'codex.base', reviewer: 'codex.review' },
            role_sources: { worker: 'product', reviewer: 'global' },
          },
          product_fast: {
            roles: { worker: 'codex.fast' },
            display_name: 'Fast',
            extends: 'product',
            upgrade_to: null,
            effective_roles: { worker: 'codex.fast', reviewer: 'codex.review' },
            role_sources: { worker: 'product_fast', reviewer: 'global' },
          },
        },
        workflow_default_teams: { managed: 'product_fast' },
      },
    })
    vi.mocked(api.startControlPlaneRun).mockResolvedValue({
      result: { run_id: 'run-started', created: true, status: 'running', schema_version: 1, manifest_path: null, reason: null, restarted_from_run_id: null },
      startup_question: null,
    })

    renderDashboard()
    await openNewRun()
    choose('Run plan', 'plans/in-progress/demo.md')

    expect((screen.getByLabelText('Run team') as HTMLInputElement).value).toBe('Product')
    expect((screen.getByLabelText('Run team stage') as HTMLInputElement).value).toBe('Fast')
    const preview = screen.getByLabelText('Effective choices for this launch').textContent ?? ''
    expect(preview).toContain('Product — workflow default (Product fast)')
    expect(preview).toContain('baseline stage Fast (product_fast)')
    expect(preview).toContain('codex.fast')

    const familyInput = screen.getByLabelText('Run team')
    fireEvent.focus(familyInput)
    expect(screen.getByRole('option', { name: /Product.*Worker: codex\.base.*Upgrade route: Base → Fast/ })).toBeDefined()

    // Choosing a stage keeps the exact child ID in the request, not its
    // display name or the family root.
    choose('Run team stage', 'product_fast')
    await waitForPreflightReady({ team: 'product_fast' })
    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))
    await waitFor(() => expect(api.startControlPlaneRun).toHaveBeenCalledTimes(1))
    expect(api.startControlPlaneRun).toHaveBeenLastCalledWith(
      'control-project',
      expect.objectContaining({ plan_path: 'plans/in-progress/demo.md', team: 'product_fast' }),
      expect.stringMatching(/^start-/),
    )

    // Clearing the stage returns both selectors to the resolved child while
    // the request again omits team so the server applies that exact default.
    await openNewRun()
    const stageInput = screen.getByLabelText('Run team stage')
    fireEvent.focus(stageInput)
    fireEvent.change(stageInput, { target: { value: '' } })
    fireEvent.click(screen.getByRole('option', { name: /Use default \(Fast\)/ }))
    expect((screen.getByLabelText('Run team') as HTMLInputElement).value).toBe('Product')
    expect((screen.getByLabelText('Run team stage') as HTMLInputElement).value).toBe('Fast')
    await waitForPreflightReady()
    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))
    await waitFor(() => expect(api.startControlPlaneRun).toHaveBeenCalledTimes(2))
    expect(api.startControlPlaneRun).toHaveBeenLastCalledWith(
      'control-project',
      { plan_path: 'plans/in-progress/demo.md', dirty_worktree_confirmed: false },
      expect.stringMatching(/^start-/),
    )
  })

  it('does not infer worker upgrades from family membership', async () => {
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [], next_cursor: null, schema_version: 1 })
    vi.mocked(api.postGlobalConfigForm).mockResolvedValue({
      ...emptyProjection,
      form: {
        ...emptyProjection.form,
        default_workflow: 'managed',
        roles: { worker: 'codex.global', reviewer: 'codex.review' },
        teams: {
          product: {
            roles: { worker: 'codex.base' },
            display_name: 'Product',
            extends: null,
            upgrade_to: null,
            effective_roles: { worker: 'codex.base', reviewer: 'codex.review' },
            role_sources: { worker: 'product', reviewer: 'global' },
          },
          product_fast: {
            roles: { worker: 'codex.fast' },
            display_name: 'Fast',
            extends: 'product',
            upgrade_to: null,
            effective_roles: { worker: 'codex.fast', reviewer: 'codex.review' },
            role_sources: { worker: 'product_fast', reviewer: 'global' },
          },
        },
        workflow_default_teams: { managed: 'product_fast' },
      },
    })

    renderDashboard()
    await openNewRun()
    choose('Run plan', 'plans/in-progress/demo.md')

    expect((screen.getByLabelText('Run team') as HTMLInputElement).value).toBe('Product')
    expect((screen.getByLabelText('Run team stage') as HTMLInputElement).value).toBe('Fast')
    const preview = screen.getByLabelText('Effective choices for this launch')
    const chain = within(preview).getByText('Worker upgrade chain', { selector: 'dt' }).closest('div')!
    expect(chain.textContent).toContain('codex.fast')
    expect(chain.textContent).not.toContain('codex.base')

    fireEvent.focus(screen.getByLabelText('Run team'))
    expect(screen.getByRole('option', { name: /Product.*Members: Base, Fast/ })).toBeDefined()
    expect(screen.queryByRole('option', { name: /Product.*Upgrade route: Base → Fast/ })).toBeNull()
  })

  it('starts a selected child upgrade chain at that child and keeps its external target', async () => {
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [], next_cursor: null, schema_version: 1 })
    vi.mocked(api.postGlobalConfigForm).mockResolvedValue({
      ...emptyProjection,
      form: {
        ...emptyProjection.form,
        default_workflow: 'managed',
        roles: { worker: 'codex.global', reviewer: 'codex.review' },
        teams: {
          base: {
            roles: { worker: 'codex.base' },
            display_name: 'Base',
            extends: null,
            upgrade_to: 'middle',
            effective_roles: { worker: 'codex.base', reviewer: 'codex.review' },
            role_sources: { worker: 'base', reviewer: 'global' },
          },
          middle: {
            roles: { worker: 'codex.middle' },
            display_name: 'Middle',
            extends: 'base',
            upgrade_to: 'external',
            effective_roles: { worker: 'codex.middle', reviewer: 'codex.review' },
            role_sources: { worker: 'middle', reviewer: 'global' },
          },
          external: {
            roles: { worker: 'codex.external' },
            display_name: 'External',
            extends: null,
            upgrade_to: null,
            effective_roles: { worker: 'codex.external', reviewer: 'codex.review' },
            role_sources: { worker: 'external', reviewer: 'global' },
          },
        },
        workflow_default_teams: { managed: 'base' },
      },
    })

    renderDashboard()
    await openNewRun()
    choose('Run plan', 'plans/in-progress/demo.md')
    choose('Run team stage', 'middle')

    const preview = screen.getByLabelText('Effective choices for this launch')
    const chain = within(preview).getByText('Worker upgrade chain', { selector: 'dt' }).closest('div')!
    expect(chain.textContent).toContain('Middle')
    expect(chain.textContent).toContain('codex.middle')
    expect(chain.textContent).toContain('External')
    expect(chain.textContent).toContain('codex.external')
    expect(chain.textContent).not.toContain('Base')
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
