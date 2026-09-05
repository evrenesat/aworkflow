import { useState } from 'react'
import type { ProjectCreateRequest, ProjectCreateResult } from '../types'

interface ProjectCreateFormProps {
  onSubmit: (request: ProjectCreateRequest) => Promise<ProjectCreateResult>
  onCancel: () => void
}

/**
 * Typed create/register form for one canonical registered project.  Every
 * field maps to the server contract; there is no free-form filesystem input
 * beyond the managed-root relative path the registry accepts.
 */
export function ProjectCreateForm({ onSubmit, onCancel }: ProjectCreateFormProps) {
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
    const request: ProjectCreateRequest = {
      mode,
      path: path.trim(),
      display_name: displayName.trim() || null,
      main_branch: mainBranch.trim() || 'main',
      initial_workflow: initialWorkflow.trim() || null,
      initial_team: initialTeam.trim() || null,
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
          Registering records an existing directory beneath the managed root. Existing
          commits, files, and configuration are never modified.
        </p>
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
