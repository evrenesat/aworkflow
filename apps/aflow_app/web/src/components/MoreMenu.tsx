import { createContext, useCallback, useContext, useState, type ReactNode } from 'react'

const MenuCloseContext = createContext<() => void>(() => {})

/**
 * Small secondary-action menu. The trigger is a compact button with an
 * accessible name; Escape and focus loss close it, and menu items close it
 * when activated. Primary actions never belong in here.
 */
export function MoreMenu({ label, children }: { label: string; children: ReactNode }) {
  const [open, setOpen] = useState(false)
  const close = useCallback(() => setOpen(false), [])
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
      aria-label={label}
      onClick={() => setOpen(current => !current)}
    >⋯</button>
    {open && (
      <div role="menu" aria-label={label}>
        <MenuCloseContext.Provider value={close}>{children}</MenuCloseContext.Provider>
      </div>
    )}
  </div>
}

/** A single action inside a MoreMenu. */
export function MenuItem({ children, onClick, danger = false }: { children: ReactNode; onClick: () => void; danger?: boolean }) {
  const close = useContext(MenuCloseContext)
  return <button
    type="button"
    role="menuitem"
    className={`btn btn-sm ${danger ? 'btn-danger' : 'btn-secondary'}`}
    onClick={() => { close(); onClick() }}
  >{children}</button>
}
