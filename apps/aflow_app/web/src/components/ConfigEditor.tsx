import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError } from '../api'
import * as api from '../api'
import type {
  ConfigBlockedRun,
  ConfigValidation,
  ProjectConfig,
  ProjectInfo,
} from '../types'
import { GuidedConfigForm } from './GuidedConfigForm'

type ConfigTab = 'aflow' | 'workflows'
type EditorMode = 'guided' | 'toml'

interface ConfigEditorProps {
  project: ProjectInfo
  /** Reports unsaved text so the shell can guard navigation. */
  onDirtyChange: (dirty: boolean) => void
  /** Reports the canonical result of a successful save without navigating. */
  onSaved?: (saved: ProjectConfig) => void
  /** Opens plans using the saved ready configuration. */
  onReady?: (saved: ProjectConfig) => void
}

interface ConflictState {
  currentRevision: string
}

function stateText(state: ConfigValidation['state']): string {
  if (state === 'ready') return 'Valid — the project configuration is ready.'
  if (state === 'configuration_required') {
    return 'Configuration required — set explicit model selectors before starting workflows.'
  }
  return 'Invalid configuration — fix the diagnostics below.'
}

function shortRevision(revision: string): string {
  return revision.slice(0, 12)
}

/**
 * Two-tab plain text editor for the canonical configuration pair.  Both
 * documents are validated and committed together under one combined
 * revision; local text is never discarded without explicit confirmation.
 */
