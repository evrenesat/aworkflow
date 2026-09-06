import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type {
  ConfigValidationIssue,
  ControlPlaneCapabilities,
  ControlPlanePlan,
  ControlPlaneReadiness,
  GuidedFormProjection,
  GuidedProfileSummary,
  RunContext,
  RunEvent,
  RunStatus,
  StartRunRequest,
  StartRunResponse,
  StartupQuestion,
} from '../types'
import { ApiError } from '../api'
import * as api from '../api'
import { Combobox } from './Combobox'

const MAX_TIMELINE_EVENTS = 100
/** Bounded wait for exact source inactivity before a successor start. */
const RESTART_POLL_INTERVAL_MS = 1_000
const MAX_RESTART_POLLS = 30
const TERMINAL_RUN_STATUSES = new Set(['completed', 'done', 'failed', 'owner_stopped', 'interrupted'])
const CONFIRM_QUESTION_KINDS = new Set(['confirm_recovery', 'confirm_worktree_dirty'])
const MAX_EXTRA_INSTRUCTION_ITEMS = 8
const MAX_EXTRA_INSTRUCTION_LENGTH = 512
const MAX_EXTRA_INSTRUCTIONS_TOTAL = 4_096

interface RunDashboardProps {
  /** The registered project whose runs are shown; its sole project authority. */
  projectId: string
  initialPlanPath: string | null
  onInitialPlanHandled: () => void
  /** Test-only override; production callers retain the bounded 1s–30poll wait. */
  restartPollIntervalMs?: number
  pendingSuccessorStart?: PendingSuccessorStart | null
  onPendingSuccessorStartChange?: (pending: PendingSuccessorStart | null) => void
  /** Opens Settings for the same project when launch prerequisites are missing. */
  onOpenSettings?: () => void
}

/**
 * The projection of the committed configuration pair, fetched through the pure
 * form endpoint.  The run form resolves launch values only from this plus the
 * canonical daemon capabilities — never from unsaved settings drafts.
 */
interface CommittedProjection {
  validationState: 'ready' | 'configuration_required' | 'invalid'
  form: GuidedFormProjection | null
  syntaxIssues: ConfigValidationIssue[]
}

interface RoleResolution {
  role: string
  selector: string | null
  source: 'team' | 'global' | 'missing'
}

/** Resolves one exact role through the team override, then the global selector. */
function resolveStepRole(
  role: string,
  teamRoles: Record<string, string>,
  globalRoles: Record<string, string>,
): RoleResolution {
  const teamSelector = teamRoles[role]
  if (typeof teamSelector === 'string' && teamSelector.trim()) {
    return { role, selector: teamSelector, source: 'team' as const }
  }
  const globalSelector = globalRoles[role]
  if (typeof globalSelector === 'string' && globalSelector.trim()) {
    return { role, selector: globalSelector, source: 'global' as const }
  }
  return { role, selector: null, source: 'missing' as const }
}

/**
 * The single max-turns validation shared by the preview and the request:
 * empty keeps the configured/default behavior, and a nonempty override must
 * be a whole number of 1 or greater — anything else blocks the launch.
 */
function maxTurnsProblem(raw: string): string | null {
  const text = raw.trim()
  if (text === '') return null
  const value = Number(text)
  if (!/^\d+$/.test(text) || !Number.isSafeInteger(value) || value < 1) {
    return 'Max turns must be a whole number of 1 or greater — correct or clear the override.'
  }
  return null
}

function selectorProfileSummary(
  selector: string,
  projection: GuidedFormProjection | null,
): GuidedProfileSummary | null {
  const dot = selector.indexOf('.')
  if (dot <= 0 || !projection) return null
  return projection.harnesses[selector.slice(0, dot)]?.[selector.slice(dot + 1)] ?? null
}

/** Model and effort text of a resolved selector; empty when unreported. */
function selectorModelEffortText(
  selector: string,
  projection: GuidedFormProjection | null,
): string {
  const summary = selectorProfileSummary(selector, projection)
  if (!summary) return ''
  if (selector.startsWith('zcode.')) return 'model and effort configured in ZCode'
  return [summary.model, summary.effort ? `effort ${summary.effort}` : null]
    .filter((part): part is string => Boolean(part))
    .join(' · ')
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

function contextRecord(context: RunContext | null, key: string): Record<string, unknown> | null {
  if (!context) return null
  const section = context.data[key]
  return typeof section === 'object' && section !== null ? section as Record<string, unknown> : null
}

function contextText(context: RunContext | null, key: string): string {
  const runMetadata = contextRecord(context, 'run_metadata')
  if (runMetadata) {
    const value = runMetadata[key]
    if (typeof value === 'string' || typeof value === 'number') return String(value)
  }
  const managerContext = contextRecord(context, 'manager_context')
  if (managerContext) {
    const value = managerContext[key]
    if (typeof value === 'string' || typeof value === 'number') return String(value)
  }
  return 'Not reported'
}

function planPathFromContext(context: RunContext | null): string {
  const direct = contextText(context, 'plan_path')
  if (direct !== 'Not reported') return direct
  const runMetadata = contextRecord(context, 'run_metadata')
  for (const key of ['active_plan_path', 'original_plan_path']) {
    if (runMetadata) {
      const value = runMetadata[key]
      if (typeof value === 'string' && value.trim()) return value
    }
  }
  const managerContext = contextRecord(context, 'manager_context')
  const planState = managerContext
    && typeof managerContext.plan_state === 'object' && managerContext.plan_state !== null
    ? managerContext.plan_state as Record<string, unknown>
    : null
  if (planState) {
    const value = planState.active_plan_path
    if (typeof value === 'string' && value.trim()) return value
  }
  return 'Not reported'
}

interface CheckpointSummary {
  complete: boolean
  name: string | null
  index: number | null
  count: number
}

function checkpointSummary(context: RunContext | null): CheckpointSummary | null {
  const managerContext = contextRecord(context, 'manager_context')
  const planState = managerContext
    && typeof managerContext.plan_state === 'object' && managerContext.plan_state !== null
    ? managerContext.plan_state as Record<string, unknown>
    : null
  if (!planState) return null
  const checkpoints = Array.isArray(planState.checkpoints) ? planState.checkpoints : []
  const current = typeof planState.current_checkpoint === 'object' && planState.current_checkpoint !== null
    ? planState.current_checkpoint as Record<string, unknown>
    : null
  const name = current && typeof current.name === 'string' ? current.name : null
  const index = current && typeof current.index === 'number' ? current.index : null
  return { name, index, count: checkpoints.length, complete: planState.is_complete === true }
}

interface ManagerOutcome {
  decision: string | null
  finishedTurn: string | null
  resultText: string | null
}

function boundedText(value: unknown, limit: number): string | null {
  if (typeof value !== 'string' || !value.trim()) return null
  return value.length <= limit ? value : `${value.slice(0, limit)}…`
}

function managerOutcome(context: RunContext | null): ManagerOutcome | null {
  const managerContext = contextRecord(context, 'manager_context')
  if (!managerContext) return null
  let decision: string | null = null
  const extract = Array.isArray(managerContext.run_extract) ? managerContext.run_extract : []
  const decisions = Array.isArray(managerContext.manager_decisions) && managerContext.manager_decisions.length > 0
    ? managerContext.manager_decisions
    : extract.filter((entry) => typeof entry === 'object' && entry !== null && entry.kind === 'manager_decision')
      .map((entry) => ({
        decision_number: entry.number,
        action: entry.routing?.action,
        reason: entry.semantic_summary,
      }))
  if (Array.isArray(decisions) && decisions.length > 0) {
    const last = decisions[decisions.length - 1]
    if (typeof last === 'object' && last !== null) {
      const entry = last as Record<string, unknown>
      const action = typeof entry.action === 'string' ? entry.action : 'unknown action'
      const number = typeof entry.decision_number === 'number' ? ` #${entry.decision_number}` : ''
      const reason = boundedText(entry.reason, 200)
      decision = `Decision${number}: ${action}${reason ? ` — ${reason}` : ''}`
    }
  }
  const finished = typeof managerContext.finished_turn === 'object' && managerContext.finished_turn !== null
    ? managerContext.finished_turn as Record<string, unknown>
    : null
  let finishedTurn: string | null = null
  let resultText: string | null = null
  if (finished) {
    const parts: string[] = []
    if (typeof finished.turn_number === 'number') parts.push(`turn ${finished.turn_number}`)
    if (typeof finished.step_name === 'string') parts.push(finished.step_name)
    if (typeof finished.status === 'string') parts.push(finished.status)
    if (typeof finished.returncode === 'number') parts.push(`exit ${finished.returncode}`)
    finishedTurn = parts.length ? parts.join(' · ') : null
    const semantic = typeof finished.semantic_result === 'object' && finished.semantic_result !== null
      ? finished.semantic_result as Record<string, unknown>
      : null
    resultText = semantic ? boundedText(semantic.result, 240) : boundedText(finished.error, 240)
  }
  if (!decision && !finishedTurn && !resultText) return null
  return { decision, finishedTurn, resultText }
}

function latestControlOverride(events: RunEvent[]): Record<string, unknown> | null {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    if (events[index].event_type === 'control_changed') return events[index].data
  }
  return null
}

