import { useEffect, useRef, useState } from 'react'
import type { MouseEvent, ReactNode } from 'react'

/** Keep the JavaScript presentation breakpoint in lockstep with styles.css. */
export const SIDEBAR_EDITOR_COMPACT_QUERY = '(max-width: 959px), (max-height: 599px)'

export interface SidebarEditorLayoutProps {
  navigation: ReactNode
  children: ReactNode
  selection: string | null
  navigationVersion?: number
  listLabel?: string
  /** Optional consumer heading that shares the compact Back row. */
  detailHeading?: ReactNode
  /** Hidden retained surfaces must not restore focus or document scroll. */
  active?: boolean
  /** A URL/deep-link entry that should open the detail surface on compact screens. */
  detailEntry?: boolean
}

export function useCompactLayout(): boolean {
  const [compact, setCompact] = useState(() => (
    typeof window !== 'undefined'
      && typeof window.matchMedia === 'function'
      && window.matchMedia(SIDEBAR_EDITOR_COMPACT_QUERY).matches
  ))

  useEffect(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return
    const media = window.matchMedia(SIDEBAR_EDITOR_COMPACT_QUERY)
    const update = () => setCompact(media.matches)
    update()
    if (typeof media.addEventListener === 'function') {
      media.addEventListener('change', update)
      return () => media.removeEventListener('change', update)
    }
    media.addListener(update)
    return () => media.removeListener(update)
  }, [])

  return compact
}

function documentScrollTop(): number {
  return document.scrollingElement?.scrollTop ?? window.scrollY
}

function scrollDocumentTo(top: number): void {
  const root = document.scrollingElement
  if (root) {
    root.scrollTop = top
    return
  }
  try {
    window.scrollTo({ top, left: 0, behavior: 'auto' })
  } catch {
    // happy-dom and older embedded browsers may not implement scrollTo options.
  }
}

function focusWithoutScroll(element: HTMLElement): void {
  try {
    element.focus({ preventScroll: true })
  } catch {
    element.focus()
  }
}

interface ListRestorePoint {
  scrollTop: number
  itemId: string | null
}

/**
 * Shared navigation/detail presentation for settings and run history.
 *
 * Domain owners still own selection, drafts, and requests. This component only
 * decides which already-mounted surface is exposed on compact screens and
 * manages the document/focus transition between those surfaces.
 */
