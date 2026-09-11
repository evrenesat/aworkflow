import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import type { ProjectInfo, RunStatus } from '../types'
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

describe('GlobalRunOverview project context', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [childRun], next_cursor: null, schema_version: 1 })
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
})
