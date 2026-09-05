import { useEffect, useState } from 'react'
import { ApiError } from '../api'
import * as api from '../api'
import type { PlanDocument, ProjectInfo } from '../types'

interface PlanPanelProps {
  project: ProjectInfo
  onOpenRunDashboard: (planPath: string) => void
}

interface ConflictState {
  currentRevision: string
}

function shortRevision(revision: string): string {
  return revision.slice(0, 12)
}

/**
 * Plan list/reader/editor against the revisioned plan routes.  Local text is
 * preserved on every network or conflict failure; the server copy is only
 * reloaded after an explicit confirmation.
 */
export function PlanPanel({ project, onOpenRunDashboard }: PlanPanelProps) {
  const [plans, setPlans] = useState<PlanDocument[]>([])
  const [selected, setSelected] = useState<PlanDocument | null>(null)
  const [content, setContent] = useState('')
  const [newName, setNewName] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [conflict, setConflict] = useState<ConflictState | null>(null)
  const [confirmReload, setConfirmReload] = useState(false)

  useEffect(() => {
    setSelected(null)
    setContent('')
    setConflict(null)
    setConfirmReload(false)
    void refresh()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [project.id])

  async function refresh() {
    try {
      setError(null)
      setPlans(await api.listProjectPlans(project.id))
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load plans')
    }
  }

  async function openPlan(plan: PlanDocument) {
    try {
      setError(null)
      setConflict(null)
      setConfirmReload(false)
      const loaded = await api.readProjectPlan(project.id, plan.status, plan.name)
      setSelected(loaded)
      setContent(loaded.content ?? '')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load plan')
    }
  }

  async function createPlan() {
    const name = newName.trim()
    if (!name) return
    try {
      setBusy(true)
      setError(null)
      const created = await api.createProjectPlan(project.id, { name, content: '# Plan\n\n' })
      setNewName('')
      await refresh()
      await openPlan(created)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create plan')
    } finally {
      setBusy(false)
    }
  }

  async function savePlan() {
    if (!selected) return
    try {
      setBusy(true)
      setError(null)
      const updated = await api.updateProjectPlan(project.id, selected.status, selected.name, {
        content,
        expected_revision: selected.revision,
      })
      setSelected(updated)
      setConflict(null)
      setConfirmReload(false)
      await refresh()
    } catch (err) {
      if (err instanceof ApiError && err.code === 'revision_conflict') {
        const current = err.detail.current_revision
        setConflict({ currentRevision: typeof current === 'string' ? current : 'unknown' })
      } else {
        setError(err instanceof Error ? err.message : 'Failed to save plan')
      }
      // The edited text stays in the editor for every failure.
    } finally {
      setBusy(false)
    }
  }

  async function promotePlan() {
    if (!selected || selected.status === 'done') return
    try {
      setBusy(true)
      setError(null)
      const promoted = await api.promoteProjectPlan(
        project.id,
        selected.status,
        selected.name,
        { expected_revision: selected.revision },
      )
      setSelected(promoted)
      setContent(promoted.content ?? content)
      setConflict(null)
      setConfirmReload(false)
      await refresh()
    } catch (err) {
      if (err instanceof ApiError && err.code === 'revision_conflict') {
        const current = err.detail.current_revision
        setConflict({ currentRevision: typeof current === 'string' ? current : 'unknown' })
      } else {
        setError(err instanceof Error ? err.message : 'Failed to promote plan')
      }
      // The edited text stays in the editor for every failure.
    } finally {
      setBusy(false)
    }
  }

  async function reloadFromServer() {
    if (!selected) return
    try {
      setBusy(true)
      setError(null)
      const listed = await api.listProjectPlans(project.id)
      const match = listed.find((plan) => plan.name === selected.name)
      if (!match) {
        setSelected(null)
        setContent('')
        setConflict(null)
        setConfirmReload(false)
        setError(`Plan ${selected.name} is no longer present in the project lifecycle.`)
        await refresh()
        return
      }
      const loaded = await api.readProjectPlan(project.id, match.status, match.name)
      setSelected(loaded)
      setContent(loaded.content ?? '')
      setConflict(null)
      setConfirmReload(false)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to reload the plan')
    } finally {
      setBusy(false)
    }
  }

  if (selected) {
    return (
      <div style={{ display: 'flex', flexDirection: 'column', height: '100%', gap: 'var(--spacing-md)' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--spacing-sm)', flexWrap: 'wrap' }}>
          <button className="btn btn-secondary btn-sm" onClick={() => setSelected(null)}>← All plans</button>
          <strong className="mono text-sm">{selected.path}</strong>
          <span className="text-xs text-dim mono" title={selected.revision}>
            Revision {shortRevision(selected.revision)}
          </span>
        </div>
        {error && <div className="error-message" role="alert">{error}</div>}
        {conflict && (
          <div className="error-message" role="alert">
            <p>
              The plan changed on the server (current revision{' '}
              <span className="mono">{shortRevision(conflict.currentRevision)}</span>). Your
              edits are still in the editor and were not saved.
            </p>
            {!confirmReload ? (
              <button className="btn btn-secondary btn-sm" onClick={() => setConfirmReload(true)}>
                Reload from server…
              </button>
            ) : (
              <div className="confirmation">
                <span className="text-sm">Discard the local edits and load the server copy?</span>
                <button className="btn btn-danger btn-sm" disabled={busy} onClick={() => void reloadFromServer()}>
                  Discard edits and reload
                </button>
                <button className="btn btn-secondary btn-sm" onClick={() => setConfirmReload(false)}>
                  Keep editing
                </button>
              </div>
            )}
          </div>
        )}
        <textarea
          className="input mono"
          aria-label="Plan content"
          value={content}
          onChange={(event) => setContent(event.target.value)}
          style={{ flex: 1, minHeight: '360px', resize: 'vertical' }}
        />
        <div style={{ display: 'flex', gap: 'var(--spacing-sm)', flexWrap: 'wrap' }}>
          <button className="btn btn-primary" onClick={() => void savePlan()} disabled={busy}>Save</button>
          {selected.status !== 'done' && (
            <button className="btn btn-secondary" onClick={() => void promotePlan()} disabled={busy}>
              Move to {selected.status === 'todo' ? 'in-progress' : 'done'}
            </button>
          )}
          {selected.status === 'in_progress' && (
            <button className="btn btn-primary" onClick={() => onOpenRunDashboard(selected.path)}>
              Open run dashboard
            </button>
          )}
        </div>
      </div>
    )
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--spacing-lg)', height: '100%', overflowY: 'auto' }}>
      {error && <div className="error-message" role="alert">{error}</div>}
      <div className="card" style={{ display: 'flex', gap: 'var(--spacing-sm)', flexWrap: 'wrap' }}>
        <input
          className="input mono"
          aria-label="New plan filename"
          placeholder="new-plan.md"
          value={newName}
          onChange={(event) => setNewName(event.target.value)}
        />
        <button className="btn btn-primary" onClick={() => void createPlan()} disabled={busy || !newName.trim()}>
          Create plan
        </button>
      </div>
      {(['todo', 'in_progress', 'done'] as const).map((status) => {
        const matching = plans.filter((plan) => plan.status === status)
        return (
          <section key={status}>
            <h3 style={{ fontSize: '1.05rem', fontWeight: 600, marginBottom: 'var(--spacing-sm)' }}>
              {status === 'in_progress' ? 'In progress' : status[0].toUpperCase() + status.slice(1)}
            </h3>
            {matching.length === 0 ? (
              <div className="card text-dim text-sm">No plans</div>
            ) : matching.map((plan) => (
              <button key={plan.path} className="card card-interactive content-button" onClick={() => void openPlan(plan)}>
                <div className="content-button-row">
                  <span className="mono text-sm">{plan.name}</span>
                  <span className="text-xs text-dim">{plan.size_bytes} bytes</span>
                </div>
              </button>
            ))}
          </section>
        )
      })}
    </div>
  )
}
