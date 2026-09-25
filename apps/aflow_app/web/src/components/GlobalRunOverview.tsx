import { useEffect, useRef, useState } from 'react'
import * as api from '../api'
import { fetchGlobalRuns, matchesGlobalRun, matchesGlobalRunProgressIdentity, matchesGlobalRunSnapshot, selectGlobalRuns, useRecentRunsLimit, type GlobalRunProgressUpdate } from '../globalRuns'
import type { ProjectInfo, RunProgressSummary, RunStatus } from '../types'
import { projectContextLabel } from '../projectPresentation'
import { useHeaderSlots } from './HeaderSlots'
import { RunListItem } from './RunListItem'
import { type RunProgressLoadState } from './RunProgress'

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

type ProjectRunCoverage = 'loading' | 'complete' | 'failed'

interface ProjectRunLoadState {
  coverage: ProjectRunCoverage
  freshRows: RunStatus[]
  previousRows: RunStatus[]
  displayedRows: RunStatus[]
}

interface GlobalRunLoadState {
  identity: string | null
  generation: number
  mode: 'initial' | 'refresh'
  settled: boolean
  projects: Record<string, ProjectRunLoadState>
}

const emptyGlobalRunLoadState: GlobalRunLoadState = {
  identity: null,
  generation: 0,
  mode: 'initial',
  settled: false,
  projects: {},
}

function runIdentity(projectId: string, runId: string): string {
  return JSON.stringify([projectId, runId])
}

function sameRunStatus(left: RunStatus, right: RunStatus): boolean {
  return JSON.stringify(left) === JSON.stringify(right)
}

/** Reuse equal rows so background polling cannot remount their previews. */
function reconcileRunRows(projectId: string, previousRows: RunStatus[], nextRows: RunStatus[]): RunStatus[] {
  const previousById = new Map(previousRows.map(run => [runIdentity(projectId, run.run_id), run]))
  const reconciled = nextRows.map(run => {
    const previous = previousById.get(runIdentity(projectId, run.run_id))
    return previous && sameRunStatus(previous, run) ? previous : run
  })
  if (reconciled.length === previousRows.length && reconciled.every((run, index) => run === previousRows[index])) {
    return previousRows
  }
  return reconciled
}

function overlayRuns(projectId: string, previousRows: RunStatus[], freshRows: RunStatus[]): RunStatus[] {
  const rows = new Map<string, RunStatus>()
  for (const run of previousRows) rows.set(runIdentity(projectId, run.run_id), run)
  for (const run of freshRows) {
    const identity = runIdentity(projectId, run.run_id)
    const previous = rows.get(identity)
    rows.set(identity, previous && sameRunStatus(previous, run) ? previous : run)
  }
  const overlaid = [...rows.values()]
  return overlaid.length === previousRows.length
    && overlaid.every((run, index) => run === previousRows[index])
    ? previousRows
    : overlaid
}

interface EnrichmentTarget {
  key: string
  projectId: string
  runId: string
  generation: number
  resultsIdentity: string
  run: RunStatus
}

interface EnrichmentRecord extends EnrichmentTarget {
  state: RunProgressLoadState
  progress: RunProgressSummary | null
  message: string | null
  settled: boolean
}

interface EnrichmentRequest extends EnrichmentTarget {
  controller: AbortController
}

const MAX_VISIBLE_ENRICHMENTS = 4
const STALE_PROGRESS_MESSAGE = 'Run changed; refresh to update checkpoint progress.'
const FAILED_PROGRESS_MESSAGE = 'Checkpoint progress unavailable — Refresh to retry.'

