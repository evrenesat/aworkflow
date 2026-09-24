import { describe, expect, it, vi } from 'vitest'
import {
  fetchGlobalRuns,
  globalRunSnapshot,
  isOngoing,
  matchesGlobalRun,
  matchesGlobalRunProgressIdentity,
  matchesGlobalRunSnapshot,
  selectGlobalRuns,
  validRecentLimit,
} from './globalRuns'
import * as api from './api'
import type { RunStatus } from './types'
vi.mock('./api', () => ({ listControlPlaneRuns: vi.fn() }))
const run = (id: string, status = 'completed'): RunStatus => ({ run_id: id, status, ownership: 'control_plane', evidence: {}, ended_at: '2026-01-01T00:00:00Z' } as RunStatus)
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(value => { resolve = value })
  return { promise, resolve }
}
describe('global runs', () => {
  it('keeps all ongoing and ten recent globally, deduplicating by pair', () => {
    const rows = ['a', 'b'].flatMap(projectId => Array.from({ length: 20 }, (_, i) => ({ projectId, run: run(String(i), i === 19 ? 'paused' : 'completed') })))
    const result = selectGlobalRuns([...rows, rows[0]], 10)
    expect(result.ongoing).toHaveLength(2)
    expect(result.recent).toHaveLength(10)
    expect(selectGlobalRuns([{ projectId: 'a', run: run('same') }, { projectId: 'b', run: run('same') }], 10).recent).toHaveLength(2)
  })
  it('classifies waiting, starting, failures and unknown liveness honestly', () => {
    for (const status of ['paused', 'waiting_for_input', 'waiting_for_valid_override', 'awaiting_startup_answer']) expect(isOngoing(run('x', status))).toBe(true)
    expect(isOngoing(run('x', 'needs_attention'))).toBe(false)
    expect(isOngoing({ ...run('x', 'manifest_only'), evidence: { startup_state: 'preparing' } })).toBe(false)
    expect(isOngoing({ ...run('x', 'launch_requested'), evidence: { startup_failure: { stage: 'unit_launch' } } })).toBe(false)
    expect(isOngoing({ ...run('x', 'needs_attention'), evidence: { startup_failure: {}, unit_active: true } })).toBe(false)
    expect(isOngoing({ ...run('x', 'running'), ownership: 'legacy' })).toBe(false)
  })
  it('follows every cursor so old ongoing and attention records survive, preserving other projects on errors', async () => {
    vi.mocked(api.listControlPlaneRuns).mockImplementation(async (id, request) => {
      if (id === 'bad') throw new Error('unavailable')
      return request?.cursor ? { runs: [run('old', 'paused')], next_cursor: null, schema_version: 1 }
        : { runs: [...Array.from({ length: 99 }, (_, i) => run(String(i))), run('attention', 'needs_attention')], next_cursor: 'next', schema_version: 1 }
    })
    const result = await fetchGlobalRuns(['a', 'bad'], new AbortController().signal)
    expect(api.listControlPlaneRuns).toHaveBeenCalledWith(
      'a',
      expect.objectContaining({ include_progress: false }),
      expect.anything(),
    )
    expect(result.byProject.a).toHaveLength(101)
    const selected = selectGlobalRuns(result.byProject.a.map(run => ({ projectId: 'a', run })), 10)
    expect(selected.ongoing.map(row => row.run.run_id)).toEqual(['old'])
    expect(selected.attention.map(row => row.run.run_id)).toEqual(['attention'])
    expect(result.errors).toEqual(['bad'])
  })

  it('publishes cumulative page progress and reports partial failure', async () => {
    vi.mocked(api.listControlPlaneRuns).mockImplementation(async (_id, request) => {
      if (!request?.cursor) return { runs: [run('first')], next_cursor: 'next', schema_version: 1 }
      throw new Error('later page unavailable')
    })
    const updates: Array<{ projectId: string; ids: string[]; state: string }> = []
    const result = await fetchGlobalRuns(['project'], new AbortController().signal, 'visible', update => {
      updates.push({ projectId: update.projectId, ids: update.runs.map(item => item.run_id), state: update.state })
    })

    expect(updates).toEqual([
      { projectId: 'project', ids: ['first'], state: 'loading' },
      { projectId: 'project', ids: ['first'], state: 'failed' },
    ])
    expect(result.byProject).toEqual({})
    expect(result.errors).toEqual(['project'])
  })

  it('rejects repeated cursors and emits no progress after abort', async () => {
    vi.mocked(api.listControlPlaneRuns).mockClear()
    vi.mocked(api.listControlPlaneRuns).mockImplementation(async (_id, request) => {
      if (!request?.cursor) return { runs: [run('first')], next_cursor: 'same', schema_version: 1 }
      return { runs: [run('second')], next_cursor: 'same', schema_version: 1 }
    })
    const repeatedUpdates: string[] = []
    const repeated = await fetchGlobalRuns(['project'], new AbortController().signal, 'visible', update => {
      repeatedUpdates.push(`${update.state}:${update.runs.map(item => item.run_id).join(',')}`)
    })
    expect(repeatedUpdates).toEqual(['loading:first', 'loading:first,second', 'failed:first,second'])
    expect(repeated.errors).toEqual(['project'])
    expect(api.listControlPlaneRuns).toHaveBeenCalledTimes(2)

    const controller = new AbortController()
    const pending = deferred<Awaited<ReturnType<typeof api.listControlPlaneRuns>>>()
    vi.mocked(api.listControlPlaneRuns).mockReturnValue(pending.promise)
    const updates: unknown[] = []
    const request = fetchGlobalRuns(['aborted'], controller.signal, 'visible', update => updates.push(update))
    controller.abort()
    pending.resolve({ runs: [run('ignored')], next_cursor: null, schema_version: 1 })
    const result = await request
    expect(updates).toEqual([])
    expect(result).toEqual({ byProject: {}, errors: [] })
  })
  it('accepts only positive safe limits', () => {
    for (const value of [null, '', '-1', '1.5', 'garbage', Infinity, 0, Number.MAX_SAFE_INTEGER + 1]) expect(validRecentLimit(value)).toBe(10)
    expect(validRecentLimit('23')).toBe(23)
  })
})

