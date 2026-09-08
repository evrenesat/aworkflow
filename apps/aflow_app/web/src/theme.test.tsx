import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { AppearanceSelector } from './components/AppearanceSelector'
import { APPEARANCE_KEY, getAppearance, initializeTheme, setAppearance } from './theme'

afterEach(() => vi.restoreAllMocks())

describe('browser appearance', () => {
  it('follows OS changes in System, persists overrides, and synchronizes tabs', () => {
    let dark = false
    const listeners = new Set<() => void>()
    vi.spyOn(window, 'matchMedia').mockImplementation(() => ({
      get matches() { return dark },
      addEventListener: (_: string, listener: () => void) => listeners.add(listener),
      removeEventListener: (_: string, listener: () => void) => listeners.delete(listener),
    } as unknown as MediaQueryList))
    const dispose = initializeTheme()
    const rendered = render(<AppearanceSelector />)
    expect(getAppearance()).toBe('system')
    expect(document.documentElement.dataset.theme).toBe('light')
    act(() => { dark = true; listeners.forEach(listener => listener()) })
    expect(document.documentElement.dataset.theme).toBe('dark')
    fireEvent.change(screen.getByLabelText('Color theme'), { target: { value: 'light' } })
    expect(localStorage.getItem(APPEARANCE_KEY)).toBe('light')
    act(() => { listeners.forEach(listener => listener()) })
    expect(document.documentElement.dataset.theme).toBe('light')
    act(() => window.dispatchEvent(new StorageEvent('storage', { key: APPEARANCE_KEY, newValue: 'dark' })))
    expect((screen.getByLabelText('Color theme') as HTMLSelectElement).value).toBe('dark')
    act(() => window.dispatchEvent(new StorageEvent('storage', { key: APPEARANCE_KEY, newValue: 'invalid' })))
    expect(getAppearance()).toBe('system')
    rendered.unmount()
    dispose()
    expect(listeners.size).toBe(0)
  })

  it('loads explicit preferences and tolerates invalid or blocked storage', () => {
    localStorage.setItem(APPEARANCE_KEY, 'dark')
    let dispose = initializeTheme()
    expect(document.documentElement.dataset.theme).toBe('dark')
    dispose()
    localStorage.setItem(APPEARANCE_KEY, 'purple')
    dispose = initializeTheme()
    expect(getAppearance()).toBe('system')
    dispose()
    vi.spyOn(localStorage, 'getItem').mockImplementation(() => { throw new Error('blocked') })
    vi.spyOn(localStorage, 'setItem').mockImplementation(() => { throw new Error('blocked') })
    dispose = initializeTheme()
    setAppearance('light')
    expect(document.documentElement.dataset.theme).toBe('light')
    dispose()
  })
})
