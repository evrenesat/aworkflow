import { useCallback, useEffect, useState } from 'react'
import type { ProjectConfig, ProjectCreateRequest, ProjectCreateResult, ProjectInfo } from './types'
import { readinessClass, readinessLabel } from './readiness'
import { ProjectPicker } from './components/ProjectPicker'
import { ProjectOverview } from './components/ProjectOverview'
import { ConfigEditor } from './components/ConfigEditor'
import { PlanPanel } from './components/PlanPanel'
import { RunDashboard, type PendingSuccessorStart } from './components/RunDashboard'
import * as api from './api'

type View = 'projects' | 'overview' | 'settings' | 'plans' | 'runs'

type ProjectView = 'overview' | 'settings' | 'plans' | 'runs'

const NAV_ITEMS: Array<{ view: View; label: string; needsProject: boolean }> = [
  { view: 'projects', label: 'Projects', needsProject: false },
  { view: 'overview', label: 'Overview', needsProject: true },
  { view: 'settings', label: 'Settings', needsProject: true },
  { view: 'plans', label: 'Plans', needsProject: true },
  { view: 'runs', label: 'Runs', needsProject: true },
]

const readinessGuidance: Record<string, string> = {
  configuration_required:
    'This project needs explicit configuration before workflows can start. '
    + 'Review both documents, then validate and save the pair.',
  blocked:
    'The registered root is not currently a usable Git project root. '
    + 'Fix the directory (a valid Git commit HEAD is required), then re-check the project.',
}

