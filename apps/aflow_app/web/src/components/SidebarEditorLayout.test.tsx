import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, describe, expect, it } from 'vitest'
import { SIDEBAR_EDITOR_COMPACT_QUERY, SidebarEditorLayout } from './SidebarEditorLayout'

type MediaController = { setMatches: (matches: boolean) => void }

function installMedia(matches: boolean): MediaController {
  let current = matches
  const listeners = new Set<() => void>()
  const media = {
    get matches() { return current },
    media: SIDEBAR_EDITOR_COMPACT_QUERY,
    addEventListener: (_event: string, listener: () => void) => listeners.add(listener),
    removeEventListener: (_event: string, listener: () => void) => listeners.delete(listener),
    addListener: (listener: () => void) => listeners.add(listener),
    removeListener: (listener: () => void) => listeners.delete(listener),
  } as unknown as MediaQueryList
  Object.defineProperty(window, 'matchMedia', { configurable: true, value: () => media })
  return {
    setMatches(next: boolean) {
      current = next
      listeners.forEach(listener => listener())
    },
  }
}

function DraftEditor({ selection }: { selection: string }) {
  const [draft, setDraft] = useState('clean')
  return <>
    <h3>Editor {selection}</h3>
    <label>Draft <input aria-label="Draft" value={draft} onChange={event => setDraft(event.target.value)} /></label>
  </>
}

function DemoLayout({ detailEntry = false, initialItems = ['one', 'two'] }: { detailEntry?: boolean; initialItems?: string[] }) {
  const [selection, setSelection] = useState(initialItems[0] ?? '')
  const [navigationVersion, setNavigationVersion] = useState(0)
  const [items, setItems] = useState(initialItems)
  function select(item: string) {
    setSelection(item)
    setNavigationVersion(version => version + 1)
  }
  return <>
    <button type="button" onClick={() => setItems(current => current.filter(item => item !== 'one'))}>Remove one</button>
    <SidebarEditorLayout
      selection={selection || null}
      navigationVersion={navigationVersion}
      listLabel="Skills"
      detailEntry={detailEntry}
      navigation={<div>
        <h3>Skills</h3>
        {items.map(item => <button type="button" data-sidebar-editor-item={item} key={item} onClick={() => select(item)}>{item}</button>)}
      </div>}
    >
      <DraftEditor selection={selection} />
    </SidebarEditorLayout>
  </>
}

const originalMatchMedia = window.matchMedia
const originalScrollTo = window.scrollTo

afterEach(() => {
  cleanup()
  if (originalMatchMedia) Object.defineProperty(window, 'matchMedia', { configurable: true, value: originalMatchMedia })
  else delete (window as Window & { matchMedia?: typeof window.matchMedia }).matchMedia
  if (originalScrollTo) Object.defineProperty(window, 'scrollTo', { configurable: true, value: originalScrollTo })
  else delete (window as Window & { scrollTo?: typeof window.scrollTo }).scrollTo
  document.body.innerHTML = ''
})