export function ConfigEditor({ project, onDirtyChange, onSaved, onReady }: ConfigEditorProps) {
  const [snapshot, setSnapshot] = useState<ProjectConfig | null>(null)
  const [aflowText, setAflowText] = useState('')
  const [workflowsText, setWorkflowsText] = useState('')
  const [activeTab, setActiveTab] = useState<ConfigTab>('aflow')
  const [mode, setMode] = useState<EditorMode>('guided')
  const [pendingFocusDocument, setPendingFocusDocument] = useState<'aflow.toml' | 'workflows.toml' | null>(null)
  const [validation, setValidation] = useState<ConfigValidation | null>(null)
  /** The exact pair the held validation describes; anything else is stale. */
  const [validationPair, setValidationPair] = useState<{ aflow: string; workflows: string } | null>(null)
  const [guidedActionPending, setGuidedActionPending] = useState(false)
  const [busy, setBusy] = useState<'validate' | 'save' | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [conflict, setConflict] = useState<ConflictState | null>(null)
  const [blockers, setBlockers] = useState<ConfigBlockedRun[] | null>(null)
  const [confirmReload, setConfirmReload] = useState(false)
  const [loading, setLoading] = useState(true)
  const aflowTabRef = useRef<HTMLButtonElement | null>(null)
  const workflowsTabRef = useRef<HTMLButtonElement | null>(null)
  const aflowTextareaRef = useRef<HTMLTextAreaElement | null>(null)
  const workflowsTextareaRef = useRef<HTMLTextAreaElement | null>(null)

  useEffect(() => {
    if (mode !== 'toml' || pendingFocusDocument === null) return
    ;(pendingFocusDocument === 'workflows.toml' ? workflowsTextareaRef : aflowTextareaRef)
      .current?.focus()
    setPendingFocusDocument(null)
  }, [mode, pendingFocusDocument])

  function openAdvancedTOML(document: 'aflow.toml' | 'workflows.toml' = 'aflow.toml') {
    setPendingFocusDocument(document)
    setMode('toml')
  }

  const dirty = Boolean(
    snapshot
    && (aflowText !== snapshot.aflow_toml || workflowsText !== snapshot.workflows_toml),
  )
  // A ready result may only offer the Plans shortcut while it describes the
  // exact committed pair: dirty or superseded candidates never qualify.
  const validationCurrent = validationPair !== null
    && validationPair.aflow === aflowText
    && validationPair.workflows === workflowsText

  const hasUnsavedWork = dirty || guidedActionPending

  useEffect(() => {
    onDirtyChange(hasUnsavedWork)
    return () => onDirtyChange(false)
  }, [hasUnsavedWork, onDirtyChange])

  useEffect(() => {
    if (!hasUnsavedWork) return
    const guard = (event: BeforeUnloadEvent) => {
      event.preventDefault()
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', guard)
    return () => window.removeEventListener('beforeunload', guard)
  }, [hasUnsavedWork])

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    setNotice(null)
    setConflict(null)
    setBlockers(null)
    setConfirmReload(false)
    try {
      const loaded = await api.getProjectConfig(project.id)
      setSnapshot(loaded)
      setAflowText(loaded.aflow_toml)
      setWorkflowsText(loaded.workflows_toml)
      setValidation(loaded.validation)
      setValidationPair({ aflow: loaded.aflow_toml, workflows: loaded.workflows_toml })
    } catch (err) {
      setSnapshot(null)
      setAflowText('')
      setWorkflowsText('')
      setValidation(null)
      setValidationPair(null)
      setError(err instanceof Error ? err.message : 'Failed to load the configuration pair')
    } finally {
      setLoading(false)
    }
  }, [project.id])

  useEffect(() => {
    void load()
  }, [load])

  function applyCommitted(next: ProjectConfig, actionLabel: string) {
    setSnapshot(next)
    setAflowText(next.aflow_toml)
    setWorkflowsText(next.workflows_toml)
    setValidation(next.validation)
    setValidationPair({ aflow: next.aflow_toml, workflows: next.workflows_toml })
    setConflict(null)
    setBlockers(null)
    setConfirmReload(false)
    setNotice(`${actionLabel} revision ${shortRevision(next.revision)}.`)
  }

  async function handleValidate() {
    const submitted = { aflow_toml: aflowText, workflows_toml: workflowsText }
    try {
      setBusy('validate')
      setError(null)
      setNotice(null)
      const result = await api.validateProjectConfig(project.id, submitted)
      setValidation(result)
      setValidationPair({ aflow: submitted.aflow_toml, workflows: submitted.workflows_toml })
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Validation request failed')
    } finally {
      setBusy(null)
    }
  }

  async function handleSave() {
    if (!snapshot) return
    try {
      setBusy('save')
      setError(null)
      setNotice(null)
      const saved = await api.saveProjectConfig(project.id, {
        aflow_toml: aflowText,
        workflows_toml: workflowsText,
        expected_revision: snapshot.revision,
      })
      applyCommitted(saved, 'Saved')
      onSaved?.(saved)
      return saved
    } catch (err) {
      if (err instanceof ApiError && err.code === 'revision_conflict') {
        const current = err.detail.current_revision
        setConflict({ currentRevision: typeof current === 'string' ? current : 'unknown' })
        setNotice(null)
      } else if (err instanceof ApiError && err.code === 'config_save_blocked') {
        const runs = Array.isArray(err.detail.blocking_runs) ? err.detail.blocking_runs : []
        setBlockers(runs as ConfigBlockedRun[])
        setNotice(null)
      } else {
        setError(err instanceof Error ? err.message : 'Failed to save the configuration pair')
      }
      // Local text is deliberately preserved for every failure above.
      return null
    } finally {
      setBusy(null)
    }
  }

  /** Guided primary path: commit the shared pair, then continue to Plans when ready. */
  async function handleSaveAndContinue() {
    const saved = await handleSave()
    if (saved && saved.validation.state === 'ready') onReady?.(saved)
  }

  async function handleDiscardAndReload() {
    await load()
  }

  function handleTabKeys(event: React.KeyboardEvent) {
    const next = event.key === 'ArrowRight' || event.key === 'Home'
      ? 'aflow' as const
      : event.key === 'ArrowLeft' || event.key === 'End'
        ? 'workflows' as const
        : null
    if (next === null) return
    event.preventDefault()
    setActiveTab(next)
    ;(next === 'aflow' ? aflowTabRef : workflowsTabRef).current?.focus()
  }

  if (loading) {
    return <div className="card dashboard-loading"><div className="spinner" />Loading configuration…</div>
  }

  if (!snapshot) {
    return (
      <div className="card config-editor">
        <h3 style={{ fontWeight: 600 }}>Configuration</h3>
        {error && <div className="error-message" role="alert">{error}</div>}
        <p className="text-sm text-dim">
          The pair <code>.aflow/config/aflow.toml</code> and <code>workflows.toml</code> could
          not be read for this project. New projects receive the starter pair; registering an
          existing project without configuration requires initializing it from the server.
        </p>
        <div className="dashboard-actions">
          <button className="btn btn-secondary" onClick={() => void load()}>Retry</button>
        </div>
      </div>
    )
  }

  return (
    <div className="config-editor">
      <div className="section-heading">
        <div>
          <h3 style={{ fontWeight: 600 }}>Configuration</h3>
          <div className="text-xs text-dim">
            Both documents are saved and validated together under one combined revision.
          </div>
        </div>
        <span className="text-xs text-dim mono" title={snapshot.revision}>
          Revision {shortRevision(snapshot.revision)}
          {dirty ? ' · Unsaved edits' : ''}
        </span>
      </div>

      {notice && <div className="success-message" role="status">{notice}</div>}
      {error && <div className="error-message" role="alert">{error}</div>}

      {conflict && (
        <div className="error-message" role="alert">
          <p>
            The saved configuration changed on the server (current revision{' '}
            <span className="mono">{shortRevision(conflict.currentRevision)}</span>). Your edits
            are still in the editor. Reload the server copy to continue — copy any text you
            want to keep first.
          </p>
          {!confirmReload ? (
            <button className="btn btn-secondary btn-sm" onClick={() => setConfirmReload(true)}>
              Reload server copy…
            </button>
          ) : (
            <div className="confirmation">
              <span className="text-sm">Discard the local edits in both tabs and reload the server copy?</span>
              <button className="btn btn-danger btn-sm" onClick={() => void handleDiscardAndReload()}>
                Discard edits and reload
              </button>
              <button className="btn btn-secondary btn-sm" onClick={() => setConfirmReload(false)}>
                Keep editing
              </button>
            </div>
          )}
        </div>
      )}

      {blockers && (
        <div className="error-message" role="alert">
          <p>Saving is blocked while these runs may still execute or resume:</p>
          <ul className="blocked-runs">
            {blockers.map((run) => (
              <li key={run.run_id} className="mono text-sm">{run.run_id} — {run.status}</li>
            ))}
          </ul>
          <p className="text-sm text-dim">
            Stop or finish the runs above, then save again. Configuration edits apply to
            future runs only.
          </p>
        </div>
      )}

      <div role="group" aria-label="Settings views" className="config-tabs">
        <button
          className={`btn btn-sm ${mode === 'guided' ? 'btn-primary' : 'btn-secondary'}`}
          aria-pressed={mode === 'guided'}
          onClick={() => setMode('guided')}
        >
          Guided settings
        </button>
        <button
          className={`btn btn-sm ${mode === 'toml' ? 'btn-primary' : 'btn-secondary'}`}
          aria-pressed={mode === 'toml'}
          onClick={() => setMode('toml')}
        >
          Advanced TOML
        </button>
      </div>

      <div hidden={mode !== 'guided'} className="guided-panel">
        <GuidedConfigForm
          project={project}
          aflowText={aflowText}
          workflowsText={workflowsText}
          onDraftChange={(nextAflow, nextWorkflows) => {
            setAflowText(nextAflow)
            setWorkflowsText(nextWorkflows)
          }}
          onActionPendingChange={setGuidedActionPending}
          onCandidateValidation={(nextValidation, pair) => {
            setValidation(nextValidation)
            setValidationPair(pair)
          }}
          onRequestAdvanced={openAdvancedTOML}
        />
      </div>

      <div hidden={mode !== 'toml'} className="toml-panel">
      <div role="tablist" aria-label="Configuration documents" className="config-tabs" onKeyDown={handleTabKeys}>
        <button
          ref={aflowTabRef}
          role="tab"
          id="config-tab-aflow"
          aria-selected={activeTab === 'aflow'}
          aria-controls="config-panel-aflow"
          tabIndex={activeTab === 'aflow' ? 0 : -1}
          className={`btn btn-sm ${activeTab === 'aflow' ? 'btn-primary' : 'btn-secondary'}`}
          onClick={() => setActiveTab('aflow')}
        >
          aflow.toml
        </button>
        <button
          ref={workflowsTabRef}
          role="tab"
          id="config-tab-workflows"
          aria-selected={activeTab === 'workflows'}
          aria-controls="config-panel-workflows"
          tabIndex={activeTab === 'workflows' ? 0 : -1}
          className={`btn btn-sm ${activeTab === 'workflows' ? 'btn-primary' : 'btn-secondary'}`}
          onClick={() => setActiveTab('workflows')}
        >
          workflows.toml
        </button>
      </div>

      <div
        role="tabpanel"
        id="config-panel-aflow"
        aria-labelledby="config-tab-aflow"
        hidden={activeTab !== 'aflow'}
      >
        <textarea
          ref={aflowTextareaRef}
          className="input mono config-textarea"
          aria-label="aflow.toml contents"
          spellCheck={false}
          value={aflowText}
          onChange={(event) => setAflowText(event.target.value)}
        />
      </div>
      <div
        role="tabpanel"
        id="config-panel-workflows"
        aria-labelledby="config-tab-workflows"
        hidden={activeTab !== 'workflows'}
      >
        <textarea
          ref={workflowsTextareaRef}
          className="input mono config-textarea"
          aria-label="workflows.toml contents"
          spellCheck={false}
          value={workflowsText}
          onChange={(event) => setWorkflowsText(event.target.value)}
        />
      </div>
      </div>

      <div className="dashboard-actions">
        <button
          className="btn btn-secondary"
          onClick={() => void handleValidate()}
          disabled={busy !== null}
        >
          {busy === 'validate' ? 'Validating…' : 'Validate'}
        </button>
        <button
          className="btn btn-primary"
          onClick={() => void handleSave()}
          disabled={busy !== null || guidedActionPending || !dirty}
        >
          {busy === 'save' ? 'Saving…' : 'Save both files'}
        </button>
        {mode === 'guided' && (
          <button
            className="btn btn-primary"
            onClick={() => void handleSaveAndContinue()}
            disabled={busy !== null || guidedActionPending || !dirty}
          >
            {busy === 'save' ? 'Saving…' : 'Save and continue to Plans'}
          </button>
        )}
      </div>

      {validation && (
        <div className="card validation-report" aria-label="Configuration validation report">
          <div className="section-heading">
            <strong className="text-sm">{stateText(validation.state)}</strong>
            {validation.state === 'ready' && onReady && !dirty && validationCurrent && (
              <button className="btn btn-primary btn-sm" disabled={busy !== null || guidedActionPending} onClick={() => onReady(snapshot)}>
                Go to plans
              </button>
            )}
          </div>
          {!validationCurrent && (
            <p className="text-xs text-dim" role="note">
              This report is out of date for the edited draft — validate again to refresh it.
            </p>
          )}
          {validation.issues.length > 0 && (
            <ul className="validation-issues">
              {validation.issues.map((issue, index) => (
                <li key={index} className="text-sm">
                  <span className="mono">
                    {issue.document ?? 'config'}{issue.line !== null ? `:${issue.line}` : ''}
                  </span>{' '}
                  {issue.message}
                </li>
              ))}
            </ul>
          )}
          {validation.placeholders.length > 0 && (
            <p className="text-sm text-dim">
              Placeholder selectors to replace:{' '}
              <span className="mono">{validation.placeholders.join(', ')}</span>
            </p>
          )}
          <p className="text-xs text-dim">
            Workflows: {validation.workflows.join(', ') || 'none'} · Teams:{' '}
            {validation.teams.join(', ') || 'none'} · Roles: {validation.roles.join(', ') || 'none'}
          </p>
        </div>
      )}
    </div>
  )
}
