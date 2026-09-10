import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { CHANGELOG_PAGE_SIZE, ChangelogSettings, groupChangelogEntries, type ChangelogEntry } from './ChangelogSettings'

const entries: ChangelogEntry[] = [
  { date: '2026-09-10', title: 'Newest release' },
  { date: '2026-09-10', title: 'Another newest change' },
  { date: '2026-09-09', title: 'Previous release' },
]

describe('groupChangelogEntries', () => {
  it('preserves newest-first order and groups adjacent entries by date', () => {
    expect(groupChangelogEntries(entries)).toEqual([
      { date: '2026-09-10', entries: [entries[0], entries[1]] },
      { date: '2026-09-09', entries: [entries[2]] },
    ])
  })
})

describe('ChangelogSettings', () => {
  it('renders escaped title text and a truthful empty state', () => {
    const hostileTitle = '<img src=x onerror=alert(1)> & <script>old()</script>'
    const { unmount } = render(<ChangelogSettings entries={[{ date: '2026-09-10', title: hostileTitle }]} />)
    expect(screen.getByText(hostileTitle)).toBeTruthy()
    expect(screen.queryByRole('img')).toBeNull()
    expect(document.querySelector('script')).toBeNull()
    unmount()

    render(<ChangelogSettings entries={[]} />)
    expect(screen.getByRole('status').textContent).toBe('No release changes are available in this build.')
    expect(screen.queryByRole('listitem')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Show more' })).toBeNull()
  })

  it('starts at twenty entries and adds another batch through a keyboard-reachable button', () => {
    const manyEntries = Array.from({ length: CHANGELOG_PAGE_SIZE * 2 + 1 }, (_, index) => ({
      date: `2026-09-${String(10 - Math.floor(index / 10)).padStart(2, '0')}`,
      title: `Release ${index}`,
    }))
    render(<ChangelogSettings entries={manyEntries} />)

    expect(screen.getAllByRole('listitem')).toHaveLength(CHANGELOG_PAGE_SIZE)
    expect(screen.queryByText('Release 20')).toBeNull()
    const showMore = screen.getByRole('button', { name: 'Show more' })
    expect(showMore).toHaveProperty('tabIndex', 0)
    showMore.focus()
    expect(document.activeElement).toBe(showMore)
    fireEvent.click(showMore)
    expect(screen.getAllByRole('listitem')).toHaveLength(CHANGELOG_PAGE_SIZE * 2)
    expect(screen.getByText('Release 20')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Show more' })).toBeTruthy()
  })
})
