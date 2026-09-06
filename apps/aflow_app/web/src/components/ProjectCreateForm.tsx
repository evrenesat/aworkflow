import { useState } from 'react'
import type { ProjectCreateRequest, ProjectCreateResult, ProjectDiscoveryCandidate } from '../types'

interface ProjectCreateFormProps {
  suggestions: ProjectDiscoveryCandidate[]
  onSubmit: (request: ProjectCreateRequest) => Promise<ProjectCreateResult>
  onCancel: () => void
}

/**
 * Typed create/register form for one canonical registered project.  Every
 * field maps to the server contract; there is no free-form filesystem input
 * beyond the managed-root relative path the registry accepts.  In register
 * mode, discovered candidates act as an accessible suggestion list while the
 * relative path stays manually editable for deeper projects.
 */
export function ProjectCreateForm({ suggestions, onSubmit, onCancel }: ProjectCreateFormProps) {
  const [mode, setMode] = useState<'create' | 'register'>('create')
  const [path, setPath] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [mainBranch, setMainBranch] = useState('main')
  const [initialWorkflow, setInitialWorkflow] = useState('')
  const [initialTeam, setInitialTeam] = useState('')
  const [initializeGit, setInitializeGit] = useState(false)
  const [initializeConfig, setInitializeConfig] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function handleSubmit() {
    if (!path.trim() || busy) return
    // Hidden starter fields must never be submitted: register mode without
    // configuration initialization writes no starter values at all.
    const sendStarterValues = mode !== 'register' || initializeConfig
    const request: ProjectCreateRequest = {
      mode,
      path: path.trim(),
      display_name: displayName.trim() || null,
      main_branch: mainBranch.trim() || 'main',
      initial_workflow: sendStarterValues ? initialWorkflow.trim() || null : null,
      initial_team: sendStarterValues ? initialTeam.trim() || null : null,
    }
    if (mode === 'register') {
      request.initialize_git = initializeGit
      request.initialize_config = initializeConfig
    }
    try {
      setBusy(true)
      setError(null)
      await onSubmit(request)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create or register the project')
    } finally {
      setBusy(false)
    }
  }

  const showStarterSettings = mode === 'create' || initializeConfig

  return (
    <form
      className="project-create-form card"
      aria-label="Create or register a project"
      onSubmit={(event) => {
        event.preventDefault()
        void handleSubmit()
      }}
    >
      <fieldset className="form-fieldset">
        <legend className="text-sm text-dim">Mode</legend>
        <label className="form-radio">
          <input
            type="radio"
            name="project-mode"
            value="create"
            checked={mode === 'create'}
            onChange={() => setMode('create')}
          />
          <span>Create a new project directory</span>
        </label>
        <label className="form-radio">
          <input
            type="radio"
            name="project-mode"
            value="register"
            checked={mode === 'register'}
            onChange={() => setMode('register')}
          />
          <span>Register an existing directory</span>
        </label>
      </fieldset>

      {mode === 'create' ? (
        <p className="text-xs text-dim">
          Creating initializes a local Git repository on the chosen main branch with one
          minimal initial commit and writes the starter configuration pair. Nothing is
          pushed and no remote is configured.
        </p>
      ) : (
        <p className="text-xs text-dim">
          Adding records an existing directory beneath the managed root. Existing
          commits, files, and configuration are never modified.
        </p>
      )}

      {mode === 'register' && suggestions.length > 0 && (
        <fieldset className="form-fieldset">
          <legend className="text-sm text-dim">Discovered candidates</legend>
          <ul className="suggestion-list" aria-label="Discovered project candidates">
            {suggestions.map((candidate) => (
              <li key={candidate.relative_path}>
                <button
                  type="button"
                  className="suggestion-option"
                  onClick={() => {
                    setPath(candidate.relative_path)
                    if (!displayName.trim()) setDisplayName(candidate.display_name)
                  }}
                >
                  <span style={{ fontWeight: 600 }}>{candidate.display_name}</span>
                  <span className="text-xs text-dim mono">{candidate.relative_path}</span>
                </button>
              </li>
            ))}
          </ul>
          <p className="text-xs text-dim">
            Discovery scans two directory levels beneath the managed root; deeper projects
            can be entered by relative path below.
          </p>
        </fieldset>
      )}

      <label className="dashboard-field">
        <span>Project path beneath the managed root</span>
        <input
          className="input mono"
          aria-label="Relative project path"
          placeholder="team/project"
          value={path}
          onChange={(event) => setPath(event.target.value)}
          required
        />
        <span className="text-xs text-dim">
          Normalized relative directory path, for example <code>team/project</code>.
        </span>
      </label>

      <div className="dashboard-form-grid">
        <label className="dashboard-field">
          <span>Display name (optional)</span>
          <input
            className="input"
            aria-label="Display name"
            value={displayName}
            onChange={(event) => setDisplayName(event.target.value)}
          />
        </label>
        <label className="dashboard-field">
          <span>Main branch</span>
          <input
            className="input mono"
            aria-label="Main branch"
            value={mainBranch}
            onChange={(event) => setMainBranch(event.target.value)}
          />
        </label>
        {showStarterSettings && (
          <>
            <label className="dashboard-field">
              <span>Initial workflow (optional)</span>
              <input
                className="input mono"
                aria-label="Initial workflow"
                value={initialWorkflow}
                onChange={(event) => setInitialWorkflow(event.target.value)}
              />
              <span className="text-xs text-dim">Starter default when empty.</span>
            </label>
            <label className="dashboard-field">
              <span>Initial team (optional)</span>
              <input
                className="input mono"
                aria-label="Initial team"
                value={initialTeam}
                onChange={(event) => setInitialTeam(event.target.value)}
              />
            </label>
          </>
        )}
      </div>

      {mode === 'register' && (
        <div className="form-checks">
          <label className="form-check">
            <input
              type="checkbox"
              checked={initializeGit}
              onChange={(event) => setInitializeGit(event.target.checked)}
            />
            <span>
              Initialize a Git repository in the target directory
            </span>
          </label>
          <p className="text-xs text-dim">
            Confirmation required for directories that are not already Git repositories;
            only an empty directory can be initialized. Existing repositories keep their
            history unchanged.
          </p>
          <label className="form-check">
            <input
              type="checkbox"
              checked={initializeConfig}
              onChange={(event) => setInitializeConfig(event.target.checked)}
            />
            <span>Write the starter configuration pair if the directory has none</span>
          </label>
          <p className="text-xs text-dim">
            Never overwrites existing <code>aflow.toml</code> / <code>workflows.toml</code>.
            Optional starter settings appear only when this is chosen.
          </p>
        </div>
      )}

      {error && <div className="error-message" role="alert">{error}</div>}

      <div className="dashboard-actions">
        <button className="btn btn-primary" type="submit" disabled={busy || !path.trim()}>
          {busy ? 'Working…' : mode === 'create' ? 'Create project' : 'Register project'}
        </button>
        <button className="btn btn-secondary" type="button" onClick={onCancel} disabled={busy}>
          Cancel
        </button>
      </div>
    </form>
  )
}