function overrideText(override: Record<string, unknown> | null, key: string): string {
  const value = override?.[key]
  if (typeof value === 'string' && value.trim()) return value
  if (typeof value === 'number') return String(value)
  return 'Not reported'
}

function overrideRoles(override: Record<string, unknown> | null): Record<string, string> {
  const roles = override?.roles
  if (typeof roles !== 'object' || roles === null) return {}
  const result: Record<string, string> = {}
  for (const [role, selector] of Object.entries(roles as Record<string, unknown>)) {
    if (typeof selector === 'string') result[role] = selector
  }
  return result
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

function parseElapsedFrom(iso: string | undefined, nowMs: number): string | null {
  if (!iso) return null
  const start = new Date(iso).getTime()
  if (Number.isNaN(start)) return null
  const seconds = Math.max(0, Math.floor((nowMs - start) / 1_000))
  const minutes = Math.floor(seconds / 60)
  const hours = Math.floor(minutes / 60)
  if (hours > 0) return `${hours}h ${minutes % 60}m`
  if (minutes > 0) return `${minutes}m ${seconds % 60}s`
  return `${seconds}s`
}

type RestartPhase = 'confirming' | 'stopping' | 'waiting' | 'starting' | 'failed' | 'unknown'

export interface PendingSuccessorStart {
  projectId: string
  sourceRunId: string
  request: StartRunRequest
  idempotencyKey: string
}

/**
 * Executable steps of the effective workflow, resolved from the committed
 * projection first and the daemon-admitted capabilities second.
 */
function configuredWorkflowSteps(
  projection: GuidedFormProjection | null,
  capabilities: ControlPlaneCapabilities | null,
  workflow: string,
): string[] {
  if (!workflow) return []
  const projected = projection?.workflows[workflow]
  if (projected?.executable_steps && projected.executable_steps.length > 0) return projected.executable_steps
  const admitted = capabilities?.workflow_details?.[workflow]?.executable_steps
  if (admitted && admitted.length > 0) return admitted
  return []
}

export function RunDashboard({ projectId, initialPlanPath, onInitialPlanHandled, restartPollIntervalMs, pendingSuccessorStart: suppliedPendingSuccessor, onPendingSuccessorStartChange, onOpenSettings }: RunDashboardProps) {
  const [projectAvailable, setProjectAvailable] = useState<boolean | null>(null)
  const [capabilities, setCapabilities] = useState<ControlPlaneCapabilities | null>(null)
  const [readiness, setReadiness] = useState<ControlPlaneReadiness | null>(null)
  const [plans, setPlans] = useState<ControlPlanePlan[]>([])
  const [plansLoaded, setPlansLoaded] = useState(false)
  const [committed, setCommitted] = useState<CommittedProjection | null>(null)
  const [committedError, setCommittedError] = useState<string | null>(null)
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
  const [startStep, setStartStep] = useState('')
  const [startMaxTurns, setStartMaxTurns] = useState('')
  const [startExtraInstructions, setStartExtraInstructions] = useState('')
  const [controlMaxTurns, setControlMaxTurns] = useState('')
  const [controlTeam, setControlTeam] = useState('')
  const [roleSelectors, setRoleSelectors] = useState<Record<string, string>>({})
  const [confirmOwnerStop, setConfirmOwnerStop] = useState(false)
  const [confirmResume, setConfirmResume] = useState(false)
  const [restartPhase, setRestartPhase] = useState<RestartPhase | null>(null)
  const [restartNotice, setRestartNotice] = useState<string | null>(null)
  const [localPendingSuccessor, setLocalPendingSuccessor] = useState<PendingSuccessorStart | null>(null)
  const pendingSuccessorStart = suppliedPendingSuccessor === undefined ? localPendingSuccessor : suppliedPendingSuccessor
  const setPendingSuccessorStart = useCallback((pending: PendingSuccessorStart | null) => {
    setLocalPendingSuccessor(pending)
    onPendingSuccessorStartChange?.(pending)
  }, [onPendingSuccessorStartChange])
  const [elapsedNow, setElapsedNow] = useState(() => Date.now())
  // New run is a compact disclosure: it opens for an exact plan handoff, when
  // no runs exist, or when a frozen successor draft needs attention.
  const [newRunOpen, setNewRunOpen] = useState(initialPlanPath !== null)
  const [advancedOpen, setAdvancedOpen] = useState(false)
  const [refreshNonce, setRefreshNonce] = useState(0)
  const selectedRunRef = useRef<string | null>(null)
  const snapshotRequestRef = useRef(0)
  const controlsForRunRef = useRef<string | null>(null)
  const previousStreamStateRef = useRef<api.StreamState>('stopped')
  const { getKey: getPendingWriteKey, clearKey: clearPendingWriteKey, clearAll: clearPendingWriteKeys } = usePendingWriteKeys()

  const selectedRun = useMemo(
    () => runs.find((run) => run.run_id === selectedRunId) ?? null,
    [runs, selectedRunId],
  )

  const successorRunIds = useMemo(
    () => (selectedRun
      ? runs.filter((run) => run.restarted_from_run_id === selectedRun.run_id).map((run) => run.run_id)
      : []),
    [runs, selectedRun],
  )

  const controlOverride = useMemo(() => latestControlOverride(events), [events])

  const extraInstructions = useMemo(
    () => startExtraInstructions.split('\n').map((line) => line.trim()).filter((line) => line.length > 0),
    [startExtraInstructions],
  )

  const extraInstructionProblem = useMemo(() => {
    if (extraInstructions.length > MAX_EXTRA_INSTRUCTION_ITEMS) {
      return `At most ${MAX_EXTRA_INSTRUCTION_ITEMS} instruction lines are accepted.`
    }
    if (extraInstructions.some((line) => line.length > MAX_EXTRA_INSTRUCTION_LENGTH)) {
      return `Each instruction line is limited to ${MAX_EXTRA_INSTRUCTION_LENGTH} characters.`
    }
    if (extraInstructions.reduce((total, line) => total + line.length, 0) > MAX_EXTRA_INSTRUCTIONS_TOTAL) {
      return `Instructions are limited to ${MAX_EXTRA_INSTRUCTIONS_TOTAL} characters in total.`
    }
    return null
  }, [extraInstructions])

  const selectedRunIsActive = Boolean(
    selectedRun
    && selectedRun.ownership === 'control_plane'
    && !TERMINAL_RUN_STATUSES.has(selectedRun.status),
  )

  useEffect(() => {
    selectedRunRef.current = selectedRunId
  }, [selectedRunId])

  useEffect(() => {
    clearPendingWriteKeys()
  }, [projectId, selectedRunId, clearPendingWriteKeys])

  useEffect(() => {
    if (!selectedRun || controlsForRunRef.current === selectedRun.run_id) return
    controlsForRunRef.current = selectedRun.run_id
    setControlMaxTurns(selectedRun.max_turns?.toString() ?? '')
    setControlTeam(selectedRun.team ?? '')
    setRoleSelectors({})
    setConfirmOwnerStop(false)
    setConfirmResume(false)
    if (!pendingSuccessorStart) {
      setRestartPhase(null)
      setRestartNotice(null)
    }
  }, [selectedRun, pendingSuccessorStart])

  // A live elapsed clock only ticks while a nonterminal owned run is selected.
  useEffect(() => {
    if (!selectedRunIsActive) return
    setElapsedNow(Date.now())
    const ticker = setInterval(() => setElapsedNow(Date.now()), 1_000)
    return () => clearInterval(ticker)
  }, [selectedRunIsActive])

  // A frozen successor draft lives in the New run form; surface it on remount.
  useEffect(() => {
    if (pendingSuccessorStart) setNewRunOpen(true)
  }, [pendingSuccessorStart])

  // The registered project id is the sole authority: no root matching, no
  // internal switcher, and never a fallback to another control-plane project.
  useEffect(() => {
    let active = true
    void (async () => {
      try {
        setLoading(true)
        setError(null)
        setProjectAvailable(null)
        const [available, readinessState] = await Promise.all([
          api.listControlPlaneProjects(),
          api.getControlPlaneReadiness(),
        ])
        if (!active) return
        setReadiness(readinessState)
        const availableHere = available.some((project) => project.project_id === projectId)
        setProjectAvailable(availableHere)
        if (availableHere) await loadDashboard(projectId)
      } catch (loadError) {
        if (active) setError(errorMessage(loadError, 'Failed to load control-plane projects'))
      } finally {
        if (active) setLoading(false)
      }
    })()
    return () => {
      active = false
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- reloads per project and explicit refresh only
  }, [projectId, refreshNonce])

  useEffect(() => {
    if (!initialPlanPath || !plansLoaded) return
    const handedOff = plans.find((plan) => plan.path === initialPlanPath)
    if (handedOff && handedOff.status === 'in_progress') {
      setStartPlanPath(initialPlanPath)
    } else if (handedOff) {
      setStartPlanPath('')
      // Only a saved Ready plan may enter launch: a Draft or Done handoff is
      // cleared with the existing promotion guidance instead of a no-op.
      setFeedback(handedOff.status === 'done'
        ? `${initialPlanPath} is done and kept for the record, so it cannot start a run. Create a new plan and move it through Draft → Ready.`
        : `${initialPlanPath} is still a draft — move it to Ready (in progress) in Plans before running it.`)
    } else {
      setStartPlanPath('')
      setFeedback('Choose a Ready plan before starting a run.')
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
    previousStreamStateRef.current = 'stopped'
    let refreshTimer: ReturnType<typeof setTimeout> | null = null
    let refreshingSnapshot = true
    let refreshRequested = false
    const scheduleSnapshotRefresh = () => {
      if (!active) return
      refreshRequested = true
      if (refreshTimer || refreshingSnapshot) return
      refreshTimer = setTimeout(() => {
        refreshTimer = null
        refreshRequested = false
        refreshingSnapshot = true
        void loadSelectedRun(projectId, selectedRunId, () => active, true).finally(() => {
          refreshingSnapshot = false
          if (refreshRequested) scheduleSnapshotRefresh()
        })
      }, 100)
    }
    void loadSelectedRun(projectId, selectedRunId, () => active).finally(() => {
      refreshingSnapshot = false
      if (refreshRequested) scheduleSnapshotRefresh()
    })
    const unsubscribe = api.subscribeToRunEvents({
      projectId,
      runId: selectedRunId,
      onEvents: (incoming) => {
        if (!active || selectedRunRef.current !== selectedRunId) return
        setEvents((current) => mergeEvents(current, incoming))
        if (incoming.length > 0) scheduleSnapshotRefresh()
      },
      onError: (streamError) => {
        if (!active) return
        setStreamNotice(`${streamError.message} The displayed run status is unchanged.`)
      },
      onStateChange: (state) => {
        if (!active) return
        const previous = previousStreamStateRef.current
        previousStreamStateRef.current = state
        setStreamState(state)
        if (state === 'connected') {
          setStreamNotice(null)
          if (previous === 'reconnecting') {
            // The stream reconnected after a gap: refresh canonical status so
            // the snapshot reflects anything missed while disconnected.
            scheduleSnapshotRefresh()
          }
        }
      },
    })
    return () => {
      active = false
      if (refreshTimer) clearTimeout(refreshTimer)
      unsubscribe()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- resubscribes only per project/run, not per render
  }, [projectId, selectedRunId])

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
      if (page.runs.length === 0) setNewRunOpen(true)
      // The launch form projects the committed pair through the pure form
      // endpoint; a projection failure only blocks launch, never the runs.
      try {
        const committedPair = await api.getProjectConfig(nextProjectId)
        const projected = await api.postProjectConfigForm(nextProjectId, {
          aflow_toml: committedPair.aflow_toml,
          workflows_toml: committedPair.workflows_toml,
        })
        setCommitted({
          validationState: projected.validation.state,
          form: projected.form,
          syntaxIssues: projected.syntax_issues,
        })
        setCommittedError(null)
      } catch (projectionError) {
        setCommitted(null)
        setCommittedError(errorMessage(projectionError, 'Failed to read the committed configuration'))
      }
    } catch (loadError) {
      // Preserve the last daemon snapshot: a connection failure is not a run transition.
      setError(`${errorMessage(loadError, 'Failed to refresh runs')}. Existing run data remains visible.`)
      setPlansLoaded(true)
    } finally {
      setRefreshing(false)
    }
  }

  async function loadSelectedRun(
    nextProjectId: string,
    runId: string,
    isActive = () => true,
    preserveFullContext = false,
  ) {
    const requestNumber = ++snapshotRequestRef.current
    try {
      const [run, tail, liteContext] = await Promise.all([
        api.getControlPlaneRun(nextProjectId, runId),
        preserveFullContext ? Promise.resolve([]) : api.listRunEvents(nextProjectId, runId, { limit: MAX_TIMELINE_EVENTS }),
        api.getRunContext(nextProjectId, runId, 'lite'),
      ])
      if (!isActive() || selectedRunRef.current !== runId || requestNumber !== snapshotRequestRef.current) return
      setRuns((current) => upsertRun(current, run))
      setEvents((current) => mergeEvents(current, tail))
      setContext((current) => preserveFullContext && current?.level === 'full' ? current : liteContext)
    } catch (loadError) {
      if (isActive() && selectedRunRef.current === runId && requestNumber === snapshotRequestRef.current) setError(errorMessage(loadError, 'Failed to load run details'))
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

  /** A workflow change keeps only start-step/team values the new workflow admits. */
  function changeStartWorkflow(workflow: string) {
    setStartWorkflow(workflow)
    setStartStep((current) => current && configuredWorkflowSteps(committedForm, capabilities, workflow).includes(current)
      ? current
      : '')
    setStartTeam((current) => (current && teamOptions.includes(current) ? current : ''))
  }

  /**
   * Launch values resolve with explicit precedence: the user override wins,
   * then the committed default (workflow, max turns, workflow default team),
   * and only a value with no configured source is omitted from the request.
   */
  function startRequestFromDraft(restartedFromRunId?: string): StartRunRequest {
    // One validation result for preview and request alike: empty keeps the
    // configured/default value, a valid override wins, and an invalid
    // nonempty override never reaches a request because the launch is
    // disabled while it is displayed.
    const maxTurns = startMaxTurns.trim() === ''
      ? configuredMaxTurns
      : startMaxTurnsProblem === null
        ? Number(startMaxTurns.trim())
        : null
    return {
      plan_path: startPlanPath.trim(),
      ...(effectiveWorkflow ? { workflow_name: effectiveWorkflow } : {}),
      ...(effectiveTeam ? { team: effectiveTeam } : {}),
      ...(startStep.trim() ? { start_step: startStep.trim() } : {}),
      ...(typeof maxTurns === 'number' && Number.isInteger(maxTurns) && maxTurns > 0
        ? { max_turns: maxTurns }
        : {}),
      ...(extraInstructions.length ? { extra_instructions: extraInstructions } : {}),
      ...(restartedFromRunId ? { restarted_from_run_id: restartedFromRunId } : {}),
    }
  }

  async function handleStart() {
    const selectedPlan = plans.find((plan) => plan.path === startPlanPath.trim())
    // Launch admission is lifecycle-checked again at submit time: only a
    // saved Ready (in progress) plan path is ever submitted.
    if (startDisabled || !projectId || !selectedPlan || selectedPlan.status !== 'in_progress') return
    const startRequest = startRequestFromDraft()
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
    if (!response.result) return
    const result = response.result
    setStartupQuestion(null)
    const lineage = result.restarted_from_run_id
      ? ` It records ${result.restarted_from_run_id} as its restart source.`
      : ''
    setFeedback(result.created
      ? `${action} created run ${result.run_id}.${lineage}`
      : `${action} replay returned existing run ${result.run_id}; no duplicate was created.`)
    setNewRunOpen(false)
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
    const effectiveMaxTurns = typeof controlOverride?.max_turns === 'number'
      ? controlOverride.max_turns
      : selectedRun.max_turns
    const effectiveTeam = typeof controlOverride?.team === 'string' && controlOverride.team.trim()
      ? controlOverride.team
      : selectedRun.team
    const request = { expected_revision: selectedRun.revision } as Parameters<typeof api.controlControlPlaneRun>[2]
    const requestedMaxTurns = Number(controlMaxTurns)
    if (hasSafeControl('max_turns') && Number.isInteger(requestedMaxTurns) && requestedMaxTurns > 0
      && requestedMaxTurns !== effectiveMaxTurns) {
      request.max_turns = requestedMaxTurns
    }
    if (hasSafeControl('team') && controlTeam && controlTeam !== effectiveTeam) request.team = controlTeam
    const nextRoleSelectors = Object.fromEntries(
      Object.entries(roleSelectors)
        .map(([role, selector]) => [role, selector.trim()])
        .filter(([, selector]) => selector)
        .sort(([left], [right]) => left.localeCompare(right)),
    )
    if (hasSafeControl('role_selectors') && Object.keys(nextRoleSelectors).length) {
      request.role_selectors = nextRoleSelectors
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
      setFeedback(response.changed
        ? `Safe controls recorded at revision ${response.revision}. The engine applies them at the next safe boundary between turns.`
        : 'Control request was already applied.')
      setRoleSelectors({})
    } catch (controlError) {
      if (apiErrorCode(controlError) === 'revision_conflict') {
        // Keep every edit in place; only the canonical revision is refreshed.
        await refreshSelectedRun()
        setFeedback('Another operator changed this run. Your edits were kept; review the refreshed revision and retry.')
      } else if (apiErrorCode(controlError) === 'restart_required') {
        setFeedback('The server requires a restart for that change. Use the guided workflow restart below.')
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

  /**
   * Wait until the canonical status proves the exact source run is terminal
   * after an explicit owner stop.  Returns null when inactivity could not be
   * proven inside the bounded window; a successor is never started then.
   */
  async function waitUntilSourceInactive(projectIdToCheck: string, runId: string): Promise<RunStatus | null> {
    const intervalMs = restartPollIntervalMs ?? RESTART_POLL_INTERVAL_MS
    for (let poll = 0; poll < MAX_RESTART_POLLS; poll += 1) {
      if (poll > 0) await new Promise((resolve) => setTimeout(resolve, intervalMs))
      try {
        const status = await api.getControlPlaneRun(projectIdToCheck, runId)
        setRuns((current) => upsertRun(current, status))
        if (status.status === 'owner_stopped' && status.launch_phase === 'owner_stopped') return status
      } catch {
        return null
      }
    }
    return null
  }

  function successorStartWasRejected(error: unknown): boolean {
    return error instanceof ApiError && error.status >= 400 && error.status < 500
  }

  async function retryPendingSuccessorStart() {
    if (!pendingSuccessorStart || pendingSuccessorStart.projectId !== projectId) return
    const { projectId: successorProjectId, request, idempotencyKey } = pendingSuccessorStart
    try {
      setRestartPhase('starting')
      setRestartNotice('Retrying the exact successor request with its original idempotency key. The source will not be stopped again.')
      setError(null)
      const response = await api.startControlPlaneRun(successorProjectId, request, idempotencyKey)
      clearPendingWriteKey('start', { project_id: successorProjectId, ...request })
      setPendingSuccessorStart(null)
      setRestartPhase(null)
      setRestartNotice(null)
      await handleStartResponse(response, 'Successor retry')
    } catch (restartError) {
      if (successorStartWasRejected(restartError)) {
        clearPendingWriteKey('start', { project_id: successorProjectId, ...request })
        setPendingSuccessorStart(null)
        setRestartPhase('failed')
        setRestartNotice(`Successor start was rejected: ${errorMessage(restartError, 'start rejected')}. No successor was started by this request. The source remains owner-stopped.`)
      } else {
        setRestartPhase('unknown')
        setRestartNotice(`Successor outcome remains unknown: ${errorMessage(restartError, 'response was lost')}. Retry only the frozen successor request after reconciliation; the source will not be stopped again.`)
      }
    }
  }

  async function handleConfirmedRestart() {
    if (!restartDraftReady || !projectId || !selectedRun || !selectedRunIsActive || pendingSuccessorStart) return
    const sourceRunId = selectedRun.run_id
    const startRequest = startRequestFromDraft(sourceRunId)
    const stopIntent = {
      project_id: projectId,
      run_id: sourceRunId,
      expected_revision: selectedRun.revision,
      restart: true,
    }
    try {
      setRestartPhase('stopping')
      setRestartNotice(null)
      setError(null)
      setFeedback(null)
      const stopped = await api.ownerStopControlPlaneRun(
        projectId,
        sourceRunId,
        selectedRun.revision,
        getPendingWriteKey('owner-stop', stopIntent),
      )
      clearPendingWriteKey('owner-stop', stopIntent)
      setRuns((current) => upsertRun(current, stopped))
      setRestartPhase('waiting')
      setRestartNotice(`Owner stop recorded for ${sourceRunId}. Waiting until the exact source unit is confirmed inactive and terminal before any successor starts.`)
      const source = await waitUntilSourceInactive(projectId, sourceRunId)
      if (!source) {
        setRestartPhase('failed')
        setRestartNotice('Source inactivity could not be confirmed (the source is not terminal after owner stop, or the network failed). No successor was started; your draft is preserved below. Use Refresh to see the current source state.')
        return
      }
      const successorIntent = { project_id: projectId, ...startRequest }
      const idempotencyKey = getPendingWriteKey('start', successorIntent)
      const pending = { projectId, sourceRunId, request: startRequest, idempotencyKey }
      setPendingSuccessorStart(pending)
      setRestartPhase('starting')
      setRestartNotice(`Source ${sourceRunId} is confirmed owner-stopped and inactive. Starting the successor run.`)
      try {
        const response = await api.startControlPlaneRun(projectId, startRequest, idempotencyKey)
        clearPendingWriteKey('start', successorIntent)
        setPendingSuccessorStart(null)
        setRestartPhase(null)
        setRestartNotice(null)
        await handleStartResponse(response, 'Successor start')
      } catch (restartError) {
        if (successorStartWasRejected(restartError)) {
          clearPendingWriteKey('start', successorIntent)
          setPendingSuccessorStart(null)
          setRestartPhase('failed')
          setRestartNotice(`Successor start was rejected: ${errorMessage(restartError, 'start rejected')}. No successor was started by this request. The source remains owner-stopped.`)
        } else {
          setRestartPhase('unknown')
          setRestartNotice(`Successor outcome is unknown: ${errorMessage(restartError, 'response was lost')}. The exact successor request is frozen; reconcile or retry it with the same idempotency key. The source will not be stopped again.`)
        }
      }
    } catch (restartError) {
      setRestartPhase('failed')
      if (apiErrorCode(restartError) === 'revision_conflict') {
        await refreshSelectedRun()
        setRestartNotice('The source revision changed before the owner stop was applied. No successor was started; your draft is preserved. Review the refreshed source state and confirm the restart again.')
      } else {
        setRestartNotice(`Restart stopped without a successor: ${errorMessage(restartError, 'restart failed')}. The source run state above is authoritative and your draft is preserved.`)
      }
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
  const canRestart = canMutate && selectedRunIsActive
  const startMaxTurnsProblem = maxTurnsProblem(startMaxTurns)
  const restartDraftWorkflow = startWorkflow.trim()
  const restartInProgress = restartPhase === 'stopping' || restartPhase === 'waiting' || restartPhase === 'starting'
  const successorOutcomeUnknown = pendingSuccessorStart !== null && restartPhase !== 'starting'
  const restartDraftFrozen = pendingSuccessorStart !== null
  const restartPendingConfirmation = restartPhase === 'confirming'

  // Effective launch values, resolved only from the committed projection plus
  // canonical capabilities.  An unresolved value is never labeled a default.
  const committedForm = committed?.form ?? null
  const effectiveWorkflow = startWorkflow.trim() || (committedForm?.default_workflow ?? '').trim()
  const effectiveWorkflowSource = startWorkflow.trim()
    ? 'your selection'
    : committedForm?.default_workflow
      ? 'project default'
      : 'not resolved'
  const configuredMaxTurns = committedForm?.max_turns ?? null
  const effectiveMaxTurns = startMaxTurns.trim() !== '' ? Number(startMaxTurns.trim()) : configuredMaxTurns
  const effectiveMaxTurnsSource = startMaxTurns.trim() !== ''
    ? 'your override'
    : configuredMaxTurns !== null
      ? 'project default'
      : 'no limit configured'
  const workflowDefaultTeam = effectiveWorkflow
    ? committedForm?.workflow_default_teams?.[effectiveWorkflow]
      ?? capabilities?.workflow_details?.[effectiveWorkflow]?.default_team
      ?? null
    : null
  const effectiveTeam = startTeam.trim() || workflowDefaultTeam || ''
  const effectiveTeamSource = startTeam.trim()
    ? 'your selection'
    : workflowDefaultTeam
      ? `workflow default (${workflowDefaultTeam})`
      : 'no team — global role assignments apply'
  const effectiveTeamRoles = effectiveTeam ? committedForm?.teams?.[effectiveTeam]?.roles ?? {} : {}
  const effectiveSteps = configuredWorkflowSteps(committedForm, capabilities, effectiveWorkflow)
  // Exact per-step roles come only from the committed projection's
  // materialized mapping; a role is never inferred from the step name, the
  // full configured role list, or raw TOML.
  const stepRoleMap = effectiveWorkflow
    ? committedForm?.workflows?.[effectiveWorkflow]?.step_roles ?? null
    : null
  const unmappedSteps = stepRoleMap === null
    ? effectiveSteps
    : effectiveSteps.filter((step) => !(step in stepRoleMap))

  // Configured-only choices for the searchable controls; the '' sentinel is
  // the explicit "no override" value that follows the configured defaults.
  const workflowOptions = ['', ...new Set([
    ...(committedForm ? Object.keys(committedForm.workflows) : []),
    ...(capabilities?.workflows ?? []),
  ])].sort()
  const workflowBadges: Record<string, string> = {}
  for (const workflow of workflowOptions) {
    if (workflow === '') {
      workflowBadges[''] = committedForm?.default_workflow
        ? `project default (${committedForm.default_workflow})`
        : 'no project default'
      continue
    }
    workflowBadges[workflow] = committedForm?.workflows[workflow] ? 'configured' : 'available on this server'
  }
  const teamOptions = ['', ...new Set([
    ...(committedForm ? Object.keys(committedForm.teams) : []),
    ...(capabilities?.teams ?? []),
  ])].sort()
  const teamBadges: Record<string, string> = { '': 'no team — follow the workflow default' }
  for (const team of teamOptions) {
    if (team === '') continue
    teamBadges[team] = committedForm?.teams[team] ? 'configured' : 'available on this server'
  }
  // Launch admission offers only saved Ready (in progress) plans: Draft and
  // Done records are never selectable, so their paths are never submitted.
  const runnablePlans = plans.filter((plan) => plan.status === 'in_progress')
  const planOptions = runnablePlans.map((plan) => plan.path)
  const planBadges: Record<string, string> = Object.fromEntries(runnablePlans.map((plan) => [
    plan.path,
    'ready',
  ]))

  const launchBlocker = (() => {
    if (!startPlanPath.trim()) return 'Choose a Ready plan to enable Start run.'
    if (!runnablePlans.some((plan) => plan.path === startPlanPath.trim())) {
      return 'Choose a Ready plan from the available options. Draft and Done plans cannot run.'
    }
    if (committedError) return `The committed configuration could not be read (${committedError}). Refresh to retry, or fix it in Settings.`
    if (committed === null) return 'The committed configuration has not finished loading yet.'
    if (committed.form === null) return 'The committed configuration has a TOML syntax error — fix it in Settings and save the pair before starting a run.'
    if (committed.validationState === 'configuration_required') return 'The committed configuration still requires explicit model selectors — finish configuration in Settings and save it before starting a run.'
    if (committed.validationState === 'invalid') return 'The committed configuration is invalid — fix the diagnostics in Settings and save the pair before starting a run.'
    if (!effectiveWorkflow || !workflowOptions.includes(effectiveWorkflow)) return 'Choose an available workflow, or set a project default in Settings.'
    if (effectiveTeam && !teamOptions.includes(effectiveTeam)) return 'Choose an available team, or clear the override to use the workflow default.'
    if (effectiveSteps.length === 0) return 'The workflow has no available executable steps to preview. Check it in Settings.'
    if (unmappedSteps.length > 0) return `The exact role preview is unavailable for ${effectiveWorkflow} step${unmappedSteps.length > 1 ? 's' : ''} ${unmappedSteps.join(', ')} — the committed configuration does not map those executable steps to roles. Refresh, or check the workflow in Settings, before starting a run.`
    return null
  })()
  const startDisabled = !projectAvailable
    || !startPlanPath
    || busyAction === 'start'
    || restartInProgress
    || restartDraftFrozen
    || Boolean(extraInstructionProblem)
    || startMaxTurnsProblem !== null
    || launchBlocker !== null
  const restartDraftReady = Boolean(restartDraftWorkflow && restartDraftWorkflow !== selectedRun?.workflow_name)
    && launchBlocker === null && startMaxTurnsProblem === null && !extraInstructionProblem
  const restartDraftHint = canRestart && !restartDraftReady
    ? 'Select a different workflow and complete the Ready plan choices in New run to enable a guided restart.'
    : null
  const runSteps = effectiveSteps
  const startStepIndex = runSteps.indexOf(startStep.trim())
  const skippedByDraft = startStepIndex > 0 ? runSteps.slice(0, startStepIndex) : []

  const readinessLabel = readiness === null
    ? 'Readiness unavailable'
    : readiness.ready ? 'Daemon ready' : 'Daemon not ready'
  const streamLabel = streamState === 'connected'
    ? 'stream connected'
    : streamState === 'reconnecting'
      ? 'stream reconnecting — last snapshot shown'
      : 'stream stopped — refresh for updates'
  const startTime = selectedRun ? textEvidence(selectedRun, 'manifest_created_at') : 'Not reported'
  const elapsed = selectedRun && selectedRunIsActive
    ? parseElapsedFrom(selectedRun.evidence.manifest_created_at as string | undefined, elapsedNow)
    : null
  const checkpoints = checkpointSummary(context)
  const outcome = managerOutcome(context)
  const selectedPlanPath = selectedRun
    ? (textEvidence(selectedRun, 'plan_path') !== 'Not reported'
      ? textEvidence(selectedRun, 'plan_path')
      : planPathFromContext(context))
    : 'Not reported'
  const roleChoices = capabilities?.roles ?? []

  if (loading) {
    return <div className="card dashboard-loading"><div className="spinner" />Loading runs…</div>
  }

  return (
    <div className="run-dashboard">
      <div className="run-dashboard-header">
        <div>
          <h2>Run dashboard</h2>
          <p className="text-sm text-dim">Persistent server records remain visible if the daemon connection is interrupted.</p>
        </div>
        <button className="btn btn-secondary btn-sm" onClick={() => setRefreshNonce((nonce) => nonce + 1)} disabled={refreshing || loading}>
          {refreshing ? 'Refreshing…' : 'Refresh'}
        </button>
      </div>

      {error && <div className="error-message">{error}</div>}
      {feedback && <div className="success-message">{feedback}</div>}
      {successorOutcomeUnknown && pendingSuccessorStart && (
        <div className="notice" role="alert">
          The successor request for {pendingSuccessorStart.sourceRunId} in {pendingSuccessorStart.projectId} is frozen while its outcome is unknown. Do not start a changed replacement.
          <div className="dashboard-actions"><button className="btn btn-primary" disabled={restartInProgress || pendingSuccessorStart.projectId !== projectId} onClick={() => void retryPendingSuccessorStart()}>Retry exact successor request</button></div>
        </div>
      )}

      {projectAvailable && capabilities && (
        <div className="dashboard-capabilities card">
          <div><strong>{projectId}</strong><span className={`status-pill ${readiness?.ready ? '' : 'status-awaiting'}`}>{readinessLabel}</span></div>
          <div className="text-xs text-dim">Workflows: {capabilities.workflows.join(', ') || 'not reported'} · Teams: {capabilities.teams.join(', ') || 'not reported'}</div>
        </div>
      )}

      {projectAvailable === false && (
        <div className="card" role="alert" aria-label="Project unavailable to the control plane">
          <h3>This project is not available to the workflow control plane</h3>
          <p className="text-sm text-dim">
            <span className="mono">{projectId}</span> is registered in the dashboard, but the control plane does not
            list it, so its plans and runs cannot be shown here and no run can be started. The daemon may need to
            reload its project registry after the project was registered or its configuration saved.
          </p>
          <p className="text-sm text-dim">Daemon readiness: {readinessLabel}.</p>
          <div className="dashboard-actions">
            <button className="btn btn-secondary btn-sm" disabled={refreshing} onClick={() => setRefreshNonce((nonce) => nonce + 1)}>
              Retry control-plane check
            </button>
          </div>
        </div>
      )}

      {projectAvailable && startupQuestion && (
        <section className="card startup-question" aria-label="Startup question">
          <div className="status-pill status-awaiting">Awaiting startup answer · {startupQuestion.kind.replace(/_/g, ' ')}</div>
          <h3>{startupQuestion.message}</h3>
          <p className="text-sm text-dim">No workflow unit exists until an answer is accepted.</p>
          <div className="dashboard-actions">
            {CONFIRM_QUESTION_KINDS.has(startupQuestion.kind) && <>
              <button className="btn btn-primary" disabled={busyAction === 'startup-answer'} onClick={() => void handleStartupAnswer(true)}>Confirm and continue</button>
              <button className="btn btn-secondary" disabled={busyAction === 'startup-answer'} onClick={() => void handleStartupAnswer(false)}>Decline and stop</button>
            </>}
            {!CONFIRM_QUESTION_KINDS.has(startupQuestion.kind) && startupQuestion.choices.map((choice) => (
              <button key={choice} className="btn btn-primary" disabled={busyAction === 'startup-answer'} onClick={() => void handleStartupAnswer(choice)}>{choice}</button>
            ))}
            {Object.entries(startupQuestion.options).map(([key, label]) => (
              <button key={key} className="btn btn-secondary" disabled={busyAction === 'startup-answer'} onClick={() => void handleStartupAnswer(key)}>{label}</button>
            ))}
          </div>
        </section>
      )}

      {projectAvailable && (
        <section className="card start-run-form">
          <div className="section-heading">
            <h3>
              <button
                type="button"
                className="disclosure-toggle"
                aria-expanded={newRunOpen}
                aria-controls="new-run-body"
                onClick={() => setNewRunOpen((open) => !open)}
              >
                New run
              </button>
            </h3>
            <span className="text-xs text-dim">Choose a Ready plan and review the steps below. AFlow checks the setup before starting.</span>
          </div>
          {newRunOpen && (
            <div id="new-run-body" className="start-run-form">
              <div className="dashboard-field">
                <Combobox
                  label="Run plan"
                  value={startPlanPath}
                  onChange={setStartPlanPath}
                  options={planOptions}
                  optionBadges={planBadges}
                  disabled={restartDraftFrozen}
                  emptyOption="Choose an allowed plan"
                  placeholder="Search Ready plans"
                />
                <span className="text-xs text-dim">Only Ready (in progress) plans are offered — move a draft to Ready in Plans to run it.</span>
              </div>
              <div className="dashboard-form-grid">
                <div className="dashboard-field">
                  <Combobox
                    label="Run workflow"
                    value={startWorkflow}
                    onChange={changeStartWorkflow}
                    options={workflowOptions}
                    optionBadges={workflowBadges}
                    disabled={restartDraftFrozen}
                    emptyOption="Leave empty to use the project default workflow"
                    placeholder="Search workflows"
                  />
                </div>
                <div className="dashboard-field">
                  <Combobox
                    label="Run team"
                    value={startTeam}
                    onChange={setStartTeam}
                    options={teamOptions}
                    optionBadges={teamBadges}
                    disabled={restartDraftFrozen}
                    emptyOption="Leave empty to use the workflow default team"
                    placeholder="Search teams"
                  />
                </div>
                <label className="dashboard-field"><span>Max turns</span>
                  <input className="input" aria-label="Run max turns" type="number" min="1" value={startMaxTurns} disabled={restartDraftFrozen} onChange={(event) => setStartMaxTurns(event.target.value)} />
                  {startMaxTurnsProblem
                    ? <span className="error-message" role="alert">{startMaxTurnsProblem}</span>
                    : <span className="text-xs text-dim">{configuredMaxTurns !== null ? `Project default: ${configuredMaxTurns}.` : 'No project default max turns is configured.'}</span>}
                </label>
              </div>
              <section className="dashboard-section" aria-label="Effective choices for this launch">
                <div className="section-heading">
                  <h4>Effective choices for this launch</h4>
                  <span className="text-xs text-dim">from your selections and the committed configuration</span>
                </div>
                <dl className="run-preview-list">
                  <div><dt>Plan</dt><dd className="mono">{startPlanPath.trim() || <span className="text-dim">Not chosen</span>}</dd></div>
                  <div><dt>Workflow</dt><dd>{effectiveWorkflow
                    ? <><span className="mono">{effectiveWorkflow}</span> — {effectiveWorkflowSource}</>
                    : <span className="text-dim">Not resolved — choose a workflow or set a project default in Settings</span>}</dd></div>
                  <div><dt>Max turns</dt><dd>{startMaxTurnsProblem !== null
                    ? <><span className="mono">{startMaxTurns.trim()}</span> — invalid override: correct the Max turns field</>
                    : effectiveMaxTurns !== null
                      ? <><span className="mono">{effectiveMaxTurns}</span> — {effectiveMaxTurnsSource}</>
                      : <span className="text-dim">{effectiveMaxTurnsSource}</span>}</dd></div>
                  <div><dt>Team</dt><dd>{effectiveTeam
                    ? <><span className="mono">{effectiveTeam}</span> — {effectiveTeamSource}</>
                    : <span className="text-dim">{effectiveTeamSource}</span>}</dd></div>
                </dl>
                {effectiveWorkflow && (effectiveSteps.length ? (
                  <table className="guided-table">
                    <caption className="text-xs text-dim">
                      Executable steps with their exact role ({effectiveTeam ? `team ${effectiveTeam} override → global` : 'global selector'})
                    </caption>
                    <thead><tr><th scope="col">Step</th><th scope="col">Role → selector</th></tr></thead>
                    <tbody>
                      {effectiveSteps.map((step) => {
                        const role = stepRoleMap?.[step]
                        const resolution = role
                          ? resolveStepRole(role, effectiveTeamRoles, committedForm?.roles ?? {})
                          : null
                        return (
                          <tr key={step}>
                            <td className="mono">{step}</td>
                            <td>
                              {!resolution
                                ? <span className="text-dim text-sm">Exact role preview unavailable for this step.</span>
                                : resolution.selector
                                  ? <div className="text-sm">
                                      <span className="mono">{resolution.role} → {resolution.selector}</span>
                                      {selectorModelEffortText(resolution.selector, committedForm) && <> · {selectorModelEffortText(resolution.selector, committedForm)}</>}
                                      {resolution.source === 'team' && <> — team {effectiveTeam} override</>}
                                      {resolution.source === 'global' && <> — global</>}
                                    </div>
                                  : <div className="text-sm"><span className="mono">{resolution.role}</span> — <span className="text-dim">missing — assign it in Settings → Roles or a team override</span></div>}
                            </td>
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>
                ) : (
                  <p className="text-sm text-dim">
                    Executable steps for <span className="mono">{effectiveWorkflow}</span> are not available from the
                    committed configuration — check the workflow declaration in Settings.
                  </p>
                ))}
              </section>
              <section className="dashboard-section">
                <h4>
                  <button
                    type="button"
                    className="disclosure-toggle"
                    aria-expanded={advancedOpen}
                    aria-controls="new-run-advanced"
                    onClick={() => setAdvancedOpen((open) => !open)}
                  >
                    Advanced options
                  </button>
                </h4>
                {advancedOpen && (
                  <div id="new-run-advanced" className="start-run-form">
                    <label className="dashboard-field"><span>Start step</span>
                      <select className="input" aria-label="Run start step" value={startStep} onChange={(event) => setStartStep(event.target.value)} disabled={!effectiveWorkflow || restartDraftFrozen}>
                        <option value="">{effectiveWorkflow ? 'Workflow first step (default)' : 'Select a workflow first'}</option>
                        {runSteps.map((step, index) => (
                          <option key={step} value={step}>{index + 1} · {step}</option>
                        ))}
                      </select>
                    </label>
                    {skippedByDraft.length > 0 && (
                      <div className="notice">
                        Starting at <strong>{startStep}</strong> skips the earlier executable steps:{' '}
                        <span className="mono">{skippedByDraft.join(', ')}</span>. They are recorded as skipped, not executed.
                      </div>
                    )}
                    <label className="dashboard-field"><span>Extra instructions (optional — one instruction per line)</span>
                      <textarea
                        className="input textarea"
                        aria-label="Run extra instructions"
                        disabled={restartDraftFrozen}
                        value={startExtraInstructions}
                        onChange={(event) => setStartExtraInstructions(event.target.value)}
                        rows={3}
                      />
                    </label>
                  </div>
                )}
              </section>
              {extraInstructionProblem && <div className="error-message" role="alert">{extraInstructionProblem} Open Advanced options to edit the instructions.</div>}
              {launchBlocker && (
                <div className="notice" role="note">
                  {launchBlocker}
                  {onOpenSettings && (
                    <div className="dashboard-actions">
                      <button className="btn btn-secondary btn-sm" onClick={onOpenSettings}>Open settings</button>
                    </div>
                  )}
                </div>
              )}
              <div className="dashboard-actions">
                <button
                  className="btn btn-primary"
                  onClick={() => void handleStart()}
                  disabled={startDisabled}
                >
                  {busyAction === 'start' ? 'Starting…' : 'Start run'}
                </button>
              </div>
            </div>
          )}
        </section>
      )}

      {projectAvailable && (
        <div className="dashboard-columns">
          <section className="card run-list" aria-label="Project runs">
          <div className="section-heading"><h3>Project runs</h3><span className="text-xs text-dim">{runs.length} recorded</span></div>
          {runs.length === 0 ? <p className="text-sm text-dim">No runs are recorded for this project.</p> : runs.map((run) => (
            <button className={`content-button run-list-item ${selectedRunId === run.run_id ? 'selected' : ''}`} key={run.run_id} onClick={() => setSelectedRunId(run.run_id)}>
              <span className="mono">{run.run_id}</span>
              <span className="status-pill">{statusLabel(run)}</span>
              <span className="text-xs text-dim">
                {run.restarted_from_run_id ? '↻ successor · ' : ''}
                {run.workflow_name ?? 'workflow not reported'} · {run.current_step ?? 'step not reported'}
                {run.skipped_steps.length > 0 ? ` · ${run.skipped_steps.length} skipped` : ''}
              </span>
            </button>
          ))}
        </section>

        <section className="card run-detail" aria-label="Run details">
          {!selectedRun ? <p className="text-sm text-dim">Select a recorded run to inspect its server status and events.</p> : <>
            <div className="section-heading"><div><h3>Run {selectedRun.run_id}</h3><p className="text-sm text-dim">Canonical identity · revision {selectedRun.revision}</p></div><span className="status-pill">{statusLabel(selectedRun)}</span></div>
            {selectedRun.ownership === 'legacy' && <div className="notice">Legacy record classified as interrupted and read-only. It is never treated as a live workflow.</div>}
            {selectedRun.status === 'needs_attention' && <div className="notice">This run needs attention. A disconnected dashboard did not stop it; explicit resume is required when safe.</div>}
            {selectedRun.reason && <div className="notice">{selectedRun.reason}</div>}
            {restartNotice && <div className="notice" role="status">{restartNotice}</div>}

            <dl className="run-metadata">
              <div><dt>Ownership</dt><dd>{selectedRun.ownership}</dd></div>
              <div><dt>Unit / reconciliation</dt><dd className="mono">{selectedRun.unit_name ?? 'Not reported'} · {selectedRun.launch_phase ?? 'phase not reported'} · {selectedRun.evidence.reconciled ? 'reconciled' : 'not reconciled'}</dd></div>
              <div><dt>Workflow / team</dt><dd>{selectedRun.workflow_name ?? 'Not reported'} · {selectedRun.team ?? 'Not reported'}</dd></div>
              <div><dt>Upgrade chain</dt><dd>{selectedRun.team ? capabilities?.team_upgrade_chains[selectedRun.team]?.join(' → ') || 'Not reported' : 'Not reported'}</dd></div>
              <div><dt>Current step / turns</dt><dd>{selectedRun.current_step ?? 'Not reported'} · {selectedRun.turns_completed ?? '0'} / {selectedRun.max_turns ?? 'Not reported'}</dd></div>
              <div><dt>Start step / skipped</dt><dd>{selectedRun.selected_start_step ?? 'Not reported'} · {selectedRun.skipped_steps.length ? selectedRun.skipped_steps.join(', ') : 'none skipped'}</dd></div>
              <div><dt>Excluded steps</dt><dd>{selectedRun.workflow_name && capabilities?.workflow_details?.[selectedRun.workflow_name]?.excluded_steps.length
                ? capabilities.workflow_details[selectedRun.workflow_name].excluded_steps.join(', ')
                : 'none'}</dd></div>
              <div><dt>Checkpoint</dt><dd>{checkpoints
                ? checkpoints.complete ? `All ${checkpoints.count} checkpoints complete` : `${checkpoints.name ?? 'unnamed'} (${checkpoints.index ?? '?'} of ${checkpoints.count})`
                : 'Not reported'}</dd></div>
              <div><dt>Start / elapsed</dt><dd>{startTime === 'Not reported' ? 'Not reported' : timestamp(startTime)}{elapsed ? ` · running for ${elapsed}` : ''}</dd></div>
              <div><dt>Lineage</dt><dd className="mono">
                {selectedRun.restarted_from_run_id ? <div>successor of {selectedRun.restarted_from_run_id}</div> : null}
                {successorRunIds.length ? <div>source of {successorRunIds.join(', ')}</div> : null}
                {!selectedRun.restarted_from_run_id && !successorRunIds.length ? 'no restart lineage' : null}
              </dd></div>
              <div><dt>Plan</dt><dd className="mono">{selectedPlanPath}</dd></div>
              <div><dt>Worktree</dt><dd className="mono">{textEvidence(selectedRun, 'worktree_path') !== 'Not reported' ? textEvidence(selectedRun, 'worktree_path') : contextText(context, 'worktree_path')}</dd></div>
              <div><dt>Branch</dt><dd className="mono">{textEvidence(selectedRun, 'branch') !== 'Not reported' ? textEvidence(selectedRun, 'branch') : contextText(context, 'branch')}</dd></div>
              <div><dt>Recorded at</dt><dd>{timestamp(selectedRun.evidence.manifest_created_at ?? selectedRun.evidence.updated_at)}</dd></div>
            </dl>

            <section className="dashboard-section">
              <div className="section-heading"><h4>Latest bounded outcomes</h4><span className="text-xs text-dim">manager · harness · live overrides</span></div>
              <div className="text-sm">
                <div>{outcome?.decision ?? 'No manager decision reported yet.'}</div>
                <div>{outcome?.finishedTurn ? `Last finalized turn: ${outcome.finishedTurn}` : 'No finalized turn reported yet.'}</div>
                {outcome?.resultText && <pre className="dashboard-payload">{outcome.resultText}</pre>}
                <div className="text-xs text-dim">
                  Live overrides: max turns {overrideText(controlOverride, 'max_turns')} · team {overrideText(controlOverride, 'team')}
                  {Object.keys(overrideRoles(controlOverride)).length
                    ? ` · selectors ${Object.entries(overrideRoles(controlOverride)).map(([role, selector]) => `${role}=${selector}`).join(', ')}`
                    : ' · no selector overrides'}
                </div>
              </div>
            </section>

            <section className="dashboard-section">
              <div className="section-heading"><h4>Safe controls</h4><span className="text-xs text-dim">Server capability and revision gated</span></div>
              {!canMutate && <div className="notice">Actions are disabled because the server classifies this as a legacy read-only record.</div>}
              <div className="notice">
                Changes are saved now and applied between turns. They remain marked Pending until the run
                confirms them. To use a profile outside this run's available choices, restart the run.
              </div>
              {capabilities && Object.entries(capabilities.control_safety).filter(([, safety]) => safety === 'restart_required').map(([control]) => (
                <div className="text-xs text-dim" key={control}>{control.replace(/_/g, ' ')} requires restart; it is not offered as a live control.</div>
              ))}
              <div className="dashboard-form-grid">
                <label className="dashboard-field"><span>Max turns</span><input className="input" aria-label="Control max turns" type="number" min="1" value={controlMaxTurns} disabled={!canMutate || !hasSafeControl('max_turns')} onChange={(event) => setControlMaxTurns(event.target.value)} /></label>
                <label className="dashboard-field"><span>Team</span><select className="input" aria-label="Control team" value={controlTeam} disabled={!canMutate || !hasSafeControl('team')} onChange={(event) => setControlTeam(event.target.value)}><option value="">No team</option>{capabilities?.teams.map((team) => <option key={team} value={team}>{team}</option>)}</select></label>
              </div>
              {roleChoices.map((role) => {
                const admitted = capabilities?.admitted_role_selectors?.[role] ?? []
                return (
                  <label className="dashboard-field" key={role}>
                    <span>Selector for {role}{overrideRoles(controlOverride)[role] ? ` (current override: ${overrideRoles(controlOverride)[role]})` : ''}</span>
                    <select
                      className="input"
                      aria-label={`Selector for ${role}`}
                      value={roleSelectors[role] ?? ''}
                      disabled={!canMutate || !hasSafeControl('role_selectors') || admitted.length === 0}
                      onChange={(event) => setRoleSelectors((current) => ({ ...current, [role]: event.target.value }))}
                    >
                      <option value="">No change</option>
                      {admitted.map((selector) => <option key={selector} value={selector}>{selector}</option>)}
                    </select>
                  </label>
                )
              })}
              <div className="dashboard-actions"><button className="btn btn-secondary" onClick={() => void handleControl()} disabled={!canMutate || busyAction === 'control'}>{busyAction === 'control' ? 'Applying…' : 'Apply safe controls'}</button></div>
            </section>

            <section className="dashboard-section dashboard-actions">
              {hasSafeControl('owner_stop') && canMutate && <>
                {!confirmOwnerStop ? <button className="btn btn-danger" disabled={restartInProgress} onClick={() => setConfirmOwnerStop(true)}>Owner stop…</button> : <div className="confirmation"><span>Confirm owner stop for {selectedRun.run_id}. This control is recorded by the server.</span><button className="btn btn-danger" disabled={busyAction === 'owner-stop' || restartInProgress} onClick={() => void handleOwnerStop()}>Confirm stop</button><button className="btn btn-secondary" onClick={() => setConfirmOwnerStop(false)}>Cancel</button></div>}
              </>}
              {canResume && <>
                {!confirmResume ? <button className="btn btn-primary" disabled={restartInProgress} onClick={() => setConfirmResume(true)}>Resume as new run…</button> : <div className="confirmation"><span>Confirm explicit resume. Source {selectedRun.run_id} remains visible; the server creates a distinct continuation run with the same workflow and frozen configuration.</span><button className="btn btn-primary" disabled={busyAction === 'resume'} onClick={() => void handleResume()}>Confirm resume</button><button className="btn btn-secondary" onClick={() => setConfirmResume(false)}>Cancel</button></div>}
              </>}
            </section>

            {canRestart && (
              <section className="dashboard-section">
                <div className="section-heading"><h4>Change workflow (guided restart)</h4><span className="text-xs text-dim">stop → confirm inactive → successor start</span></div>
                <div className="notice">
                  A workflow change is never applied in place. Confirming performs an owner stop of{' '}
                  <span className="mono">{selectedRun.run_id}</span>, waits until the canonical status proves that
                  exact unit is inactive and terminal, and only then starts a fresh run from the typed draft in the
                  New run form with <span className="mono">restarted_from_run_id</span> lineage. If the stop, the
                  wait, or the successor start fails, automation halts: the draft is preserved and the source state
                  above stays authoritative. Explicit resume stays a separate same-workflow action.
                </div>
                {restartDraftHint
                  ? <div className="text-xs text-dim">{restartDraftHint}</div>
                  : !restartPendingConfirmation && !restartInProgress && (
                    <div className="dashboard-actions"><button className="btn btn-danger" onClick={() => setRestartPhase('confirming')}>Change workflow: stop and restart…</button></div>
                  )}
                {restartPendingConfirmation && !restartDraftReady && (
                  <div className="text-xs text-dim">Select a successor workflow and plan in the New run form before confirming.</div>
                )}
                {restartPendingConfirmation && restartDraftReady && (
                  <div className="confirmation">
                    <span>
                      Stop <span className="mono">{selectedRun.run_id}</span> (revision {selectedRun.revision}) and start
                      successor workflow <strong>{restartDraftWorkflow}</strong>
                      {startStep.trim() ? <> at step <strong>{startStep.trim()}</strong></> : null}
                      {startPlanPath ? <> with plan <span className="mono">{startPlanPath.trim()}</span></> : null}? The successor records this run as its restart source.
                    </span>
                    <button className="btn btn-danger" disabled={busyAction !== null || restartInProgress} onClick={() => void handleConfirmedRestart()}>Confirm stop and start successor</button>
                    <button className="btn btn-secondary" onClick={() => setRestartPhase(null)}>Cancel restart</button>
                  </div>
                )}
                {restartInProgress && (
                  <div className="status-pill status-awaiting">
                    {restartPhase === 'stopping' && 'Stopping the source run…'}
                    {restartPhase === 'waiting' && 'Waiting for exact source inactivity…'}
                    {restartPhase === 'starting' && 'Starting the successor run…'}
                  </div>
                )}
              </section>
            )}

            <section className="dashboard-section">
              <div className="section-heading"><h4>Context</h4><span className="text-xs text-dim">{context?.level ?? 'not loaded'}</span></div>
              <div className="dashboard-actions"><button className={`btn btn-sm ${contextLevel === 'lite' ? 'btn-primary' : 'btn-secondary'}`} onClick={() => void selectContextLevel('lite')}>Lite — bounded operational summary</button><button className={`btn btn-sm ${contextLevel === 'full' ? 'btn-primary' : 'btn-secondary'}`} disabled={!capabilities?.context_levels.includes('full')} onClick={() => void selectContextLevel('full')}>Full — disclosed scoped detail</button></div>
              {contextLevel === 'full' && <label className="context-disclosure"><input type="checkbox" checked={fullContextAcknowledged} onChange={(event) => setFullContextAcknowledged(event.target.checked)} /> I understand Full context may expose additional bounded run metadata and request it for this authenticated session only.</label>}
              <div className="dashboard-actions"><button className="btn btn-secondary btn-sm" disabled={busyAction === 'context' || (contextLevel === 'full' && !fullContextAcknowledged)} onClick={() => void loadContext()}>Load {contextLevel} context</button></div>
              {context && <pre className="dashboard-payload">{JSON.stringify(context.data, null, 2)}</pre>}
            </section>

            <section className="dashboard-section">
              <div className="section-heading"><div><h4>Event timeline</h4><span className="text-xs text-dim">{events.length} bounded events · {streamLabel}</span></div><button className="btn btn-secondary btn-sm" onClick={() => void refreshSelectedRun()}>Refresh status</button></div>
              {streamNotice && <div className="notice">{streamNotice}</div>}
              {events.length === 0 ? <p className="text-sm text-dim">No bounded events are available yet.</p> : <div className="run-timeline">{events.map((event) => <article className="timeline-event" key={event.sequence}><div><strong>{event.event_type.replace(/_/g, ' ')}</strong><span className="text-xs text-dim">#{event.sequence} · {timestamp(event.timestamp)}</span></div><pre>{JSON.stringify(event.data, null, 2)}</pre></article>)}</div>}
            </section>
          </>}
        </section>
        </div>
      )}
    </div>
  )
}
