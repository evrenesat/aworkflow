import { useCallback, useEffect, useId, useRef, useState } from 'react'
import type { ProjectConfig, ProjectCreateRequest, ProjectCreateResult, ProjectInfo } from './types'
import { markUserActivity } from './activity'
import { ProjectPicker } from './components/ProjectPicker'
import { GlobalSettings } from './components/GlobalSettings'
import { GlobalRunOverview } from './components/GlobalRunOverview'
import { PlanPanel } from './components/PlanPanel'
import { RunDashboard, type PendingSuccessorStart, type RunSelectionChange } from './components/RunDashboard'
import { HeaderSlotsProvider, type HeaderSlotContribution } from './components/HeaderSlots'
import { useCompactLayout } from './components/SidebarEditorLayout'
import { projectContextLabel } from './projectPresentation'
import * as api from './api'
import {
  normalizeWorkspaceQuery,
  parseWorkspaceQuery,
  sameWorkspaceQuery,
  workspaceHref,
  type WorkspaceQuery,
} from './urlState'

type View = WorkspaceQuery['view']

interface RunNavigationIntent {
  projectId: string
  runId: string
}

/** Returns only URL entries that were capable of explicitly opening a run. */
function runNavigationIntentFor(query: WorkspaceQuery): RunNavigationIntent | null {
  if (query.view !== 'runs' || query.project === null || query.run === null) return null
  return { projectId: query.project, runId: query.run }
}

/** Why the login gate is (or is not) shown. */
type AuthGate = 'checking' | 'signedOut' | 'restoreFailed' | 'expired' | 'signedIn'

