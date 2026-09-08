import { useCallback, useEffect, useState } from 'react'
import type {
  ProjectCreateRequest,
  ProjectCreateResult,
  ProjectDiscovery,
  ProjectDiscoveryCandidate,
  ProjectInfo,
} from '../types'
import { readinessClass, readinessLabel } from '../readiness'
import * as api from '../api'
import { ProjectCreateForm } from './ProjectCreateForm'

interface ProjectPickerProps {
  projects: ProjectInfo[]
  selectedProjectId: string | null
  loading: boolean
  error: string | null
  onSelectProject: (project: ProjectInfo) => void
  onRefresh: () => void | Promise<void>
  onCreate: (request: ProjectCreateRequest) => Promise<ProjectCreateResult>
  onUnregister: (projectId: string) => Promise<void>
}

function matchesQuery(query: string, ...texts: string[]): boolean {
  if (!query) return true
  return texts.some((text) => text.toLowerCase().includes(query))
}

/**
 * Shared candidate text.  Interactive behavior lives in the caller so an
 * unregistered candidate never exposes a no-op control.
 */
function CandidateContent({
  candidate,
  registered,
}: {
  candidate: ProjectDiscoveryCandidate
  registered: ProjectInfo | null
}) {
  return (
    <>
      <div className="content-button-row">
        <span style={{ fontWeight: 600, overflowWrap: 'anywhere' }}>{candidate.display_name}</span>
        {candidate.registered_project_id !== null && (
          <span className="status-pill">Added</span>
        )}
      </div>
      <span className="text-xs text-dim mono" style={{ overflowWrap: 'anywhere' }}>
        {candidate.relative_path}
      </span>
      {!registered && candidate.add_blocker && (
        <span className="text-xs text-dim">
          Cannot be added: {candidate.add_blocker}.
        </span>
      )}
    </>
  )
}

