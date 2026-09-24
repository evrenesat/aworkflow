import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import type { ProjectInfo, RunProgressSummary, RunStatus } from '../types'
import { GlobalRunOverview } from './GlobalRunOverview'

vi.mock('../api', async () => {
  const actual = await vi.importActual<typeof import('../api')>('../api')
  return { ...actual, listControlPlaneRuns: vi.fn(), getControlPlaneRun: vi.fn() }
})

const primary: ProjectInfo = {
  id: 'primary', display_name: 'Primary project', current_path: '/srv/primary',
  is_git_root: true, registered_at: '2026-01-01T00:00:00Z', readiness: 'ready',
}
const child: ProjectInfo = {
  id: 'child', display_name: 'Feature worktree', current_path: '/srv/feature',
  is_git_root: true, registered_at: '2026-01-01T00:00:00Z', readiness: 'ready',
  parent_project_id: 'primary',
}
const childRun = {
  run_id: 'child-run',
  status: 'done',
  schema_version: 1,
  ownership: 'control_plane',
  revision: 1,
  history_state: 'visible',
  status_reason_code: 'completed',
  activity: 'inactive',
  evidence: {},
  plan_path: 'plans/done/child.md',
} as RunStatus

function makeRun(run_id: string, overrides: Record<string, unknown> = {}): RunStatus {
  return {
    run_id,
    status: 'done',
    schema_version: 1,
    ownership: 'control_plane',
    revision: 1,
    history_state: 'visible',
    status_reason_code: 'completed',
    activity: 'inactive',
    evidence: {},
    plan_path: `plans/${run_id}.md`,
    ...overrides,
  } as RunStatus
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((value, failure) => { resolve = value; reject = failure })
  return { promise, resolve, reject }
}

function page(runs: RunStatus[]) {
  return { runs, next_cursor: null, schema_version: 1 }
}

function canonicalProgress(overrides: Partial<RunProgressSummary> = {}): RunProgressSummary {
  const known = (value: number) => ({ value, coverage: 'complete' as const })
  return {
    schema_version: 1, availability: 'complete', observed_at: '2026-09-11T12:00:00Z', evidence_at: '2026-09-11T11:59:00Z',
    reason_codes: [], original_plan_identity: 'canonical-plan', original_plan_display_name: 'canonical-plan.md', original_plan_path: '/srv/plans/canonical-plan.md',
    total_checkpoints: known(11), approved_checkpoints: known(4), recorded_complete_checkpoints: known(4),
    current_checkpoint_id: 'cp-5', current_checkpoint_ordinal: 5, current_checkpoint_title: 'Checkpoint 5: Active',
    activity: 'active', phase: 'implementing', run_status: 'running', current_executor: null, last_executor: null,
    worker_attempts: known(2), repair_passes: known(0), reviews: known(1), runtime_retries: known(0), applied_upgrades: known(0),
    ...overrides,
  }
}

function matchingDetail(run: RunStatus, progress: RunProgressSummary | null = null): RunStatus {
  return { ...run, progress } as RunStatus
}

function canonicalDetail(run: RunStatus, overrides: Partial<RunProgressSummary> = {}): RunStatus {
  return matchingDetail(run, canonicalProgress({
    original_plan_display_name: run.original_plan_display_name ?? null,
    original_plan_path: run.original_plan_path ?? null,
    ...overrides,
  }))
}

