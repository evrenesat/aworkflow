import { useState } from 'react'
import generatedChangelog from '../generated/changelog.json'

export const CHANGELOG_PAGE_SIZE = 20

export interface ChangelogEntry {
  date: string
  title: string
}

export interface ChangelogGroup {
  date: string
  entries: ChangelogEntry[]
}

export interface ChangelogSettingsProps {
  /** Optional fixture data keeps the empty state and presentation testable. */
  entries?: readonly ChangelogEntry[]
}

const generatedEntries = (generatedChangelog as { entries: ChangelogEntry[] }).entries

/** Groups adjacent entries while preserving the generator's release order. */
export function groupChangelogEntries(entries: readonly ChangelogEntry[]): ChangelogGroup[] {
  const groups: ChangelogGroup[] = []
  for (const entry of entries) {
    const previous = groups[groups.length - 1]
    if (previous?.date === entry.date) {
      previous.entries.push(entry)
    } else {
      groups.push({ date: entry.date, entries: [entry] })
    }
  }
  return groups
}

export function ChangelogSettings({ entries = generatedEntries }: ChangelogSettingsProps) {
  const [visibleCount, setVisibleCount] = useState(CHANGELOG_PAGE_SIZE)
  const visibleEntries = entries.slice(0, visibleCount)
  const groups = groupChangelogEntries(visibleEntries)
  const hasMore = visibleEntries.length < entries.length

  return <section className="changelog-settings" aria-labelledby="changelog-heading">
    <header className="changelog-settings-header">
      <h2 id="changelog-heading">Changelog</h2>
      <p className="changelog-settings-intro">Changes included in this version</p>
    </header>
    {entries.length === 0 ? <p className="changelog-empty" role="status">No release changes are available in this build.</p> : <>
      <div className="changelog-groups" id="changelog-entries">
        {groups.map((group, groupIndex) => <section className="changelog-group" data-changelog-group key={`${group.date}-${groupIndex}`} aria-labelledby={`changelog-date-${groupIndex}`}>
          <h3 id={`changelog-date-${groupIndex}`}><time dateTime={group.date}>{group.date}</time></h3>
          <ul>
            {group.entries.map((entry, entryIndex) => <li data-changelog-title key={`${entry.date}-${entryIndex}`}>{entry.title}</li>)}
          </ul>
        </section>)}
      </div>
      {hasMore && <button type="button" tabIndex={0} className="btn btn-secondary changelog-more" aria-controls="changelog-entries" onClick={() => setVisibleCount(count => Math.min(count + CHANGELOG_PAGE_SIZE, entries.length))}>Show more</button>}
    </>}
  </section>
}
