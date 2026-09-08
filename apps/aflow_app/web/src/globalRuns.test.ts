import { describe, expect, it, vi } from 'vitest'
import { fetchGlobalRuns, isOngoing, selectGlobalRuns, validRecentLimit } from './globalRuns'
import * as api from './api'
import type { RunStatus } from './types'
vi.mock('./api', () => ({ listControlPlaneRuns: vi.fn() }))
const run = (id: string, status = 'completed'): RunStatus => ({ run_id: id, status, ownership: 'control_plane', evidence: {}, ended_at: '2026-01-01T00:00:00Z' } as RunStatus)
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
  it('follows every cursor so old ongoing records survive, preserving other projects on errors', async () => {
    vi.mocked(api.listControlPlaneRuns).mockImplementation(async (id, request) => {
      if (id === 'bad') throw new Error('unavailable')
      return request?.cursor ? { runs: [run('old', 'paused')], next_cursor: null, schema_version: 1 }
        : { runs: Array.from({ length: 100 }, (_, i) => run(String(i))), next_cursor: 'next', schema_version: 1 }
    })
    const result = await fetchGlobalRuns(['a', 'bad'], new AbortController().signal)
    expect(result.byProject.a).toHaveLength(101)
    expect(result.errors).toEqual(['bad'])
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
