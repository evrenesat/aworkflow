import { useCallback, useEffect, useRef, useState } from 'react'
import type { ProjectConfig, ProjectCreateRequest, ProjectCreateResult, ProjectInfo } from './types'
import { readinessClass, readinessLabel } from './readiness'
import { markUserActivity } from './activity'
import { ProjectPicker } from './components/ProjectPicker'
import { ProjectOverview } from './components/ProjectOverview'
import { ConfigEditor } from './components/ConfigEditor'
import { SettingsPanel } from './components/SettingsPanel'
import { PlanPanel } from './components/PlanPanel'
import { RunDashboard, type PendingSuccessorStart, type RunSelectionChange } from './components/RunDashboard'
import * as api from './api'
import {
  normalizeWorkspaceQuery,
  parseWorkspaceQuery,
  sameWorkspaceQuery,
  workspaceHref,
  type WorkspaceQuery,
} from './urlState'

type View = WorkspaceQuery['view']

type ProjectView = 'overview' | 'settings' | 'plans' | 'runs'

/** Why the login gate is (or is not) shown. */
type AuthGate = 'checking' | 'signedOut' | 'restoreFailed' | 'expired' | 'signedIn'

const NAV_ITEMS: Array<{ view: View; label: string; needsProject: boolean }> = [
  { view: 'projects', label: 'Projects', needsProject: false },
  { view: 'overview', label: 'Overview', needsProject: true },
  { view: 'settings', label: 'Settings', needsProject: false },
  { view: 'plans', label: 'Plans', needsProject: true },
  { view: 'runs', label: 'Runs', needsProject: true },
]

const readinessGuidance: Record<string, string> = {
  configuration_required:
    'The shared AFlow configuration needs explicit settings before workflows can start. '
    + 'Open Settings, review both documents, then validate and save the pair.',
  blocked:
    'The registered root is not currently a usable Git project root. '
    + 'Fix the directory (a valid Git commit HEAD is required), then re-check the project.',
}

function initialWorkspaceQuery(): WorkspaceQuery {
  return normalizeWorkspaceQuery(parseWorkspaceQuery(window.location.search))
}

