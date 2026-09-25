import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { RunProgressCount, RunProgressSummary, RunStatus } from '../types'
import { RunListItem } from './RunListItem'

const run: RunStatus = {
  run_id: 'run-compact-1', status: 'running', schema_version: 1, ownership: 'control_plane', revision: 4,
  reason: null, unit_name: 'aflow-run', launch_phase: 'unit_started', workflow_name: 'managed', team: 'base-team',
  original_plan_display_name: 'Automatic plan consumption and repair upgrades',
  current_step: 'implement', turns_completed: 4, max_turns: 100, selected_start_step: 'implement',
  skipped_steps: [], restarted_from_run_id: null, started_at: '2026-09-22T10:00:00Z', ended_at: null,
  activity: 'active', evidence: {}, progress: null,
}

const progressCount = (value: number | null, coverage: RunProgressCount['coverage'] = 'complete'): RunProgressCount => ({ value, coverage })

function previewProgress(approvedCheckpoints: RunProgressCount, totalCheckpoints: RunProgressCount): RunProgressSummary {
  return {
    schema_version: 1, availability: 'complete', observed_at: '2026-09-23T12:00:00Z', evidence_at: '2026-09-23T11:59:00Z',
    reason_codes: [], original_plan_identity: 'plan-compact', original_plan_display_name: run.original_plan_display_name ?? null,
    original_plan_path: '/srv/plans/automatic-plan-consumption.md', total_checkpoints: totalCheckpoints,
    approved_checkpoints: approvedCheckpoints, recorded_complete_checkpoints: progressCount(3),
    current_checkpoint_id: 'cp-5', current_checkpoint_ordinal: 5, current_checkpoint_title: 'Keep row evidence useful',
    activity: 'active', phase: 'implementing', run_status: 'running', current_executor: null, last_executor: null,
    worker_attempts: progressCount(4), repair_passes: progressCount(1), reviews: progressCount(2),
    runtime_retries: progressCount(0), applied_upgrades: progressCount(0),
  }
}

afterEach(() => vi.useRealTimers())