export function App() {
  const [authToken, setAuthTokenState] = useState('')
  const [isAuthenticated, setIsAuthenticated] = useState(false)
  const [projects, setProjects] = useState<ProjectInfo[]>([])
  const [projectsLoading, setProjectsLoading] = useState(false)
  const [projectsError, setProjectsError] = useState<string | null>(null)
  const [selectedProjectId, setSelectedProjectId] = useState<string | null>(null)
  const [view, setView] = useState<View>('projects')
  const [configDirty, setConfigDirty] = useState(false)
  const [planDirty, setPlanDirty] = useState(false)
  const [pendingAction, setPendingAction] = useState<{ description: string; run: () => void } | null>(null)
  const [runDashboardPlanPath, setRunDashboardPlanPath] = useState<string | null>(null)
  const [pendingSuccessorStart, setPendingSuccessorStart] = useState<PendingSuccessorStart | null>(null)

  useEffect(() => {
    const token = api.getAuthToken()
    if (token) {
      setAuthTokenState(token)
      setIsAuthenticated(true)
    }
  }, [])


  useEffect(() => {
    if (!pendingSuccessorStart) return
    const preventSilentExit = (event: BeforeUnloadEvent) => {
      event.preventDefault()
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', preventSilentExit)
    return () => window.removeEventListener('beforeunload', preventSilentExit)
  }, [pendingSuccessorStart])

  const loadProjects = useCallback(async () => {
    try {
      setProjectsLoading(true)
      setProjectsError(null)
      setProjects(await api.listProjects())
    } catch (err) {
      setProjectsError(err instanceof Error ? err.message : 'Failed to load registered projects')
    } finally {
      setProjectsLoading(false)
    }
  }, [])

  useEffect(() => {
    if (isAuthenticated) void loadProjects()
  }, [isAuthenticated, loadProjects])

  function handleLogin() {
    if (!authToken.trim()) return
    api.setAuthToken(authToken)
    setIsAuthenticated(true)
  }

  function handleLogout() {
    api.clearAuthToken()
    setIsAuthenticated(false)
    setAuthTokenState('')
    setProjects([])
    setProjectsError(null)
    setSelectedProjectId(null)
    setView('projects')
    setConfigDirty(false)
    setPlanDirty(false)
    setPendingAction(null)
    setRunDashboardPlanPath(null)
    setPendingSuccessorStart(null)
  }

  /**
   * Guards navigation away from unsaved editor text: the requested
   * action only runs after an explicit confirmation.
   */
  function requestGuarded(description: string, run: () => void) {
    if (configDirty || planDirty) setPendingAction({ description, run })
    else run()
  }

  function confirmPendingAction() {
    if (!pendingAction) return
    setConfigDirty(false)
    setPlanDirty(false)
    pendingAction.run()
    setPendingAction(null)
  }

  function switchView(next: View) {
    if (next === view) return
    if (NAV_ITEMS.find((item) => item.view === next)?.needsProject && !selectedProject) return
    requestGuarded(`leave the editor for ${next}`, () => setView(next))
  }

  function openProject(project: ProjectInfo) {
    requestGuarded(`open ${project.display_name} with unsaved edits`, () => {
      setSelectedProjectId(project.id)
      setRunDashboardPlanPath(null)
      setView('overview')
    })
  }

  async function handleCreateProject(request: ProjectCreateRequest): Promise<ProjectCreateResult> {
    const created = await api.createProject(request)
    const createdProject: ProjectInfo = {
      id: created.id,
      display_name: created.display_name,
      current_path: created.root,
      is_git_root: true,
      registered_at: created.created_at,
      readiness: created.readiness,
    }
    setProjects((current) => [...current.filter((project) => project.id !== created.id), createdProject])
    setSelectedProjectId(created.id)
    setView(created.readiness === 'ready' ? 'plans' : 'settings')
    try {
      const refreshed = await api.listProjects()
      const canonical = refreshed.find((project) => project.id === created.id)
      setProjects([...refreshed.filter((project) => project.id !== created.id), canonical ?? createdProject])
    } catch (err) {
      setProjectsError(
        `Project ${created.display_name} was created, but the project list could not refresh. ${
          err instanceof Error ? err.message : 'Retry the refresh to update the list.'
        }`,
      )
    }
    return created
  }

  async function handleUnregister(projectId: string) {
    await api.unregisterProject(projectId)
    if (selectedProjectId === projectId) {
      setSelectedProjectId(null)
      setView('projects')
    }
    await loadProjects()
  }

  const selectedProject = projects.find((project) => project.id === selectedProjectId) ?? null

  const handleConfigDirty = useCallback((dirty: boolean) => setConfigDirty(dirty), [])
  const handlePlanDirty = useCallback((dirty: boolean) => setPlanDirty(dirty), [])
  const handleConfigSaved = useCallback((saved: ProjectConfig) => {
    const readiness = saved.validation.state
    if (readiness === 'invalid') return
    setProjects((current) => current.map((project) => (
      project.id === saved.project_id ? { ...project, readiness } : project
    )))
  }, [])
  const handleConfigReady = useCallback((saved: ProjectConfig) => {
    if (saved.validation.state !== 'ready') return
    setProjects((current) => current.map((project) => (
      project.id === saved.project_id ? { ...project, readiness: 'ready' } : project
    )))
    setView('plans')
  }, [])

  function handleOpenRunDashboard(planPath: string) {
    requestGuarded('leave the plan editor for the run dashboard', () => {
      setRunDashboardPlanPath(planPath)
      setView('runs')
    })
  }

  if (!isAuthenticated) {
    return (
      <div style={{ height: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 'var(--spacing-lg)' }}>
        <div className="card" style={{ maxWidth: '400px', width: '100%' }}>
          <h1 style={{ fontSize: '1.5rem', fontWeight: 600, marginBottom: 'var(--spacing-lg)' }}>aflow Remote</h1>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--spacing-md)' }}>
            <input className="input" type="password" placeholder="Auth token" value={authToken}
              onChange={(event) => setAuthTokenState(event.target.value)}
              onKeyDown={(event) => event.key === 'Enter' && handleLogin()} />
            <button className="btn btn-primary" onClick={handleLogin} disabled={!authToken.trim()}>Login</button>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="app-shell">
      <header className="app-header">
        <div style={{ display: 'flex', flexDirection: 'column', minWidth: 0 }}>
          <h1 style={{ fontSize: '1.25rem', fontWeight: 600 }}>aflow</h1>
          <div className="text-xs text-dim truncate">Set up your code project, plan the work, and follow every run</div>
        </div>
        <button className="btn btn-secondary btn-sm" onClick={handleLogout}>Logout</button>
      </header>

      <nav className="workspace-nav" aria-label="Workspace views">
        {NAV_ITEMS.map((item) => (
          <span key={item.view} className="nav-item-group">
            {item.view === 'overview' && <span className="nav-separator" aria-hidden="true" />}
            <button
              className={`nav-tab ${view === item.view ? 'active' : ''}`}
              aria-current={view === item.view ? 'page' : undefined}
              disabled={item.needsProject && !selectedProject}
              title={item.needsProject && !selectedProject ? 'Open a project first' : undefined}
              onClick={() => switchView(item.view)}
            >
              {item.label}
            </button>
          </span>
        ))}
      </nav>

      {selectedProject && (
        <div className="project-context-bar" aria-label="Selected project">
          <div className="content-button-row" style={{ minWidth: 0 }}>
            <strong className="truncate">{selectedProject.display_name}</strong>
            <span className={readinessClass(selectedProject.readiness)}>
              {readinessLabel(selectedProject.readiness)}
            </span>
            <span className="text-xs text-dim mono truncate">{selectedProject.current_path}</span>
          </div>
          <button
            className="btn btn-secondary btn-sm"
            onClick={() => requestGuarded('return to the project list', () => setView('projects'))}
          >
            Change project
          </button>
          {readinessGuidance[selectedProject.readiness] && (
            <p className="text-xs readiness-note">
              {readinessGuidance[selectedProject.readiness]}
              {selectedProject.readiness === 'configuration_required' && view !== 'settings' && (
                <button className="btn btn-secondary btn-sm" onClick={() => switchView('settings')}>
                  Open settings
                </button>
              )}
            </p>
          )}
        </div>
      )}

      {pendingSuccessorStart && view !== 'runs' && (
        <div className="notice" role="status">
          A successor request for {pendingSuccessorStart.sourceRunId} is unresolved. Its exact request remains preserved.
          <button className="btn btn-secondary btn-sm" onClick={() => requestGuarded('return to the pending successor request', () => {
            setSelectedProjectId(pendingSuccessorStart.projectId)
            setView('runs')
          })}>Resolve pending successor</button>
        </div>
      )}

      {pendingAction && (
        <div className="card unsaved-guard" role="alertdialog" aria-label="Unsaved editor edits">
          <span className="text-sm">
            You have unsaved editor edits. Leave anyway to continue?
          </span>
          <div className="dashboard-actions">
            <button className="btn btn-danger btn-sm" onClick={confirmPendingAction}>Leave anyway</button>
            <button className="btn btn-secondary btn-sm" onClick={() => setPendingAction(null)}>Stay</button>
          </div>
        </div>
      )}

      <main className="workspace-main">
        {view === 'projects' && !selectedProject && (
          <p className="text-xs text-dim no-project-hint" role="note">
            Overview, Settings, Plans, and Runs become available after you open a project below.
          </p>
        )}

        {view === 'projects' && (
          <ProjectPicker
            projects={projects}
            selectedProjectId={selectedProjectId}
            loading={projectsLoading}
            error={projectsError}
            onSelectProject={openProject}
            onRefresh={() => void loadProjects()}
            onCreate={handleCreateProject}
            onUnregister={handleUnregister}
          />
        )}

        {view !== 'projects' && !selectedProject && (
          <div className="card choose-project-state">
            <h2 style={{ fontSize: '1.15rem', fontWeight: 600 }}>Choose a project first</h2>
            <p className="text-sm text-dim">
              Overview, Settings, Plans, and Runs belong to a single project. Open one from the
              project list to continue.
            </p>
            <div className="dashboard-actions">
              <button className="btn btn-primary btn-sm" onClick={() => setView('projects')}>
                Go to Projects
              </button>
            </div>
          </div>
        )}

        {view !== 'projects' && selectedProject && (
          <div className="workspace-content">
            {view === 'overview' && (
              <ProjectOverview
                project={selectedProject}
                onOpenView={(next: ProjectView | 'projects') => switchView(next)}
              />
            )}
            {view === 'settings' && (
              <ConfigEditor
                project={selectedProject}
                onDirtyChange={handleConfigDirty}
                onSaved={handleConfigSaved}
                onReady={handleConfigReady}
              />
            )}
            {view === 'plans' && (
              <PlanPanel
                project={selectedProject}
                onDirtyChange={handlePlanDirty}
                onOpenRunDashboard={handleOpenRunDashboard}
              />
            )}
            {view === 'runs' && (
              <RunDashboard
                initialProjectRoot={selectedProject.current_path}
                initialPlanPath={runDashboardPlanPath}
                onInitialPlanHandled={() => setRunDashboardPlanPath(null)}
                pendingSuccessorStart={pendingSuccessorStart}
                onPendingSuccessorStartChange={setPendingSuccessorStart}
              />
            )}
          </div>
        )}
      </main>
    </div>
  )
}
