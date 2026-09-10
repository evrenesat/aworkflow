import type { ReactNode } from 'react'

/**
 * Shared wide layout for a navigation surface and its editor/detail content.
 *
 * Selection and navigationVersion remain part of the presentation contract for
 * the following responsive checkpoint. This checkpoint deliberately leaves
 * focus and scroll ownership to the document instead of creating a detail
 * scroll wrapper or resetting it during selection changes.
 */
export function SidebarEditorLayout({ navigation, children }: {
  navigation: ReactNode; children: ReactNode; selection: string | null; navigationVersion?: number
}) {
  return <div className="sidebar-editor-layout">
    <div className="sidebar-editor-navigation">{navigation}</div>
    <div className="sidebar-editor-detail">{children}</div>
  </div>
}
