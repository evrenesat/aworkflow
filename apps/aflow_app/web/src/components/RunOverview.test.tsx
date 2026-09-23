import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { RunEvent } from '../types'
import { RunOverview } from './RunOverview'

function event(sequence: number, eventType: string, data: Record<string, unknown> = {}): RunEvent {
  return {
    sequence,
    event_type: eventType,
    data,
    schema_version: 1,
    timestamp: `2026-09-12T12:0${sequence}:00Z`,
  }
}

describe('RunOverview', () => {
  it('does not invent an empty result or payload disclosure', () => {
    render(
      <RunOverview
        header={<h3>Demo</h3>}
        currentWork={<p>Implement · turn 2</p>}
        latestResult={null}
        events={[event(1, 'run_started'), event(2, 'heartbeat')]}
      />,
    )

    expect(screen.queryByRole('heading', { name: 'Latest result' })).toBeNull()
    expect(screen.queryByText(/No finished result has been recorded/)).toBeNull()
    expect(screen.queryByText('Read full update')).toBeNull()
    expect(screen.getByText('No meaningful activity has been reported yet.')).toBeDefined()
  })

  it('leads with recorded activity facts and keeps payload detail behind a disclosure', () => {
    render(
      <RunOverview
        header={<h3>Demo</h3>}
        currentWork={<p>Implement · turn 2</p>}
        latestResult={<p>Recorded result: implemented the checkpoint</p>}
        events={[
          event(1, 'status_changed', { status: 'running' }),
          event(2, 'turn_finished', { step_name: 'implement', step_role: 'worker', turn_number: 2, summary: 'implemented the checkpoint' }),
        ]}
      />,
    )

    expect(screen.getByRole('heading', { name: 'Latest result' })).toBeDefined()
    expect(screen.getByText(/Implement · Worker · turn 2 · implemented the checkpoint/)).toBeDefined()
    expect(document.querySelector('.run-overview-event-time')).toBeDefined()
    const eventDetails = screen.getByText('Read full update').closest('details')
    expect(eventDetails).toBeDefined()
    expect(eventDetails?.hasAttribute('open')).toBe(false)
    expect(eventDetails?.querySelector('pre')).toBeNull()
  })
})