describe('SidebarEditorLayout compact presentation', () => {
  it('starts with the list on compact first entry and hides the inactive editor', () => {
    installMedia(true)
    const { container } = render(<DemoLayout />)
    const navigation = container.querySelector('.sidebar-editor-navigation') as HTMLElement
    const detail = container.querySelector('.sidebar-editor-detail') as HTMLElement
    expect(navigation.hidden).toBe(false)
    expect(detail.hidden).toBe(true)
    expect(navigation.getAttribute('aria-label')).toBe('Skills')
    expect(screen.getByRole('button', { name: 'one' }).closest('[hidden]')).toBeNull()
    expect(screen.getByLabelText('Draft').closest('[hidden]')).toBe(detail)
  })

  it('opens on selection, supports re-clicking the selected row, and Back preserves selection', async () => {
    installMedia(true)
    const { container } = render(<DemoLayout />)
    const row = screen.getByRole('button', { name: 'one' })
    fireEvent.click(row)
    expect(screen.getByRole('button', { name: /Back to Skills/ })).toBeDefined()
    expect((container.querySelector('.sidebar-editor-navigation') as HTMLElement).hidden).toBe(true)
    expect((container.querySelector('.sidebar-editor-detail') as HTMLElement).hidden).toBe(false)

    fireEvent.click(screen.getByRole('button', { name: /Back to Skills/ }))
    expect((container.querySelector('.sidebar-editor-navigation') as HTMLElement).hidden).toBe(false)
    expect((container.querySelector('.sidebar-editor-detail') as HTMLElement).hidden).toBe(true)
    await waitFor(() => expect(document.activeElement).toBe(row))

    fireEvent.click(row)
    expect(screen.getByRole('heading', { name: 'Editor one' })).toBeDefined()
    expect(screen.getByRole('button', { name: /Back to Skills/ })).toBeDefined()
  })

  it('opens an explicit deep link in detail and focuses its heading once', async () => {
    installMedia(true)
    const scrollTo = () => {}
    Object.defineProperty(window, 'scrollTo', { configurable: true, value: scrollTo })
    const { container } = render(<DemoLayout detailEntry />)
    await waitFor(() => expect(document.activeElement).toBe(container.querySelector('.sidebar-editor-detail h3')))
    expect((container.querySelector('.sidebar-editor-navigation') as HTMLElement).hidden).toBe(true)
    expect((container.querySelector('.sidebar-editor-detail') as HTMLElement).hidden).toBe(false)
    expect((container.querySelector('.sidebar-editor-detail h3') as HTMLElement).tabIndex).toBe(-1)
  })

  it('falls back to the labelled list surface when the opened row is removed', async () => {
    installMedia(true)
    const { container } = render(<DemoLayout />)
    fireEvent.click(screen.getByRole('button', { name: 'one' }))
    fireEvent.click(screen.getByRole('button', { name: 'Remove one' }))
    fireEvent.click(screen.getByRole('button', { name: /Back to Skills/ }))
    await waitFor(() => expect(document.activeElement).toBe(container.querySelector('.sidebar-editor-navigation h3')))
    expect(screen.queryByRole('button', { name: 'one' })).toBeNull()
  })

  it('keeps a dirty mounted editor through Back and an open-detail resize', async () => {
    const media = installMedia(true)
    const { container } = render(<DemoLayout />)
    fireEvent.click(screen.getByRole('button', { name: 'one' }))
    fireEvent.change(screen.getByLabelText('Draft'), { target: { value: 'dirty draft' } })
    act(() => media.setMatches(false))
    await waitFor(() => expect((container.querySelector('.sidebar-editor-detail') as HTMLElement).hidden).toBe(false))
    act(() => media.setMatches(true))
    await waitFor(() => expect((container.querySelector('.sidebar-editor-detail') as HTMLElement).hidden).toBe(false))
    expect((screen.getByLabelText('Draft') as HTMLInputElement).value).toBe('dirty draft')
    fireEvent.click(screen.getByRole('button', { name: /Back to Skills/ }))
    await waitFor(() => expect(document.activeElement).toBe(container.querySelector('[data-sidebar-editor-item="one"]')))
    expect((screen.getByLabelText('Draft').closest('.sidebar-editor-detail') as HTMLElement).hidden).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: 'one' }))
    expect((screen.getByLabelText('Draft') as HTMLInputElement).value).toBe('dirty draft')
  })

  it('uses the list after an untouched wide default is resized compact', async () => {
    const media = installMedia(false)
    const { container } = render(<DemoLayout />)
    expect((container.querySelector('.sidebar-editor-detail') as HTMLElement).hidden).toBe(false)
    act(() => media.setMatches(true))
    await waitFor(() => expect((container.querySelector('.sidebar-editor-navigation') as HTMLElement).hidden).toBe(false))
    expect((container.querySelector('.sidebar-editor-detail') as HTMLElement).hidden).toBe(true)
  })

  it('removes inactive controls from the accessibility surface with hidden semantics', () => {
    installMedia(true)
    const { container } = render(<DemoLayout />)
    fireEvent.click(screen.getByRole('button', { name: 'one' }))
    const navigation = container.querySelector('.sidebar-editor-navigation') as HTMLElement
    expect(navigation.hidden).toBe(true)
    expect(container.querySelector('[data-sidebar-editor-item="one"]')?.closest('[hidden]')).toBe(navigation)
    fireEvent.click(screen.getByRole('button', { name: /Back to Skills/ }))
    const detail = container.querySelector('.sidebar-editor-detail') as HTMLElement
    expect(detail.hidden).toBe(true)
    expect(screen.getByLabelText('Draft').closest('[hidden]')).toBe(detail)
  })
})
