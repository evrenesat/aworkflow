import { useEffect, useState } from 'react'
import type { ProjectCreateRequest, ProjectCreateResult, ProjectInfo } from '../types'
import { readinessClass, readinessLabel } from '../readiness'
import { ProjectCreateForm } from './ProjectCreateForm'

interface ProjectPickerProps {
  projects: ProjectInfo[]
  selectedProjectId: string | null
  loading: boolean
  error: string | null
  onSelectProject: (project: ProjectInfo) => void
  onRefresh: () => void
  onCreate: (request: ProjectCreateRequest) => Promise<ProjectCreateResult>
  onUnregister: (projectId: string) => Promise<void>
}

/**
 * Lists exactly the registry-backed projects and offers typed create,
 * register, and non-destructive unregister flows.
 */
export function ProjectPicker({
  projects,
  selectedProjectId,
  loading,
  error,
  onSelectProject,
  onRefresh,
  onCreate,
  onUnregister,
}: ProjectPickerProps) {
  const [showCreateForm, setShowCreateForm] = useState(false)
  const [confirmingUnregisterId, setConfirmingUnregisterId] = useState<string | null>(null)
  const [unregisterError, setUnregisterError] = useState<string | null>(null)

  useEffect(() => {
    if (!loading && !error && projects.length === 0) setShowCreateForm(true)
  }, [loading, error, projects.length])

  async function handleUnregister(project: ProjectInfo) {
    try {
      setUnregisterError(null)
      await onUnregister(project.id)
      setConfirmingUnregisterId(null)
    } catch (err) {
      setUnregisterError(err instanceof Error ? err.message : 'Failed to unregister project')
    }
  }

  return (
    <div className="project-picker">
      <div className="section-heading">
        <div>
          <h2 style={{ fontSize: '1.15rem', fontWeight: 600 }}>Projects</h2>
          <div className="text-xs text-dim">Registered beneath the server's managed root.</div>
        </div>
        <div className="dashboard-actions">
          <button className="btn btn-secondary btn-sm" onClick={onRefresh} disabled={loading}>
            Refresh
          </button>
          <button
            className="btn btn-primary btn-sm"
            onClick={() => setShowCreateForm((current) => !current)}
            aria-expanded={showCreateForm}
          >
            {showCreateForm ? 'Close form' : 'Add project'}
          </button>
        </div>
      </div>

      {error && (
        <div className="error-message" role="alert">
          {error}
          <div className="dashboard-actions" style={{ marginTop: 'var(--spacing-sm)' }}>
            <button className="btn btn-secondary btn-sm" onClick={onRefresh}>Retry</button>
          </div>
        </div>
      )}

      {loading && <div className="dashboard-loading card"><div className="spinner" />Loading registered projects…</div>}

      {!loading && !error && (
        <>
          {projects.length === 0 && (
            <div className="card text-dim text-sm">
              No registered projects. Create a new project or register an existing directory
              beneath the managed root.
            </div>
          )}

          <div className="project-list" role="list">
            {projects.map((project) => {
              const isSelected = selectedProjectId === project.id
              const confirming = confirmingUnregisterId === project.id
              return (
                <div
                  key={project.id}
                  role="listitem"
                  className={`card card-interactive project-item ${isSelected ? 'selected' : ''}`}
                >
                  <button
                    className="content-button"
                    aria-pressed={isSelected}
                    onClick={() => onSelectProject(project)}
                  >
                    <div className="content-button-row">
                      <span style={{ fontWeight: 600, overflowWrap: 'anywhere' }}>{project.display_name}</span>
                      <span className={readinessClass(project.readiness)}>
                        {readinessLabel(project.readiness)}
                      </span>
                    </div>
                    <span className="text-xs text-dim mono" style={{ overflowWrap: 'anywhere' }}>
                      {project.current_path}
                    </span>
                  </button>

                  {confirming ? (
                    <div className="confirmation unregister-confirm">
                      <span className="text-sm">
                        Remove “{project.display_name}” from the registry? Files, Git history,
                        and plans on disk are preserved; the project can be registered again.
                      </span>
                      <div className="dashboard-actions">
                        <button className="btn btn-danger btn-sm" onClick={() => void handleUnregister(project)}>
                          Unregister (keeps files)
                        </button>
                        <button className="btn btn-secondary btn-sm" onClick={() => setConfirmingUnregisterId(null)}>
                          Cancel
                        </button>
                      </div>
                    </div>
                  ) : (
                    <div className="dashboard-actions">
                      <button className="btn btn-secondary btn-sm" onClick={() => onSelectProject(project)}>
                        Open
                      </button>
                      <button
                        className="btn btn-secondary btn-sm"
                        onClick={() => { setUnregisterError(null); setConfirmingUnregisterId(project.id) }}
                      >
                        Unregister…
                      </button>
                    </div>
                  )}
                </div>
              )
            })}
          </div>

          {unregisterError && <div className="error-message" role="alert">{unregisterError}</div>}

          {showCreateForm && (
            <ProjectCreateForm
              onSubmit={async (request) => {
                const created = await onCreate(request)
                setShowCreateForm(false)
                return created
              }}
              onCancel={() => setShowCreateForm(false)}
            />
          )}
        </>
      )}
    </div>
  )
}
