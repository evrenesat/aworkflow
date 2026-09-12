import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import type {
  RunProgressCount,
  RunProgressDetail,
  RunProgressDetailCheckpoint,
  RunProgressDetailEvent,
  RunProgressExecutor,
  RunProgressSummary,
  RunStatus,
} from '../types'
import { CheckpointHistory, reconcileCheckpointSelection, type CheckpointSelectionState } from './CheckpointHistory'

function count(value: number | null, coverage: RunProgressCount['coverage'] = value === null ? 'unavailable' : 'complete'): RunProgressCount {
  return { value, coverage }
}

function executor(overrides: Partial<RunProgressExecutor> = {}): RunProgressExecutor {
  return {
    role: 'worker', team: 'base', selector: 'codex.worker', harness: 'codex', model: 'gpt-5', model_display: 'GPT-5', effort: 'high',
    source_run_id: 'run-current', invocation_id: 'invocation-1', turn_number: 3,
    started_at: '2026-09-11T10:00:00Z', ended_at: '2026-09-11T10:02:00Z', duration_seconds: 120,
    ...overrides,
  }
}

function checkpoint(overrides: Partial<RunProgressDetailCheckpoint>): RunProgressDetailCheckpoint {
  return {
    checkpoint_id: 'cp-1', ordinal: 1, title: 'Checkpoint 1: Build', status: 'approved', awaiting_review: false,
    worker_attempts: count(1), repair_passes: count(0), reviews: count(1), runtime_retries: count(0), applied_upgrades: count(0),
    recorded_at: '2026-09-11T09:00:00Z', duration_seconds: 60, scope_id: 'scope-1', generation_id: 'generation-1',
    parent_checkpoint_id: null, source_run_id: 'run-current', ...overrides,
  }
}

function event(overrides: Partial<RunProgressDetailEvent>): RunProgressDetailEvent {
  return {
    event_id: 'worker-1', checkpoint_id: 'cp-2', scope_id: 'scope-2', source_run_id: 'run-current', turn_number: 2, decision_number: null,
    kind: 'worker_attempt', outcome: 'completed', executor: executor(), started_at: '2026-09-11T10:00:00Z', ended_at: '2026-09-11T10:02:00Z',
    duration_seconds: 120, reason: 'initial implementation', source_reference: { run_id: 'run-current', turn_number: 2 }, ...overrides,
  }
}

function summary(overrides: Partial<RunProgressSummary> = {}): RunProgressSummary {
  return {
    schema_version: 1, availability: 'complete', observed_at: '2026-09-11T10:10:00Z', evidence_at: '2026-09-11T10:08:00Z', reason_codes: [],
    original_plan_identity: 'plan-identity', original_plan_display_name: 'run-progress.md', original_plan_path: 'plans/in-progress/run-progress.md',
    total_checkpoints: count(3), approved_checkpoints: count(1), recorded_complete_checkpoints: count(1),
    current_checkpoint_id: 'cp-2', current_checkpoint_ordinal: 2, current_checkpoint_title: 'Checkpoint 2: Repair',
    activity: 'active', phase: 'reviewing', run_status: 'running', current_executor: executor({ role: 'reviewer', selector: 'codex.reviewer', invocation_id: 'review-2', turn_number: 4 }),
    last_executor: executor({ role: 'worker', selector: 'codex.worker', invocation_id: 'worker-2', turn_number: 3 }),
    worker_attempts: count(2), repair_passes: count(1), reviews: count(2), runtime_retries: count(1), applied_upgrades: count(1), ...overrides,
  }
}

function detail(overrides: Partial<RunProgressDetail> = {}): RunProgressDetail {
  return {
    ...summary(),
    checkpoints: [
      checkpoint({}),
      checkpoint({ checkpoint_id: 'cp-2', ordinal: 2, title: 'Checkpoint 2: Repair', status: 'repairing', worker_attempts: count(2), repair_passes: count(1), reviews: count(2), runtime_retries: count(1), applied_upgrades: count(1), recorded_at: null, duration_seconds: null, scope_id: 'scope-2' }),
      checkpoint({ checkpoint_id: 'cp-3', ordinal: 3, title: 'Checkpoint 3: Pending', status: 'pending', worker_attempts: count(0), repair_passes: count(0), reviews: count(0), runtime_retries: count(0), applied_upgrades: count(0), recorded_at: null, duration_seconds: null, scope_id: 'scope-3' }),
    ],
    events: [
      event({ event_id: 'worker-2', kind: 'worker_attempt', turn_number: 2, started_at: '2026-09-11T10:00:00Z', reason: 'initial implementation' }),
      event({ event_id: 'rejection-1', kind: 'review_rejection', outcome: 'rejected', turn_number: 3, executor: executor({ role: 'reviewer', selector: 'codex.reviewer', invocation_id: 'review-1', turn_number: 3 }), reason: 'review requested a repair', started_at: '2026-09-11T10:03:00Z' }),
      event({ event_id: 'repair-1', kind: 'repair_attempt', outcome: 'completed', turn_number: 4, executor: executor({ selector: 'codex.repair', model_display: 'GPT-5 Repair', invocation_id: 'repair-1', turn_number: 4 }), reason: 'addressed review rejection', started_at: '2026-09-11T10:04:00Z' }),
      event({ event_id: 'retry-1', kind: 'runtime_retry', outcome: 'retrying', turn_number: 5, reason: 'provider retry', started_at: '2026-09-11T10:05:00Z', source_reference: { run_id: 'run-current', retry_of: 'repair-1' } }),
      event({ event_id: 'inherited-1', checkpoint_id: 'cp-1', source_run_id: 'run-predecessor', turn_number: 1, kind: 'worker_attempt', reason: 'inherited source evidence', started_at: '2026-09-11T09:00:00Z' }),
      event({ event_id: 'unassigned-1', checkpoint_id: null, source_run_id: 'run-current', turn_number: null, kind: 'runtime_retry', outcome: null, reason: 'checkpoint association unavailable', started_at: '2026-09-11T10:06:00Z' }),
    ],
    applied_changes: [{
      change_id: 'upgrade-1', status: 'applied', kind: 'worker_upgrade', roles: ['worker'], old_team: 'base', new_team: 'full', old_selector: 'codex.worker', new_selector: 'codex.repair',
      old_model: 'gpt-5', new_model: 'gpt-5-repair', old_effort: 'medium', new_effort: 'high', turn_number: 4, checkpoint_id: 'cp-2', generation_id: null, reason: 'explicit applied upgrade', recorded_at: '2026-09-11T10:04:00Z', source_reference: { run_id: 'run-current' },
    }],
    pending_changes: [{
      change_id: 'pending-1', status: 'pending', kind: 'team_change', roles: ['reviewer'], old_team: 'base', new_team: 'full', old_selector: null, new_selector: 'codex.reviewer', old_model: null, new_model: null, old_effort: null, new_effort: null,
      turn_number: null, checkpoint_id: 'cp-2', generation_id: null, reason: 'applies on next safe turn', recorded_at: null, source_reference: { run_id: 'run-current' },
    }],
    delivery: [
      { stage: 'final_review', status: 'succeeded', recorded_at: '2026-09-11T10:07:00Z', reason: null, source_reference: null },
      { stage: 'ci', status: 'unknown', recorded_at: null, reason: 'No CI receipt', source_reference: null },
    ],
    truncation: { evidence_bytes: 100, records_read: 10, checkpoints_read: 3, events_read: 6, omitted_records: 0, omitted_checkpoints: 0, notices: [] },
    ...overrides,
  }
}

