import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { RunProgressCount, RunProgressExecutor, RunProgressSummary, RunStatus } from '../types'
import { RunProgress } from './RunProgress'

const count = (value: number | null, coverage: RunProgressCount['coverage'] = 'complete'): RunProgressCount => ({ value, coverage })

const executor: RunProgressExecutor = {
  role: 'worker', team: 'repair-team', selector: 'selector.5-repair', harness: 'codex', model: 'model.raw',
  model_display: 'Model display 52', effort: 'high', source_run_id: 'run-1', invocation_id: 'inv-5',
  turn_number: 8, started_at: '2026-09-11T11:57:00Z', ended_at: null, duration_seconds: 120,
}

function progress(overrides: Partial<RunProgressSummary> = {}): RunProgressSummary {
  return {
    schema_version: 1,
    availability: 'complete',
    observed_at: '2026-09-11T12:00:00Z',
    evidence_at: '2026-09-11T11:59:00Z',
    reason_codes: [],
    original_plan_identity: 'plan-1',
    original_plan_display_name: 'run-progress.md',
    original_plan_path: '/srv/work/plans/run-progress.md',
    total_checkpoints: count(11),
    approved_checkpoints: count(4),
    recorded_complete_checkpoints: count(4),
    current_checkpoint_id: 'cp-5',
    current_checkpoint_ordinal: 5,
    current_checkpoint_title: 'Checkpoint 5: A very long title that should wrap without changing the evidence',
    activity: 'active',
    phase: 'implementing',
    run_status: 'running',
    current_executor: executor,
    last_executor: null,
    worker_attempts: count(8),
    repair_passes: count(3),
    reviews: count(7),
    runtime_retries: count(1),
    applied_upgrades: count(2),
    ...overrides,
  }
}

function run(overrides: Partial<RunStatus> = {}): RunStatus {
  return {
    run_id: 'run-1', status: 'running', schema_version: 1, ownership: 'control_plane', revision: 1,
    reason: null, unit_name: null, launch_phase: 'running', workflow_name: 'managed', team: 'base',
    current_step: 'implement', turns_completed: 8, max_turns: 12, selected_start_step: null,
    skipped_steps: [], restarted_from_run_id: null, started_at: '2026-09-11T11:00:00Z', ended_at: null,
    activity: 'active', evidence: {}, progress: progress(), ...overrides,
  }
}

