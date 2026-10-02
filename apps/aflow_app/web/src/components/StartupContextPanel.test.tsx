import { fireEvent, render, screen, within } from '@testing-library/react'
import { expect, it, vi } from 'vitest'
import type { StartupContext } from '../types'
import { StartupContextPanel } from './StartupContextPanel'

export function startupContext(overrides: Partial<StartupContext> = {}): StartupContext {
  return {
    schema_version: 1, availability: 'available', reason_codes: [], reason: null,
    observed_at: '2026-09-27T00:00:00Z', plan_path: 'plans/in-progress/demo.md',
    plan_identity: 'plan-1', plan_revision: 'revision-1', total_checkpoints: 6,
    recorded_complete_checkpoints: 3,
    next_checkpoint: { ordinal: 4, title: 'Checkpoint 4: Build feature', heading_checked: false, checked_tasks: 0, total_tasks: 2 },
    checkpoints: [
      { ordinal: 3, title: 'Checkpoint 3: Approved', heading_checked: true, checked_tasks: 2, total_tasks: 2 },
      { ordinal: 4, title: 'Checkpoint 4: Build feature', heading_checked: false, checked_tasks: 0, total_tasks: 2 },
    ],
    pending_tasks: ['Implement the feature', 'Verify the result'],
    checkpoint_outline_truncated: false, pending_tasks_truncated: false, text_truncated: false,
    workflow_name: 'managed', selected_step: 'implement_plan', step_source: 'workflow_default',
    recommendation: 'start', recommendation_reason: null, related_runs: [],
    related_runs_complete: true, related_runs_truncated: false,
    ...overrides,
  }
}

it('shows recorded plan facts without claiming execution and retains the outline on refresh', () => {
  const onOpenPlan = vi.fn()
  const first = startupContext()
  const view = render(<StartupContextPanel context={first} mode="pending" planAvailable onOpenPlan={onOpenPlan} />)
  const panel = screen.getByRole('region', { name: 'Startup context pending' })
  expect(within(panel).getByText('Next in plan: Checkpoint 4 of 6 — Build feature')).toBeDefined()
  expect(within(panel).getByText('3 checkpoints marked complete in the plan.')).toBeDefined()
  expect(within(panel).getByText('Implement the feature')).toBeDefined()
  expect(panel.textContent).not.toContain('approved by review')
  const outline = within(panel).getByText('Checkpoints (2)').closest('details')!
  outline.setAttribute('open', '')
  fireEvent(outline, new Event('toggle'))
  expect(within(panel).getByText(/Checkpoint 3: Approved/)).toBeDefined()
  view.rerender(<StartupContextPanel context={startupContext({ observed_at: '2026-09-27T00:01:00Z' })} mode="pending" planAvailable onOpenPlan={onOpenPlan} />)
  expect(within(panel).getByText('Checkpoints (2)').closest('details')?.hasAttribute('open')).toBe(true)
  fireEvent.click(within(panel).getByRole('button', { name: 'Open plan' }))
  expect(onOpenPlan).toHaveBeenCalledWith('plans/in-progress/demo.md')
  within(panel).getByRole('button', { name: 'Open plan' }).focus()
  expect(document.activeElement).toBe(within(panel).getByRole('button', { name: 'Open plan' }))
})

it('leads with preserved work and navigates to the exact prior run without writing', () => {
  const open = vi.fn()
  const context = startupContext({
    recommendation: 'review_previous_run', recommendation_reason: 'Earlier implementation work is preserved.',
    related_runs: [{ run_id: 'prior-17', status: 'failed', step: 'implement_plan', activity: 'inactive', history_state: 'visible', failure_reason: 'Provider stopped', worktree_path: '/worktree/prior-17', worktree_verified: true, branch: 'feature/prior-17', branch_verified: true, unmerged_work: true, uncommitted_work: true, can_resume: true }],
  })
  render(<StartupContextPanel context={context} mode="preparation" onOpenRun={open} />)
  expect(screen.getByText('Previous work needs recovery')).toBeDefined()
  expect(screen.getByText('Uncommitted implementation work is preserved')).toBeDefined()
  expect(screen.getByText('Previously reported: Provider stopped')).toBeDefined()
  fireEvent.click(screen.getByRole('button', { name: 'Review previous run…' }))
  expect(open).toHaveBeenCalledWith('prior-17')
})

it('explains partial and missing evidence without offering an automatic continuation', () => {
  const inspect = startupContext({ availability: 'partial', recommendation: 'inspect_previous_runs', recommendation_reason: 'Run history coverage is incomplete.', related_runs_complete: false })
  const view = render(<StartupContextPanel context={inspect} mode="pending" />)
  expect(screen.getByText('Inspect earlier runs before starting')).toBeDefined()
  expect(screen.getByText(/summary is partial/)).toBeDefined()
  view.rerender(<StartupContextPanel context={startupContext({ availability: 'unavailable', reason: 'Plan missing', next_checkpoint: null })} mode="pending" />)
  expect(screen.getByText(/Plan missing/)).toBeDefined()
  expect(screen.queryByText(/Next in plan/)).toBeNull()
  view.rerender(<StartupContextPanel context={null} mode="pending" />)
  expect(screen.getByText(/startup context is unavailable from this server/)).toBeDefined()
})

it('keeps the plan facts and shows the explicit consistency warning for an inconsistent plan', () => {
  const context = startupContext({
    availability: 'partial',
    reason_codes: ['inconsistent_checkpoint_state'],
    reason: 'A completed checkpoint has unchecked tasks. Correct the plan before starting.',
    recommendation: 'start',
    recommendation_reason: 'No unresolved earlier work was found in the complete scan.',
    next_checkpoint: { ordinal: 2, title: 'Checkpoint 2: Broken', heading_checked: true, checked_tasks: 0, total_tasks: 1 },
    pending_tasks: ['leftover task'],
  })
  const view = render(<StartupContextPanel context={context} mode="review" planAvailable onOpenPlan={() => {}} />)
  const panel = screen.getByRole('region', { name: 'Startup context review' })
  const alert = within(panel).getByRole('alert')
  expect(alert.textContent).toContain('A completed checkpoint has unchecked tasks. Correct the plan before starting.')
  expect(alert.textContent).toContain('Explicit recovery confirmation is required before starting.')
  // Recorded plan facts stay visible and truthful alongside the warning.
  expect(within(panel).getByText('Next in plan: Checkpoint 2 of 6 — Broken')).toBeDefined()
  expect(within(panel).getByText('leftover task')).toBeDefined()
  view.rerender(<StartupContextPanel context={startupContext({ availability: 'available', reason_codes: [], reason: null, next_checkpoint: { ordinal: 4, title: 'Checkpoint 4: Build feature', heading_checked: false, checked_tasks: 0, total_tasks: 2 } })} mode="review" planAvailable onOpenPlan={() => {}} />)
  expect(within(panel).queryByRole('alert')).toBeNull()
})

it('opens an active exact run and never presents it as recoverable', () => {
  const open = vi.fn()
  render(<StartupContextPanel context={startupContext({ recommendation: 'open_existing_run', related_runs: [{ run_id: 'running-9', status: 'running', step: 'implement', activity: 'active', history_state: 'visible', failure_reason: null, worktree_path: null, worktree_verified: false, branch: null, branch_verified: false, unmerged_work: null, uncommitted_work: null, can_resume: false }] })} mode="preparation" onOpenRun={open} />)
  expect(screen.getByText('This plan is already running')).toBeDefined()
  expect(screen.queryByRole('button', { name: /resume/i })).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Open running run' }))
  expect(open).toHaveBeenCalledWith('running-9')
})