it('keeps unresolved records outside Recent and hides archived/deleted records by default', () => {
  const rows = ['a', 'b', 'c'].map(id => ({ projectId: 'p', run: run(id, 'needs_attention') }))
  rows.push({ projectId: 'p', run: run('done') }, { projectId: 'p', run: { ...run('archive'), history_state: 'archived' } }, { projectId: 'p', run: { ...run('deleted'), history_state: 'deleted' } })
  const result = selectGlobalRuns(rows, 1)
  expect(result.attention).toHaveLength(3)
  expect(result.recent.map(row => row.run.run_id)).toEqual(['done'])
  expect(selectGlobalRuns(rows, 10, 'archived').recent.map(row => row.run.run_id)).toEqual(['archive'])
})

it('separates a confirmed history gap from actionable attention across history filters', () => {
  const historicalGap: RunStatus = {
    ...run('20260911t140026z-d06cc7d7', 'needs_attention'),
    status_reason_code: 'unit_missing',
    reason: 'No current workflow unit was found and no normal outcome was recorded.',
    activity: 'unknown',
    unit_name: 'aflow-run-20260911t140026z-d06cc7d7.service',
    evidence: { unit_observation: 'missing', has_run_metadata: false, can_resume: false },
    ended_at: null,
  }
  const archivedGap = { ...historicalGap, run_id: 'archived-gap', unit_name: 'aflow-run-archived-gap.service', history_state: 'archived' as const }
  const actionable = { ...run('actionable', 'needs_attention'), status_reason_code: 'worker_attention' }
  const completed = run('completed-history')
  const deletedGap = { ...historicalGap, run_id: 'deleted-gap', unit_name: 'aflow-run-deleted-gap.service', history_state: 'deleted' as const }
  const rows = [historicalGap, archivedGap, actionable, completed, deletedGap].map(run => ({ projectId: 'project', run }))

  const visible = selectGlobalRuns(rows, 1)
  expect(visible.historyGaps.map(row => row.run.run_id)).toEqual([historicalGap.run_id])
  expect(visible.attention.map(row => row.run.run_id)).toEqual(['actionable'])
  expect(visible.recent.map(row => row.run.run_id)).toEqual(['completed-history'])

  const all = selectGlobalRuns(rows, 1, 'all')
  expect(all.historyGaps.map(row => row.run.run_id)).toEqual([historicalGap.run_id, 'archived-gap'])
  expect(all.attention.map(row => row.run.run_id)).toEqual(['actionable'])
  expect(selectGlobalRuns(rows, 1, 'archived').historyGaps.map(row => row.run.run_id)).toEqual(['archived-gap'])
})