describe('RunListItem', () => {
  it('keeps selection and preview as sibling controls with a stable row key', () => {
    const { container } = render(<RunListItem run={run} stableKey="project:one:run-compact-1" onSelect={vi.fn()} />)
    const row = container.querySelector('[data-run-key="project:one:run-compact-1"]')
    expect(row).toBeTruthy()
    expect(row?.querySelector('.run-list-select')).toBeTruthy()
    expect(row?.querySelector('.run-row-preview-toggle')).toBeTruthy()
    expect(row?.querySelectorAll('button')).toHaveLength(2)
    expect(row?.querySelector('button button')).toBeNull()
    expect(screen.getByRole('button', { name: /run-compact-1/ })).toBeTruthy()
    expect(screen.getByRole('button', { name: /Preview Automatic/ })).toBeTruthy()
  })

  it('keeps a finished date in the accessible name and preview, outside the collapsed row', () => {
    const finished: RunStatus = {
      ...run,
      status: 'completed',
      activity: 'inactive',
      ended_at: '2026-09-25T10:00:00Z',
    }
    const { container } = render(<RunListItem run={finished} stableKey="finished" onSelect={vi.fn()} />)
    const selection = container.querySelector<HTMLButtonElement>('.run-list-select')!
    expect(selection.textContent).not.toContain('2026')
    expect(selection.getAttribute('aria-label')).toContain('2026')
    fireEvent.click(screen.getByRole('button', { name: /Preview Automatic/ }))
    expect(screen.getByRole('dialog').textContent).toContain('2026')
  })

  it('keeps preview activation independent from the exact selection control', () => {
    const onSelect = vi.fn()
    const { container } = render(<RunListItem run={run} stableKey="run-controls" onSelect={onSelect} />)
    const selection = container.querySelector<HTMLButtonElement>('.run-list-select')!
    const previewToggle = screen.getByRole('button', { name: /Preview Automatic/ })

    fireEvent.click(previewToggle)
    expect(previewToggle.getAttribute('aria-expanded')).toBe('true')
    expect(onSelect).not.toHaveBeenCalled()

    fireEvent.click(selection)
    expect(onSelect).toHaveBeenCalledTimes(1)
    expect(previewToggle.getAttribute('aria-expanded')).toBe('false')
  })

  it('opens after a pointer hover delay and closes after leaving without moving siblings', () => {
    vi.useFakeTimers()
    const { container } = render(<>
      <RunListItem run={run} stableKey="run-compact-1" onSelect={vi.fn()} />
      <RunListItem run={{ ...run, run_id: 'run-compact-2' }} stableKey="run-compact-2" onSelect={vi.fn()} />
    </>)
    const rows = container.querySelectorAll<HTMLElement>('[data-run-row]')
    const row = rows[0]
    fireEvent.pointerEnter(row, { pointerType: 'mouse' })
    expect(screen.queryByRole('dialog')).toBeNull()
    act(() => vi.advanceTimersByTime(299))
    expect(screen.queryByRole('dialog')).toBeNull()
    act(() => vi.advanceTimersByTime(1))
    expect(screen.getByRole('dialog')).toBeTruthy()
    const secondRowBefore = rows[1].getBoundingClientRect().top
    fireEvent.pointerLeave(row, { pointerType: 'mouse' })
    act(() => vi.advanceTimersByTime(140))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(rows[1].getBoundingClientRect().top).toBe(secondRowBefore)
  })

  it('supports focus, explicit touch toggle, Escape, and full-title preview content', () => {
    vi.useFakeTimers()
    const longRun = {
      ...run,
      original_plan_display_name: 'Automatic plan consumption and repair upgrades with a very long title',
    }
    const { container } = render(<RunListItem run={longRun} stableKey="run-focus" onSelect={vi.fn()} />)
    const selection = container.querySelector<HTMLButtonElement>('.run-list-select')!
    const previewToggle = screen.getByRole('button', { name: /Preview Automatic/ })
    fireEvent.pointerEnter(container.querySelector<HTMLElement>('[data-run-row]')!, { pointerType: 'touch' })
    expect(screen.queryByRole('dialog')).toBeNull()
    previewToggle.focus()
    fireEvent.click(previewToggle)
    expect(previewToggle.getAttribute('aria-expanded')).toBe('true')
    expect(screen.getByRole('dialog').textContent).toContain(longRun.original_plan_display_name!)
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(previewToggle.getAttribute('aria-expanded')).toBe('false')
    expect(document.activeElement).toBe(previewToggle)
    fireEvent.focus(selection)
    act(() => vi.advanceTimersByTime(300))
    expect(screen.getByRole('dialog')).toBeTruthy()
  })

  it('shows compact approval beside status while keeping full evidence in the accessible name and preview', () => {
    const progressRun: RunStatus = { ...run, progress: previewProgress(progressCount(4), progressCount(11)) }
    const { container } = render(<RunListItem run={progressRun} stableKey="run-compact-evidence" onSelect={vi.fn()} />)
    const selection = container.querySelector<HTMLButtonElement>('.run-list-select')!

    expect(selection.textContent).toContain('Running')
    expect(selection.textContent).toContain('4/11 approved')
    expect(selection.textContent).not.toContain('4 of 11 checkpoints approved')
    expect(selection.getAttribute('aria-label')).toContain('run-compact-1')
    expect(selection.getAttribute('aria-label')).toContain('Running')
    expect(selection.getAttribute('aria-label')).toContain('4 of 11 checkpoints approved')

    fireEvent.click(screen.getByRole('button', { name: /Preview Automatic/ }))
    const preview = screen.getByRole('dialog')
    expect(preview.textContent).toContain('4 of 11 checkpoints approved')
    expect(preview.textContent).toContain('CP5 of 11 · Implementing — Keep row evidence useful')
    expect(preview.textContent).toContain(run.run_id)
  })

  it('presents an unresolved historical outcome with a muted label and no missing-data filler', () => {
    const historicalGap: RunStatus = {
      ...run,
      run_id: '20260911t140026z-d06cc7d7',
      status: 'needs_attention',
      status_reason_code: 'unit_missing',
      reason: 'No current workflow unit was found and no normal outcome was recorded.',
      activity: 'unknown',
      unit_name: 'aflow-run-20260911t140026z-d06cc7d7.service',
      workflow_name: null,
      team: null,
      current_step: null,
      started_at: null,
      ended_at: null,
      evidence: { unit_observation: 'missing', has_run_metadata: false, can_resume: false },
      progress: null,
    }
    const { container } = render(<RunListItem run={historicalGap} stableKey={historicalGap.run_id} onSelect={vi.fn()} />)
    const selection = container.querySelector<HTMLButtonElement>('.run-list-select')!
    const status = selection.querySelector<HTMLElement>('[data-status-category]')

    expect(status?.textContent).toBe('Outcome not recorded')
    expect(status?.dataset.statusCategory).toBe('outcome-unrecorded')
    expect(selection.textContent).not.toContain('Needs attention')
    expect(selection.textContent).not.toMatch(/Duration not reported|Checkpoint progress unavailable|approval unknown|current checkpoint not reported/)
    expect(selection.getAttribute('aria-label')).not.toMatch(/Duration not reported|Activity not reported/)
    fireEvent.click(screen.getByRole('button', { name: /Preview/ }))
    const preview = screen.getByRole('dialog')
    expect(preview.querySelector('[data-status-category]')?.getAttribute('data-status-category')).toBe('outcome-unrecorded')
    expect(preview.textContent).toContain('Outcome not recorded')
  })

  it('keeps a truthful zero checkpoint count in the compact row', () => {
    const zeroProgressRun: RunStatus = {
      ...run,
      progress: previewProgress(progressCount(0), progressCount(11)),
    }
    const { container } = render(<RunListItem run={zeroProgressRun} stableKey="run-zero-progress" onSelect={vi.fn()} />)
    const selection = container.querySelector<HTMLButtonElement>('.run-list-select')!

    expect(selection.textContent).toContain('0/11 approved')
    expect(selection.textContent).not.toMatch(/approval unknown|total unknown|unavailable/)
  })

  it('shows one known fact in a quiet outcome row while its preview retains full evidence', () => {
    const gap: RunStatus = {
      ...run,
      run_id: '20260911t140026z-d06cc7d7',
      status: 'needs_attention',
      status_reason_code: 'unit_missing',
      activity: 'unknown',
      started_at: null,
      progress: null,
      original_plan_display_name: 'historical-work-20260911.md',
      original_plan_path: '/plans/historical-work-20260911.md',
      evidence: { unit_observation: 'missing', has_run_metadata: false, can_resume: false },
    }
    const onSelect = vi.fn()
    const { container } = render(<RunListItem run={gap} stableKey={gap.run_id} quietOutcome contextLabel="Selected · older history" onSelect={onSelect} />)
    const row = container.querySelector<HTMLElement>('.run-list-item')!
    const selection = row.querySelector<HTMLButtonElement>('.run-list-select')!

    expect(row.querySelector('.status-pill')).toBeNull()
    expect(row.querySelector('.run-row-meta')?.textContent).toContain('2026')
    expect(selection.getAttribute('aria-label')).toContain('Selected · older history')
    expect(selection.getAttribute('aria-label')).toContain('Outcome not recorded')
    fireEvent.click(screen.getByRole('button', { name: /Preview Historical work/ }))
    expect(onSelect).not.toHaveBeenCalled()
    const preview = screen.getByRole('dialog')
    expect(preview.textContent).toContain('Outcome not recorded')
    expect(preview.textContent).toContain(gap.run_id)
    expect(preview.textContent).toContain('2026')
  })

  it('restores selection focus after a focus-open preview closes with Escape', () => {
    vi.useFakeTimers()
    const { container } = render(<RunListItem run={run} stableKey="run-selection-focus" onSelect={vi.fn()} />)
    const selection = container.querySelector<HTMLButtonElement>('.run-list-select')!
    const previewToggle = screen.getByRole('button', { name: /Preview Automatic/ })
    selection.focus()
    act(() => vi.advanceTimersByTime(300))
    expect(screen.getByRole('dialog')).toBeTruthy()
    previewToggle.focus()
    fireEvent.pointerEnter(container.querySelector<HTMLElement>('[data-run-row]')!, { pointerType: 'mouse' })
    act(() => vi.advanceTimersByTime(300))

    fireEvent.keyDown(document, { key: 'Escape' })

    expect(screen.queryByRole('dialog')).toBeNull()
    expect(document.activeElement).toBe(selection)
    act(() => vi.advanceTimersByTime(301))
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('keeps an unrelated control focused after a pointer-open preview closes with Escape', () => {
    vi.useFakeTimers()
    const { container } = render(<>
      <input aria-label="Search loaded runs" />
      <RunListItem run={run} stableKey="run-pointer-focus" onSelect={vi.fn()} />
    </>)
    const search = screen.getByRole('textbox', { name: 'Search loaded runs' })
    const selection = container.querySelector<HTMLButtonElement>('.run-list-select')!
    search.focus()
    fireEvent.pointerEnter(selection, { pointerType: 'mouse' })
    act(() => vi.advanceTimersByTime(300))
    expect(screen.getByRole('dialog')).toBeTruthy()

    fireEvent.keyDown(document, { key: 'Escape' })

    expect(screen.queryByRole('dialog')).toBeNull()
    expect(document.activeElement).toBe(search)
  })

  it('includes loading progress in the selection accessibility name without a visible filler notice', () => {
    const { container } = render(<RunListItem run={run} stableKey="run-loading" loadState="loading" onSelect={vi.fn()} />)
    const selection = container.querySelector<HTMLButtonElement>('.run-list-select')!

    expect(screen.getByRole('button', { name: /Loading checkpoint progress…/ })).toBe(selection)
    expect(selection.getAttribute('aria-label')).toContain('Loading checkpoint progress…')
    expect(container.querySelector('.compact-run-progress-row-notice')).toBeNull()
  })

  it('keeps a real failed progress request visible in the collapsed row', () => {
    const { container } = render(<RunListItem
      run={run}
      stableKey="run-progress-failed"
      loadState="failed"
      loadMessage="Checkpoint evidence request failed — Refresh to retry."
      onSelect={vi.fn()}
    />)

    expect(container.querySelector('.compact-run-progress-row-notice')?.textContent).toBe('Checkpoint evidence request failed — Refresh to retry.')
  })

  it.each([
    ['known total', progressCount(null, 'unavailable'), progressCount(11), '11 checkpoints', 'CP5 of 11'],
    ['known approved count', progressCount(4), progressCount(null, 'unavailable'), '4 approved', 'CP5 · Implementing'],
  ] as const)('keeps %s in the anchored preview without unknown-count filler', (
    _caseName,
    approved,
    total,
    aggregateFact,
    positionFact,
  ) => {
    const partialCountsRun: RunStatus = { ...run, progress: previewProgress(approved, total) }
    render(<RunListItem run={partialCountsRun} stableKey={`run-preview-${_caseName}`} onSelect={vi.fn()} />)

    fireEvent.click(screen.getByRole('button', { name: /Preview Automatic/ }))
    const preview = screen.getByRole('dialog')
    expect(preview.textContent).toContain(run.run_id)
    expect(preview.textContent).toContain(aggregateFact)
    expect(preview.textContent).toContain(positionFact)
    expect(preview.textContent).toContain('4 of 100 turns used')
    expect(preview.textContent).not.toContain('approval unknown')
    expect(preview.textContent).not.toContain('total unknown')
  })

  it('keeps partial-total truth in the row and preview without accessible unknown-approval filler', () => {
    const partialTotalRun: RunStatus = {
      ...run,
      progress: previewProgress(progressCount(null, 'unavailable'), progressCount(11, 'partial')),
    }
    const { container } = render(<RunListItem run={partialTotalRun} stableKey="run-preview-partial-total" onSelect={vi.fn()} />)
    const selection = container.querySelector<HTMLButtonElement>('.run-list-select')!

    expect(selection.textContent).toContain('CP5 · Implementing')
    expect(selection.textContent).not.toContain('at least 11')
    expect(selection.getAttribute('aria-label')).toContain('CP5 of at least 11')
    fireEvent.click(screen.getByRole('button', { name: /Preview Automatic/ }))

    const preview = screen.getByRole('dialog')
    const strip = preview.querySelector<HTMLElement>('[role="img"]')!
    expect(preview.textContent).toContain(run.run_id)
    expect(preview.textContent).toContain('at least 11 checkpoints')
    expect(preview.textContent).toContain('CP5 of at least 11')
    expect(strip.getAttribute('aria-label')).not.toContain('aggregate approval count unknown')
    expect(strip.getAttribute('title')).not.toContain('aggregate approval count unknown')
    expect(strip.querySelector('.sr-only')?.textContent).not.toContain('aggregate approval count unknown')
  })

  it('keeps a path-derived date reachable without adding a third compact row line', () => {
    const datedRun = {
      ...run,
      run_id: 'run-dated',
      original_plan_display_name: 'readable-run-history-20260923.md',
      original_plan_path: '/srv/plans/readable-run-history-20260923.md',
    }
    const { container } = render(<RunListItem run={datedRun} stableKey="run-dated" onSelect={vi.fn()} />)
    const selection = container.querySelector<HTMLButtonElement>('.run-list-select')!

    expect(selection.querySelector('.run-list-title')?.textContent).toBe('Readable run history')
    expect(selection.querySelector('.run-title-date')).toBeNull()
    expect(selection.textContent).not.toContain('2026')
    expect(selection.getAttribute('aria-label')).toContain('2026')

    fireEvent.click(screen.getByRole('button', { name: /Preview Readable run history/ }))
    expect(screen.getByRole('dialog').textContent).toContain('2026')
  })

  it('bounds a long project identity and omits expected missing preview facts', () => {
    const projectLabel = 'Suno Live Personas · Worktree: Suno checkpoint review-frequency arm'
    const longRun = {
      ...run,
      original_plan_display_name: 'Automatic plan consumption with a very long repair-policy review title',
      started_at: null,
      workflow_name: null,
      team: null,
      current_step: null,
      progress: null,
    }
    const { container } = render(<RunListItem
      run={longRun}
      stableKey="long-project:run-compact-1"
      projectLabel={projectLabel}
      onSelect={vi.fn()}
    />)

    const row = container.querySelector<HTMLElement>('[data-run-row]')!
    expect(row.querySelector('.global-run-row-project')?.getAttribute('title')).toBe(projectLabel)
    expect(row.querySelector('.run-list-title')?.textContent).toBe(longRun.original_plan_display_name)
    expect(row.textContent).not.toContain('Duration not reported')
    expect(row.textContent).not.toContain('Activity not reported')
    expect(row.textContent).not.toContain('Checkpoint progress unavailable')

    fireEvent.click(screen.getByRole('button', { name: /Preview Automatic/ }))
    const preview = screen.getByRole('dialog')
    expect(preview.textContent).toContain(projectLabel)
    expect(preview.textContent).toContain(longRun.original_plan_display_name)
    expect(preview.textContent).toContain(longRun.run_id)
    expect(preview.textContent).not.toContain('Duration not reported')
    expect(preview.textContent).not.toContain('Activity not reported')
    expect(preview.textContent).not.toContain('Checkpoint progress unavailable')
  })

  it('clamps the preview to the viewport beside the anchored row', () => {
    const { container } = render(<RunListItem run={run} stableKey="run-position" onSelect={vi.fn()} />)
    const row = container.querySelector<HTMLElement>('[data-run-row]')!
    vi.spyOn(row, 'getBoundingClientRect').mockReturnValue({
      x: 1080, y: 80, top: 80, right: 1190, bottom: 140, left: 1080,
      width: 110, height: 60, toJSON: () => ({}),
    })
    fireEvent.click(screen.getByRole('button', { name: /Preview Automatic/ }))
    const preview = screen.getByRole('dialog')
    expect(Number.parseInt(preview.style.left, 10)).toBeGreaterThanOrEqual(12)
    expect(Number.parseInt(preview.style.top, 10)).toBeGreaterThanOrEqual(12)
    expect(Number.parseInt(preview.style.width, 10)).toBeLessThanOrEqual(320)
  })
})
