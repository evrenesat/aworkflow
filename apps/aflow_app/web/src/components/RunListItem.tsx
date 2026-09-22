import { useEffect, useId, useRef, useState } from 'react'
import type { CSSProperties, FocusEvent, PointerEvent } from 'react'
import type { RunStatus } from '../types'
import {
  checkpointApprovalText,
  runActivityText,
  runDurationText,
  runPlanPresentationForRun,
  statusLabel,
} from '../runPresentation'
import { RunProgress, type RunProgressLoadState } from './RunProgress'

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
  const previewButtonRef = useRef<HTMLButtonElement>(null)
  const openTimerRef = useRef<number | null>(null)
  const closeTimerRef = useRef<number | null>(null)
  const previewTriggerRef = useRef<'focus' | 'toggle' | null>(null)
  const previewId = `run-preview-${previewKey(useId())}`
  const plan = runPlanPresentationForRun(run)
  const status = statusLabel(run)
  const progressLabel = loadState === 'ready'
    ? run.progress ? checkpointApprovalText(run.progress) : 'Checkpoint progress unavailable'
    : loadMessage || (loadState === 'loading' ? 'Loading checkpoint progress…' : 'Checkpoint progress unavailable')
  const accessibleName = projectLabel
    ? [projectLabel, status, plan.label, progressLabel, runDurationText(run), runActivityText(run), run.run_id].join(' · ')
    : `${run.run_id} ${status} · ${plan.label} · ${progressLabel} · ${runDurationText(run)} · ${runActivityText(run)}`

  function clearTimers(): void {
    if (openTimerRef.current !== null) window.clearTimeout(openTimerRef.current)
    if (closeTimerRef.current !== null) window.clearTimeout(closeTimerRef.current)
    openTimerRef.current = null
    closeTimerRef.current = null
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

  function openPreview(trigger: 'focus' | 'toggle'): void {
    clearTimers()
    previewTriggerRef.current = trigger
    setPreviewOpen(true)
  }

  function closePreview(): void {
    clearTimers()
    previewTriggerRef.current = null
    setPreviewOpen(false)
  }

  function scheduleOpen(): void {
    clearTimers()
    openTimerRef.current = window.setTimeout(() => {
      openTimerRef.current = null
      openPreview('focus')
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
      closePreview()
    }
    document.addEventListener('keydown', handleEscape)
    return () => document.removeEventListener('keydown', handleEscape)
  }, [previewOpen])

  useEffect(() => () => clearTimers(), [])

  function handleFocus(event: FocusEvent<HTMLDivElement>): void {
    const target = event.target
    if (target instanceof HTMLElement && target.closest('.run-list-select')) {
      openPreview('focus')
    }
  }

  function handleBlur(event: FocusEvent<HTMLDivElement>): void {
    if (previewTriggerRef.current === 'toggle') return
    const nextTarget = event.relatedTarget
    if (!(nextTarget instanceof Node) || !rowRef.current?.contains(nextTarget)) scheduleClose()
  }

  function handlePointerEnter(event: PointerEvent<HTMLDivElement>): void {
    if (event.pointerType === 'touch') return
    scheduleOpen()
  }

  function handlePointerLeave(event: PointerEvent<HTMLDivElement>): void {
    if (event.pointerType === 'touch') return
    scheduleClose()
  }

  function handlePreviewPointerDown(): void {
    clearTimers()
  }

  function handlePreviewToggle(): void {
    clearTimers()
    if (previewOpen && previewTriggerRef.current === 'toggle') {
      previewTriggerRef.current = null
      setPreviewOpen(false)
      return
    }
    previewTriggerRef.current = 'toggle'
    setPreviewOpen(true)
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
      type="button"
      data-sidebar-editor-item={dataSidebarEditorItem}
      data-enrichment-state={dataEnrichmentState}
      className="content-button run-list-select"
      aria-current={selected ? 'true' : undefined}
      aria-label={accessibleName}
      onClick={onSelect}
    >
      <span className="run-list-context">
        <strong className="run-list-title" title={plan.label}>{plan.label}</strong>
        {plan.date && <span className="run-title-date">{plan.date}</span>}
        <span className="status-pill">{status}</span>
        {projectLabel && <span className="global-run-row-project">{projectLabel}</span>}
        {run.history_state === 'archived' && <span className="status-pill">Archived</span>}
      </span>
      <span className="run-row-meta text-xs text-dim">
        {projectLabel && <>
          <span>{runDurationText(run)}</span>
          <span>{runActivityText(run)}</span>
        </>}
        <RunProgress run={run} mode="row" loadState={loadState} loadMessage={loadMessage} />
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
      {projectLabel && <p className="text-xs text-dim">{projectLabel}</p>}
      <p className="run-row-preview-facts">{status}{run.history_state === 'archived' ? ' · Archived' : ''}</p>
      <p className="run-row-preview-facts"><span>{runDurationText(run)}</span> · <span>{runActivityText(run)}</span></p>
      <RunProgress run={run} loadState={loadState} loadMessage={loadMessage} />
      <p className="run-row-preview-id mono">{run.run_id}</p>
    </div>}
  </div>
}
