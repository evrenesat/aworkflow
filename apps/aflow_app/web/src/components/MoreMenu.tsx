import { createContext, useCallback, useContext, useEffect, useId, useRef, useState, type ReactNode } from 'react'

type MenuClose = (restoreFocus?: boolean) => void

const MenuCloseContext = createContext<MenuClose>(() => {})

/**
 * Small secondary-action menu. The trigger is a compact button with an
 * accessible name; Escape and focus loss close it, and menu items close it
 * when activated. Primary actions never belong in here.
 */
export function MoreMenu({
  label,
  triggerLabel = label,
  triggerContent = '⋯',
  className,
  children,
}: {
  label: string
  triggerLabel?: string
  triggerContent?: ReactNode
  className?: string
  children: ReactNode
}) {
  const [open, setOpen] = useState(false)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const restoreFocusRef = useRef(false)
  const close = useCallback((restoreFocus = true) => {
    restoreFocusRef.current = restoreFocus
    setOpen(false)
  }, [])
  useEffect(() => {
    if (open || !restoreFocusRef.current) return
    restoreFocusRef.current = false
    triggerRef.current?.focus()
  }, [open])
  const menuId = useId()
  return <div
    className={`more-menu${className ? ` ${className}` : ''}`}
    onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget as Node | null)) close(false) }}
    onKeyDown={event => { if (event.key === 'Escape') close(true) }}
  >
    <button
      type="button"
      className="btn btn-secondary btn-sm more-menu-trigger"
      ref={triggerRef}
      aria-haspopup="menu"
      aria-expanded={open}
      aria-controls={menuId}
      aria-label={triggerLabel}
      onClick={() => { restoreFocusRef.current = false; setOpen(current => !current) }}
    >{triggerContent}</button>
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
