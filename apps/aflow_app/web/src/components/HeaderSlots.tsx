import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'

/** The four page-owned regions available in the shared application header. */
export interface HeaderSlotContribution {
  context?: ReactNode
  /** Optional compact-only context when the body already has the detail heading. */
  compactContext?: ReactNode
  local?: ReactNode
  primary?: ReactNode
  more?: ReactNode
}

interface RegisteredContribution {
  id: string
  token: symbol
  slots: HeaderSlotContribution
}

interface HeaderSlotRegistration {
  register: (id: string, slots: HeaderSlotContribution, token: symbol) => void
  unregister: (id: string, token: symbol) => void
}

const registrationContext = createContext<HeaderSlotRegistration | null>(null)
const slotsContext = createContext<HeaderSlotContribution | null>(null)

/**
 * Provides a single active page contribution to the application shell.
 * Registration is tokenized so a hidden dashboard cannot clear a newer page's
 * actions during effect cleanup.
 */
export function HeaderSlotsProvider({ children, renderHeader }: {
  children: ReactNode
  renderHeader: (slots: HeaderSlotContribution) => ReactNode
}) {
  const [registered, setRegistered] = useState<RegisteredContribution | null>(null)
  const register = useCallback((id: string, slots: HeaderSlotContribution, token: symbol) => {
    setRegistered(current => current?.id === id && current.token === token && current.slots === slots
      ? current
      : { id, token, slots })
  }, [])
  const unregister = useCallback((id: string, token: symbol) => {
    setRegistered(current => current?.id === id && current.token === token ? null : current)
  }, [])
  const registration = useMemo(() => ({ register, unregister }), [register, unregister])
  const slots = registered?.slots ?? {}

  return <registrationContext.Provider value={registration}>
    <slotsContext.Provider value={registered?.slots ?? null}>
      {renderHeader(slots)}
      {children}
    </slotsContext.Provider>
  </registrationContext.Provider>
}

/**
 * Registers page-owned header nodes. The hook is a no-op outside the hosted
 * shell, allowing focused component tests to retain their local fallback UI.
 */
export function useHeaderSlots(id: string, slots: HeaderSlotContribution, enabled = true): boolean {
  const registration = useContext(registrationContext)
  useEffect(() => {
    if (!registration || !enabled) return
    const token = Symbol(id)
    registration.register(id, slots, token)
    return () => registration.unregister(id, token)
  }, [enabled, id, registration, slots])
  return registration !== null
}

/** Indicates whether the caller is rendered inside the application shell. */
export function useHeaderShell(): boolean {
  return useContext(registrationContext) !== null
}

/** The currently rendered contribution, useful only to shell-owned outlets. */
export function useHeaderSlotContribution(): HeaderSlotContribution {
  return useContext(slotsContext) ?? {}
}