const run = {
  run_id: 'run-current', status: 'running', schema_version: 1, ownership: 'control_plane', revision: 1, reason: null, unit_name: null, launch_phase: 'running',
  workflow_name: 'managed', team: 'base', current_step: 'implement', turns_completed: 4, max_turns: 8, selected_start_step: null, skipped_steps: [], restarted_from_run_id: null,
  started_at: '2026-09-11T09:00:00Z', ended_at: null, evidence: {},
} as RunStatus

function installMedia(matches: boolean) {
  let current = matches
  const listeners = new Set<() => void>()
  const media = {
    get matches() { return current },
    media: '(max-width: 959px), (max-height: 599px)',
    addEventListener: (_event: string, listener: () => void) => listeners.add(listener),
    removeEventListener: (_event: string, listener: () => void) => listeners.delete(listener),
    addListener: (listener: () => void) => listeners.add(listener),
    removeListener: (listener: () => void) => listeners.delete(listener),
  } as unknown as MediaQueryList
  Object.defineProperty(window, 'matchMedia', { configurable: true, value: () => media })
  return { setMatches(next: boolean) { current = next; listeners.forEach(listener => listener()) } }
}

const originalMatchMedia = window.matchMedia
const originalScrollTo = window.scrollTo

afterEach(() => {
  cleanup()
  if (originalMatchMedia) Object.defineProperty(window, 'matchMedia', { configurable: true, value: originalMatchMedia })
  else delete (window as Window & { matchMedia?: typeof window.matchMedia }).matchMedia
  if (originalScrollTo) Object.defineProperty(window, 'scrollTo', { configurable: true, value: originalScrollTo })
  document.body.innerHTML = ''
})

