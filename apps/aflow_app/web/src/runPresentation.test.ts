import { describe, expect, it } from 'vitest'
import type { RunStatus } from './types'
import { executionDuration, runPlanDisplayName, statusLabel } from './runPresentation'

const run = { status: 'manifest_only', ownership: 'control_plane', evidence: { manifest_created_at: '2026-09-08T10:00:00Z' } } as RunStatus
describe('honest run timing', () => {
  it('never uses submission time as execution time', () => {
    for (const status of ['manifest_only', 'awaiting_startup_answer', 'needs_attention', 'running', 'completed']) {
      for (const now of [Date.parse('2026-09-08T10:01:00Z'), Date.parse('2026-09-08T12:00:00Z')]) {
        expect(executionDuration({ ...run, status }, now)).toBeNull()
      }
    }
  })
  it('ticks only running execution and fixes terminal duration', () => {
    const started = { ...run, started_at: '2026-09-08T10:00:00Z', status: 'running' }
    expect(executionDuration(started, Date.parse('2026-09-08T10:01:00Z'))).toBe('1m 0s')
    expect(executionDuration(started, Date.parse('2026-09-08T10:02:00Z'))).toBe('2m 0s')
    const finished = { ...started, status: 'completed', ended_at: '2026-09-08T10:03:00Z' }
    expect(executionDuration(finished, Date.now())).toBe('3m 0s')
    expect(executionDuration(finished, Date.now() + 100000)).toBe('3m 0s')
    expect(executionDuration({ ...started, status: 'waiting_for_valid_override' }, Date.now())).toBeNull()
    expect(executionDuration({ ...finished, ended_at: null }, Date.now())).toBeNull()
  })
  it('distinguishes failure, questions, launch and runtime', () => {
    expect(statusLabel(run)).toBe('Needs attention')
    expect(statusLabel({ ...run, activity: 'active' })).toBe('Starting')
    expect(statusLabel({ ...run, status: 'awaiting_startup_answer' })).toBe('Input needed')
    expect(statusLabel({ ...run, status: 'needs_attention', evidence: { no_agent_started: true, startup_failure: { stage: 'preparation' } } })).toBe('Could not start')
    expect(statusLabel({ ...run, status: 'needs_attention', evidence: { startup_failure: { stage: 'unit_launch' } } })).toBe('Needs attention')
  })
})

describe('run plan presentation', () => {
  it('uses a readable basename while preserving supplied casing after the initial character', () => {
    expect(runPlanDisplayName('/srv/work/plans/clear--Run_UI.md', 'run-1')).toBe('Clear Run UI')
    expect(runPlanDisplayName('C:\\work\\plans\\mixed_Name.MD', 'run-2')).toBe('Mixed Name')
  })

  it('falls back to the exact run id only when the plan name is absent', () => {
    expect(runPlanDisplayName(null, 'run-without-plan')).toBe('run-without-plan')
    expect(runPlanDisplayName('/srv/plans/.md', 'run-with-empty-name')).toBe('run-with-empty-name')
  })
})
