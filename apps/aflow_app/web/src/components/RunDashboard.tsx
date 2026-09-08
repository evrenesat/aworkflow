import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react'
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
import { NewRunPage } from './NewRunPage'
import { statusLabel, executionDuration } from '../runPresentation'
import { workspaceHref } from '../urlState'

const MAX_TIMELINE_EVENTS = 100
/** Bounded wait for exact source inactivity before a successor start. */
const RESTART_POLL_INTERVAL_MS = 1_000
const MAX_RESTART_POLLS = 30
const CONFIRM_QUESTION_KINDS = new Set(['confirm_recovery', 'confirm_worktree_dirty'])
const MAX_EXTRA_INSTRUCTION_ITEMS = 8
const MAX_EXTRA_INSTRUCTION_LENGTH = 512
const MAX_EXTRA_INSTRUCTIONS_TOTAL = 4_096

/** A run-selection report used to keep the public URL truthful. */
export interface RunSelectionChange {
  /** The run now displayed; null when nothing is selected. */
  runId: string | null
  /** True for an explicit user pick (a history push); false for a passive sync. */
  userInitiated: boolean
  /** Set when a URL-requested run does not exist in this project. */
  missingRunId?: string
}

interface RunDashboardProps {
  page?: 'runs' | 'new-run'
  visible?: boolean
  onNewRun?: () => void
  onCancelNewRun?: () => void
  onRunStarted?: (runId: string) => void
  /** The registered project whose runs are shown; its sole project authority. */
  projectId: string
  /**
   * The run requested through the public URL, if any.  Each new request is
   * selected and validated through the direct project-scoped run endpoint; a
   * missing request never selects a substitute.
   */
  requestedRunId?: string | null
  onRunSelectionChange?: (change: RunSelectionChange) => void
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

/** Prefer canonical launch time, then the UTC timestamp in legacy run ids. */
function runCreatedAt(run: RunStatus): number {
  const created = run.evidence.manifest_created_at
  if (typeof created === 'string') {
    const timestamp = Date.parse(created)
    if (Number.isFinite(timestamp)) return timestamp
  }
  const match = /^(\d{4})(\d{2})(\d{2})t(\d{2})(\d{2})(\d{2})z(?:-|$)/i.exec(run.run_id)
  if (!match) return 0
  const [, year, month, day, hour, minute, second] = match
  const timestamp = Date.parse(`${year}-${month}-${day}T${hour}:${minute}:${second}Z`)
  return Number.isFinite(timestamp) ? timestamp : 0
}

function newestRunsFirst(runs: RunStatus[]): RunStatus[] {
  return [...runs].sort((left, right) => runCreatedAt(right) - runCreatedAt(left))
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


function overrideRoles(override: Record<string, unknown> | null): Record<string, string> {
  const roles = override?.roles
  if (typeof roles !== 'object' || roles === null) return {}
  const result: Record<string, string> = {}
  for (const [role, selector] of Object.entries(roles as Record<string, unknown>)) {
    if (typeof selector === 'string') result[role] = selector
  }
  return result
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

export function RunDashboard({ visible = true, page, onNewRun, onCancelNewRun, onRunStarted, projectId, requestedRunId = null, onRunSelectionChange, initialPlanPath, onInitialPlanHandled, restartPollIntervalMs, pendingSuccessorStart: suppliedPendingSuccessor, onPendingSuccessorStartChange, onOpenSettings }: RunDashboardProps) {
  const [projectAvailable, setProjectAvailable] = useState<boolean | null>(null)
  const [capabilities, setCapabilities] = useState<ControlPlaneCapabilities | null>(null)
  const [readiness, setReadiness] = useState<ControlPlaneReadiness | null>(null)
  const [plans, setPlans] = useState<ControlPlanePlan[]>([])
  const [committed, setCommitted] = useState<CommittedProjection | null>(null)
  const [committedError, setCommittedError] = useState<string | null>(null)
  const [runs, setRuns] = useState<RunStatus[]>([])
  const [selectedRunId, setSelectedRunId] = useState<string | null>(requestedRunId)
  const [missingRunId, setMissingRunId] = useState<string | null>(null)
  const [events, setEvents] = useState<RunEvent[]>([])
  const [context, setContext] = useState<RunContext | null>(null)
  const [rawOpen, setRawOpen] = useState(false)
  const [contextBusy, setContextBusy] = useState(false)
  const [contextError, setContextError] = useState<string | null>(null)
  const [statusUpdatedAt, setStatusUpdatedAt] = useState<string | null>(null)
  const [contextUpdatedAt, setContextUpdatedAt] = useState<string | null>(null)
  const [streamState, setStreamState] = useState<api.StreamState>('stopped')
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [busyAction, setBusyAction] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [streamNotice, setStreamNotice] = useState<string | null>(null)
  const [feedback, setFeedback] = useState<string | null>(null)
  const [handoffError, setHandoffError] = useState<string | null>(null)
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
  const [restartSource, setRestartSource] = useState<RunStatus | null>(null)
  const [restartAdmission, setRestartAdmission] = useState<api.RestartOptions | null>(null)
  const [missingRestartInstructions, setMissingRestartInstructions] = useState(false)
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
  const [localPage, setLocalPage] = useState<'runs' | 'new-run'>(initialPlanPath ? 'new-run' : 'runs')
  const newRunPage = (page ?? localPage) === 'new-run'
  function openNewRunPage() { setLocalPage('new-run'); onNewRun?.() }
  const [failedRequestId, setFailedRequestId] = useState<string | null>(null)
  const [advancedOpen, setAdvancedOpen] = useState(false)
  const [technicalOpen, setTechnicalOpen] = useState(false)
  const technicalId = useId()
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'failed'>('idle')
  const [refreshNonce, setRefreshNonce] = useState(0)
  const selectedRunRef = useRef<string | null>(selectedRunId)
  const requestedRunRef = useRef<string | null>(requestedRunId)
  const missingRunRef = useRef<string | null>(null)
  const snapshotRequestRef = useRef(0)
  const contextRequestRef = useRef(0)
  const requestAbortRef = useRef(new AbortController())
  const contextAbortRef = useRef(new AbortController())
  const desiredContextLevelRef = useRef<'lite' | 'full'>('lite')
  desiredContextLevelRef.current = technicalOpen && rawOpen && capabilities?.context_levels.includes('full') ? 'full' : 'lite'
  useEffect(() => {
    requestAbortRef.current = new AbortController()
    return () => { requestAbortRef.current.abort(); contextAbortRef.current.abort() }
  }, [projectId, selectedRunId, visible])
  const diagnosticsLoadedForRef = useRef<string | null>(null)
  const controlsForRunRef = useRef<string | null>(null)
  const previousStreamStateRef = useRef<api.StreamState>('stopped')
  const onRunSelectionChangeRef = useRef(onRunSelectionChange)
  onRunSelectionChangeRef.current = onRunSelectionChange
  missingRunRef.current = missingRunId
  const { getKey: getPendingWriteKey, clearKey: clearPendingWriteKey, clearAll: clearPendingWriteKeys } = usePendingWriteKeys()

  const selectedRun = useMemo(
    () => runs.find((run) => run.run_id === selectedRunId) ?? null,
    [runs, selectedRunId],
  )

  useEffect(() => {
    if (!visible || !selectedRunId) return
    let active = true
    setRestartAdmission(null)
    void api.getRestartOptions(projectId, selectedRunId).then(result => {
      if (active) setRestartAdmission(result)
    }).catch(() => { /* The run remains readable if restart admission is unavailable. */ })
    return () => { active = false }
  }, [visible, projectId, selectedRunId, selectedRun?.status, refreshNonce])

  function openRestart() {
    if (!selectedRun) return
    const options = restartAdmission?.options
    setRestartSource(selectedRun)
    setStartPlanPath(options?.plan_path ?? selectedRun.plan_path ?? String(selectedRun.evidence.plan_path ?? ''))
    setStartWorkflow(options?.workflow_name ?? selectedRun.workflow_name ?? '')
    setStartTeam(options?.team ?? selectedRun.team ?? '')
    setStartMaxTurns((options?.max_turns ?? selectedRun.max_turns)?.toString() ?? '')
    setStartStep(options?.start_step ?? selectedRun.selected_start_step ?? '')
    setStartExtraInstructions(options?.extra_instructions?.join('\n') ?? '')
    setMissingRestartInstructions(restartAdmission?.extra_instructions_unavailable ?? false)
    setRestartPhase('confirming')
    openNewRunPage()
  }

  useEffect(() => {
    const question = selectedRun?.evidence.startup_question
    if (selectedRun?.status === 'awaiting_startup_answer' && question && typeof question === 'object') {
      setStartupQuestion(question as StartupQuestion)
    }
  }, [selectedRun])

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
    && selectedRun.status === 'running',
  )

  const selectedRunHasLiveControls = Boolean(
    selectedRun?.ownership === 'control_plane'
    && ['running', 'paused', 'waiting_for_valid_override', 'waiting_for_input'].includes(selectedRun.status),
  )

  useEffect(() => {
    selectedRunRef.current = selectedRunId
  }, [selectedRunId])

  // The public URL is authoritative for a requested run: every new request
  // selects exactly that run and is validated through the direct
  // project-scoped endpoint. Clearing the request keeps the visible
  // selection; the dashboard reports it back so the URL is re-synced.
  useEffect(() => {
    if (requestedRunId === requestedRunRef.current) return
    requestedRunRef.current = requestedRunId
    if (requestedRunId !== null) {
      setMissingRunId(null)
      setSelectedRunId(requestedRunId)
    }
  }, [requestedRunId])

  // Clipboard feedback describes one link; a new selection starts a new one.
  useEffect(() => {
    setCopyState('idle')
  }, [projectId, selectedRunId])

  useEffect(() => {
    clearPendingWriteKeys()
  }, [projectId, clearPendingWriteKeys])

  useEffect(() => {
    if (!selectedRun || controlsForRunRef.current === selectedRun.run_id) return
    controlsForRunRef.current = selectedRun.run_id
    setControlMaxTurns(selectedRun.max_turns?.toString() ?? '')
    setControlTeam(selectedRun.team ?? '')
    setRoleSelectors({})
    setConfirmOwnerStop(false)
    setConfirmResume(false)
    if (!pendingSuccessorStart && !restartSource) {
      setRestartPhase(null)
      setRestartNotice(null)
    }
  }, [selectedRun, pendingSuccessorStart, restartSource])

  // A live elapsed clock only ticks while a nonterminal owned run is selected.
  useEffect(() => {
    if (!visible || !selectedRunIsActive || !selectedRun?.started_at) return
    setElapsedNow(Date.now())
    const ticker = setInterval(() => setElapsedNow(Date.now()), 1_000)
    return () => clearInterval(ticker)
  }, [visible, selectedRunIsActive, selectedRun?.started_at])

  // The registered project id is the sole authority: no root matching, no
  // internal switcher, and never a fallback to another control-plane project.
  useEffect(() => {
    if (!visible) return
    let active = true
    void (async () => {
      try {
        setLoading(true)
        setProjectAvailable(null)
        const [available, readinessState] = await Promise.all([
          api.listControlPlaneProjects(),
          api.getControlPlaneReadiness(),
        ])
        if (!active) return
        setReadiness(readinessState)
        const availableHere = available.some((project) => project.project_id === projectId)
        setProjectAvailable(availableHere)
        if (availableHere) await loadDashboard(projectId, () => active)
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
  }, [projectId, refreshNonce, visible])

  const handoffCallback = useRef(onInitialPlanHandled)
  const handledHandoff = useRef<string | null>(null)
  handoffCallback.current = onInitialPlanHandled
  useEffect(() => {
    if (!initialPlanPath) { handledHandoff.current = null; return }
    const identity = JSON.stringify([projectId, initialPlanPath])
    if (!visible || handledHandoff.current === identity) return
    let active = true
    void api.listControlPlanePlans(projectId).then(freshPlans => {
    if (!active) return
    setHandoffError(null)
    setPlans(freshPlans)
    const handedOff = freshPlans.find((plan) => plan.path === initialPlanPath)
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
    handledHandoff.current = identity
    handoffCallback.current()
    }).catch(reason => {
      if (active) setHandoffError(`${errorMessage(reason, 'Could not refresh plans')}. Refresh to retry the selected plan.`)
    })
    return () => { active = false }
  }, [visible, projectId, initialPlanPath, refreshNonce])

  useEffect(() => {
    if (!visible || !projectId || !selectedRunId) return
    let active = true
    setEvents([])
    setContext(null)
    setStatusUpdatedAt(null)
    setContextUpdatedAt(null)
    setContextError(null)
    contextRequestRef.current += 1
    diagnosticsLoadedForRef.current = null
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
        void refreshPageRef.current().finally(() => {
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
    const poll = setInterval(() => {
      if (document.visibilityState !== 'hidden') scheduleSnapshotRefresh()
    }, 10_000)
    return () => {
      active = false
      clearInterval(poll)
      if (refreshTimer) clearTimeout(refreshTimer)
      unsubscribe()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- resubscribes only per project/run, not per render
  }, [visible, projectId, selectedRunId])

  async function loadDashboard(nextProjectId: string, isActive = () => true) {
    try {
      setRefreshing(true)
      setError(null)
      const [nextReadiness, nextCapabilities, nextPlans, page] = await Promise.all([
        api.getControlPlaneReadiness(),
        api.getControlPlaneCapabilities(nextProjectId),
        api.listControlPlanePlans(nextProjectId),
        api.listControlPlaneRuns(nextProjectId, { limit: 100 }),
      ])
      if (!isActive()) return
      setReadiness(nextReadiness)
      setCapabilities(nextCapabilities)
      setPlans(nextPlans)
      const orderedRuns = newestRunsFirst(page.runs)
      setRuns((current) => {
        // The first page never silently displaces a run that was fetched
        // directly (a requested link target or the current selection): it is
        // retained until the page itself carries it again.
        const pageIds = new Set(page.runs.map((run) => run.run_id))
        const pinned = current.filter((run) => !pageIds.has(run.run_id)
          && (run.run_id === requestedRunRef.current || run.run_id === selectedRunRef.current))
        return [...orderedRuns.map(run => run.run_id === selectedRunRef.current ? current.find(item => item.run_id === run.run_id) ?? run : run), ...pinned]
      })
      // Default to the newest returned run only when there is no requested
      // or current selection: a linked run is validated separately through
      // the direct endpoint and is never silently substituted.
      const newestId = orderedRuns[0]?.run_id ?? null
      const currentSelection = selectedRunRef.current
      if (requestedRunRef.current === null && missingRunRef.current === null
        && currentSelection === null) {
        setSelectedRunId(newestId)
        if (newestId !== null && newestId !== currentSelection) {
          onRunSelectionChangeRef.current?.({ runId: newestId, userInitiated: false })
        }
      }
      // The launch form projects the committed pair through the pure form
      // endpoint; a projection failure only blocks launch, never the runs.
      try {
        const committedPair = await api.getGlobalConfig()
        const projected = await api.postGlobalConfigForm({
          aflow_toml: committedPair.aflow_toml,
          workflows_toml: committedPair.workflows_toml,
        })
        if (!isActive()) return
        setCommitted({
          validationState: projected.validation.state,
          form: projected.form,
          syntaxIssues: projected.syntax_issues,
        })
        setCommittedError(null)
      } catch (projectionError) {
        if (!isActive()) return
        setCommitted(null)
        setCommittedError(errorMessage(projectionError, 'Failed to read the committed configuration'))
      }
    } catch (loadError) {
      if (!isActive()) return
      // Preserve the last daemon snapshot: a connection failure is not a run transition.
      setError(`${errorMessage(loadError, 'Failed to refresh runs')}. Existing run data remains visible.`)
    } finally {
      if (isActive()) setRefreshing(false)
    }
  }

  async function loadSelectedRun(
    nextProjectId: string,
    runId: string,
    isActive = () => true,
  ) {
    const requestNumber = ++snapshotRequestRef.current
    try {
      const [run, tail] = await Promise.all([
        api.getControlPlaneRun(nextProjectId, runId, { signal: requestAbortRef.current.signal }),
        api.listRunEvents(nextProjectId, runId, { limit: MAX_TIMELINE_EVENTS }, { signal: requestAbortRef.current.signal }).catch(reason => {
          if (isActive() && selectedRunRef.current === runId && requestNumber === snapshotRequestRef.current) setStreamNotice(`Activity timeline is stale: ${errorMessage(reason, 'events unavailable')}. Use Refresh to retry.`)
          return []
        }),
      ])
      if (!isActive() || selectedRunRef.current !== runId || requestNumber !== snapshotRequestRef.current) return
      setRuns((current) => upsertRun(current, run))
      setStatusUpdatedAt(new Date().toISOString())
      setEvents((current) => mergeEvents(current, tail))
    } catch (loadError) {
      if (!isActive() || selectedRunRef.current !== runId || requestNumber !== snapshotRequestRef.current) return
      if (loadError instanceof ApiError && loadError.status === 404) {
        // A linked run that does not exist selects no substitute: the Runs
        // view keeps its list and New run offer, and the URL drops the id.
        setMissingRunId(runId)
        setSelectedRunId(null)
        onRunSelectionChangeRef.current?.({ runId: null, userInitiated: false, missingRunId: runId })
        return
      }
      setError(errorMessage(loadError, 'Failed to load run details'))
    }
  }

  const pageRefreshRef = useRef<Promise<void> | null>(null)
  const refreshEpochRef = useRef(0)
  useEffect(() => {
    refreshEpochRef.current += 1
    pageRefreshRef.current = null
    return () => { requestAbortRef.current.abort(); contextAbortRef.current.abort(); refreshEpochRef.current += 1; snapshotRequestRef.current += 1; contextRequestRef.current += 1 }
  }, [projectId, selectedRunId, visible])
  async function refreshPage() {
    if (!visible || !projectId) return
    if (projectAvailable === false) { setRefreshNonce(nonce => nonce + 1); return }
    if (pageRefreshRef.current) return pageRefreshRef.current
    const epoch = refreshEpochRef.current
    const active = () => epoch === refreshEpochRef.current
    const task = (async () => {
      setRefreshing(true)
      if (handoffError) setRefreshNonce(nonce => nonce + 1)
      await loadDashboard(projectId, active)
      if (!active()) return
      setRefreshing(true)
      await Promise.all([
        selectedRunId ? loadSelectedRun(projectId, selectedRunId, active) : Promise.resolve(),
        loadContext(desiredContextLevelRef.current),
      ])
    })().finally(() => { if (active()) { pageRefreshRef.current = null; setRefreshing(false) } })
    pageRefreshRef.current = task
    return task
  }
  const refreshPageRef = useRef(refreshPage)
  refreshPageRef.current = refreshPage
  async function refreshSelectedRun() { await refreshPage() }

  /** An explicit run pick: cleared stale-link guidance and a history push report. */
  function selectRun(runId: string) {
    setMissingRunId(null)
    setSelectedRunId(runId)
    onRunSelectionChangeRef.current?.({ runId, userInitiated: true })
  }

  /** Copy only project/run identities confirmed by the server, never ambient URL data. */
  async function handleCopyLink() {
    try {
      const href = workspaceHref({ project: projectId, view: 'runs', run: selectedRun?.run_id ?? null })
      const url = new URL(window.location.pathname + href, window.location.origin)
      await navigator.clipboard.writeText(url.href)
      setCopyState('copied')
    } catch {
      setCopyState('failed')
    }
  }

  function hasSafeControl(control: string): boolean {
    return Boolean(
      capabilities?.controls.includes(control)
      && capabilities.control_safety[control] === 'safe',
    )
  }

  /**
   * Loads one debugging-context level for the selected run. Opening
   * Diagnostics is the intentional action: full context (with the transport
   * compatibility flag) needs no further acknowledgement. A stale response
   * (run changed, diagnostics closed, dashboard hidden) is discarded.
   */
  async function loadContext(level: 'lite' | 'full', runId: string | null = selectedRunId) {
    if (!projectId || !runId) return
    const request = ++contextRequestRef.current
    contextAbortRef.current.abort()
    contextAbortRef.current = new AbortController()
    setContextBusy(true)
    setContextError(null)
    try {
      const result = await api.getRunContext(projectId, runId, level, level === 'full', { signal: contextAbortRef.current.signal })
      if (request !== contextRequestRef.current || selectedRunRef.current !== runId) return
      setContext(result)
      setContextUpdatedAt(new Date().toISOString())
      setContextBusy(false)
    } catch (contextLoadError) {
      if (request !== contextRequestRef.current || selectedRunRef.current !== runId) return
      setContextError(errorMessage(contextLoadError, 'Failed to load run debugging info'))
      setContextBusy(false)
    }
  }

  // Opening Diagnostics fetches the best supported detail level once for the
  // selected run; closing or hiding the dashboard invalidates in-flight
  // responses so a late payload can never land in a closed panel.
  useEffect(() => {
    if (!visible) {
      contextRequestRef.current += 1
      return
    }
  }, [technicalOpen, rawOpen, visible])
  useEffect(() => {
    if (!visible || !selectedRunId || !capabilities) return
    const level = technicalOpen && rawOpen && capabilities.context_levels.includes('full') ? 'full' : 'lite'
    const identity = `${selectedRunId}:${level}`
    if (diagnosticsLoadedForRef.current === identity && context?.run_id === selectedRunId) return
    diagnosticsLoadedForRef.current = identity
    void loadContext(level, selectedRunId)
    // eslint-disable-next-line react-hooks/exhaustive-deps -- fetch once per opened run, plus explicit refresh
  }, [technicalOpen, rawOpen, visible, selectedRunId, capabilities])

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
      // Only an explicit selection is submitted; an empty field means the
      // server applies the global default workflow / the workflow default
      // team, so following a default never turns into a permanent override.
      ...(startWorkflow.trim() ? { workflow_name: startWorkflow.trim() } : {}),
      ...(startTeam.trim() ? { team: startTeam.trim() } : {}),
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
      if (startError instanceof ApiError && typeof startError.detail.run_id === 'string') {
        setFailedRequestId(startError.detail.run_id)
        clearPendingWriteKey('start', intent)
      }
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
    if (result.status === 'needs_attention') {
      setError(result.reason ?? 'Startup did not complete; the original error was not recorded.')
      setFailedRequestId(result.run_id)
      await loadDashboard(projectId)
      return
    }
    setFailedRequestId(null)
    setStartupQuestion(null)
    const lineage = result.restarted_from_run_id
      ? ` It records ${result.restarted_from_run_id} as its restart source.`
      : ''
    setFeedback(result.created
      ? `${action} created run ${result.run_id}.${lineage}`
      : `${action} replay returned existing run ${result.run_id}; no duplicate was created.`)
    setMissingRunId(null)
    await loadDashboard(projectId)
    setSelectedRunId(result.run_id)
    setLocalPage('runs')
    onRunStarted?.(result.run_id)
    // Passive URL update: the dashboard link now identifies the returned run.
    onRunSelectionChangeRef.current?.({ runId: result.run_id, userInitiated: false })
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
      if (answerError instanceof ApiError && answerError.code === 'startup_failed' && typeof answerError.detail.run_id === 'string') {
        setFailedRequestId(answerError.detail.run_id)
        setStartupQuestion(null)
        clearPendingWriteKey('startup-answer', intent)
      }
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
    return error instanceof ApiError && (error.code === 'startup_failed' || (error.status >= 400 && error.status < 500 && error.status !== 408))
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
        setRestartNotice(`Successor start was rejected: ${errorMessage(restartError, 'start rejected')}. Correct the draft before trying a new attempt.`)
        if (restartError instanceof ApiError && typeof restartError.detail.run_id === 'string') { setFailedRequestId(restartError.detail.run_id); setError(restartError.message) }
      } else {
        setRestartPhase('unknown')
        setRestartNotice(`Successor outcome remains unknown: ${errorMessage(restartError, 'response was lost')}. Retry only the frozen successor request after reconciliation; the source will not be stopped again.`)
      }
    }
  }

  async function handleConfirmedRestart() {
    if (!restartDraftReady || !projectId || !restartSource || pendingSuccessorStart) return
    const sourceRunId = restartSource.run_id
    const startRequest = startRequestFromDraft(sourceRunId)
    const stopIntent = {
      project_id: projectId,
      run_id: sourceRunId,
      expected_revision: restartSource.revision,
      restart: true,
    }
    try {
      setRestartPhase('stopping')
      setRestartNotice(null)
      setError(null)
      setFeedback(null)
      const canonicalSource = restartPhase === 'failed'
        ? await api.getControlPlaneRun(projectId, sourceRunId) : restartSource
      if (['running', 'paused', 'waiting_for_input', 'waiting_for_valid_override'].includes(canonicalSource.status)) {
      const stopped = await api.ownerStopControlPlaneRun(
        projectId,
        sourceRunId,
        restartSource.revision,
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
      }
      const successorIntent = { project_id: projectId, ...startRequest }
      const idempotencyKey = getPendingWriteKey('start', successorIntent)
      const pending = { projectId, sourceRunId, request: startRequest, idempotencyKey }
      setPendingSuccessorStart(pending)
      setRestartPhase('starting')
      setRestartNotice(`Validating source ${sourceRunId} and starting a new attempt with plan progress retained.`)
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
          setRestartNotice(`Successor start was rejected: ${errorMessage(restartError, 'start rejected')}. Correct the draft before trying a new attempt.`)
          if (restartError instanceof ApiError && typeof restartError.detail.run_id === 'string') { setFailedRequestId(restartError.detail.run_id); setError(restartError.message) }
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
        setRestartNotice(`Restart stopped without a confirmed successor: ${errorMessage(restartError, 'restart failed')}. The source state may have changed. Refresh or retry; your draft is preserved.`)
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
      setMissingRunId(null)
      await loadDashboard(projectId)
      setSelectedRunId(continuation.run_id)
      // Passive URL update: the dashboard link now identifies the continuation.
      onRunSelectionChangeRef.current?.({ runId: continuation.run_id, userInitiated: false })
    } catch (resumeError) {
      setError(errorMessage(resumeError, 'Failed to resume run'))
    } finally {
      setBusyAction(null)
    }
  }

  const canMutate = selectedRun?.ownership === 'control_plane'
  const canResume = canMutate && selectedRun?.evidence.can_resume === true
  const canRestart = canMutate && (restartAdmission?.eligible === true || (selectedRunHasLiveControls && hasSafeControl('owner_stop')))
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
      ? 'global default'
      : 'no default workflow configured'
  const configuredMaxTurns = committedForm?.max_turns ?? null
  const effectiveMaxTurns = startMaxTurns.trim() !== '' ? Number(startMaxTurns.trim()) : configuredMaxTurns
  const effectiveMaxTurnsSource = startMaxTurns.trim() !== ''
    ? 'your override'
    : configuredMaxTurns !== null
      ? 'global default'
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

  // Configured-only choices for the searchable controls. The empty stored
  // value means "follow default"; it is represented by the explicit default
  // option row, never by a blank entry.
  const workflowOptions = [...new Set([
    ...(committedForm ? Object.keys(committedForm.workflows) : []),
    ...(capabilities?.workflows ?? []),
  ])].sort()
  const workflowBadges: Record<string, string> = {}
  for (const workflow of workflowOptions) {
    workflowBadges[workflow] = committedForm?.workflows[workflow] ? 'configured' : 'available on this server'
  }
  const teamOptions = [...new Set([
    ...(committedForm ? Object.keys(committedForm.teams) : []),
    ...(capabilities?.teams ?? []),
  ])].sort()
  const teamBadges: Record<string, string> = {}
  for (const team of teamOptions) {
    teamBadges[team] = committedForm?.teams[team] ? 'configured' : 'available on this server'
  }
  // Visible resolved values: before focus each selector shows the effective
  // name with a Default indicator; the open list offers an explicit default row.
  const workflowResolvedDisplay = startWorkflow.trim()
    ? null
    : committedForm?.default_workflow || 'No default workflow configured'
  const workflowResolvedBadge = !startWorkflow.trim() && committedForm?.default_workflow ? 'Default' : undefined
  const workflowDefaultOption = {
    value: '',
    label: committedForm?.default_workflow ? `Use default (${committedForm.default_workflow})` : 'Use default',
    hint: committedForm?.default_workflow ? 'the global default workflow applies' : 'no global default workflow is configured',
  }
  const teamResolvedDisplay = startTeam.trim() ? null : (workflowDefaultTeam || 'No team — global roles')
  const teamResolvedBadge = !startTeam.trim() && workflowDefaultTeam ? 'Default' : undefined
  const teamDefaultOption = {
    value: '',
    label: workflowDefaultTeam ? `Use default (${workflowDefaultTeam})` : 'Use default (no team)',
    hint: workflowDefaultTeam ? 'the workflow default team applies' : 'global role assignments apply',
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
    if (restartSource && !planOptions.includes(startPlanPath)) return 'The original plan is no longer Ready or its path moved. Choose a current Ready plan; progress will be retained.'
    if (restartSource && startStep && !effectiveSteps.includes(startStep)) return 'The original start step is unavailable in the saved workflow. Choose a current step.'
    if (restartSource && missingRestartInstructions && !startExtraInstructions.trim()) return 'Original extra instructions were not retained for this source. Re-enter them in Advanced options before restarting.'
    if (!startPlanPath.trim()) return 'Choose a Ready plan to enable Start run.'
    if (!runnablePlans.some((plan) => plan.path === startPlanPath.trim())) {
      return 'Choose a Ready plan from the available options. Draft and Done plans cannot run.'
    }
    if (committedError) return `The committed configuration could not be read (${committedError}). Refresh to retry, or fix it in Settings.`
    if (committed === null) return 'The committed configuration has not finished loading yet.'
    if (committed.form === null) return 'The committed configuration has a TOML syntax error — fix it in Settings and save the pair before starting a run.'
    if (committed.validationState === 'configuration_required') return 'The committed configuration still requires explicit model selectors — finish configuration in Settings and save it before starting a run.'
    if (committed.validationState === 'invalid') return 'The committed configuration is invalid — fix the diagnostics in Settings and save the pair before starting a run.'
    if (!effectiveWorkflow || !workflowOptions.includes(effectiveWorkflow)) return 'No default workflow configured — choose an available workflow, or set a global default in Settings.'
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
  const restartDraftReady = Boolean(restartDraftWorkflow && startPlanPath && planOptions.includes(startPlanPath))
    && (!missingRestartInstructions || Boolean(startExtraInstructions.trim()))
    && launchBlocker === null && startMaxTurnsProblem === null && !extraInstructionProblem
  const restartDraftHint = canRestart && !restartDraftReady
    ? 'Choose an available workflow and a Ready plan. Original choices that are no longer available must be corrected.'
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
  const startTime = selectedRun?.started_at ?? null
  const elapsed = selectedRun ? executionDuration(selectedRun, elapsedNow) : null
  const checkpoints = checkpointSummary(context)
  const outcome = managerOutcome(context)
  const selectedPlanPath = selectedRun
    ? selectedRun.plan_path ?? (textEvidence(selectedRun, 'plan_path') !== 'Not reported'
      ? textEvidence(selectedRun, 'plan_path')
      : planPathFromContext(context))
    : 'Not reported'
  // The header names the plan file; the full relative path stays under
  // Technical details.
  const selectedPlanFileName = selectedPlanPath !== 'Not reported'
    ? selectedPlanPath.split('/').pop() || selectedPlanPath
    : 'Not reported'
  const roleChoices = capabilities?.roles ?? []
  const savedOverrides = selectedRun?.evidence.overrides as { state?: string; revision?: number; max_turns?: number; team?: string; role_selectors?: Record<string, string> } | null

  const workflowRoleList = stepRoleMap ? [...new Set(Object.values(stepRoleMap))].sort() : []
  const otherConfiguredRoles = committedForm
    ? Object.keys(committedForm.roles).filter((role) => !workflowRoleList.includes(role)).sort()
    : []
  const upgradeChain = effectiveTeam ? capabilities?.team_upgrade_chains?.[effectiveTeam] ?? null : null

  function membershipRow(role: string, teamRoles: Record<string, string>, teamName: string | null) {
    const resolution = resolveStepRole(role, teamRoles, committedForm?.roles ?? {})
    const modelEffort = resolution.selector ? selectorModelEffortText(resolution.selector, committedForm) : ''
    return (
      <tr key={`${teamName ?? 'workspace'}-${role}`}>
        <td>{role}</td>
        <td className="mono">{resolution.selector ?? <span className="text-dim">not assigned</span>}</td>
        <td>{modelEffort || '—'}</td>
        <td className="text-sm">
          {resolution.source === 'team'
            ? <span>team override</span>
            : resolution.source === 'global'
              ? <span>global fallback</span>
              : <span className="text-dim">missing — assign it in Settings</span>}
        </td>
      </tr>
    )
  }

  const launchPreview = (
              <section className="dashboard-section" aria-label="Effective choices for this launch">
                <div className="section-heading">
                  <h4>Effective choices for this launch</h4>
                  <span className="text-xs text-dim">from your selections and the committed configuration</span>
                </div>
                <dl className="run-preview-list">
                  <div><dt>Plan</dt><dd className="mono">{startPlanPath.trim() || <span className="text-dim">Not chosen</span>}</dd></div>
                  <div><dt>Workflow</dt><dd>{effectiveWorkflow
                    ? <><span className="mono">{effectiveWorkflow}</span> — {effectiveWorkflowSource}</>
                    : <span className="text-dim">No default workflow configured — choose a workflow or set a global default in Settings</span>}</dd></div>
                  <div><dt>Max turns</dt><dd>{startMaxTurnsProblem !== null
                    ? <><span className="mono">{startMaxTurns.trim()}</span> — invalid override: correct the Max turns field</>
                    : effectiveMaxTurns !== null
                      ? <><span className="mono">{effectiveMaxTurns}</span> — {effectiveMaxTurnsSource}</>
                      : <span className="text-dim">{effectiveMaxTurnsSource}</span>}</dd></div>
                  <div><dt>Team</dt><dd>{effectiveTeam
                    ? <><span className="mono">{effectiveTeam}</span> — {effectiveTeamSource}</>
                    : <span className="text-dim">{effectiveTeamSource}</span>}</dd></div>
                </dl>
                {workflowRoleList.length > 0 && (
                  <div>
                    <h4>Team members</h4>
                    <table className="guided-table">
                      <caption className="text-xs text-dim">
                        Effective role assignments for this workflow{effectiveTeam ? ` with team ${effectiveTeam}` : ''} (team override → global fallback)
                      </caption>
                      <thead><tr><th scope="col">Role</th><th scope="col">Selector</th><th scope="col">Model / effort</th><th scope="col">Source</th></tr></thead>
                      <tbody>
                        {workflowRoleList.map((role) => membershipRow(role, effectiveTeamRoles, effectiveTeam || null))}
                      </tbody>
                    </table>
                    {otherConfiguredRoles.length > 0 && (
                      <details>
                        <summary className="text-xs text-dim">Other configured roles ({otherConfiguredRoles.length})</summary>
                        <table className="guided-table">
                          <tbody>
                            {otherConfiguredRoles.map((role) => membershipRow(role, effectiveTeamRoles, effectiveTeam || null))}
                          </tbody>
                        </table>
                      </details>
                    )}
                  </div>
                )}
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
                                      {resolution.source === 'team' && <> — team override</>}
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
                {effectiveTeam && (
                  <div>
                    <h4>Worker upgrade chain</h4>
                    {!capabilities ? (
                      <p className="text-sm text-dim">The upgrade chain cannot be read because project capabilities failed to load. Refresh to retry.</p>
                    ) : !upgradeChain ? (
                      <p className="text-sm text-dim">Team <span className="mono">{effectiveTeam}</span> is not part of the configured upgrade graph. Refresh, or check the team links in Settings.</p>
                    ) : (
                      <>
                        <ol className="upgrade-chain">
                          {upgradeChain.map((team, index) => {
                            const teamRoles = committedForm?.teams?.[team]?.roles ?? {}
                            const worker = resolveStepRole('worker', teamRoles, committedForm?.roles ?? {})
                            const isLast = index === upgradeChain.length - 1
                            const modelEffort = worker.selector ? selectorModelEffortText(worker.selector, committedForm) : ''
                            const stageRoles = [...new Set([...Object.keys(teamRoles), ...Object.keys(committedForm?.roles ?? {})])].sort()
                            return (
                              <li key={team}>
                                <span className="mono">{team}</span>
                                <span className="text-sm"> — worker {worker.selector
                                  ? <span className="mono">{worker.selector}</span>
                                  : <span className="text-dim">not assigned</span>}{modelEffort ? <> · {modelEffort}</> : null}</span>
                                {isLast && <span className="text-xs text-dim"> — no further upgrade configured</span>}
                                {stageRoles.length > 0 && (
                                  <details className="upgrade-stage-details">
                                    <summary className="text-xs text-dim">stage team members</summary>
                                    <table className="guided-table">
                                      <tbody>
                                        {stageRoles.map((role) => membershipRow(role, teamRoles, team))}
                                      </tbody>
                                    </table>
                                  </details>
                                )}
                              </li>
                            )
                          })}
                        </ol>
                        <p className="text-xs text-dim">This is the configured escalation path, not a promise that every stage runs. Failure recovery through a backup team stays separate from these quality upgrades.</p>
                      </>
                    )}
                  </div>
                )}
              </section>

  )
  const restartActions = restartSource && restartPhase ? (
                <section className="dashboard-section">
                  <div className="section-heading"><h4>Restart with changes</h4><span className="text-xs text-dim">stop → confirm inactive → successor start</span></div>
                  <div className="notice">
                    Create a new attempt from <span className="mono">{restartSource.run_id}</span> with these choices.
                    Plan progress is retained. Active sources are stopped first. This attempt uses the current saved configuration; Resume uses the source snapshot.
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
                        Confirm source <span className="mono">{restartSource.run_id}</span> (revision {restartSource.revision}) and start
                        successor workflow <strong>{restartDraftWorkflow}</strong>
                        {startStep.trim() ? <> at step <strong>{startStep.trim()}</strong></> : null}
                        {startPlanPath ? <> with plan <span className="mono">{startPlanPath.trim()}</span></> : null}? The successor records this run as its restart source.
                      </span>
                      <button className="btn btn-danger" disabled={busyAction !== null || restartInProgress} onClick={() => void handleConfirmedRestart()}>Confirm stop and start successor</button>
                      <button className="btn btn-secondary" onClick={() => { setRestartPhase(null); setRestartSource(null); setLocalPage('runs'); onCancelNewRun?.() }}>Cancel restart</button>
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
              ) : null

  if (loading) {
    return <div className="card dashboard-loading"><div className="spinner" />Loading runs…</div>
  }

  return (
    <div className="run-dashboard">
      <div className="run-dashboard-header">
        <div>
          <h2>{newRunPage ? 'New run' : 'Runs'}</h2>
        </div>
        <div className="header-actions">
          {!newRunPage && <button className="btn btn-primary" onClick={openNewRunPage}>New run</button>}
          {!newRunPage && <button className="btn btn-secondary btn-sm" onClick={() => void handleCopyLink()}>Copy link</button>}
          <button className="btn btn-secondary btn-sm" onClick={() => void refreshPage()} disabled={refreshing || loading}>
            {refreshing ? 'Refreshing…' : 'Refresh'}
          </button>
        </div>
      </div>

      {copyState === 'copied' && <div className="success-message" role="status">Link copied to the clipboard.</div>}
      {copyState === 'failed' && <div className="notice" role="status">Clipboard access failed — copy the address from the browser address bar instead.</div>}
      {error && <div className="error-message" role="alert">{error}{failedRequestId && <button className="btn btn-secondary btn-sm" onClick={() => { setLocalPage('runs'); onRunStarted?.(failedRequestId); selectRun(failedRequestId) }}>View failed request</button>}</div>}
      {feedback && <div className="success-message">{feedback}</div>}
      {handoffError && <div className="error-message" role="alert">{handoffError}</div>}
      {restartNotice && <div className="notice" role="status">{restartNotice}</div>}
      {successorOutcomeUnknown && pendingSuccessorStart && (
        <div className="notice" role="alert">
          The successor request for {pendingSuccessorStart.sourceRunId} in {pendingSuccessorStart.projectId} is frozen while its outcome is unknown. Do not start a changed replacement.
          <div className="dashboard-actions"><button className="btn btn-primary" disabled={restartInProgress || pendingSuccessorStart.projectId !== projectId} onClick={() => void retryPendingSuccessorStart()}>Retry exact successor request</button></div>
        </div>
      )}

      {projectAvailable && missingRunId !== null && !selectedRun && (
        <div className="notice" role="alert">
          The linked run <span className="mono">{missingRunId}</span> is not recorded for this project. Nothing else
          was selected in its place — choose from the current runs below or start a New run.
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
          <p>Use Refresh to retry the control-plane check.</p>
        </div>
      )}

      {projectAvailable && startupQuestion && (newRunPage || selectedRun?.run_id === startupQuestion.run_id) && (
        <section className="card startup-question" aria-label="Startup question">
          <div className="status-pill status-awaiting">Input needed</div>
          <h3>{startupQuestion.message}</h3>
          <p className="text-sm text-dim">No agent started. Answer to continue.</p>
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

      {projectAvailable && !newRunPage && (
        <div className="dashboard-columns">
          <section className="card run-list" aria-label="Project runs">
            <div className="section-heading"><h3>Project runs</h3><span className="text-xs text-dim">{runs.length} recorded</span></div>
            {runs.length === 0 ? <p className="text-sm text-dim">No runs yet</p> : runs.map((run) => (
              <button className={`content-button run-list-item ${selectedRunId === run.run_id ? 'selected' : ''}`} key={run.run_id} onClick={() => selectRun(run.run_id)}>
                <span>{run.plan_path?.split('/').pop() ?? run.run_id}</span>
                <span className="status-pill">{statusLabel(run)}</span>
                <span className="text-xs text-dim">
                  {run.restarted_from_run_id ? '↻ successor · ' : ''}
                  {run.workflow_name}{run.current_step ? ` · ${run.current_step}` : ''}
                  {run.skipped_steps.length > 0 ? ` · ${run.skipped_steps.length} skipped` : ''}
                </span>
              </button>
            ))}
          </section>

          <section className="card run-detail" aria-label="Run details">
            {!selectedRun ? <p className="text-sm text-dim">Select a recorded run to inspect its server status and events.</p> : <>
              <div className="run-progress-header">
                <div className="section-heading">
                  <div>
                    <h3>{selectedPlanFileName === 'Not reported' ? `Run ${selectedRun.run_id}` : selectedPlanFileName}</h3>
                    <button className="text-xs text-dim mono" title="Copy run ID" onClick={() => void navigator.clipboard?.writeText(selectedRun.run_id)}>{selectedRun.run_id}</button>
                  </div>
                  <span className="status-pill">{statusLabel(selectedRun)}</span>
                </div>
                <dl className="run-progress-strip">
                  {selectedRun.current_step && <div><dt>Current step / turns</dt><dd>{selectedRun.current_step} · {selectedRun.turns_completed ?? 0}{selectedRun.max_turns ? ` / ${selectedRun.max_turns}` : ''}</dd></div>}
                  {selectedRun.workflow_name && <div><dt>Workflow / team</dt><dd>{selectedRun.workflow_name}{selectedRun.team ? ` · ${selectedRun.team}` : ''}</dd></div>}
                  {startTime ? <div><dt>Started</dt><dd>{timestamp(startTime)}{elapsed ? ` · ${selectedRunIsActive ? 'running for' : 'duration'} ${elapsed}` : ''}</dd></div>
                    : selectedRun.evidence.manifest_created_at ? <div><dt>Submitted</dt><dd>{timestamp(selectedRun.evidence.manifest_created_at)}</dd></div> : null}
                  {selectedRun.ended_at && <div><dt>Ended</dt><dd>{timestamp(selectedRun.ended_at)}</dd></div>}
                </dl>
              </div>
              {selectedRun.ownership === 'legacy' && <div className="notice">Legacy record classified as interrupted and read-only. It is never treated as a live workflow.</div>}
              {selectedRun.evidence.no_agent_started === true && selectedRun.status !== 'running' && <p>No agent started.</p>}
              {streamState === 'reconnecting' && <div className="notice">Updates are stale. Use Refresh to retry.</div>}
              {selectedRun.reason && <div className="notice">{selectedRun.reason}</div>}


              {(checkpoints || outcome?.decision || outcome?.finishedTurn || outcome?.resultText) && <section className="dashboard-section">
                <h4>Latest progress</h4>
                {checkpoints && <p>{checkpoints.complete ? `All ${checkpoints.count} checkpoints complete` : `${checkpoints.name ?? 'Checkpoint'} (${checkpoints.index ?? '?'} of ${checkpoints.count})`}</p>}
                {outcome?.decision && <p>{outcome.decision}</p>}
                {outcome?.finishedTurn && <p>Last finished turn: {outcome.finishedTurn}</p>}
                {outcome?.resultText && <pre className="dashboard-payload">{outcome.resultText}</pre>}
              </section>}

              {(selectedRun.max_turns || selectedRun.team) && <details className="dashboard-section"><summary>Effective settings</summary>
                <p>Max turns: {selectedRun.max_turns ?? 'Default'}{selectedRun.team ? ` · Team: ${selectedRun.team}` : ''}</p>
                {savedOverrides && <div>
                  <p>{savedOverrides.state === 'applied' ? 'Applied' : savedOverrides.state === 'rejected' ? 'Rejected' : 'Pending'} changes · revision {savedOverrides.revision}</p>
                  {savedOverrides.max_turns && <p>Max turns: {savedOverrides.max_turns}</p>}
                  {savedOverrides.team && <p>Team: {savedOverrides.team}</p>}
                  {Object.entries(savedOverrides.role_selectors ?? {}).map(([role, selector]) => <p key={role}>{role}: {selector}</p>)}
                </div>}
              </details>}

              {selectedRunHasLiveControls && <details className="dashboard-section"><summary>Adjust run</summary>
                {!canMutate && <div className="notice">Actions are disabled because the server classifies this as a legacy read-only record.</div>}
                <div className="notice">
                  Changes are saved now and applied between turns. They remain marked Pending until the run
                  confirms them. To use a profile outside this run's available choices, restart the run.
                </div>
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
                <div className="dashboard-actions"><button className="btn btn-secondary" onClick={() => void handleControl()} disabled={!canMutate || busyAction === 'control'}>{busyAction === 'control' ? 'Applying…' : 'Save run settings'}</button></div>
              </details>}

              <section className="dashboard-section dashboard-actions">
                {hasSafeControl('owner_stop') && selectedRunHasLiveControls && <>
                  {!confirmOwnerStop ? <button className="btn btn-danger" disabled={restartInProgress} onClick={() => setConfirmOwnerStop(true)}>Owner stop…</button> : <div className="confirmation"><span>Confirm owner stop for {selectedRun.run_id}. This control is recorded by the server.</span><button className="btn btn-danger" disabled={busyAction === 'owner-stop' || restartInProgress} onClick={() => void handleOwnerStop()}>Confirm stop</button><button className="btn btn-secondary" onClick={() => setConfirmOwnerStop(false)}>Cancel</button></div>}
                </>}
                {canResume && <>
                  {!confirmResume ? <button className="btn btn-primary" disabled={restartInProgress} onClick={() => setConfirmResume(true)}>Resume as new run…</button> : <div className="confirmation"><span>Confirm explicit resume. Source {selectedRun.run_id} remains visible; the server creates a distinct continuation run with the same workflow and frozen configuration.</span><button className="btn btn-primary" disabled={busyAction === 'resume'} onClick={() => void handleResume()}>Confirm resume</button><button className="btn btn-secondary" onClick={() => setConfirmResume(false)}>Cancel</button></div>}
                </>}
              </section>

              {canRestart && <button className="btn btn-secondary" onClick={openRestart}>Restart with changes</button>}
              {!canResume && selectedRun.evidence.no_agent_started === true && <p className="text-sm">No execution state is available to Resume.</p>}
              {!canRestart && restartAdmission?.reason && <p className="text-sm">Restart unavailable: {restartAdmission.reason}</p>}

              <section className="dashboard-section">
                <h4>
                  <button
                    type="button"
                    className="disclosure-toggle"
                    aria-label="Diagnostics"
                    aria-expanded={technicalOpen}
                    aria-controls={technicalId}
                    onClick={() => setTechnicalOpen((open) => !open)}
                  >
                    Diagnostics
                  </button>
                </h4>
                {technicalOpen && (
                  <div id={technicalId} className="run-technical-details">
              <section className="dashboard-section">
                <div className="section-heading"><div><h4>Activity timeline</h4><span className="text-xs text-dim">{events.length} recent events</span></div></div>
                {streamNotice && <div className="notice">{streamNotice}</div>}
                {events.length === 0 ? <p className="text-sm text-dim">No activity has been reported yet.</p> : <div className="run-timeline">{events.map((event) => <article className="timeline-event" key={event.sequence}><div><strong>{event.event_type.replace(/_/g, ' ')}</strong><span className="text-xs text-dim">#{event.sequence} · {timestamp(event.timestamp)}</span></div></article>)}</div>}
              </section>

<p>Backend state: {selectedRun.status} · {streamLabel}</p>
                    <dl className="run-metadata">
                      <div><dt>Revision</dt><dd>{selectedRun.revision}</dd></div>
                      <div><dt>Ownership</dt><dd>{selectedRun.ownership}</dd></div>
                      <div><dt>Unit / reconciliation</dt><dd className="mono">{selectedRun.unit_name ?? 'Not reported'} · {selectedRun.launch_phase ?? 'phase not reported'} · {selectedRun.evidence.reconciled ? 'reconciled' : 'not reconciled'}</dd></div>
                      <div><dt>Upgrade chain</dt><dd>{selectedRun.team ? capabilities?.team_upgrade_chains[selectedRun.team]?.join(' → ') || 'Not reported' : 'Not reported'}</dd></div>
                      <div><dt>Start step / skipped</dt><dd>{selectedRun.selected_start_step ?? 'Not reported'} · {selectedRun.skipped_steps.length ? selectedRun.skipped_steps.join(', ') : 'none skipped'}</dd></div>
                      <div><dt>Excluded steps</dt><dd>{selectedRun.workflow_name && capabilities?.workflow_details?.[selectedRun.workflow_name]?.excluded_steps.length
                        ? capabilities.workflow_details[selectedRun.workflow_name].excluded_steps.join(', ')
                        : 'none'}</dd></div>
                      <div><dt>Lineage</dt><dd className="mono">
                        {selectedRun.restarted_from_run_id ? <div>successor of {selectedRun.restarted_from_run_id}</div> : null}
                        {successorRunIds.length ? <div>source of {successorRunIds.join(', ')}</div> : null}
                        {!selectedRun.restarted_from_run_id && !successorRunIds.length ? 'no restart lineage' : null}
                      </dd></div>
                      <div><dt>Plan path</dt><dd className="mono">{selectedPlanPath}</dd></div>
                      <div><dt>Worktree</dt><dd className="mono">{textEvidence(selectedRun, 'worktree_path') !== 'Not reported' ? textEvidence(selectedRun, 'worktree_path') : contextText(context, 'worktree_path')}</dd></div>
                      <div><dt>Branch</dt><dd className="mono">{textEvidence(selectedRun, 'branch') !== 'Not reported' ? textEvidence(selectedRun, 'branch') : contextText(context, 'branch')}</dd></div>
                      <div><dt>Recorded at</dt><dd>{timestamp(selectedRun.evidence.manifest_created_at ?? selectedRun.evidence.updated_at)}</dd></div>
                    </dl>

                    <section className="dashboard-section">
                      <h4>Diagnostics summary</h4><span className="text-xs text-dim">run {selectedRun.run_id}</span>
                      {statusUpdatedAt && <p className="text-xs text-dim">Status observed: {timestamp(statusUpdatedAt)}</p>}
                      {contextError && <p className="notice">Diagnostic details are stale. Use Refresh to retry.</p>}
                      <p>Observed state: {statusLabel(selectedRun)}. Worker: {selectedRun.evidence.unit_active === true ? 'active' : selectedRun.evidence.unit_active === false ? 'inactive' : 'activity unconfirmed'}.</p>
                      {selectedRun.worker_exit && <>
                        <p>{selectedRun.worker_exit.stage === 'wrapper_spawn' ? 'Worker could not be spawned' : !selectedRun.evidence.has_run_metadata ? 'Worker exited during startup' : 'Worker exited'}{selectedRun.worker_exit.exit_code !== null ? ` (code ${selectedRun.worker_exit.exit_code})` : ''}</p>
                        <p>Failure stage: {selectedRun.worker_exit.stage}</p>
                        {selectedRun.worker_exit.reason && <p>{selectedRun.worker_exit.reason}</p>}
                        {selectedRun.worker_exit.exited_at && <p>Worker exit: {timestamp(selectedRun.worker_exit.exited_at)}</p>}
                        {selectedRun.worker_exit.diagnostic_unavailable && <p>Original worker error was not retained.</p>}
                      </>}
                      {!selectedRun.worker_exit && selectedRun.reason && <p>{selectedRun.reason}</p>}
                      <p>{[selectedRun.plan_path, selectedRun.workflow_name, selectedRun.team, selectedRun.current_step].filter(Boolean).join(' · ')}</p>
                      {events.length > 0 && <p>Last event: {events[events.length - 1].event_type.replace(/_/g, ' ')} · {timestamp(events[events.length - 1].timestamp)}</p>}
                      <p>{selectedRun.evidence.can_resume === true ? 'Saved continuation is available.' : 'Resume is unavailable: no admitted saved continuation.'} {selectedRun.status === 'failed' ? 'Restart with options checks eligibility before creating a fresh run.' : ''}</p>
                      <details open={rawOpen}>
                        <summary onClick={event => { event.preventDefault(); setRawOpen(open => !open) }}>Raw details</summary>
                        {contextUpdatedAt && <p className="text-xs text-dim">Details observed: {timestamp(contextUpdatedAt)}</p>}
                        {contextBusy && <p role="status">Loading raw details…</p>}
                        {contextError && <div className="error-message" role="alert">Raw details are stale: {contextError}. Use Refresh to retry.</div>}
                        {context && Object.entries(context.data).map(([source, value]) => <details key={source}><summary>{source.replace(/_/g, ' ')}</summary><pre className="dashboard-payload">{JSON.stringify({ [source]: value }, null, 2)}</pre></details>)}
                        {!context && !contextBusy && !contextError && <p>No raw details are available yet.</p>}
                      </details>
                    </section>

                  </div>
                )}
              </section>
            </>}
          </section>
        </div>
      )}

      {projectAvailable && newRunPage && <NewRunPage
        startPlanPath={startPlanPath}
        setStartPlanPath={setStartPlanPath}
        planOptions={planOptions}
        planBadges={planBadges}
        restartDraftFrozen={restartDraftFrozen}
        startWorkflow={startWorkflow}
        changeStartWorkflow={changeStartWorkflow}
        workflowOptions={workflowOptions}
        workflowBadges={workflowBadges}
        workflowPresentation={{
          resolvedDisplay: workflowResolvedDisplay,
          resolvedBadge: workflowResolvedBadge,
          defaultLabel: workflowDefaultOption.label,
          defaultHint: workflowDefaultOption.hint,
        }}
        startTeam={startTeam}
        setStartTeam={setStartTeam}
        teamOptions={teamOptions}
        teamBadges={teamBadges}
        teamPresentation={{
          resolvedDisplay: teamResolvedDisplay,
          resolvedBadge: teamResolvedBadge,
          defaultLabel: teamDefaultOption.label,
          defaultHint: teamDefaultOption.hint,
        }}
        startMaxTurns={startMaxTurns}
        setStartMaxTurns={setStartMaxTurns}
        startMaxTurnsProblem={startMaxTurnsProblem}
        configuredMaxTurns={configuredMaxTurns}
        preview={launchPreview}
        restartActions={restartActions}
        onCancel={() => { if (!restartInProgress && !pendingSuccessorStart) { setRestartPhase(null); setRestartSource(null) }; setLocalPage('runs'); onCancelNewRun?.() }}
        advancedOpen={advancedOpen}
        setAdvancedOpen={setAdvancedOpen}
        startStep={startStep}
        setStartStep={setStartStep}
        effectiveWorkflow={effectiveWorkflow}
        runSteps={runSteps}
        skippedByDraft={skippedByDraft}
        startExtraInstructions={startExtraInstructions}
        setStartExtraInstructions={setStartExtraInstructions}
        extraInstructionProblem={extraInstructionProblem}
        launchBlocker={launchBlocker}
        onOpenSettings={onOpenSettings}
        handleStart={handleStart}
        startDisabled={startDisabled}
        busyAction={busyAction}
      />}

    </div>
  )
}
