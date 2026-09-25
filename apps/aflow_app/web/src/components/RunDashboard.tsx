import { memo, useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import type {
  ConfigValidationIssue,
  ControlPlaneCapabilities,
  ControlPlanePlan,
  ControlPlaneReadiness,
  GuidedFormProjection,
  GuidedProfileSummary,
  ProjectConfig,
  ProjectConfigFormResponse,
  RecoveryRequest,
  RecoveryWorkerEvidence,
  RunProgressDetail,
  RunProgressAvailability,
  RunProgressReason,
  RunProgressTurn,
  RunContext,
  RunEvent,
  RunStatus,
  StartRunRequest,
  StartRunResponse,
  StartupQuestion,
  WorktreePreflight,
} from '../types'
import { ApiError } from '../api'
import * as api from '../api'
import { SidebarEditorLayout } from './SidebarEditorLayout'
import { MoreMenu, MenuItem } from './MoreMenu'
import { NewRunPage, WorktreePreflightPanel, type WorktreePreflightLoadState } from './NewRunPage'
import { Combobox } from './Combobox'
import { useHeaderSlots } from './HeaderSlots'
import {
  launchTeamFamilyGroups,
  launchTeamFamilyHint,
  launchTeamFamilyLabel,
  launchTeamFamilyRoute,
  launchTeamUpgradeRoute,
  launchTeamStageLabel,
  launchTeamRoleSummary,
  formatLocalTimestamp,
  runPlanPresentation,
  runPlanPresentationForRun,
  runFinishText,
  runDisplayProjection,
  isTerminalInactiveRun,
  latestRunResultEvent,
  presentRunEvent,
  deliveryIssueText,
  runCurrentWorkText,
  runExecutorFacts,
  shortRunId,
  statusLabel,
  executionDuration,
} from '../runPresentation'
import { workspaceHref } from '../urlState'
import { formatMachineChoice, formatMachineLabel } from '../label'
import { RunListItem } from './RunListItem'
import { CheckpointHistory } from './CheckpointHistory'
import { RunOverview } from './RunOverview'

const MAX_TIMELINE_EVENTS = 100
/** Bounded wait for exact source inactivity before a successor start. */
const RESTART_POLL_INTERVAL_MS = 1_000
const MAX_RESTART_POLLS = 30
const CONFIRM_QUESTION_KINDS = new Set(['confirm_recovery', 'confirm_worktree_dirty'])
const MAX_EXTRA_INSTRUCTION_ITEMS = 8
const MAX_EXTRA_INSTRUCTION_LENGTH = 512
const MAX_EXTRA_INSTRUCTIONS_TOTAL = 4_096
const DURABLE_RECOVERY_STATUSES = new Set(['failed', 'interrupted', 'owner_stopped'])

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
  /** True only for an explicit initial URL or browser navigation to a run. */
  explicitRunNavigation?: boolean
  onRunSelectionChange?: (change: RunSelectionChange) => void
  initialPlanPath: string | null
  onInitialPlanHandled: () => void
  /** Test-only override; production callers retain the bounded 1s–30poll wait. */
  restartPollIntervalMs?: number
  pendingSuccessorStart?: PendingSuccessorStart | null
  onPendingSuccessorStartChange?: (pending: PendingSuccessorStart | null) => void
  /** Opens Settings for the same project when launch prerequisites are missing. */
  onOpenSettings?: () => void
  /** Opens a newly created follow-up draft in the project Plans view. */
  onOpenPlan?: (planPath: string) => void
}

/**
 * The projection of the committed configuration pair, fetched through the pure
 * form endpoint.  The run form resolves launch values only from this plus the
 * canonical daemon capabilities — never from unsaved settings drafts.
 */
interface CommittedProjection {
  revision: string
  validationState: 'ready' | 'configuration_required' | 'invalid'
  form: GuidedFormProjection | null
  syntaxIssues: ConfigValidationIssue[]
  serverDefaultMaxTurns: number | null
}

function committedProjectionFrom(
  pair: ProjectConfig,
  projected: ProjectConfigFormResponse,
): CommittedProjection {
  return {
    revision: pair.revision,
    validationState: projected.validation.state,
    form: projected.form,
    syntaxIssues: projected.syntax_issues,
    serverDefaultMaxTurns: projected.server_default_max_turns ?? null,
  }
}

interface WorktreePreflightState {
  status: WorktreePreflightLoadState
  result: WorktreePreflight | null
  error: string | null
  identity: string
}

