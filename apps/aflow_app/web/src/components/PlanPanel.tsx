import { useEffect, useRef, useState } from 'react'
import { ApiError } from '../api'
import * as api from '../api'
import type { PlanBackupPage, PlanBackupSummary, PlanDocument, ProjectInfo } from '../types'
import { formatMachineLabel } from '../label'
import { MenuItem, MoreMenu } from './MoreMenu'
import { useHeaderSlots } from './HeaderSlots'
import { TextEditor } from './TextEditor'

interface PlanPanelProps {
  project: ProjectInfo
  /** Reports unsaved text so the shell can guard navigation. */
  onDirtyChange: (dirty: boolean) => void
  onOpenRunDashboard: (planPath: string) => void
  /** Opens the exact draft path handed off by another workspace surface. */
  initialPlanPath?: string | null
  onInitialPlanHandled?: () => void
}

interface ConflictState {
  currentRevision: string
}

interface BackupHistoryState {
  page: PlanBackupPage | null
  loading: boolean
  error: string | null
}

function shortRevision(revision: string): string {
  return revision.slice(0, 12)
}

function planIdentity(projectId: string, plan: PlanDocument): string {
  return JSON.stringify([projectId, plan.status, plan.name, plan.path])
}

function backupOriginLabel(summary: PlanBackupSummary): string {
  if (summary.kind === 'follow_up') return 'Follow-up'
  if (summary.kind === 'snapshot' && summary.baseline_status === 'known') return 'Baseline'
  if (summary.kind === 'snapshot') return 'Snapshot'
  return 'Unknown origin'
}

function backupOriginDescription(summary: PlanBackupSummary, label: string): string {
  if (label === 'Baseline') return 'Initial Ready baseline'
  if (label === 'Snapshot' && summary.baseline_status === 'unknown') return 'Captured plan snapshot'
  if (label === 'Follow-up') return 'Follow-up evidence'
  return 'Provenance is unavailable for this backup'
}

function backupEventLabel(event: string | null): string {
  return typeof event === 'string' && event.trim() ? formatMachineLabel(event) : 'Unavailable'
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
    hint: 'Runnable plans. Startup checks run when you start one. Open one and use “Run this plan” to start a run from it.',
  },
  {
    status: 'done',
    title: 'Done',
    hint: 'Finished plans, kept for the record. A done plan is no longer runnable.',
  },
]

const READY_START_GUIDANCE = 'Ready is a plan lifecycle state; startup checks run when you start the plan.'

/**
 * Plan list/reader/editor against the revisioned plan routes.  Local text is
 * preserved on every network or conflict failure; the server copy is only
 * reloaded after an explicit confirmation.
 */
