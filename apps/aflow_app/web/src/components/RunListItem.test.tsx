import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { RunStatus } from '../types'
import { RunListItem } from './RunListItem'

const run: RunStatus = {
  run_id: 'run-compact-1', status: 'running', schema_version: 1, ownership: 'control_plane', revision: 4,
  reason: null, unit_name: 'aflow-run', launch_phase: 'unit_started', workflow_name: 'managed', team: 'base-team',
  original_plan_display_name: 'Automatic plan consumption and repair upgrades',
  current_step: 'implement', turns_completed: 4, max_turns: 100, selected_start_step: 'implement',
  skipped_steps: [], restarted_from_run_id: null, started_at: '2026-09-22T10:00:00Z', ended_at: null,
  activity: 'active', evidence: {}, progress: null,
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
    fireEvent.click(previewToggle)
    expect(previewToggle.getAttribute('aria-expanded')).toBe('true')
    expect(screen.getByRole('dialog').textContent).toContain(longRun.original_plan_display_name!)
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(previewToggle.getAttribute('aria-expanded')).toBe('false')
    fireEvent.focus(selection)
    act(() => vi.advanceTimersByTime(300))
    expect(screen.getByRole('dialog')).toBeTruthy()
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