export function GlobalRunOverview({ projects, onOpen, registryLoading = false, registryError = null }: { projects: ProjectInfo[]; onOpen: (project: string, run: string) => void; registryLoading?: boolean; registryError?: string | null }) {
  const [history, setHistory] = useState<'visible' | 'archived' | 'all'>('visible')
  const [loadState, setLoadState] = useState<GlobalRunLoadState>(emptyGlobalRunLoadState)
  const loadStateRef = useRef(loadState)
  const requestGenerationRef = useRef(0)
  const enrichmentRef = useRef(new Map<string, EnrichmentRecord>())
  const enrichmentQueueRef = useRef<EnrichmentTarget[]>([])
  const activeEnrichmentRef = useRef(new Map<string, EnrichmentRequest>())
  const visibleEnrichmentRef = useRef(new Map<string, EnrichmentTarget>())
  const enrichmentEpochRef = useRef(0)
  const rowTokenRef = useRef(new Map<string, number>())
  const nextRowTokenRef = useRef(0)
  const pumpEnrichmentRef = useRef<(() => void) | null>(null)
  const [, setEnrichmentVersion] = useState(0)
  const [visibilityRevision, setVisibilityRevision] = useState(0)
  const [nonce, setNonce] = useState(0)
  const explicitRefreshRef = useRef(false)
  const [limit] = useRecentRunsLimit()
  const [search, setSearch] = useState('')
  const [attentionVisible, setAttentionVisible] = useState(10)
  const [historyGapVisible, setHistoryGapVisible] = useState(10)
  const projectIds = projects.map(p => p.id).sort()
  const ids = projectIds.join('\n')
  const resultsIdentity = JSON.stringify([history, projectIds])
  const registryReady = !registryLoading && !registryError
  const currentLoad = loadState.identity === resultsIdentity ? loadState : null
  const projectStates = currentLoad?.projects ?? {}
  const resultsPending = !registryError && (!registryReady || !currentLoad || projectIds.some(id => projectStates[id]?.coverage === 'loading' || !projectStates[id]))
  const coverageComplete = registryReady && currentLoad !== null && projectIds.every(id => projectStates[id]?.coverage === 'complete')
  const progressAdmissionReady = registryReady && currentLoad !== null && currentLoad.settled
    && projectIds.every(id => projectStates[id] !== undefined && projectStates[id].coverage !== 'loading')
  const failedProjectIds = projectIds.filter(id => projectStates[id]?.coverage === 'failed')

  function updateLoadState(updater: (current: GlobalRunLoadState) => GlobalRunLoadState): void {
    const next = updater(loadStateRef.current)
    loadStateRef.current = next
    setLoadState(next)
  }

  useEffect(() => {
    const controller = new AbortController()
    let busy = false
    const identity = resultsIdentity
    const requestedProjectIds = ids ? ids.split('\n') : []

    const beginRequest = (generation: number, explicitRefresh: boolean): void => {
      const previous = loadStateRef.current
      const sameIdentity = previous.identity === identity
      const previousProjects = sameIdentity ? previous.projects : {}
      const projectsState = Object.fromEntries(requestedProjectIds.map(projectId => {
        const previousRows = previousProjects[projectId]?.displayedRows ?? []
        return [projectId, {
          coverage: 'loading' as const,
          freshRows: [],
          previousRows: [...previousRows],
          displayedRows: [...previousRows],
        }]
      }))
      updateLoadState(() => ({
        identity,
        // Only an explicit Refresh invalidates every enrichment. A changed
        // background row has its own object identity and re-enriches alone.
        generation: sameIdentity
          ? previous.generation + (explicitRefresh ? 1 : 0)
          : generation,
        mode: sameIdentity && previous.settled ? 'refresh' : 'initial',
        settled: requestedProjectIds.length === 0,
        projects: projectsState,
      }))
    }

    const refresh = async () => {
      if (busy || document.visibilityState === 'hidden' || !registryReady) return
      busy = true
      const generation = ++requestGenerationRef.current
      const explicitRefresh = explicitRefreshRef.current
      explicitRefreshRef.current = false
      beginRequest(generation, explicitRefresh)
      if (!ids) {
        busy = false
        return
      }
      const current = (update: GlobalRunProgressUpdate): void => {
        if (controller.signal.aborted || requestGenerationRef.current !== generation) return
        updateLoadState(previous => {
          if (previous.identity !== identity) return previous
          const project = previous.projects[update.projectId]
          if (!project) return previous
          const freshRows = [...update.runs]
          const displayedRows = update.state === 'complete'
            ? reconcileRunRows(update.projectId, project.displayedRows, freshRows)
            : overlayRuns(update.projectId, project.previousRows, freshRows)
          const projectsState = {
            ...previous.projects,
            [update.projectId]: {
              coverage: update.state,
              freshRows,
              previousRows: project.previousRows,
              displayedRows,
            },
          }
          return {
            ...previous,
            generation: previous.generation,
            settled: requestedProjectIds.every(projectId => projectsState[projectId]?.coverage !== 'loading'),
            projects: projectsState,
          }
        })
      }
      try {
        const result = await fetchGlobalRuns(requestedProjectIds, controller.signal, history, current)
        if (controller.signal.aborted || requestGenerationRef.current !== generation) return
        updateLoadState(previous => {
          if (previous.identity !== identity) return previous
          const projectsState = { ...previous.projects }
          for (const [projectId, runs] of Object.entries(result.byProject)) {
            const project = projectsState[projectId]
            if (!project) continue
            const freshRows = [...runs]
            const displayedRows = reconcileRunRows(projectId, project.displayedRows, freshRows)
            projectsState[projectId] = {
              coverage: 'complete',
              freshRows,
              previousRows: project.previousRows,
              displayedRows,
            }
          }
          for (const projectId of result.errors) {
            const project = projectsState[projectId]
            if (!project) continue
            projectsState[projectId] = { ...project, coverage: 'failed' }
          }
          return {
            ...previous,
            generation: previous.generation,
            mode: 'initial',
            settled: true,
            projects: projectsState,
          }
        })
      } finally {
        busy = false
      }
    }
    void refresh()
    const timer = setInterval(() => void refresh(), 10000)
    document.addEventListener('visibilitychange', refresh)
    window.addEventListener('aflow-history-changed', refresh)
    return () => {
      controller.abort()
      requestGenerationRef.current += 1
      clearInterval(timer)
      document.removeEventListener('visibilitychange', refresh)
      window.removeEventListener('aflow-history-changed', refresh)
    }
  }, [history, ids, nonce, registryReady, resultsIdentity])
  const rows = projects.flatMap(project => (projectStates[project.id]?.displayedRows ?? []).map(run => ({ projectId: project.id, run })))
  const filteredRows = rows.filter(row => {
    const project = projects.find(candidate => candidate.id === row.projectId)
    return matchesGlobalRun(row, search, project ? projectContextLabel(project, projects) : row.projectId)
  })
  const selected = selectGlobalRuns(filteredRows, limit, history)
  const attentionRows = selected.attention.slice(0, attentionVisible)
  const remainingAttention = selected.attention.length - attentionRows.length
  const historyGapRows = selected.historyGaps.slice(0, historyGapVisible)
  const remainingHistoryGaps = selected.historyGaps.length - historyGapRows.length
  const hasUsableRows = rows.length > 0
  const backgroundRefresh = currentLoad?.mode === 'refresh' && hasUsableRows && failedProjectIds.length === 0
  const incompleteCoverage = !backgroundRefresh && (resultsPending || !coverageComplete)
  const showGroups = projects.length > 0 && (hasUsableRows || coverageComplete) && (!registryError || hasUsableRows)
  const groups = ([
    { key: 'ongoing', label: `${incompleteCoverage ? 'Loaded ongoing' : 'Ongoing'} (${selected.ongoing.length})`, rows: selected.ongoing },
    { key: 'recent', label: `${incompleteCoverage ? 'Loaded recent' : 'Recent'} (${selected.recent.length})`, rows: selected.recent },
    { key: 'attention', label: `${incompleteCoverage ? 'Loaded needs attention' : 'Needs attention'} (${selected.attention.length})`, rows: attentionRows },
    { key: 'history-gaps', label: `Outcome not recorded (${selected.historyGaps.length})`, rows: historyGapRows },
  ] as const)
  // A populated overview should spend space on meaningful groups only. Empty
  // states remain explicit at the page level (no projects, no runs, or no
  // search match) instead of becoming repeated decorative group cards.
  const visibleGroups = groups.filter(group => group.rows.length > 0)
  useEffect(() => {
    setAttentionVisible(10)
    setHistoryGapVisible(10)
  }, [history, ids, search])

  const renderedRows = [...selected.ongoing, ...selected.recent, ...attentionRows, ...historyGapRows]
  const currentGeneration = currentLoad?.generation ?? 0
  const renderedSnapshot = renderedRows.map(({ projectId, run }) => {
    const identity = runIdentity(projectId, run.run_id)
    let token = rowTokenRef.current.get(identity)
    if (token === undefined) {
      token = ++nextRowTokenRef.current
      rowTokenRef.current.set(identity, token)
    }
    return `${identity}:${token}`
  }).join('\u0000')

  useEffect(() => {
    const targets = new Map<string, EnrichmentTarget>()
    for (const { projectId, run } of renderedRows) {
      const key = runIdentity(projectId, run.run_id)
      targets.set(key, { key, projectId, runId: run.run_id, generation: currentGeneration, resultsIdentity, run })
    }
    visibleEnrichmentRef.current = targets

    const sameTarget = (left: EnrichmentTarget, right: EnrichmentTarget): boolean => (
      left.key === right.key
      && left.generation === right.generation
      && left.resultsIdentity === right.resultsIdentity
      && left.run === right.run
    )
    const isCurrentTarget = (target: EnrichmentTarget): boolean => {
      const current = targets.get(target.key)
      return current !== undefined && sameTarget(current, target)
    }
    const hasQueuedTarget = (target: EnrichmentTarget): boolean => enrichmentQueueRef.current.some(candidate => sameTarget(candidate, target))
    const hasActiveTarget = (target: EnrichmentTarget): boolean => {
      const active = activeEnrichmentRef.current.get(target.key)
      return active !== undefined && sameTarget(active, target)
    }
    let changed = false
    const enqueueTarget = (target: EnrichmentTarget): void => {
      if (hasQueuedTarget(target) || hasActiveTarget(target)) return
      enrichmentQueueRef.current.push(target)
      changed = true
    }

    for (const [key, request] of activeEnrichmentRef.current) {
      if (!isCurrentTarget(request)) {
        request.controller.abort()
        activeEnrichmentRef.current.delete(key)
      }
    }
    enrichmentQueueRef.current = enrichmentQueueRef.current.filter(isCurrentTarget)

    for (const key of [...enrichmentRef.current.keys()]) {
      if (!targets.has(key)) {
        enrichmentRef.current.delete(key)
        changed = true
      }
    }

    for (const target of targets.values()) {
      const existing = enrichmentRef.current.get(target.key)
      if (existing && sameTarget(existing, target)) {
        if (!existing.settled && !hasActiveTarget(target)) enqueueTarget(target)
        continue
      }

      if (target.run.progress) {
        enrichmentRef.current.set(target.key, {
          ...target,
          state: 'ready',
          progress: target.run.progress,
          message: null,
          settled: true,
        })
        changed = true
        continue
      }

      const rawChanged = existing !== undefined && existing.run !== target.run
      enrichmentRef.current.set(target.key, {
        ...target,
        state: rawChanged && existing?.progress ? 'stale' : 'loading',
        progress: existing?.progress ?? null,
        message: rawChanged && existing?.progress ? STALE_PROGRESS_MESSAGE : null,
        settled: false,
      })
      enqueueTarget(target)
    }

    const settle = (
      target: EnrichmentTarget,
      state: RunProgressLoadState,
      progress: RunProgressSummary | null,
      message: string | null,
    ): void => {
      const current = enrichmentRef.current.get(target.key)
      const visible = visibleEnrichmentRef.current.get(target.key)
      if (
        !current
        || !visible
        || current.generation !== target.generation
        || current.resultsIdentity !== target.resultsIdentity
        || current.run !== target.run
        || visible.generation !== target.generation
        || visible.resultsIdentity !== target.resultsIdentity
        || visible.run !== target.run
      ) return
      enrichmentRef.current.set(target.key, { ...current, state, progress, message, settled: true })
      setEnrichmentVersion(version => version + 1)
    }

    const pump = (): void => {
      if (pumpEnrichmentRef.current !== pump) return
      if (document.visibilityState === 'hidden') return
      if (!progressAdmissionReady) return
      while (activeEnrichmentRef.current.size < MAX_VISIBLE_ENRICHMENTS) {
        const target = enrichmentQueueRef.current.shift()
        if (!target) break
        if (!isCurrentTarget(target)) continue
        const current = enrichmentRef.current.get(target.key)
        if (
          !current
          || current.generation !== target.generation
          || current.resultsIdentity !== target.resultsIdentity
          || current.run !== target.run
          || (current.state !== 'loading' && current.state !== 'stale')
        ) continue
        const controller = new AbortController()
        const request: EnrichmentRequest = { ...target, controller }
        const requestEpoch = enrichmentEpochRef.current
        activeEnrichmentRef.current.set(target.key, request)
        void api.getControlPlaneRun(target.projectId, target.runId, { signal: controller.signal })
          .then(detail => {
            if (controller.signal.aborted || enrichmentEpochRef.current !== requestEpoch) return
            if (
              detail.run_id !== target.runId
              || !matchesGlobalRunSnapshot(target.run, detail)
              || !matchesGlobalRunProgressIdentity(target.run, detail)
            ) {
              settle(target, 'stale', current.progress, STALE_PROGRESS_MESSAGE)
              return
            }
            settle(target, 'ready', detail.progress ?? null, null)
          })
          .catch(() => {
            if (!controller.signal.aborted && enrichmentEpochRef.current === requestEpoch) {
              const latest = enrichmentRef.current.get(target.key)
              settle(target, 'failed', latest?.progress ?? null, FAILED_PROGRESS_MESSAGE)
            }
          })
          .finally(() => {
            if (activeEnrichmentRef.current.get(target.key) === request) {
              activeEnrichmentRef.current.delete(target.key)
            }
            if (!controller.signal.aborted && enrichmentEpochRef.current === requestEpoch) pumpEnrichmentRef.current?.()
          })
      }
    }
    pumpEnrichmentRef.current = pump
    if (changed) setEnrichmentVersion(version => version + 1)
    pump()
  }, [renderedSnapshot, currentGeneration, resultsIdentity, progressAdmissionReady, visibilityRevision])

  useEffect(() => () => {
    enrichmentEpochRef.current += 1
    for (const request of activeEnrichmentRef.current.values()) request.controller.abort()
    activeEnrichmentRef.current.clear()
    enrichmentQueueRef.current = []
    enrichmentRef.current.clear()
    visibleEnrichmentRef.current.clear()
    pumpEnrichmentRef.current = null
  }, [])

  useEffect(() => {
    const pauseEnrichment = (): void => {
      if (document.visibilityState === 'hidden') {
        enrichmentEpochRef.current += 1
        for (const request of activeEnrichmentRef.current.values()) request.controller.abort()
        activeEnrichmentRef.current.clear()
        enrichmentQueueRef.current = []
      }
      setVisibilityRevision(revision => revision + 1)
    }
    document.addEventListener('visibilitychange', pauseEnrichment)
    return () => document.removeEventListener('visibilitychange', pauseEnrichment)
  }, [])

  function changeHistory(next: 'visible' | 'archived' | 'all') {
    updateLoadState(() => emptyGlobalRunLoadState)
    setAttentionVisible(10)
    setHistoryGapVisible(10)
    setHistory(next)
  }

  function requestRefresh(): void {
    explicitRefreshRef.current = true
    setNonce(n => n + 1)
  }

  function renderRunRow({ projectId, run }: { projectId: string; run: RunStatus }) {
    const project = projects.find(candidate => candidate.id === projectId)
    const projectLabel = project ? projectContextLabel(project, projects) : projectId
    const key = runIdentity(projectId, run.run_id)
    const enrichment = enrichmentRef.current.get(key)
    const currentEnrichment = enrichment
      && enrichment.generation === currentGeneration
      && enrichment.resultsIdentity === resultsIdentity
      && enrichment.run === run
      ? enrichment
      : null
    const progressRun = currentEnrichment ? { ...run, progress: currentEnrichment.progress } : run
    const progressState: RunProgressLoadState = currentEnrichment?.state ?? (run.progress ? 'ready' : 'loading')
    return <li key={JSON.stringify([projectId, run.run_id])}>
      <RunListItem
        run={progressRun}
        stableKey={key}
        projectLabel={projectLabel}
        rowClassName="global-run-row"
        dataEnrichmentState={progressState === 'ready' ? 'settled' : progressState}
        loadState={progressState}
        loadMessage={currentEnrichment?.message}
        onSelect={() => onOpen(projectId, run.run_id)}
      />
    </li>
  }

  const hosted = useHeaderSlots('global-run-overview', {
    context: <h2 className="header-context-title">All runs</h2>,
    local: <label className="header-filter-select"><span>Run history</span><select aria-label="Run history" value={history} onChange={event => changeHistory(event.target.value as typeof history)}><option value="visible">Visible</option><option value="archived">Archived</option><option value="all">All history</option></select></label>,
    primary: <button className="btn btn-secondary btn-sm" onClick={requestRefresh}>Refresh</button>,
  })
  return <div className="workspace-content">
    {!hosted && <div className="section-heading"><h2>All runs</h2><button className="btn btn-secondary" onClick={requestRefresh}>Refresh</button></div>}
    {!hosted && <label>Run history<select className="input" aria-label="Run history" value={history} onChange={event => changeHistory(event.target.value as typeof history)}><option value="visible">Visible</option><option value="archived">Archived</option><option value="all">All history</option></select></label>}
    <label className="run-search-field">Search loaded runs<input className="input" type="search" aria-label="Search loaded runs" placeholder="Plan, project, status, or run ID" value={search} onChange={event => setSearch(event.target.value)} /></label>
    <div className="global-run-results" aria-busy={resultsPending && !backgroundRefresh}>
      {resultsPending && !backgroundRefresh && <p role="status" className="global-run-loading"><span className="spinner global-run-loading-spinner" aria-hidden="true" /><span>{currentLoad?.mode === 'refresh' ? 'Refreshing runs…' : 'Loading runs…'} Results are incomplete.</span></p>}
      {failedProjectIds.length > 0 && <p role="alert" className="notice">{rows.length > 0 ? 'Partial or stale results' : 'Run results unavailable'} for: {failedProjectIds.join(', ')}. {rows.length > 0 ? 'Last available runs are retained.' : 'Use Refresh to try again.'}</p>}
      {registryError && <p role="alert">Project list unavailable: {registryError}. Open Projects to retry.</p>}
      {coverageComplete && !registryError && !projects.length && <p>No registered projects. Add a project in Projects to start.</p>}
      {coverageComplete && !registryError && !rows.length && projects.length > 0 && <p>No runs yet.</p>}
      {rows.length > 0 && filteredRows.length === 0 && search.trim() && <p className="text-sm text-dim">No loaded runs match “{search.trim()}”.</p>}
      {showGroups && visibleGroups.map(group => <section key={group.key} className={group.key === 'history-gaps' ? 'global-run-history-gap-group' : undefined}>
        <h3>{group.label}</h3>
        <ul className="compact-list">{group.rows.map(renderRunRow)}</ul>
        {group.key === 'attention' && remainingAttention > 0 && <button type="button" className="btn btn-secondary" onClick={() => setAttentionVisible(count => count + 10)}>Show more ({remainingAttention} remaining)</button>}
        {group.key === 'history-gaps' && remainingHistoryGaps > 0 && <button type="button" className="btn btn-secondary" onClick={() => setHistoryGapVisible(count => count + 10)}>Show more ({remainingHistoryGaps} remaining)</button>}
      </section>)}
    </div>
  </div>
}
