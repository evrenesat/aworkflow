import { useEffect, useState } from 'react'
import { fetchGlobalRuns, selectGlobalRuns, useRecentRunsLimit } from '../globalRuns'
import type { ProjectInfo, RunStatus } from '../types'
import { executionDuration, statusLabel } from '../runPresentation'

export function RecentRunsLimit() {
  const [limit, setLimit] = useRecentRunsLimit()
  const [text, setText] = useState(String(limit))
  const [error, setError] = useState<string | null>(null)
  useEffect(() => setText(String(limit)), [limit])
  function commit() {
    const value = Number(text)
    if (!/^\d+$/.test(text) || !Number.isSafeInteger(value) || value < 1) { setError('Enter a positive whole number.'); return }
    setLimit(value); setText(String(value)); setError(null)
  }
  return <label>Recent non-running runs shown <input className="input" aria-label="Recent non-running runs shown" inputMode="numeric" value={text} onChange={e => { setText(e.target.value); setError(null) }} onBlur={commit} onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); commit() } }} />{error && <span role="alert">{error}</span>}</label>
}

export function GlobalRunOverview({ projects, onOpen, registryLoading = false, registryError = null }: { projects: ProjectInfo[]; onOpen: (project: string, run: string) => void; registryLoading?: boolean; registryError?: string | null }) {
  const [history, setHistory] = useState<'visible' | 'archived' | 'all'>('visible')
  const [byProject, setByProject] = useState<Record<string, RunStatus[]>>({})
  const [errors, setErrors] = useState<string[]>([])
  const [loading, setLoading] = useState(true)
  const [nonce, setNonce] = useState(0)
  const [limit] = useRecentRunsLimit()
  const ids = projects.map(p => p.id).sort().join('\n')
  useEffect(() => {
    const controller = new AbortController()
    let busy = false
    const refresh = async () => {
      if (busy || document.visibilityState === 'hidden') return
      busy = true
      const result = await fetchGlobalRuns(ids ? ids.split('\n') : [], controller.signal, history)
      if (!controller.signal.aborted) {
        setByProject(previous => ({ ...previous, ...result.byProject }))
        setErrors(result.errors)
        setLoading(false)
      }
      busy = false
    }
    void refresh()
    const timer = setInterval(() => void refresh(), 10000)
    document.addEventListener('visibilitychange', refresh)
    window.addEventListener('aflow-history-changed', refresh)
    return () => { controller.abort(); clearInterval(timer); document.removeEventListener('visibilitychange', refresh); window.removeEventListener('aflow-history-changed', refresh) }
  }, [ids, nonce, history])
  const rows = projects.flatMap(project => (byProject[project.id] ?? []).map(run => ({ projectId: project.id, run })))
  const selected = selectGlobalRuns(rows, limit, history)
  return <div className="workspace-content">
    <div className="section-heading"><h2>All runs</h2><button className="btn btn-secondary" onClick={() => setNonce(n => n + 1)}>Refresh</button></div>
    <label>Run history<select className="input" aria-label="Run history" value={history} onChange={event => { setByProject({}); setHistory(event.target.value as typeof history) }}><option value="visible">Visible</option><option value="archived">Archived</option><option value="all">All history</option></select></label>
    {errors.length > 0 && <p role="alert" className="notice">Partial or stale results: {errors.join(', ')}. Last available runs are retained.</p>}
    {loading && <p>Loading runs…</p>}
    {registryError && <p role="alert">Project list unavailable: {registryError}. Open Projects to retry.</p>}
    {!registryLoading && !registryError && !projects.length && <p>No registered projects. Add a project in Projects to start.</p>}
    {!loading && !rows.length && !errors.length && projects.length > 0 && <p>No runs yet.</p>}
    {(['ongoing', 'attention', 'recent'] as const).map(group => <section key={group}>
      <h3>{group === 'ongoing' ? 'Ongoing' : group === 'attention' ? 'Needs attention' : `Recent (${selected.recent.length})`}</h3>
      {selected[group].length === 0 && <p className="text-sm text-dim">{group === 'ongoing' ? 'No ongoing runs.' : group === 'attention' ? 'No runs need attention.' : 'No recent runs.'}</p>}
      {selected[group].length > 0 && <ul className="compact-list">
        {selected[group].map(({ projectId, run }) => <li key={JSON.stringify([projectId, run.run_id])}>
          <button className="card global-run-row" onClick={() => onOpen(projectId, run.run_id)}>
            <strong>{projects.find(p => p.id === projectId)?.display_name ?? projectId} · {statusLabel(run)}{run.history_state === 'archived' ? ' · Archived' : ''}</strong>
            <span>{run.plan_path ?? run.run_id}</span>
            <span className="text-sm text-dim">{[run.workflow_name, run.team, run.current_step, executionDuration(run, Date.now())].filter(Boolean).join(' · ')}</span>
          </button>
        </li>)}
      </ul>}
    </section>)}
  </div>
}
