import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError } from '../api'
import * as api from '../api'
import type { SettingsResponse } from '../types'
import { TextEditor } from './TextEditor'

/**
 * Global transport settings panel: shared password, binding, and projects
 * root, edited through write-only fields and a credential-free advanced TOML
 * view. Binding and root changes require a server restart; a password change
 * applies immediately and ends other sessions.
 */
export function SettingsPanel({ onDirtyChange }: { onDirtyChange: (dirty: boolean) => void }) {
  const [snapshot, setSnapshot] = useState<SettingsResponse | null>(null)
  const [advancedText, setAdvancedText] = useState('')
  const [password, setPassword] = useState('')
  const [rootDraft, setRootDraft] = useState('')
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const advancedTouchedRef = useRef(false)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    setNotice(null)
    try {
      const loaded = await api.getSettings()
      setSnapshot(loaded)
      setAdvancedText(loaded.advanced_toml)
      setRootDraft(loaded.managed_projects_root)
      advancedTouchedRef.current = false
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load settings')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void load() }, [load])

  const dirty = Boolean(
    snapshot
    && (advancedTouchedRef.current || password !== '' || rootDraft !== snapshot.managed_projects_root),
  )

  useEffect(() => {
    onDirtyChange(dirty)
    return () => onDirtyChange(false)
  }, [dirty, onDirtyChange])

  async function handleSave() {
    if (!snapshot) return
    try {
      setBusy(true)
      setError(null)
      setNotice(null)
      const saved = await api.saveSettings({
        expected_revision: snapshot.revision,
        advanced_toml: advancedTouchedRef.current ? advancedText : null,
        managed_projects_root: rootDraft.trim() === snapshot.managed_projects_root ? null : rootDraft.trim(),
        password: password === '' ? null : password,
      })
      setSnapshot(saved)
      setAdvancedText(saved.advanced_toml)
      setRootDraft(saved.managed_projects_root)
      setPassword('')
      advancedTouchedRef.current = false
      const restartKeys = Object.entries(saved.restart).filter(([, changed]) => changed).map(([key]) => key)
      setNotice(
        restartKeys.length > 0
          ? `Settings saved. Restart the server (aflow ui --stop, then aflow ui) for: ${restartKeys.join(', ')}.`
          : 'Settings saved.',
      )
    } catch (err) {
      if (err instanceof ApiError && err.code === 'revision_conflict') {
        setError('The settings changed on the server. Reload and reapply your edits.')
      } else {
        setError(err instanceof Error ? err.message : 'Failed to save settings')
      }
    } finally {
      setBusy(false)
    }
  }

  if (loading) {
    return <div className="card dashboard-loading"><div className="spinner" />Loading server settings…</div>
  }

  if (!snapshot) {
    return (
      <div className="card">
        <h3 style={{ fontWeight: 600 }}>Server settings</h3>
        {error && <div className="error-message" role="alert">{error}</div>}
        <button className="btn btn-secondary" onClick={() => void load()}>Retry</button>
      </div>
    )
  }

  const restartPending = Object.values(snapshot.restart).some(Boolean)

  return (
    <div className="card config-editor">
      <div className="section-heading">
        <div>
          <h3 style={{ fontWeight: 600 }}>Server settings</h3>
          <div className="text-xs text-dim">
            Shared by every project on this AFlow installation. The password is the
            login credential and is never displayed after it is set.
          </div>
        </div>
        <span className="text-xs text-dim mono" title={snapshot.revision}>
          Revision {snapshot.revision.slice(0, 12)}
        </span>
      </div>

      {restartPending && (
        <div className="notice" role="status">
          Saved binding or projects-root changes are not active for the running server yet.
          Restart with <code>aflow ui --stop</code> then <code>aflow ui</code> to apply them.
        </div>
      )}
      {notice && <div className="success-message" role="status">{notice}</div>}
      {error && <div className="error-message" role="alert">{error}</div>}

      <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--spacing-md)' }}>
        <label style={{ display: 'flex', flexDirection: 'column', gap: 'var(--spacing-xs)' }}>
          <span className="text-sm">New password (leave empty to keep the current one)</span>
          <input
            className="input"
            type="password"
            autoComplete="new-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            placeholder={snapshot.password_set ? '••••••••' : 'Not set yet'}
          />
          {password !== '' && (
            <span className="text-xs text-dim">Saving ends existing browser sessions.</span>
          )}
        </label>

        <label style={{ display: 'flex', flexDirection: 'column', gap: 'var(--spacing-xs)' }}>
          <span className="text-sm">Projects root</span>
          <input
            className="input mono"
            value={rootDraft}
            onChange={(event) => setRootDraft(event.target.value)}
            aria-label="Projects root"
          />
          <span className="text-xs text-dim">
            Only registered Git projects beneath this root are reachable. Changing it requires a
            restart; records outside the new root become unavailable until moved back.
          </span>
        </label>

        <details>
          <summary className="text-sm" style={{ cursor: 'pointer' }}>Advanced TOML (connection settings)</summary>
          <p className="text-xs text-dim">
            The credential keys are intentionally omitted here; omitting them preserves the
            stored password. Editing this file by hand also works — the legacy
            <code> aflow-app-server</code> entry point reads the same file.
          </p>
          <TextEditor
            className="mono config-textarea"
            aria-label="Advanced connection settings TOML"
            spellCheck={false}
            value={advancedText}
            onChange={(event) => { advancedTouchedRef.current = true; setAdvancedText(event.target.value) }}
          />
        </details>
      </div>

      <div className="dashboard-actions">
        <button
          className="btn btn-primary"
          onClick={() => void handleSave()}
          disabled={busy || !dirty}
        >
          {busy ? 'Saving…' : 'Save settings'}
        </button>
      </div>
    </div>
  )
}