/**
 * Lists exactly the registry-backed projects, plus read-only discovery of
 * existing Git roots on the connected server.  Discovery never grants
 * access: Add goes through the existing registry-backed register contract.
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
  const [discovery, setDiscovery] = useState<ProjectDiscovery | null>(null)
  const [discoveryLoading, setDiscoveryLoading] = useState(false)
  const [discoveryError, setDiscoveryError] = useState<string | null>(null)
  const [search, setSearch] = useState('')
  const [addError, setAddError] = useState<string | null>(null)
  const [addingPath, setAddingPath] = useState<string | null>(null)

  const loadDiscovery = useCallback(async () => {
    try {
      setDiscoveryLoading(true)
      setDiscoveryError(null)
      setDiscovery(await api.getProjectDiscovery())
    } catch (err) {
      setDiscoveryError(err instanceof Error ? err.message : 'Failed to discover existing projects')
    } finally {
      setDiscoveryLoading(false)
    }
  }, [])

  useEffect(() => {
    void loadDiscovery()
  }, [loadDiscovery])

  useEffect(() => {
    if (!loading && !error && projects.length === 0) setShowCreateForm(true)
  }, [loading, error, projects.length])

  async function handleUnregister(project: ProjectInfo) {
    try {
      setUnregisterError(null)
      await onUnregister(project.id)
      // The removed candidate must become addable again immediately, without
      // a separate manual discovery refresh.
      await loadDiscovery()
      setConfirmingUnregisterId(null)
    } catch (err) {
      setUnregisterError(err instanceof Error ? err.message : 'Failed to unregister project')
    }
  }

  async function handleAdd(candidate: ProjectDiscoveryCandidate) {
    if (addingPath) return
    setAddError(null)
    setAddingPath(candidate.relative_path)
    try {
      await onCreate({
        mode: 'register',
        path: candidate.relative_path,
        display_name: candidate.display_name,
        main_branch: 'main',

        initialize_git: false,
      })
      await loadDiscovery()
    } catch (err) {
      setAddError(err instanceof Error ? err.message : 'Failed to add the project')
      // Keep the current search; refresh both lists in case another client
      // raced us, so a raced registration becomes openable without a manual
      // refresh.
      await Promise.allSettled([onRefresh(), loadDiscovery()])
    } finally {
      setAddingPath(null)
    }
  }

  async function handleRefresh() {
    await onRefresh()
    await loadDiscovery()
  }

  const query = search.trim().toLowerCase()
  const filteredProjects = projects.filter((project) =>
    matchesQuery(query, project.display_name, project.current_path),
  )
  const allCandidates = discovery?.candidates ?? []
  const filteredCandidates = allCandidates.filter((candidate) =>
    matchesQuery(query, candidate.display_name, candidate.relative_path),
  )
  const suggestions = filteredCandidates.filter(
    (candidate) => candidate.registered_project_id === null && candidate.addable,
  )

  return (
    <div className="project-picker">
      <div className="section-heading">
        <div>
          <h2 style={{ fontSize: '1.15rem', fontWeight: 600 }}>Projects</h2>
          <div className="text-xs text-dim">
            Registered beneath the server's managed root
            {discovery && <> — <span className="mono">{discovery.managed_root}</span></>}.
          </div>
        </div>
        <div className="dashboard-actions">
          <button className="btn btn-secondary btn-sm" onClick={handleRefresh} disabled={loading || discoveryLoading}>
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
            <button className="btn btn-secondary btn-sm" onClick={handleRefresh}>Retry</button>
          </div>
        </div>
      )}

      {loading && <div className="dashboard-loading card"><div className="spinner" />Loading registered projects…</div>}

      {!loading && !error && (
        <>
          <label className="dashboard-field project-search">
            <span>Search projects</span>
            <input
              className="input"
              aria-label="Search projects and available candidates"
              placeholder="Type to filter by name or path"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
            />
          </label>

          <h3 className="project-subheading">Added projects</h3>
          {projects.length > 0 && filteredProjects.length === 0 && (
            <div className="card text-dim text-sm">
              No added projects match “{search.trim()}”.
              <div className="dashboard-actions" style={{ marginTop: 'var(--spacing-sm)' }}>
                <button className="btn btn-secondary btn-sm" onClick={() => setSearch('')}>Clear search</button>
              </div>
            </div>
          )}
          {projects.length === 0 && (
            <div className="card text-dim text-sm">
              No registered projects yet. Add one from the available candidates below, or create a
              new project.
            </div>
          )}

          <div className="project-list" role="list">
            {filteredProjects.map((project) => {
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

          <h3 className="project-subheading">Available on this server</h3>
          {discoveryLoading && (
            <div className="dashboard-loading card"><div className="spinner" />Finding existing projects…</div>
          )}
          {discoveryError && (
            <div className="error-message" role="alert">
              Existing projects could not be listed: {discoveryError}
              <div className="dashboard-actions" style={{ marginTop: 'var(--spacing-sm)' }}>
                <button className="btn btn-secondary btn-sm" onClick={() => void loadDiscovery()}>Retry discovery</button>
              </div>
            </div>
          )}
          {!discoveryLoading && !discoveryError && discovery && discovery.candidates.length === 0 && (
            <div className="card text-dim text-sm">
              No discoverable Git projects beneath the managed root yet. Discovery covers direct
              and nested directories up to two levels; deeper or skipped locations can still be
              added by relative path with Add project.
            </div>
          )}
          {!discoveryLoading && !discoveryError && discovery && discovery.candidates.length > 0 && filteredCandidates.length === 0 && (
            <div className="card text-dim text-sm">
              No available projects match “{search.trim()}”.
              <div className="dashboard-actions" style={{ marginTop: 'var(--spacing-sm)' }}>
                <button className="btn btn-secondary btn-sm" onClick={() => setSearch('')}>Clear search</button>
              </div>
            </div>
          )}
          {discovery && discovery.truncated && (
            <div className="notice" role="status">
              Results were limited: at most {discovery.limits.max_candidates} projects and{' '}
              {discovery.limits.max_visited_entries} directory entries are inspected. Narrow the
              layout or add deeper projects by relative path.
            </div>
          )}
          {discovery && discovery.skipped_unreadable > 0 && (
            <div className="notice" role="status">
              Some directories could not be inspected by the server.
            </div>
          )}

          <div className="project-list" role="list">
            {filteredCandidates.map((candidate) => {
              const registered = candidate.registered_project_id !== null
                ? projects.find((project) => project.id === candidate.registered_project_id) ?? null
                : null
              return (
                <div
                  key={candidate.relative_path}
                  role="listitem"
                  className="card card-interactive project-item"
                >
                  {registered ? (
                    <button
                      className="content-button"
                      onClick={() => onSelectProject(registered)}
                    >
                      <CandidateContent candidate={candidate} registered={registered} />
                    </button>
                  ) : (
                    <div className="content-static">
                      <CandidateContent candidate={candidate} registered={null} />
                    </div>
                  )}
                  <div className="dashboard-actions">
                    {registered && (
                      <button className="btn btn-secondary btn-sm" onClick={() => onSelectProject(registered)}>
                        Open
                      </button>
                    )}
                    {candidate.registered_project_id !== null && !registered && (
                      <button className="btn btn-secondary btn-sm" onClick={handleRefresh}>
                        Refresh to open
                      </button>
                    )}
                    {!registered && candidate.addable && (
                      <button
                        className="btn btn-primary btn-sm"
                        onClick={() => void handleAdd(candidate)}
                        disabled={addingPath !== null}
                      >
                        {addingPath === candidate.relative_path ? 'Adding…' : 'Add'}
                      </button>
                    )}
                  </div>
                </div>
              )
            })}
          </div>

          {addError && (
            <div className="error-message" role="alert">
              {addError}
            </div>
          )}

          {showCreateForm && (
            <ProjectCreateForm
              suggestions={suggestions}
              onSubmit={async (request) => {
                const created = await onCreate(request)
                setShowCreateForm(false)
                void loadDiscovery()
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
