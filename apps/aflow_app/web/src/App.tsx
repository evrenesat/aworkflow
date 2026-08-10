import { useEffect, useState } from 'react'
import type { ProjectInfo } from './types'
import { ProjectPicker } from './components/ProjectPicker'
import { SessionPanel } from './components/SessionPanel'
import { PlanPanel } from './components/PlanPanel'
import { RunDashboard } from './components/RunDashboard'
import * as api from './api'

type View = 'sessions' | 'plans' | 'runs'

export function App() {
  const [authToken, setAuthTokenState] = useState('')
  const [isAuthenticated, setIsAuthenticated] = useState(false)
  const [selectedProject, setSelectedProject] = useState<ProjectInfo | null>(null)
  const [currentView, setCurrentView] = useState<View>('sessions')
  const [runDashboardPlanPath, setRunDashboardPlanPath] = useState<string | null>(null)
  const [savingPlan, setSavingPlan] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)

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
    setCurrentView('sessions')
    setRunDashboardPlanPath(null)
  }

  function handleSelectProject(project: ProjectInfo) {
    setSelectedProject(project)
    setCurrentView('sessions')
  }

  async function handleSavePlanDraft(content: string) {
    if (!selectedProject) return

    try {
      setSavingPlan(true)
      setSaveError(null)
      const timestamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, -5)
      const name = `plan-${timestamp}`
      await api.savePlanDraft(selectedProject.id, { name, content })
      setCurrentView('plans')
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : 'Failed to save plan')
    } finally {
      setSavingPlan(false)
    }
  }

  function handleOpenRunDashboard(planPath: string) {
    setRunDashboardPlanPath(planPath)
    setCurrentView('runs')
  }

  if (!isAuthenticated) {
    return (
      <div
        style={{
          height: '100%',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          padding: 'var(--spacing-lg)',
        }}
      >
        <div className="card" style={{ maxWidth: '400px', width: '100%' }}>
          <h1 style={{ fontSize: '1.5rem', fontWeight: 600, marginBottom: 'var(--spacing-lg)' }}>aflow Remote</h1>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--spacing-md)' }}>
            <input
              className="input"
              type="password"
              placeholder="Auth token"
              value={authToken}
              onChange={(e) => setAuthTokenState(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && handleLogin()}
            />
            <button className="btn btn-primary" onClick={handleLogin} disabled={!authToken.trim()}>
              Login
            </button>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div style={{ minHeight: '100%', display: 'flex', flexDirection: 'column' }}>
      <header
        style={{
          padding: 'var(--spacing-md)',
          borderBottom: '1px solid var(--color-border)',
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          gap: 'var(--spacing-md)',
        }}
      >
        <div style={{ display: 'flex', flexDirection: 'column', minWidth: 0 }}>
          <h1 style={{ fontSize: '1.25rem', fontWeight: 600 }}>aflow</h1>
          <div className="text-xs text-dim truncate">Projects, planning sessions, and daemon-owned runs</div>
        </div>
        <button className="btn btn-secondary btn-sm" onClick={handleLogout}>
          Logout
        </button>
      </header>

      <main
        style={{
          flex: 1,
          display: 'grid',
          gridTemplateColumns: selectedProject ? 'minmax(280px, 360px) minmax(0, 1fr)' : 'minmax(0, 1fr)',
          gap: 'var(--spacing-md)',
          padding: 'var(--spacing-md)',
          minHeight: 0,
          alignItems: 'stretch',
          overflow: 'hidden',
        }}
      >
        <aside style={{ minWidth: 0, minHeight: 0, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
          <ProjectPicker selectedProjectId={selectedProject?.id ?? null} onSelectProject={handleSelectProject} />
        </aside>

        <section style={{ minWidth: 0, minHeight: 0, display: 'flex', flexDirection: 'column', gap: 'var(--spacing-md)', overflow: 'hidden' }}>
          {selectedProject ? (
            <>
              <div className="card" style={{ display: 'flex', flexDirection: 'column', gap: 'var(--spacing-sm)' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', gap: 'var(--spacing-sm)', alignItems: 'flex-start' }}>
                  <div style={{ minWidth: 0 }}>
                    <div style={{ fontWeight: 600, overflowWrap: 'anywhere', wordBreak: 'break-word' }}>{selectedProject.display_name}</div>
                    <div className="text-xs text-dim mono" style={{ overflowWrap: 'anywhere', wordBreak: 'break-word' }}>
                      {selectedProject.current_path}
                    </div>
                  </div>
                  <div className="text-xs text-dim">{selectedProject.linked_session_count} linked sessions</div>
                </div>

                <div style={{ display: 'flex', gap: 'var(--spacing-sm)', flexWrap: 'wrap' }}>
                  <button
                    className="btn btn-secondary btn-sm"
                    onClick={() => setCurrentView('sessions')}
                    style={{
                      borderBottom: currentView === 'sessions' ? '2px solid var(--color-primary)' : undefined,
                    }}
                  >
                    Sessions
                  </button>
                  <button
                    className="btn btn-secondary btn-sm"
                    onClick={() => setCurrentView('plans')}
                    style={{
                      borderBottom: currentView === 'plans' ? '2px solid var(--color-primary)' : undefined,
                    }}
                  >
                    Plans
                  </button>
                  <button
                    className="btn btn-secondary btn-sm"
                    onClick={() => setCurrentView('runs')}
                    style={{
                      borderBottom: currentView === 'runs' ? '2px solid var(--color-primary)' : undefined,
                    }}
                  >
                    Runs
                  </button>
                </div>
              </div>

              <div style={{ minHeight: 0, flex: 1 }}>
                {currentView === 'sessions' && <SessionPanel project={selectedProject} onSavePlanDraft={handleSavePlanDraft} />}
                {currentView === 'plans' && <PlanPanel project={selectedProject} onOpenRunDashboard={handleOpenRunDashboard} />}
                {currentView === 'runs' && (
                  <RunDashboard
                    initialProjectRoot={selectedProject.current_path}
                    initialPlanPath={runDashboardPlanPath}
                    onInitialPlanHandled={() => setRunDashboardPlanPath(null)}
                  />
                )}
              </div>
            </>
          ) : (
            <div className="card" style={{ minHeight: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center', textAlign: 'center', padding: 'var(--spacing-lg)' }}>
              <div style={{ maxWidth: '28rem' }}>
                <div style={{ fontSize: '1.125rem', fontWeight: 600, marginBottom: 'var(--spacing-sm)' }}>Select a project</div>
                <div className="text-sm text-dim">
                  The project list is on the left. When you pick one, planning sessions and plan tools appear here.
                </div>
              </div>
            </div>
          )}
        </section>
      </main>

      {savingPlan && (
        <div
          style={{
            position: 'fixed',
            top: 0,
            left: 0,
            right: 0,
            bottom: 0,
            background: 'rgba(0, 0, 0, 0.8)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
          }}
        >
          <div className="card">
            <div className="spinner" />
            <div className="text-sm text-dim" style={{ marginTop: 'var(--spacing-md)' }}>
              Saving plan draft...
            </div>
          </div>
        </div>
      )}

      {saveError && (
        <div style={{ position: 'fixed', bottom: 'var(--spacing-md)', left: 'var(--spacing-md)', right: 'var(--spacing-md)' }}>
          <div className="error-message">{saveError}</div>
        </div>
      )}
    </div>
  )
}
