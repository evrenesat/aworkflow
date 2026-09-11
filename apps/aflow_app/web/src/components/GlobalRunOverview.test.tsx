import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import type { ProjectInfo, RunProgressSummary, RunStatus } from '../types'
import { GlobalRunOverview } from './GlobalRunOverview'

vi.mock('../api', async () => {
  const actual = await vi.importActual<typeof import('../api')>('../api')
  return { ...actual, listControlPlaneRuns: vi.fn() }
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
  const promise = new Promise<T>(value => { resolve = value })
  return { promise, resolve }
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

describe('GlobalRunOverview project context', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [childRun], next_cursor: null, schema_version: 1 })
  })

  it('waits for the registry and current runs before making empty-state claims', async () => {
    const runs = deferred<ReturnType<typeof page>>()
    vi.mocked(api.listControlPlaneRuns).mockReturnValue(runs.promise)
    const view = render(<GlobalRunOverview projects={[]} registryLoading onOpen={vi.fn()} />)

    expect(screen.getByRole('status').textContent).toBe('Loading runs…')
    expect(view.container.querySelector('.global-run-results')?.getAttribute('aria-busy')).toBe('true')
    expect(view.container.querySelector('.global-run-loading-spinner')).toBeTruthy()
    expect(screen.queryByText(/No registered projects/)).toBeNull()
    expect(screen.queryByRole('heading', { name: /Ongoing/ })).toBeNull()
    expect(api.listControlPlaneRuns).not.toHaveBeenCalled()

    view.rerender(<GlobalRunOverview projects={[primary]} registryLoading={false} onOpen={vi.fn()} />)
    await waitFor(() => expect(api.listControlPlaneRuns).toHaveBeenCalledWith('primary', expect.anything(), expect.anything()))
    expect(screen.getByRole('status').textContent).toBe('Loading runs…')
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
    expect(screen.getByRole('heading', { name: 'Ongoing (0)' })).toBeTruthy()
    expect(screen.getByText('No ongoing runs.')).toBeTruthy()
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
    await waitFor(() => expect(screen.getByRole('status').textContent).toBe('Refreshing runs…'))
    // The refresh is for the selected history, so the usable archived row stays visible.
    expect(screen.getByRole('button', { name: /archived-run/ })).toBeTruthy()
    expect(view.container.querySelector('.global-run-results')?.getAttribute('aria-busy')).toBe('true')

    refreshed.resolve(page([makeRun('refreshed-run', { history_state: 'archived', plan_path: 'plans/refreshed.md' })]))
    expect(await screen.findByRole('button', { name: /refreshed-run/ })).toBeTruthy()
    expect(screen.queryByRole('status')).toBeNull()
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

  it('requests and displays the selected archived history without changing run identity', async () => {
    const visible = makeRun('visible-run', { plan_path: 'plans/visible.md' })
    const archived = makeRun('archived-run', { history_state: 'archived', plan_path: 'plans/archived.md' })
    vi.mocked(api.listControlPlaneRuns).mockImplementation(async (_projectId, options) => ({
      runs: options?.history === 'archived' ? [archived] : [visible],
      next_cursor: null,
      schema_version: 1,
    }))

    render(<GlobalRunOverview projects={[primary]} onOpen={vi.fn()} />)
    expect(await screen.findByRole('button', { name: /Primary project · Completed · Visible · visible-run/ })).toBeTruthy()

    fireEvent.change(screen.getByLabelText('Run history'), { target: { value: 'archived' } })
    expect(await screen.findByRole('button', { name: /Primary project · Completed · Archived · archived-run/ })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /Primary project · Completed · Visible · visible-run/ })).toBeNull()
    expect(screen.getByText('Plan: plans/archived.md · Run: archived-run')).toBeTruthy()
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
    expect(await screen.findByRole('button', { name: /Primary project · Completed · Shared · primary-run/ })).toBeTruthy()
    expect(screen.getByRole('button', { name: /Primary project · Worktree: Feature worktree · Completed · Shared · child-run-same-name/ })).toBeTruthy()

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

    expect(await screen.findByText('4 / 11 approved')).toBeTruthy()
    expect(screen.getByText(/Implementing CP5 of 11/)).toBeTruthy()
    expect(screen.getByText('Plan: /srv/plans/canonical-plan.md · Run: canonical-run')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: /canonical-run/ }))
    expect(onOpen).toHaveBeenCalledWith('primary', 'canonical-run')
    expect(api.listControlPlaneRuns).toHaveBeenCalledTimes(1)
  })
})