describe('RunProgress', () => {
  it('shows approved progress separately from the active checkpoint and recorded executor', () => {
    const { container } = render(<RunProgress run={run()} now={Date.parse('2026-09-11T12:00:00Z')} />)

    expect(screen.getByText('4 / 11 approved')).toBeTruthy()
    expect(screen.getByText(/Implementing CP5 of 11/)).toBeTruthy()
    expect(screen.getByText(/Checkpoint 5: A very long title/)).toBeTruthy()
    expect(screen.getByText(/Executor Worker · Model display 52 · selector\.5-repair · 2m/)).toBeTruthy()
    expect(screen.getByText('8 worker attempts · 3 repair passes · 7 reviews · 1 runtime retry · 2 upgrades · 4 recorded-complete checkpoints')).toBeTruthy()
    expect(screen.getByText('Latest evidence 1m ago')).toBeTruthy()
    expect(container.querySelectorAll('.compact-run-progress-segment')).toHaveLength(11)
  })

  it('keeps known zero totals and an absent current checkpoint truthful before the first attempt', () => {
    render(<RunProgress run={run({
      status: 'paused', activity: 'inactive', progress: progress({
        current_checkpoint_id: null, current_checkpoint_ordinal: null, current_checkpoint_title: null,
        phase: null, activity: null, current_executor: null, last_executor: null,
        approved_checkpoints: count(0),
        worker_attempts: count(0), repair_passes: count(0), reviews: count(0), runtime_retries: count(0), applied_upgrades: count(0),
        recorded_complete_checkpoints: count(0),
      }),
    })} now={Date.parse('2026-09-11T12:00:00Z')} />)

    expect(screen.getByText('0 / 11 approved')).toBeTruthy()
    expect(screen.getByText('Paused · No current checkpoint reported')).toBeTruthy()
    expect(screen.getByText('0 worker attempts · 0 repair passes · 0 reviews · 0 runtime retries · 0 upgrades · 0 recorded-complete checkpoints')).toBeTruthy()
    expect(screen.queryByText(/Executor/)).toBeNull()
  })

  it('renders the canonical successor last executor when no worker is active', () => {
    render(<RunProgress run={run({
      activity: 'inactive',
      progress: progress({
        activity: 'inactive', current_executor: null,
        last_executor: { ...executor, selector: 'successor.worker', source_run_id: 'successor-run' },
      }),
    })} />)

    expect(screen.getByText(/Executor Worker · Model display 52 · successor\.worker/)).toBeTruthy()
  })

  it('labels partial and recorded-complete evidence without upgrading it to approval', () => {
    render(<RunProgress run={run({
      status: 'waiting_for_input', activity: 'inactive', progress: progress({
        availability: 'partial', phase: 'awaiting_review', activity: 'inactive',
        approved_checkpoints: count(4, 'partial'), recorded_complete_checkpoints: count(5, 'partial'),
      }),
    })} now={Date.parse('2026-09-11T12:00:00Z')} />)

    expect(screen.getByText('At least 4 / 11 approved')).toBeTruthy()
    expect(screen.getByText(/Awaiting review CP5 of 11/)).toBeTruthy()
    expect(screen.getByText('Progress evidence partial')).toBeTruthy()
    expect(screen.getByText(/At least 5 recorded-complete checkpoints/)).toBeTruthy()
  })

  it('labels a complete recorded checkpoint as awaiting review', () => {
    render(<RunProgress run={run({
      status: 'waiting_for_input', activity: 'inactive', progress: progress({
        phase: null, activity: null, approved_checkpoints: count(4), recorded_complete_checkpoints: count(5),
      }),
    })} />)

    expect(screen.getByText('Recorded complete · awaiting review')).toBeTruthy()
    expect(screen.getByText('4 / 11 approved')).toBeTruthy()
  })

  it('renders unavailable and non-checkpoint modes without an empty success state', () => {
    const view = render(<RunProgress run={run({ progress: progress({ availability: 'unavailable', reason_codes: ['missing_plan'] }) })} />)
    expect(screen.getByText('Checkpoint progress unavailable')).toBeTruthy()
    expect(view.container.querySelector('.compact-run-progress-strip')).toBeNull()
    expect(view.container.querySelector('.compact-run-progress-bar')).toBeNull()

    view.rerender(<RunProgress run={run({ progress: progress({ availability: 'not_applicable', reason_codes: ['non_checkpoint_plan'] }) })} />)
    expect(screen.getByText('Non-checkpoint workflow')).toBeTruthy()
    expect(screen.getByText('Checkpoint progress unavailable')).toBeTruthy()
  })

  it('keeps missing and stale projections visibly unavailable or partial', () => {
    const view = render(<RunProgress run={run({ progress: null })} />)
    expect(screen.getByText('Checkpoint progress unavailable')).toBeTruthy()

    view.rerender(<RunProgress run={run({ progress: progress({ reason_codes: ['history_partial'] }) })} />)
    expect(screen.getByText('Earlier progress history unavailable')).toBeTruthy()

    view.rerender(<RunProgress run={run({ progress: progress({
      total_checkpoints: count(null, 'unavailable'), approved_checkpoints: count(null, 'unavailable'),
      worker_attempts: count(null, 'unavailable'), repair_passes: count(null, 'unavailable'), reviews: count(null, 'unavailable'),
      runtime_retries: count(null, 'unavailable'), applied_upgrades: count(null, 'unavailable'),
    }) })} />)
    expect(screen.getByText('Approval progress unknown')).toBeTruthy()
    expect(screen.getByText(/Unknown worker attempts · Unknown repair passes/)).toBeTruthy()
  })

  it.each([
    ['waiting_for_input', 'Waiting for input'],
    ['failed', 'Failed'],
    ['owner_stopped', 'Stopped'],
  ] as const)('keeps the %s run state visible alongside progress', (status, label) => {
    render(<RunProgress run={run({ status, activity: 'inactive', progress: progress({ phase: null, activity: null }) })} />)
    expect(screen.getByText(new RegExp(`${label} CP5`))).toBeTruthy()
  })

  it('uses a continuous bar for capped checkpoint lists', () => {
    const { container } = render(<RunProgress run={run({ progress: progress({
      total_checkpoints: count(31), approved_checkpoints: count(12), current_checkpoint_ordinal: 29,
    }) })} />)
    expect(container.querySelector('.compact-run-progress-bar')).toBeTruthy()
    expect(container.querySelectorAll('.compact-run-progress-segment')).toHaveLength(0)
  })

  it('renders each compact marker from its checkpoint identity rather than aggregate counts', () => {
    const { container } = render(<RunProgress run={run({ progress: progress({
      total_checkpoints: count(5), approved_checkpoints: count(4), recorded_complete_checkpoints: count(4),
      current_checkpoint_ordinal: 5,
      checkpoint_states: { '2': 'approved', '3': 'recorded_complete', '4': 'pending' },
    }) })} />)

    expect([...container.querySelectorAll('.compact-run-progress-segment')].map(segment => segment.className)).toEqual([
      'compact-run-progress-segment unknown',
      'compact-run-progress-segment approved',
      'compact-run-progress-segment recorded',
      'compact-run-progress-segment pending',
      'compact-run-progress-segment current',
    ])
  })

  it('keeps legacy or empty marker maps unknown outside the independently proven current checkpoint', () => {
    const { container } = render(<RunProgress run={run({ progress: progress({
      total_checkpoints: count(5), approved_checkpoints: count(4), recorded_complete_checkpoints: count(4),
      current_checkpoint_ordinal: 5, checkpoint_states: {},
    }) })} />)

    expect([...container.querySelectorAll('.compact-run-progress-segment')].map(segment => segment.className)).toEqual([
      'compact-run-progress-segment unknown',
      'compact-run-progress-segment unknown',
      'compact-run-progress-segment unknown',
      'compact-run-progress-segment unknown',
      'compact-run-progress-segment current',
    ])
  })
})
