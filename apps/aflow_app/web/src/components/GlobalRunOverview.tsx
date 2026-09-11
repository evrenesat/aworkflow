import { useEffect, useState } from 'react'
import { fetchGlobalRuns, matchesGlobalRun, selectGlobalRuns, useRecentRunsLimit } from '../globalRuns'
import type { ProjectInfo, RunStatus } from '../types'
import { executionDuration, runPlanDisplayName, runPlanPath, statusLabel } from '../runPresentation'
import { formatMachineLabel } from '../label'
import { projectContextLabel } from '../projectPresentation'
import { useHeaderSlots } from './HeaderSlots'

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
  const [search, setSearch] = useState('')
  const [attentionVisible, setAttentionVisible] = useState(10)
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
  const filteredRows = rows.filter(row => {
    const project = projects.find(candidate => candidate.id === row.projectId)
    return matchesGlobalRun(row, search, project ? projectContextLabel(project, projects) : row.projectId)
  })
  const selected = selectGlobalRuns(filteredRows, limit, history)
  const attentionRows = selected.attention.slice(0, attentionVisible)
  const remainingAttention = selected.attention.length - attentionRows.length
  useEffect(() => setAttentionVisible(10), [history, ids, search])

  function changeHistory(next: 'visible' | 'archived' | 'all') {
    setByProject({})
    setLoading(true)
    setAttentionVisible(10)
    setHistory(next)
  }

  function renderRunRow({ projectId, run }: { projectId: string; run: RunStatus }) {
    const project = projects.find(candidate => candidate.id === projectId)
    const projectLabel = project ? projectContextLabel(project, projects) : projectId
    const status = statusLabel(run)
    const exactPath = runPlanPath(run)
    const displayName = runPlanDisplayName(exactPath, run.run_id)
    return <li key={JSON.stringify([projectId, run.run_id])}>
      <button
        className="card global-run-row"
        aria-label={`${projectLabel} · ${status} · ${displayName} · ${run.run_id}`}
        onClick={() => onOpen(projectId, run.run_id)}
      >
        <span className="global-run-row-heading">
          <strong className="global-run-row-title">{displayName}</strong>
          <span className="global-run-row-project">{projectLabel}</span>
          <span className="status-pill">{status}</span>
          {run.history_state === 'archived' && <span className="status-pill">Archived</span>}
        </span>
        <span className="text-sm text-dim">{[run.workflow_name ? formatMachineLabel(run.workflow_name) : null, run.team ? formatMachineLabel(run.team) : null, run.current_step ? formatMachineLabel(run.current_step) : null, executionDuration(run, Date.now())].filter(Boolean).join(' · ') || 'No execution details reported'}</span>
        <span className="text-xs text-dim mono">Plan: {exactPath ?? 'Not reported'} · Run: {run.run_id}</span>
      </button>
    </li>
  }

  const hosted = useHeaderSlots('global-run-overview', {
    context: <h2 className="header-context-title">All runs</h2>,
    local: <label className="header-filter-select"><span>Run history</span><select aria-label="Run history" value={history} onChange={event => changeHistory(event.target.value as typeof history)}><option value="visible">Visible</option><option value="archived">Archived</option><option value="all">All history</option></select></label>,
    primary: <button className="btn btn-secondary btn-sm" onClick={() => setNonce(n => n + 1)}>Refresh</button>,
  })
  return <div className="workspace-content">
    {!hosted && <div className="section-heading"><h2>All runs</h2><button className="btn btn-secondary" onClick={() => setNonce(n => n + 1)}>Refresh</button></div>}
    {!hosted && <label>Run history<select className="input" aria-label="Run history" value={history} onChange={event => changeHistory(event.target.value as typeof history)}><option value="visible">Visible</option><option value="archived">Archived</option><option value="all">All history</option></select></label>}
    <label className="run-search-field">Search loaded runs<input className="input" type="search" aria-label="Search loaded runs" placeholder="Plan, project, status, or run ID" value={search} onChange={event => setSearch(event.target.value)} /></label>
    {errors.length > 0 && <p role="alert" className="notice">Partial or stale results: {errors.join(', ')}. Last available runs are retained.</p>}
    {loading && <p>Loading runs…</p>}
    {registryError && <p role="alert">Project list unavailable: {registryError}. Open Projects to retry.</p>}
    {!registryLoading && !registryError && !projects.length && <p>No registered projects. Add a project in Projects to start.</p>}
    {!loading && !rows.length && !errors.length && projects.length > 0 && <p>No runs yet.</p>}
    {!loading && rows.length > 0 && filteredRows.length === 0 && <p className="text-sm text-dim">No loaded runs match “{search.trim()}”.</p>}
    {([
      { key: 'ongoing', label: `Ongoing (${selected.ongoing.length})`, rows: selected.ongoing },
      { key: 'recent', label: `Recent (${selected.recent.length})`, rows: selected.recent },
      { key: 'attention', label: `Needs attention (${selected.attention.length})`, rows: attentionRows },
    ] as const).map(group => <section key={group.key}>
      <h3>{group.label}</h3>
      {group.rows.length === 0 && <p className="text-sm text-dim">{group.key === 'ongoing' ? 'No ongoing runs.' : group.key === 'attention' ? 'No runs need attention.' : 'No recent runs.'}</p>}
      {group.rows.length > 0 && <ul className="compact-list">{group.rows.map(renderRunRow)}</ul>}
      {group.key === 'attention' && remainingAttention > 0 && <button type="button" className="btn btn-secondary" onClick={() => setAttentionVisible(count => count + 10)}>Show more ({remainingAttention} remaining)</button>}
    </section>)}
  </div>
}
