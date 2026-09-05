import { useEffect, useState } from 'react'
import type { ProjectInfo } from './types'
import { ProjectPicker } from './components/ProjectPicker'
import { PlanPanel } from './components/PlanPanel'
import { RunDashboard } from './components/RunDashboard'
import * as api from './api'

type View = 'plans' | 'runs'

export function App() {
  const [authToken, setAuthTokenState] = useState('')
  const [isAuthenticated, setIsAuthenticated] = useState(false)
  const [selectedProject, setSelectedProject] = useState<ProjectInfo | null>(null)
  const [currentView, setCurrentView] = useState<View>('plans')
  const [runDashboardPlanPath, setRunDashboardPlanPath] = useState<string | null>(null)

  useEffect(() => {
    const token = api.getAuthToken()
    if (token) {
      setAuthTokenState(token)
      setIsAuthenticated(true)
    }
  }, [])

  function handleLogin() {
    if (!authToken.trim()) return
    api.setAuthToken(authToken)
    setIsAuthenticated(true)
  }

  function handleLogout() {
    api.clearAuthToken()
    setIsAuthenticated(false)
    setAuthTokenState('')
    setSelectedProject(null)
    setCurrentView('plans')
    setRunDashboardPlanPath(null)
  }

  function handleSelectProject(project: ProjectInfo) {
    setSelectedProject(project)
    setCurrentView('plans')
  }

  function handleOpenRunDashboard(planPath: string) {
    setRunDashboardPlanPath(planPath)
    setCurrentView('runs')
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
    <div style={{ minHeight: '100%', display: 'flex', flexDirection: 'column' }}>
      <header style={{ padding: 'var(--spacing-md)', borderBottom: '1px solid var(--color-border)', display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 'var(--spacing-md)' }}>
        <div style={{ display: 'flex', flexDirection: 'column', minWidth: 0 }}>
          <h1 style={{ fontSize: '1.25rem', fontWeight: 600 }}>aflow</h1>
          <div className="text-xs text-dim truncate">Registered projects, plans, configuration, and daemon-owned runs</div>
        </div>
        <button className="btn btn-secondary btn-sm" onClick={handleLogout}>Logout</button>
      </header>

      <main style={{ flex: 1, display: 'grid', gridTemplateColumns: selectedProject ? 'minmax(280px, 360px) minmax(0, 1fr)' : 'minmax(0, 1fr)', gap: 'var(--spacing-md)', padding: 'var(--spacing-md)', minHeight: 0, alignItems: 'stretch', overflow: 'hidden' }}>
        <aside style={{ minWidth: 0, minHeight: 0, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
          <ProjectPicker selectedProjectId={selectedProject?.id ?? null} onSelectProject={handleSelectProject} />
        </aside>
        <section style={{ minWidth: 0, minHeight: 0, display: 'flex', flexDirection: 'column', gap: 'var(--spacing-md)', overflow: 'hidden' }}>
          {selectedProject ? (
            <>
              <div className="card" style={{ display: 'flex', flexDirection: 'column', gap: 'var(--spacing-sm)' }}>
                <div style={{ fontWeight: 600 }}>{selectedProject.display_name}</div>
                <div className="text-xs text-dim mono">{selectedProject.current_path}</div>
                <div style={{ display: 'flex', gap: 'var(--spacing-sm)' }}>
                  <button className="btn btn-secondary btn-sm" onClick={() => setCurrentView('plans')}>Plans</button>
                  <button className="btn btn-secondary btn-sm" onClick={() => setCurrentView('runs')}>Runs</button>
                </div>
              </div>
              <div style={{ minHeight: 0, flex: 1 }}>
                {currentView === 'plans' && <PlanPanel project={selectedProject} onOpenRunDashboard={handleOpenRunDashboard} />}
                {currentView === 'runs' && (
                  <RunDashboard initialProjectRoot={selectedProject.current_path} initialPlanPath={runDashboardPlanPath}
                    onInitialPlanHandled={() => setRunDashboardPlanPath(null)} />
                )}
              </div>
            </>
          ) : (
            <div className="card" style={{ minHeight: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center', textAlign: 'center', padding: 'var(--spacing-lg)' }}>
              <div><strong>Select a registered project</strong><div className="text-sm text-dim">Plan and run tools will appear here.</div></div>
            </div>
          )}
        </section>
      </main>
    </div>
  )
}