export function PlanPanel({ project, onDirtyChange, onOpenRunDashboard, initialPlanPath = null, onInitialPlanHandled = () => {} }: PlanPanelProps) {
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
  const [pendingOpenPlan, setPendingOpenPlan] = useState<PlanDocument | null>(null)
  const [historyOpen, setHistoryOpen] = useState(false)
  const [history, setHistory] = useState<BackupHistoryState>({ page: null, loading: false, error: null })
  const planLoadRequest = useRef(0)
  const historyRequest = useRef(0)
  const projectIdRef = useRef(project.id)
  const selectedRef = useRef<PlanDocument | null>(selected)
  const selectedIdentityRef = useRef<string | null>(null)
  const initialPlanHandledRef = useRef<string | null>(null)
  const initialPlanCallbackRef = useRef(onInitialPlanHandled)
  projectIdRef.current = project.id
  selectedRef.current = selected
  selectedIdentityRef.current = selected ? planIdentity(project.id, selected) : null
  initialPlanCallbackRef.current = onInitialPlanHandled
  const dirty = selected !== null && content !== savedContent

  function resetBackupHistory() {
    historyRequest.current += 1
    setHistoryOpen(false)
    setHistory({ page: null, loading: false, error: null })
  }

  function isCurrentPlan(expectedProjectId: string, expectedIdentity: string): boolean {
    return projectIdRef.current === expectedProjectId
      && selectedRef.current?.project_id === expectedProjectId
      && selectedIdentityRef.current === expectedIdentity
  }

  async function loadBackupHistory(plan: PlanDocument, offset = 0) {
    const expectedProjectId = project.id
    const expectedIdentity = planIdentity(expectedProjectId, plan)
    if (!isCurrentPlan(expectedProjectId, expectedIdentity)) return
    const request = ++historyRequest.current
    setHistory((current) => ({ ...current, loading: true, error: null }))
    try {
      const page = await api.listProjectPlanBackups(expectedProjectId, plan.status, plan.name, {
        offset,
        limit: 50,
      })
      if (request !== historyRequest.current || !isCurrentPlan(expectedProjectId, expectedIdentity)) return
      setHistory({ page, loading: false, error: null })
    } catch (err) {
      if (request !== historyRequest.current || !isCurrentPlan(expectedProjectId, expectedIdentity)) return
      setHistory((current) => ({
        ...current,
        loading: false,
        error: err instanceof Error ? err.message : 'Failed to load backup history',
      }))
    }
  }

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
    planLoadRequest.current += 1
    setSelected(null)
    setContent('')
    setSavedContent('')
    setConflict(null)
    setConfirmReload(false)
    setConfirmClose(false)
    setPendingOpenPlan(null)
    initialPlanHandledRef.current = null
    resetBackupHistory()
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
    const expectedProjectId = project.id
    const expectedIdentity = planIdentity(expectedProjectId, plan)
    const request = ++planLoadRequest.current
    resetBackupHistory()
    try {
      setError(null)
      setConflict(null)
      setConfirmReload(false)
      const loaded = await api.readProjectPlan(expectedProjectId, plan.status, plan.name)
      if (request !== planLoadRequest.current || projectIdRef.current !== expectedProjectId) return
      if (loaded.project_id !== expectedProjectId || planIdentity(expectedProjectId, loaded) !== expectedIdentity) return
      setSelected(loaded)
      const loadedContent = loaded.content ?? ''
      setContent(loadedContent)
      setSavedContent(loadedContent)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load plan')
    }
  }

  function openRequestedPlan(plan: PlanDocument) {
    const identity = JSON.stringify([project.id, plan.path])
    if (selected && dirty && planIdentity(project.id, selected) !== planIdentity(project.id, plan)) {
      initialPlanHandledRef.current = identity
      setPendingOpenPlan(plan)
      setConfirmClose(true)
      initialPlanCallbackRef.current()
      return
    }
    initialPlanHandledRef.current = identity
    void openPlan(plan)
    initialPlanCallbackRef.current()
  }

  useEffect(() => {
    if (!initialPlanPath) {
      initialPlanHandledRef.current = null
      return
    }
    const identity = JSON.stringify([project.id, initialPlanPath])
    if (initialPlanHandledRef.current === identity) return
    const match = plans.find((plan) => plan.path === initialPlanPath)
    if (match) openRequestedPlan(match)
    // A successful list refresh will rerun this effect if the exact path is
    // not present yet. Do not clear the request on a transient list failure.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialPlanPath, plans, project.id])

  async function createPlan() {
    const name = newName.trim()
    if (!name) return
    try {
      setBusy(true)
      setError(null)
      const created = await api.createProjectPlan(project.id, { name })
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
      resetBackupHistory()
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
    const expectedProjectId = project.id
    const expectedIdentity = planIdentity(expectedProjectId, selected)
    const request = ++planLoadRequest.current
    try {
      setBusy(true)
      setError(null)
      const listed = await api.listProjectPlans(expectedProjectId)
      if (request !== planLoadRequest.current || !isCurrentPlan(expectedProjectId, expectedIdentity)) return
      const match = listed.find((plan) => plan.path === selected.path)
      if (!match) {
        setSelected(null)
        setContent('')
        setSavedContent('')
        setConflict(null)
        setConfirmReload(false)
        setPendingOpenPlan(null)
        initialPlanHandledRef.current = null
        resetBackupHistory()
        setError(`Plan ${selected.name} is no longer present in the project lifecycle.`)
        await refresh()
        return
      }
      const loaded = await api.readProjectPlan(expectedProjectId, match.status, match.name)
      if (request !== planLoadRequest.current || !isCurrentPlan(expectedProjectId, expectedIdentity)) return
      setSelected(loaded)
      const loadedContent = loaded.content ?? ''
      setContent(loadedContent)
      setSavedContent(loadedContent)
      setConflict(null)
      setConfirmReload(false)
      resetBackupHistory()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to reload the plan')
    } finally {
      setBusy(false)
    }
  }

  function closePlan() {
    planLoadRequest.current += 1
    setSelected(null)
    setContent('')
    setSavedContent('')
    setConflict(null)
    setConfirmReload(false)
    setConfirmClose(false)
    setPendingOpenPlan(null)
    resetBackupHistory()
  }

  function discardAndOpenPendingPlan() {
    const next = pendingOpenPlan
    setPendingOpenPlan(null)
    closePlan()
    if (next) void openPlan(next)
  }

  const runnable = selected !== null && selected.status === 'in_progress' && !dirty
  const hosted = useHeaderSlots('plan-panel', {
    context: selected ? <h2 className="header-context-title">{selected.name} <span className="text-xs text-dim">{selected.status === 'todo' ? 'Draft' : selected.status === 'in_progress' ? 'Ready' : 'Done'}</span></h2> : <h2 className="header-context-title">Plans</h2>,
    compactContext: selected ? <span className="header-context-title">{selected.name}</span> : undefined,
    local: selected ? <button className="btn btn-secondary btn-sm" onClick={() => (dirty ? setConfirmClose(true) : closePlan())}>← Back to Plans</button> : <label className="header-plan-name"><span>New plan</span><input className="input mono" aria-label="New plan filename" placeholder="new-plan.md" value={newName} onChange={(event) => setNewName(event.target.value)} /></label>,
    primary: selected ? <button className="btn btn-primary btn-sm" onClick={() => void savePlan()} disabled={busy}>{busy ? 'Working…' : 'Save'}</button> : <button className="btn btn-primary btn-sm" onClick={() => void createPlan()} disabled={busy || !newName.trim()}>Create plan</button>,
    more: selected ? <MoreMenu label="More plan actions" triggerLabel="More">
      {conflict && !confirmReload && <MenuItem onClick={() => setConfirmReload(true)}>Reload from server…</MenuItem>}
      {selected.status !== 'done' && <MenuItem disabled={busy || dirty} onClick={() => void promotePlan()}>Move to {selected.status === 'todo' ? 'Ready' : 'Done'}</MenuItem>}
      {selected.status === 'in_progress' && <MenuItem disabled={!runnable} onClick={() => onOpenRunDashboard(selected.path)}>Run this plan</MenuItem>}
    </MoreMenu> : <MoreMenu label="More plan actions" triggerLabel="More"><MenuItem onClick={() => void refresh()}>Refresh plans</MenuItem></MoreMenu>,
  })

  if (selected) {
    // Only a saved Ready plan can run: a dirty draft or another lifecycle
    // state explains its next step instead of exposing a button that no-ops.
    const lifecycleExplanation = selected.status === 'todo'
      ? 'This draft is not runnable yet. Save it and move it to Ready (in progress) to enable “Run this plan”.'
      : selected.status === 'done'
        ? 'Done plans are kept for the record and cannot run. Create a new plan and move it through Draft → Ready to run this work again.'
        : dirty
          ? 'Save this draft before running the plan.'
          : READY_START_GUIDANCE

    return (
      <div className="plan-editor">
        {!hosted && <div className="plan-editor-header">
          <button className="btn btn-secondary btn-sm" onClick={() => (dirty ? setConfirmClose(true) : closePlan())}>← Back to Plans</button>
          <strong className="mono text-sm">{selected.path}</strong>
          <span className={`status-pill ${selected.status === 'in_progress' ? 'status-awaiting' : ''}`}>
            {selected.status === 'todo' ? 'Draft — not runnable yet' : selected.status === 'in_progress' ? 'Ready' : 'Done — not runnable'}
          </span>
          <span className="text-xs text-dim mono" title={selected.revision}>
            Revision {shortRevision(selected.revision)}
          </span>
        </div>}
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
            <p>Your unsaved plan edits will be lost. Save the plan or discard the edits to {pendingOpenPlan ? 'open the requested plan' : 'return to all plans'}.</p>
            <div className="dashboard-actions">
              <button className="btn btn-danger btn-sm" onClick={discardAndOpenPendingPlan}>Discard edits</button>
              <button className="btn btn-secondary btn-sm" onClick={() => { setPendingOpenPlan(null); setConfirmClose(false) }}>Keep editing</button>
            </div>
          </div>
        )}
        <TextEditor
          className="mono plan-editor-textarea"
          aria-label="Plan content"
          value={content}
          onChange={(event) => setContent(event.target.value)}
        />
        <details
          className="plan-backup-history"
          open={historyOpen}
        >
          <summary onClick={(event) => {
            event.preventDefault()
            const open = !historyOpen
            setHistoryOpen(open)
            if (open && selected) void loadBackupHistory(selected)
          }}>Backup history</summary>
          <div className="plan-backup-history-body">
            <p className="text-sm text-dim">
              Read-only provenance for preserved backup bytes. Unknown original baselines cannot be safely reset;
              restore/reset behavior is deferred.
            </p>
            {history.loading && <p className="text-sm text-dim" role="status">Loading backup history…</p>}
            {history.error && <div className="notice" role="alert">Backup history unavailable: {history.error}</div>}
            {history.page && history.page.backups.length === 0 && !history.loading && !history.error && (
              <p className="text-sm text-dim">No validated backup history is available for this plan.</p>
            )}
            {history.page && history.page.backups.length > 0 && (
              <>
                <ol className="plan-backup-history-list">
                  {history.page.backups.map((backup) => {
                    const label = backupOriginLabel(backup)
                    return (
                      <li className="plan-backup-history-entry" key={`${backup.backup_filename}:${backup.content_sha256 ?? 'unknown'}`}>
                        <div className="plan-backup-history-heading">
                          <span className="status-pill">{label}</span>
                          <strong className="mono text-sm">{backup.backup_filename}</strong>
                        </div>
                        <p className="text-sm text-dim">{backupOriginDescription(backup, label)}</p>
                        <dl className="plan-backup-history-meta">
                          <div><dt>Event</dt><dd>{backupEventLabel(backup.capture_event)}</dd></div>
                          <div><dt>Run</dt><dd className="mono">{backup.run_id ?? 'Not allocated'}</dd></div>
                          <div><dt>Turn</dt><dd>{backup.turn_number ?? 'Not recorded'}</dd></div>
                          <div><dt>Captured</dt><dd className="mono">{backup.timestamp ?? 'Unavailable'}</dd></div>
                        </dl>
                      </li>
                    )
                  })}
                </ol>
                <div className="plan-backup-history-pagination">
                  <span className="text-xs text-dim">
                    Showing {history.page.offset + 1}–{Math.min(history.page.offset + history.page.backups.length, history.page.total_items)} of {history.page.total_items}
                  </span>
                  <div className="plan-editor-actions">
                    {history.page.offset > 0 && (
                      <button
                        type="button"
                        className="btn btn-secondary btn-sm"
                        disabled={history.loading}
                        onClick={() => void loadBackupHistory(selected, Math.max(history.page!.offset - history.page!.limit, 0))}
                      >
                        Previous
                      </button>
                    )}
                    {history.page.next_offset !== null && (
                      <button
                        type="button"
                        className="btn btn-secondary btn-sm"
                        disabled={history.loading}
                        onClick={() => void loadBackupHistory(selected, history.page!.next_offset!)}
                      >
                        Next
                      </button>
                    )}
                  </div>
                </div>
              </>
            )}
          </div>
        </details>
        {!hosted && <div className="plan-editor-actions">
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
        </div>}
        {lifecycleExplanation && <div className="text-sm text-dim">{lifecycleExplanation}</div>}
        {dirty && selected.status !== 'in_progress' && (
          <div className="text-sm text-dim">Save this draft before moving it through the lifecycle.</div>
        )}
      </div>
    )
  }

  return (
    <div className="plan-list">
      {error && <div className="error-message" role="alert">{error}</div>}
      {!hosted && <div className="card" style={{ display: 'flex', gap: 'var(--spacing-sm)', flexWrap: 'wrap' }}>
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
      </div>}
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
