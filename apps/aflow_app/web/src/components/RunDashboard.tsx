import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type {
  ControlPlaneCapabilities,
  ControlPlanePlan,
  ControlPlaneProject,
  ControlPlaneReadiness,
  RunContext,
  RunEvent,
  RunStatus,
  StartRunResponse,
  StartupQuestion,
} from '../types'
import { ApiError } from '../api'
import * as api from '../api'

const MAX_TIMELINE_EVENTS = 100

interface RunDashboardProps {
  initialProjectRoot: string
  initialPlanPath: string | null
  onInitialPlanHandled: () => void
}

function requestKey(prefix: string): string {
  const identifier = globalThis.crypto?.randomUUID?.()
    ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`
  return `${prefix}-${identifier}`
}

interface PendingWriteKey {
  intent: string
  key: string
}

/**
 * Holds an idempotency key only while its write outcome is uncertain.  Keeping
 * this state in a ref makes retries survive renders without persisting either
 * bearer material or write metadata in browser storage.
 */
function usePendingWriteKeys() {
  const pendingKeys = useRef(new Map<string, PendingWriteKey>())

  const getKey = useCallback((operation: string, intent: Record<string, unknown>): string => {
    const serializedIntent = JSON.stringify(intent)
    const pending = pendingKeys.current.get(operation)
    if (pending?.intent === serializedIntent) return pending.key

    const key = requestKey(operation)
    pendingKeys.current.set(operation, { intent: serializedIntent, key })
    return key
  }, [])

  const clearKey = useCallback((operation: string, intent: Record<string, unknown>) => {
    const pending = pendingKeys.current.get(operation)
    if (pending?.intent === JSON.stringify(intent)) pendingKeys.current.delete(operation)
  }, [])

  const clearAll = useCallback(() => {
    pendingKeys.current.clear()
  }, [])

  useEffect(() => clearAll, [clearAll])

  return { getKey, clearKey, clearAll }
}

function mergeEvents(current: RunEvent[], next: RunEvent[]): RunEvent[] {
  const bySequence = new Map<number, RunEvent>()
  for (const event of [...current, ...next]) bySequence.set(event.sequence, event)
  return [...bySequence.values()]
    .sort((left, right) => left.sequence - right.sequence)
    .slice(-MAX_TIMELINE_EVENTS)
}

function upsertRun(current: RunStatus[], next: RunStatus): RunStatus[] {
  const existing = current.findIndex((run) => run.run_id === next.run_id)
  if (existing < 0) return [...current, next]
  return current.map((run) => run.run_id === next.run_id ? next : run)
}

function timestamp(value: unknown): string {
  if (typeof value !== 'string') return 'Not reported'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
}

function textEvidence(run: RunStatus, key: string): string {
  const value = run.evidence[key]
  return typeof value === 'string' || typeof value === 'number' ? String(value) : 'Not reported'
}

function contextText(context: RunContext | null, key: string): string {
  if (!context) return 'Not reported'
  const data = context.data
  const runMetadata = data.run_metadata
  if (typeof runMetadata === 'object' && runMetadata !== null && key in runMetadata) {
    const value = (runMetadata as Record<string, unknown>)[key]
    if (typeof value === 'string' || typeof value === 'number') return String(value)
  }
  const managerContext = data.manager_context
  if (typeof managerContext === 'object' && managerContext !== null && key in managerContext) {
    const value = (managerContext as Record<string, unknown>)[key]
    if (typeof value === 'string' || typeof value === 'number') return String(value)
  }
  return 'Not reported'
}

function statusLabel(run: RunStatus): string {
  if (run.ownership === 'legacy') return 'Legacy interrupted (read-only)'
  if (run.status === 'needs_attention') return 'Needs attention — explicit resume required'
  return run.status.replace(/_/g, ' ')
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback
}

function apiErrorCode(error: unknown): string | null {
  if (error instanceof ApiError) return error.code
  if (typeof error === 'object' && error !== null && 'code' in error) {
    const code = (error as { code?: unknown }).code
    return typeof code === 'string' ? code : null
  }
  return null
}

export function RunDashboard({ initialProjectRoot, initialPlanPath, onInitialPlanHandled }: RunDashboardProps) {
  const [projects, setProjects] = useState<ControlPlaneProject[]>([])
  const [projectId, setProjectId] = useState<string | null>(null)
  const [capabilities, setCapabilities] = useState<ControlPlaneCapabilities | null>(null)
  const [readiness, setReadiness] = useState<ControlPlaneReadiness | null>(null)
  const [plans, setPlans] = useState<ControlPlanePlan[]>([])
  const [plansLoaded, setPlansLoaded] = useState(false)
  const [runs, setRuns] = useState<RunStatus[]>([])
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null)
  const [events, setEvents] = useState<RunEvent[]>([])
  const [context, setContext] = useState<RunContext | null>(null)
  const [contextLevel, setContextLevel] = useState<'lite' | 'full'>('lite')
  const [fullContextAcknowledged, setFullContextAcknowledged] = useState(false)
  const [streamState, setStreamState] = useState<api.StreamState>('stopped')
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [busyAction, setBusyAction] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [streamNotice, setStreamNotice] = useState<string | null>(null)
  const [feedback, setFeedback] = useState<string | null>(null)
  const [startupQuestion, setStartupQuestion] = useState<StartupQuestion | null>(null)
  const [startPlanPath, setStartPlanPath] = useState('')
  const [startWorkflow, setStartWorkflow] = useState('')
  const [startTeam, setStartTeam] = useState('')
  const [startMaxTurns, setStartMaxTurns] = useState('')
  const [controlMaxTurns, setControlMaxTurns] = useState('')
  const [controlTeam, setControlTeam] = useState('')
  const [roleWorkers, setRoleWorkers] = useState<Record<string, string>>({})
  const [confirmOwnerStop, setConfirmOwnerStop] = useState(false)
  const [confirmResume, setConfirmResume] = useState(false)
  const selectedRunRef = useRef<string | null>(null)
  const { getKey: getPendingWriteKey, clearKey: clearPendingWriteKey, clearAll: clearPendingWriteKeys } = usePendingWriteKeys()

  const selectedRun = useMemo(
    () => runs.find((run) => run.run_id === selectedRunId) ?? null,
    [runs, selectedRunId],
  )

  const selectedProject = useMemo(
    () => projects.find((project) => project.project_id === projectId) ?? null,
    [projects, projectId],
  )

  useEffect(() => {
    selectedRunRef.current = selectedRunId
  }, [selectedRunId])

  useEffect(() => {
    clearPendingWriteKeys()
  }, [projectId, selectedRunId, clearPendingWriteKeys])

  useEffect(() => {
    void (async () => {
      try {
        setLoading(true)
        setError(null)
        const available = await api.listControlPlaneProjects()
        setProjects(available)
        const matchingProject = available.find((project) => project.root === initialProjectRoot)
        setProjectId((current) => current && available.some((project) => project.project_id === current)
          ? current
          : matchingProject?.project_id ?? available[0]?.project_id ?? null)
      } catch (loadError) {
        setError(errorMessage(loadError, 'Failed to load control-plane projects'))
      } finally {
        setLoading(false)
      }
    })()
  }, [initialProjectRoot])

  useEffect(() => {
    if (projectId) void loadDashboard(projectId)
  }, [projectId])

  useEffect(() => {
    if (!initialPlanPath || !plansLoaded) return
    if (plans.some((plan) => plan.path === initialPlanPath)) {
      setStartPlanPath(initialPlanPath)
    } else {
      setFeedback('Select a daemon-approved plan from the run dashboard before starting a run.')
    }
    onInitialPlanHandled()
  }, [initialPlanPath, onInitialPlanHandled, plans, plansLoaded])

  useEffect(() => {
    if (!projectId || !selectedRunId) return
    let active = true
    setEvents([])
    setContext(null)
    setContextLevel('lite')
    setFullContextAcknowledged(false)
    void loadSelectedRun(projectId, selectedRunId, active)
    const unsubscribe = api.subscribeToRunEvents({
      projectId,
      runId: selectedRunId,
      onEvents: (incoming) => {
        if (!active || selectedRunRef.current !== selectedRunId) return
        setEvents((current) => mergeEvents(current, incoming))
      },
      onError: (streamError) => {
        if (!active) return
        setStreamNotice(`${streamError.message} The displayed run status is unchanged.`)
      },
      onStateChange: (state) => {
        if (!active) return
        setStreamState(state)
      },
    })
    return () => {
      active = false
      unsubscribe()
    }
  }, [projectId, selectedRunId])

  useEffect(() => {
    if (!selectedRun) return
    setControlMaxTurns(selectedRun.max_turns?.toString() ?? '')
    setControlTeam(selectedRun.team ?? '')
    setRoleWorkers({})
    setConfirmOwnerStop(false)
    setConfirmResume(false)
  }, [selectedRun])

  async function loadDashboard(nextProjectId: string) {
    try {
      setRefreshing(true)
      setError(null)
      setPlansLoaded(false)
      const [nextReadiness, nextCapabilities, nextPlans, page] = await Promise.all([
        api.getControlPlaneReadiness(),
        api.getControlPlaneCapabilities(nextProjectId),
        api.listControlPlanePlans(nextProjectId),
        api.listControlPlaneRuns(nextProjectId, { limit: 100 }),
      ])
      setReadiness(nextReadiness)
      setCapabilities(nextCapabilities)
      setPlans(nextPlans)
      setPlansLoaded(true)
      setRuns(page.runs)
      setSelectedRunId((current) => page.runs.some((run) => run.run_id === current)
        ? current
        : page.runs[0]?.run_id ?? null)
    } catch (loadError) {
      // Preserve the last daemon snapshot: a connection failure is not a run transition.
      setError(`${errorMessage(loadError, 'Failed to refresh runs')}. Existing run data remains visible.`)
      setPlansLoaded(true)
    } finally {
      setRefreshing(false)
    }
  }

  async function loadSelectedRun(nextProjectId: string, runId: string, active = true) {
    try {
      const [run, tail, liteContext] = await Promise.all([
        api.getControlPlaneRun(nextProjectId, runId),
        api.listRunEvents(nextProjectId, runId, { limit: MAX_TIMELINE_EVENTS }),
        api.getRunContext(nextProjectId, runId, 'lite'),
      ])
      if (!active || selectedRunRef.current !== runId) return
      setRuns((current) => upsertRun(current, run))
      setEvents((current) => mergeEvents(current, tail))
      setContext(liteContext)
    } catch (loadError) {
      if (active) setError(errorMessage(loadError, 'Failed to load run details'))
    }
  }

  async function refreshSelectedRun() {
    if (!projectId || !selectedRunId) return
    await loadSelectedRun(projectId, selectedRunId)
  }

  function hasSafeControl(control: string): boolean {
    return Boolean(
      capabilities?.controls.includes(control)
      && capabilities.control_safety[control] === 'safe',
    )
  }

  async function selectContextLevel(level: 'lite' | 'full') {
    setContextLevel(level)
    if (level === 'full') {
      setFullContextAcknowledged(false)
      return
    }
    await loadContext('lite', false)
  }

  async function loadContext(level = contextLevel, fullScope = fullContextAcknowledged) {
    if (!projectId || !selectedRunId) return
    if (level === 'full' && !fullScope) {
      setError('Confirm the Full-context disclosure before requesting it.')
      return
    }
    try {
      setBusyAction('context')
      setError(null)
      setContext(await api.getRunContext(projectId, selectedRunId, level, fullScope))
    } catch (contextError) {
      setError(errorMessage(contextError, 'Failed to load run context'))
    } finally {
      setBusyAction(null)
    }
  }

  async function handleStart() {
    if (!projectId || !startPlanPath) return
    const startRequest = {
      plan_path: startPlanPath.trim(),
      ...(startWorkflow.trim() ? { workflow_name: startWorkflow.trim() } : {}),
      ...(startTeam.trim() ? { team: startTeam.trim() } : {}),
      ...(startMaxTurns.trim() ? { max_turns: Number(startMaxTurns.trim()) } : {}),
    }
    const intent = { project_id: projectId, ...startRequest }
    try {
      setBusyAction('start')
      setError(null)
      setFeedback(null)
      const response = await api.startControlPlaneRun(projectId, startRequest, getPendingWriteKey('start', intent))
      clearPendingWriteKey('start', intent)
      await handleStartResponse(response, 'Start request')
    } catch (startError) {
      setError(errorMessage(startError, 'Failed to start run'))
    } finally {
      setBusyAction(null)
    }
  }

  async function handleStartResponse(response: StartRunResponse, action: string) {
    if (response.startup_question) {
      setStartupQuestion(response.startup_question)
      setFeedback(`${action} is awaiting a startup answer. No workflow has been started.`)
      return
    }
    if (!response.result || !projectId) return
    const result = response.result
    setStartupQuestion(null)
    setFeedback(result.created
      ? `${action} created run ${result.run_id}.`
      : `${action} replay returned existing run ${result.run_id}; no duplicate was created.`)
    await loadDashboard(projectId)
    setSelectedRunId(result.run_id)
  }

  async function handleStartupAnswer(answer: string | number | boolean) {
    if (!projectId || !startupQuestion) return
    const intent = {
      project_id: projectId,
      question_id: startupQuestion.question_id,
      answer,
    }
    try {
      setBusyAction('startup-answer')
      setError(null)
      const response = await api.answerStartupQuestion(
        projectId,
        startupQuestion.question_id,
        answer,
        getPendingWriteKey('startup-answer', intent),
      )
      clearPendingWriteKey('startup-answer', intent)
      await handleStartResponse(response, 'Startup answer')
    } catch (answerError) {
      setError(errorMessage(answerError, 'Failed to answer startup question'))
    } finally {
      setBusyAction(null)
    }
  }

  async function handleControl() {
    if (!projectId || !selectedRun) return
    const request = { expected_revision: selectedRun.revision } as Parameters<typeof api.controlControlPlaneRun>[2]
    const requestedMaxTurns = Number(controlMaxTurns)
    if (hasSafeControl('max_turns') && Number.isInteger(requestedMaxTurns) && requestedMaxTurns > 0
      && requestedMaxTurns !== selectedRun.max_turns) {
      request.max_turns = requestedMaxTurns
    }
    if (hasSafeControl('team') && controlTeam && controlTeam !== selectedRun.team) request.team = controlTeam
    const nextRoleWorkers = Object.fromEntries(
      Object.entries(roleWorkers)
        .map(([role, worker]) => [role, worker.trim()])
        .filter(([, worker]) => worker)
        .sort(([left], [right]) => left.localeCompare(right)),
    )
    if (hasSafeControl('role_selectors') && Object.keys(nextRoleWorkers).length) {
      request.role_selectors = nextRoleWorkers
    }
    if (Object.keys(request).length === 1) {
      setFeedback('Choose a safe control change before applying it.')
      return
    }
    const intent = {
      project_id: projectId,
      run_id: selectedRun.run_id,
      expected_revision: request.expected_revision,
      ...(request.max_turns === undefined ? {} : { max_turns: request.max_turns }),
      ...(request.team === undefined ? {} : { team: request.team }),
      ...(request.role_selectors === undefined ? {} : { role_selectors: request.role_selectors }),
    }
    try {
      setBusyAction('control')
      setError(null)
      const response = await api.controlControlPlaneRun(projectId, selectedRun.run_id, request, getPendingWriteKey('control', intent))
      clearPendingWriteKey('control', intent)
      setRuns((current) => upsertRun(current, response.run))
      setFeedback(response.changed ? 'Safe controls applied.' : 'Control request was already applied.')
    } catch (controlError) {
      if (apiErrorCode(controlError) === 'revision_conflict') {
        await refreshSelectedRun()
        setFeedback('Another operator changed this run. The dashboard refreshed the current revision; review and retry.')
      } else if (apiErrorCode(controlError) === 'restart_required') {
        setFeedback('The server requires a restart for that change. Start a new run or explicitly resume an eligible run.')
      } else {
        setError(errorMessage(controlError, 'Failed to apply controls'))
      }
    } finally {
      setBusyAction(null)
    }
  }

  async function handleOwnerStop() {
    if (!projectId || !selectedRun) return
    const intent = {
      project_id: projectId,
      run_id: selectedRun.run_id,
      expected_revision: selectedRun.revision,
    }
    try {
      setBusyAction('owner-stop')
      setError(null)
      const stopped = await api.ownerStopControlPlaneRun(
        projectId,
        selectedRun.run_id,
        selectedRun.revision,
        getPendingWriteKey('owner-stop', intent),
      )
      clearPendingWriteKey('owner-stop', intent)
      setRuns((current) => upsertRun(current, stopped))
      setFeedback(`Owner stop recorded for ${stopped.run_id}.`)
      setConfirmOwnerStop(false)
    } catch (stopError) {
      setError(errorMessage(stopError, 'Failed to stop run'))
    } finally {
      setBusyAction(null)
    }
  }

  async function handleResume() {
    if (!projectId || !selectedRun) return
    const sourceRun = selectedRun.run_id
    const intent = { project_id: projectId, source_run_id: sourceRun }
    try {
      setBusyAction('resume')
      setError(null)
      const continuation = await api.resumeControlPlaneRun(projectId, sourceRun, getPendingWriteKey('resume', intent))
      clearPendingWriteKey('resume', intent)
      setFeedback(continuation.created
        ? `Continuation ${continuation.run_id} was created from source run ${sourceRun}. The source remains separate.`
        : `Replay returned continuation ${continuation.run_id} from source run ${sourceRun}; no duplicate was created. The source remains separate.`)
      setConfirmResume(false)
      await loadDashboard(projectId)
      setSelectedRunId(continuation.run_id)
    } catch (resumeError) {
      setError(errorMessage(resumeError, 'Failed to resume run'))
    } finally {
      setBusyAction(null)
    }
  }

  const canMutate = selectedRun?.ownership === 'control_plane'
  const canResume = canMutate && selectedRun?.status === 'needs_attention'
  const readinessLabel = readiness === null
    ? 'Readiness unavailable'
    : readiness.ready ? 'Daemon ready' : 'Daemon not ready'

  if (loading) {
    return <div className="card dashboard-loading"><div className="spinner" />Loading daemon-owned runs…</div>
  }

  return (
    <div className="run-dashboard">
      <div className="run-dashboard-header">
        <div>
          <h2>Run dashboard</h2>
          <p className="text-sm text-dim">Persistent server records remain visible if the daemon connection is interrupted.</p>
        </div>
        <button className="btn btn-secondary btn-sm" onClick={() => projectId && void loadDashboard(projectId)} disabled={refreshing || !projectId}>
          {refreshing ? 'Refreshing…' : 'Refresh'}
        </button>
      </div>

      {error && <div className="error-message">{error}</div>}
      {feedback && <div className="success-message">{feedback}</div>}

      <label className="dashboard-field">
        <span>Control-plane project</span>
        <select className="input" aria-label="Control-plane project" value={projectId ?? ''} onChange={(event) => setProjectId(event.target.value || null)}>
          {projects.length === 0 && <option value="">No control-plane projects available</option>}
          {projects.map((project) => <option key={project.project_id} value={project.project_id}>{project.project_id} — {project.root}</option>)}
        </select>
      </label>

      {selectedProject && capabilities && (
        <div className="dashboard-capabilities card">
          <div><strong>{selectedProject.project_id}</strong><span className="text-dim mono">{selectedProject.root}</span><span className={`status-pill ${readiness?.ready ? '' : 'status-awaiting'}`}>{readinessLabel}</span></div>
          <div className="text-xs text-dim">Workflows: {capabilities.workflows.join(', ') || 'not reported'} · Teams: {capabilities.teams.join(', ') || 'not reported'}</div>
        </div>
      )}

      {startupQuestion && (
        <section className="card startup-question" aria-label="Startup question">
          <div className="status-pill status-awaiting">Awaiting startup answer</div>
          <h3>{startupQuestion.message}</h3>
          <p className="text-sm text-dim">No workflow unit exists until an answer is accepted.</p>
          <div className="dashboard-actions">
            {startupQuestion.choices.map((choice) => (
              <button key={choice} className="btn btn-primary" disabled={busyAction === 'startup-answer'} onClick={() => void handleStartupAnswer(choice)}>{choice}</button>
            ))}
            {Object.entries(startupQuestion.options).map(([key, label]) => (
              <button key={key} className="btn btn-secondary" disabled={busyAction === 'startup-answer'} onClick={() => void handleStartupAnswer(key)}>{label}</button>
            ))}
          </div>
        </section>
      )}

      <section className="card start-run-form">
        <div>
          <h3>Start a daemon-owned run</h3>
          <p className="text-sm text-dim">The server owns validation and returns either a run or a startup question.</p>
        </div>
        <label className="dashboard-field"><span>Plan</span>
          <select className="input" aria-label="Run plan" value={startPlanPath} onChange={(event) => setStartPlanPath(event.target.value)}>
            <option value="">Select an allowed plan</option>
            {plans.map((plan) => <option key={plan.path} value={plan.path}>{plan.path}</option>)}
          </select>
        </label>
        <div className="dashboard-form-grid">
          <label className="dashboard-field"><span>Workflow</span>
            <select className="input" aria-label="Run workflow" value={startWorkflow} onChange={(event) => setStartWorkflow(event.target.value)}>
              <option value="">Server default</option>
              {capabilities?.workflows.map((workflow) => <option key={workflow} value={workflow}>{workflow}</option>)}
            </select>
          </label>
          <label className="dashboard-field"><span>Team</span>
            <select className="input" aria-label="Run team" value={startTeam} onChange={(event) => setStartTeam(event.target.value)}>
              <option value="">Server default</option>
              {capabilities?.teams.map((team) => <option key={team} value={team}>{team}</option>)}
            </select>
          </label>
          <label className="dashboard-field"><span>Max turns</span>
            <input className="input" aria-label="Run max turns" type="number" min="1" value={startMaxTurns} onChange={(event) => setStartMaxTurns(event.target.value)} />
          </label>
        </div>
        <button className="btn btn-primary" onClick={() => void handleStart()} disabled={!projectId || !startPlanPath || busyAction === 'start'}>
          {busyAction === 'start' ? 'Starting…' : 'Start run'}
        </button>
      </section>

      <div className="dashboard-columns">
        <section className="card run-list" aria-label="Project runs">
          <div className="section-heading"><h3>Project runs</h3><span className="text-xs text-dim">{runs.length} recorded</span></div>
          {runs.length === 0 ? <p className="text-sm text-dim">No runs are recorded for this project.</p> : runs.map((run) => (
            <button className={`content-button run-list-item ${selectedRunId === run.run_id ? 'selected' : ''}`} key={run.run_id} onClick={() => setSelectedRunId(run.run_id)}>
              <span className="mono">{run.run_id}</span>
              <span className="status-pill">{statusLabel(run)}</span>
              <span className="text-xs text-dim">{run.workflow_name ?? 'workflow not reported'} · {run.current_step ?? 'step not reported'}</span>
            </button>
          ))}
        </section>

        <section className="card run-detail" aria-label="Run details">
          {!selectedRun ? <p className="text-sm text-dim">Select a recorded run to inspect its server status and events.</p> : <>
            <div className="section-heading"><div><h3>Run {selectedRun.run_id}</h3><p className="text-sm text-dim">Canonical identity · revision {selectedRun.revision}</p></div><span className="status-pill">{statusLabel(selectedRun)}</span></div>
            {selectedRun.ownership === 'legacy' && <div className="notice">Legacy record classified as interrupted and read-only. It is never treated as a live workflow.</div>}
            {selectedRun.status === 'needs_attention' && <div className="notice">This run needs attention. A disconnected dashboard did not stop it; explicit resume is required when safe.</div>}
            {selectedRun.reason && <div className="notice">{selectedRun.reason}</div>}

            <dl className="run-metadata">
              <div><dt>Ownership</dt><dd>{selectedRun.ownership}</dd></div>
              <div><dt>Unit / status</dt><dd className="mono">{selectedRun.unit_name ?? 'Not reported'} · {selectedRun.status}</dd></div>
              <div><dt>Workflow / team</dt><dd>{selectedRun.workflow_name ?? 'Not reported'} · {selectedRun.team ?? 'Not reported'}</dd></div>
              <div><dt>Upgrade chain</dt><dd>{selectedRun.team ? capabilities?.team_upgrade_chains[selectedRun.team]?.join(' → ') || 'Not reported' : 'Not reported'}</dd></div>
              <div><dt>Current step / turns</dt><dd>{selectedRun.current_step ?? 'Not reported'} · {selectedRun.turns_completed ?? '0'} / {selectedRun.max_turns ?? 'Not reported'}</dd></div>
              <div><dt>Plan</dt><dd className="mono">{textEvidence(selectedRun, 'plan_path') !== 'Not reported' ? textEvidence(selectedRun, 'plan_path') : contextText(context, 'plan_path')}</dd></div>
              <div><dt>Worktree</dt><dd className="mono">{textEvidence(selectedRun, 'worktree_path') !== 'Not reported' ? textEvidence(selectedRun, 'worktree_path') : contextText(context, 'worktree_path')}</dd></div>
              <div><dt>Branch</dt><dd className="mono">{textEvidence(selectedRun, 'branch') !== 'Not reported' ? textEvidence(selectedRun, 'branch') : contextText(context, 'branch')}</dd></div>
              <div><dt>Recorded at</dt><dd>{timestamp(selectedRun.evidence.manifest_created_at ?? selectedRun.evidence.updated_at)}</dd></div>
            </dl>

            <section className="dashboard-section">
              <div className="section-heading"><h4>Safe controls</h4><span className="text-xs text-dim">Server capability and revision gated</span></div>
              {!canMutate && <div className="notice">Actions are disabled because the server classifies this as a legacy read-only record.</div>}
              {capabilities && Object.entries(capabilities.control_safety).filter(([, safety]) => safety === 'restart_required').map(([control]) => (
                <div className="text-xs text-dim" key={control}>{control.replace(/_/g, ' ')} requires restart; it is not offered as a live control.</div>
              ))}
              <div className="dashboard-form-grid">
                <label className="dashboard-field"><span>Max turns</span><input className="input" aria-label="Control max turns" type="number" min="1" value={controlMaxTurns} disabled={!canMutate || !hasSafeControl('max_turns')} onChange={(event) => setControlMaxTurns(event.target.value)} /></label>
                <label className="dashboard-field"><span>Team</span><select className="input" aria-label="Control team" value={controlTeam} disabled={!canMutate || !hasSafeControl('team')} onChange={(event) => setControlTeam(event.target.value)}><option value="">No team</option>{capabilities?.teams.map((team) => <option key={team} value={team}>{team}</option>)}</select></label>
              </div>
              {capabilities?.roles.map((role) => <label className="dashboard-field" key={role}><span>Worker selector for {role}</span><input className="input" aria-label={`Worker selector for ${role}`} placeholder="Fully qualified selector, or leave blank" disabled={!canMutate || !hasSafeControl('role_selectors')} value={roleWorkers[role] ?? ''} onChange={(event) => setRoleWorkers((current) => ({ ...current, [role]: event.target.value }))} /></label>)}
              <div className="dashboard-actions"><button className="btn btn-secondary" onClick={() => void handleControl()} disabled={!canMutate || busyAction === 'control'}>{busyAction === 'control' ? 'Applying…' : 'Apply safe controls'}</button></div>
            </section>

            <section className="dashboard-section dashboard-actions">
              {hasSafeControl('owner_stop') && canMutate && <>
                {!confirmOwnerStop ? <button className="btn btn-danger" onClick={() => setConfirmOwnerStop(true)}>Owner stop…</button> : <div className="confirmation"><span>Confirm owner stop for {selectedRun.run_id}. This control is recorded by the server.</span><button className="btn btn-danger" disabled={busyAction === 'owner-stop'} onClick={() => void handleOwnerStop()}>Confirm stop</button><button className="btn btn-secondary" onClick={() => setConfirmOwnerStop(false)}>Cancel</button></div>}
              </>}
              {canResume && <>
                {!confirmResume ? <button className="btn btn-primary" onClick={() => setConfirmResume(true)}>Resume as new run…</button> : <div className="confirmation"><span>Confirm explicit resume. Source {selectedRun.run_id} remains visible; the server creates a distinct continuation run.</span><button className="btn btn-primary" disabled={busyAction === 'resume'} onClick={() => void handleResume()}>Confirm resume</button><button className="btn btn-secondary" onClick={() => setConfirmResume(false)}>Cancel</button></div>}
              </>}
            </section>

            <section className="dashboard-section">
              <div className="section-heading"><h4>Context</h4><span className="text-xs text-dim">{context?.level ?? 'not loaded'}</span></div>
              <div className="dashboard-actions"><button className={`btn btn-sm ${contextLevel === 'lite' ? 'btn-primary' : 'btn-secondary'}`} onClick={() => void selectContextLevel('lite')}>Lite — bounded operational summary</button><button className={`btn btn-sm ${contextLevel === 'full' ? 'btn-primary' : 'btn-secondary'}`} disabled={!capabilities?.context_levels.includes('full')} onClick={() => void selectContextLevel('full')}>Full — disclosed scoped detail</button></div>
              {contextLevel === 'full' && <label className="context-disclosure"><input type="checkbox" checked={fullContextAcknowledged} onChange={(event) => setFullContextAcknowledged(event.target.checked)} /> I understand Full context may expose additional bounded run metadata and request it for this authenticated session only.</label>}
              <div className="dashboard-actions"><button className="btn btn-secondary btn-sm" disabled={busyAction === 'context' || (contextLevel === 'full' && !fullContextAcknowledged)} onClick={() => void loadContext()}>Load {contextLevel} context</button></div>
              {context && <pre className="dashboard-payload">{JSON.stringify(context.data, null, 2)}</pre>}
            </section>

            <section className="dashboard-section">
              <div className="section-heading"><div><h4>Event timeline</h4><span className="text-xs text-dim">{events.length} bounded events · stream {streamState}</span></div><button className="btn btn-secondary btn-sm" onClick={() => void refreshSelectedRun()}>Refresh status</button></div>
              {streamNotice && <div className="notice">{streamNotice}</div>}
              {events.length === 0 ? <p className="text-sm text-dim">No bounded events are available yet.</p> : <div className="run-timeline">{events.map((event) => <article className="timeline-event" key={event.sequence}><div><strong>{event.event_type.replace(/_/g, ' ')}</strong><span className="text-xs text-dim">#{event.sequence} · {timestamp(event.timestamp)}</span></div><pre>{JSON.stringify(event.data, null, 2)}</pre></article>)}</div>}
            </section>
          </>}
        </section>
      </div>
    </div>
  )
}