describe('CheckpointHistory', () => {
  it('preserves an explicit current-run CP4 against a stale CP5 default', () => {
    const selected: CheckpointSelectionState = {
      runKey: 'project-current/run-current',
      key: 'checkpoint:cp-4',
      notice: null,
    }
    const reconciled = reconcileCheckpointSelection(selected, {
      runKey: 'project-current/run-current',
      preferredKey: 'checkpoint:cp-5',
      availableKeys: new Set(['checkpoint:cp-4', 'checkpoint:cp-5']),
    })

    expect(reconciled).toBe(selected)
    expect(reconciled.key).toBe('checkpoint:cp-4')

    const switchedRun = reconcileCheckpointSelection(selected, {
      runKey: 'project-successor/run-current',
      preferredKey: 'checkpoint:cp-4',
      availableKeys: new Set(['checkpoint:cp-4', 'checkpoint:cp-5']),
    })
    expect(switchedRun).toEqual({ runKey: 'project-successor/run-current', key: 'checkpoint:cp-4', notice: null })
  })

  it('shows the worker, rejection, repair and upgrade evidence without inventing delivery', () => {
    render(<CheckpointHistory projectId="project-current" run={run} progress={summary()} detail={detail()} />)

    expect(screen.getByText('1 of 3 checkpoints approved')).toBeDefined()
    expect(screen.getAllByText(/CP2 of 3 · Reviewing/).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/codex.reviewer/).length).toBeGreaterThan(0)
    fireEvent.click(screen.getByText('Details'))
    expect(screen.getByText('Current attempt')).toBeDefined()

    fireEvent.click(screen.getByRole('button', { name: /Checkpoint 2: Repair/ }))
    expect(screen.getByText('Review rejection')).toBeDefined()
    expect(screen.getByText('Repair attempt')).toBeDefined()
    expect(screen.getByRole('link', { name: 'Retry of recorded event' }).getAttribute('href')).toBe('#checkpoint-event-repair-1')
    expect(screen.getByText('Unassigned history')).toBeDefined()
    fireEvent.click(screen.getByRole('button', { name: /Checkpoint 1: Build/ }))
    expect(screen.getByText(/Inherited from run run-predecessor/)).toBeDefined()

    fireEvent.click(screen.getByText('Team & change history'))
    expect(screen.getAllByText(/base → full/).length).toBeGreaterThan(0)
    expect(screen.getByText('Pending changes')).toBeDefined()
    expect(screen.getByText(/applies on next safe turn/)).toBeDefined()

    fireEvent.click(screen.getByText('Delivery evidence'))
    expect(screen.getAllByText('Final review').length).toBeGreaterThan(0)
    const delivery = document.querySelector('.checkpoint-history-delivery-list')
    expect(delivery?.textContent).toContain('Succeeded')
    expect(delivery?.textContent).toContain('CI')
    expect(delivery?.textContent).toContain('Unknown')
    fireEvent.click(screen.getByText('Count definitions & evidence'))
    expect(screen.getByText(/Partial values are lower bounds/)).toBeDefined()
  })

  it('links canonical retry completions only to the matching source run, turn, and role', () => {
    const canonical = detail({
      events: [
        event({ event_id: 'worker-current-2', turn_number: 2, executor: executor({ role: 'worker', source_run_id: 'run-current', turn_number: 2 }) }),
        event({ event_id: 'review-current-2', kind: 'review', turn_number: 2, executor: executor({ role: 'reviewer', source_run_id: 'run-current', turn_number: 2 }) }),
        event({ event_id: 'retry-worker', kind: 'runtime_retry', turn_number: 1, executor: executor({ role: 'worker', source_run_id: 'run-current', turn_number: 1 }), source_reference: { completion_turn_number: 2 } }),
        event({ event_id: 'review-inherited-2', kind: 'review', source_run_id: 'run-predecessor', turn_number: 2, executor: executor({ role: 'reviewer', source_run_id: 'run-predecessor', turn_number: 2 }) }),
        event({ event_id: 'retry-review-inherited', kind: 'runtime_retry', source_run_id: 'run-predecessor', turn_number: 1, executor: executor({ role: 'reviewer', source_run_id: 'run-predecessor', turn_number: 1 }), source_reference: { completion_turn_number: 2 } }),
      ],
    })
    render(<CheckpointHistory projectId="project-current" run={run} progress={canonical} detail={canonical} />)

    const workerRetry = document.getElementById('checkpoint-event-retry-worker') as HTMLElement
    const inheritedRetry = document.getElementById('checkpoint-event-retry-review-inherited') as HTMLElement
    expect(within(workerRetry).getByRole('link', { name: 'Retried invocation: turn 2' }).getAttribute('href')).toBe('#checkpoint-event-worker-current-2')
    expect(within(inheritedRetry).getByRole('link', { name: 'Retried invocation: turn 2' }).getAttribute('href')).toBe('#checkpoint-event-review-inherited-2')
  })

  it('keeps resolved completion turns truthful when no unique invocation is returned', () => {
    const bounded = detail({
      events: [
        event({ event_id: 'retry-missing', kind: 'runtime_retry', turn_number: 1, source_reference: { completion_turn_number: 8 } }),
        event({ event_id: 'retry-ambiguous', kind: 'runtime_retry', turn_number: 2, source_reference: { completion_turn_number: 3 } }),
        event({ event_id: 'worker-3', turn_number: 3 }),
        event({ event_id: 'repair-3', kind: 'repair_attempt', turn_number: 3 }),
        event({ event_id: 'retry-pending', kind: 'runtime_retry', turn_number: 4, source_reference: { turn_number: 4 } }),
        event({ event_id: 'retry-invalid', kind: 'runtime_retry', turn_number: 5, source_reference: { completion_turn_number: '6' } }),
      ],
    })
    render(<CheckpointHistory projectId="project-current" run={run} progress={bounded} detail={bounded} />)

    expect(screen.getAllByText('Retried invocation: turn 8 (no unique invocation returned in this view).')).toHaveLength(1)
    expect(screen.getAllByText('Retried invocation: turn 3 (no unique invocation returned in this view).')).toHaveLength(1)
    expect(screen.queryByRole('link', { name: 'Retried invocation: turn 8' })).toBeNull()
    expect(screen.queryByRole('link', { name: 'Retried invocation: turn 3' })).toBeNull()
    expect(screen.getAllByText('Retry relationship not established by the evidence.')).toHaveLength(2)
  })

  it('keeps the successor executor summary separate from inherited review history', () => {
    const successor = executor({ selector: 'successor.worker', source_run_id: 'successor-run' })
    const inherited = event({
      event_id: 'inherited-review', checkpoint_id: 'cp-1', kind: 'review_rejection', outcome: 'rejected',
      source_run_id: 'predecessor-run', turn_number: 20,
      executor: executor({ role: 'reviewer', selector: 'predecessor.reviewer', source_run_id: 'predecessor-run', turn_number: 20 }),
      reason: 'The predecessor requested a repair.', source_reference: { run_id: 'predecessor-run', turn_number: 20 },
    })
    const projected = detail({ current_executor: null, last_executor: successor, events: [inherited] })
    render(<CheckpointHistory projectId="project-current" run={{ ...run, run_id: 'successor-run' }} progress={projected} detail={projected} />)

    expect(screen.getByText('Last executor').parentElement?.textContent).toContain('successor.worker')
    fireEvent.click(screen.getByRole('button', { name: /Checkpoint 1: Build/ }))
    expect(screen.getByText(/Inherited from run predecessor-run/)).toBeDefined()
    expect(within(document.getElementById('checkpoint-event-inherited-review') as HTMLElement).getAllByText(/predecessor\.reviewer/).length).toBeGreaterThan(0)
  })

  it('retains an earlier checkpoint through progress refresh and resets on a new run', () => {
    const view = render(<CheckpointHistory projectId="project-current" run={run} progress={summary()} detail={detail()} />)
    fireEvent.click(screen.getByRole('button', { name: /Checkpoint 1: Build/ }))
    expect(document.querySelector('.checkpoint-history-detail-heading h5')?.textContent).toBe('Checkpoint 1: Build')

    const advanced = summary({ current_checkpoint_id: 'cp-3', current_checkpoint_ordinal: 3, current_checkpoint_title: 'Checkpoint 3: Pending', phase: 'implementing' })
    view.rerender(<CheckpointHistory projectId="project-current" run={run} progress={advanced} detail={detail({ ...advanced, checkpoints: detail().checkpoints })} />)
    expect(document.querySelector('.checkpoint-history-detail-heading h5')?.textContent).toBe('Checkpoint 1: Build')

    const successor = { ...run, run_id: 'run-successor', restarted_from_run_id: 'run-current' }
    const successorSummary = summary({ current_checkpoint_id: 'cp-3', current_checkpoint_ordinal: 3, current_checkpoint_title: 'Checkpoint 3: Pending' })
    view.rerender(<CheckpointHistory projectId="project-current" run={successor} progress={successorSummary} detail={detail({ ...successorSummary })} />)
    expect(document.querySelector('.checkpoint-history-detail-heading h5')?.textContent).toBe('Checkpoint 3: Pending')
    expect(document.querySelector('.checkpoint-history-detail > p:last-child')?.textContent).toContain('successor of run-current')
  })

  it('retains an explicit CP4 through a CP5 default refresh and follows Current checkpoint on request', () => {
    const cp4 = checkpoint({ checkpoint_id: 'cp-4', ordinal: 4, title: 'Checkpoint 4: Reviewed', status: 'approved' })
    const cp5 = checkpoint({ checkpoint_id: 'cp-5', ordinal: 5, title: 'Checkpoint 5: Active', status: 'implementing' })
    const initialSummary = summary({ current_checkpoint_id: 'cp-5', current_checkpoint_ordinal: 5, current_checkpoint_title: 'Checkpoint 5: Active' })
    const initial = detail({ ...initialSummary, checkpoints: [cp4, cp5], events: [] })
    const view = render(<CheckpointHistory projectId="project-current" run={run} progress={initialSummary} detail={initial} />)

    fireEvent.click(screen.getByRole('button', { name: /Checkpoint 4: Reviewed/ }))
    expect(document.querySelector('.checkpoint-history-detail-heading h5')?.textContent).toBe('Checkpoint 4: Reviewed')

    const refreshedSummary = summary({ ...initialSummary, phase: 'reviewing' })
    const refreshed = detail({ ...refreshedSummary, checkpoints: [
      { ...cp4, status: 'recorded_complete' },
      { ...cp5, status: 'implementing' },
    ], events: [] })
    view.rerender(<CheckpointHistory projectId="project-current" run={run} progress={refreshedSummary} detail={refreshed} />)
    expect(document.querySelector('.checkpoint-history-detail-heading h5')?.textContent).toBe('Checkpoint 4: Reviewed')

    fireEvent.click(screen.getByRole('button', { name: 'Current checkpoint' }))
    expect(document.querySelector('.checkpoint-history-detail-heading h5')?.textContent).toBe('Checkpoint 5: Active')
  })

  it('shows the existing fallback notice when the selected checkpoint is removed', () => {
    const cp4 = checkpoint({ checkpoint_id: 'cp-4', ordinal: 4, title: 'Checkpoint 4: Reviewed', status: 'approved' })
    const cp5 = checkpoint({ checkpoint_id: 'cp-5', ordinal: 5, title: 'Checkpoint 5: Active', status: 'implementing' })
    const currentSummary = summary({ current_checkpoint_id: 'cp-5', current_checkpoint_ordinal: 5, current_checkpoint_title: 'Checkpoint 5: Active' })
    const initial = detail({ ...currentSummary, checkpoints: [cp4, cp5], events: [] })
    const view = render(<CheckpointHistory projectId="project-current" run={run} progress={currentSummary} detail={initial} />)

    fireEvent.click(screen.getByRole('button', { name: /Checkpoint 4: Reviewed/ }))
    const refreshed = detail({ ...currentSummary, checkpoints: [cp5], events: [] })
    view.rerender(<CheckpointHistory projectId="project-current" run={run} progress={currentSummary} detail={refreshed} />)

    expect(document.querySelector('.checkpoint-history-detail-heading h5')?.textContent).toBe('Checkpoint 5: Active')
    expect(screen.getByText(/previously selected checkpoint is no longer in the refreshed bounded history/)).toBeDefined()
  })

  it('keeps the selected parent checkpoint through generation and current-checkpoint refresh', () => {
    const initial = detail({
      checkpoints: [
        checkpoint({ checkpoint_id: 'plan-lineage::checkpoint-1', generation_id: 'generation-one' }),
        checkpoint({ checkpoint_id: 'plan-lineage::checkpoint-2', ordinal: 2, title: 'Checkpoint 2: Repair', generation_id: null }),
      ],
      events: [event({ checkpoint_id: 'plan-lineage::checkpoint-1', kind: 'review_rejection', outcome: 'rejected', reason: 'Repair the review finding.' })],
    })
    const view = render(<CheckpointHistory projectId="project-current" run={run} progress={initial} detail={initial} />)
    fireEvent.click(screen.getByRole('button', { name: /Checkpoint 1: Build/ }))
    expect(screen.getByText('Review rejection')).toBeDefined()

    const refreshedSummary = summary({
      current_checkpoint_id: 'plan-lineage::checkpoint-2',
      current_checkpoint_ordinal: 2,
      current_checkpoint_title: 'Checkpoint 2: Repair',
      phase: 'implementing',
    })
    const refreshed = detail({
      ...refreshedSummary,
      checkpoints: [
        checkpoint({ checkpoint_id: 'plan-lineage::checkpoint-1', generation_id: 'generation-two' }),
        checkpoint({ checkpoint_id: 'plan-lineage::checkpoint-2', ordinal: 2, title: 'Checkpoint 2: Repair', generation_id: null }),
      ],
      events: [event({ checkpoint_id: 'plan-lineage::checkpoint-1', kind: 'review_rejection', outcome: 'rejected', reason: 'Repair the review finding.' })],
    })
    view.rerender(<CheckpointHistory projectId="project-current" run={run} progress={refreshedSummary} detail={refreshed} />)
    expect(document.querySelector('.checkpoint-history-detail-heading h5')?.textContent).toBe('Checkpoint 1: Build')
    expect(screen.getByText('Review rejection')).toBeDefined()
  })

  it('labels queued review separately from an active reviewer', () => {
    const queued = detail({
      checkpoints: [checkpoint({ status: 'reviewing', awaiting_review: true })],
      events: [],
    })
    const view = render(<CheckpointHistory projectId="project-current" run={run} progress={queued} detail={queued} />)
    expect(screen.getByText('Awaiting review')).toBeDefined()
    fireEvent.click(screen.getByRole('button', { name: /Checkpoint 1: Build/ }))
    expect(screen.getByText(/Awaiting review · cp-1/)).toBeDefined()

    const active = detail({
      checkpoints: [checkpoint({ status: 'reviewing', awaiting_review: false })],
      events: [],
    })
    view.rerender(<CheckpointHistory projectId="project-current" run={run} progress={active} detail={active} />)
    expect(screen.getByText(/Reviewing · cp-1/)).toBeDefined()
  })

  it('renders each retained repartition generation in the existing change history', () => {
    const generations = detail({
      applied_changes: [
        { ...detail().applied_changes[0], change_id: 'generation-one', kind: 'repartition', checkpoint_id: 'cp-1', generation_id: 'generation-one' },
        { ...detail().applied_changes[0], change_id: 'generation-two', kind: 'repartition', checkpoint_id: 'cp-1', generation_id: 'generation-two' },
      ],
    })
    render(<CheckpointHistory projectId="project-current" run={run} progress={generations} detail={generations} />)
    fireEvent.click(screen.getByRole('button', { name: /Checkpoint 1: Build/ }))
    fireEvent.click(screen.getByText('Team & change history'))
    expect(screen.getByText(/generation generation-one/)).toBeDefined()
    expect(screen.getByText(/generation generation-two/)).toBeDefined()
  })

  it('retains compact selection and focus when lineage IDs survive plan checkbox edits', async () => {
    const media = installMedia(true)
    Object.defineProperty(window, 'scrollTo', { configurable: true, value: () => {} })
    const initialDetail = detail()
    const lineageCheckpoints = initialDetail.checkpoints.map((checkpoint, index) => ({
      ...checkpoint,
      checkpoint_id: `plan-lineage::checkpoint-${index + 1}`,
    }))
    const initialSummary = summary({
      current_checkpoint_id: 'plan-lineage::checkpoint-2',
      current_checkpoint_ordinal: 2,
      current_checkpoint_title: 'Checkpoint 2: Repair',
    })
    const view = render(
      <CheckpointHistory
        projectId="project-current"
        run={run}
        progress={initialSummary}
        detail={detail({ ...initialSummary, checkpoints: lineageCheckpoints })}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: /Checkpoint 1: Build/ }))
    expect(document.querySelector('.checkpoint-history-detail-heading h5')?.textContent).toBe('Checkpoint 1: Build')
    expect(document.activeElement?.textContent).toContain('Checkpoint 1: Build')

    const advancedSummary = summary({
      current_checkpoint_id: 'plan-lineage::checkpoint-2',
      current_checkpoint_ordinal: 2,
      current_checkpoint_title: 'Checkpoint 2: Repair',
      phase: 'reviewing',
    })
    const advancedCheckpoints = lineageCheckpoints.map((checkpoint, index) => ({
      ...checkpoint,
      status: index === 0 ? 'recorded_complete' as const : 'reviewing' as const,
    }))
    view.rerender(
      <CheckpointHistory
        projectId="project-current"
        run={run}
        progress={advancedSummary}
        detail={detail({ ...advancedSummary, checkpoints: advancedCheckpoints })}
      />,
    )

    const navigation = document.querySelector('.checkpoint-history-layout .sidebar-editor-navigation') as HTMLElement
    const detailSurface = document.querySelector('.checkpoint-history-layout .sidebar-editor-detail') as HTMLElement
    expect(document.querySelector('.checkpoint-history-detail-heading h5')?.textContent).toBe('Checkpoint 1: Build')
    expect(detailSurface.hidden).toBe(false)
    expect(navigation.hidden).toBe(true)

    fireEvent.click(screen.getByRole('button', { name: /Back to Checkpoints/ }))
    expect(navigation.hidden).toBe(false)
    await act(async () => {
      await new Promise<void>(resolve => setTimeout(resolve, 0))
    })
    expect(document.activeElement?.getAttribute('data-sidebar-editor-item')).toBe('checkpoint:plan-lineage::checkpoint-1')
    act(() => media.setMatches(false))
  })

  it('uses compact list-detail navigation, current action, and visible partial-history notices', () => {
    const media = installMedia(true)
    Object.defineProperty(window, 'scrollTo', { configurable: true, value: () => {} })
    const partial = detail({
      ...summary({ availability: 'partial', reason_codes: ['history_partial'], approved_checkpoints: count(1, 'partial') }),
      truncation: { evidence_bytes: 100, records_read: 10, checkpoints_read: 2, events_read: 5, omitted_records: 2, omitted_checkpoints: 1, notices: ['Earlier history unavailable in this bounded view'] },
    })
    const view = render(<CheckpointHistory projectId="project-current" run={run} progress={partial} detail={partial} />)
    const navigation = document.querySelector('.checkpoint-history-layout .sidebar-editor-navigation') as HTMLElement
    const detailSurface = document.querySelector('.checkpoint-history-layout .sidebar-editor-detail') as HTMLElement
    expect(navigation.hidden).toBe(false)
    expect(detailSurface.hidden).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: /Checkpoint 1: Build/ }))
    expect(screen.getByRole('button', { name: /Back to Checkpoints/ })).toBeDefined()
    expect(detailSurface.hidden).toBe(false)
    fireEvent.click(screen.getByRole('button', { name: /Back to Checkpoints/ }))
    expect(navigation.hidden).toBe(false)
    fireEvent.click(screen.getByRole('button', { name: 'Current checkpoint' }))
    expect(document.querySelector('.checkpoint-history-detail-heading h5')?.textContent).toBe('Checkpoint 2: Repair')

    fireEvent.click(screen.getByText('Count definitions & evidence'))
    expect(screen.getByText(/Earlier history unavailable in this bounded view/)).toBeDefined()
    act(() => media.setMatches(false))
    expect(detailSurface.hidden).toBe(false)
    view.unmount()
  })

  it('separates unassigned, whole-plan, and outside-page history', () => {
    const grouped = detail({
      events: [
        event({ event_id: 'whole-plan-review', checkpoint_id: null, association: 'whole_plan', kind: 'review' }),
        event({ event_id: 'outside-review', checkpoint_id: null, association: 'outside_returned', kind: 'review' }),
        event({ event_id: 'unknown-history', checkpoint_id: null, association: 'unassigned', kind: 'history' }),
      ],
      truncation: {
        evidence_bytes: 100,
        records_read: 10,
        checkpoints_read: 3,
        events_read: 3,
        omitted_records: 4,
        omitted_checkpoints: 2,
        response_limit_records: 4,
        response_limit_checkpoints: 2,
        notices: ['Earlier history unavailable in this bounded view'],
      },
    })
    render(<CheckpointHistory projectId="project-current" run={run} progress={grouped} detail={grouped} />)

    expect(screen.getByRole('button', { name: /Unassigned history/ })).toBeDefined()
    expect(screen.getByRole('button', { name: /Whole-plan review history/ })).toBeDefined()
    expect(screen.getByRole('button', { name: /History outside returned checkpoints/ })).toBeDefined()

    fireEvent.click(screen.getByRole('button', { name: /Whole-plan review history/ }))
    expect(screen.getByText(/does not prove approval for a returned checkpoint/)).toBeDefined()
    fireEvent.click(screen.getByRole('button', { name: /History outside returned checkpoints/ }))
    expect(screen.getByText(/no placeholder checkpoint was created/)).toBeDefined()
    fireEvent.click(screen.getByText('Count definitions & evidence'))
    expect(screen.getByText(/History omitted by the response limit: 4 records and 2 checkpoints\./)).toBeDefined()
  })

  it('deduplicates repeated event identities and keeps unknown fields explicit', () => {
    const duplicate = event({ event_id: 'duplicate-1', kind: 'review', outcome: null, executor: null, duration_seconds: null, reason: null })
    const repeated = detail({ events: [duplicate, duplicate], availability: 'unavailable', reason_codes: ['missing_plan'], checkpoints: [] })
    render(<CheckpointHistory projectId="project-current" run={run} progress={repeated} detail={repeated} />)
    expect(screen.getAllByText('Evidence unavailable').length).toBeGreaterThan(0)
    fireEvent.click(screen.getByRole('button', { name: /Unassigned history/ }))
    expect(screen.getByText('1 unique event')).toBeDefined()
    expect(screen.getByText('Outcome not reported')).toBeDefined()
    expect(document.querySelector('.checkpoint-history-event-meta')?.textContent).toContain('Not reported')
    expect(screen.getByText('Duration', { selector: 'dt' })).toBeDefined()
  })

  it('shows missing delivery evidence as unavailable instead of successful', () => {
    const missing = detail({ availability: 'partial', reason_codes: ['invalid_evidence'], delivery: [] })
    render(<CheckpointHistory projectId="project-current" run={run} progress={missing} detail={missing} />)
    fireEvent.click(screen.getByText('Delivery evidence'))
    expect(screen.getByText('Delivery evidence not reported.')).toBeDefined()
    expect(screen.queryByText('Succeeded')).toBeNull()
  })

  it('renders compact event disclosures and retains an expanded event through refresh', () => {
    const initial = detail({
      events: [event({
        event_id: 'stable-review', checkpoint_id: 'cp-2', kind: 'review_rejection', outcome: 'rejected', turn_number: 3,
        executor: executor({ role: 'reviewer', selector: 'codex.reviewer', invocation_id: 'review-stable', turn_number: 3 }),
        started_at: '2026-09-11T10:03:00Z', ended_at: '2026-09-11T10:04:00Z', duration_seconds: 60,
        reason: 'Review requested a repair.', source_reference: { run_id: 'run-current', artifact: 'turns/turn-003/result.json', turn_number: 3 },
      })],
    })
    const view = render(<CheckpointHistory projectId="project-current" run={run} progress={initial} detail={initial} />)
    fireEvent.click(screen.getByRole('button', { name: /Checkpoint 2: Repair/ }))

    const eventDisclosure = document.querySelector('#checkpoint-event-stable-review details') as HTMLDetailsElement
    expect(eventDisclosure.querySelector('summary')?.textContent).toContain('Turn 3')
    expect(eventDisclosure.querySelector('summary')?.textContent).toContain('Reviewer')
    expect(eventDisclosure.querySelector('summary')?.textContent).toContain('codex.reviewer')
    expect(eventDisclosure.querySelector('summary')?.textContent).toContain('1m 0s')
    expect(eventDisclosure.querySelector('summary')?.textContent).toContain('Rejected')

    // jsdom does not run the native <details> toggle default action; dispatch
    // the toggle event after setting the property to model a browser toggle.
    eventDisclosure.open = true
    fireEvent(eventDisclosure, new Event('toggle'))
    expect(eventDisclosure.hasAttribute('open')).toBe(true)
    expect(within(eventDisclosure).getByText('Event identity', { selector: 'dt' })).toBeDefined()
    expect(within(eventDisclosure).getByText(/run run-current · evidence turns\/turn-003\/result\.json · turn 3/)).toBeDefined()
    expect(within(eventDisclosure).getByText(/Reason: Review requested a repair\./)).toBeDefined()
    expect(within(eventDisclosure).getAllByText(/UTC|GMT/).length).toBeGreaterThan(0)

    const refreshed = detail({ ...initial, events: [{ ...initial.events[0], outcome: 'rejected' }] })
    view.rerender(<CheckpointHistory projectId="project-current" run={run} progress={refreshed} detail={refreshed} />)
    expect((document.querySelector('#checkpoint-event-stable-review details') as HTMLDetailsElement).hasAttribute('open')).toBe(true)
  })

  it('summarizes each delivery gate and opens the selected run receipts', () => {
    const delivered = detail({
      delivery: [
        { stage: 'Final review', status: 'succeeded', recorded_at: '2026-09-11T10:07:00Z', reason: null, source_reference: { run_id: 'run-current', artifact: 'review/result.json' } },
        { stage: 'Merge', status: 'succeeded', recorded_at: '2026-09-11T10:08:00Z', reason: null, source_reference: { run_id: 'run-current', artifact: 'merge/receipt.json' } },
        { stage: 'Publish', status: 'unknown', recorded_at: null, reason: 'Publication receipt is pending.', source_reference: null },
        { stage: 'CI', status: 'failed', recorded_at: '2026-09-11T10:09:00Z', reason: 'CI receipt failed.', source_reference: { run_id: 'run-current', artifact: 'ci/receipt.json' } },
        { stage: 'Live verification', status: 'not_applicable', recorded_at: null, reason: 'No live verification receipt.', source_reference: null },
      ],
    })
    render(<CheckpointHistory projectId="project-current" run={run} progress={delivered} detail={delivered} />)

    const summaryLine = document.querySelector('.checkpoint-history-delivery-summary') as HTMLElement
    expect(summaryLine.textContent).toContain('Final review: Succeeded')
    expect(summaryLine.textContent).toContain('Merge: Succeeded')
    expect(summaryLine.textContent).toContain('Publication: Unknown')
    expect(summaryLine.textContent).toContain('CI: Failed')
    expect(summaryLine.textContent).toContain('Live: Not applicable')
    expect(summaryLine.textContent).toContain('View detailed receipts')

    fireEvent.click(screen.getByRole('link', { name: 'View detailed receipts' }))
    expect(document.getElementById('checkpoint-history-delivery-evidence')?.hasAttribute('open')).toBe(true)
    expect(screen.getByText('CI receipt failed.')).toBeDefined()
    expect(screen.getByText(/ci\/receipt\.json/)).toBeDefined()
  })

  it('deduplicates overlapping worker and reviewer intervals before computing unattributed time', () => {
    const terminalRun = {
      ...run,
      status: 'done' as const,
      activity: 'inactive' as const,
      started_at: '2026-09-11T09:00:00Z',
      ended_at: '2026-09-11T10:30:00Z',
    }
    const timed = detail({
      events: [
        event({
          event_id: 'worker-timing', checkpoint_id: 'cp-2', turn_number: 1,
          executor: executor({ role: 'worker', selector: 'codex.worker', invocation_id: 'worker-timing', turn_number: 1, started_at: '2026-09-11T10:00:00Z', ended_at: '2026-09-11T10:10:00Z', duration_seconds: 600 }),
          started_at: '2026-09-11T10:00:00Z', ended_at: '2026-09-11T10:10:00Z', duration_seconds: 600,
        }),
        event({
          event_id: 'worker-timing-duplicate', checkpoint_id: 'cp-2', turn_number: 1,
          executor: executor({ role: 'worker', selector: 'codex.worker', invocation_id: 'worker-timing', turn_number: 1, started_at: '2026-09-11T10:00:00Z', ended_at: '2026-09-11T10:10:00Z', duration_seconds: 600 }),
          started_at: '2026-09-11T10:00:00Z', ended_at: '2026-09-11T10:10:00Z', duration_seconds: 600,
        }),
        event({
          event_id: 'reviewer-timing', checkpoint_id: 'cp-2', kind: 'review', turn_number: 2,
          executor: executor({ role: 'reviewer', selector: 'codex.reviewer', invocation_id: 'reviewer-timing', turn_number: 2, started_at: '2026-09-11T10:05:00Z', ended_at: '2026-09-11T10:20:00Z', duration_seconds: 900 }),
          started_at: '2026-09-11T10:05:00Z', ended_at: '2026-09-11T10:20:00Z', duration_seconds: 900,
        }),
      ],
    })
    render(<CheckpointHistory projectId="project-current" run={terminalRun} progress={timed} detail={timed} />)
    fireEvent.click(screen.getByText('Time details'))

    expect(screen.getByText('Total run elapsed', { selector: 'dt' }).parentElement?.textContent).toContain('1h 30m')
    expect(screen.getByText('Known invocation coverage', { selector: 'dt' }).parentElement?.textContent).toContain('20m 0s')
    expect(screen.getByText('Unattributed time', { selector: 'dt' }).parentElement?.textContent).toContain('1h 10m')
    expect(screen.getByText('Recorded duration total: 10m 0s')).toBeDefined()
    expect(screen.getByText('Recorded duration total: 15m 0s')).toBeDefined()
    expect(screen.getAllByText(/Invocation worker-timing ·/)).toHaveLength(1)
    expect(screen.getByText(/union of complete, non-overlapping/)).toBeDefined()
    expect(screen.getByText(/separate from browser data-load latency/)).toBeDefined()
  })

  it('deduplicates review and approval timing for the same legacy turn', () => {
    const terminalRun = {
      ...run,
      status: 'completed' as const,
      activity: 'inactive' as const,
      started_at: '2026-09-12T13:00:00Z',
      ended_at: '2026-09-12T13:11:23Z',
    }
    const workerInterval = {
      started_at: '2026-09-12T13:00:03Z',
      ended_at: '2026-09-12T13:03:50Z',
    }
    const reviewerInterval = {
      started_at: '2026-09-12T13:08:57Z',
      ended_at: '2026-09-12T13:11:23Z',
    }
    const legacyWorker = event({
      event_id: 'legacy-worker-timing', checkpoint_id: 'cp-2', kind: 'worker_attempt', turn_number: 1,
      executor: executor({ role: 'worker', source_run_id: 'run-current', invocation_id: null, turn_number: 1, ...workerInterval, duration_seconds: null }),
      ...workerInterval, duration_seconds: null,
    })
    const legacyReview = event({
      event_id: 'legacy-review-timing', checkpoint_id: 'cp-2', kind: 'review', turn_number: 2,
      executor: executor({ role: 'reviewer', source_run_id: 'run-current', invocation_id: null, turn_number: 2, ...reviewerInterval, duration_seconds: null }),
      ...reviewerInterval, duration_seconds: null,
    })
    const legacyApproval = event({
      event_id: 'legacy-approval-timing', checkpoint_id: 'cp-2', kind: 'checkpoint_approval', turn_number: 2,
      executor: executor({ role: 'reviewer', source_run_id: 'run-current', invocation_id: null, turn_number: 2, ...reviewerInterval, duration_seconds: null }),
      ...reviewerInterval, duration_seconds: null,
    })
    const otherSourceReview = event({
      event_id: 'other-source-review', checkpoint_id: 'cp-2', kind: 'review', turn_number: 2,
      source_run_id: 'other-run', started_at: null, ended_at: null, duration_seconds: null,
      executor: executor({ role: 'reviewer', source_run_id: 'other-run', invocation_id: null, turn_number: 2, started_at: null, ended_at: null, duration_seconds: null }),
    })
    const sameTurnWorker = event({
      event_id: 'same-turn-worker', checkpoint_id: 'cp-2', kind: 'worker_attempt', turn_number: 2,
      source_run_id: 'run-current', started_at: null, ended_at: null, duration_seconds: null,
      executor: executor({ role: 'worker', source_run_id: 'run-current', invocation_id: null, turn_number: 2, started_at: null, ended_at: null, duration_seconds: null }),
    })
    const timed = detail({
      events: [legacyWorker, legacyReview, legacyApproval, otherSourceReview, sameTurnWorker],
    })
    render(<CheckpointHistory projectId="project-current" run={terminalRun} progress={timed} detail={timed} />)
    fireEvent.click(screen.getByText('Time details'))

    expect(screen.getByText('Known invocation coverage', { selector: 'dt' }).parentElement?.textContent).toContain('6m 13s')
    expect(screen.getByText('Recorded duration total: 3m 47s')).toBeDefined()
    expect(screen.getByText('Recorded duration total: 2m 26s')).toBeDefined()
    expect(screen.queryByText('Recorded duration total: 4m 52s')).toBeNull()

    const workerGroup = screen.getByRole('heading', { name: 'Worker invocations' }).parentElement as HTMLElement
    const reviewerGroup = screen.getByRole('heading', { name: 'Reviewer invocations' }).parentElement as HTMLElement
    const workerRows = within(workerGroup).getAllByRole('listitem')
    const reviewerRows = within(reviewerGroup).getAllByRole('listitem')
    expect(workerRows).toHaveLength(2)
    expect(workerRows.some(row => row.textContent?.includes('Turn 2 invocation'))).toBe(true)
    expect(reviewerRows).toHaveLength(2)
    expect(reviewerRows.filter(row => !row.textContent?.includes('source run other-run'))).toHaveLength(1)
    expect(reviewerRows.some(row => row.textContent?.includes('2m 26s'))).toBe(true)
    expect(document.getElementById('checkpoint-event-legacy-review-timing')).toBeDefined()
    expect(document.getElementById('checkpoint-event-legacy-approval-timing')).toBeDefined()
  })

  it('labels missing timing boundaries and duration-only records without inventing a remainder', () => {
    const partial = detail({
      availability: 'partial',
      reason_codes: ['history_partial'],
      events: [
        event({
          event_id: 'duration-only', checkpoint_id: 'cp-2', started_at: null, ended_at: null, duration_seconds: 45,
          executor: executor({ invocation_id: 'duration-only', started_at: null, ended_at: null, duration_seconds: 45 }),
        }),
        event({
          event_id: 'missing-end', checkpoint_id: 'cp-2', started_at: '2026-09-11T10:03:00Z', ended_at: null, duration_seconds: null,
          executor: executor({ invocation_id: 'missing-end', started_at: '2026-09-11T10:03:00Z', ended_at: null, duration_seconds: null }),
        }),
      ],
      truncation: { evidence_bytes: 100, records_read: 3, checkpoints_read: 3, events_read: 2, omitted_records: 1, omitted_checkpoints: 0, notices: ['Some timing history is missing'] },
    })
    render(<CheckpointHistory projectId="project-current" run={run} progress={partial} detail={partial} />)
    fireEvent.click(screen.getByText('Time details'))

    expect(screen.getByText(/Timing breakdown is partial/)).toBeDefined()
    expect(screen.getByText(/Individual durations are shown/)).toBeDefined()
    expect(screen.queryByText('Unattributed time', { selector: 'dt' })).toBeNull()
    expect(screen.getByText(/records without both boundaries/)).toBeDefined()
  })

  it('keeps terminal rows outcome-focused and removes current-work placeholders', () => {
    const terminal = { ...run, status: 'failed', activity: 'inactive' as const, ended_at: '2026-09-12T12:00:00Z' }
    render(<CheckpointHistory projectId="project-current" run={terminal} progress={summary()} detail={detail()} />)

    expect(screen.getByText(/^Finished /)).toBeDefined()
    expect(screen.getByText('Elapsed')).toBeDefined()
    expect(screen.queryByText('Current checkpoint', { selector: 'button' })).toBeNull()
    expect(screen.queryByText('Current executor', { selector: 'dt' })).toBeNull()
    expect(screen.queryByText('Current attempt', { selector: 'dt' })).toBeNull()
    expect(screen.queryByText('Run activity', { selector: 'dt' })).toBeNull()
    expect(screen.queryByText('Checkpoint position', { selector: 'dt' })).toBeNull()
    expect(screen.getByText('Last executor', { selector: 'dt' })).toBeDefined()
  })
})
