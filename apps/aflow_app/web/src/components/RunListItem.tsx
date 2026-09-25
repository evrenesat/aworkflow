import { useEffect, useId, useRef, useState } from 'react'
import type { CSSProperties, FocusEvent, PointerEvent } from 'react'
import type { RunStatus } from '../types'
import {
  isTerminalInactiveRun,
  runActivityText,
  runDurationText,
  runPlanPresentationForRun,
  runDisplayProjection,
} from '../runPresentation'
import { RunProgress, compactRunProgressText, runProgressAccessibleText, type RunProgressLoadState } from './RunProgress'

const PREVIEW_OPEN_DELAY_MS = 300
const PREVIEW_CLOSE_DELAY_MS = 140
const PREVIEW_GAP_PX = 8
const PREVIEW_MARGIN_PX = 12
const PREVIEW_MAX_WIDTH_PX = 320

export interface RunListItemProps {
  run: RunStatus
  stableKey: string
  onSelect: () => void
  selected?: boolean
  projectLabel?: string
  dataSidebarEditorItem?: string
  rowClassName?: string
  dataEnrichmentState?: string
  loadState?: RunProgressLoadState
  loadMessage?: string | null
}

function previewKey(value: string): string {
  return value.replace(/[^a-zA-Z0-9_-]/g, '-')
}

function viewportSize(): { width: number; height: number } {
  const width = Math.max(document.documentElement.clientWidth, window.innerWidth || 0)
  const height = Math.max(document.documentElement.clientHeight, window.innerHeight || 0)
  return { width, height }
}

function knownFact(value: string): string | null {
  return /(?:not reported|unknown|unavailable)$/i.test(value.trim()) ? null : value
}

/**
 * Shared project/global run row. The selection and preview controls are
 * siblings so opening a preview can never select a run or create nested
 * interactive controls.
 */