describe('GlobalRunOverview project context', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [childRun], next_cursor: null, schema_version: 1 })
    vi.mocked(api.getControlPlaneRun).mockRejectedValue(new Error('detail unavailable'))
  })

  it('waits for the registry and current runs before making empty-state claims', async () => {
    const runs = deferred<ReturnType<typeof page>>()
    vi.mocked(api.listControlPlaneRuns).mockReturnValue(runs.promise)
    const view = render(<GlobalRunOverview projects={[]} registryLoading onOpen={vi.fn()} />)

    expect(screen.getByRole('status').textContent).toBe('Loading runs… Results are incomplete.')
    expect(view.container.querySelector('.global-run-results')?.getAttribute('aria-busy')).toBe('true')
    expect(view.container.querySelector('.global-run-loading-spinner')).toBeTruthy()
    expect(screen.queryByText(/No registered projects/)).toBeNull()
    expect(screen.queryByRole('heading', { name: /Ongoing/ })).toBeNull()
    expect(api.listControlPlaneRuns).not.toHaveBeenCalled()

    view.rerender(<GlobalRunOverview projects={[primary]} registryLoading={false} onOpen={vi.fn()} />)
    await waitFor(() => expect(api.listControlPlaneRuns).toHaveBeenCalledWith('primary', expect.anything(), expect.anything()))
    expect(screen.getByRole('status').textContent).toBe('Loading runs… Results are incomplete.')
    expect(screen.queryByText('No runs yet.')).toBeNull()
    expect(screen.queryByText('No ongoing runs.')).toBeNull()

    runs.resolve(page([makeRun('populated-run')]))
    expect(await screen.findByRole('button', { name: /populated-run/ })).toBeTruthy()
    expect(screen.queryByRole('status')).toBeNull()
    expect(view.container.querySelector('.global-run-results')?.getAttribute('aria-busy')).toBe('false')
  })

  it('renders a successful empty result only after the run request completes', async () => {
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue(page([]))
    render(<GlobalRunOverview projects={[primary]} onOpen={vi.fn()} />)

    expect(await screen.findByText('No runs yet.')).toBeTruthy()
    expect(screen.queryByRole('heading', { name: 'Ongoing (0)' })).toBeNull()
    expect(screen.queryByText('No ongoing runs.')).toBeNull()
    expect(screen.queryByRole('heading', { name: 'Recent (0)' })).toBeNull()
    expect(screen.queryByRole('heading', { name: 'Needs attention (0)' })).toBeNull()
    expect(screen.queryByRole('status')).toBeNull()
  })

  it('exits loading with a complete run failure instead of showing an empty state', async () => {
    vi.mocked(api.listControlPlaneRuns).mockRejectedValue(new Error('runs unavailable'))
    render(<GlobalRunOverview projects={[primary]} onOpen={vi.fn()} />)

    expect((await screen.findByRole('alert')).textContent).toMatch(/Run results unavailable/)
    expect(screen.queryByText('No runs yet.')).toBeNull()
    expect(screen.queryByRole('heading', { name: /Ongoing/ })).toBeNull()
    expect(screen.queryByRole('status')).toBeNull()
  })

  it('retains successful rows while reporting a partial run failure', async () => {
    const retained = makeRun('retained-run')
    vi.mocked(api.listControlPlaneRuns).mockImplementation(async projectId => {
      if (projectId === 'child') throw new Error('child unavailable')
      return page([retained])
    })
    render(<GlobalRunOverview projects={[primary, child]} onOpen={vi.fn()} />)

    expect(await screen.findByRole('button', { name: /retained-run/ })).toBeTruthy()
    expect(screen.getByRole('alert').textContent).toMatch(/Partial or stale results/)
    expect(screen.queryByText('No runs yet.')).toBeNull()
    expect(screen.queryByRole('heading', { name: 'Ongoing (0)' })).toBeNull()
  })

  it('clears the current result for history changes and keeps rows during refresh', async () => {
    const visible = makeRun('visible-run', { plan_path: 'plans/visible.md' })
    const archived = makeRun('archived-run', { history_state: 'archived', plan_path: 'plans/archived.md' })
    const refreshed = deferred<ReturnType<typeof page>>()
    let visibleCalls = 0
    let archivedCalls = 0
    vi.mocked(api.listControlPlaneRuns).mockImplementation(async (_projectId, options) => {
      if (options?.history === 'archived') {
        archivedCalls += 1
        return archivedCalls === 1 ? page([archived]) : refreshed.promise
      }
      visibleCalls += 1
      return visibleCalls === 1 ? page([visible]) : refreshed.promise
    })
    const view = render(<GlobalRunOverview projects={[primary]} onOpen={vi.fn()} />)
    expect(await screen.findByRole('button', { name: /visible-run/ })).toBeTruthy()

    fireEvent.change(screen.getByLabelText('Run history'), { target: { value: 'archived' } })
    expect(await screen.findByRole('button', { name: /archived-run/ })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /visible-run/ })).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    await waitFor(() => expect(screen.queryByRole('status')).toBeNull())
    // The refresh is for the selected history, so the usable archived row stays visible.
    expect(screen.getByRole('button', { name: /archived-run/ })).toBeTruthy()
    expect(view.container.querySelector('.global-run-results')?.getAttribute('aria-busy')).toBe('false')

    refreshed.resolve(page([makeRun('refreshed-run', { history_state: 'archived', plan_path: 'plans/refreshed.md' })]))
    expect(await screen.findByRole('button', { name: /refreshed-run/ })).toBeTruthy()
    expect(screen.queryByRole('status')).toBeNull()
  })

  it('keeps a history-gap row mounted across equal and changed refreshes and retains it on failure', async () => {
    const historicalGap = makeRun('refresh-history-gap', {
      status: 'needs_attention',
      status_reason_code: 'unit_missing',
      activity: 'unknown',
      unit_name: 'aflow-run-refresh-history-gap.service',
      evidence: { unit_observation: 'missing', has_run_metadata: false, can_resume: false },
      original_plan_display_name: 'Original history title',
      original_plan_path: 'plans/original-history-title.md',
    })
    const equalRefresh = deferred<ReturnType<typeof page>>()
    let calls = 0
    vi.mocked(api.listControlPlaneRuns).mockImplementation(() => {
      calls += 1
      if (calls === 1) return Promise.resolve(page([historicalGap]))
      if (calls === 2) return equalRefresh.promise
      if (calls === 3) return Promise.resolve(page([{
        ...historicalGap,
        original_plan_display_name: 'Updated history title',
        original_plan_path: 'plans/updated-history-title.md',
      }]))
      return Promise.reject(new Error('history refresh unavailable'))
    })

    const view = render(<GlobalRunOverview projects={[primary]} onOpen={vi.fn()} />)
    expect(await screen.findByText('Original history title')).toBeTruthy()
    const row = view.container.querySelector<HTMLElement>('.global-run-row')
    expect(row).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    await waitFor(() => expect(api.listControlPlaneRuns).toHaveBeenCalledTimes(2))
    expect(view.container.querySelector('.global-run-row')).toBe(row)
    await act(async () => {
      equalRefresh.resolve(page([historicalGap]))
      await equalRefresh.promise
    })
    expect(view.container.querySelector('.global-run-row')).toBe(row)

    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    await screen.findByText('Updated history title')
    expect(view.container.querySelector('.global-run-row')).toBe(row)

    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    expect((await screen.findByRole('alert')).textContent).toMatch(/Partial or stale results/)
    expect(screen.getByText('Updated history title')).toBeTruthy()
    expect(view.container.querySelector('.global-run-row')).toBe(row)
  })

  it('stops initial loading and exposes a registry failure', () => {
    const view = render(<GlobalRunOverview projects={[]} registryError="registry unavailable" onOpen={vi.fn()} />)

    expect(screen.getByRole('alert').textContent).toMatch(/Project list unavailable: registry unavailable/)
    expect(screen.queryByRole('status')).toBeNull()
    expect(view.container.querySelector('.global-run-results')?.getAttribute('aria-busy')).toBe('false')
    expect(screen.queryByRole('heading', { name: /Ongoing/ })).toBeNull()
  })

  it('labels child history with the parent while opening the exact child ID', async () => {
    const onOpen = vi.fn()
    render(<GlobalRunOverview projects={[primary, child]} onOpen={onOpen} />)
    const row = await screen.findByRole('button', { name: /Primary project · Worktree: Feature worktree · Completed/ })
    fireEvent.click(row)
    expect(onOpen).toHaveBeenCalledWith('child', 'child-run')
    expect(api.listControlPlaneRuns).toHaveBeenCalledWith('child', expect.anything(), expect.anything())
  })

  it('orders groups, paginates attention, searches loaded rows, and opens exact identities', async () => {
    const ongoing = makeRun('run-ongoing', {
      status: 'waiting_for_input', activity: 'inactive',
      plan_path: '/srv/plans/long_plan-name.md', started_at: '2026-09-10T10:00:00Z',
    })
    const recent = makeRun('run-recent', { ended_at: '2026-09-10T09:00:00Z' })
    const attention = Array.from({ length: 21 }, (_, index) => makeRun(`attention-${index + 1}`, {
      status: 'needs_attention', status_reason_code: 'worker_attention',
      ended_at: `2026-09-${String(10 - Math.floor(index / 9)).padStart(2, '0')}T${String(20 - index).padStart(2, '0')}:00:00Z`,
    }))
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [ongoing, recent, ...attention], next_cursor: null, schema_version: 1 })
    const onOpen = vi.fn()

    render(<GlobalRunOverview projects={[primary]} onOpen={onOpen} />)

    expect(await screen.findByRole('heading', { name: 'Ongoing (1)' })).toBeTruthy()
    expect(screen.getByRole('heading', { name: 'Recent (1)' })).toBeTruthy()
    expect(screen.getByRole('heading', { name: 'Needs attention (21)' })).toBeTruthy()
    expect(screen.getByText('Long plan name')).toBeTruthy()
    expect(screen.getAllByRole('button', { name: /attention-/ })).toHaveLength(10)

    fireEvent.click(screen.getByRole('button', { name: 'Show more (11 remaining)' }))
    expect(screen.getAllByRole('button', { name: /attention-/ })).toHaveLength(20)
    fireEvent.click(screen.getByRole('button', { name: 'Show more (1 remaining)' }))
    expect(screen.getAllByRole('button', { name: /attention-/ })).toHaveLength(21)
    expect(screen.queryByRole('button', { name: /Show more/ })).toBeNull()

    const search = screen.getByRole('searchbox', { name: 'Search loaded runs' })
    fireEvent.change(search, { target: { value: 'attention-21' } })
    expect(await screen.findByRole('heading', { name: 'Needs attention (1)' })).toBeTruthy()
    expect(screen.getAllByRole('button', { name: /attention-21/ })).toHaveLength(1)
    expect(screen.queryByRole('button', { name: /Show more/ })).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: /attention-21/ }))
    expect(onOpen).toHaveBeenCalledWith('primary', 'attention-21')

    fireEvent.change(search, { target: { value: '' } })
    await waitFor(() => expect(screen.getAllByRole('button', { name: /attention-/ })).toHaveLength(10))
  })

  it('shows unresolved outcomes in a separate history group while keeping attention and archive filters intact', async () => {
    const historicalGap = makeRun('20260911t140026z-d06cc7d7', {
      status: 'needs_attention', status_reason_code: 'unit_missing',
      reason: 'No current workflow unit was found and no normal outcome was recorded.',
      activity: 'unknown', unit_name: 'aflow-run-20260911t140026z-d06cc7d7.service', ended_at: null,
      evidence: { unit_observation: 'missing', has_run_metadata: false, can_resume: false },
    })
    const archivedGap = makeRun('archived-gap', {
      ...historicalGap,
      run_id: 'archived-gap',
      unit_name: 'aflow-run-archived-gap.service',
      history_state: 'archived',
    })
    const actionable = makeRun('actionable-attention', {
      status: 'needs_attention', status_reason_code: 'worker_attention',
    })
    const recent = makeRun('recent-record')
    const deleted = makeRun('deleted-gap', {
      ...historicalGap,
      run_id: 'deleted-gap',
      unit_name: 'aflow-run-deleted-gap.service',
      history_state: 'deleted',
    })
    const allHistory = [historicalGap, archivedGap, actionable, recent, deleted]
    vi.mocked(api.listControlPlaneRuns).mockImplementation(async (_projectId, request) => ({
      runs: request?.history === 'all' ? allHistory : [historicalGap, actionable, recent, deleted],
      next_cursor: null,
      schema_version: 1,
    }))
    const onOpen = vi.fn()

    render(<GlobalRunOverview projects={[primary]} onOpen={onOpen} />)

    expect(await screen.findByRole('heading', { name: 'Outcome not recorded (1)' })).toBeTruthy()
    expect(screen.getByRole('heading', { name: 'Needs attention (1)' })).toBeTruthy()
    expect(screen.getByRole('heading', { name: 'Recent (1)' })).toBeTruthy()
    expect(screen.getByRole('button', { name: /Outcome not recorded/ })).toBeTruthy()
    expect(screen.getByRole('button', { name: /Needs attention.*actionable-attention/ })).toBeTruthy()

    fireEvent.change(screen.getByLabelText('Run history'), { target: { value: 'all' } })
    expect(await screen.findByRole('heading', { name: 'Outcome not recorded (2)' })).toBeTruthy()
    const archivedRow = screen.getByRole('button', { name: /Outcome not recorded.*archived-gap/ })
    expect(archivedRow.textContent).toContain('Archived')

    fireEvent.change(screen.getByRole('searchbox', { name: 'Search loaded runs' }), { target: { value: 'Outcome not recorded' } })
    expect(await screen.findByRole('heading', { name: 'Outcome not recorded (2)' })).toBeTruthy()
    expect(screen.queryByRole('heading', { name: /Needs attention/ })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /Outcome not recorded.*archived-gap/ }))
    expect(onOpen).toHaveBeenCalledWith('primary', 'archived-gap')
  })

  it('searches a loaded recent run outside the recent display limit', async () => {
    const runs = Array.from({ length: 11 }, (_, index) => makeRun(`recent-${index + 1}`, {
      ended_at: `2026-09-${String(20 - index).padStart(2, '0')}T00:00:00Z`,
    }))
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue(page(runs))

    render(<GlobalRunOverview projects={[primary]} onOpen={vi.fn()} />)
    expect(await screen.findByRole('heading', { name: 'Recent (10)' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /recent-11/ })).toBeNull()

    fireEvent.change(screen.getByRole('searchbox', { name: 'Search loaded runs' }), { target: { value: 'recent-11' } })
    expect(await screen.findByRole('button', { name: /recent-11/ })).toBeTruthy()
    expect(screen.getByRole('heading', { name: 'Recent (1)' })).toBeTruthy()
  })

  it('requests and displays the selected archived history without changing run identity', async () => {
    const visible = makeRun('visible-run', { plan_path: 'plans/visible.md' })
    const archived = makeRun('archived-run', { history_state: 'archived', plan_path: 'plans/archived.md' })
    vi.mocked(api.listControlPlaneRuns).mockImplementation(async (_projectId, options) => ({
      runs: options?.history === 'archived' ? [archived] : [visible],
      next_cursor: null,
      schema_version: 1,
    }))

    render(<GlobalRunOverview projects={[primary]} onOpen={vi.fn()} />)
    expect(await screen.findByRole('button', { name: /Primary project · Completed · Visible/ })).toBeTruthy()

    fireEvent.change(screen.getByLabelText('Run history'), { target: { value: 'archived' } })
    expect(await screen.findByRole('button', { name: /Primary project · Completed · Archived/ })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /Primary project · Completed · Visible · visible-run/ })).toBeNull()
    expect(screen.queryByText('Plan: plans/archived.md · Run: archived-run')).toBeNull()
    expect(screen.queryByText(/^Duration /)).toBeNull()
    expect(api.listControlPlaneRuns).toHaveBeenLastCalledWith('primary', expect.objectContaining({ history: 'archived' }), expect.anything())
  })

  it('keeps identical basenames distinct across projects', async () => {
    const primaryRun = makeRun('primary-run', { plan_path: 'plans/shared.md' })
    const childRunWithSameName = makeRun('child-run-same-name', { plan_path: 'plans/shared.md' })
    vi.mocked(api.listControlPlaneRuns).mockImplementation(async (projectId) => ({
      runs: [projectId === 'primary' ? primaryRun : childRunWithSameName],
      next_cursor: null,
      schema_version: 1,
    }))
    const onOpen = vi.fn()

    render(<GlobalRunOverview projects={[primary, child]} onOpen={onOpen} />)
    expect(await screen.findByRole('button', { name: /Primary project · Completed · Shared/ })).toBeTruthy()
    expect(screen.getByRole('button', { name: /Primary project · Worktree: Feature worktree · Completed · Shared/ })).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: /child-run-same-name/ }))
    expect(onOpen).toHaveBeenCalledWith('child', 'child-run-same-name')
  })

  it('shows canonical compact progress while retaining exact project and run navigation', async () => {
    const onOpen = vi.fn()
    const run = makeRun('canonical-run', {
      plan_path: null,
      progress: canonicalProgress(),
      status: 'running',
      activity: 'active',
      started_at: '2026-09-11T11:00:00Z',
    })
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue(page([run]))

    render(<GlobalRunOverview projects={[primary]} onOpen={onOpen} />)

    expect(await screen.findByText('4/11 approved')).toBeTruthy()
    expect(screen.queryByText(/CP5 of 11 · Implementing/)).toBeNull()
    expect(screen.queryByText('Plan: /srv/plans/canonical-plan.md · Run: canonical-run')).toBeNull()
    expect(screen.queryByText(/^Duration /)).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /canonical-run/ }))
    expect(onOpen).toHaveBeenCalledWith('primary', 'canonical-run')
    expect(api.listControlPlaneRuns).toHaveBeenCalledTimes(1)
  })

  it('keeps first-page rows usable but admits only final rendered targets after traversal', async () => {
    const early = makeRun('fast-first', { ended_at: '2026-01-01T00:00:00Z', original_plan_display_name: 'Early searchable' })
    const finalRuns = Array.from({ length: 10 }, (_, index) => makeRun(`final-${index + 1}`, {
      ended_at: `2026-09-${String(20 - index).padStart(2, '0')}T00:00:00Z`,
    }))
    const laterPrimary = deferred<ReturnType<typeof page>>()
    const slowChild = deferred<ReturnType<typeof page>>()
    let primaryCalls = 0
    vi.mocked(api.listControlPlaneRuns).mockImplementation((projectId) => {
      if (projectId === 'primary') {
        primaryCalls += 1
        return primaryCalls === 1
          ? Promise.resolve({ runs: [early], next_cursor: 'older', schema_version: 1 })
          : laterPrimary.promise
      }
      return slowChild.promise
    })
    vi.mocked(api.getControlPlaneRun).mockImplementation(async (projectId, runId) => {
      const run = finalRuns.find(candidate => candidate.run_id === runId) ?? makeRun('slow-child')
      return canonicalDetail(run, { original_plan_path: run.original_plan_path ?? null })
    })

    const onOpen = vi.fn()
    render(<GlobalRunOverview projects={[primary, child]} onOpen={onOpen} />)
    const earlyRow = await screen.findByRole('button', { name: /Early searchable.*fast-first/ })
    expect(screen.getByRole('status').textContent).toBe('Loading runs… Results are incomplete.')
    expect(screen.getByRole('heading', { name: 'Loaded recent (1)' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /slow-child/ })).toBeNull()
    expect(api.getControlPlaneRun).not.toHaveBeenCalled()

    const search = screen.getByRole('searchbox', { name: 'Search loaded runs' })
    fireEvent.change(search, { target: { value: 'Early searchable' } })
    fireEvent.click(earlyRow)
    expect(onOpen).toHaveBeenCalledWith('primary', early.run_id)
    expect(api.getControlPlaneRun).not.toHaveBeenCalled()
    fireEvent.change(search, { target: { value: '' } })

    laterPrimary.resolve(page(finalRuns))
    await waitFor(() => expect(screen.queryByRole('button', { name: /fast-first/ })).toBeNull())
    expect(api.getControlPlaneRun).not.toHaveBeenCalled()
    slowChild.resolve(page([makeRun('slow-child')]))
    await waitFor(() => expect(screen.queryByRole('status')).toBeNull())
    await waitFor(() => expect(api.getControlPlaneRun).toHaveBeenCalled())
    expect(api.getControlPlaneRun).not.toHaveBeenCalledWith('primary', early.run_id, expect.anything())
    expect(vi.mocked(api.getControlPlaneRun).mock.calls.every(([, runId]) => runId !== early.run_id)).toBe(true)
  })

  it('releases usable current rows after another project traversal fails', async () => {
    const usable = makeRun('usable-after-failure')
    const failedProject = deferred<ReturnType<typeof page>>()
    vi.mocked(api.listControlPlaneRuns).mockImplementation(projectId => (
      projectId === 'primary' ? Promise.resolve(page([usable])) : failedProject.promise
    ))
    vi.mocked(api.getControlPlaneRun).mockResolvedValue(canonicalDetail(usable, {
      total_checkpoints: { value: 1, coverage: 'complete' },
      approved_checkpoints: { value: 1, coverage: 'complete' },
    }))

    render(<GlobalRunOverview projects={[primary, child]} onOpen={vi.fn()} />)
    const row = await screen.findByRole('button', { name: /usable-after-failure/ })
    expect(api.getControlPlaneRun).not.toHaveBeenCalled()

    failedProject.reject(new Error('child unavailable'))
    expect((await screen.findByRole('alert')).textContent).toMatch(/Partial or stale results/)
    await waitFor(() => expect(api.getControlPlaneRun).toHaveBeenCalledWith('primary', usable.run_id, { signal: expect.anything() }))
    await waitFor(() => expect(row.getAttribute('data-enrichment-state')).toBe('settled'))
    expect(row.textContent).toContain('1/1 approved')
  })

  it('retains old ongoing and attention rows until refresh coverage completes', async () => {
    const oldOngoing = makeRun('ongoing-old', { status: 'paused', activity: 'active' })
    const oldAttention = makeRun('attention-old', { status: 'needs_attention', status_reason_code: 'worker_attention' })
    const refreshFirst = deferred<ReturnType<typeof page>>()
    const refreshLater = deferred<ReturnType<typeof page>>()
    let calls = 0
    vi.mocked(api.listControlPlaneRuns).mockImplementation(() => {
      calls += 1
      if (calls === 1) return Promise.resolve(page([oldOngoing, oldAttention]))
      if (calls === 2) return refreshFirst.promise
      return refreshLater.promise
    })

    render(<GlobalRunOverview projects={[primary]} onOpen={vi.fn()} />)
    expect(await screen.findByRole('button', { name: /ongoing-old/ })).toBeTruthy()
    expect(screen.getByRole('button', { name: /attention-old/ })).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    await waitFor(() => expect(screen.queryByRole('status')).toBeNull())
    expect(screen.getByRole('button', { name: /ongoing-old/ })).toBeTruthy()
    expect(screen.getByRole('button', { name: /attention-old/ })).toBeTruthy()

    refreshFirst.resolve({ runs: [makeRun('ongoing-new', { status: 'running', activity: 'active' })], next_cursor: 'older', schema_version: 1 })
    expect(await screen.findByRole('button', { name: /ongoing-new/ })).toBeTruthy()
    expect(screen.getByRole('button', { name: /attention-old/ })).toBeTruthy()

    refreshLater.resolve(page([makeRun('attention-new', { status: 'needs_attention', status_reason_code: 'worker_attention' })]))
    expect(await screen.findByRole('button', { name: /attention-new/ })).toBeTruthy()
    await waitFor(() => expect(screen.queryByRole('status')).toBeNull())
    expect(screen.queryByRole('button', { name: /ongoing-old/ })).toBeNull()
    expect(screen.queryByRole('button', { name: /attention-old/ })).toBeNull()
  })

  it('rejects stale pages after history and project changes', async () => {
    const visiblePage = deferred<ReturnType<typeof page>>()
    const archivedPage = deferred<ReturnType<typeof page>>()
    const childPage = deferred<ReturnType<typeof page>>()
    vi.mocked(api.listControlPlaneRuns).mockImplementation((projectId, options) => {
      if (options?.history === 'visible') return visiblePage.promise
      if (projectId === 'primary') return archivedPage.promise
      return childPage.promise
    })
    const view = render(<GlobalRunOverview projects={[primary]} onOpen={vi.fn()} />)
    await waitFor(() => expect(api.listControlPlaneRuns).toHaveBeenCalledWith('primary', expect.objectContaining({ history: 'visible' }), expect.anything()))

    fireEvent.change(screen.getByLabelText('Run history'), { target: { value: 'archived' } })
    await waitFor(() => expect(api.listControlPlaneRuns).toHaveBeenCalledWith('primary', expect.objectContaining({ history: 'archived' }), expect.anything()))

    visiblePage.resolve(page([makeRun('stale-visible')]))
    await Promise.resolve()
    expect(screen.queryByRole('button', { name: /stale-visible/ })).toBeNull()
    expect(api.getControlPlaneRun).not.toHaveBeenCalled()

    view.rerender(<GlobalRunOverview projects={[child]} onOpen={vi.fn()} />)
    await waitFor(() => expect(api.listControlPlaneRuns).toHaveBeenCalledWith('child', expect.objectContaining({ history: 'archived' }), expect.anything()))
    archivedPage.resolve(page([makeRun('stale-archived', { history_state: 'archived' })]))
    await Promise.resolve()
    expect(screen.queryByRole('button', { name: /stale-archived/ })).toBeNull()
    expect(api.getControlPlaneRun).not.toHaveBeenCalled()

    childPage.resolve(page([makeRun('current-child', { history_state: 'archived' })]))
    expect(await screen.findByRole('button', { name: /current-child/ })).toBeTruthy()
    await waitFor(() => expect(screen.queryByRole('status')).toBeNull())
    await waitFor(() => expect(api.getControlPlaneRun).toHaveBeenCalledWith('child', 'current-child', { signal: expect.anything() }))
  })

  it('retains partial pages on failure without complete empty claims', async () => {
    let calls = 0
    vi.mocked(api.listControlPlaneRuns).mockImplementation(async () => {
      calls += 1
      if (calls === 1) return { runs: [makeRun('partial-ongoing', { status: 'paused', activity: 'active' })], next_cursor: 'later', schema_version: 1 }
      throw new Error('later page unavailable')
    })

    render(<GlobalRunOverview projects={[primary]} onOpen={vi.fn()} />)
    expect(await screen.findByRole('button', { name: /partial-ongoing/ })).toBeTruthy()
    expect((await screen.findByRole('alert')).textContent).toMatch(/Partial or stale results/)
    expect(screen.queryByRole('status')).toBeNull()
    expect(screen.getByRole('heading', { name: 'Loaded ongoing (1)' })).toBeTruthy()
    expect(screen.queryByText('No runs yet.')).toBeNull()
    expect(screen.queryByText('No ongoing runs.')).toBeNull()
  })

  it('caps visible enrichment, keeps all ongoing rows, and does not self-restart settled work', async () => {
    const runs = Array.from({ length: 6 }, (_, index) => makeRun(`ongoing-${index + 1}`, {
      status: 'running', activity: 'active', current_step: 'implement',
      started_at: `2026-09-1${index}T00:00:00Z`,
    }))
    const pending = new Map<string, ReturnType<typeof deferred<RunStatus>>>()
    let active = 0
    let maximum = 0
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue(page(runs))
    vi.mocked(api.getControlPlaneRun).mockImplementation(async (_projectId, runId) => {
      const request = deferred<RunStatus>()
      pending.set(runId, request)
      active += 1
      maximum = Math.max(maximum, active)
      return request.promise.finally(() => { active -= 1 })
    })

    render(<GlobalRunOverview projects={[primary]} onOpen={vi.fn()} />)
    expect(await screen.findByRole('heading', { name: 'Ongoing (6)' })).toBeTruthy()
    await waitFor(() => expect(api.getControlPlaneRun).toHaveBeenCalledTimes(4))
    expect(maximum).toBeLessThanOrEqual(4)
    expect(screen.getAllByRole('button', { name: /ongoing-/ })).toHaveLength(6)

    for (const call of vi.mocked(api.getControlPlaneRun).mock.calls.slice(0, 4)) {
      const run = runs.find(item => item.run_id === call[1])!
      pending.get(run.run_id)?.resolve(matchingDetail(run))
    }
    await waitFor(() => expect(api.getControlPlaneRun).toHaveBeenCalledTimes(6))
    for (const run of runs) pending.get(run.run_id)?.resolve(matchingDetail(run))
    await waitFor(() => expect(screen.getAllByRole('button', { name: /ongoing-/ }).every(row => row.getAttribute('data-enrichment-state') === 'settled')).toBe(true))
    expect(api.getControlPlaneRun).toHaveBeenCalledTimes(6)
    expect(maximum).toBeLessThanOrEqual(4)
  })

  it('searches a hidden raw canonical title before admitting one exact enrichment request', async () => {
    const target = makeRun('hidden-canonical', {
      ended_at: '2026-01-01T00:00:00Z',
      original_plan_display_name: 'Hidden original title',
      original_plan_path: 'plans/in-progress/original.md',
      plan_path: 'plans/in-progress/active-overlay.md',
    })
    const visible = Array.from({ length: 10 }, (_, index) => makeRun(`recent-${index + 1}`, {
      ended_at: `2026-09-${String(20 - index).padStart(2, '0')}T00:00:00Z`,
    }))
    const targetResponse = deferred<RunStatus>()
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue(page([...visible, target]))
    vi.mocked(api.getControlPlaneRun).mockImplementation(async (_projectId, runId) => {
      if (runId === target.run_id) return targetResponse.promise
      return matchingDetail(visible.find(run => run.run_id === runId)!)
    })

    const onOpen = vi.fn()
    render(<GlobalRunOverview projects={[primary]} onOpen={onOpen} />)
    expect(await screen.findByRole('heading', { name: 'Recent (10)' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /hidden-canonical/ })).toBeNull()
    expect(api.getControlPlaneRun).not.toHaveBeenCalledWith('primary', target.run_id, expect.anything())

    fireEvent.change(screen.getByRole('searchbox', { name: 'Search loaded runs' }), { target: { value: 'Hidden original title' } })
    const row = await screen.findByRole('button', { name: /Hidden original title.*hidden-canonical/ })
    await waitFor(() => expect(api.getControlPlaneRun).toHaveBeenCalledWith('primary', target.run_id, { signal: expect.anything() }))
    expect(row.getAttribute('data-enrichment-state')).toBe('loading')
    expect(row.getAttribute('aria-label')).toContain('Loading checkpoint progress…')
    expect(row.querySelector('.compact-run-progress-row-notice')).toBeNull()
    expect(row.querySelector('.compact-run-progress-row-line .sr-only')?.textContent).toBe('Loading checkpoint progress…')

    targetResponse.resolve(canonicalDetail(target, { total_checkpoints: { value: 1, coverage: 'complete' }, approved_checkpoints: { value: 1, coverage: 'complete' } }))
    await waitFor(() => expect(row.getAttribute('data-enrichment-state')).toBe('settled'))
    expect(row.textContent).toContain('1/1 approved')
    fireEvent.click(row)
    expect(onOpen).toHaveBeenCalledWith('primary', target.run_id)
  })

  it.each([
    ['status', (run: RunStatus) => ({ ...run, status: 'running' })],
    ['activity', (run: RunStatus) => ({ ...run, activity: 'active' })],
    ['current step', (run: RunStatus) => ({ ...run, current_step: 'review' })],
    ['history revision', (run: RunStatus) => ({ ...run, history_revision: 2 })],
    ['canonical identity', (run: RunStatus) => ({
      ...run,
      progress: canonicalProgress({ original_plan_display_name: 'Other title', original_plan_path: 'plans/other.md' }),
    })],
  ])('settles a %s mismatch as visible stale without retrying', async (_label, mismatch) => {
    const run = makeRun('stale-run', {
      original_plan_display_name: 'Expected title', original_plan_path: 'plans/expected.md',
    })
    const response = deferred<RunStatus>()
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue(page([run]))
    vi.mocked(api.getControlPlaneRun).mockReturnValue(response.promise)

    render(<GlobalRunOverview projects={[primary]} onOpen={vi.fn()} />)
    const row = await screen.findByRole('button', { name: /Expected title.*stale-run/ })
    await waitFor(() => expect(api.getControlPlaneRun).toHaveBeenCalledTimes(1))
    response.resolve(mismatch(run) as RunStatus)
    await waitFor(() => expect(row.getAttribute('data-enrichment-state')).toBe('stale'))
    expect(row.textContent).toContain('Run changed; refresh to update checkpoint progress.')
    expect(api.getControlPlaneRun).toHaveBeenCalledTimes(1)
  })

  it.each([
    ['stale', 'Run changed; refresh to update checkpoint progress.'],
    ['failed', 'Checkpoint progress unavailable — Refresh to retry.'],
  ] as const)('keeps the last known projection visible with a sighted %s enrichment notice', async (state, message) => {
    const initial = makeRun('retained-enrichment', {
      status: 'running', activity: 'active', revision: 1,
      original_plan_display_name: 'Retained progress', original_plan_path: 'plans/retained.md',
    })
    const replacement = { ...initial, revision: 2 }
    const initialResponse = deferred<RunStatus>()
    const replacementResponse = deferred<RunStatus>()
    let listCalls = 0
    let detailCalls = 0
    vi.mocked(api.listControlPlaneRuns).mockImplementation(async () => {
      listCalls += 1
      return page([listCalls === 1 ? initial : replacement])
    })
    vi.mocked(api.getControlPlaneRun).mockImplementation(async () => {
      detailCalls += 1
      return detailCalls === 1 ? initialResponse.promise : replacementResponse.promise
    })

    render(<GlobalRunOverview projects={[primary]} onOpen={vi.fn()} />)
    const row = await screen.findByRole('button', { name: /Retained progress.*retained-enrichment/ })
    await waitFor(() => expect(detailCalls).toBe(1))
    initialResponse.resolve(canonicalDetail(initial, {
      total_checkpoints: { value: 2, coverage: 'complete' },
      approved_checkpoints: { value: 1, coverage: 'complete' },
    }))
    await waitFor(() => expect(row.textContent).toContain('1/2 approved'))

    fireEvent.click(screen.getByRole('button', { name: 'Refresh', exact: true }))
    await waitFor(() => expect(listCalls).toBe(2))
    await waitFor(() => expect(detailCalls).toBe(2))
    if (state === 'stale') replacementResponse.resolve({ ...replacement, revision: 1 } as RunStatus)
    else replacementResponse.reject(new Error('temporary detail failure'))

    await waitFor(() => expect(row.getAttribute('data-enrichment-state')).toBe(state))
    expect(row.textContent).toContain('1/2 approved')
    expect(row.querySelector('.compact-run-progress-row-notice')?.textContent).toBe(message)
  })

  it('keeps a terminal stale mismatch from retrying after raw traversal settles', async () => {
    const staleRun = makeRun('review-stale', {
      original_plan_display_name: 'Review stale', original_plan_path: 'plans/review-stale.md',
    })
    const laterRun = makeRun('later-raw', { ended_at: '2026-09-12T00:00:00Z' })
    const laterPage = deferred<ReturnType<typeof page>>()
    const mismatchResponse = deferred<RunStatus>()
    const originalVisibility = Object.getOwnPropertyDescriptor(document, 'visibilityState')
    let view: ReturnType<typeof render> | null = null
    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' })
    vi.mocked(api.listControlPlaneRuns)
      .mockImplementationOnce(async () => ({ runs: [staleRun], next_cursor: 'later', schema_version: 1 }))
      .mockImplementationOnce(async () => laterPage.promise)
    vi.mocked(api.getControlPlaneRun).mockImplementation(async (_projectId, runId) => {
      if (runId === staleRun.run_id) return mismatchResponse.promise
      return matchingDetail(laterRun)
    })

    try {
      view = render(<GlobalRunOverview projects={[primary]} onOpen={vi.fn()} />)
      const row = await screen.findByRole('button', { name: /Review stale.*review-stale/ })
      expect(api.getControlPlaneRun).not.toHaveBeenCalled()

      await act(async () => {
        laterPage.resolve(page([laterRun]))
        await laterPage.promise
      })
      await waitFor(() => expect(api.getControlPlaneRun).toHaveBeenCalledWith('primary', laterRun.run_id, { signal: expect.anything() }))
      await waitFor(() => expect(api.getControlPlaneRun).toHaveBeenCalledWith('primary', staleRun.run_id, { signal: expect.anything() }))
      mismatchResponse.resolve({ ...staleRun, status: 'running' } as RunStatus)
      await waitFor(() => expect(row.getAttribute('data-enrichment-state')).toBe('stale'))
      expect(vi.mocked(api.getControlPlaneRun).mock.calls.filter(([, runId]) => runId === staleRun.run_id)).toHaveLength(1)
      expect(row.getAttribute('data-enrichment-state')).toBe('stale')
    } finally {
      view?.unmount()
      mismatchResponse.resolve(matchingDetail(staleRun))
      laterPage.resolve(page([]))
      if (originalVisibility) Object.defineProperty(document, 'visibilityState', originalVisibility)
      else delete (document as unknown as { visibilityState?: string }).visibilityState
    }
  })

  it('does not retry a terminal stale attention row when Show more expands the set', async () => {
    const runs = Array.from({ length: 11 }, (_, index) => makeRun(`stale-attention-${index + 1}`, {
      status: 'needs_attention', status_reason_code: 'worker_attention',
      ended_at: `2026-09-${String(20 - index).padStart(2, '0')}T00:00:00Z`,
    }))
    const staleRun = runs[0]
    const mismatchResponse = deferred<RunStatus>()
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue(page(runs))
    vi.mocked(api.getControlPlaneRun).mockImplementation(async (_projectId, runId) => {
      if (runId === staleRun.run_id) return mismatchResponse.promise
      return matchingDetail(runs.find(run => run.run_id === runId)!)
    })

    render(<GlobalRunOverview projects={[primary]} onOpen={vi.fn()} />)
    const staleRow = await screen.findByRole('button', { name: /stale-attention-1$/ })
    await waitFor(() => expect(api.getControlPlaneRun).toHaveBeenCalledWith('primary', staleRun.run_id, { signal: expect.anything() }))
    mismatchResponse.resolve({ ...staleRun, status: 'running' } as RunStatus)
    await waitFor(() => expect(staleRow.getAttribute('data-enrichment-state')).toBe('stale'))
    const callsBeforeExpansion = vi.mocked(api.getControlPlaneRun).mock.calls.filter(([, runId]) => runId === staleRun.run_id).length

    fireEvent.click(screen.getByRole('button', { name: 'Show more (1 remaining)' }))
    expect(await screen.findByRole('button', { name: /stale-attention-11/ })).toBeTruthy()
    await waitFor(() => expect(api.getControlPlaneRun).toHaveBeenCalledTimes(11))
    expect(vi.mocked(api.getControlPlaneRun).mock.calls.filter(([, runId]) => runId === staleRun.run_id)).toHaveLength(callsBeforeExpansion)
    expect(staleRow.getAttribute('data-enrichment-state')).toBe('stale')
  })

  it('retries a terminal stale mismatch after an ordinary Refresh', async () => {
    const run = makeRun('refresh-stale', {
      original_plan_display_name: 'Refresh stale', original_plan_path: 'plans/refresh-stale.md',
    })
    const mismatchResponse = deferred<RunStatus>()
    const retryResponse = deferred<RunStatus>()
    let detailCalls = 0
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue(page([run]))
    vi.mocked(api.getControlPlaneRun).mockImplementation(async () => {
      detailCalls += 1
      if (detailCalls === 1) return mismatchResponse.promise
      return retryResponse.promise
    })

    render(<GlobalRunOverview projects={[primary]} onOpen={vi.fn()} />)
    const row = await screen.findByRole('button', { name: /Refresh stale.*refresh-stale/ })
    await waitFor(() => expect(detailCalls).toBe(1))
    mismatchResponse.resolve({ ...run, status: 'running' } as RunStatus)
    await waitFor(() => expect(row.getAttribute('data-enrichment-state')).toBe('stale'))

    fireEvent.click(screen.getByRole('button', { name: 'Refresh', exact: true }))
    await waitFor(() => expect(detailCalls).toBe(2))
    retryResponse.resolve(canonicalDetail(run, {
      total_checkpoints: { value: 1, coverage: 'complete' },
      approved_checkpoints: { value: 1, coverage: 'complete' },
    }))
    await waitFor(() => expect(row.getAttribute('data-enrichment-state')).toBe('settled'))
    expect(row.textContent).toContain('1/1 approved')
    expect(detailCalls).toBe(2)
  })

  it('closes admission during Refresh while retaining stale progress, then reopens once', async () => {
    const run = makeRun('refresh-gated', {
      original_plan_display_name: 'Refresh gated', original_plan_path: 'plans/refresh-gated.md',
    })
    const refreshPage = deferred<ReturnType<typeof page>>()
    const initialDetail = canonicalDetail(run, {
      total_checkpoints: { value: 2, coverage: 'complete' },
      approved_checkpoints: { value: 1, coverage: 'complete' },
    })
    const refreshedDetail = deferred<RunStatus>()
    let listCalls = 0
    let detailCalls = 0
    vi.mocked(api.listControlPlaneRuns).mockImplementation(() => {
      listCalls += 1
      return listCalls === 1 ? Promise.resolve(page([run])) : refreshPage.promise
    })
    vi.mocked(api.getControlPlaneRun).mockImplementation(async () => {
      detailCalls += 1
      return detailCalls === 1 ? initialDetail : refreshedDetail.promise
    })

    render(<GlobalRunOverview projects={[primary]} onOpen={vi.fn()} />)
    const row = await screen.findByRole('button', { name: /Refresh gated.*refresh-gated/ })
    await waitFor(() => expect(row.textContent).toContain('1/2 approved'))

    fireEvent.click(screen.getByRole('button', { name: 'Refresh', exact: true }))
    await waitFor(() => expect(screen.queryByRole('status')).toBeNull())
    expect(row.textContent).toContain('1/2 approved')
    expect(detailCalls).toBe(1)

    refreshPage.resolve(page([run]))
    await waitFor(() => expect(detailCalls).toBe(2))
    refreshedDetail.resolve(canonicalDetail(run, {
      total_checkpoints: { value: 2, coverage: 'complete' },
      approved_checkpoints: { value: 2, coverage: 'complete' },
    }))
    await waitFor(() => expect(row.textContent).toContain('2/2 approved'))
    expect(detailCalls).toBe(2)
  })

  it('settles failures explicitly and retries only after an ordinary Refresh', async () => {
    const run = makeRun('retry-run', {
      original_plan_display_name: 'Retry title', original_plan_path: 'plans/retry.md',
    })
    const retryResponse = deferred<RunStatus>()
    let detailCalls = 0
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue(page([run]))
    vi.mocked(api.getControlPlaneRun).mockImplementation(async () => {
      detailCalls += 1
      if (detailCalls === 1) throw new Error('temporary detail failure')
      return retryResponse.promise
    })

    render(<GlobalRunOverview projects={[primary]} onOpen={vi.fn()} />)
    const row = await screen.findByRole('button', { name: /Retry title.*retry-run/ })
    await waitFor(() => expect(row.getAttribute('data-enrichment-state')).toBe('failed'))
    expect(row.textContent).toContain('Checkpoint progress unavailable — Refresh to retry.')
    expect(detailCalls).toBe(1)

    fireEvent.click(screen.getByRole('button', { name: 'Refresh', exact: true }))
    await waitFor(() => expect(detailCalls).toBe(2))
    retryResponse.resolve(canonicalDetail(run, { total_checkpoints: { value: 1, coverage: 'complete' }, approved_checkpoints: { value: 0, coverage: 'complete' } }))
    await waitFor(() => expect(row.getAttribute('data-enrichment-state')).toBe('settled'))
    expect(row.textContent).toContain('0/1 approved')
    expect(detailCalls).toBe(2)
  })

  it('discards a late response for a replaced raw row and owns sibling projects independently', async () => {
    const first = makeRun('shared-run', {
      status: 'done', current_step: 'first', original_plan_display_name: 'First title', original_plan_path: 'plans/shared.md',
    })
    const replacement = makeRun('shared-run', {
      status: 'running', activity: 'active', current_step: 'second', original_plan_display_name: 'Second title', original_plan_path: 'plans/shared.md',
    })
    const sibling = makeRun('shared-run', {
      original_plan_display_name: 'Sibling title', original_plan_path: 'plans/sibling.md',
    })
    const oldResponse = deferred<RunStatus>()
    const newResponse = deferred<RunStatus>()
    let listCalls = 0
    let primaryDetailCalls = 0
    vi.mocked(api.listControlPlaneRuns).mockImplementation(async projectId => {
      if (projectId === 'child') return page([sibling])
      listCalls += 1
      return page([listCalls === 1 ? first : replacement])
    })
    vi.mocked(api.getControlPlaneRun).mockImplementation(async projectId => {
      if (projectId === 'child') return canonicalDetail(sibling, { total_checkpoints: { value: 3, coverage: 'complete' }, approved_checkpoints: { value: 3, coverage: 'complete' } })
      primaryDetailCalls += 1
      return primaryDetailCalls === 1 ? oldResponse.promise : newResponse.promise
    })

    const view = render(<GlobalRunOverview projects={[primary, child]} onOpen={vi.fn()} />)
    await waitFor(() => expect(api.getControlPlaneRun).toHaveBeenCalledTimes(2))
    fireEvent.click(screen.getByRole('button', { name: 'Refresh', exact: true }))
    await waitFor(() => expect(listCalls).toBe(2))
    await waitFor(() => expect(primaryDetailCalls).toBe(2))

    oldResponse.resolve(canonicalDetail(first, { total_checkpoints: { value: 1, coverage: 'complete' }, approved_checkpoints: { value: 1, coverage: 'complete' } }))
    newResponse.resolve(canonicalDetail(replacement, { total_checkpoints: { value: 2, coverage: 'complete' }, approved_checkpoints: { value: 2, coverage: 'complete' } }))
    await waitFor(() => expect(screen.getByRole('button', { name: /Second title.*shared-run/ }).getAttribute('data-enrichment-state')).toBe('settled'))
    expect(screen.getByRole('button', { name: /Second title.*shared-run/ }).textContent).toContain('2/2 approved')
    expect(screen.getByRole('button', { name: /Sibling title.*shared-run/ }).textContent).toContain('3/3 approved')
    expect(screen.queryByRole('button', { name: /First title.*shared-run/ })).toBeNull()
    expect(view.container.querySelectorAll('[data-enrichment-state="stale"]').length).toBe(0)
  })

  it('admits the next attention row only after Show more changes the rendered set', async () => {
    const runs = Array.from({ length: 11 }, (_, index) => makeRun(`attention-${index + 1}`, {
      status: 'needs_attention', status_reason_code: 'worker_attention',
      ended_at: `2026-09-${String(20 - index).padStart(2, '0')}T00:00:00Z`,
    }))
    const pending = new Map<string, ReturnType<typeof deferred<RunStatus>>>()
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue(page(runs))
    vi.mocked(api.getControlPlaneRun).mockImplementation(async (_projectId, runId) => {
      const response = deferred<RunStatus>()
      pending.set(runId, response)
      return response.promise
    })

    render(<GlobalRunOverview projects={[primary]} onOpen={vi.fn()} />)
    expect(await screen.findByRole('heading', { name: 'Needs attention (11)' })).toBeTruthy()
    await waitFor(() => expect(api.getControlPlaneRun).toHaveBeenCalledTimes(4))
    expect(api.getControlPlaneRun).not.toHaveBeenCalledWith('primary', 'attention-11', expect.anything())

    for (const [runId, response] of pending) {
      response.resolve(matchingDetail(runs.find(run => run.run_id === runId)!))
    }
    await waitFor(() => expect(api.getControlPlaneRun).toHaveBeenCalledTimes(8))
    for (const [runId, response] of pending) {
      response.resolve(matchingDetail(runs.find(run => run.run_id === runId)!))
    }
    await waitFor(() => expect(api.getControlPlaneRun).toHaveBeenCalledTimes(10))
    expect(screen.queryByRole('button', { name: /attention-11/ })).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Show more (1 remaining)' }))
    const expanded = await screen.findByRole('button', { name: /attention-11/ })
    await waitFor(() => expect(api.getControlPlaneRun).toHaveBeenCalledWith('primary', 'attention-11', { signal: expect.anything() }))
    pending.get('attention-11')?.resolve(matchingDetail(runs[10]))
    await waitFor(() => expect(expanded.getAttribute('data-enrichment-state')).toBe('settled'))
    expect(api.getControlPlaneRun).toHaveBeenCalledTimes(11)
  })

  it('aborts an in-flight visible enrichment on unmount', async () => {
    const run = makeRun('abort-run')
    const response = deferred<RunStatus>()
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue(page([run]))
    vi.mocked(api.getControlPlaneRun).mockReturnValue(response.promise)

    const view = render(<GlobalRunOverview projects={[primary]} onOpen={vi.fn()} />)
    await screen.findByRole('button', { name: /abort-run/ })
    await waitFor(() => expect(api.getControlPlaneRun).toHaveBeenCalledTimes(1))
    const signal = vi.mocked(api.getControlPlaneRun).mock.calls[0][2]?.signal
    expect(signal?.aborted).toBe(false)
    view.unmount()
    expect(signal?.aborted).toBe(true)
    response.resolve(matchingDetail(run))
  })

  it('does not start detail enrichment for a raw page that arrives while hidden', async () => {
    const run = makeRun('late-hidden-page')
    const rawPage = deferred<ReturnType<typeof page>>()
    const originalVisibility = Object.getOwnPropertyDescriptor(document, 'visibilityState')
    let view: ReturnType<typeof render> | null = null
    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' })
    vi.mocked(api.listControlPlaneRuns).mockReturnValue(rawPage.promise)
    vi.mocked(api.getControlPlaneRun).mockRejectedValue(new Error('hidden detail must not start'))

    try {
      view = render(<GlobalRunOverview projects={[primary]} onOpen={vi.fn()} />)
      await waitFor(() => expect(api.listControlPlaneRuns).toHaveBeenCalledTimes(1))
      await act(async () => {
        Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'hidden' })
        document.dispatchEvent(new Event('visibilitychange'))
        rawPage.resolve(page([run]))
        await rawPage.promise
      })

      expect((await screen.findByRole('button', { name: /late-hidden-page/ })).getAttribute('data-enrichment-state')).toBe('loading')
      expect(api.getControlPlaneRun).not.toHaveBeenCalled()
    } finally {
      view?.unmount()
      if (originalVisibility) Object.defineProperty(document, 'visibilityState', originalVisibility)
      else delete (document as unknown as { visibilityState?: string }).visibilityState
    }
  })

  it('does not publish a cancelled detail response or restart work while hidden', async () => {
    const first = makeRun('cancelled-hidden')
    const cancelledResponse = deferred<RunStatus>()
    const originalVisibility = Object.getOwnPropertyDescriptor(document, 'visibilityState')
    let view: ReturnType<typeof render> | null = null
    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' })
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue(page([first]))
    vi.mocked(api.getControlPlaneRun).mockReturnValue(cancelledResponse.promise)

    try {
      view = render(<GlobalRunOverview projects={[primary]} onOpen={vi.fn()} />)
      await waitFor(() => expect(api.getControlPlaneRun).toHaveBeenCalledTimes(1))
      const firstSignal = vi.mocked(api.getControlPlaneRun).mock.calls[0][2]?.signal

      await act(async () => {
        Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'hidden' })
        document.dispatchEvent(new Event('visibilitychange'))
        await Promise.resolve()
      })
      expect(firstSignal?.aborted).toBe(true)

      await act(async () => {
        cancelledResponse.resolve(canonicalDetail(first, {
          total_checkpoints: { value: 1, coverage: 'complete' },
          approved_checkpoints: { value: 1, coverage: 'complete' },
        }))
        await cancelledResponse.promise
      })
      expect(api.getControlPlaneRun).toHaveBeenCalledTimes(1)
      const cancelledRow = screen.getByRole('button', { name: /cancelled-hidden/ })
      expect(cancelledRow.getAttribute('data-enrichment-state')).toBe('loading')
      expect(cancelledRow.textContent).not.toContain('1/1 approved')
    } finally {
      view?.unmount()
      cancelledResponse.resolve(matchingDetail(first))
      if (originalVisibility) Object.defineProperty(document, 'visibilityState', originalVisibility)
      else delete (document as unknown as { visibilityState?: string }).visibilityState
    }
  })

  it('keeps visibility recovery gated while raw cursors remain pending', async () => {
    const run = makeRun('recovery-run')
    const laterPage = deferred<ReturnType<typeof page>>()
    const currentResponse = deferred<RunStatus>()
    const originalVisibility = Object.getOwnPropertyDescriptor(document, 'visibilityState')
    let view: ReturnType<typeof render> | null = null
    let detailCalls = 0
    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' })
    // Keep the second list page pending without changing the first page's run identity.
    vi.mocked(api.listControlPlaneRuns).mockImplementationOnce(async () => ({
      runs: [run], next_cursor: 'later', schema_version: 1,
    })).mockImplementationOnce(async () => laterPage.promise)
    vi.mocked(api.getControlPlaneRun).mockImplementation(async (_projectId, runId) => {
      detailCalls += 1
      if (detailCalls === 1 && runId === run.run_id) return currentResponse.promise
      throw new Error('duplicate recovery detail request')
    })

    try {
      const onOpen = vi.fn()
      view = render(<GlobalRunOverview projects={[primary]} onOpen={onOpen} />)
      const row = await screen.findByRole('button', { name: /recovery-run/ })
      fireEvent.click(row)
      expect(onOpen).toHaveBeenCalledWith('primary', run.run_id)
      expect(detailCalls).toBe(0)

      await act(async () => {
        Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'hidden' })
        document.dispatchEvent(new Event('visibilitychange'))
        await Promise.resolve()
      })
      expect(detailCalls).toBe(0)

      await act(async () => {
        Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' })
        document.dispatchEvent(new Event('visibilitychange'))
        await Promise.resolve()
      })
      expect(detailCalls).toBe(0)
      expect(row.getAttribute('data-enrichment-state')).toBe('loading')
      expect(view.container.querySelector('.global-run-results')?.getAttribute('aria-busy')).toBe('true')

      await act(async () => {
        laterPage.resolve(page([]))
        await laterPage.promise
      })
      await waitFor(() => expect(detailCalls).toBe(1))
      expect(view.container.querySelector('.global-run-results')?.getAttribute('aria-busy')).toBe('false')

      currentResponse.resolve(canonicalDetail(run, {
        total_checkpoints: { value: 1, coverage: 'complete' },
        approved_checkpoints: { value: 1, coverage: 'complete' },
      }))
      await waitFor(() => expect(row.getAttribute('data-enrichment-state')).toBe('settled'))
      expect(row.textContent).toContain('1/1 approved')
      expect(detailCalls).toBe(1)
    } finally {
      view?.unmount()
      currentResponse.resolve(matchingDetail(run))
      laterPage.resolve(page([]))
      if (originalVisibility) Object.defineProperty(document, 'visibilityState', originalVisibility)
      else delete (document as unknown as { visibilityState?: string }).visibilityState
    }
  })

  it('aborts admitted enrichment when hidden and admits one current replacement on recovery', async () => {
    const run = makeRun('hidden-abort-run')
    const cancelledResponse = deferred<RunStatus>()
    const replacementResponse = deferred<RunStatus>()
    const originalVisibility = Object.getOwnPropertyDescriptor(document, 'visibilityState')
    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' })
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue(page([run]))
    let detailCalls = 0
    vi.mocked(api.getControlPlaneRun).mockImplementation(async () => {
      detailCalls += 1
      if (detailCalls === 1) return cancelledResponse.promise
      if (detailCalls === 2) return replacementResponse.promise
      throw new Error('duplicate recovery detail request')
    })

    const view = render(<GlobalRunOverview projects={[primary]} onOpen={vi.fn()} />)
    const row = await screen.findByRole('button', { name: /hidden-abort-run/ })
    await waitFor(() => expect(api.getControlPlaneRun).toHaveBeenCalledTimes(1))
    const signal = vi.mocked(api.getControlPlaneRun).mock.calls[0][2]?.signal
    await act(async () => {
      Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'hidden' })
      document.dispatchEvent(new Event('visibilitychange'))
      await Promise.resolve()
    })
    expect(signal?.aborted).toBe(true)

    await act(async () => {
      Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' })
      document.dispatchEvent(new Event('visibilitychange'))
      await Promise.resolve()
    })
    await waitFor(() => expect(detailCalls).toBe(2))
    replacementResponse.resolve(canonicalDetail(run, {
      total_checkpoints: { value: 1, coverage: 'complete' },
      approved_checkpoints: { value: 1, coverage: 'complete' },
    }))
    await waitFor(() => expect(row.getAttribute('data-enrichment-state')).toBe('settled'))
    expect(detailCalls).toBe(2)

    view.unmount()
    if (originalVisibility) Object.defineProperty(document, 'visibilityState', originalVisibility)
    else delete (document as unknown as { visibilityState?: string }).visibilityState
    cancelledResponse.resolve(matchingDetail(run))
    replacementResponse.resolve(matchingDetail(run))
  })
})
