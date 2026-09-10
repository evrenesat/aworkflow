import { createContext, useCallback, useContext, useId, useState, type ReactNode } from 'react'

const MenuCloseContext = createContext<() => void>(() => {})

/**
 * Small secondary-action menu. The trigger is a compact button with an
 * accessible name; Escape and focus loss close it, and menu items close it
 * when activated. Primary actions never belong in here.
 */
export function MoreMenu({ label, triggerLabel = label, children }: { label: string; triggerLabel?: string; children: ReactNode }) {
  const [open, setOpen] = useState(false)
  const close = useCallback(() => setOpen(false), [])
  const menuId = useId()
  return <div
    className="more-menu"
    onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setOpen(false) }}
    onKeyDown={event => { if (event.key === 'Escape') { setOpen(false); event.currentTarget.querySelector('button')?.focus() } }}
  >
    <button
      type="button"
      className="btn btn-secondary btn-sm more-menu-trigger"
      aria-haspopup="menu"
      aria-expanded={open}
      aria-controls={menuId}
      aria-label={triggerLabel}
      onClick={() => setOpen(current => !current)}
    >⋯</button>
    {open && (
      <div id={menuId} role="menu" aria-label={label}>
        <MenuCloseContext.Provider value={close}>{children}</MenuCloseContext.Provider>
      </div>
    )}
  </div>
}

/** A single action inside a MoreMenu. */
export function MenuItem({ children, onClick, danger = false, disabled = false }: { children: ReactNode; onClick: () => void; danger?: boolean; disabled?: boolean }) {
  const close = useContext(MenuCloseContext)
  return <button
    type="button"
    role="menuitem"
    className={`btn btn-sm ${danger ? 'btn-danger' : 'btn-secondary'}`}
    disabled={disabled}
    onClick={() => { if (disabled) return; close(); onClick() }}
  >{children}</button>
}