it('searches loaded run identities and readable labels without changing the source rows', () => {
  const row = { projectId: 'project-a', run: { ...run('run-exact'), plan_path: 'plans/clear-run-ui.md', workflow_name: 'managed', team: 'full', current_step: 'review' } }
  expect(matchesGlobalRun(row, 'clear review', 'AFlow project')).toBe(true)
  expect(matchesGlobalRun(row, 'run-exact', 'AFlow project')).toBe(true)
  expect(matchesGlobalRun(row, 'other', 'AFlow project')).toBe(false)
  expect(row.run.plan_path).toBe('plans/clear-run-ui.md')
})

it('keeps a history gap searchable by both its display label and raw canonical status', () => {
  const row = {
    projectId: 'project-a',
    run: {
      ...run('history-gap', 'needs_attention'),
      status_reason_code: 'unit_missing',
      activity: 'unknown',
      unit_name: 'aflow-run-history-gap.service',
      evidence: { unit_observation: 'missing', has_run_metadata: false, can_resume: false },
    },
  }

  expect(matchesGlobalRun(row, 'Outcome not recorded')).toBe(true)
  expect(matchesGlobalRun(row, 'needs_attention')).toBe(true)
})

it('searches the raw canonical title before visible grouping and enrichment', () => {
  const row = {
    projectId: 'project-a',
    run: {
      ...run('hidden-canonical'),
      plan_path: 'plans/active-overlay.md',
      original_plan_display_name: 'Hidden original title',
      original_plan_path: 'plans/in-progress/original.md',
    },
  }
  expect(matchesGlobalRun(row, 'Hidden original title', 'AFlow project')).toBe(true)
  expect(matchesGlobalRun(row, 'original.md', 'AFlow project')).toBe(true)
  expect(row.run.plan_path).toBe('plans/active-overlay.md')
})

it('compares the explicit raw snapshot and progress identity without fabricating values', () => {
  const raw = {
    ...run('snapshot-run', 'running'),
    activity: undefined,
    status_reason_code: undefined,
    original_plan_display_name: 'Canonical title',
    original_plan_path: 'plans/original.md',
    history_state: undefined,
    history_revision: undefined,
    evidence: {},
  } as RunStatus
  const matchingDetail = {
    ...raw,
    activity: 'unknown' as const,
    progress: {
      original_plan_display_name: 'Canonical title',
      original_plan_path: 'plans/original.md',
    },
  } as RunStatus
  expect(globalRunSnapshot(raw)).toMatchObject({ activity: 'unknown', history_state: 'visible', history_revision: 0, unit_active: null })
  expect(matchesGlobalRunSnapshot(raw, matchingDetail)).toBe(true)
  expect(matchesGlobalRunSnapshot(raw, { ...matchingDetail, current_step: 'review' } as RunStatus)).toBe(false)
  expect(matchesGlobalRunProgressIdentity(raw, matchingDetail)).toBe(true)
  expect(matchesGlobalRunProgressIdentity(raw, {
    ...matchingDetail,
    progress: { original_plan_display_name: 'Contradictory title', original_plan_path: 'plans/other.md' },
  } as RunStatus)).toBe(false)
})
