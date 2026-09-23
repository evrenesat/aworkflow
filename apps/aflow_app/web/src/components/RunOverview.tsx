import { useState, type ReactNode } from 'react'
import type { RunEvent } from '../types'
import { meaningfulRunEvents, presentRunEvent } from '../runPresentation'

interface RunOverviewProps {
  header: ReactNode
  notices?: ReactNode
  currentWork: ReactNode
  latestResult?: ReactNode | null
  events: RunEvent[]
  streamNotice?: ReactNode
}

function RunOverviewEvent({ event }: { event: RunEvent }) {
  const [open, setOpen] = useState(false)
  const presentation = presentRunEvent(event)
  const eventSummary = [presentation.facts.join(' · '), presentation.summary]
    .filter((value): value is string => Boolean(value))
    .join(' · ')
  const summary = (
    <>
      {presentation.recordedAt && <time className="run-overview-event-time">{presentation.recordedAt}</time>}
      <span className="run-overview-event-summary">
        <strong>{presentation.label}</strong>
        {eventSummary && <span>{eventSummary}</span>}
      </span>
    </>
  )

  return (
    <li className="run-overview-event">
      {presentation.hasPayload
        ? <details onToggle={(toggleEvent) => setOpen(toggleEvent.currentTarget.open)}>
          <summary>
            {summary}
            <span className="run-overview-event-action">Read full update</span>
          </summary>
          {open && <pre className="dashboard-payload">{presentation.detail}</pre>}
        </details>
        : <div className="run-overview-event-line">{summary}</div>}
    </li>
  )
}

/**
 * The short, recorded-first surface for one run.  Action owners remain in the
 * dashboard; this component only establishes the reading order and keeps
 * event detail behind an explicit, non-mutating disclosure.
 */
export function RunOverview({ header, notices, currentWork, latestResult, events, streamNotice }: RunOverviewProps) {
  const recentEvents = meaningfulRunEvents(events)

  return (
    <div className="run-overview" aria-label="Run overview">
      {header}
      {notices}

      <section className="dashboard-section run-overview-section" data-ui-fidelity-anchor="current-work">
        <div className="section-heading">
          <h4>Current work</h4>
        </div>
        <div className="run-overview-content">{currentWork}</div>
      </section>

      {latestResult != null && <section className="dashboard-section run-overview-section" data-ui-fidelity-anchor="latest-result">
          <div className="section-heading">
            <h4>Latest result</h4>
          </div>
          <div className="run-overview-content">{latestResult}</div>
        </section>}

      <section className="dashboard-section run-overview-section run-overview-activity" data-ui-fidelity-anchor="recent-activity">
        <div className="section-heading" data-ui-fidelity-anchor="mobile-recent-activity">
          <h4>Recent activity</h4>
        </div>
        {streamNotice}
        {recentEvents.length === 0 ? (
          <p className="text-sm text-dim">No meaningful activity has been reported yet.</p>
        ) : (
          <ol className="run-overview-event-list">
            {recentEvents.map((event) => (
              <RunOverviewEvent event={event} key={`${event.sequence}-${event.event_type}`} />
            ))}
          </ol>
        )}
      </section>
    </div>
  )
}
