import { useEffect, useState } from 'react'
import { ApiError } from '../api'
import * as api from '../api'
import type { PlanDocument, ProjectInfo } from '../types'

interface PlanPanelProps {
  project: ProjectInfo
  /** Reports unsaved text so the shell can guard navigation. */
  onDirtyChange: (dirty: boolean) => void
  onOpenRunDashboard: (planPath: string) => void
}

interface ConflictState {
  currentRevision: string
}

function shortRevision(revision: string): string {
  return revision.slice(0, 12)
}

/** Lifecycle sections always render in canonical order with runnable guidance. */
const LIFECYCLE_SECTIONS: Array<{ status: PlanDocument['status']; title: string; hint: string }> = [
  {
    status: 'todo',
    title: 'Draft (todo)',
    hint: 'Editable working notes. A draft is not runnable yet — save it, then move it to Ready.',
  },
  {
    status: 'in_progress',
    title: 'Ready (in progress)',
    hint: 'Runnable plans. Open one and use “Run this plan” to start a run from it.',
  },
  {
    status: 'done',
    title: 'Done',
    hint: 'Finished plans, kept for the record. A done plan is no longer runnable.',
  },
]

/**
 * Plan list/reader/editor against the revisioned plan routes.  Local text is
 * preserved on every network or conflict failure; the server copy is only
 * reloaded after an explicit confirmation.
 */
export function PlanPanel({ project, onDirtyChange, onOpenRunDashboard }: PlanPanelProps) {
  const [plans, setPlans] = useState<PlanDocument[]>([])
  const [selected, setSelected] = useState<PlanDocument | null>(null)
  const [content, setContent] = useState('')
  const [savedContent, setSavedContent] = useState('')
  const [newName, setNewName] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [conflict, setConflict] = useState<ConflictState | null>(null)
  const [confirmReload, setConfirmReload] = useState(false)
  const [confirmClose, setConfirmClose] = useState(false)
  const dirty = selected !== null && content !== savedContent

  useEffect(() => {
    onDirtyChange(dirty)
    return () => onDirtyChange(false)
  }, [dirty, onDirtyChange])

  useEffect(() => {
    if (!dirty) return
    const guard = (event: BeforeUnloadEvent) => {
      event.preventDefault()
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', guard)
    return () => window.removeEventListener('beforeunload', guard)
  }, [dirty])

  useEffect(() => {
    setSelected(null)
    setContent('')
    setSavedContent('')
    setConflict(null)
    setConfirmReload(false)
    setConfirmClose(false)
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
      const loadedContent = loaded.content ?? ''
      setContent(loadedContent)
      setSavedContent(loadedContent)
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
      setSavedContent(content)
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
    if (!selected || selected.status === 'done' || dirty) return
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
      const promotedContent = promoted.content ?? content
      setContent(promotedContent)
      setSavedContent(promotedContent)
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
        setSavedContent('')
        setConflict(null)
        setConfirmReload(false)
        setError(`Plan ${selected.name} is no longer present in the project lifecycle.`)
        await refresh()
        return
      }
      const loaded = await api.readProjectPlan(project.id, match.status, match.name)
      setSelected(loaded)
      const loadedContent = loaded.content ?? ''
      setContent(loadedContent)
      setSavedContent(loadedContent)
      setConflict(null)
      setConfirmReload(false)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to reload the plan')
    } finally {
      setBusy(false)
    }
  }

  function closePlan() {
    setSelected(null)
    setContent('')
    setSavedContent('')
    setConflict(null)
    setConfirmReload(false)
    setConfirmClose(false)
  }

  if (selected) {
    // Only a saved Ready plan can run: a dirty draft or another lifecycle
    // state explains its next step instead of exposing a button that no-ops.
    const runnable = selected.status === 'in_progress' && !dirty
    const lifecycleExplanation = selected.status === 'todo'
      ? 'This draft is not runnable yet. Save it and move it to Ready (in progress) to enable “Run this plan”.'
      : selected.status === 'done'
        ? 'Done plans are kept for the record and cannot run. Create a new plan and move it through Draft → Ready to run this work again.'
        : dirty
          ? 'Save this draft before running the plan.'
          : null

    return (
      <div style={{ display: 'flex', flexDirection: 'column', height: '100%', gap: 'var(--spacing-md)' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--spacing-sm)', flexWrap: 'wrap' }}>
          <button className="btn btn-secondary btn-sm" onClick={() => (dirty ? setConfirmClose(true) : closePlan())}>← All plans</button>
          <strong className="mono text-sm">{selected.path}</strong>
          <span className={`status-pill ${selected.status === 'in_progress' ? 'status-awaiting' : ''}`}>
            {selected.status === 'todo' ? 'Draft — not runnable yet' : selected.status === 'in_progress' ? 'Ready — runnable' : 'Done — not runnable'}
          </span>
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
        {confirmClose && (
          <div className="error-message" role="alertdialog" aria-label="Unsaved plan edits">
            <p>Your unsaved plan edits will be lost. Save the plan or discard the edits to return to all plans.</p>
            <div className="dashboard-actions">
              <button className="btn btn-danger btn-sm" onClick={closePlan}>Discard edits</button>
              <button className="btn btn-secondary btn-sm" onClick={() => setConfirmClose(false)}>Keep editing</button>
            </div>
          </div>
        )}
        <textarea
          className="input mono"
          aria-label="Plan content"
          value={content}
          onChange={(event) => setContent(event.target.value)}
          style={{ flex: 1, minHeight: '360px', resize: 'vertical' }}
        />
        <div style={{ display: 'flex', gap: 'var(--spacing-sm)', flexWrap: 'wrap', alignItems: 'center' }}>
          <button className="btn btn-primary" onClick={() => void savePlan()} disabled={busy}>Save</button>
          {selected.status !== 'done' && (
            <button className="btn btn-secondary" onClick={() => void promotePlan()} disabled={busy || dirty}>
              Move to {selected.status === 'todo' ? 'Ready' : 'Done'}
            </button>
          )}
          {selected.status === 'in_progress' && (
            <button
              className="btn btn-primary"
              onClick={() => onOpenRunDashboard(selected.path)}
              disabled={!runnable}
            >
              Run this plan
            </button>
          )}
        </div>
        {lifecycleExplanation && <div className="text-sm text-dim">{lifecycleExplanation}</div>}
        {dirty && selected.status !== 'in_progress' && (
          <div className="text-sm text-dim">Save this draft before moving it through the lifecycle.</div>
        )}
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
      {LIFECYCLE_SECTIONS.map(({ status, title, hint }) => {
        const matching = plans.filter((plan) => plan.status === status)
        return (
          <section key={status}>
            <h3 style={{ fontSize: '1.05rem', fontWeight: 600, marginBottom: 'var(--spacing-xs)' }}>{title}</h3>
            <p className="text-xs text-dim" style={{ marginTop: 0 }}>{hint}</p>
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