interface StartConfirmationToken {
  generation: number
  operationId: number
  reviewIdentity: string
  launchIdentity: string
  projectId: string
  submitted: boolean
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
  teamRoleSources?: Record<string, string>,
): RoleResolution {
  const teamSelector = teamRoles[role]
  if (typeof teamSelector === 'string' && teamSelector.trim()) {
    return {
      role,
      selector: teamSelector,
      source: teamRoleSources?.[role] === 'global' ? 'global' : 'team',
    }
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

/**
 * Holds an idempotency key only while its write outcome is uncertain.  Keeping
 * this state in a ref makes retries survive renders without persisting either
 * bearer material or write metadata in browser storage.
 */
function usePendingWriteKeys() {
  const pendingKeys = useRef(new Map<string, Map<string, string>>())

  const getKey = useCallback((operation: string, intent: Record<string, unknown>): string => {
    const serializedIntent = JSON.stringify(intent)
    const operationKeys = pendingKeys.current.get(operation) ?? new Map<string, string>()
    const pending = operationKeys.get(serializedIntent)
    if (pending) return pending

    const key = requestKey(operation)
    operationKeys.set(serializedIntent, key)
    pendingKeys.current.set(operation, operationKeys)
    return key
  }, [])

  const clearKey = useCallback((operation: string, intent: Record<string, unknown>) => {
    const operationKeys = pendingKeys.current.get(operation)
    if (!operationKeys) return
    operationKeys.delete(JSON.stringify(intent))
    if (operationKeys.size === 0) pendingKeys.current.delete(operation)
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
  const merged = [...bySequence.values()]
    .sort((left, right) => left.sequence - right.sequence)
    .slice(-MAX_TIMELINE_EVENTS)
  if (merged.length === current.length && merged.every((event, index) => (
    event === current[index] || JSON.stringify(event) === JSON.stringify(current[index])
  ))) return current
  return merged
}

function upsertRun(current: RunStatus[], next: RunStatus): RunStatus[] {
  const existing = current.findIndex((run) => run.run_id === next.run_id)
  if (existing < 0) return [...current, next]
  let changed = false
  const updated = current.map((run) => {
    if (run.run_id !== next.run_id || run.revision > next.revision) return run
    const candidate = { ...next, ...((run.history_revision ?? 0) > (next.history_revision ?? 0) ? { history_state: run.history_state, history_revision: run.history_revision } : {}) }
    if (candidate === run || JSON.stringify(candidate) === JSON.stringify(run)) return run
    changed = true
    return candidate
  })
  return changed ? updated : current
}

function reconcilePlans(current: ControlPlanePlan[], next: ControlPlanePlan[]): ControlPlanePlan[] {
  const previousByPath = new Map(current.map(plan => [plan.path, plan]))
  const reconciled = next.map(plan => {
    const previous = previousByPath.get(plan.path)
    return previous && JSON.stringify(previous) === JSON.stringify(plan) ? previous : plan
  })
  if (reconciled.length === current.length && reconciled.every((plan, index) => plan === current[index])) return current
  return reconciled
}

function timestamp(value: unknown): string {
  return formatLocalTimestamp(typeof value === 'string' ? value : null) ?? 'Not reported'
}

function conciseRunText(value: unknown, limit = 240): string | null {
  if (typeof value !== 'string' || !value.trim()) return null
  const text = value.trim().replace(/\s+/g, ' ')
  return text.length <= limit ? text : `${text.slice(0, limit - 1)}…`
}

function textEvidence(run: RunStatus, key: string): string {
  const value = run.evidence[key]
  return typeof value === 'string' || typeof value === 'number' ? String(value) : 'Not reported'
}

function contextRecord(context: RunContext | null, key: string): Record<string, unknown> | null {
  if (!context) return null
  const section = context.data[key]
  return typeof section === 'object' && section !== null && !Array.isArray(section)
    ? section as Record<string, unknown>
    : null
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

function canonicalProgressFromContext(context: RunContext | null, runId: string | null): RunProgressDetail | null {
  if (!context || !runId || context.run_id !== runId) return null
  const progress = context.data.progress
  if (typeof progress !== 'object' || progress === null || Array.isArray(progress)) return null
  if (!['complete', 'partial', 'unavailable', 'not_applicable'].includes(progress.availability)) return null
  // Older observer context uses the same `progress` key for a smaller
  // checkpoint summary.  Only the canonical contract owns the selected-run
  // detail view; keep the legacy projection on its compatibility path.
  if (!hasCanonicalProgressFields(progress)) return null
  return progress as RunProgressDetail
}

function hasCanonicalProgressFields(progress: object): boolean {
  return Object.prototype.hasOwnProperty.call(progress, 'total_checkpoints')
    && Object.prototype.hasOwnProperty.call(progress, 'approved_checkpoints')
    && Object.prototype.hasOwnProperty.call(progress, 'recorded_complete_checkpoints')
    && Object.prototype.hasOwnProperty.call(progress, 'current_checkpoint_id')
}

function looksLikeCanonicalProgress(progress: Record<string, unknown>): boolean {
  return progress.availability === 'complete'
    || progress.availability === 'not_applicable'
    || hasCanonicalProgressFields(progress)
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

const ACTIVE_TURN_STATUSES = new Set(['starting', 'running', 'active', 'in_progress'])
const PROGRESS_REASONS = new Set<RunProgressReason>([
  'missing_plan',
  'unreadable_plan',
  'non_checkpoint_plan',
  'missing_scope',
  'invalid_evidence',
])

interface CheckpointSummary {
  availability: RunProgressAvailability
  authoritative: boolean
  complete: boolean | null
  name: string | null
  index: number | null
  total: number | null
  repairing: boolean
  overlayFileName: string | null
  reason: RunProgressReason | null
  currentTurn: RunProgressTurn | null
  lastFinishedTurn: RunProgressTurn | null
}

function positiveInteger(value: unknown): number | null {
  return typeof value === 'number' && Number.isSafeInteger(value) && value > 0 ? value : null
}

function boundedNonEmptyText(value: unknown, limit: number): string | null {
  if (typeof value !== 'string') return null
  const text = value.trim()
  if (!text) return null
  return text.length <= limit ? text : `${text.slice(0, limit - 1)}…`
}

function progressReason(value: unknown): RunProgressReason | null {
  return typeof value === 'string' && PROGRESS_REASONS.has(value as RunProgressReason)
    ? value as RunProgressReason
    : null
}

function progressCheckpoint(value: unknown): { index: number | null; name: string | null } | null {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return null
  const checkpoint = value as Record<string, unknown>
  const index = positiveInteger(checkpoint.index)
  const name = boundedNonEmptyText(checkpoint.name, 240)
  return index !== null || name !== null ? { index, name } : null
}

function progressTurn(value: unknown): RunProgressTurn | null {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return null
  const turn = value as Record<string, unknown>
  const turnNumber = positiveInteger(turn.turn_number)
  const step = boundedNonEmptyText(turn.step ?? turn.step_name, 240)
  const status = boundedNonEmptyText(turn.status, 120)
  const summary = boundedNonEmptyText(turn.summary, 512)
  return turnNumber !== null || step !== null || status !== null || summary !== null
    ? { turn_number: turnNumber, step, status, summary }
    : null
}

function isActiveTurn(turn: RunProgressTurn | null): boolean {
  if (!turn?.status) return false
  return ACTIVE_TURN_STATUSES.has(turn.status.toLowerCase())
}

function isFinishedTurn(turn: RunProgressTurn | null): turn is RunProgressTurn {
  return turn !== null && turn.status !== null && !isActiveTurn(turn)
}

function overlayFileName(value: unknown): string | null {
  const path = boundedNonEmptyText(value, 512)
  if (!path) return null
  const filename = path.split(/[\\/]/).pop()
  return filename && filename !== '.' && filename !== '..' ? filename : null
}

function emptyProgressSummary(authoritative: boolean, reason: RunProgressReason | null = null): CheckpointSummary {
  return {
    availability: 'unavailable',
    authoritative,
    complete: null,
    name: null,
    index: null,
    total: null,
    repairing: false,
    overlayFileName: null,
    reason,
    currentTurn: null,
    lastFinishedTurn: null,
  }
}

function checkpointSummaryFromProjection(progress: Record<string, unknown>): CheckpointSummary {
  const availability = progress.availability
  if (availability !== 'available' && availability !== 'partial' && availability !== 'unavailable') {
    return emptyProgressSummary(true, 'invalid_evidence')
  }
  const checkpoint = progressCheckpoint(progress.checkpoint)
  const total = positiveInteger(progress.total)
  const complete = typeof progress.complete === 'boolean' ? progress.complete : null
  const lastFinishedTurn = progressTurn(progress.last_finished_turn)
  const currentTurn = progressTurn(progress.current_turn)
  return {
    availability,
    authoritative: true,
    complete: availability === 'available' ? complete : null,
    name: checkpoint?.name ?? null,
    index: checkpoint?.index ?? null,
    total,
    repairing: progress.repairing === true,
    overlayFileName: overlayFileName(progress.overlay_path),
    reason: progressReason(progress.reason),
    currentTurn,
    lastFinishedTurn: isFinishedTurn(lastFinishedTurn) ? lastFinishedTurn : null,
  }
}

function legacyScopeSummary(managerContext: Record<string, unknown>): CheckpointSummary | null {
  const controller = contextObject(managerContext.controller_state)
  const candidates = [
    managerContext.active_implementation_scope,
    controller?.active_implementation_scope,
  ]
  for (const candidate of candidates) {
    const scope = contextObject(candidate)
    if (!scope) continue
    const index = positiveInteger(scope.checkpoint_index)
    const name = boundedNonEmptyText(scope.checkpoint_name, 240)
    if (index === null || name === null) continue
    return {
      availability: 'partial',
      authoritative: false,
      complete: null,
      name,
      index,
      total: null,
      repairing: false,
      overlayFileName: null,
      reason: null,
      currentTurn: null,
      lastFinishedTurn: null,
    }
  }
  return null
}

function contextObject(value: unknown): Record<string, unknown> | null {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

function legacyPlanSummary(managerContext: Record<string, unknown>): CheckpointSummary | null {
  const planState = contextObject(managerContext.plan_state)
  if (!planState || !Array.isArray(planState.checkpoints) || planState.checkpoints.length === 0) return null
  const checkpoints = planState.checkpoints
  const valid = checkpoints.every((value, offset) => {
    const checkpoint = contextObject(value)
    return positiveInteger(checkpoint?.index) === offset + 1
      && boundedNonEmptyText(checkpoint?.name, 240) !== null
  })
  if (!valid) return null
  const currentCandidate = progressCheckpoint(planState.current_checkpoint)
  const current = currentCandidate
    && currentCandidate.index !== null
    && currentCandidate.name !== null
    && currentCandidate.index <= checkpoints.length
    && boundedNonEmptyText(
      contextObject(checkpoints[currentCandidate.index - 1])?.name,
      240,
    ) === currentCandidate.name
    ? currentCandidate
    : null
  const complete = typeof planState.is_complete === 'boolean' ? planState.is_complete : null
  return {
    availability: 'available',
    authoritative: false,
    complete: complete === true ? true : complete === false ? false : null,
    name: current?.name ?? null,
    index: current?.index ?? null,
    total: checkpoints.length,
    repairing: false,
    overlayFileName: null,
    reason: null,
    currentTurn: null,
    lastFinishedTurn: null,
  }
}

function legacyProgressSummary(context: RunContext): CheckpointSummary {
  const managerContext = contextRecord(context, 'manager_context')
  if (managerContext) {
    const scope = legacyScopeSummary(managerContext)
    if (scope) return scope
    const plan = legacyPlanSummary(managerContext)
    if (plan) return plan
  }
  return emptyProgressSummary(false)
}

function checkpointSummary(context: RunContext | null): CheckpointSummary | null {
  if (!context) return null
  if (Object.prototype.hasOwnProperty.call(context.data, 'execution_progress')) {
    const executionProgress = contextObject(context.data.execution_progress)
    return executionProgress
      ? checkpointSummaryFromProjection(executionProgress)
      : emptyProgressSummary(true)
  }
  if (Object.prototype.hasOwnProperty.call(context.data, 'progress')) {
    const progress = contextObject(context.data.progress)
    if (!progress) return emptyProgressSummary(true, 'invalid_evidence')
    // A genuinely old payload used data.progress for this smaller shape. Do
    // not reinterpret canonical history as a checkpoint summary when the
    // explicit compatibility key is absent from a retained legacy payload.
    return looksLikeCanonicalProgress(progress)
      ? legacyProgressSummary(context)
      : checkpointSummaryFromProjection(progress)
  }
  return legacyProgressSummary(context)
}

interface LastExecutedEvidence {
  turnNumber: number | null
  stepName: string | null
  role: string | null
  selector: string | null
  model: string | null
}

interface RunIssue {
  kind: 'failure' | 'attention'
  cause: string
}

interface RecoveryProvenance {
  mode: 'durable_evidence'
  sourceRunId: string
  targetRunId: string
  sourceSelector: string
  targetSelector: string
  artifactPath: string | null
  evidenceReferences: string[]
  sourceSessionContextTransferred: boolean | null
}

function recoveryWorkerEvidenceFromRun(
  run: RunStatus | null,
): RecoveryWorkerEvidence | null {
  const value = run?.evidence.recovery_worker
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return null
  const evidence = value as Record<string, unknown>
  const sessionStatus = evidence.session_status
  if (
    evidence.schema_version !== 1
    || typeof evidence.target_run_id !== 'string'
    || typeof evidence.target_selector !== 'string'
    || !evidence.target_run_id.trim()
    || !evidence.target_selector.trim()
    || !['pending', 'in_flight', 'consumed'].includes(String(evidence.operation_state))
    || typeof evidence.operation_started !== 'boolean'
    || typeof evidence.session_started !== 'boolean'
    || (evidence.session_started && !evidence.operation_started)
    || (sessionStatus !== null && !['active', 'handed_over', 'closed'].includes(String(sessionStatus)))
  ) return null
  return {
    schema_version: 1,
    target_run_id: evidence.target_run_id,
    target_selector: evidence.target_selector,
    operation_state: evidence.operation_state as RecoveryWorkerEvidence['operation_state'],
    operation_started: evidence.operation_started,
    session_started: evidence.session_started,
    session_status: sessionStatus as RecoveryWorkerEvidence['session_status'],
  }
}

function recoveryProvenanceFromEvents(events: RunEvent[]): RecoveryProvenance | null {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index]
    if (event.event_type !== 'recovery_requested') continue
    const data = event.data
    if (data.mode !== 'durable_evidence') continue
    const requiredText = (value: unknown): string | null => (
      typeof value === 'string' && value.trim() ? value.trim().slice(0, 240) : null
    )
    const sourceRunId = requiredText(data.source_run_id)
    const targetRunId = requiredText(data.target_run_id)
    const sourceSelector = requiredText(data.source_selector)
    const targetSelector = requiredText(data.target_selector)
    if (!sourceRunId || !targetRunId || !sourceSelector || !targetSelector) continue
    const evidenceReferences = Array.isArray(data.evidence)
      ? data.evidence.map((value): string | null => {
        if (typeof value === 'string') return value
        if (typeof value !== 'object' || value === null || Array.isArray(value)) return null
        const path = (value as Record<string, unknown>).path
        return typeof path === 'string' ? path : null
      }).filter((value): value is string => value !== null).slice(0, 12)
      : []
    return {
      mode: 'durable_evidence',
      sourceRunId,
      targetRunId,
      sourceSelector,
      targetSelector,
      artifactPath: requiredText(data.artifact_path),
      evidenceReferences,
      sourceSessionContextTransferred: data.source_session_context_transferred === false
        ? false
        : data.source_session_context_transferred === true ? true : null,
    }
  }
  return null
}

function runIssue(run: RunStatus): RunIssue | null {
  const category = runDisplayProjection(run).category
  const failure = category === 'failure'
  if (!failure && category !== 'actionable-attention') return null

  const startupFailure = contextObject(run.evidence.startup_failure)
  const cause = conciseRunText(run.reason)
    ?? conciseRunText(run.worker_exit?.reason)
    ?? conciseRunText(startupFailure?.message)
  if (cause) return { kind: failure ? 'failure' : 'attention', cause }

  if (run.worker_exit) {
    const phase = run.worker_exit.stage === 'wrapper_spawn'
      ? 'Worker could not be spawned'
      : run.evidence.has_run_metadata ? 'Worker exited' : 'Worker exited during startup'
    const code = run.worker_exit.exit_code === null ? '' : ` (code ${run.worker_exit.exit_code})`
    return { kind: failure ? 'failure' : 'attention', cause: `${phase}${code}. The original worker error was not retained.` }
  }

  return {
    kind: failure ? 'failure' : 'attention',
    cause: failure ? 'The run failed without a readable recorded cause.' : 'Review is needed; no short reason was recorded.',
  }
}

function runActorSummary(lastExecuted: LastExecutedEvidence | null): string {
  if (!lastExecuted) return 'Not reported'
  const rawRole = lastExecuted.role?.trim() ?? ''
  const role = rawRole.toLowerCase() === 'worker'
    ? 'Worker'
    : rawRole.toLowerCase() === 'reviewer'
      ? 'Reviewer'
      : rawRole ? formatMachineLabel(rawRole) : 'Role not reported'
  const detail = [lastExecuted.selector, lastExecuted.model].filter((value): value is string => Boolean(value)).join(' · ')
  return detail ? `${role} · ${detail}` : role
}

function runTimingSummary(run: RunStatus, elapsed: string | null): string {
  if (elapsed) return `${run.status === 'running' ? 'Running for' : 'Duration'} ${elapsed}`
  const finish = runFinishText(run)
  if (finish) return finish
  if (run.started_at) return `Started ${timestamp(run.started_at)}; duration not reported`
  return 'Not reported'
}

const LiveRunTiming = memo(function LiveRunTiming({ run }: { run: RunStatus }) {
  const [now, setNow] = useState(() => Date.now())
  const ticking = run.status === 'running' || run.activity === 'active'
  useEffect(() => {
    if (!ticking) return
    setNow(Date.now())
    const timer = setInterval(() => setNow(Date.now()), 1_000)
    return () => clearInterval(timer)
  }, [ticking, run.started_at, run.ended_at])
  return runTimingSummary(run, executionDuration(run, now))
})

const LiveRunElapsed = memo(function LiveRunElapsed({ run, label }: { run: RunStatus; label: string }) {
  const [now, setNow] = useState(() => Date.now())
  const ticking = run.status === 'running' || run.activity === 'active'
  useEffect(() => {
    if (!ticking) return
    setNow(Date.now())
    const timer = setInterval(() => setNow(Date.now()), 1_000)
    return () => clearInterval(timer)
  }, [ticking, run.started_at, run.ended_at])
  const elapsed = executionDuration(run, now)
  return elapsed ? ` · ${label} ${elapsed}` : null
})

const LiveRunHeaderTiming = memo(function LiveRunHeaderTiming({ run }: { run: RunStatus }) {
  const [now, setNow] = useState(() => Date.now())
  const ticking = run.status === 'running' || run.activity === 'active'
  useEffect(() => {
    if (!ticking) return
    setNow(Date.now())
    const timer = setInterval(() => setNow(Date.now()), 1_000)
    return () => clearInterval(timer)
  }, [ticking, run.started_at, run.ended_at])
  const timing = runTimingSummary(run, executionDuration(run, now))
  return timing === 'Not reported' ? null : <span className="run-overview-header-duration">{timing}</span>
})

function lastExecutedEvidence(
  events: RunEvent[],
  context: RunContext | null,
): LastExecutedEvidence | null {
  const textValue = (record: Record<string, unknown>, ...keys: string[]): string | null => {
    for (const key of keys) {
      const value = record[key]
      if (typeof value === 'string' && value.trim()) return value.trim().slice(0, 240)
    }
    return null
  }

  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index]
    if (event.event_type !== 'turn_started') continue
    const data = event.data
    const evidence = {
      turnNumber: typeof data.turn_number === 'number' ? data.turn_number : null,
      stepName: textValue(data, 'step_name'),
      role: textValue(data, 'step_role', 'role'),
      selector: textValue(data, 'resolved_selector', 'selector'),
      model: textValue(data, 'resolved_model_display', 'model', 'resolved_model'),
    }
    if (evidence.turnNumber !== null || evidence.stepName || evidence.role || evidence.selector || evidence.model) {
      return evidence
    }
  }

  const managerContext = contextRecord(context, 'manager_context')
  const finishedTurn = managerContext?.finished_turn
  if (typeof finishedTurn !== 'object' || finishedTurn === null || Array.isArray(finishedTurn)) {
    return null
  }
  const evidence = finishedTurn as Record<string, unknown>
  const fallback = {
    turnNumber: typeof evidence.turn_number === 'number' ? evidence.turn_number : null,
    stepName: textValue(evidence, 'step_name'),
    role: textValue(evidence, 'role', 'step_role'),
    selector: textValue(evidence, 'selector', 'resolved_selector'),
    model: textValue(evidence, 'resolved_model_display', 'model', 'resolved_model'),
  }
  return fallback.turnNumber !== null || fallback.stepName || fallback.role || fallback.selector || fallback.model
    ? fallback
    : null
}

interface ManagerOutcome {
  decision: string | null
  currentTurn: string | null
  finishedTurn: string | null
  finishedSummary: string | null
  resultText: string | null
}

function boundedText(value: unknown, limit: number): string | null {
  if (typeof value !== 'string' || !value.trim()) return null
  return value.length <= limit ? value : `${value.slice(0, limit)}…`
}

function progressTurnText(turn: RunProgressTurn | null): string | null {
  if (!turn) return null
  const parts: string[] = []
  if (turn.turn_number !== null) parts.push(`turn ${turn.turn_number}`)
  if (turn.step) parts.push(formatMachineLabel(turn.step))
  if (turn.status) parts.push(turn.status)
  return parts.length > 0 ? parts.join(' · ') : null
}

function hasLegacyFinishEvidence(turn: Record<string, unknown>): boolean {
  return typeof turn.finished_at === 'string'
    || typeof turn.ended_at === 'string'
    || typeof turn.returncode === 'number'
    || contextObject(turn.semantic_result) !== null
    || boundedNonEmptyText(turn.error, 240) !== null
    || Array.isArray(turn.raw_artifacts)
}

function managerOutcome(context: RunContext | null, progress: CheckpointSummary | null): ManagerOutcome | null {
  const managerContext = contextRecord(context, 'manager_context')
  let decision: string | null = null
  if (managerContext) {
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
  }
  let currentTurn: string | null = null
  let finishedTurn: string | null = null
  let finishedSummary: string | null = null
  let resultText: string | null = null
  if (progress?.authoritative) {
    currentTurn = progress.currentTurn && !isFinishedTurn(progress.currentTurn)
      ? progressTurnText(progress.currentTurn)
      : null
    const lastFinished = progress.lastFinishedTurn
    if (isFinishedTurn(lastFinished)) {
      finishedTurn = progressTurnText(lastFinished)
      finishedSummary = lastFinished.summary
    }
  } else if (managerContext) {
    const current = progressTurn(managerContext.current_turn)
    currentTurn = current && !isFinishedTurn(current) ? progressTurnText(current) : null
    const finished = contextObject(managerContext.finished_turn)
    const normalizedFinished = progressTurn(finished)
    if (normalizedFinished !== null && isFinishedTurn(normalizedFinished) && finished && hasLegacyFinishEvidence(finished)) {
      const parts: string[] = []
      if (normalizedFinished.turn_number !== null) parts.push(`turn ${normalizedFinished.turn_number}`)
      if (normalizedFinished.step) parts.push(formatMachineLabel(normalizedFinished.step))
      if (normalizedFinished.status) parts.push(normalizedFinished.status)
      if (typeof finished.returncode === 'number') parts.push(`exit ${finished.returncode}`)
      finishedTurn = parts.length ? parts.join(' · ') : null
      const semantic = contextObject(finished.semantic_result)
      resultText = semantic ? boundedText(semantic.result, 240) : boundedText(finished.error, 240)
    }
  }
  if (!decision && !currentTurn && !finishedTurn && !finishedSummary && !resultText) return null
  return { decision, currentTurn, finishedTurn, finishedSummary, resultText }
}

function progressUnavailableReason(reason: RunProgressReason | null): string {
  switch (reason) {
    case 'missing_plan': return 'the original plan is unavailable'
    case 'unreadable_plan': return 'the original plan could not be read'
    case 'non_checkpoint_plan': return 'the source is not a checkpoint plan'
    case 'missing_scope': return 'the active checkpoint scope is unavailable'
    case 'invalid_evidence': return 'the recorded progress evidence is invalid'
    default: return 'verified progress evidence was not returned'
  }
}

function checkpointProgressText(progress: CheckpointSummary): string {
  if (progress.availability === 'unavailable') {
    return `Progress unavailable — ${progressUnavailableReason(progress.reason)}.`
  }
  if (progress.availability === 'available' && progress.complete === true) {
    return progress.total !== null
      ? `All ${progress.total} checkpoints complete`
      : 'Completed (verified)'
  }
  const name = progress.name ?? (progress.index !== null ? `Checkpoint ${progress.index}` : null)
  if (name) {
    if (progress.total !== null && progress.index !== null && progress.index <= progress.total) {
      return `${name} (${progress.index} of ${progress.total})`
    }
    return progress.index !== null ? `${name} (${progress.index})` : name
  }
  return progress.total !== null
    ? `Progress available — current checkpoint not reported (${progress.total} checkpoints)`
    : 'Progress available — current checkpoint not reported'
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

function followupDraftErrorMessage(error: unknown, name: string): string {
  const code = apiErrorCode(error)
  if (code === 'plan_exists' || (error instanceof ApiError && error.status === 409)) {
    return `A draft named ${name} already exists. Choose a different filename and try again.`
  }
  if (code === 'operation_forbidden' || (error instanceof ApiError && error.status === 403)) {
    return `You do not have permission to create ${name} in this project. Check the project access and try again.`
  }
  if (code === 'run_not_found' || code === 'run_deleted' || (error instanceof ApiError && error.status === 404)) {
    return `The selected source run is no longer available. Refresh the run details before creating a follow-up draft.`
  }
  if (code === 'control_plane_unavailable' || (error instanceof ApiError && error.status >= 500)) {
    return `The selected run evidence is temporarily unavailable. Refresh the run details and try again; no new filename was chosen.`
  }
  if (code === 'operation_rejected' || (error instanceof ApiError && error.status === 422)) {
    return `The filename ${name} was rejected. Correct it and try again.`
  }
  return `Could not create ${name}: ${errorMessage(error, 'the selected run evidence could not be read')}. Correct the filename or refresh the run, then try again.`
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

export function RunDashboard({ visible = true, page, onNewRun, onCancelNewRun, onRunStarted, projectId, requestedRunId = null, explicitRunNavigation, onRunSelectionChange, initialPlanPath, onInitialPlanHandled, restartPollIntervalMs, pendingSuccessorStart: suppliedPendingSuccessor, onPendingSuccessorStartChange, onOpenSettings, onOpenPlan }: RunDashboardProps) {
  // Project access and the direct snapshot are evidence, not timing signals.
  // Keep their identities with the values so a render for a new project/run
  // cannot briefly reuse an accepted result before its cleanup effect runs.
  const [projectAvailability, setProjectAvailability] = useState<{ projectId: string; available: boolean } | null>(null)
  const projectAvailable = projectAvailability?.projectId === projectId
    ? projectAvailability.available
    : null
  const [acceptedSelectedDetail, setAcceptedSelectedDetail] = useState<{ projectId: string; runId: string } | null>(null)
  const [exposedShellProjectId, setExposedShellProjectId] = useState<string | null>(null)
  const [capabilities, setCapabilities] = useState<ControlPlaneCapabilities | null>(null)
  const [readiness, setReadiness] = useState<ControlPlaneReadiness | null>(null)
  const [plans, setPlans] = useState<ControlPlanePlan[]>([])
  const [committed, setCommitted] = useState<CommittedProjection | null>(null)
  const [committedError, setCommittedError] = useState<string | null>(null)
  const [historyFilter, setHistoryFilter] = useState<'visible' | 'archived' | 'all'>('visible')
  const historyFilterRef = useRef(historyFilter)
  historyFilterRef.current = historyFilter
  const currentHistoryScope = JSON.stringify([projectId, historyFilter])
  const [historyLoad, setHistoryLoad] = useState<{ scope: string; status: 'pending' | 'ready' | 'error' }>({ scope: '', status: 'pending' })
  const [historySnapshot, setHistorySnapshot] = useState<{ scope: string; runIds: string[] }>({ scope: '', runIds: [] })
  const [gapDisclosure, setGapDisclosure] = useState<{ scope: string; open: boolean }>({ scope: '', open: false })
  const runListRef = useRef<HTMLElement>(null)
  const pendingPinTransitionRef = useRef<{ runId: string; scrollTop: number; focus: 'select' | 'preview' | null } | null>(null)
  const loadedHistory = useRef({ scope: '', pages: 1 })
  const historyRequest = useRef(0)
  const [nextRunCursor, setNextRunCursor] = useState<string | null>(null)
  const [navigationVersion, setNavigationVersion] = useState(0)
  const [deletedIds, setDeletedIds] = useState<Set<string>>(new Set())
  const deletedRef = useRef(new Set<string>())
  const historyIntents = useRef(new Map<string, { action: 'archive' | 'restore' | 'delete'; revision: number; key: string; acknowledged: boolean }>())
  const [historyConfirm, setHistoryConfirm] = useState<'archive' | 'delete' | null>(null)
  const [acknowledgeActive, setAcknowledgeActive] = useState(false)
  const [runs, setRuns] = useState<RunStatus[]>([])
  const [selectedRunId, setSelectedRunId] = useState<string | null>(requestedRunId)
  const [missingRunId, setMissingRunId] = useState<string | null>(null)
  const [events, setEvents] = useState<RunEvent[]>([])
  const [context, setContext] = useState<RunContext | null>(null)
  const runsRef = useRef(runs)
  const contextRef = useRef(context)
  runsRef.current = runs
  contextRef.current = context
  const [rawOpen, setRawOpen] = useState(false)
  const [reportOpen, setReportOpen] = useState(false)
  const [contextBusy, setContextBusy] = useState(false)
  const [contextError, setContextError] = useState<string | null>(null)
  const [statusUpdatedAt, setStatusUpdatedAt] = useState<string | null>(null)
  const [contextUpdatedAt, setContextUpdatedAt] = useState<string | null>(null)
  const [streamState, setStreamState] = useState<api.StreamState>('stopped')
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [busyAction, setBusyAction] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [followupDraft, setFollowupDraft] = useState<{ runId: string; name: string } | null>(null)
  const [followupError, setFollowupError] = useState<string | null>(null)
  const [projectReadError, setProjectReadError] = useState<string | null>(null)
  const [dashboardReadError, setDashboardReadError] = useState<string | null>(null)
  const [selectedRunReadError, setSelectedRunReadError] = useState<string | null>(null)
  const [streamNotice, setStreamNotice] = useState<string | null>(null)
  const [feedback, setFeedback] = useState<string | null>(null)
  const [handoffError, setHandoffError] = useState<string | null>(null)
  const [startupQuestion, setStartupQuestion] = useState<StartupQuestion | null>(null)
  const [dirtyWorktreeConfirmed, setDirtyWorktreeConfirmed] = useState(false)
  const [worktreePreflight, setWorktreePreflight] = useState<WorktreePreflightState>({
    status: 'idle',
    result: null,
    error: null,
    identity: '',
  })
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
  const [stopReviewOpen, setStopReviewOpen] = useState(false)
  const [adjustRunOpen, setAdjustRunOpen] = useState(false)
  const [recoveryOpen, setRecoveryOpen] = useState(false)
  const [recoverySelector, setRecoverySelector] = useState('')
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
  // New run is a compact disclosure: it opens for an exact plan handoff, when
  // no runs exist, or when a frozen successor draft needs attention.
  const [localPage, setLocalPage] = useState<'runs' | 'new-run'>(initialPlanPath ? 'new-run' : 'runs')
  const newRunPage = (page ?? localPage) === 'new-run'
  function openNewRunPage() { setLocalPage('new-run'); onNewRun?.() }
  const [failedRequestId, setFailedRequestId] = useState<string | null>(null)
  const [advancedOpen, setAdvancedOpen] = useState(false)
  const [startReviewIdentity, setStartReviewIdentity] = useState<string | null>(null)
  const [startRevalidationPending, setStartRevalidationPending] = useState(false)
  const startReviewIdentityRef = useRef<string | null>(null)
  const startReviewGenerationRef = useRef(0)
  const startReviewContextRef = useRef<string | null>(null)
  const startConfirmationRef = useRef<StartConfirmationToken | null>(null)
  const startSubmittedOperationRef = useRef<StartConfirmationToken | null>(null)
  const startConfirmationSequenceRef = useRef(0)
  const startReviewProjectRef = useRef(projectId)
  const startReviewVisibleRef = useRef(visible)
  const startReviewPageRef = useRef(newRunPage)
  const [technicalOpenByRun, setTechnicalOpenByRun] = useState<Record<string, boolean>>({})
  const [runChangesOpenByRun, setRunChangesOpenByRun] = useState<Record<string, boolean>>({})
  const technicalId = useId()
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'failed'>('idle')
  const [copyKind, setCopyKind] = useState<'link' | 'run-id' | null>(null)

  useEffect(() => {
    if (newRunPage && visible) return
    startReviewGenerationRef.current += 1
    startConfirmationRef.current = null
    startReviewIdentityRef.current = null
    setStartReviewIdentity(null)
    setStartRevalidationPending(false)
  }, [newRunPage, visible])
  useEffect(() => () => {
    startReviewGenerationRef.current += 1
    startConfirmationRef.current = null
    startSubmittedOperationRef.current = null
  }, [])
  const copyRequestRef = useRef(0)
  const [refreshNonce, setRefreshNonce] = useState(0)
  const selectedRunRef = useRef<string | null>(selectedRunId)
  const requestedRunRef = useRef<string | null>(requestedRunId)
  const missingRunRef = useRef<string | null>(null)
  const restartFocusPendingRef = useRef(false)
  const startReviewTriggerRef = useRef<HTMLElement | null>(null)
  const snapshotRequestRef = useRef(0)
  const contextRequestRef = useRef(0)
  const followupRequestRef = useRef(0)
  const followupBusyRequestRef = useRef<number | null>(null)
  const requestAbortRef = useRef(new AbortController())
  const contextAbortRef = useRef(new AbortController())
  const preflightRequestRef = useRef(0)
  const preflightAbortRef = useRef(new AbortController())
  const desiredContextLevelRef = useRef<'lite' | 'full'>('lite')
  const disclosureRunKey = selectedRunId ? `${projectId}/${selectedRunId}` : null
  const technicalOpen = disclosureRunKey !== null && technicalOpenByRun[disclosureRunKey] === true
  const runChangesOpen = disclosureRunKey !== null && runChangesOpenByRun[disclosureRunKey] === true
  desiredContextLevelRef.current = technicalOpen && rawOpen && capabilities?.context_levels.includes('full') ? 'full' : 'lite'
  useEffect(() => {
    requestAbortRef.current = new AbortController()
    return () => { requestAbortRef.current.abort(); contextAbortRef.current.abort() }
  }, [projectId, selectedRunId, visible])
  useEffect(() => () => {
    preflightRequestRef.current += 1
    preflightAbortRef.current.abort()
  }, [projectId, visible])
  const diagnosticsLoadedForRef = useRef<string | null>(null)
  const controlsForRunRef = useRef<string | null>(null)
  const previousStreamStateRef = useRef<api.StreamState>('stopped')
  const onRunSelectionChangeRef = useRef(onRunSelectionChange)
  onRunSelectionChangeRef.current = onRunSelectionChange
  missingRunRef.current = missingRunId
  const { getKey: getPendingWriteKey, clearKey: clearPendingWriteKey } = usePendingWriteKeys()

  const selectedRun = useMemo(
    () => runs.find((run) => run.run_id === selectedRunId) ?? null,
    [runs, selectedRunId],
  )
  const selectedDetailAccepted = Boolean(
    selectedRun
    && selectedRunId
    && acceptedSelectedDetail?.projectId === projectId
    && acceptedSelectedDetail.runId === selectedRunId,
  )

  useEffect(() => {
    if (projectAvailable === true && selectedDetailAccepted) setExposedShellProjectId(projectId)
  }, [projectAvailable, projectId, selectedDetailAccepted])

  useLayoutEffect(() => {
    // Clear selection-scoped state before the new run's form can be edited.
    // A passive effect can erase a fast filename edit after navigation.
    selectedRunRef.current = selectedRunId
    followupRequestRef.current += 1
    setFollowupDraft(null)
    setFollowupError(null)
  }, [projectId, selectedRunId])

  useEffect(() => {
    if (!visible || !selectedRunId) return
    let active = true
    // Keep an already-admitted action menu mounted while a status refresh
    // requests the same run again. The direct snapshot and history snapshot
    // can settle in either order; clearing the same-run admission here would
    // make the menu disappear between those two responses.
    setRestartAdmission(current => current?.run_id === selectedRunId ? current : null)
    void api.getRestartOptions(projectId, selectedRunId).then(result => {
      if (active) setRestartAdmission(result)
    }).catch(() => { /* The run remains readable if restart admission is unavailable. */ })
    return () => { active = false }
  }, [visible, projectId, selectedRunId, selectedRun?.status, refreshNonce])

  function openRestart() {
    if (!selectedRun) return
    restartFocusPendingRef.current = true
    setStopReviewOpen(false)
    setAdjustRunOpen(false)
    setConfirmOwnerStop(false)
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
    if (!restartFocusPendingRef.current || !newRunPage || !restartSource || restartPhase !== 'confirming') return
    const planInput = document.querySelector<HTMLElement>('.start-run-form [aria-label="Run plan"]')
    if (!planInput) return
    planInput.focus()
    restartFocusPendingRef.current = false
  }, [newRunPage, restartPhase, restartSource])

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

  // The public URL is authoritative for a requested run: every new request
  // selects exactly that run and is validated through the direct
  // project-scoped endpoint. Clearing the request keeps the visible
  // selection; the dashboard reports it back so the URL is re-synced.
  useEffect(() => {
    if (requestedRunId === requestedRunRef.current) return
    requestedRunRef.current = requestedRunId
    setActionError(null)
    setFailedRequestId(null)
    if (requestedRunId !== null) {
      copyRequestRef.current += 1
      setMissingRunId(null)
      setSelectedRunId(requestedRunId)
    }
  }, [requestedRunId])

  // Clipboard feedback describes the most recent identity copy; a new
  // selection starts a new one and invalidates completions from older writes.
  useEffect(() => {
    copyRequestRef.current += 1
    setCopyState('idle')
    setCopyKind(null)
  }, [projectId, selectedRunId])

  useEffect(() => {
    setReportOpen(false)
  }, [selectedRunId])

  // Recovery is a draft on the selected source. Only a different source run
  // should clear it; status/detail refreshes for the same run must preserve
  // the owner's open form and selected replacement worker.
  useEffect(() => {
    setRecoveryOpen(false)
    setRecoverySelector('')
  }, [selectedRunId])

  useEffect(() => {
    if (!selectedRun || controlsForRunRef.current === selectedRun.run_id) return
    controlsForRunRef.current = selectedRun.run_id
    setControlMaxTurns(selectedRun.max_turns?.toString() ?? '')
    setControlTeam(selectedRun.team ?? '')
    setRoleSelectors({})
    setConfirmOwnerStop(false)
    setConfirmResume(false)
  }, [selectedRun])

  // The registered project id is the sole authority: no root matching, no
  // internal switcher, and never a fallback to another control-plane project.
  useEffect(() => {
    if (!visible) return
    let active = true
    void (async () => {
      try {
        setLoading(true)
        setProjectAvailability(null)
        const [available, readinessState] = await Promise.all([
          api.listControlPlaneProjects(),
          api.getControlPlaneReadiness(),
        ])
        if (!active) return
        setProjectReadError(null)
        setReadiness(readinessState)
        const availableHere = available.some((project) => project.project_id === projectId)
        setProjectAvailability({ projectId, available: availableHere })
        if (!availableHere) {
          setAcceptedSelectedDetail(null)
          setExposedShellProjectId(null)
        }
        if (availableHere) await loadDashboard(projectId, () => active)
      } catch (loadError) {
        if (active) setProjectReadError(errorMessage(loadError, 'Failed to load control-plane projects'))
      } finally {
        if (active) {
          setLoading(false)
          // A stream-triggered background read may supersede the initial
          // dashboard request; it must not strand the initial refresh label.
          setRefreshing(false)
        }
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
    if (!visible || !projectId || !selectedRunId || deletedIds.has(selectedRunId)) return
    let active = true
    setEvents([])
    contextRef.current = null
    setContext(null)
    setStatusUpdatedAt(null)
    setContextUpdatedAt(null)
    setContextError(null)
    setSelectedRunReadError(null)
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
        void refreshPageRef.current({ background: true }).finally(() => {
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
  }, [visible, projectId, selectedRunId, deletedIds])

  async function loadDashboard(nextProjectId: string, isActive = () => true, { background = false }: { background?: boolean } = {}) {
    const requestedHistory = historyFilter
    const scope = JSON.stringify([nextProjectId, requestedHistory])
    if (loadedHistory.current.scope !== scope) loadedHistory.current = { scope, pages: 1 }
    const pageCount = loadedHistory.current.pages
    const request = ++historyRequest.current
    const callerActive = isActive
    isActive = () => callerActive() && requestedHistory === historyFilterRef.current && request === historyRequest.current
    // A retained, ready history snapshot remains authoritative while a
    // background read is in flight. Only a new scope enters the initial
    // pending state; failures later become an explicit stale notice.
    if (historyLoad.scope !== scope) setHistoryLoad({ scope, status: 'pending' })
    async function reloadHistory() {
      const runs: RunStatus[] = []
      let cursor: string | undefined
      const seen = new Set<string>()
      try {
        for (let index = 0; index < pageCount; index++) {
          const page = await api.listControlPlaneRuns(nextProjectId, { limit: 100, history: requestedHistory, order: 'recent', ...(cursor ? { cursor } : {}) })
          runs.push(...page.runs)
          cursor = page.next_cursor ?? undefined
          if (!cursor || !isActive()) break
          if (seen.has(cursor)) throw new Error('Repeated history cursor')
          seen.add(cursor)
        }
        const result = { runs, next_cursor: cursor ?? null }
        if (isActive()) {
          setHistoryLoad(current => current.scope === scope && current.status === 'ready'
            ? current
            : { scope, status: 'ready' })
          const runIds = result.runs.map(run => run.run_id)
          const pinnedRow = runListRef.current?.querySelector<HTMLElement>('.run-list-pinned')
          if (pinnedRow && runIds.includes(pinnedRow.dataset.runKey ?? '')) {
            const focused = document.activeElement
            pendingPinTransitionRef.current = {
              runId: pinnedRow.dataset.runKey!,
              scrollTop: document.scrollingElement?.scrollTop ?? 0,
              focus: focused && pinnedRow.contains(focused)
                ? focused.classList.contains('run-row-preview-toggle') ? 'preview' : 'select'
                : null,
            }
          }
          setHistorySnapshot(current => current.scope === scope
            && current.runIds.length === runIds.length
            && current.runIds.every((runId, index) => runId === runIds[index])
            ? current
            : { scope, runIds })
        }
        return result
      } catch (error) {
        if (isActive()) setHistoryLoad({ scope, status: 'error' })
        throw error
      }
    }
    try {
      if (!background) setRefreshing(true)
      const [nextReadiness, nextCapabilities, nextPlans, page] = await Promise.all([
        api.getControlPlaneReadiness(),
        api.getControlPlaneCapabilities(nextProjectId),
        api.listControlPlanePlans(nextProjectId),
        reloadHistory(),
      ])
      if (!isActive()) return
      setDashboardReadError(null)
      setReadiness(current => JSON.stringify(current) === JSON.stringify(nextReadiness) ? current : nextReadiness)
      setCapabilities(current => JSON.stringify(current) === JSON.stringify(nextCapabilities) ? current : nextCapabilities)
      setPlans(current => reconcilePlans(current, nextPlans))
      setNextRunCursor(page.next_cursor)
      setExposedShellProjectId(nextProjectId)
      const orderedRuns = page.runs.filter(run => !deletedRef.current.has(run.run_id))
      setRuns((current) => {
        // Refreshed pages never silently displace a run that was fetched
        // directly (a requested link target or the current selection): it is
        // retained until the page itself carries it again.
        const pageIds = new Set(page.runs.map((run) => run.run_id))
        const pinned = current.filter((run) => !pageIds.has(run.run_id)
          && (run.run_id === requestedRunRef.current || run.run_id === selectedRunRef.current))
        const refreshed = [...orderedRuns.map(run => {
          const previous = current.find(item => item.run_id === run.run_id)
          const next = run.run_id === selectedRunRef.current && previous ? previous : run
          const history = previous && (previous.history_revision ?? 0) > (run.history_revision ?? 0) ? previous : run
          const candidate = { ...next, history_state: history.history_state, history_revision: history.history_revision }
          return previous && JSON.stringify(previous) === JSON.stringify(candidate) ? previous : candidate
        }), ...pinned]
        return refreshed.length === current.length && refreshed.every((run, index) => run === current[index]) ? current : refreshed
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
        const nextCommitted = committedProjectionFrom(committedPair, projected)
        setCommitted(current => JSON.stringify(current) === JSON.stringify(nextCommitted) ? current : nextCommitted)
        setCommittedError(null)
      } catch (projectionError) {
        if (!isActive()) return
        setCommittedError(errorMessage(projectionError, 'Failed to read the committed configuration'))
      }
    } catch (loadError) {
      if (!isActive()) return
      // Preserve the last daemon snapshot: a connection failure is not a run transition.
      setDashboardReadError(`${errorMessage(loadError, 'Failed to refresh runs')}. Existing run data remains visible.`)
    } finally {
      if (isActive() && !background) setRefreshing(false)
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
      if (deletedRef.current.has(runId)) return
      setSelectedRunReadError(null)
      const refreshedRuns = upsertRun(runsRef.current, run)
      setRuns((current) => upsertRun(current, run))
      setAcceptedSelectedDetail(current => current?.projectId === nextProjectId && current.runId === runId
        ? current
        : { projectId: nextProjectId, runId })
      if (refreshedRuns !== runsRef.current) setStatusUpdatedAt(new Date().toISOString())
      setEvents((current) => mergeEvents(current, tail))
    } catch (loadError) {
      if (!isActive() || selectedRunRef.current !== runId || requestNumber !== snapshotRequestRef.current) return
      if (loadError instanceof ApiError && loadError.status === 410) {
        setSelectedRunReadError(null)
        markDeleted(runId)
        return
      }
      if (loadError instanceof ApiError && loadError.status === 404) {
        // A linked run that does not exist selects no substitute: the Runs
        // view keeps its list and New run offer, and the URL drops the id.
        setSelectedRunReadError(null)
        setMissingRunId(runId)
        setSelectedRunId(null)
        onRunSelectionChangeRef.current?.({ runId: null, userInitiated: false, missingRunId: runId })
        return
      }
      setSelectedRunReadError(errorMessage(loadError, 'Failed to load run details'))
    }
  }

  const pageRefreshRef = useRef<Promise<void> | null>(null)
  const manualRefreshRef = useRef<Promise<void> | null>(null)
  const refreshEpochRef = useRef(0)
  useEffect(() => {
    refreshEpochRef.current += 1
    pageRefreshRef.current = null
    manualRefreshRef.current = null
    return () => { requestAbortRef.current.abort(); contextAbortRef.current.abort(); refreshEpochRef.current += 1; snapshotRequestRef.current += 1; contextRequestRef.current += 1 }
  }, [projectId, selectedRunId, visible])
  async function refreshPage({ background = false }: { background?: boolean } = {}) {
    if (!visible || !projectId) return
    if (projectAvailable !== true) { setRefreshNonce(nonce => nonce + 1); return }
    const epoch = refreshEpochRef.current
    const active = () => epoch === refreshEpochRef.current
    const startRefresh = (runInBackground: boolean): Promise<void> => {
      let task: Promise<void>
      task = (async () => {
        if (!runInBackground) setRefreshing(true)
        if (handoffError) setRefreshNonce(nonce => nonce + 1)
        await loadDashboard(projectId, active, { background: runInBackground })
        if (!active()) return
        if (!runInBackground) setRefreshing(true)
        await Promise.all([
          selectedRunId ? loadSelectedRun(projectId, selectedRunId, active) : Promise.resolve(),
          loadContext(desiredContextLevelRef.current, selectedRunId, { background: runInBackground }),
          newRunPage ? refreshPreflight(runInBackground) : Promise.resolve(),
        ])
      })().finally(() => {
        if (active() && pageRefreshRef.current === task) {
          pageRefreshRef.current = null
          if (!runInBackground) setRefreshing(false)
        }
      })
      pageRefreshRef.current = task
      return task
    }

    if (!background && manualRefreshRef.current) return manualRefreshRef.current
    if (pageRefreshRef.current) {
      if (background) return pageRefreshRef.current
      const olderRefresh = pageRefreshRef.current
      let queuedRefresh: Promise<void>
      queuedRefresh = olderRefresh.catch(() => undefined).then(() => {
        if (!active() || !visible || !projectId) return
        return startRefresh(false)
      }).finally(() => {
        if (manualRefreshRef.current === queuedRefresh) manualRefreshRef.current = null
      })
      manualRefreshRef.current = queuedRefresh
      return queuedRefresh
    }

    const task = startRefresh(background)
    if (background) return task
    let manualRefresh: Promise<void>
    manualRefresh = task.finally(() => {
      if (manualRefreshRef.current === manualRefresh) manualRefreshRef.current = null
    })
    manualRefreshRef.current = manualRefresh
    return manualRefresh
  }
  const refreshPageRef = useRef(refreshPage)
  refreshPageRef.current = refreshPage
  useEffect(() => {
    const refreshInBackground = (): void => {
      // markDeleted already owns the selected deleted-record projection;
      // do not let the broadcast used by the global history view replace it
      // before the user can review the retained recovery message.
      if (selectedRunRef.current && deletedRef.current.has(selectedRunRef.current)) return
      void refreshPageRef.current({ background: true })
    }
    window.addEventListener('aflow-history-changed', refreshInBackground)
    return () => window.removeEventListener('aflow-history-changed', refreshInBackground)
  }, [projectId, visible])
  async function refreshSelectedRun() { await refreshPage() }

  /** An explicit run pick: cleared stale-link guidance and a history push report. */
  async function loadMoreRuns() {
    if (!nextRunCursor || refreshing) return
    loadedHistory.current.pages += 1
    await loadDashboard(projectId)
  }
  function markDeleted(runId: string) {
    deletedRef.current.add(runId)
    setDeletedIds(new Set(deletedRef.current))
    setAcceptedSelectedDetail((current) => current?.projectId === projectId && current.runId === runId ? null : current)
    setRuns(current => current.filter(run => run.run_id !== runId))
    snapshotRequestRef.current += 1
    window.dispatchEvent(new Event('aflow-history-changed'))
  }
  async function mutateHistory(action: 'archive' | 'restore' | 'delete') {
    if (!selectedRun || busyAction === 'history') return
    const runId = selectedRun.run_id
    const identity = JSON.stringify([projectId, runId, action])
    const pending = historyIntents.current.get(identity) ?? { action, revision: selectedRun.history_revision ?? 0, key: requestKey('history'), acknowledged: acknowledgeActive }
    historyIntents.current.set(identity, pending)
    setBusyAction('history'); clearActionFeedback()
    try {
      const result = await api.changeRunHistory(projectId, runId, action, pending.revision, pending.key, pending.acknowledged)
      historyIntents.current.delete(identity)
      if (result.state === 'deleted') markDeleted(runId)
      else setRuns(current => current.map(run => run.run_id === runId ? { ...run, history_state: result.state, history_revision: result.revision } : run))
      setHistoryConfirm(null); setAcknowledgeActive(false)
      window.dispatchEvent(new Event('aflow-history-changed'))
    } catch (reason) {
      if (reason instanceof ApiError && [400, 403, 404, 409, 410, 422].includes(reason.status)) {
        historyIntents.current.delete(identity)
        await loadSelectedRun(projectId, runId)
      }
      setActionError(`${errorMessage(reason, 'History update failed')}. Your action is still pending; retry after reviewing the current record.`)
    } finally { setBusyAction(null) }
  }
  useEffect(() => { if (projectAvailable) void loadDashboard(projectId) }, [historyFilter]) // eslint-disable-line react-hooks/exhaustive-deps
  const listedRuns = runs.filter(run => !deletedIds.has(run.run_id) && (run.history_state ?? 'visible') !== 'deleted' && (historyFilter === 'all' || (run.history_state ?? 'visible') === historyFilter))
  const historyLoadCurrent = historyLoad.scope === currentHistoryScope ? historyLoad : null
  const historyPending = historyLoadCurrent?.status === 'pending' || (historyLoadCurrent === null && loading)
  const historyIncomplete = historyPending || historyLoadCurrent?.status === 'error'
  const historyLoadedCount = historySnapshot.scope === currentHistoryScope
    ? listedRuns.filter(run => historySnapshot.runIds.includes(run.run_id)).length
    : 0
  const historyCountLabel = historyIncomplete
    ? `${historyLoadedCount} loaded so far`
    : `${historyLoadedCount} ${nextRunCursor ? 'loaded' : 'recorded'}`
  const selectedRunOutsideLoadedHistory = Boolean(selectedRun && (
    historyIncomplete
    || historySnapshot.scope !== currentHistoryScope
    || !historySnapshot.runIds.includes(selectedRun.run_id)
  ))
  const pageIds = historySnapshot.scope === currentHistoryScope ? new Set(historySnapshot.runIds) : new Set<string>()
  const pageRuns = listedRuns.filter(run => pageIds.has(run.run_id))
  const pinnedRun = selectedRunOutsideLoadedHistory && selectedRun && !pageIds.has(selectedRun.run_id)
    && listedRuns.some(run => run.run_id === selectedRun.run_id) ? selectedRun : null
  const ordinaryRuns = pageRuns.filter(run => runDisplayProjection(run).category !== 'outcome-unrecorded')
  const outcomeGapRuns = pageRuns.filter(run => runDisplayProjection(run).category === 'outcome-unrecorded')
  const selectedGapInGroup = outcomeGapRuns.some(run => run.run_id === selectedRunId)
  const gapUserOpen = gapDisclosure.scope === currentHistoryScope && gapDisclosure.open
  const gapOpen = selectedGapInGroup || gapUserOpen
  useLayoutEffect(() => {
    const transition = pendingPinTransitionRef.current
    if (!transition) return
    if (selectedRunId !== transition.runId) {
      pendingPinTransitionRef.current = null
      return
    }
    if (pinnedRun || !pageIds.has(transition.runId)) return
    pendingPinTransitionRef.current = null
    const row = Array.from(runListRef.current?.querySelectorAll<HTMLElement>('.run-list-item') ?? [])
      .find(item => item.dataset.runKey === transition.runId)
    const focusTarget = row?.querySelector<HTMLElement>(transition.focus === 'preview' ? '.run-row-preview-toggle' : '.run-list-select')
    if (transition.focus && focusTarget) focusTarget.focus({ preventScroll: true })
    if (document.scrollingElement) document.scrollingElement.scrollTop = transition.scrollTop
  }, [historySnapshot, pinnedRun, pageIds, selectedRunId])
  function invalidateCopyFeedback() {
    copyRequestRef.current += 1
  }

  function clearActionFeedback() {
    setActionError(null)
    setFailedRequestId(null)
  }

  function selectRun(runId: string) {
    setNavigationVersion(value => value + 1)
    setHistoryConfirm(null)
    setStopReviewOpen(false)
    setAdjustRunOpen(false)
    setConfirmOwnerStop(false)
    setMissingRunId(null)
    invalidateCopyFeedback()
    clearActionFeedback()
    setFollowupError(null)
    setSelectedRunId(runId)
    onRunSelectionChangeRef.current?.({ runId, userInitiated: true })
  }

  async function createFollowupDraft() {
    if (!projectId || !selectedRun || !['failed', 'needs_attention'].includes(selectedRun.status) || busyAction !== null) return
    const runId = selectedRun.run_id
    const name = (followupDraft?.runId === runId ? followupDraft.name : `followup-${runId}.md`).trim()
    if (!name) {
      setFollowupError('Enter a draft filename before creating the follow-up.')
      return
    }
    const request = ++followupRequestRef.current
    followupBusyRequestRef.current = request
    setFollowupError(null)
    setBusyAction('followup')
    try {
      const created = await api.createProjectPlanFromRun(projectId, runId, name)
      if (request !== followupRequestRef.current || selectedRunRef.current !== runId) return
      if (created.project_id !== projectId || !created.path) {
        setFollowupError('The server returned a draft for a different project. Refresh the run details before trying again.')
        return
      }
      setFeedback(`${created.path} is ready as editable planning work. The source run was not changed and no run was started.`)
      onOpenPlan?.(created.path)
    } catch (reason) {
      if (request !== followupRequestRef.current || selectedRunRef.current !== runId) return
      setFollowupError(followupDraftErrorMessage(reason, name))
    } finally {
      if (followupBusyRequestRef.current === request) {
        followupBusyRequestRef.current = null
        setBusyAction(current => current === 'followup' ? null : current)
      }
    }
  }

  async function copyText(value: string, kind: 'link' | 'run-id'): Promise<void> {
    const copyRequest = ++copyRequestRef.current
    try {
      await navigator.clipboard.writeText(value)
      if (copyRequest !== copyRequestRef.current) return
      setCopyKind(kind)
      setCopyState('copied')
    } catch {
      if (copyRequest !== copyRequestRef.current) return
      setCopyKind(kind)
      setCopyState('failed')
    }
  }

  /** Copy only project/run identities confirmed by the server, never ambient URL data. */
  async function handleCopyLink(): Promise<void> {
    const href = workspaceHref({ project: projectId, view: 'runs', run: selectedRun?.run_id ?? null })
    const url = new URL(window.location.pathname + href, window.location.origin)
    await copyText(url.href, 'link')
  }

  async function handleCopyRunId(): Promise<void> {
    if (!selectedRun) return
    await copyText(selectedRun.run_id, 'run-id')
  }

  function hasSafeControl(control: string): boolean {
    return Boolean(
      capabilities?.controls.includes(control)
      && capabilities.control_safety[control] === 'safe',
    )
  }

  function setTechnicalDisclosure(open: boolean): void {
    if (disclosureRunKey === null) return
    setTechnicalOpenByRun(current => current[disclosureRunKey] === open
      ? current
      : { ...current, [disclosureRunKey]: open })
  }

  function setRunChangesDisclosure(open: boolean): void {
    if (disclosureRunKey === null) return
    setRunChangesOpenByRun(current => current[disclosureRunKey] === open
      ? current
      : { ...current, [disclosureRunKey]: open })
  }

  /**
   * Loads one debugging-context level for the selected run. Opening
   * Diagnostics is the intentional action: full context (with the transport
   * compatibility flag) needs no further acknowledgement. A stale response
   * (run changed, diagnostics closed, dashboard hidden) is discarded.
   */
  async function loadContext(level: 'lite' | 'full', runId: string | null = selectedRunId, { background = false }: { background?: boolean } = {}) {
    if (!projectId || !runId) return
    const request = ++contextRequestRef.current
    contextAbortRef.current.abort()
    contextAbortRef.current = new AbortController()
    if (!background) setContextBusy(true)
    setContextError(null)
    try {
      const result = await api.getRunContext(projectId, runId, level, level === 'full', { signal: contextAbortRef.current.signal })
      if (request !== contextRequestRef.current || selectedRunRef.current !== runId) return
      const previous = contextRef.current
      let nextContext: RunContext = result
      if (previous?.run_id === runId && level === 'lite') {
        // A progress refresh is intentionally allowed to use Lite context,
        // but it must not discard a richer Full diagnostics payload. Preserve
        // the full response and replace only the additive canonical progress
        // projection when the Lite response actually returned one.
        if (previous.level === 'full') {
          nextContext = Object.prototype.hasOwnProperty.call(result.data, 'progress')
            ? { ...previous, data: { ...previous.data, progress: result.data.progress } }
            : previous
        } else if (Object.prototype.hasOwnProperty.call(result.data, 'progress')) {
          nextContext = result
        } else {
          nextContext = previous.data.progress === undefined
          ? result
          : { ...result, data: { ...result.data, progress: previous.data.progress } }
        }
      }
      if (previous && nextContext !== previous && JSON.stringify(nextContext) === JSON.stringify(previous)) nextContext = previous
      if (nextContext !== previous) {
        contextRef.current = nextContext
        setContext(nextContext)
        setContextUpdatedAt(new Date().toISOString())
      }
      if (!background) setContextBusy(false)
    } catch (contextLoadError) {
      if (request !== contextRequestRef.current || selectedRunRef.current !== runId) return
      setContextError(errorMessage(contextLoadError, 'Failed to load run debugging info'))
      if (!background) setContextBusy(false)
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
  function startRequestFromDraft(restartedFromRunId?: string, dirtyConfirmed = dirtyWorktreeConfirmed): StartRunRequest {
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
      dirty_worktree_confirmed: dirtyConfirmed,
    }
  }

  async function handleStart(confirmationToken?: StartConfirmationToken) {
    if (confirmationToken && !isCurrentStartConfirmation(confirmationToken)) return
    if (startupQuestion?.kind === 'confirm_worktree_dirty') {
      if (startDisabled || !dirtyWorktreeConfirmed) return
      await handleStartupAnswer(true)
      return
    }
    const selectedPlan = plans.find((plan) => plan.path === startPlanPath.trim())
    // Launch admission is lifecycle-checked again at submit time: only a
    // saved Ready (in progress) plan path is ever submitted.
    if (startDisabled || !projectId || !selectedPlan || selectedPlan.status !== 'in_progress') return
    const startRequest = startRequestFromDraft()
    const intent = { project_id: projectId, ...startRequest }
    try {
      if (confirmationToken) {
        if (!isCurrentStartConfirmation(confirmationToken)) return
        confirmationToken.submitted = true
        startSubmittedOperationRef.current = confirmationToken
        // The review is no longer cancellable once the mutation is sent. The
        // token remains owned until the response is reconciled so an older
        // operation cannot clear a newer review or pending state.
        setStartReviewIdentity(null)
      }
      setBusyAction('start')
      clearActionFeedback()
      setFeedback(null)
      // A new admission attempt owns its own failure link. A prior reserved
      // request must not remain attached to a later rejection without a run ID.
      const response = await api.startControlPlaneRun(projectId, startRequest, getPendingWriteKey('start', intent))
      let reconciled = false
      if (confirmationToken) {
        if (!ownsSubmittedStart(confirmationToken) || !isCurrentSubmittedStartContext(confirmationToken)) return
        reconciled = await handleStartResponse(response, 'Start request', confirmationToken)
      } else {
        reconciled = await handleStartResponse(response, 'Start request')
      }
      if (reconciled) clearPendingWriteKey('start', intent)
    } catch (startError) {
      if (confirmationToken && (!ownsSubmittedStart(confirmationToken) || !isCurrentSubmittedStartContext(confirmationToken))) {
        if (startError instanceof ApiError && typeof startError.detail.run_id === 'string') {
          clearPendingWriteKey('start', intent)
        }
        return
      }
      setActionError(errorMessage(startError, 'Failed to start run'))
      if (startError instanceof ApiError && typeof startError.detail.run_id === 'string') {
        setFailedRequestId(startError.detail.run_id)
        clearPendingWriteKey('start', intent)
      }
    } finally {
      if (!confirmationToken) {
        setBusyAction(null)
      } else if (startSubmittedOperationRef.current?.operationId === confirmationToken.operationId) {
        startSubmittedOperationRef.current = null
        setBusyAction((current) => current === 'start' ? null : current)
      }
    }
  }

  async function handleStartResponse(response: StartRunResponse, action: string, submittedToken?: StartConfirmationToken): Promise<boolean> {
    const canReconcile = () => submittedToken === undefined || isCurrentSubmittedStartContext(submittedToken)
    if (!canReconcile()) return false
    if (response.startup_question) {
      if (response.startup_question.kind === 'confirm_worktree_dirty') {
        setDirtyWorktreeConfirmed(false)
        await refreshPreflight()
        if (!canReconcile()) return false
      }
      if (!canReconcile()) return false
      setStartupQuestion(response.startup_question)
      setFeedback(`${action} is awaiting a startup answer. No workflow has been started.`)
      return true
    }
    if (!response.result) return true
    const result = response.result
    if (result.status === 'needs_attention') {
      if (!canReconcile()) return false
      setActionError(result.reason ?? 'Startup did not complete; the original error was not recorded.')
      setFailedRequestId(result.run_id)
      await loadDashboard(projectId)
      return canReconcile()
    }
    if (!canReconcile()) return false
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
    if (!canReconcile()) return false
    setSelectedRunId(result.run_id)
    setLocalPage('runs')
    onRunStarted?.(result.run_id)
    // Passive URL update: the dashboard link now identifies the returned run.
    onRunSelectionChangeRef.current?.({ runId: result.run_id, userInitiated: false })
    return true
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
      clearActionFeedback()
      const response = await api.answerStartupQuestion(
        projectId,
        startupQuestion.question_id,
        answer,
        getPendingWriteKey('startup-answer', intent),
      )
      clearPendingWriteKey('startup-answer', intent)
      await handleStartResponse(response, 'Startup answer')
    } catch (answerError) {
      setActionError(errorMessage(answerError, 'Failed to answer startup question'))
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
    clearActionFeedback()
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
        setActionError(errorMessage(controlError, 'Failed to apply controls'))
      }
    } finally {
      setBusyAction(null)
    }
  }

  async function handleOwnerStop() {
    if (!projectId || !selectedRun || !canMutate || !selectedRunHasLiveControls || !hasSafeControl('owner_stop')) return
    clearActionFeedback()
    const runId = selectedRun.run_id
    const intent = {
      project_id: projectId,
      run_id: runId,
      expected_revision: selectedRun.revision,
    }
    try {
      setBusyAction('owner-stop')
      const stopped = await api.ownerStopControlPlaneRun(
        projectId,
        runId,
        selectedRun.revision,
        getPendingWriteKey('owner-stop', intent),
      )
      clearPendingWriteKey('owner-stop', intent)
      if (selectedRunRef.current !== runId) return
      setRuns((current) => upsertRun(current, stopped))
      setFeedback(`Stop now recorded for ${stopped.run_id}.`)
      setConfirmOwnerStop(false)
    } catch (stopError) {
      if (apiErrorCode(stopError) === 'revision_conflict') {
        await refreshSelectedRun()
        if (selectedRunRef.current === runId) {
          setFeedback('Another operator changed this run. Stop now was not retried; review the refreshed revision and try again.')
        }
      } else {
        setActionError(errorMessage(stopError, 'Failed to stop run'))
      }
    } finally {
      setBusyAction(null)
    }
  }

  async function handleBoundaryStop() {
    if (!projectId || !selectedRun || !canMutate || !selectedRunHasLiveControls || !hasSafeControl('owner_stop')) return
    clearActionFeedback()
    const runId = selectedRun.run_id
    const request: Parameters<typeof api.controlControlPlaneRun>[2] = {
      expected_revision: selectedRun.revision,
      owner_stop: true,
    }
    const intent = {
      project_id: projectId,
      run_id: runId,
      expected_revision: request.expected_revision,
      owner_stop: true,
    }
    try {
      setBusyAction('boundary-stop')
      const response = await api.controlControlPlaneRun(
        projectId,
        runId,
        request,
        getPendingWriteKey('boundary-stop', intent),
      )
      clearPendingWriteKey('boundary-stop', intent)
      if (selectedRunRef.current !== runId) return
      setRuns((current) => upsertRun(current, response.run))
      setFeedback(response.changed
        ? `Stop after current turn requested for ${runId}. The current worker/reviewer call may finish at the existing boundary; no checkpoint approval is implied.`
        : `Stop after current turn was already requested for ${runId}.`)
    } catch (stopError) {
      if (apiErrorCode(stopError) === 'revision_conflict') {
        await refreshSelectedRun()
        if (selectedRunRef.current === runId) {
          setFeedback('Another operator changed this run. The stop request was not retried; review the refreshed revision and try again.')
        }
      } else {
        setActionError(errorMessage(stopError, 'Failed to request stop after the current turn'))
      }
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
    clearActionFeedback()
    const { projectId: successorProjectId, request, idempotencyKey } = pendingSuccessorStart
    try {
      setRestartPhase('starting')
      setRestartNotice('Retrying the exact successor request with its original idempotency key. The source will not be stopped again.')
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
        if (restartError instanceof ApiError && typeof restartError.detail.run_id === 'string') { setFailedRequestId(restartError.detail.run_id); setActionError(restartError.message) }
      } else {
        setRestartPhase('unknown')
        setRestartNotice(`Successor outcome remains unknown: ${errorMessage(restartError, 'response was lost')}. Retry only the frozen successor request after reconciliation; the source will not be stopped again.`)
      }
    }
  }

  async function handleConfirmedRestart() {
    if (!restartDraftReady || !projectId || !restartSource || pendingSuccessorStart) return
    clearActionFeedback()
    const sourceRunId = restartSource.run_id
    const startRequest = startRequestFromDraft(sourceRunId, dirtyWorktreeConfirmed)
    const stopIntent = {
      project_id: projectId,
      run_id: sourceRunId,
      expected_revision: restartSource.revision,
      restart: true,
    }
    try {
      setRestartPhase('stopping')
      setRestartNotice(null)
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
          if (restartError instanceof ApiError && typeof restartError.detail.run_id === 'string') { setFailedRequestId(restartError.detail.run_id); setActionError(restartError.message) }
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
    clearActionFeedback()
    const sourceRun = selectedRun.run_id
    const intent = { project_id: projectId, source_run_id: sourceRun }
    try {
      setBusyAction('resume')
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
      setActionError(errorMessage(resumeError, 'Failed to resume run'))
    } finally {
      setBusyAction(null)
    }
  }

  async function handleRecovery() {
    const workerSelector = recoverySelector.trim()
    if (!projectId || !selectedRun || !canRecover || !workerSelector) return
    const sourceRun = selectedRun.run_id
    const recovery: RecoveryRequest = {
      mode: 'durable_evidence',
      worker_selector: workerSelector,
    }
    const request = { recovery }
    const intent = { project_id: projectId, source_run_id: sourceRun, recovery }
    try {
      setBusyAction('recovery')
      clearActionFeedback()
      const continuation = await api.resumeControlPlaneRun(
        projectId,
        sourceRun,
        getPendingWriteKey('recovery', intent),
        request,
      )
      clearPendingWriteKey('recovery', intent)
      setFeedback(continuation.created
        ? `Recovery successor ${continuation.run_id} was created from source run ${sourceRun}. Its replacement start is shown in the successor details; the source remains separate.`
        : `Recovery replay returned successor ${continuation.run_id} from source run ${sourceRun}; no duplicate was created. Its replacement start is shown in the successor details.`)
      setRecoveryOpen(false)
      setRecoverySelector('')
      setMissingRunId(null)
      await loadDashboard(projectId)
      setSelectedRunId(continuation.run_id)
      onRunSelectionChangeRef.current?.({ runId: continuation.run_id, userInitiated: false })
    } catch (recoveryError) {
      // Keep the selected source and recovery draft intact. In particular, an
      // admission rejection must never fall back to ordinary Resume.
      setActionError(`Recovery was rejected: ${errorMessage(recoveryError, 'the source was not admitted for replacement')}`)
    } finally {
      setBusyAction(null)
    }
  }

  // Direct details stay readable while dashboard/configuration work is pending,
  // but controls that rely on that unresolved admission context do not.
  const canMutate = selectedRun?.ownership === 'control_plane' && !loading
  const canResume = canMutate && selectedRun?.evidence.can_resume === true
  const canRestart = canMutate && (restartAdmission?.eligible === true || (selectedRunHasLiveControls && hasSafeControl('owner_stop')))
  const recoveryWorkerOptions = [...new Set(capabilities?.admitted_role_selectors?.worker ?? [])].sort()
  const canRecover = Boolean(
    canMutate
    && restartAdmission?.eligible === true
    && selectedRun
    && DURABLE_RECOVERY_STATUSES.has(selectedRun.status)
    && selectedRun.activity !== 'active'
    && selectedRun.activity !== 'unknown'
    && recoveryWorkerOptions.length > 0,
  )
  const startMaxTurnsProblem = maxTurnsProblem(startMaxTurns)
  const restartDraftWorkflow = startWorkflow.trim()
  const restartInProgress = restartPhase === 'stopping' || restartPhase === 'waiting' || restartPhase === 'starting'
  const successorOutcomeUnknown = pendingSuccessorStart !== null && restartPhase !== 'starting'
  const restartDraftFrozen = pendingSuccessorStart !== null
  const restartPendingConfirmation = restartPhase === 'confirming'

  // Effective launch values, resolved only from the committed projection plus
  // canonical capabilities.  An unresolved value is never labeled a default.
  const committedForm = committed?.form ?? null
  const configurationDisplay = committedError
    ? 'Configuration unavailable'
    : 'Loading configuration…'
  const effectiveWorkflow = startWorkflow.trim() || (committedForm?.default_workflow ?? '').trim()
  const effectiveWorkflowSource = startWorkflow.trim()
    ? 'your selection'
    : committedForm?.default_workflow
      ? 'global default'
      : 'no default workflow configured'
  const configuredMaxTurns = committedForm?.max_turns ?? null
  const serverDefaultMaxTurns = committed?.serverDefaultMaxTurns ?? null
  const effectiveMaxTurns = startMaxTurns.trim() !== ''
    ? Number(startMaxTurns.trim())
    : configuredMaxTurns ?? serverDefaultMaxTurns
  const effectiveMaxTurnsSource = startMaxTurns.trim() !== ''
    ? 'your override'
    : configuredMaxTurns !== null
      ? 'global default'
      : serverDefaultMaxTurns !== null
        ? 'server default'
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
      ? `workflow default (${formatMachineLabel(workflowDefaultTeam)})`
      : 'no team — global role assignments apply'
  function teamRolesForPreview(teamId: string): Record<string, string> {
    const summary = committedForm?.teams?.[teamId]
    return summary?.effective_roles ?? summary?.roles ?? {}
  }
  function teamRoleSourcesForPreview(teamId: string): Record<string, string> | undefined {
    return committedForm?.teams?.[teamId]?.role_sources
  }
  const effectiveTeamRoles = effectiveTeam ? teamRolesForPreview(effectiveTeam) : {}
  const effectiveTeamRoleSources = effectiveTeam ? teamRoleSourcesForPreview(effectiveTeam) : undefined
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
  const launchTeamGroups = launchTeamFamilyGroups(committedForm, teamOptions)
  const teamFamilyOptions = launchTeamGroups.map((group) => group.rootId)
  const teamFamilyLabelCounts = new Map<string, number>()
  for (const group of launchTeamGroups) {
    const label = launchTeamFamilyLabel(group)
    teamFamilyLabelCounts.set(label, (teamFamilyLabelCounts.get(label) ?? 0) + 1)
  }
  const teamFamilyGroupFor = (teamId: string): typeof launchTeamGroups[number] | null => (
    launchTeamGroups.find((group) => group.memberIds.includes(teamId)) ?? null
  )
  const effectiveTeamGroup = effectiveTeam ? teamFamilyGroupFor(effectiveTeam) : null
  const explicitTeamGroup = startTeam.trim() ? teamFamilyGroupFor(startTeam.trim()) : null
  const activeTeamGroup = explicitTeamGroup ?? effectiveTeamGroup
  const teamStageOptions = activeTeamGroup && activeTeamGroup.memberIds.length > 1
    ? launchTeamFamilyRoute(activeTeamGroup)
    : []
  const teamFamilyOptionLabel = (rootId: string): string => {
    const group = launchTeamGroups.find((entry) => entry.rootId === rootId)
    if (!group) return formatMachineChoice(rootId, teamFamilyOptions)
    const label = launchTeamFamilyLabel(group)
    return (teamFamilyLabelCounts.get(label) ?? 0) > 1 ? `${label} (${rootId})` : label
  }
  const teamFamilyOptionHint = (rootId: string): string | null => {
    const group = launchTeamGroups.find((entry) => entry.rootId === rootId)
    return group ? launchTeamFamilyHint(committedForm, group) : null
  }
  const teamStageLabelCounts = new Map<string, number>()
  for (const teamId of teamStageOptions) {
    const label = activeTeamGroup ? launchTeamStageLabel(committedForm, activeTeamGroup, teamId) : formatMachineLabel(teamId)
    teamStageLabelCounts.set(label, (teamStageLabelCounts.get(label) ?? 0) + 1)
  }
  const teamStageOptionLabel = (teamId: string): string => {
    const label = activeTeamGroup ? launchTeamStageLabel(committedForm, activeTeamGroup, teamId) : formatMachineLabel(teamId)
    return (teamStageLabelCounts.get(label) ?? 0) > 1 ? `${label} (${teamId})` : label
  }
  const teamStageBadges: Record<string, string> = Object.fromEntries(teamStageOptions.map((teamId) => [
    teamId,
    committedForm?.teams[teamId] ? 'configured' : 'available on this server',
  ]))
  const startTeamFamily = startTeam.trim() && explicitTeamGroup ? explicitTeamGroup.rootId : ''
  const startTeamStage = startTeam.trim() && activeTeamGroup?.memberIds.includes(startTeam.trim()) ? startTeam.trim() : ''
  const workflowDefaultTeamGroup = workflowDefaultTeam ? teamFamilyGroupFor(workflowDefaultTeam) : null
  const workflowDefaultFamilyLabel = workflowDefaultTeamGroup
    ? teamFamilyOptionLabel(workflowDefaultTeamGroup.rootId)
    : workflowDefaultTeam
      ? formatMachineChoice(workflowDefaultTeam, teamOptions)
      : null
  const workflowDefaultStageLabel = workflowDefaultTeamGroup && workflowDefaultTeam
    ? launchTeamStageLabel(committedForm, workflowDefaultTeamGroup, workflowDefaultTeam)
    : workflowDefaultTeam
      ? formatMachineChoice(workflowDefaultTeam, teamOptions)
      : null
  // Visible resolved values: before focus each selector shows the effective
  // name with a Default indicator; the open list offers an explicit default row.
  const workflowResolvedDisplay = startWorkflow.trim()
    ? null
    : committed === null ? configurationDisplay : committedForm?.default_workflow ? formatMachineChoice(committedForm.default_workflow, workflowOptions) : 'No default workflow configured'
  const workflowResolvedBadge = !startWorkflow.trim() && committedForm?.default_workflow ? 'Default' : undefined
  const workflowDefaultOption = {
    value: '',
    label: committed === null
      ? configurationDisplay
      : committedForm?.default_workflow ? `Use default (${formatMachineChoice(committedForm.default_workflow, workflowOptions)})` : 'Use default',
    hint: committed === null
      ? 'wait for committed configuration to resolve the default'
      : committedForm?.default_workflow ? 'the global default workflow applies' : 'no global default workflow is configured',
  }
  const resolvedTeamFamilyLabel = effectiveTeamGroup
    ? teamFamilyOptionLabel(effectiveTeamGroup.rootId)
    : effectiveTeam
      ? formatMachineChoice(effectiveTeam, teamOptions)
      : null
  const resolvedTeamStageLabel = effectiveTeamGroup
    ? launchTeamStageLabel(committedForm, effectiveTeamGroup, effectiveTeam)
    : effectiveTeam
      ? formatMachineChoice(effectiveTeam, teamOptions)
      : null
  const teamFamilyResolvedDisplay = startTeam.trim()
    ? null
    : committed === null
      ? configurationDisplay
      : (resolvedTeamFamilyLabel ?? 'No team — global roles')
  const teamFamilyResolvedBadge = !startTeam.trim() && workflowDefaultTeam ? 'Default' : undefined
  const teamFamilyDefaultOption = {
    value: '',
    label: committed === null
      ? configurationDisplay
      : workflowDefaultFamilyLabel ? `Use default (${workflowDefaultFamilyLabel})` : 'Use default (no team)',
    hint: committed === null
      ? 'wait for committed configuration to resolve the default'
      : workflowDefaultTeam ? 'the workflow default team applies' : 'global role assignments apply',
  }
  const teamStageResolvedDisplay = startTeam.trim()
    ? null
    : committed === null
      ? configurationDisplay
      : (resolvedTeamStageLabel ?? 'No baseline stage — global roles')
  const teamStageResolvedBadge = !startTeam.trim() && workflowDefaultTeam ? 'Default' : undefined
  const teamStageDefaultOption = {
    value: '',
    label: committed === null
      ? configurationDisplay
      : workflowDefaultStageLabel ? `Use default (${workflowDefaultStageLabel})` : 'Use default (no stage)',
    hint: committed === null
      ? 'wait for committed configuration to resolve the default'
      : workflowDefaultTeam ? 'the exact workflow default team applies' : 'global role assignments apply',
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
    if (unmappedSteps.length > 0) return `The exact role preview is unavailable for ${formatMachineLabel(effectiveWorkflow)} step${unmappedSteps.length > 1 ? 's' : ''} ${unmappedSteps.map(formatMachineLabel).join(', ')} — the committed configuration does not map those executable steps to roles. Refresh, or check the workflow in Settings, before starting a run.`
    return null
  })()
  const dirtyStartupQuestion = startupQuestion?.kind === 'confirm_worktree_dirty'
  const preflightEligible = projectAvailable === true
    && newRunPage
    && !restartInProgress
    && launchBlocker === null
    && startMaxTurnsProblem === null
    && extraInstructionProblem === null
  const preflightRequest = preflightEligible
    ? startRequestFromDraft(restartSource?.run_id, false)
    : null
  const preflightSelectionIdentity = JSON.stringify([
    projectId,
    startPlanPath.trim(),
    startWorkflow.trim(),
    restartSource?.run_id ?? null,
  ])
  // The request body intentionally omits values that follow committed
  // defaults. Keep those resolved values (and the committed revision) in the
  // UI identity as well, so a late configuration response cannot leave the
  // previous ready inspection attached to the current launch draft.
  const preflightRequestIdentity = JSON.stringify([
    projectId,
    preflightRequest,
    committed?.revision ?? null,
    effectiveWorkflow,
    effectiveTeam,
    effectiveMaxTurns,
  ])
  // React renders once before the request effect runs. Treat an old state
  // identity as pending during that render so a previous inspection cannot
  // authorize the newly selected launch.
  const displayedWorktreePreflight: WorktreePreflightState = !preflightEligible
    ? { status: 'idle', result: null, error: null, identity: preflightRequestIdentity }
    : worktreePreflight.identity === preflightRequestIdentity
      ? worktreePreflight
      : { status: 'loading', result: null, error: null, identity: preflightRequestIdentity }
  const worktreeLaunchBlocked = preflightEligible && (
    displayedWorktreePreflight.status !== 'ready'
    || displayedWorktreePreflight.result === null
    || displayedWorktreePreflight.error !== null
    || displayedWorktreePreflight.result.blockers.length > 0
    || ((displayedWorktreePreflight.result.requires_confirmation || dirtyStartupQuestion) && !dirtyWorktreeConfirmed)
  )
  const startDisabled = !projectAvailable
    || !startPlanPath
    || busyAction === 'start'
    || restartInProgress
    || restartDraftFrozen
    || Boolean(extraInstructionProblem)
    || startMaxTurnsProblem !== null
    || launchBlocker !== null
    || worktreeLaunchBlocked
    || (startupQuestion !== null && !dirtyStartupQuestion)
  const selectedStartPlan = runnablePlans.find((plan) => plan.path === startPlanPath.trim()) ?? null
  const currentLaunchIdentity = JSON.stringify([
    projectId,
    selectedStartPlan ? [selectedStartPlan.path, selectedStartPlan.status, selectedStartPlan.modified_at, selectedStartPlan.schema_version] : null,
    committed ? [committed.revision, committed.serverDefaultMaxTurns] : null,
    [effectiveWorkflow, effectiveTeam, effectiveMaxTurns],
    startRequestFromDraft(restartSource?.run_id, dirtyWorktreeConfirmed),
    preflightRequestIdentity,
  ])
  const startReviewReady = startReviewIdentity !== null && startReviewIdentity === currentLaunchIdentity
  const currentLaunchIdentityRef = useRef<string | null>(null)
  currentLaunchIdentityRef.current = currentLaunchIdentity
  startReviewIdentityRef.current = startReviewIdentity
  startReviewProjectRef.current = projectId
  startReviewVisibleRef.current = visible
  startReviewPageRef.current = newRunPage
  const startReviewContext = JSON.stringify([projectId, visible, newRunPage, currentLaunchIdentity])
  if (startReviewContextRef.current === null) {
    startReviewContextRef.current = startReviewContext
  } else if (startReviewContextRef.current !== startReviewContext) {
    startReviewContextRef.current = startReviewContext
    startReviewGenerationRef.current += 1
    startConfirmationRef.current = null
  }
  const startRevalidationPendingForRender = startRevalidationPending && startConfirmationRef.current !== null

  function isCurrentStartConfirmation(token: StartConfirmationToken): boolean {
    return startConfirmationRef.current?.operationId === token.operationId
      && startReviewGenerationRef.current === token.generation
      && startReviewProjectRef.current === token.projectId
      && startReviewVisibleRef.current
      && startReviewPageRef.current
      && currentLaunchIdentityRef.current === token.launchIdentity
      && !token.submitted
      && startReviewIdentityRef.current === token.reviewIdentity
  }

  function ownsSubmittedStart(token: StartConfirmationToken): boolean {
    return startSubmittedOperationRef.current?.operationId === token.operationId
      && token.submitted
  }

  function isCurrentSubmittedStartContext(token: StartConfirmationToken): boolean {
    return ownsSubmittedStart(token)
      && startReviewGenerationRef.current === token.generation
      && startReviewProjectRef.current === token.projectId
      && startReviewVisibleRef.current
      && startReviewPageRef.current
      && currentLaunchIdentityRef.current === token.launchIdentity
  }

  function invalidateStartReview() {
    startReviewGenerationRef.current += 1
    startConfirmationRef.current = null
    startReviewIdentityRef.current = null
    setStartReviewIdentity(null)
    setStartRevalidationPending(false)
  }

  function openStartReview(trigger?: HTMLElement) {
    // A dirty-worktree startup question is an answer to an already allocated
    // request, not a new launch. Keep that explicit continuation path intact.
    if (startupQuestion?.kind === 'confirm_worktree_dirty') {
      void handleStart()
      return
    }
    if (startDisabled || (restartSource !== null && restartPhase !== null)) return
    startReviewGenerationRef.current += 1
    startConfirmationRef.current = null
    startReviewTriggerRef.current = trigger ?? null
    startReviewIdentityRef.current = currentLaunchIdentity
    setStartRevalidationPending(false)
    setStartReviewIdentity(currentLaunchIdentity)
  }

  function cancelStartReview() {
    if (busyAction === 'start') return
    const trigger = startReviewTriggerRef.current
    invalidateStartReview()
    const restoreFocus = () => {
      if (trigger?.isConnected) {
        trigger.focus()
        return
      }
      document.querySelector<HTMLElement>('[data-launch-review-trigger="true"]')?.focus()
    }
    if (trigger?.isConnected) restoreFocus()
    else window.setTimeout(restoreFocus, 0)
  }

  async function confirmStart() {
    const reviewIdentity = startReviewIdentityRef.current
    if (!reviewIdentity || startConfirmationRef.current !== null) return
    if (!startReviewReady) {
      invalidateStartReview()
      setActionError('Launch choices changed while the review was open. Review the current choices before starting.')
      return
    }
    if (!committed) {
      invalidateStartReview()
      setActionError('The committed launch configuration is unavailable. Review the current choices before starting.')
      return
    }
    const token: StartConfirmationToken = {
      generation: startReviewGenerationRef.current,
      operationId: ++startConfirmationSequenceRef.current,
      reviewIdentity,
      launchIdentity: currentLaunchIdentity,
      projectId,
      submitted: false,
    }
    startConfirmationRef.current = token
    setStartRevalidationPending(true)
    try {
      const latestPair = await api.getGlobalConfig()
      if (!isCurrentStartConfirmation(token)) return
      if (latestPair.revision !== committed.revision) {
        const latestProjection = await api.postGlobalConfigForm({
          aflow_toml: latestPair.aflow_toml,
          workflows_toml: latestPair.workflows_toml,
        })
        if (!isCurrentStartConfirmation(token)) return
        setCommitted(committedProjectionFrom(latestPair, latestProjection))
        setCommittedError(null)
        invalidateStartReview()
        setActionError('Committed launch defaults changed while the review was open. Review the current choices before starting.')
        return
      }
    } catch (revalidationError) {
      if (!isCurrentStartConfirmation(token)) return
      invalidateStartReview()
      setActionError(`Could not revalidate the committed launch configuration: ${errorMessage(revalidationError, 'configuration read failed')}. Review again before starting.`)
      return
    }
    if (!isCurrentStartConfirmation(token)) return
    if (startReviewIdentityRef.current !== token.reviewIdentity || currentLaunchIdentityRef.current !== token.launchIdentity) {
      invalidateStartReview()
      setActionError('Launch choices changed while the review was open. Review the current choices before starting.')
      return
    }
    await handleStart(token)
    // Only the operation that still owns the lock may release the pending UI.
    if (startConfirmationRef.current?.operationId === token.operationId) {
      startConfirmationRef.current = null
      setStartRevalidationPending(false)
    }
  }

  const restartDraftChoicesReady = Boolean(restartDraftWorkflow && startPlanPath && planOptions.includes(startPlanPath))
    && (!missingRestartInstructions || Boolean(startExtraInstructions.trim()))
    && launchBlocker === null && startMaxTurnsProblem === null && !extraInstructionProblem
  const restartDraftReady = restartDraftChoicesReady && !worktreeLaunchBlocked
  const restartDraftHint = canRestart && !restartDraftReady
    ? restartDraftChoicesReady
      ? 'Working-tree inspection must finish before the successor can start.'
      : 'Choose an available workflow and a Ready plan. Original choices that are no longer available must be corrected.'
    : null
  const runSteps = effectiveSteps
  const startStepIndex = runSteps.indexOf(startStep.trim())
  const skippedByDraft = startStepIndex > 0 ? runSteps.slice(0, startStepIndex) : []

  async function requestWorktreePreflight(
    request: StartRunRequest,
    identity: string,
    offset: number,
    append: boolean,
    preserveReady = false,
  ): Promise<void> {
    const requestNumber = ++preflightRequestRef.current
    preflightAbortRef.current.abort()
    const controller = new AbortController()
    preflightAbortRef.current = controller
    setWorktreePreflight((current) => preserveReady
      && current.identity === identity
      && current.status === 'ready'
      && current.result !== null
      ? current
      : {
          status: 'loading',
          identity,
          result: append && current.identity === identity ? current.result : null,
          error: null,
        })
    try {
      const result = await api.preflightControlPlaneRun(projectId, request, { offset, limit: 200, signal: controller.signal })
      if (requestNumber !== preflightRequestRef.current) return
      setWorktreePreflight((current) => {
        if (current.identity !== identity) return current
        const items = append && current.result
          ? [...current.result.items, ...result.items]
          : result.items
        return { status: 'ready', identity, result: { ...result, items }, error: null }
      })
    } catch (preflightError) {
      if (requestNumber !== preflightRequestRef.current) return
      if (preflightError instanceof DOMException && preflightError.name === 'AbortError') return
      setWorktreePreflight((current) => current.identity === identity
        ? { ...current, status: 'error', error: errorMessage(preflightError, 'Could not inspect the working tree') }
        : current)
    }
  }

  async function refreshPreflight(preserveReady = false): Promise<void> {
    if (!preflightEligible || !preflightRequest) return
    await requestWorktreePreflight(preflightRequest, preflightRequestIdentity, 0, false, preserveReady)
  }

  async function loadMorePreflight(): Promise<void> {
    const nextOffset = worktreePreflight.result?.next_offset
    if (!preflightEligible || !preflightRequest || nextOffset === null || nextOffset === undefined || worktreePreflight.status === 'loading') return
    await requestWorktreePreflight(preflightRequest, preflightRequestIdentity, nextOffset, true)
  }

  useEffect(() => {
    setDirtyWorktreeConfirmed(false)
    if (startupQuestion?.kind === 'confirm_worktree_dirty') setStartupQuestion(null)
    // The acknowledgement follows only the launch identity, not unrelated
    // draft controls such as max turns, team, or extra instructions.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [preflightSelectionIdentity])

  useEffect(() => {
    preflightRequestRef.current += 1
    preflightAbortRef.current.abort()
    if (!visible || !preflightEligible || !preflightRequest) {
      setWorktreePreflight({ status: 'idle', result: null, error: null, identity: preflightRequestIdentity })
      return
    }
    void requestWorktreePreflight(preflightRequest, preflightRequestIdentity, 0, false)
    // The serialized request is the complete preflight identity; changing a
    // draft field cannot let a late response update the next launch.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [visible, preflightEligible, preflightRequestIdentity])

  const readinessLabel = readiness === null
    ? 'Readiness unavailable'
    : readiness.ready ? 'Daemon ready' : 'Daemon not ready'
  const streamLabel = streamState === 'connected'
    ? 'stream connected'
    : streamState === 'reconnecting'
      ? 'stream reconnecting — last snapshot shown'
      : 'stream stopped — refresh for updates'
  const startTime = selectedRun?.started_at ?? null
  const canonicalDetail = canonicalProgressFromContext(context, selectedRun?.run_id ?? null)
  const canonicalProgress = selectedRun?.progress ?? canonicalDetail
  // The canonical projection owns history and approval facts when present.
  // The bounded execution summary still supplies older current/finished
  // prose and recovery completion without becoming a second authority.
  const compatibilitySummary = checkpointSummary(context)
  const checkpoints = canonicalProgress ? null : compatibilitySummary
  const compatibilityProgressText = canonicalProgress
    && compatibilitySummary?.authoritative
    && compatibilitySummary.availability !== 'unavailable'
    ? checkpointProgressText(compatibilitySummary)
    : null
  const outcome = managerOutcome(context, compatibilitySummary)
  const selectedPlanPath = selectedRun
    ? selectedRun.plan_path ?? (textEvidence(selectedRun, 'plan_path') !== 'Not reported'
      ? textEvidence(selectedRun, 'plan_path')
      : planPathFromContext(context))
    : 'Not reported'
  // The header names the plan; the full path stays under Technical details.
  // Canonical custom names remain unchanged, while path-derived names use the
  // same readable presentation as the history row.
  const selectedPlanPresentation = selectedRun
    ? selectedRun.progress?.original_plan_display_name?.trim()
      ? runPlanPresentationForRun(selectedRun)
      : selectedPlanPath === 'Not reported'
        ? null
        : runPlanPresentation(selectedPlanPath, selectedRun.run_id)
    : null
  const selectedPlanFileName = selectedPlanPresentation?.label ?? 'Not reported'
  const roleChoices = capabilities?.roles ?? []
  const savedOverrides = selectedRun?.evidence.overrides as { state?: string; revision?: number; max_turns?: number; team?: string; role_selectors?: Record<string, string>; owner_stop?: boolean } | null
  const pendingBoundaryStop = Boolean(
    selectedRunHasLiveControls
    && savedOverrides?.state === 'pending'
    && savedOverrides.owner_stop === true,
  )
  const lastExecuted = lastExecutedEvidence(events, context)
  const selectedRunIssue = selectedRun ? runIssue(selectedRun) : null
  const canCreateFollowup = Boolean(
    selectedRun && ['failed', 'needs_attention'].includes(selectedRun.status),
  )
  const followupName = selectedRun
    ? followupDraft?.runId === selectedRun.run_id
      ? followupDraft.name
      : `followup-${selectedRun.run_id}.md`
    : ''
  const recoveryProvenance = recoveryProvenanceFromEvents(events)
  const recoveryWorkerEvidence = recoveryWorkerEvidenceFromRun(selectedRun)
  const recoveryWorkerEvidenceMatches = Boolean(
    recoveryProvenance
    && selectedRun?.run_id === recoveryProvenance.targetRunId
    && recoveryWorkerEvidence?.target_run_id === recoveryProvenance.targetRunId
    && recoveryWorkerEvidence.target_selector === recoveryProvenance.targetSelector
  )
  const recoveryWorkerSessionStarted = Boolean(
    recoveryWorkerEvidenceMatches && recoveryWorkerEvidence?.session_started,
  )
  const recoveryReplacementStarted = Boolean(
    recoveryWorkerEvidenceMatches && recoveryWorkerEvidence?.operation_started,
  )
  const latestResultEvent = latestRunResultEvent(events)
  const latestResultPresentation = latestResultEvent ? presentRunEvent(latestResultEvent) : null
  const observedInvocation = selectedRun?.current_step
    ? [
        formatMachineLabel(selectedRun.current_step),
        selectedRun.turns_completed !== null ? `turn ${selectedRun.turns_completed}` : null,
      ].filter((value): value is string => Boolean(value)).join(' · ')
    : lastExecuted
      ? [
          lastExecuted.stepName ? formatMachineLabel(lastExecuted.stepName) : null,
          lastExecuted.role ? formatMachineLabel(lastExecuted.role) : null,
          lastExecuted.turnNumber !== null ? `turn ${lastExecuted.turnNumber}` : null,
        ].filter((value): value is string => Boolean(value)).join(' · ') || null
      : null
  const deliveryWarning = canonicalDetail ? deliveryIssueText(canonicalDetail.delivery) : null
  const failedReview = selectedRun?.status === 'failed'
    && /review/i.test(selectedRun.current_step ?? lastExecuted?.stepName ?? '')
  const recordedOrdinal = canonicalProgress?.current_checkpoint_ordinal ?? compatibilitySummary?.index
  const recordedTitle = canonicalProgress?.current_checkpoint_title ?? compatibilitySummary?.name
  const recordedPosition = recordedOrdinal
    ? `CP${recordedOrdinal}${recordedTitle ? ` — ${recordedTitle.replace(/^Checkpoint\s+\d+\s*:\s*/i, '')}` : ''}`
    : null
  const lastReviewStep = [recordedPosition, observedInvocation].filter((value): value is string => Boolean(value)).join(' · ')
  const reviewerHarnessFailed = failedReview && events.some(event => event.event_type === 'turn_finished'
    && event.data?.outcome === 'harness-failed'
    && /review/i.test(String(event.data?.step_name ?? '')))
  const readableFailure = [selectedRun?.reason, latestResultPresentation?.summary, outcome?.finishedSummary, outcome?.resultText]
    .map(value => conciseRunText(value))
    .find(value => value && !/^exit\s+\d+\.?$/i.test(value) && !/^#|\s#+\s/.test(value) && !/reason unavailable|result unavailable/i.test(value) && !/^harness[-_ ]failed\.?$/i.test(value)) ?? null
  const overviewCurrentWork = selectedRun
    ? runDisplayProjection(selectedRun).category === 'outcome-unrecorded'
      ? null
      : (() => {
      const currentWork = failedReview
        ? `Review stopped${lastReviewStep ? ` · ${lastReviewStep}` : ''}.`
        : isTerminalInactiveRun(selectedRun)
        ? `No current work — ${statusLabel(selectedRun)}.`
        : canonicalProgress
          ? runCurrentWorkText(canonicalProgress, selectedRun.current_step)
          : selectedRun.current_step
            ? [
                formatMachineLabel(selectedRun.current_step),
                selectedRun.turns_completed !== null ? `turn ${selectedRun.turns_completed}` : null,
              ].filter((value): value is string => Boolean(value)).join(' · ')
          : checkpoints
            ? checkpointProgressText(checkpoints)
            : selectedRun.activity === 'active'
                ? 'Active work is in progress.'
                : `${statusLabel(selectedRun)} — current work is not reported.`
      const executorFacts = failedReview
        ? [lastExecuted?.selector, lastExecuted?.model].filter((value): value is string => Boolean(value))
        : [
        selectedRun.current_step && !isTerminalInactiveRun(selectedRun) ? formatMachineLabel(selectedRun.current_step) : null,
        ...(canonicalProgress?.current_executor
          ? runExecutorFacts(canonicalProgress.current_executor)
          : lastExecuted
            ? [
                lastExecuted.role ? formatMachineLabel(lastExecuted.role) : null,
                lastExecuted.selector,
                lastExecuted.model,
                lastExecuted.turnNumber !== null ? `turn ${lastExecuted.turnNumber}` : null,
              ]
            : []),
      ].filter((value): value is string => Boolean(value))
      return (
        <>
          <p className="run-overview-lead">{currentWork}</p>
          {executorFacts.length > 0 && <p className="run-overview-inline-facts"><span>{executorFacts.join(' · ')}</span></p>}
        </>
      )
    })()
    : null
  const overviewLatestResult = selectedRun
    ? (() => {
      if (failedReview) return <>
        <p>{readableFailure ?? (reviewerHarnessFailed
          ? 'The reviewer harness failed before a decision.'
          : 'Review stopped before a decision. The specific cause was not recorded in a short result.')}</p>
        <p className="text-sm text-dim"><a href={`#${technicalId}`} onClick={() => setTechnicalDisclosure(true)}>Open Diagnostics</a> for technical detail and full activity.</p>
        {(outcome?.resultText || latestResultPresentation?.hasPayload) && <details className="run-report" open={reportOpen}>
          <summary onClick={(event) => { event.preventDefault(); setReportOpen((open) => !open) }}>Read full update</summary>
          {reportOpen && <pre className="dashboard-payload">{outcome?.resultText ?? latestResultPresentation?.detail}</pre>}
        </details>}
      </>
      const hasRecordedResult = Boolean(
        outcome?.finishedTurn
        || outcome?.finishedSummary
        || outcome?.resultText
        || latestResultPresentation?.summary
        || latestResultPresentation?.hasPayload,
      )
      const latestEventStatus = typeof latestResultEvent?.data?.status === 'string'
        ? latestResultEvent.data.status
        : ''
      const latestEventIsProblem = Boolean(
        latestResultEvent
        && /(?:fail|error|reject|stop|interrupt|cancel)/i.test(`${latestResultEvent.event_type} ${latestEventStatus}`),
      )
      const latestSummary = latestEventIsProblem
        ? latestResultPresentation?.summary
        : conciseRunText(outcome?.resultText)
          ?? outcome?.finishedSummary
          ?? latestResultPresentation?.summary
          ?? outcome?.finishedTurn
      const latestLabel = latestEventIsProblem && latestResultPresentation
        ? `${latestResultPresentation.label}:`
        : outcome?.resultText
          ? 'Recorded result:'
          : 'Recorded update:'
      const latestDetail = outcome?.resultText
        ?? (latestResultPresentation?.hasPayload ? latestResultPresentation.detail : null)
      if (!hasRecordedResult && !isTerminalInactiveRun(selectedRun)) return null
      return (
        <>
          {latestSummary && <p><span className="text-sm text-dim">{latestLabel}</span> {latestSummary}</p>}
          {latestDetail && <details className="run-report" open={reportOpen}>
            <summary onClick={(event) => {
              event.preventDefault()
              setReportOpen((open) => !open)
            }}>{outcome?.resultText ? 'Read full result' : 'Read full update'}</summary>
            {reportOpen && <pre className="dashboard-payload">{latestDetail}</pre>}
          </details>}
          {!hasRecordedResult && isTerminalInactiveRun(selectedRun) && (
            <p className="text-sm text-dim">
              {statusLabel(selectedRun)} is recorded, but no result text was retained.
            </p>
          )}
        </>
      )
    })()
    : null
  const hasRecordedProgress = Boolean(
    compatibilitySummary?.repairing
    || compatibilityProgressText
    || outcome?.currentTurn
    || observedInvocation
    || outcome?.decision
    || outcome?.finishedTurn
    || outcome?.finishedSummary
    || checkpoints,
  )
  const recordedProgressContent: ReactNode | null = hasRecordedProgress ? <>
    {compatibilitySummary?.repairing && <p><strong>Repairing</strong>{compatibilitySummary.overlayFileName ? <> · {compatibilitySummary.overlayFileName}</> : null}</p>}
    {compatibilityProgressText && <p>{compatibilityProgressText}</p>}
    {checkpoints && <p>{checkpointProgressText(checkpoints)}</p>}
    {outcome?.currentTurn && <p>Current turn: {outcome.currentTurn}</p>}
    {observedInvocation && <p>Last observed invocation: <span>{observedInvocation}</span></p>}
    {outcome?.decision && <p>{outcome.decision}</p>}
    {outcome?.finishedTurn && <p>Last finished turn: {outcome.finishedTurn}</p>}
    {outcome?.finishedSummary && <p>Last finished summary: {outcome.finishedSummary}</p>}
  </> : null

  const workflowRoleList = stepRoleMap ? [...new Set(Object.values(stepRoleMap))].sort() : []
  const otherConfiguredRoles = committedForm
    ? Object.keys(committedForm.roles).filter((role) => !workflowRoleList.includes(role)).sort()
    : []
  const upgradeChain = effectiveTeam
    ? committedForm?.teams?.[effectiveTeam]
      ? launchTeamUpgradeRoute(committedForm, effectiveTeam)
      : capabilities?.team_upgrade_chains?.[effectiveTeam] ?? null
    : null
  const workerUpgradeStages = upgradeChain?.map((team) => {
    const teamRoles = teamRolesForPreview(team)
    const worker = resolveStepRole('worker', teamRoles, committedForm?.roles ?? {}, teamRoleSourcesForPreview(team))
    return {
      team,
      worker,
      modelEffort: worker.selector ? selectorModelEffortText(worker.selector, committedForm) : '',
    }
  }) ?? []
  const reviewerResolution = resolveStepRole('reviewer', effectiveTeamRoles, committedForm?.roles ?? {}, effectiveTeamRoleSources)
  const reviewerModelEffort = reviewerResolution.selector
    ? selectorModelEffortText(reviewerResolution.selector, committedForm)
    : ''

  function membershipRow(role: string, teamRoles: Record<string, string>, teamName: string | null, teamRoleSources?: Record<string, string>) {
    const resolution = resolveStepRole(role, teamRoles, committedForm?.roles ?? {}, teamRoleSources)
    const modelEffort = resolution.selector ? selectorModelEffortText(resolution.selector, committedForm) : ''
    return (
      <tr key={`${teamName ?? 'workspace'}-${role}`}>
        <td data-label="Role">{formatMachineLabel(role)}</td>
        <td data-label="Selector" className="mono">{resolution.selector ?? <span className="text-dim">not assigned</span>}</td>
        <td data-label="Model / effort">{modelEffort || '—'}</td>
        <td data-label="Source" className="text-sm">
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
              <section className="dashboard-section launch-preparation-preview" aria-label="Effective choices for this launch">
                <details className="launch-preparation-details">
                  <summary>Effective choices and role details</summary>
                  <div className="launch-preparation-details-body">
                <div className="section-heading">
                  <h4>Effective choices for this launch</h4>
                  <span className="text-xs text-dim">from your selections and the committed configuration</span>
                </div>
                <dl className="run-preview-list">
                  <div><dt>Plan</dt><dd className="mono">{startPlanPath.trim() || <span className="text-dim">Not chosen</span>}</dd></div>
                  <div><dt>Workflow</dt><dd>{!startWorkflow.trim() && committed === null
                    ? <span className="text-dim">{configurationDisplay}</span>
                    : effectiveWorkflow
                    ? <><span className="mono">{formatMachineLabel(effectiveWorkflow)}</span> — {effectiveWorkflowSource}</>
                    : <span className="text-dim">No default workflow configured — choose a workflow or set a global default in Settings</span>}</dd></div>
                  <div><dt>Max turns</dt><dd>{startMaxTurnsProblem !== null
                    ? <><span className="mono">{startMaxTurns.trim()}</span> — invalid override: correct the Max turns field</>
                    : !startMaxTurns.trim() && committed === null
                      ? <span className="text-dim">{configurationDisplay}</span>
                    : effectiveMaxTurns !== null
                      ? <><span className="mono">{effectiveMaxTurns}</span> — {effectiveMaxTurnsSource}</>
                      : <span className="text-dim">{effectiveMaxTurnsSource}</span>}</dd></div>
                  <div><dt>Team</dt><dd>{!startTeam.trim() && committed === null
                    ? <span className="text-dim">{configurationDisplay}</span>
                    : effectiveTeam
                    ? <>
                        <span className="mono">{resolvedTeamFamilyLabel ?? formatMachineLabel(effectiveTeam)}</span>
                        {' — '}{effectiveTeamSource}
                        {effectiveTeamGroup && <>
                          {' · baseline stage '}
                          <span className="mono">{resolvedTeamStageLabel ?? formatMachineLabel(effectiveTeam)}</span>
                          <span className="text-xs text-dim"> ({effectiveTeam})</span>
                        </>}
                      </>
                    : <span className="text-dim">{effectiveTeamSource}</span>}</dd></div>
                </dl>
                <div className="launch-role-summary">
                  <div className="section-heading">
                    <h4>Roles at launch</h4>
                    <span className="text-xs text-dim">the selected team and committed role assignments</span>
                  </div>
                  <dl className="run-role-summary">
                    <div>
                      <dt>Worker upgrade chain</dt>
                      <dd>
                        {committed === null ? (
                          <span className="text-dim">{configurationDisplay}</span>
                        ) : workerUpgradeStages.length > 0 ? (
                          <ol className="upgrade-chain upgrade-chain-compact">
                            {workerUpgradeStages.map((stage) => (
                              <li key={stage.team}>
                                <span className="mono">{formatMachineLabel(stage.team)}</span>
                                <span className="text-sm"> — {stage.worker.selector
                                  ? <><span className="mono">{stage.worker.selector}</span>{stage.modelEffort ? <> · {stage.modelEffort}</> : null}</>
                                  : <span className="text-dim">worker not assigned</span>}</span>
                              </li>
                            ))}
                          </ol>
                        ) : effectiveTeam ? (
                          <span className="text-dim">No configured worker upgrade chain for <span className="mono">{formatMachineLabel(effectiveTeam)}</span>.</span>
                        ) : (
                          <span className="text-dim">No team selected; global role assignments apply.</span>
                        )}
                      </dd>
                    </div>
                    <div>
                      <dt>Reviewer</dt>
                      <dd>
                        {committed === null ? (
                          <span className="text-dim">{configurationDisplay}</span>
                        ) : reviewerResolution.selector ? (
                          <><span className="mono">{reviewerResolution.selector}</span>{reviewerModelEffort ? <> · {reviewerModelEffort}</> : <span className="text-dim"> · model/effort not reported</span>}</>
                        ) : (
                          <span className="text-dim">Not assigned in the selected team or global roles.</span>
                        )}
                      </dd>
                    </div>
                  </dl>
                </div>
                <details className="launch-preview-details">
                  <summary>Details</summary>
                  <div className="launch-preview-details-body">
                {workflowRoleList.length > 0 && (
                  <div>
                    <h4>Team members</h4>
                    <table className="guided-table responsive-data-table">
                      <caption className="text-xs text-dim">
                        Effective role assignments for this workflow{effectiveTeam ? ` with team ${formatMachineLabel(effectiveTeam)}` : ''} (team override → global fallback)
                      </caption>
                      <thead><tr><th scope="col">Role</th><th scope="col">Selector</th><th scope="col">Model / effort</th><th scope="col">Source</th></tr></thead>
                      <tbody>
                        {workflowRoleList.map((role) => membershipRow(role, effectiveTeamRoles, effectiveTeam || null))}
                      </tbody>
                    </table>
                    {otherConfiguredRoles.length > 0 && (
                      <details>
                        <summary className="text-xs text-dim">Other configured roles ({otherConfiguredRoles.length})</summary>
                        <table className="guided-table responsive-data-table">
                          <tbody>
                            {otherConfiguredRoles.map((role) => membershipRow(role, effectiveTeamRoles, effectiveTeam || null))}
                          </tbody>
                        </table>
                      </details>
                    )}
                  </div>
                )}
                {effectiveWorkflow && (effectiveSteps.length ? (
                  <table className="guided-table responsive-data-table">
                    <caption className="text-xs text-dim">
                      Executable steps with their exact role ({effectiveTeam ? `team ${formatMachineLabel(effectiveTeam)} override → global` : 'global selector'})
                    </caption>
                    <thead><tr><th scope="col">Step</th><th scope="col">Role → selector</th></tr></thead>
                    <tbody>
                      {effectiveSteps.map((step) => {
                        const role = stepRoleMap?.[step]
                        const resolution = role
                          ? resolveStepRole(role, effectiveTeamRoles, committedForm?.roles ?? {}, effectiveTeamRoleSources)
                          : null
                        return (
                          <tr key={step}>
                            <td data-label="Step" className="mono">{formatMachineLabel(step)}</td>
                            <td data-label="Role → selector">
                              {!resolution
                                ? <span className="text-dim text-sm">Exact role preview unavailable for this step.</span>
                                : resolution.selector
                                  ? <div className="text-sm">
                                      <span className="mono">{formatMachineLabel(resolution.role)} → {resolution.selector}</span>
                                      {selectorModelEffortText(resolution.selector, committedForm) && <> · {selectorModelEffortText(resolution.selector, committedForm)}</>}
                                      {resolution.source === 'team' && <> — team override</>}
                                      {resolution.source === 'global' && <> — global</>}
                                    </div>
                                  : <div className="text-sm"><span className="mono">{formatMachineLabel(resolution.role)}</span> — <span className="text-dim">missing — assign it in Settings → Roles or a team override</span></div>}
                            </td>
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>
                ) : (
                  <p className="text-sm text-dim">
                    Executable steps for <span className="mono">{formatMachineLabel(effectiveWorkflow)}</span> are not available from the
                    committed configuration — check the workflow declaration in Settings.
                  </p>
                ))}
                {effectiveTeam && (
                  <div>
                    <h4>Worker upgrade chain</h4>
                    {!capabilities ? (
                      <p className="text-sm text-dim">The upgrade chain cannot be read because project capabilities failed to load. Refresh to retry.</p>
                    ) : !upgradeChain ? (
                    <p className="text-sm text-dim">Team <span className="mono">{formatMachineLabel(effectiveTeam)}</span> is not part of the configured upgrade graph. Refresh, or check the team links in Settings.</p>
                    ) : (
                      <>
                        <ol className="upgrade-chain">
                          {upgradeChain.map((team, index) => {
                            const teamRoles = teamRolesForPreview(team)
                            const worker = resolveStepRole('worker', teamRoles, committedForm?.roles ?? {}, teamRoleSourcesForPreview(team))
                            const isLast = index === upgradeChain.length - 1
                            const modelEffort = worker.selector ? selectorModelEffortText(worker.selector, committedForm) : ''
                            const stageRoles = [...new Set([...Object.keys(teamRoles), ...Object.keys(committedForm?.roles ?? {})])].sort()
                            return (
                              <li key={team}>
                                <span className="mono">{formatMachineLabel(team)}</span>
                                <span className="text-sm"> — {formatMachineLabel('worker')} {worker.selector
                                  ? <span className="mono">{worker.selector}</span>
                                  : <span className="text-dim">not assigned</span>}{modelEffort ? <> · {modelEffort}</> : null}</span>
                                {isLast && <span className="text-xs text-dim"> — no further upgrade configured</span>}
                                {stageRoles.length > 0 && (
                                  <details className="upgrade-stage-details">
                                    <summary className="text-xs text-dim">stage team members</summary>
                                    <table className="guided-table responsive-data-table">
                                      <tbody>
                                        {stageRoles.map((role) => membershipRow(role, teamRoles, team, teamRoleSourcesForPreview(team)))}
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
                  </div>
                </details>
                  </div>
                </details>
              </section>

  )
  const worktreePreflightPanel = (
    <WorktreePreflightPanel
      status={displayedWorktreePreflight.status}
      result={displayedWorktreePreflight.result}
      error={displayedWorktreePreflight.error}
      dirtyWorktreeConfirmed={dirtyWorktreeConfirmed}
      onDirtyWorktreeConfirmedChange={setDirtyWorktreeConfirmed}
      onRefresh={() => void refreshPreflight()}
      onLoadMore={() => void loadMorePreflight()}
      dirtyQuestionMessage={dirtyStartupQuestion ? startupQuestion?.message ?? 'The working tree changed while starting. Review it before continuing.' : null}
    />
  )
  const reviewedPreflight = displayedWorktreePreflight.status === 'ready' ? displayedWorktreePreflight.result : null
  const reviewedChangeCount = reviewedPreflight ? Math.max(reviewedPreflight.total_items, reviewedPreflight.items.length) : 0
  const reviewedExecutionMode = reviewedPreflight?.execution_mode === 'new_worktree'
    ? 'New worktree'
    : reviewedPreflight?.execution_mode === 'same_checkout'
      ? 'Existing checkout'
      : 'Not inspected'
  const reviewedWorkingTree = reviewedPreflight
    ? reviewedPreflight.dirty
      ? reviewedChangeCount > 0
        ? `${reviewedChangeCount} uncommitted change${reviewedChangeCount === 1 ? '' : 's'}${dirtyWorktreeConfirmed ? ' · acknowledged' : ' · acknowledgement required'}`
        : `Uncommitted changes detected${dirtyWorktreeConfirmed ? ' · acknowledged' : ' · acknowledgement required'}`
      : 'Clean checkout'
    : displayedWorktreePreflight.status === 'error'
      ? 'Inspection failed — refresh before starting'
      : 'Inspection in progress'
  const launchReview = (
    <div className="launch-review-summary">
      <p className="launch-review-consequence">Review the effective choices and working-tree consequence below. No run is allocated until you choose Start run.</p>
      <dl className="run-preview-list">
        <div><dt>Plan</dt><dd className="mono">{startPlanPath.trim() || 'Not chosen'}</dd></div>
        <div><dt>Workflow</dt><dd>{effectiveWorkflow ? formatMachineLabel(effectiveWorkflow) : 'Not resolved'}</dd></div>
        <div><dt>Team</dt><dd>{effectiveTeam ? `${resolvedTeamFamilyLabel ?? formatMachineLabel(effectiveTeam)} · ${effectiveTeamSource}` : effectiveTeamSource}</dd></div>
        <div><dt>Turn limit</dt><dd>{effectiveMaxTurns !== null ? `${effectiveMaxTurns} · ${effectiveMaxTurnsSource}` : 'Not resolved'}</dd></div>
        <div><dt>Start step</dt><dd>{startStep.trim() ? formatMachineLabel(startStep.trim()) : 'Workflow beginning'}</dd></div>
      </dl>
      <dl className="launch-review-requirements">
        <div>
          <dt>Working tree</dt>
          <dd>{reviewedWorkingTree}</dd>
        </div>
        <div>
          <dt>Execution mode</dt>
          <dd>{reviewedExecutionMode}</dd>
        </div>
        <div>
          <dt>Start readiness</dt>
          <dd>
            {!startReviewReady
              ? 'Choices changed — close this review and review again.'
              : launchBlocker ?? (worktreeLaunchBlocked ? 'Complete the working-tree acknowledgement or wait for inspection.' : 'Ready for the final start action.')}
          </dd>
        </div>
      </dl>
      <details className="launch-review-details">
        <summary>Full launch details</summary>
        <dl className="launch-review-details-list">
          <div>
            <dt>Checkout</dt>
            <dd className="mono">{reviewedPreflight?.checkout_path ?? 'Not inspected'}</dd>
          </div>
          <div>
            <dt>Baseline stage</dt>
            <dd>{startTeamStage.trim() ? teamStageOptionLabel(startTeamStage.trim()) : 'Workflow default'}</dd>
          </div>
          <div>
            <dt>Extra instructions</dt>
            <dd>{extraInstructions.length > 0 ? `${extraInstructions.length} line${extraInstructions.length === 1 ? '' : 's'}` : 'None'}</dd>
          </div>
          <div>
            <dt>Worker upgrade chain</dt>
            <dd>{workerUpgradeStages.length > 0 ? workerUpgradeStages.map((stage) => formatMachineLabel(stage.team)).join(' → ') : 'No configured upgrade chain'}</dd>
          </div>
          <div>
            <dt>Reviewer</dt>
            <dd className="mono">{reviewerResolution.selector ?? 'Not assigned'}</dd>
          </div>
        </dl>
      </details>
    </div>
  )
  const restartSourcePlan = restartSource?.plan_path ?? String(restartSource?.evidence.plan_path ?? '')
  const restartSourceWorkflow = restartSource?.workflow_name ?? ''
  const restartSourceTeam = restartSource?.team ?? ''
  const restartSourceMaxTurns = restartSource?.max_turns?.toString() ?? ''
  const restartChangedValues = restartSource ? [
    {
      label: 'Plan',
      value: startPlanPath.trim() || 'Not selected',
      changed: startPlanPath.trim() !== restartSourcePlan,
    },
    {
      label: 'Workflow',
      value: restartDraftWorkflow ? formatMachineLabel(restartDraftWorkflow) : 'Not selected',
      changed: restartDraftWorkflow !== restartSourceWorkflow,
    },
    {
      label: 'Team',
      value: startTeam.trim() ? formatMachineLabel(startTeam.trim()) : 'Saved/default selection',
      changed: startTeam.trim() !== restartSourceTeam,
    },
    {
      label: 'Max turns',
      value: startMaxTurns.trim() || 'Saved/default selection',
      changed: startMaxTurns.trim() !== restartSourceMaxTurns,
    },
    {
      label: 'Start step',
      value: startStep.trim() ? formatMachineLabel(startStep.trim()) : 'Workflow beginning',
      changed: startStep.trim() !== (restartSource?.selected_start_step ?? ''),
    },
    {
      label: 'Extra instructions',
      value: extraInstructions.length > 0 ? `${extraInstructions.length} line${extraInstructions.length === 1 ? '' : 's'}` : 'None',
      changed: extraInstructions.length > 0 || missingRestartInstructions,
    },
  ] : []
  const restartActions = restartSource && restartPhase ? (
                <section className="dashboard-section">
                  <div className="section-heading"><h4>Restart review</h4><span className="text-xs text-dim">review → stop safely → create successor</span></div>
                  <div className="notice">
                    No stop or start has been requested. Review the exact source and successor choices below before the final confirmation.
                  </div>
                  <dl className="run-metadata restart-review-details">
                    <div><dt>Source run</dt><dd className="mono">{restartSource.run_id}</dd></div>
                    <div><dt>Source revision</dt><dd>{restartSource.revision}</dd></div>
                    <div><dt>Changed values</dt><dd><ul>{restartChangedValues.map(item => <li key={item.label}><strong>{item.label}:</strong> {item.value}{item.changed ? ' · changed' : ' · unchanged'}</li>)}</ul></dd></div>
                  </dl>
                  <p className="text-sm text-dim">
                    If the source is active, the existing owner-stop protection runs first, then the dashboard waits for exact terminal inactivity. No successor starts when that proof is uncertain; an inactive source is not stopped again.
                  </p>
                  <p className="text-sm text-dim">
                    Confirming creates one distinct successor linked to this source. The source history, progress, and evidence remain readable, while the successor receives a fresh turn/token budget; prior usage stays attributed to the source.
                  </p>
                  {restartDraftHint
                    ? <div className="text-xs text-dim">{restartDraftHint}</div>
                    : !restartPendingConfirmation && !restartInProgress && (
                      <div className="dashboard-actions"><button className="btn btn-danger" onClick={() => setRestartPhase('confirming')}>Review successor choices…</button></div>
                    )}
                  {restartPendingConfirmation && !restartDraftReady && (
                    <div className="text-xs text-dim">Select a successor workflow and plan in the New run form before confirming.</div>
                  )}
                  {restartPendingConfirmation && restartDraftReady && (
                    <div className="confirmation">
                      <span>
                        Confirm source <span className="mono">{restartSource.run_id}</span> (revision {restartSource.revision}) and start
                        successor workflow <strong>{formatMachineLabel(restartDraftWorkflow)}</strong>
                        {startStep.trim() ? <> at step <strong>{formatMachineLabel(startStep.trim())}</strong></> : null}
                        {startPlanPath ? <> with plan <span className="mono">{startPlanPath.trim()}</span></> : null}? The successor records this run as its restart source.
                      </span>
                      <button className="btn btn-danger" disabled={busyAction !== null || restartInProgress} onClick={() => void handleConfirmedRestart()}>Confirm restart</button>
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

  function cancelNewRun() {
    restartFocusPendingRef.current = false
    invalidateStartReview()
    if (!restartInProgress && !pendingSuccessorStart) { setRestartPhase(null); setRestartSource(null) }
    setLocalPage('runs')
    onCancelNewRun?.()
  }

  const renderAcceptedDetail = projectAvailable === true && selectedDetailAccepted && !newRunPage
  const retainedProjectShell = exposedShellProjectId === projectId && projectAvailable !== false && !newRunPage
  const dashboardRenderable = !loading || renderAcceptedDetail || retainedProjectShell
  const hosted = useHeaderSlots(`run-dashboard:${projectId}`, {
    context: <h2 className="header-context-title">{newRunPage ? 'New run' : 'Runs'}</h2>,
    local: newRunPage ? <button className="btn btn-secondary btn-sm" onClick={cancelNewRun}>← Run history</button> : <label className="header-filter-select"><span>Run history</span><select aria-label="Run history" value={historyFilter} onChange={event => setHistoryFilter(event.target.value as typeof historyFilter)}><option value="visible">Visible</option><option value="archived">Archived</option><option value="all">All history</option></select></label>,
    primary: newRunPage ? <button className="btn btn-primary btn-sm" onClick={(event) => void (startReviewIdentity ? confirmStart() : openStartReview(event.currentTarget))} disabled={startReviewIdentity ? startDisabled || !startReviewReady || startRevalidationPendingForRender || Boolean(restartActions) : startDisabled || Boolean(restartActions)}>{busyAction === 'start' ? 'Starting…' : startRevalidationPendingForRender ? 'Checking…' : startReviewIdentity ? 'Start run' : 'Review start…'}</button> : <button className="btn btn-primary btn-sm" onClick={openNewRunPage}>New run</button>,
    more: <MoreMenu label={newRunPage ? 'More new run actions' : 'More run page actions'} triggerLabel="More">
      {newRunPage ? <MenuItem disabled={busyAction === 'start'} onClick={startReviewIdentity ? cancelStartReview : cancelNewRun}>{startReviewIdentity ? 'Cancel review' : 'Cancel'}</MenuItem> : <>
        <MenuItem onClick={() => void handleCopyLink()}>Copy link</MenuItem>
        <MenuItem disabled={refreshing || loading} onClick={() => void refreshPage()}>{refreshing ? 'Refreshing…' : 'Refresh'}</MenuItem>
      </>}
    </MoreMenu>,
  }, visible && dashboardRenderable)

  if (!dashboardRenderable) {
    return <div className="card dashboard-loading"><div className="spinner" />Loading runs…</div>
  }

  return (
    <div className="run-dashboard">
      {!hosted && <div className="run-dashboard-header">
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
      </div>}

      {copyState === 'copied' && <div className="success-message" role="status">{copyKind === 'run-id' ? 'Full run ID copied to the clipboard.' : 'Link copied to the clipboard.'}</div>}
      {copyState === 'failed' && <div className="notice" role="status">Clipboard access failed — {copyKind === 'run-id' ? 'copy the full run ID from Diagnostics instead.' : 'copy the address from the browser address bar instead.'}</div>}
      {projectReadError && <div className="error-message" role="alert">{projectReadError}</div>}
      {dashboardReadError && <div className="error-message" role="alert">{dashboardReadError}</div>}
      {selectedRunReadError && <div className="error-message" role="alert">{selectedRunReadError}</div>}
      {actionError && <div className="error-message" role="alert">{actionError}{failedRequestId && <button className="btn btn-secondary btn-sm" onClick={() => { setLocalPage('runs'); onRunStarted?.(failedRequestId); selectRun(failedRequestId) }}>View failed request</button>}</div>}
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

      {projectAvailable && startupQuestion && (!dirtyStartupQuestion || !newRunPage) && (newRunPage || selectedRun?.run_id === startupQuestion.run_id) && (
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
        <SidebarEditorLayout selection={selectedRunId} navigationVersion={navigationVersion} listLabel="Run history" detailEntry={explicitRunNavigation ?? Boolean(requestedRunId)} navigation={
          <section ref={runListRef} className="card run-list" aria-label="Project runs">
            <div className="section-heading"><h3>Project runs</h3><span className="text-xs text-dim">{historyCountLabel}</span></div>
            {!hosted && <label>Run history<select className="input" aria-label="Run history" value={historyFilter} onChange={event => setHistoryFilter(event.target.value as typeof historyFilter)}><option value="visible">Visible</option><option value="archived">Archived</option><option value="all">All history</option></select></label>}
            {pinnedRun && <>
              <span className="run-list-pinned-label">Selected · older history</span>
              <RunListItem key={pinnedRun.run_id} run={pinnedRun} stableKey={pinnedRun.run_id} dataSidebarEditorItem={pinnedRun.run_id} rowClassName="run-list-pinned" contextLabel="Selected · older history" selected onSelect={() => selectRun(pinnedRun.run_id)} />
            </>}
            {pageRuns.length === 0 && !pinnedRun ? (historyIncomplete ? null : <p className="text-sm text-dim">No runs yet</p>) : ordinaryRuns.map((run) => (
              <RunListItem
                key={run.run_id}
                run={run}
                stableKey={run.run_id}
                dataSidebarEditorItem={run.run_id}
                selected={selectedRunId === run.run_id}
                onSelect={() => selectRun(run.run_id)}
              />
            ))}
            {outcomeGapRuns.length > 0 && <div className="run-list-gap-group">
              <button type="button" className="run-list-gap-toggle" aria-expanded={gapOpen} aria-controls={`${technicalId}-outcome-gap`} disabled={selectedGapInGroup} onClick={() => setGapDisclosure({ scope: currentHistoryScope, open: !gapOpen })}>
                <span aria-hidden="true">{gapOpen ? '▾' : '▸'}</span>
                <span>Older runs without a recorded outcome · {outcomeGapRuns.length} loaded</span>
              </button>
              <div id={`${technicalId}-outcome-gap`} hidden={!gapOpen} className="run-list-gap-items">
                {outcomeGapRuns.map(run => <RunListItem key={run.run_id} run={run} stableKey={run.run_id} dataSidebarEditorItem={run.run_id} selected={selectedRunId === run.run_id} quietOutcome onSelect={() => selectRun(run.run_id)} />)}
              </div>
            </div>}
            {nextRunCursor && <button className="btn btn-secondary" disabled={refreshing} onClick={() => void loadMoreRuns()}>Load more runs</button>}
          </section>}>

          <section className="card run-detail" aria-label="Run details">
            {selectedRunId && deletedIds.has(selectedRunId) ? <div><h3>Deleted record</h3><p>Workflow files and recovery data are retained.</p></div>
              : loading && selectedRunId && !selectedDetailAccepted
                ? <div role="status"><h3>Loading run details</h3><p className="text-sm text-dim">Waiting for the exact run <span className="mono">{selectedRunId}</span>.</p></div>
                : !selectedRun ? <p className="text-sm text-dim">Select a recorded run to inspect its server status and events.</p> : <>
              <RunOverview
                header={<div className="run-progress-header">
                  <div className="section-heading">
                    <div>
                      <h3>{selectedPlanFileName === 'Not reported' ? `Run ${selectedRun.run_id}` : selectedPlanFileName}{selectedPlanPresentation?.date && <>{' '}<span className="run-title-date">{selectedPlanPresentation.date}</span></>}</h3>
                      <button className="text-xs text-dim mono run-id-copy" title="Copy full run ID" aria-label={selectedRun.run_id} aria-describedby={`${technicalId}-copy-run-id`} onClick={() => void handleCopyRunId()}>{shortRunId(selectedRun.run_id)}<span className="copy-full-id-label">Copy full ID</span></button>
                      <span id={`${technicalId}-copy-run-id`} className="sr-only">Copy full run ID</span>
                      <LiveRunHeaderTiming run={selectedRun} />
                    </div>
                    <span className={`status-pill status-${runDisplayProjection(selectedRun).category}`} data-status-category={runDisplayProjection(selectedRun).category}>{statusLabel(selectedRun)}</span>
                    {selectedRun.history_state === 'archived' && <span className="status-pill">Archived</span>}
                    {selectedRun.history_state === 'archived' && <button className="btn btn-secondary" disabled={busyAction === 'history' || historyConfirm !== null} onClick={() => void mutateHistory('restore')}>Restore</button>}
                    {(canRestart || selectedRunHasLiveControls || Boolean(restartAdmission?.reason)) && <MoreMenu label="Run actions" triggerLabel="Actions" triggerContent="Actions" className="run-actions-menu">
                      {canRestart && <MenuItem disabled={!canMutate || busyAction !== null} onClick={openRestart}>Configure restart…</MenuItem>}
                      {!canRestart && restartAdmission?.reason && <MenuItem disabled onClick={() => {}}>Restart unavailable: {restartAdmission.reason}</MenuItem>}
                      {selectedRunHasLiveControls && <MenuItem disabled={!canMutate || busyAction !== null} onClick={() => { setStopReviewOpen(false); setConfirmOwnerStop(false); setAdjustRunOpen(true) }}>Adjust run settings…</MenuItem>}
                      {selectedRunHasLiveControls && (hasSafeControl('owner_stop') || capabilities === null) && <MenuItem disabled={!canMutate || busyAction !== null || !hasSafeControl('owner_stop')} onClick={() => { setAdjustRunOpen(false); setStopReviewOpen(true); setConfirmOwnerStop(false) }}>Review stop options…</MenuItem>}
                    </MoreMenu>}
                    <MoreMenu label="More run actions">
                      {selectedRun.history_state !== 'archived' && <MenuItem disabled={busyAction === 'history' || historyConfirm !== null} onClick={() => { if (selectedRun.activity === 'active') { setHistoryConfirm('archive'); setAcknowledgeActive(false) } else void mutateHistory('archive') }}>Archive</MenuItem>}
                      <MenuItem danger disabled={busyAction === 'history' || historyConfirm !== null} onClick={() => { setHistoryConfirm('delete'); setAcknowledgeActive(false) }}>Delete record…</MenuItem>
                    </MoreMenu>
                  </div>
                  {historyConfirm && <div role="alertdialog" aria-label={`${historyConfirm} record ${selectedRun.run_id}`}>
                    <p>{selectedRun.run_id}: {historyConfirm === 'delete' ? 'Delete this run from history permanently? Workflow files and recovery data will be kept.' : 'Archive this run from the default history lists?'}</p>
                    {selectedRun.activity === 'active' && <label><input type="checkbox" checked={acknowledgeActive} onChange={event => setAcknowledgeActive(event.target.checked)} />I understand this will not stop the active workflow.</label>}
                    <button className="btn btn-danger" disabled={busyAction === 'history' || (selectedRun.activity === 'active' && !acknowledgeActive)} onClick={() => void mutateHistory(historyConfirm)}>Confirm {historyConfirm}</button>
                    <button className="btn btn-secondary" onClick={() => setHistoryConfirm(null)}>Cancel</button>
                  </div>}
                </div>}
                notices={<>
                  {selectedRun.ownership === 'legacy' && <div className="notice">Legacy execution record. Workflow controls are unavailable; history controls remain available.</div>}
                  {pendingBoundaryStop && <div className="notice" role="status">Stop requested — finishing current turn. The current worker/reviewer call may finish before the run becomes Stopped; this does not approve the checkpoint.</div>}
                  {selectedRun.evidence.no_agent_started === true && selectedRun.status !== 'running' && <p>No agent started.</p>}
                  {streamState === 'reconnecting' && <div className="notice">Updates are stale. Use Refresh to retry.</div>}
                  {deliveryWarning && <div className="notice run-delivery-warning" role="alert">Recorded delivery issue: {deliveryWarning}. Open Delivery evidence for the receipt.</div>}
                  {selectedRunIssue && <section className={`run-issue-summary run-issue-${selectedRunIssue.kind}`} role={selectedRunIssue.kind === 'failure' ? 'alert' : undefined}>
                    <div>
                      <strong>{selectedRunIssue.kind === 'failure' ? 'Failure' : 'Needs attention'}</strong>
                      <p>{failedReview ? 'Review stopped before a decision.' : selectedRunIssue.cause}</p>
                    </div>
                    <div className="dashboard-actions">
                      {canResume && (!confirmResume
                        ? <button className="btn btn-primary" disabled={restartInProgress} onClick={() => setConfirmResume(true)}>Resume as new run…</button>
                        : <div className="confirmation"><span>Confirm explicit resume. Source {selectedRun.run_id} remains visible; the server creates a distinct continuation run with the same workflow and current configuration source.</span><button className="btn btn-primary" disabled={busyAction === 'resume'} onClick={() => void handleResume()}>Confirm resume</button><button className="btn btn-secondary" onClick={() => setConfirmResume(false)}>Cancel</button></div>)}
                      {canRestart && <span className="text-sm text-dim">Configure a restart from Actions after reviewing the recorded issue.</span>}
                      {!canResume && !canRestart && <span className="text-sm text-dim">Open Diagnostics for the recorded details.</span>}
                    </div>
                  </section>}
                  {!selectedRunIssue && selectedRun.reason && runDisplayProjection(selectedRun).category !== 'outcome-unrecorded' && <div className="notice">{conciseRunText(selectedRun.reason) ?? 'A run reason was recorded.'}</div>}
                </>}
                currentWork={overviewCurrentWork}
                latestResult={overviewLatestResult}
                events={events}
                streamNotice={streamNotice}
              />

              {selectedRunHasLiveControls && adjustRunOpen && <details className="dashboard-section" open><summary onClick={event => { event.preventDefault(); setAdjustRunOpen(false) }}>Adjust run</summary>
                {!canMutate && loading && <div className="notice">Initial run controls are pending admission. Actions stay disabled until loading finishes.</div>}
                <div className="notice">
                  Changes are saved now and apply at the next safe turn or when the run resumes. Refresh after saving Settings to use newly saved teams and profiles; restarting is not required.
                </div>
                <div className="dashboard-form-grid">
                  <label className="dashboard-field"><span>Max turns</span><input className="input" aria-label="Control max turns" type="number" min="1" value={controlMaxTurns} disabled={!canMutate || !hasSafeControl('max_turns')} onChange={(event) => setControlMaxTurns(event.target.value)} /></label>
                  <label className="dashboard-field"><span>Team</span><select className="input" aria-label="Control team" value={controlTeam} disabled={!canMutate || !hasSafeControl('team')} onChange={(event) => setControlTeam(event.target.value)}><option value="">No team</option>{(capabilities?.teams ?? []).map((team) => <option key={team} value={team}>{formatMachineChoice(team, capabilities?.teams ?? [])}</option>)}</select></label>
                </div>
                {roleChoices.map((role) => {
                  const admitted = capabilities?.admitted_role_selectors?.[role] ?? []
                  return (
                    <label className="dashboard-field" key={role}>
                      <span>Selector for {formatMachineChoice(role, roleChoices)}{overrideRoles(controlOverride)[role] ? ` (current override: ${overrideRoles(controlOverride)[role]})` : ''}</span>
                      <select
                        className="input"
                        aria-label={`Selector for ${formatMachineChoice(role, roleChoices)}`}
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

              {stopReviewOpen && hasSafeControl('owner_stop') && selectedRunHasLiveControls && <section className="dashboard-section run-stop-review" aria-label="Review stop options">
                <div className="section-heading">
                  <div>
                    <h4>Review stop options</h4>
                    <span className="text-xs text-dim">No stop request is submitted until you choose a final action.</span>
                  </div>
                  <button className="btn btn-secondary btn-sm" onClick={() => { setStopReviewOpen(false); setConfirmOwnerStop(false) }}>Cancel review</button>
                </div>
                <div className="run-stop-options">
                  <div className="run-stop-option">
                    <h5>Request a graceful stop</h5>
                    <p className="text-sm text-dim">The current worker or reviewer call may finish at its existing safe boundary. This does not approve the checkpoint.</p>
                    {pendingBoundaryStop
                      ? <p className="notice" role="status">Stop after the current turn is already requested. The pending request remains visible in the overview above.</p>
                      : <button className="btn btn-primary" disabled={!canMutate || busyAction !== null || restartInProgress} onClick={() => void handleBoundaryStop()}>Request stop after current turn</button>}
                  </div>
                  <div className="run-stop-option">
                    <h5>Interrupt immediately</h5>
                    <p className="text-sm text-dim">This interrupts the active worker/reviewer call and does not approve the checkpoint.</p>
                    {!confirmOwnerStop
                      ? <button className="btn btn-secondary" disabled={!canMutate || busyAction !== null || restartInProgress} onClick={() => setConfirmOwnerStop(true)}>Stop now…</button>
                      : <div className="confirmation"><span>Stop {selectedRun.run_id} immediately? This interrupts the active worker/reviewer call; it does not approve the checkpoint.</span><button className="btn btn-danger" disabled={!canMutate || busyAction === 'owner-stop' || restartInProgress} onClick={() => void handleOwnerStop()}>Stop now</button><button className="btn btn-secondary" onClick={() => setConfirmOwnerStop(false)}>Cancel</button></div>}
                  </div>
                </div>
              </section>}

              {!selectedRunIssue && canResume && <section className="dashboard-section dashboard-actions">
                <>
                  {!confirmResume ? <button className="btn btn-primary" disabled={restartInProgress} onClick={() => setConfirmResume(true)}>Resume as new run…</button> : <div className="confirmation"><span>Confirm explicit resume. Source {selectedRun.run_id} remains visible; the server creates a distinct continuation run with the same workflow and current configuration source.</span><button className="btn btn-primary" disabled={busyAction === 'resume'} onClick={() => void handleResume()}>Confirm resume</button><button className="btn btn-secondary" onClick={() => setConfirmResume(false)}>Cancel</button></div>}
                </>
              </section>}

              {!canResume && selectedRun.evidence.no_agent_started === true && <p className="text-sm">No execution state is available to Resume.</p>}
              <div className="run-progress-evidence">
                {canonicalProgress
                  ? <CheckpointHistory
                    projectId={projectId}
                    run={selectedRun}
                    progress={canonicalProgress}
                    detail={canonicalDetail}
                    recordedProgress={recordedProgressContent}
                    parentInformationalDisclosuresOpen={technicalOpen && (!savedOverrides || runChangesOpen)}
                    onBulkInformationalDisclosureChange={open => {
                      setTechnicalDisclosure(open)
                      if (savedOverrides) setRunChangesDisclosure(open)
                    }}
                  />
                  : <>
                    {(recordedProgressContent || checkpoints) && <details className="checkpoint-history-disclosure run-legacy-progress-disclosure">
                      <summary>Recorded progress</summary>
                      {recordedProgressContent}
                      <dl className="run-scan-summary">
                        <div><dt>Project</dt><dd className="mono">{projectId}</dd></div>
                        <div><dt>Worker / reviewer</dt><dd>{runActorSummary(lastExecuted)}</dd></div>
                        <div><dt>Elapsed / completion</dt><dd><LiveRunTiming run={selectedRun} /></dd></div>
                      </dl>
                      <dl className="run-progress-strip">
                        {selectedRun.workflow_name && <div><dt>Workflow</dt><dd>{formatMachineLabel(selectedRun.workflow_name)}</dd></div>}
                        <div><dt>Team</dt><dd>{selectedRun.team ? formatMachineLabel(selectedRun.team) : 'Not recorded'}</dd></div>
                        <div><dt>Max turns</dt><dd>{selectedRun.max_turns ?? 'Not reported'}</dd></div>
                        {lastExecuted && <div><dt>Last executed</dt><dd>{lastExecuted.turnNumber !== null ? `turn ${lastExecuted.turnNumber}` : 'Not reported'}</dd></div>}
                        {startTime ? <div><dt>Started</dt><dd>{timestamp(startTime)}<LiveRunElapsed run={selectedRun} label={selectedRunIsActive ? 'running for' : 'duration'} /></dd></div>
                          : selectedRun.evidence.manifest_created_at ? <div><dt>Submitted</dt><dd>{timestamp(selectedRun.evidence.manifest_created_at)}</dd></div> : null}
                        {selectedRun.ended_at && <div><dt>Ended</dt><dd>{timestamp(selectedRun.ended_at)}</dd></div>}
                      </dl>
                    </details>}
                  </>}
              </div>
              {canCreateFollowup && <section className="dashboard-section followup-draft-action" aria-label="Create follow-up draft">
                <div className="section-heading">
                  <div>
                    <h4>Create follow-up draft</h4>
                    <span className="text-xs text-dim">Editable planning work from this run's bounded evidence</span>
                  </div>
                </div>
                <p>
                  Proposed filename: <span className="mono">{followupName}</span>. Creating this draft does not change the source run or start a workflow; it opens the editable plan in Plans.
                </p>
                <label className="dashboard-field">
                  <span>Draft filename</span>
                  <input
                    className="input mono"
                    aria-label="Follow-up draft filename"
                    value={followupName}
                    disabled={busyAction !== null}
                    onChange={(event) => setFollowupDraft({ runId: selectedRun.run_id, name: event.target.value })}
                  />
                </label>
                {followupError && <div className="error-message" role="alert">{followupError}</div>}
                <div className="dashboard-actions">
                  <button className="btn btn-primary" disabled={busyAction !== null} onClick={() => void createFollowupDraft()}>
                    {busyAction === 'followup' ? 'Creating draft…' : 'Create follow-up draft'}
                  </button>
                </div>
              </section>}
              {canRecover && <section className="dashboard-section recovery-action" aria-label="Durable provider recovery">
                <div className="section-heading">
                  <div>
                    <h4>Recover with another worker</h4>
                    <span className="text-xs text-dim">Secondary action for a confirmed inactive source</span>
                  </div>
                  {recoveryOpen && <span className="status-pill">Explicit durable evidence</span>}
                </div>
                <p>Use this when the original provider session is unavailable. The replacement uses the saved plan, worktree, and durable results; private context from the old provider session is unavailable.</p>
                {!recoveryOpen
                  ? <button className="btn btn-secondary" disabled={busyAction !== null || restartInProgress} onClick={() => setRecoveryOpen(true)}>Recover with another worker…</button>
                  : <>
                    <div className="dashboard-form-grid">
                      <Combobox
                        label="Recovery worker"
                        visibleLabel="Replacement worker"
                        value={recoverySelector}
                        onChange={setRecoverySelector}
                        options={recoveryWorkerOptions}
                        optionLabel={(selector) => formatMachineChoice(selector, recoveryWorkerOptions)}
                        optionBadges={Object.fromEntries(recoveryWorkerOptions.map((selector) => [selector, 'configured current setting']))}
                        placeholder="Choose a configured replacement worker"
                        disabled={busyAction === 'recovery' || restartInProgress}
                      />
                    </div>
                    <p className="text-xs text-dim">Submission mode: <span className="mono">durable_evidence</span>. This creates a distinct successor and never retries the source provider session.</p>
                    <div className="dashboard-actions">
                      <button className="btn btn-secondary" disabled={!recoverySelector.trim() || busyAction === 'recovery' || restartInProgress} onClick={() => void handleRecovery()}>{busyAction === 'recovery' ? 'Recovering…' : 'Recover with selected worker'}</button>
                      <button className="btn btn-secondary" disabled={busyAction === 'recovery'} onClick={() => setRecoveryOpen(false)}>Cancel</button>
                    </div>
                  </>}
              </section>}
              {recoveryProvenance && <section className="dashboard-section recovery-provenance" id="recovery-evidence">
                <div className="section-heading">
                  <h4>{recoveryWorkerSessionStarted
                    ? 'Replacement worker started'
                    : recoveryReplacementStarted
                      ? 'Replacement worker operation started'
                      : 'Recovery requested'}</h4>
                  <span className="status-pill">{recoveryProvenance.mode}</span>
                </div>
                <p>{recoveryWorkerSessionStarted
                  ? `A fresh replacement provider session is recorded for the selected worker${recoveryWorkerEvidence?.session_status ? ` (session state: ${recoveryWorkerEvidence.session_status}).` : '.'}`
                  : recoveryReplacementStarted
                    ? 'The replacement worker operation started, but no provider session identity was recorded.'
                    : 'The recovery intent is recorded, but a replacement start has not been confirmed yet.'}</p>
                <dl className="run-metadata">
                  <div><dt>Source run</dt><dd className="mono">{recoveryProvenance.sourceRunId}</dd></div>
                  <div><dt>Source worker</dt><dd className="mono">{recoveryProvenance.sourceSelector}</dd></div>
                  <div><dt>Replacement run</dt><dd className="mono">{recoveryProvenance.targetRunId}</dd></div>
                  <div><dt>Replacement worker</dt><dd className="mono">{recoveryProvenance.targetSelector}</dd></div>
                </dl>
                <p>
                  Recovery evidence: <a href={`#${technicalId}`} onClick={() => setTechnicalDisclosure(true)}>open the recorded event and artifact details in Technical details</a>
                  {recoveryProvenance.artifactPath && <> · <span className="mono">{recoveryProvenance.artifactPath}</span></>}
                </p>
                {recoveryProvenance.evidenceReferences.length > 0 && <p className="text-xs text-dim">Saved evidence references: {recoveryProvenance.evidenceReferences.join(' · ')}</p>}
                {recoveryProvenance.sourceSessionContextTransferred === false && <p className="notice">Private context from the old provider session was unavailable and was not transferred.</p>}
              </section>}

              {savedOverrides && <details
                className="dashboard-section run-settings-changes-disclosure"
                open={runChangesOpen}
                onToggle={event => setRunChangesDisclosure(event.currentTarget.open)}
              ><summary>Run settings &amp; changes</summary>
                {savedOverrides && <div>
                  <p>{savedOverrides.state === 'applied' ? 'Applied' : savedOverrides.state === 'rejected' ? 'Rejected' : 'Pending'} changes · revision {savedOverrides.revision}{savedOverrides.state === 'pending' ? ' · applies at the next turn or on resume' : ''}</p>
                  {savedOverrides.max_turns && <p>Max turns: {savedOverrides.max_turns}</p>}
                  {savedOverrides.team && <p>Team: {formatMachineLabel(savedOverrides.team)}</p>}
                  {Object.entries(savedOverrides.role_selectors ?? {}).map(([role, selector]) => {
                    const modelEffort = selectorModelEffortText(selector, committedForm)
                    return <p key={role}>{formatMachineLabel(role)}: {selector}{modelEffort ? <> · {modelEffort}</> : null}</p>
                  })}
                  {savedOverrides.state === 'pending' && <p>Saved controls apply at the next turn or when the run resumes.</p>}
                </div>}
              </details>}

              <section className="dashboard-section run-technical-section">
                <h4>
                  <button
                    type="button"
                    className="disclosure-toggle"
                    aria-label="Diagnostics"
                    aria-expanded={technicalOpen}
                    aria-controls={technicalId}
                    onClick={() => setTechnicalDisclosure(!technicalOpen)}
                  >
                    Technical details
                  </button>
                </h4>
                {technicalOpen && (
                  <div id={technicalId} className="run-technical-details">
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
                      <p>Canonical status: {selectedRun.status}{selectedRun.status_reason_code ? ` · ${selectedRun.status_reason_code}` : ''}</p>
                      <p>Observed state: {statusLabel(selectedRun)}. Worker: {selectedRun.evidence.unit_active === true ? 'active' : selectedRun.evidence.unit_active === false ? 'inactive' : 'activity unconfirmed'}.</p>
                      {selectedRun.worker_exit && <>
                        <p>{selectedRun.worker_exit.stage === 'wrapper_spawn' ? 'Worker could not be spawned' : !selectedRun.evidence.has_run_metadata ? 'Worker exited during startup' : 'Worker exited'}{selectedRun.worker_exit.exit_code !== null ? ` (code ${selectedRun.worker_exit.exit_code})` : ''}</p>
                        <p>Failure stage: {selectedRun.worker_exit.stage}</p>
                        {selectedRun.worker_exit.reason && <p>{conciseRunText(selectedRun.worker_exit.reason)}</p>}
                        {selectedRun.worker_exit.exited_at && <p>Worker exit: {timestamp(selectedRun.worker_exit.exited_at)}</p>}
                        {selectedRun.worker_exit.diagnostic_unavailable && <p>Original worker error was not retained.</p>}
                      </>}
                      {!selectedRun.worker_exit && selectedRun.reason && <p>{conciseRunText(selectedRun.reason)}</p>}
                      <p>{[selectedRun.plan_path, selectedRun.workflow_name ? formatMachineLabel(selectedRun.workflow_name) : null, selectedRun.team ? formatMachineLabel(selectedRun.team) : null, selectedRun.current_step ? formatMachineLabel(selectedRun.current_step) : null].filter(Boolean).join(' · ')}</p>
                      {events.length > 0 && <p>Last event: {formatMachineLabel(events[events.length - 1].event_type)} · {timestamp(events[events.length - 1].timestamp)}</p>}
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
        </SidebarEditorLayout>
      )}
      {projectAvailable && !newRunPage && historyIncomplete && <div className="notice text-sm" role="status">
        {historyPending
          ? `Run history is still loading. ${historyLoadedCount} loaded so far; the history is incomplete.`
          : `Run history could not be loaded. ${historyLoadedCount} previously loaded; the history is incomplete. Use Refresh to retry.`}
      </div>}

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
        startTeamFamily={startTeamFamily}
        setStartTeamFamily={(familyId) => setStartTeam(familyId)}
        teamFamilyOptions={teamFamilyOptions}
        teamFamilyBadges={Object.fromEntries(launchTeamGroups.map((group) => [
          group.rootId,
          committedForm?.teams[group.rootId] ? 'configured' : 'available on this server',
        ]))}
        teamFamilyOptionLabel={teamFamilyOptionLabel}
        teamFamilyOptionHint={teamFamilyOptionHint}
        teamFamilyPresentation={{
          resolvedDisplay: teamFamilyResolvedDisplay,
          resolvedBadge: teamFamilyResolvedBadge,
          defaultLabel: teamFamilyDefaultOption.label,
          defaultHint: teamFamilyDefaultOption.hint,
        }}
        startTeamStage={startTeamStage}
        setStartTeamStage={setStartTeam}
        teamStageOptions={teamStageOptions}
        teamStageBadges={teamStageBadges}
        teamStageOptionLabel={teamStageOptionLabel}
        teamStageOptionHint={(teamId) => launchTeamRoleSummary(committedForm, teamId)}
        teamStagePresentation={{
          resolvedDisplay: teamStageResolvedDisplay,
          resolvedBadge: teamStageResolvedBadge,
          defaultLabel: teamStageDefaultOption.label,
          defaultHint: teamStageDefaultOption.hint,
        }}
        startMaxTurns={startMaxTurns}
        setStartMaxTurns={setStartMaxTurns}
        startMaxTurnsProblem={startMaxTurnsProblem}
        configuredMaxTurns={configuredMaxTurns}
        serverDefaultMaxTurns={serverDefaultMaxTurns}
        preview={launchPreview}
        worktreePreflight={worktreePreflightPanel}
        restartActions={restartActions}
        onCancel={cancelNewRun}
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
        reviewOpen={startReviewIdentity !== null}
        review={launchReview}
        onOpenStartReview={openStartReview}
        onCancelStartReview={cancelStartReview}
        onConfirmStart={confirmStart}
        startActionLabel="Review start…"
        reviewReady={startReviewReady}
        startDisabled={startDisabled}
        startRevalidationPending={startRevalidationPendingForRender}
        busyAction={busyAction}
        hideActions={hosted}
      />}

    </div>
  )
}
