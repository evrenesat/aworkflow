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
import { CheckpointHistory } from './CheckpointHistory'

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
  it('shows the worker, rejection, repair and upgrade evidence without inventing delivery', () => {
    render(<CheckpointHistory projectId="project-current" run={run} progress={summary()} detail={detail()} />)

    expect(screen.getByText('1 / 3 approved')).toBeDefined()
    expect(screen.getAllByText(/CP2 of 3 · Reviewing/).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/codex.reviewer/).length).toBeGreaterThan(0)
    expect(screen.getByText('Current attempt')).toBeDefined()

    fireEvent.click(screen.getByRole('button', { name: /Checkpoint 2: Repair/ }))
    expect(screen.getByText('Review rejection')).toBeDefined()
    expect(screen.getByText('Repair attempt')).toBeDefined()
    expect(screen.getByRole('link', { name: 'Retry of recorded event' }).getAttribute('href')).toBe('#checkpoint-event-repair-1')
    expect(screen.getByText('Unassigned or omitted history')).toBeDefined()
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
    expect(screen.getByText(/predecessor\.reviewer/)).toBeDefined()
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

  it('deduplicates repeated event identities and keeps unknown fields explicit', () => {
    const duplicate = event({ event_id: 'duplicate-1', kind: 'review', outcome: null, executor: null, duration_seconds: null, reason: null })
    const repeated = detail({ events: [duplicate, duplicate], availability: 'unavailable', reason_codes: ['missing_plan'], checkpoints: [] })
    render(<CheckpointHistory projectId="project-current" run={run} progress={repeated} detail={repeated} />)
    expect(screen.getAllByText('Evidence unavailable').length).toBeGreaterThan(0)
    fireEvent.click(screen.getByRole('button', { name: /Unassigned or omitted history/ }))
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
})