export function SidebarEditorLayout({
  navigation,
  children,
  selection,
  navigationVersion = 0,
  listLabel = 'Items',
  detailHeading,
  active = true,
  detailEntry = false,
}: SidebarEditorLayoutProps) {
  const compact = useCompactLayout()
  const [detailOpen, setDetailOpen] = useState(() => !compact || detailEntry)
  const navigationRef = useRef<HTMLElement>(null)
  const detailRef = useRef<HTMLDivElement>(null)
  const previousCompactRef = useRef(compact)
  const previousNavigationVersionRef = useRef(navigationVersion)
  const previousDetailEntryRef = useRef(detailEntry)
  const previousSelectionRef = useRef(selection)
  const userOpenedRef = useRef(detailEntry)
  const pendingOpenRef = useRef(detailEntry)
  const restorePointRef = useRef<ListRestorePoint | null>(null)

  function requestDetailOpen(itemId: string | null): void {
    if (restorePointRef.current === null) {
      restorePointRef.current = { scrollTop: documentScrollTop(), itemId }
    }
    userOpenedRef.current = true
    pendingOpenRef.current = true
    setDetailOpen(true)
  }

  function handleNavigationClick(event: MouseEvent<HTMLElement>): void {
    if (!compact) return
    const target = event.target
    if (!(target instanceof Element)) return
    const item = target.closest<HTMLElement>('[data-sidebar-editor-item]')
    if (!item || !navigationRef.current?.contains(item)) return
    requestDetailOpen(item.dataset.sidebarEditorItem ?? selection)
  }

  // navigationVersion is the explicit signal for keyboard/programmatic item
  // selection; click capture above records the exact row before React updates.
  useEffect(() => {
    if (navigationVersion === previousNavigationVersionRef.current) return
    previousNavigationVersionRef.current = navigationVersion
    if (!compact) {
      userOpenedRef.current = true
      return
    }
    requestDetailOpen(selection)
  }, [compact, navigationVersion, selection])

  // A requested run can arrive after the dashboard has mounted. Local Settings
  // selection never reaches this prop, so it cannot create URL history.
  useEffect(() => {
    const entryChanged = detailEntry !== previousDetailEntryRef.current
    const selectionChanged = selection !== previousSelectionRef.current
    previousDetailEntryRef.current = detailEntry
    previousSelectionRef.current = selection
    if (detailEntry && (entryChanged || selectionChanged)) {
      userOpenedRef.current = true
      pendingOpenRef.current = true
      setDetailOpen(true)
    }
  }, [detailEntry, selection])

  // Wide mode always exposes both surfaces. On a wide-to-compact transition,
  // only a detail the user actually opened remains open; an untouched default
  // selection starts at the list.
  useEffect(() => {
    if (compact === previousCompactRef.current) return
    previousCompactRef.current = compact
    if (compact) setDetailOpen(userOpenedRef.current)
    else setDetailOpen(true)
  }, [compact])

  // Open transitions focus the detail heading after its mounted content exists.
  // selection is included so a URL/deferred detail load gets one retry without
  // making passive polling a focus or scroll event.
  useEffect(() => {
    if (!active || !pendingOpenRef.current || !detailOpen) return
    const heading = detailRef.current?.querySelector<HTMLElement>('h1, h2, h3, h4, h5, h6, legend')
    if (!heading) return
    heading.tabIndex = -1
    scrollDocumentTo(0)
    focusWithoutScroll(heading)
    pendingOpenRef.current = false
  }, [active, children, detailOpen, selection, navigationVersion])

  // Back restores the document position captured before the row click. A row
  // may disappear after refresh/delete, so the labelled navigation surface is
  // the stable focus target in that case.
  useEffect(() => {
    const restorePoint = restorePointRef.current
    if (!active || !compact || detailOpen || restorePoint === null) return
    let finalFrame: number | null = null
    let finalTimer: number | null = null
    const restore = () => {
      restorePointRef.current = null
      scrollDocumentTo(restorePoint.scrollTop)
      const item = restorePoint.itemId === null
        ? null
        : Array.from(navigationRef.current?.querySelectorAll<HTMLElement>('[data-sidebar-editor-item]') ?? [])
          .find(candidate => candidate.dataset.sidebarEditorItem === restorePoint.itemId) ?? null
      const listHeading = navigationRef.current?.querySelector<HTMLElement>('h1, h2, h3, h4, h5, h6')
      if (!item && listHeading) listHeading.tabIndex = -1
      focusWithoutScroll(item ?? listHeading ?? navigationRef.current!)
      // Some browsers still apply a small focus adjustment after a hidden
      // sibling is removed; the final scroll restores the captured position.
      if (typeof window.requestAnimationFrame === 'function') {
        finalFrame = window.requestAnimationFrame(() => scrollDocumentTo(restorePoint.scrollTop))
      } else {
        finalTimer = window.setTimeout(() => scrollDocumentTo(restorePoint.scrollTop), 0)
      }
    }
    if (typeof window.requestAnimationFrame === 'function') {
      const frame = window.requestAnimationFrame(restore)
      return () => {
        window.cancelAnimationFrame(frame)
        if (finalFrame !== null) window.cancelAnimationFrame(finalFrame)
        if (finalTimer !== null) window.clearTimeout(finalTimer)
      }
    }
    const timer = window.setTimeout(restore, 0)
    return () => {
      window.clearTimeout(timer)
      if (finalTimer !== null) window.clearTimeout(finalTimer)
    }
  }, [active, compact, detailOpen])

  function handleBack(): void {
    if (restorePointRef.current === null) {
      restorePointRef.current = { scrollTop: documentScrollTop(), itemId: selection }
    }
    userOpenedRef.current = false
    setDetailOpen(false)
  }

  const navigationHidden = compact && detailOpen
  const detailHidden = compact && !detailOpen
  return <div className="sidebar-editor-layout" data-sidebar-editor-list={listLabel}>
    <nav
      className="sidebar-editor-navigation"
      aria-label={listLabel}
      hidden={navigationHidden}
      aria-hidden={navigationHidden || undefined}
      tabIndex={-1}
      ref={navigationRef}
      onClickCapture={handleNavigationClick}
    >{navigation}</nav>
    <div
      className="sidebar-editor-detail"
      hidden={detailHidden}
      aria-hidden={detailHidden || undefined}
      ref={detailRef}
    >
      {detailHeading ? <div className="sidebar-editor-detail-heading">
        {compact && detailOpen && <button type="button" className="btn btn-secondary sidebar-editor-back" aria-label={`← Back to ${listLabel}`} onClick={handleBack}>← Back</button>}
        {detailHeading}
      </div> : compact && detailOpen && <button type="button" className="btn btn-secondary sidebar-editor-back" onClick={handleBack}>← Back to {listLabel}</button>}
      {children}
    </div>
  </div>
}