export function RunListItem({
  run,
  stableKey,
  onSelect,
  selected = false,
  projectLabel,
  dataSidebarEditorItem,
  rowClassName = '',
  dataEnrichmentState,
  loadState = 'ready',
  loadMessage = null,
}: RunListItemProps): JSX.Element {
  const [previewOpen, setPreviewOpen] = useState(false)
  const [previewStyle, setPreviewStyle] = useState<CSSProperties>({})
  const rowRef = useRef<HTMLDivElement>(null)
  const previewRef = useRef<HTMLDivElement>(null)
  const selectionButtonRef = useRef<HTMLButtonElement>(null)
  const previewButtonRef = useRef<HTMLButtonElement>(null)
  const openTimerRef = useRef<number | null>(null)
  const closeTimerRef = useRef<number | null>(null)
  const previewTriggerRef = useRef<'focus' | 'pointer' | 'toggle' | null>(null)
  const previewOpenerRef = useRef<HTMLElement | null>(null)
  const restoringSelectionFocusRef = useRef<HTMLButtonElement | null>(null)
  const previewId = `run-preview-${previewKey(useId())}`
  const plan = runPlanPresentationForRun(run)
  const statusPresentation = runDisplayProjection(run)
  const status = statusPresentation.label
  const progressLabel = run.progress ? runProgressAccessibleText(run.progress, run) : null
  const progressLoadLabel = loadState === 'loading'
    ? 'Loading checkpoint progress…'
    : loadState === 'stale'
      ? loadMessage || 'Checkpoint progress stale — Refresh to update.'
      : loadState === 'failed'
        ? loadMessage || 'Checkpoint progress unavailable — Refresh to retry.'
        : null
  const duration = knownFact(runDurationText(run))
  const activity = knownFact(runActivityText(run))
  const compactProgressFact = run.progress ? compactRunProgressText(run.progress, run) : null
  const rowActivityFact = compactProgressFact || isTerminalInactiveRun(run) ? null : activity
  const accessibleFacts = [plan.label, plan.date, progressLabel, progressLoadLabel, duration, activity].filter((value): value is string => Boolean(value))
  const accessibleName = projectLabel
    ? [projectLabel, status, ...accessibleFacts, run.run_id].join(' · ')
    : [`${run.run_id} ${status}`, ...accessibleFacts].join(' · ')

  function clearTimers(): void {
    if (openTimerRef.current !== null) window.clearTimeout(openTimerRef.current)
    if (closeTimerRef.current !== null) window.clearTimeout(closeTimerRef.current)
    openTimerRef.current = null
    closeTimerRef.current = null
  }

  function selectionOpenerFor(target: EventTarget | null): HTMLButtonElement | null {
    if (!(target instanceof HTMLElement)) return null
    return target.closest('.run-list-select') === selectionButtonRef.current ? selectionButtonRef.current : null
  }

  function updatePreviewPosition(): void {
    const row = rowRef.current
    if (!row) return
    const rowBox = row.getBoundingClientRect()
    const panelHeight = previewRef.current?.getBoundingClientRect().height ?? 220
    const viewport = viewportSize()
    const width = Math.min(PREVIEW_MAX_WIDTH_PX, Math.max(0, viewport.width - PREVIEW_MARGIN_PX * 2))
    const mobile = viewport.width < 680
    let left = rowBox.right + PREVIEW_GAP_PX
    let top = rowBox.top
    if (mobile) {
      left = PREVIEW_MARGIN_PX
      top = rowBox.bottom + PREVIEW_GAP_PX
      if (top + panelHeight > viewport.height - PREVIEW_MARGIN_PX) {
        top = Math.max(PREVIEW_MARGIN_PX, rowBox.top - panelHeight - PREVIEW_GAP_PX)
      }
    } else if (left + width > viewport.width - PREVIEW_MARGIN_PX) {
      left = Math.max(PREVIEW_MARGIN_PX, rowBox.left - width - PREVIEW_GAP_PX)
    }
    if (top + panelHeight > viewport.height - PREVIEW_MARGIN_PX) {
      top = Math.max(PREVIEW_MARGIN_PX, viewport.height - panelHeight - PREVIEW_MARGIN_PX)
    }
    setPreviewStyle({
      left: `${Math.round(left)}px`,
      top: `${Math.round(top)}px`,
      width: `${Math.floor(width)}px`,
      maxHeight: `${Math.max(120, Math.floor(viewport.height - PREVIEW_MARGIN_PX * 2))}px`,
    })
  }

  function openPreview(trigger: 'focus' | 'pointer' | 'toggle', opener: HTMLElement | null): void {
    clearTimers()
    previewTriggerRef.current = trigger
    previewOpenerRef.current = opener
    setPreviewOpen(true)
  }

  function closePreview(restoreFocus = false): void {
    const trigger = previewTriggerRef.current
    const opener = previewOpenerRef.current
    const activeElement = document.activeElement instanceof HTMLElement ? document.activeElement : null
    clearTimers()
    previewTriggerRef.current = null
    previewOpenerRef.current = null
    setPreviewOpen(false)
    if (restoreFocus) {
      const focusTarget = trigger === 'toggle' ? previewButtonRef.current : opener ?? activeElement
      if (focusTarget?.isConnected) {
        const guardSelectionFocus = focusTarget === selectionButtonRef.current && document.activeElement !== focusTarget
        restoringSelectionFocusRef.current = guardSelectionFocus ? selectionButtonRef.current : null
        focusTarget.focus()
        if (document.activeElement !== focusTarget) restoringSelectionFocusRef.current = null
      }
    }
  }

  function scheduleOpen(trigger: 'focus' | 'pointer', opener: HTMLElement | null): void {
    clearTimers()
    openTimerRef.current = window.setTimeout(() => {
      openTimerRef.current = null
      openPreview(trigger, opener)
    }, PREVIEW_OPEN_DELAY_MS)
  }

  function scheduleClose(): void {
    if (previewTriggerRef.current === 'toggle') return
    if (openTimerRef.current !== null) window.clearTimeout(openTimerRef.current)
    openTimerRef.current = null
    if (closeTimerRef.current !== null) window.clearTimeout(closeTimerRef.current)
    closeTimerRef.current = window.setTimeout(() => {
      closeTimerRef.current = null
      closePreview()
    }, PREVIEW_CLOSE_DELAY_MS)
  }

  useEffect(() => {
    if (!previewOpen) return
    updatePreviewPosition()
    const update = () => updatePreviewPosition()
    window.addEventListener('resize', update)
    window.addEventListener('scroll', update, true)
    return () => {
      window.removeEventListener('resize', update)
      window.removeEventListener('scroll', update, true)
    }
  }, [previewOpen])

  useEffect(() => {
    if (!previewOpen) return
    const handleEscape = (event: KeyboardEvent): void => {
      if (event.key !== 'Escape') return
      event.preventDefault()
      closePreview(true)
    }
    document.addEventListener('keydown', handleEscape)
    return () => document.removeEventListener('keydown', handleEscape)
  }, [previewOpen])

  useEffect(() => () => clearTimers(), [])

  function handleFocus(event: FocusEvent<HTMLDivElement>): void {
    const opener = selectionOpenerFor(event.target)
    if (opener && restoringSelectionFocusRef.current === opener) {
      restoringSelectionFocusRef.current = null
      return
    }
    if (opener) scheduleOpen('focus', opener)
  }

  function handleBlur(event: FocusEvent<HTMLDivElement>): void {
    if (previewTriggerRef.current === 'toggle') return
    const nextTarget = event.relatedTarget
    if (!(nextTarget instanceof Node) || !rowRef.current?.contains(nextTarget)) scheduleClose()
  }

  function handlePointerEnter(event: PointerEvent<HTMLDivElement>): void {
    if (event.pointerType === 'touch') return
    // Pointer movement after keyboard focus must not replace the focus opener.
    if (previewTriggerRef.current === 'focus' || document.activeElement === selectionButtonRef.current) return
    scheduleOpen('pointer', null)
  }

  function handlePointerLeave(event: PointerEvent<HTMLDivElement>): void {
    if (event.pointerType === 'touch') return
    if (previewTriggerRef.current === 'focus') return
    scheduleClose()
  }

  function handlePreviewPointerDown(): void {
    clearTimers()
  }

  function handlePreviewToggle(): void {
    clearTimers()
    if (previewOpen && previewTriggerRef.current === 'toggle') {
      previewTriggerRef.current = null
      previewOpenerRef.current = null
      setPreviewOpen(false)
      return
    }
    openPreview('toggle', previewButtonRef.current)
  }

  function handleSelect(): void {
    // A hover/touch preview belongs to the list surface. Once the exact run
    // is selected, close it before the detail surface can receive focus or
    // hit-testing; a stale fixed preview must never cover detail actions.
    closePreview()
    onSelect()
  }

  return <div
    ref={rowRef}
    className={`run-list-item ${rowClassName} ${selected ? 'selected' : ''}`.trim()}
    data-run-key={stableKey}
    data-run-row="true"
    data-enrichment-state={dataEnrichmentState}
    onPointerEnter={handlePointerEnter}
    onPointerLeave={handlePointerLeave}
    onFocusCapture={handleFocus}
    onBlurCapture={handleBlur}
  >
    <button
      ref={selectionButtonRef}
      type="button"
      data-sidebar-editor-item={dataSidebarEditorItem}
      data-enrichment-state={dataEnrichmentState}
      className="content-button run-list-select"
      aria-current={selected ? 'true' : undefined}
      aria-label={accessibleName}
      onClick={handleSelect}
    >
      <span className="run-list-context">
        <span className="run-list-title-line">
          <strong className="run-list-title" title={plan.label}>{plan.label}</strong>
        </span>
        <span className="run-row-meta text-xs text-dim">
          <span className={`status-pill status-${statusPresentation.category}`} data-status-category={statusPresentation.category}>{status}</span>
          {run.history_state === 'archived' && <span className="status-pill">Archived</span>}
          {projectLabel && <span className="global-run-row-project" title={projectLabel}>{projectLabel}</span>}
          <RunProgress run={run} mode="row" loadState={loadState} loadMessage={loadMessage} />
          {rowActivityFact && <span className="run-row-meta-fact" title={rowActivityFact}>{rowActivityFact}</span>}
        </span>
      </span>
    </button>
    <button
      ref={previewButtonRef}
      type="button"
      className="run-row-preview-toggle"
      aria-label={`Preview ${plan.label}`}
      aria-controls={previewId}
      aria-expanded={previewOpen}
      onPointerDown={handlePreviewPointerDown}
      onClick={handlePreviewToggle}
    >
      <span aria-hidden="true">⋯</span>
    </button>
    {previewOpen && <div
      ref={previewRef}
      id={previewId}
      className="run-row-preview"
      role="dialog"
      aria-label={`Preview ${plan.label}`}
      style={previewStyle}
      onPointerEnter={() => { if (closeTimerRef.current !== null) { window.clearTimeout(closeTimerRef.current); closeTimerRef.current = null } }}
      onPointerLeave={scheduleClose}
    >
      <strong className="run-row-preview-title">{plan.label}</strong>
      {plan.date && <p className="run-row-preview-facts">{plan.date}</p>}
      {projectLabel && <p className="text-xs text-dim">{projectLabel}</p>}
      <p className={`run-row-preview-facts status-${statusPresentation.category}`} data-status-category={statusPresentation.category}>{status}{run.history_state === 'archived' ? ' · Archived' : ''}</p>
      {(duration || activity) && <p className="run-row-preview-facts">{[duration, activity].filter((value): value is string => Boolean(value)).join(' · ')}</p>}
      <RunProgress run={run} mode="preview" loadState={loadState} loadMessage={loadMessage} />
      <p className="run-row-preview-id mono">{run.run_id}</p>
    </div>}
  </div>
}