export function App() {
  const [authGate, setAuthGate] = useState<AuthGate>('checking')
  const [restoreAttempt, setRestoreAttempt] = useState(0)
  const [loginDraft, setLoginDraft] = useState('')
  const [loginPending, setLoginPending] = useState(false)
  const [loginError, setLoginError] = useState<string | null>(null)
  const [logoutPending, setLogoutPending] = useState(false)
  const [logoutError, setLogoutError] = useState<string | null>(null)
  const [projects, setProjects] = useState<ProjectInfo[]>([])
  const [projectsLoading, setProjectsLoading] = useState(true)
  const [projectsError, setProjectsError] = useState<string | null>(null)
  const [query, setQuery] = useState<WorkspaceQuery>(initialWorkspaceQuery)
  const [staleLinkTarget, setStaleLinkTarget] = useState<string | null>(null)
  const [configDirty, setConfigDirty] = useState(false)
  const [planDirty, setPlanDirty] = useState(false)
  const [pendingAction, setPendingAction] = useState<{ description: string; run: () => void; onCancel?: () => void } | null>(null)
  const [runDashboardPlanPath, setRunDashboardPlanPath] = useState<string | null>(null)
  const [pendingSuccessorStart, setPendingSuccessorStart] = useState<PendingSuccessorStart | null>(null)
  // Bumped on logout so late responses cannot restore signed-in UI.
  const authEpoch = useRef(0)

  const queryRef = useRef(query)
  queryRef.current = query
  const dirtyRef = useRef(false)
  dirtyRef.current = configDirty || planDirty

  // On load, ask the server whether the browser session cookie is still
  // valid. Only a definitive 401 shows Login; a network/server failure
  // offers Retry instead of a false signed-out message.
  useEffect(() => {
    let cancelled = false
    if (document.visibilityState === 'visible') markUserActivity()
    api.checkSession()
      .then(() => { if (!cancelled) setAuthGate('signedIn') })
      .catch((err: unknown) => {
        if (cancelled) return
        if (err instanceof api.ApiError && err.status === 401) setAuthGate('signedOut')
        else setAuthGate('restoreFailed')
      })
    return () => { cancelled = true }
  }, [restoreAttempt])

  // A 401 on any ordinary API or stream request means the session expired
  // (or the token was rotated); the workspace state is preserved so signing
  // in again returns to the same project and view.
  useEffect(() => {
    api.setSessionExpiredHandler(() => {
      setAuthGate((gate) => (gate === 'signedIn' ? 'expired' : gate))
    })
    return () => api.setSessionExpiredHandler(null)
  }, [])

  // Track visible page entry and real visible pointer/keyboard/focus
  // activity; the marker lives only in memory and is consumed by the next
  // authenticated REST request (see activity.ts).
  useEffect(() => {
    if (authGate !== 'signedIn') return
    const markVisibleActivity = () => {
      if (document.visibilityState === 'visible') markUserActivity()
    }
    markVisibleActivity()
    window.addEventListener('pointerdown', markVisibleActivity)
    window.addEventListener('keydown', markVisibleActivity)
    window.addEventListener('focus', markVisibleActivity)
    document.addEventListener('visibilitychange', markVisibleActivity)
    return () => {
      window.removeEventListener('pointerdown', markVisibleActivity)
      window.removeEventListener('keydown', markVisibleActivity)
      window.removeEventListener('focus', markVisibleActivity)
      document.removeEventListener('visibilitychange', markVisibleActivity)
    }
  }, [authGate])


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
    const epoch = authEpoch.current
    try {
      setProjectsLoading(true)
      setProjectsError(null)
      const loaded = await api.listProjects()
      if (authEpoch.current !== epoch) return
      setProjects(loaded)
    } catch (err) {
      if (authEpoch.current !== epoch) return
      setProjectsError(err instanceof Error ? err.message : 'Failed to load registered projects')
    } finally {
      if (authEpoch.current === epoch) setProjectsLoading(false)
    }
  }, [])

  useEffect(() => {
    if (authGate === 'signedIn') void loadProjects()
  }, [authGate, loadProjects])

  /**
   * Applies the next workspace query to state and the browser history entry.
   * User navigation pushes a history entry; validation fixes and passive
   * selection/status syncs replace, so refreshes never flood the stack.
   */
  const applyQuery = useCallback((next: WorkspaceQuery, mode: 'push' | 'replace') => {
    const href = workspaceHref(next)
    const stateMatches = sameWorkspaceQuery(queryRef.current, next)
    const urlMatches = window.location.search === href && window.location.hash === ''
    if (stateMatches && urlMatches) return
    if (!urlMatches) {
      const url = `${window.location.pathname}${href}`
      if (mode === 'push') window.history.pushState(null, '', url)
      else window.history.replaceState(null, '', url)
    }
    if (!stateMatches) setQuery(next)
  }, [])

  // Browser back/forward is navigation like any other: it passes the same
  // unsaved-edits guard, and cancelling restores the prior visible URL.
  useEffect(() => {
    const onPopState = () => {
      const target = normalizeWorkspaceQuery(parseWorkspaceQuery(window.location.search))
      const current = queryRef.current
      if (sameWorkspaceQuery(target, current)) return
      const apply = () => applyQuery(target, 'replace')
      if (dirtyRef.current) {
        setPendingAction({
          description: 'follow the browser navigation',
          run: apply,
          onCancel: () => {
            window.history.pushState(null, '', `${window.location.pathname}${workspaceHref(current)}`)
          },
        })
      } else {
        apply()
      }
    }
    window.addEventListener('popstate', onPopState)
    return () => window.removeEventListener('popstate', onPopState)
  }, [applyQuery])

  // After the registry fetch the public identifiers are validated: the
  // registered project id is the sole project authority. An unknown project
  // clears the scoped link with guidance instead of a substitute; a missing
  // or unknown view normalizes to Overview; run ids are validated inside the
  // Runs dashboard through the project-scoped direct run endpoint.
  useEffect(() => {
    if (authGate !== 'signedIn' || projectsLoading || projectsError) return
    if (query.project === null) {
      applyQuery({ project: null, view: 'projects', run: null }, 'replace')
      return
    }
    if (!projects.some((project) => project.id === query.project)) {
      setStaleLinkTarget(query.project)
      applyQuery({ project: null, view: 'projects', run: null }, 'replace')
      return
    }
    setStaleLinkTarget(null)
    // Rewrite the address to the concrete workspace state (a missing or
    // unknown view becomes overview; stray parameters are dropped).
    applyQuery(query, 'replace')
  }, [applyQuery, authGate, projects, projectsError, projectsLoading, query])

  async function handleLogin() {
    const token = loginDraft.trim()
    if (!token || loginPending) return
    setLoginPending(true)
    setLoginError(null)
    try {
      await api.loginSession(token)
      api.clearAuthToken()
      // The token is never kept; the HttpOnly session cookie carries auth.
      setLoginDraft('')
      setAuthGate('signedIn')
    } catch (err) {
      if (err instanceof api.ApiError && err.status === 401) {
        setLoginError('Login failed: the server did not accept that token. Check it and try again.')
      } else {
        setLoginError('Login could not reach the server. Your entered token is preserved; retry once the connection recovers.')
      }
    } finally {
      setLoginPending(false)
    }
  }

  function resetWorkspace() {
    setProjects([])
    setProjectsError(null)
    setStaleLinkTarget(null)
    applyQuery({ project: null, view: 'projects', run: null }, 'replace')
    setConfigDirty(false)
    setPlanDirty(false)
    setPendingAction(null)
    setRunDashboardPlanPath(null)
    setPendingSuccessorStart(null)
  }

  async function handleLogout() {
    if (logoutPending) return
    setLogoutPending(true)
    setLogoutError(null)
    const epoch = authEpoch.current
    try {
      await api.logoutSession()
      ++authEpoch.current
      api.clearAuthToken()
      resetWorkspace()
      setLoginDraft('')
      setLoginError(null)
      setAuthGate('signedOut')
    } catch (err) {
      if (authEpoch.current !== epoch) return
      setLogoutError(
        `Logout could not be confirmed by the server. ${
          err instanceof Error ? err.message : 'Check the connection and try again.'
        }`,
      )
    } finally {
      setLogoutPending(false)
    }
  }

  /**
   * Guards navigation away from unsaved editor text: the requested
   * action only runs after an explicit confirmation.
   */
  function requestGuarded(description: string, run: () => void, onCancel?: () => void) {
    if (configDirty || planDirty) setPendingAction({ description, run, onCancel })
    else run()
  }

  function confirmPendingAction() {
    if (!pendingAction) return
    setConfigDirty(false)
    setPlanDirty(false)
    pendingAction.run()
    setPendingAction(null)
  }

  /** Stays in the current view; restores the URL the visible state came from. */
  function cancelPendingAction() {
    if (!pendingAction) return
    const onCancel = pendingAction.onCancel
    setPendingAction(null)
    onCancel?.()
  }

  function switchView(next: View) {
    if (next === query.view) return
    if (NAV_ITEMS.find((item) => item.view === next)?.needsProject && !selectedProject) return
    requestGuarded(`leave the editor for ${next}`, () => applyQuery({ ...queryRef.current, view: next, run: null }, 'push'))
  }

  function openProject(project: ProjectInfo) {
    requestGuarded(`open ${project.display_name} with unsaved edits`, () => {
      setRunDashboardPlanPath(null)
      applyQuery({ project: project.id, view: 'overview', run: null }, 'push')
    })
  }

  async function handleCreateProject(request: ProjectCreateRequest): Promise<ProjectCreateResult> {
    const epoch = authEpoch.current
    const created = await api.createProject(request)
    if (authEpoch.current !== epoch) throw new Error('Signed out before the project list could refresh')
    const createdProject: ProjectInfo = {
      id: created.id,
      display_name: created.display_name,
      current_path: created.root,
      is_git_root: true,
      registered_at: created.created_at,
      readiness: created.readiness,
    }
    setProjects((current) => [...current.filter((project) => project.id !== created.id), createdProject])
    if (authEpoch.current !== epoch) return created
    applyQuery({ project: created.id, view: created.readiness === 'ready' ? 'plans' : 'settings', run: null }, 'push')
    try {
      const refreshed = await api.listProjects()
      if (authEpoch.current !== epoch) return created
      const canonical = refreshed.find((project) => project.id === created.id)
      setProjects([...refreshed.filter((project) => project.id !== created.id), canonical ?? createdProject])
    } catch (err) {
      if (authEpoch.current !== epoch) return created
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
    if (queryRef.current.project === projectId) {
      applyQuery({ project: null, view: 'projects', run: null }, 'replace')
    }
    await loadProjects()
  }

  const selectedProject = projects.find((project) => project.id === query.project) ?? null
  const view = query.view

  const handleConfigDirty = useCallback((dirty: boolean) => setConfigDirty(dirty), [])
  const handlePlanDirty = useCallback((dirty: boolean) => setPlanDirty(dirty), [])
  const handleConfigSaved = useCallback((saved: ProjectConfig) => {
    const readiness = saved.validation.state
    if (readiness === 'invalid') return
    // The saved configuration is global: every registered project's
    // readiness follows the same shared pair.
    setProjects((current) => current.map((project) => (
      project.readiness === 'blocked' ? project : { ...project, readiness }
    )))
  }, [])
  const handleConfigReady = useCallback((saved: ProjectConfig) => {
    if (saved.validation.state !== 'ready') return
    setProjects((current) => current.map((project) => (
      project.readiness === 'blocked' ? project : { ...project, readiness: 'ready' }
    )))
    applyQuery({ ...queryRef.current, view: 'plans', run: null }, 'push')
  }, [applyQuery])

  function handleOpenRunDashboard(planPath: string) {
    requestGuarded('leave the plan editor for the run dashboard', () => {
      setRunDashboardPlanPath(planPath)
      applyQuery({ ...queryRef.current, view: 'runs', run: null }, 'push')
    })
  }

  /** Run selection reports from the dashboard become passive replaces or user pushes. */
  const handleRunSelectionChange = useCallback((change: RunSelectionChange) => {
    const current = queryRef.current
    if (current.view !== 'runs') return
    if (change.userInitiated) applyQuery({ ...current, run: change.runId }, 'push')
    else if (change.missingRunId) applyQuery({ ...current, run: null }, 'replace')
    else applyQuery({ ...current, run: change.runId }, 'replace')
  }, [applyQuery])

  if (authGate !== 'signedIn') {
    return (
      <div style={{ height: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 'var(--spacing-lg)' }}>
        <div className="card" style={{ maxWidth: '420px', width: '100%' }}>
          <h1 style={{ fontSize: '1.5rem', fontWeight: 600, marginBottom: 'var(--spacing-lg)' }}>aflow Remote</h1>
          {authGate === 'checking' && (
            <p className="text-sm text-dim" role="status">Checking your saved session…</p>
          )}
          {authGate === 'restoreFailed' && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--spacing-md)' }}>
              <p className="text-sm" role="alert">
                The server could not be reached to check your session. This is a connection problem, not a signed-out state.
              </p>
              <button
                className="btn btn-primary"
                onClick={() => { setAuthGate('checking'); setRestoreAttempt((attempt) => attempt + 1) }}
              >
                Retry
              </button>
            </div>
          )}
          {(authGate === 'signedOut' || authGate === 'expired') && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--spacing-md)' }}>
              {authGate === 'expired' && (
                <p className="text-sm" role="alert">
                  Your session ended (30 days without dashboard activity, or the server token was rotated).
                  Sign in again to return to your current project and view.
                </p>
              )}
              {loginError && <p className="text-sm" role="alert">{loginError}</p>}
              <input className="input" type="password" placeholder="Auth token" value={loginDraft}
                onChange={(event) => setLoginDraft(event.target.value)}
                onKeyDown={(event) => event.key === 'Enter' && void handleLogin()} />
              <button className="btn btn-primary" onClick={() => void handleLogin()}
                disabled={!loginDraft.trim() || loginPending}>
                {loginPending ? 'Signing in…' : 'Login'}
              </button>
              <div className="text-xs text-dim" style={{ display: 'flex', flexDirection: 'column', gap: 'var(--spacing-xs)' }}>
                <span>Stay signed in for 30 days of inactivity; your dashboard activity renews that window.</span>
                <span>On a shared browser, use Logout when you finish.</span>
                <span>
                  The token is the deployment bearer from the server's configured token file or <code>AFLOW_APP_TOKEN</code>;
                  ask your operator if you no longer have it. It is sent only at login and never stored in the browser.
                </span>
              </div>
            </div>
          )}
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
        <button className="btn btn-secondary btn-sm" onClick={() => void handleLogout()} disabled={logoutPending}>
          {logoutPending ? 'Signing out…' : 'Logout'}
        </button>
      </header>

      {logoutError && (
        <div className="notice" role="alert" style={{ display: 'flex', gap: 'var(--spacing-md)', alignItems: 'center' }}>
          <span className="text-sm">{logoutError}</span>
          <button className="btn btn-secondary btn-sm" onClick={() => void handleLogout()}>Retry logout</button>
        </div>
      )}

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
            onClick={() => requestGuarded('return to the project list', () => applyQuery({ ...queryRef.current, project: null, view: 'projects', run: null }, 'push'))}
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

      {pendingSuccessorStart && (view !== 'runs' || query.project !== pendingSuccessorStart.projectId) && (
        <div className="notice" role="status">
          A successor request for {pendingSuccessorStart.sourceRunId} is unresolved. Its exact request remains preserved.
          <button className="btn btn-secondary btn-sm" onClick={() => requestGuarded('return to the pending successor request', () => {
            applyQuery({ project: pendingSuccessorStart.projectId, view: 'runs', run: null }, 'push')
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
            <button className="btn btn-secondary btn-sm" onClick={cancelPendingAction}>Stay</button>
          </div>
        </div>
      )}

      <main className="workspace-main">
        {view === 'settings' && (
          <div className="workspace-content">
            <ConfigEditor
              onDirtyChange={handleConfigDirty}
              onSaved={handleConfigSaved}
              onReady={handleConfigReady}
            />
            <SettingsPanel onDirtyChange={handleConfigDirty} />
          </div>
        )}
        {staleLinkTarget && view === 'projects' && (
          <div className="notice" role="alert">
            The link pointed to project <span className="mono">{staleLinkTarget}</span>, which is not in the
            registered project list. Its scoped link was cleared — open or add the project below.
          </div>
        )}

        {view === 'projects' && !selectedProject && (
          <p className="text-xs text-dim no-project-hint" role="note">
            Overview, Plans, and Runs become available after you open a project below.
            Settings is shared by every project.
          </p>
        )}

        {view === 'projects' && (
          <ProjectPicker
            projects={projects}
            selectedProjectId={query.project}
            loading={projectsLoading}
            error={projectsError}
            onSelectProject={openProject}
            onRefresh={loadProjects}
            onCreate={handleCreateProject}
            onUnregister={handleUnregister}
          />
        )}

        {view !== 'projects' && !selectedProject && query.project !== null && projectsLoading && (
          <div className="card choose-project-state">
            <div className="spinner" />
            <p className="text-sm text-dim">Opening the linked project…</p>
          </div>
        )}

        {view !== 'projects' && !selectedProject && !(query.project !== null && projectsLoading) && (
          <div className="card choose-project-state">
            <h2 style={{ fontSize: '1.15rem', fontWeight: 600 }}>Choose a project first</h2>
            <p className="text-sm text-dim">
              Overview, Plans, and Runs belong to a single project. Open one from the
              project list to continue.
            </p>
            <div className="dashboard-actions">
              <button className="btn btn-primary btn-sm" onClick={() => applyQuery({ project: null, view: 'projects', run: null }, 'push')}>
                Go to Projects
              </button>
            </div>
          </div>
        )}

        {view !== 'projects' && view !== 'settings' && selectedProject && (
          <div className="workspace-content">
            {view === 'overview' && (
              <ProjectOverview
                project={selectedProject}
                onOpenView={(next: ProjectView | 'projects') => switchView(next)}
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
                key={selectedProject.id}
                projectId={selectedProject.id}
                requestedRunId={query.run}
                onRunSelectionChange={handleRunSelectionChange}
                initialPlanPath={runDashboardPlanPath}
                onInitialPlanHandled={() => setRunDashboardPlanPath(null)}
                pendingSuccessorStart={pendingSuccessorStart}
                onPendingSuccessorStartChange={setPendingSuccessorStart}
                onOpenSettings={() => switchView('settings')}
              />
            )}
          </div>
        )}
      </main>
    </div>
  )
}
