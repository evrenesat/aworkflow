import { useLayoutEffect, useRef, type ReactNode } from 'react'

/** Selection changes reset only the editor; refreshes preserve both panes. */
export function SidebarEditorLayout({ navigation, children, selection, navigationVersion = 0 }: {
  navigation: ReactNode; children: ReactNode; selection: string | null; navigationVersion?: number
}) {
  const detail = useRef<HTMLDivElement>(null)
  const lastNavigation = useRef(navigationVersion)
  useLayoutEffect(() => {
    const pane = detail.current
    if (!pane) return
    pane.scrollTop = 0
    const heading = pane.querySelector<HTMLElement>('h3, h2, legend')
    if (heading) {
      heading.tabIndex = -1
      if (navigationVersion !== lastNavigation.current) heading.focus({ preventScroll: true })
      if (window.matchMedia?.('(max-width: 767px)').matches) heading.scrollIntoView?.({ block: 'nearest' })
    }
    lastNavigation.current = navigationVersion
  }, [selection, navigationVersion])
  return <div className="sidebar-editor-layout">
    <div className="sidebar-editor-navigation">{navigation}</div>
    <div className="sidebar-editor-detail" ref={detail}>{children}</div>
  </div>
}