const NAV_ITEMS: Array<{ view: View; label: string; needsProject: boolean }> = [
  { view: 'all-runs', label: 'All runs', needsProject: false },
  { view: 'projects', label: 'Projects', needsProject: false },
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

interface AppHeaderProps {
  selectedProject: ProjectInfo | null
  projects: ProjectInfo[]
  view: View
  slots: HeaderSlotContribution
  onSwitchView: (view: View) => void
  onLogout: () => void
  logoutPending: boolean
}

function AppHeader({ selectedProject, projects, view, slots, onSwitchView, onLogout, logoutPending }: AppHeaderProps) {
  const compact = useCompactLayout()
  const [menuOpen, setMenuOpen] = useState(false)
  const menuButtonRef = useRef<HTMLButtonElement>(null)
  const menuId = useId()
  const pageLabel = NAV_ITEMS.find(item => item.view === view)?.label ?? 'Workspace'
  const defaultContext = <span className="header-context-title">{pageLabel}</span>
  const projectTitle = selectedProject
    ? `AFlow · ${projectContextLabel(selectedProject, projects)}`
    : 'AFlow'

  useEffect(() => {
    if (!compact) setMenuOpen(false)
  }, [compact])

  useEffect(() => {
    if (!menuOpen) return
    const handleEscape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      event.preventDefault()
      setMenuOpen(false)
      menuButtonRef.current?.focus()
    }
    document.addEventListener('keydown', handleEscape)
    return () => document.removeEventListener('keydown', handleEscape)
  }, [menuOpen])

  function closeMenu(returnFocus = false) {
    setMenuOpen(false)
    if (returnFocus) menuButtonRef.current?.focus()
  }

  function navigate(next: View) {
    closeMenu(true)
    onSwitchView(next)
  }

  function navigationButton(item: typeof NAV_ITEMS[number], menuItem = false) {
    const disabled = item.needsProject && !selectedProject
    return <button
      key={item.view}
      className={`nav-tab ${view === item.view ? 'active' : ''}`}
      role={menuItem ? 'menuitem' : undefined}
      aria-current={view === item.view ? 'page' : undefined}
      disabled={disabled}
      title={disabled ? 'Open a project first' : undefined}
      onClick={() => navigate(item.view)}
    >
      {item.label}
    </button>
  }

  return <>
    <header className="app-header">
      <div className="app-header-row app-header-row-one">
        {compact ? <>
          <button
            ref={menuButtonRef}
            type="button"
            className="btn btn-secondary app-menu-trigger"
            aria-label="Menu"
            aria-haspopup="menu"
            aria-expanded={menuOpen}
            aria-controls={menuId}
            onClick={() => setMenuOpen(open => !open)}
          ><span aria-hidden="true">☰</span></button>
          <div className="app-branding app-branding-compact">
            <h1 className="app-brand-title truncate" title={projectTitle}>{projectTitle}</h1>
            <div className="mobile-page-context">{slots.compactContext ?? slots.context ?? defaultContext}</div>
          </div>
        </> : <>
          <div className="app-branding">
            <h1 className="app-brand-title truncate" title={projectTitle}>{projectTitle}</h1>
        </div>
        <nav className="workspace-global-nav" aria-label="Global navigation">
            {NAV_ITEMS.filter(item => !item.needsProject || selectedProject).map(item => navigationButton(item))}
          </nav>
          <button className="btn btn-secondary app-account-action" onClick={onLogout} disabled={logoutPending}>
            {logoutPending ? 'Signing out…' : 'Logout'}
          </button>
        </>}
      </div>

      {compact && menuOpen && <nav id={menuId} className="app-mobile-menu" role="menu" aria-label="Workspace navigation">
        {NAV_ITEMS.map(item => navigationButton(item, true))}
        <div className="app-mobile-menu-separator" />
        <button role="menuitem" className="nav-tab app-mobile-logout" onClick={() => { closeMenu(true); onLogout() }} disabled={logoutPending}>
          {logoutPending ? 'Signing out…' : 'Logout'}
        </button>
      </nav>}
    </header>
    <div className="app-header-row app-header-row-two">
      {!compact && <div className="header-slot-context">{slots.context ?? defaultContext}</div>}
      {slots.local && <div className="header-slot-local">{slots.local}</div>}
      <div className="header-slot-spacer" />
      {slots.primary && <div className="header-slot-primary">{slots.primary}</div>}
      {slots.more && <div className="header-slot-more">{slots.more}</div>}
    </div>
  </>
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
  const [runNavigationIntent, setRunNavigationIntent] = useState<RunNavigationIntent | null>(() => (
    runNavigationIntentFor(initialWorkspaceQuery())
  ))
  const [visitedProjects, setVisitedProjects] = useState<string[]>([])
  useEffect(() => {
    if (query.project) setVisitedProjects(ids => ids.includes(query.project!) ? ids : [...ids, query.project!])
  }, [query.project])
  const [staleLinkTarget, setStaleLinkTarget] = useState<string | null>(null)
  const [configDirty, setConfigDirty] = useState(false)
  const [planDirty, setPlanDirty] = useState(false)
  const [pendingAction, setPendingAction] = useState<{ description: string; run: () => void; onCancel?: () => void } | null>(null)
  const [runDashboardPlanPath, setRunDashboardPlanPath] = useState<string | null>(null)
  const [pendingSuccessorStart, setPendingSuccessorStart] = useState<PendingSuccessorStart | null>(null)
  const alertRef = useRef<HTMLElement | null>(null)
  const setAlertRef = useCallback((element: HTMLElement | null) => {
    alertRef.current = element
  }, [])
  // Bumped on logout so late responses cannot restore signed-in UI.
  const authEpoch = useRef(0)

  const queryRef = useRef(query)
  queryRef.current = query
  const dirtyRef = useRef(false)
  dirtyRef.current = configDirty || planDirty

  useEffect(() => {
    if (!loginError && !logoutError && !pendingAction && !pendingSuccessorStart) return
    const alert = alertRef.current
    if (!alert) return
    try {
      alert.focus({ preventScroll: true })
    } catch {
      alert.focus()
    }
    if (typeof alert.scrollIntoView === 'function') {
      alert.scrollIntoView({ block: 'nearest', inline: 'nearest' })
    }
  }, [loginError, logoutError, pendingAction, pendingSuccessorStart])

  // A passive selected-run URL replacement must not leave an old explicit
  // entry marker armed for a later visit to the same run.
  useEffect(() => {
    if (!runNavigationIntent) return
    if (query.view === 'runs' && query.project === runNavigationIntent.projectId && query.run === runNavigationIntent.runId) return
    setRunNavigationIntent(null)
  }, [query, runNavigationIntent])

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
      const apply = () => {
        setRunNavigationIntent(runNavigationIntentFor(target))
        applyQuery(target, 'replace')
      }
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
  // or unknown view normalizes to Runs; run ids are validated inside the
  // Runs dashboard through the project-scoped direct run endpoint.
  useEffect(() => {
    if (authGate !== 'signedIn' || projectsLoading || projectsError) return
    if (query.project === null) {
      applyQuery({ project: null, view: ['settings', 'projects', 'all-runs'].includes(query.view) ? query.view : 'all-runs', run: null }, 'replace')
      return
    }
    if (!projects.some((project) => project.id === query.project)) {
      setStaleLinkTarget(query.project)
      applyQuery({ project: null, view: 'projects', run: null }, 'replace')
      return
    }
    setStaleLinkTarget(null)
    // Rewrite the address to the concrete workspace state (a missing or
    // unknown view becomes Runs; stray parameters are dropped).
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
    setVisitedProjects([])
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
    requestGuarded(`leave the editor for ${next}`, () => applyQuery({ ...queryRef.current, ...(next === 'all-runs' || next === 'projects' ? { project: null } : {}), view: next, run: null }, 'push'))
  }

  function openProject(project: ProjectInfo) {
    requestGuarded(`open ${project.display_name} with unsaved edits`, () => {
      setRunDashboardPlanPath(null)
      applyQuery({ project: project.id, view: 'runs', run: null }, 'push')
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

  function handleOpenRunDashboard(planPath: string) {
    requestGuarded('leave the plan editor for the run dashboard', () => {
      setRunDashboardPlanPath(planPath)
      applyQuery({ ...queryRef.current, view: 'new-run', run: null }, 'push')
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

  /** Opens a run from outside its mounted history list as an explicit detail entry. */
  const openExplicitRun = useCallback((project: string, run: string) => {
    setRunNavigationIntent({ projectId: project, runId: run })
    applyQuery({ project, view: 'runs', run }, 'push')
  }, [applyQuery])

  if (authGate !== 'signedIn') {
    return (
      <div className="auth-gate">
        <div className="card" style={{ maxWidth: '420px', width: '100%' }}>
          <h1 style={{ fontSize: '1.5rem', fontWeight: 600, marginBottom: 'var(--spacing-lg)' }}>aflow Remote</h1>
          {authGate === 'checking' && (
            <p className="text-sm text-dim" role="status">Checking your saved session…</p>
          )}
          {authGate === 'restoreFailed' && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--spacing-md)' }}>
              <p ref={setAlertRef} className="text-sm" role="alert" tabIndex={-1}>
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
              {loginError && <p ref={setAlertRef} className="text-sm" role="alert" tabIndex={-1}>{loginError}</p>}
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
      <HeaderSlotsProvider renderHeader={(slots) => <AppHeader
        selectedProject={selectedProject}
        projects={projects}
        view={view}
        slots={slots}
        onSwitchView={switchView}
        onLogout={() => void handleLogout()}
        logoutPending={logoutPending}
      />}>
        {logoutError && (
          <div ref={setAlertRef} className="notice app-notice" role="alert" tabIndex={-1}>
            <span className="text-sm">{logoutError}</span>
            <button className="btn btn-secondary btn-sm" onClick={() => void handleLogout()}>Retry logout</button>
          </div>
        )}

        {pendingSuccessorStart && ((view !== 'runs' && view !== 'new-run') || query.project !== pendingSuccessorStart.projectId) && (
          <div ref={setAlertRef} className="notice app-notice" role="status" tabIndex={-1}>
            A successor request for {pendingSuccessorStart.sourceRunId} is unresolved. Its exact request remains preserved.
            <button className="btn btn-secondary btn-sm" onClick={() => requestGuarded('return to the pending successor request', () => {
              applyQuery({ project: pendingSuccessorStart.projectId, view: 'new-run', run: null }, 'push')
            })}>Resolve pending successor</button>
          </div>
        )}

        {pendingAction && (
          <div ref={setAlertRef} className="card unsaved-guard" role="alertdialog" aria-label="Unsaved editor edits" tabIndex={-1}>
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
        {view === 'all-runs' && <GlobalRunOverview projects={projects} registryLoading={projectsLoading} registryError={projectsError} onOpen={openExplicitRun} />}
        {view === 'settings' && (
            <GlobalSettings
              onDirtyChange={handleConfigDirty}
              onSaved={handleConfigSaved}
            />
        )}
        {selectedProject && readinessGuidance[selectedProject.readiness] && view === 'settings' && (
          <p className="notice" role="note">{readinessGuidance[selectedProject.readiness]}</p>
        )}
        {staleLinkTarget && view === 'projects' && (
          <div className="notice" role="alert">
            The link pointed to project <span className="mono">{staleLinkTarget}</span>, which is not in the
            registered project list. Its scoped link was cleared — open or add the project below.
          </div>
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

        {view !== 'all-runs' && view !== 'projects' && view !== 'settings' && !selectedProject && !(query.project !== null && projectsLoading) && (
          <div className="card choose-project-state">
            <h2 style={{ fontSize: '1.15rem', fontWeight: 600 }}>Choose a project first</h2>
            <p className="text-sm text-dim">
              Plans and Runs belong to a single project. Open one from the
              project list to continue.
            </p>
            <div className="dashboard-actions">
              <button className="btn btn-primary btn-sm" onClick={() => applyQuery({ project: null, view: 'projects', run: null }, 'push')}>
                Go to Projects
              </button>
            </div>
          </div>
        )}

        {view === 'plans' && selectedProject && (
          <div className="workspace-content">
            {selectedProject.readiness !== 'ready' && readinessGuidance[selectedProject.readiness] && (
              <p className="notice" role="note">
                {readinessGuidance[selectedProject.readiness]}
                {selectedProject.readiness === 'configuration_required' && (
                  <span className="dashboard-actions" style={{ justifyContent: 'flex-start' }}>
                    <button className="btn btn-secondary btn-sm" onClick={() => switchView('settings')}>
                      Open settings
                    </button>
                  </span>
                )}
              </p>
            )}
            <PlanPanel
              project={selectedProject}
              onDirtyChange={handlePlanDirty}
              onOpenRunDashboard={handleOpenRunDashboard}
            />
          </div>
        )}
        {selectedProject && readinessGuidance[selectedProject.readiness] && (view === 'runs' || view === 'new-run') && (
          <p className="notice" role="note">
            {readinessGuidance[selectedProject.readiness]}
            {selectedProject.readiness === 'configuration_required' && (
              <span className="dashboard-actions" style={{ justifyContent: 'flex-start' }}>
                <button className="btn btn-secondary btn-sm" onClick={() => switchView('settings')}>
                  Open settings
                </button>
              </span>
            )}
          </p>
        )}
        {visitedProjects.filter(id => projects.some(p => p.id === id)).map(id => (
          <div className="dashboard-host" key={id} hidden={query.project !== id || (view !== 'runs' && view !== 'new-run')}>
            <RunDashboard
              projectId={id}
              visible={query.project === id && (view === 'runs' || view === 'new-run')}
              page={query.project === id && view === 'new-run' ? 'new-run' : 'runs'}
              requestedRunId={query.project === id ? query.run : null}
              explicitRunNavigation={query.project === id
                && runNavigationIntent?.projectId === id
                && runNavigationIntent.runId === query.run}
              onRunSelectionChange={change => { if (queryRef.current.project === id) handleRunSelectionChange(change) }}
              initialPlanPath={query.project === id ? runDashboardPlanPath : null}
              onInitialPlanHandled={() => setRunDashboardPlanPath(null)}
              pendingSuccessorStart={pendingSuccessorStart}
              onPendingSuccessorStartChange={setPendingSuccessorStart}
              onOpenSettings={() => switchView('settings')}
              onNewRun={() => applyQuery({ project: id, view: 'new-run', run: null }, 'push')}
              onCancelNewRun={() => applyQuery({ project: id, view: 'runs', run: null }, 'push')}
              onRunStarted={runId => {
                if (queryRef.current.project === id && ['runs', 'new-run'].includes(queryRef.current.view)) openExplicitRun(id, runId)
              }}
            />
          </div>
        ))}
        </main>
      </HeaderSlotsProvider>
    </div>
  )
}
