import { useEffect, useMemo, useState, type ReactNode } from 'react'
import type {
  RunProgressChange,
  RunProgressCount,
  RunProgressDeliveryStage,
  RunProgressDetail,
  RunProgressDetailCheckpoint,
  RunProgressDetailEvent,
  RunProgressExecutor,
  RunProgressEventAssociation,
  RunProgressSummary,
  RunStatus,
} from '../types'
import { formatMachineLabel } from '../label'
import {
  checkpointApprovalText,
  executionDuration,
  formatLocalTimestamp,
  isRunActive,
  isTerminalInactiveRun,
  progressHistoryNotice,
  runFinishText,
  runFinishTimestamp,
  runTurnBudgetText,
} from '../runPresentation'
import { SidebarEditorLayout } from './SidebarEditorLayout'

interface CheckpointHistoryProps {
  projectId: string
  run: RunStatus
  progress: RunProgressSummary | null
  detail: RunProgressDetail | null
  recordedProgress?: ReactNode
  parentInformationalDisclosuresOpen?: boolean
  onBulkInformationalDisclosureChange?: (open: boolean) => void
}

type HistoryDisclosureKey = 'checkpoints' | 'changes' | 'delivery' | 'time' | 'evidence'

interface HistoryDisclosureState {
  open: Record<HistoryDisclosureKey, boolean>
  openEventIds: Set<string>
}

function initialHistoryDisclosureState(): HistoryDisclosureState {
  return {
    open: {
      checkpoints: false,
      changes: false,
      delivery: false,
      time: false,
      evidence: false,
    },
    openEventIds: new Set(),
  }
}

interface HistoryEntry {
  key: string
  checkpoint: RunProgressDetailCheckpoint | null
  events: RunProgressDetailEvent[]
  title: string
  synthetic: boolean
  association: RunProgressEventAssociation
}

export interface CheckpointSelectionState {
  runKey: string | null
  key: string | null
  notice: string | null
}

interface CheckpointSelectionReconciliation {
  runKey: string
  preferredKey: string | null
  availableKeys: ReadonlySet<string>
}

export function reconcileCheckpointSelection(
  current: CheckpointSelectionState,
  next: CheckpointSelectionReconciliation,
): CheckpointSelectionState {
  if (current.runKey !== next.runKey) {
    return { runKey: next.runKey, key: next.preferredKey, notice: null }
  }

  if (current.key !== null && next.availableKeys.has(current.key)) return current
  if (current.key === null && next.preferredKey === null) return current

  const notice = current.key !== null
    ? next.preferredKey
      ? 'The previously selected checkpoint is no longer in the refreshed bounded history; showing the nearest available evidence.'
      : 'The previously selected checkpoint is no longer in the refreshed bounded history; no replacement evidence was returned.'
    : null
  if (current.key === next.preferredKey && current.notice === notice) return current
  return { runKey: next.runKey, key: next.preferredKey, notice }
}

const ACTIVE_CHECKPOINT_STATUSES = new Set(['implementing', 'reviewing', 'repairing'])

function trimmed(value: string | null | undefined): string | null {
  const text = value?.trim()
  return text ? text : null
}

function countValue(count: RunProgressCount | null | undefined): number | null {
  return count?.coverage !== 'unavailable'
    && typeof count?.value === 'number'
    && Number.isSafeInteger(count.value)
    && count.value >= 0
    ? count.value
    : null
}

function countText(count: RunProgressCount | null | undefined, singular: string, plural: string): string {
  const value = countValue(count)
  if (value === null) return '-'
  return `${count?.coverage === 'partial' ? 'At least ' : ''}${value} ${value === 1 ? singular : plural}`
}

function countShort(count: RunProgressCount | null | undefined): string {
  const value = countValue(count)
  if (value === null) return '-'
  return `${count?.coverage === 'partial' ? '≥' : ''}${value}`
}

function availabilityText(availability: RunProgressSummary['availability']): string {
  switch (availability) {
    case 'complete': return 'Complete evidence'
    case 'partial': return 'Partial evidence'
    case 'not_applicable': return 'Not a checkpoint workflow'
    case 'unavailable': return 'Evidence unavailable'
  }
}

function checkpointStatusText(checkpoint: RunProgressDetailCheckpoint): string {
  return checkpoint.awaiting_review ? 'Awaiting review' : formatMachineLabel(checkpoint.status)
}

function checkpointTitle(checkpoint: RunProgressDetailCheckpoint): string {
  return trimmed(checkpoint.title)
    ?? (checkpoint.ordinal !== null ? `Checkpoint ${checkpoint.ordinal}` : 'Checkpoint identity not reported')
}

function checkpointKey(checkpoint: RunProgressDetailCheckpoint, index: number): string {
  if (checkpoint.checkpoint_id) return `checkpoint:${checkpoint.checkpoint_id}`
  const fallback = [
    checkpoint.scope_id,
    checkpoint.generation_id,
    checkpoint.parent_checkpoint_id,
    checkpoint.source_run_id,
    checkpoint.ordinal,
    checkpoint.title,
  ].map(value => value ?? '').join('|')
  return `checkpoint:unidentified:${fallback || index}`
}

function formatDuration(seconds: number | null | undefined): string | null {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds) || seconds < 0) return null
  const whole = Math.floor(seconds)
  if (whole < 60) return `${whole}s`
  if (whole < 3600) return `${Math.floor(whole / 60)}m ${whole % 60}s`
  return `${Math.floor(whole / 3600)}h ${Math.floor((whole % 3600) / 60)}m`
}

function formatTimestamp(value: string | null | undefined): string | null {
  return formatLocalTimestamp(value)
}

type TimingRole = 'worker' | 'reviewer'

interface InvocationTiming {
  key: string
  role: TimingRole
  executor: RunProgressExecutor
  sourceRunId: string | null
  turnNumber: number | null
  durationSeconds: number | null
  startedAt: string | null
  endedAt: string | null
  startedAtMs: number | null
  endedAtMs: number | null
}

interface TimeBreakdown {
  groups: Record<TimingRole, InvocationTiming[]>
  runElapsedSeconds: number | null
  intervalCoverageSeconds: number | null
  runScopedRecords: InvocationTiming[]
  incompleteRunScopedRecords: number
  unidentifiableRecords: number
  unscopedRecords: number
  hasDurationOnlyRecords: boolean
  canComputeRemainder: boolean
  unattributedSeconds: number | null
}

const INVOCATION_EVENT_KINDS = new Set([
  'worker_attempt',
  'repair_attempt',
  'review',
  'review_rejection',
  'checkpoint_approval',
])

function positiveDuration(value: number | null | undefined): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null
}

function timestampMillis(value: string | null | undefined): number | null {
  if (!value || !Number.isFinite(Date.parse(value))) return null
  return Date.parse(value)
}

function firstTimestamp(...values: Array<string | null | undefined>): { text: string | null; millis: number | null } {
  for (const value of values) {
    const text = trimmed(value)
    const millis = timestampMillis(text)
    if (text && millis !== null) return { text, millis }
  }
  return { text: null, millis: null }
}

function timingRole(executor: RunProgressExecutor): TimingRole | null {
  const role = trimmed(executor.role)?.toLowerCase()
  return role === 'worker' || role === 'reviewer' ? role : null
}

function timingSourceRunId(event: RunProgressDetailEvent, executor: RunProgressExecutor): string | null {
  return trimmed(executor.source_run_id)
    ?? trimmed(event.source_run_id)
    ?? referenceValue(event.source_reference, 'run_id', 'source_run_id')
}

function timingIdentity(
  event: RunProgressDetailEvent,
  executor: RunProgressExecutor,
  role: TimingRole,
  sourceRunId: string | null,
): string | null {
  const invocationId = trimmed(executor.invocation_id)
  if (invocationId) return `${sourceRunId ?? 'unknown'}|${role}|invocation|${invocationId}`
  const turnNumber = executor.turn_number ?? event.turn_number
  if (sourceRunId && turnNumber !== null && Number.isSafeInteger(turnNumber) && turnNumber > 0) {
    return `${sourceRunId}|${role}|turn|${turnNumber}`
  }
  return null
}

function mergeBoundary(
  currentText: string | null,
  currentMillis: number | null,
  nextText: string | null,
  nextMillis: number | null,
  chooseEarlier: boolean,
): { text: string | null; millis: number | null } {
  if (currentMillis === null) return { text: nextText, millis: nextMillis }
  if (nextMillis === null) return { text: currentText, millis: currentMillis }
  const useNext = chooseEarlier ? nextMillis < currentMillis : nextMillis > currentMillis
  return useNext ? { text: nextText, millis: nextMillis } : { text: currentText, millis: currentMillis }
}

function mergeInvocationTiming(current: InvocationTiming, next: InvocationTiming): InvocationTiming {
  const started = mergeBoundary(current.startedAt, current.startedAtMs, next.startedAt, next.startedAtMs, true)
  const ended = mergeBoundary(current.endedAt, current.endedAtMs, next.endedAt, next.endedAtMs, false)
  return {
    ...current,
    executor: current.executor,
    sourceRunId: current.sourceRunId ?? next.sourceRunId,
    turnNumber: current.turnNumber ?? next.turnNumber,
    durationSeconds: current.durationSeconds ?? next.durationSeconds,
    startedAt: started.text,
    endedAt: ended.text,
    startedAtMs: started.millis,
    endedAtMs: ended.millis,
  }
}

function intervalSeconds(timing: InvocationTiming): number | null {
  if (timing.startedAtMs === null || timing.endedAtMs === null || timing.endedAtMs < timing.startedAtMs) return null
  return (timing.endedAtMs - timing.startedAtMs) / 1000
}

function unionIntervalSeconds(records: InvocationTiming[]): number | null {
  const intervals = records
    .map(record => record.startedAtMs !== null && record.endedAtMs !== null && record.endedAtMs >= record.startedAtMs
      ? [record.startedAtMs, record.endedAtMs] as const
      : null)
    .filter((value): value is readonly [number, number] => value !== null)
    .sort((left, right) => left[0] - right[0] || left[1] - right[1])
  if (intervals.length === 0) return null
  let total = 0
  let currentStart = intervals[0][0]
  let currentEnd = intervals[0][1]
  for (const [start, end] of intervals.slice(1)) {
    if (start <= currentEnd) {
      currentEnd = Math.max(currentEnd, end)
      continue
    }
    total += currentEnd - currentStart
    currentStart = start
    currentEnd = end
  }
  return (total + currentEnd - currentStart) / 1000
}

function runElapsedSeconds(run: RunStatus, now = Date.now()): number | null {
  const start = timestampMillis(run.started_at)
  const finish = timestampMillis(runFinishTimestamp(run))
  const end = finish ?? (isRunActive(run) ? now : null)
  if (start === null || end === null || end < start) return null
  return (end - start) / 1000
}

function timingEvidenceIsComplete(detail: RunProgressDetail): boolean {
  const truncation = detail.truncation
  return detail.availability === 'complete'
    && (truncation?.omitted_records ?? 0) === 0
    && (truncation?.omitted_checkpoints ?? 0) === 0
    && (truncation?.response_limit_records ?? 0) === 0
    && (truncation?.response_limit_checkpoints ?? 0) === 0
    && (truncation?.notices?.length ?? 0) === 0
}

function buildTimeBreakdown(run: RunStatus, detail: RunProgressDetail): TimeBreakdown {
  const byIdentity = new Map<string, InvocationTiming>()
  let unidentifiableRecords = 0
  for (const event of detail.events) {
    if (!INVOCATION_EVENT_KINDS.has(event.kind) || !event.executor) continue
    const role = timingRole(event.executor)
    if (!role) continue
    const sourceRunId = timingSourceRunId(event, event.executor)
    const key = timingIdentity(event, event.executor, role, sourceRunId)
    if (!key) {
      unidentifiableRecords += 1
      continue
    }
    const started = firstTimestamp(event.started_at, event.executor.started_at)
    const ended = firstTimestamp(event.ended_at, event.executor.ended_at)
    const timing: InvocationTiming = {
      key,
      role,
      executor: event.executor,
      sourceRunId,
      turnNumber: event.executor.turn_number ?? event.turn_number,
      durationSeconds: positiveDuration(event.duration_seconds) ?? positiveDuration(event.executor.duration_seconds),
      startedAt: started.text,
      endedAt: ended.text,
      startedAtMs: started.millis,
      endedAtMs: ended.millis,
    }
    const existing = byIdentity.get(key)
    byIdentity.set(key, existing ? mergeInvocationTiming(existing, timing) : timing)
  }

  const records = [...byIdentity.values()]
  const groups: Record<TimingRole, InvocationTiming[]> = { worker: [], reviewer: [] }
  records.forEach(record => groups[record.role].push(record))
  const runScopedRecords = records.filter(record => record.sourceRunId === run.run_id)
  const intervalCoverageSeconds = unionIntervalSeconds(runScopedRecords)
  const incompleteRunScopedRecords = runScopedRecords.filter(record => intervalSeconds(record) === null).length
  const unscopedRecords = records.filter(record => record.sourceRunId === null).length
  const hasDurationOnlyRecords = records.some(record => record.durationSeconds !== null && intervalSeconds(record) === null)
  const runElapsed = runElapsedSeconds(run)
  const completeCoverage = timingEvidenceIsComplete(detail)
    && unidentifiableRecords === 0
    && unscopedRecords === 0
    && runScopedRecords.length > 0
    && incompleteRunScopedRecords === 0
  const canComputeRemainder = completeCoverage
    && runElapsed !== null
    && intervalCoverageSeconds !== null
    && intervalCoverageSeconds <= runElapsed
  return {
    groups,
    runElapsedSeconds: runElapsed,
    intervalCoverageSeconds,
    runScopedRecords,
    incompleteRunScopedRecords,
    unidentifiableRecords,
    unscopedRecords,
    hasDurationOnlyRecords,
    canComputeRemainder,
    unattributedSeconds: canComputeRemainder && runElapsed !== null && intervalCoverageSeconds !== null
      ? runElapsed - intervalCoverageSeconds
      : null,
  }
}

function relativeAge(timestamp: string | null, now = Date.now()): string | null {
  if (!timestamp) return null
  const value = Date.parse(timestamp)
  if (!Number.isFinite(value)) return null
  const seconds = Math.max(0, Math.floor((now - value) / 1000))
  if (seconds < 60) return 'just now'
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`
  return `${Math.floor(seconds / 86400)}d ago`
}

function executorParts(executor: RunProgressExecutor | null): string[] {
  if (!executor) return []
  const model = trimmed(executor.model_display) || trimmed(executor.model)
  const parts = [
    executor.role ? formatMachineLabel(executor.role) : null,
    trimmed(executor.team),
    trimmed(executor.selector),
    model,
    trimmed(executor.harness),
    executor.effort ? `effort ${executor.effort}` : null,
  ]
  return [...new Set(parts.filter((value): value is string => Boolean(value)))]
}

function executorText(executor: RunProgressExecutor | null, currentRunId?: string): string | null {
  if (!executor) return null
  const parts = executorParts(executor)
  if (executor.source_run_id && executor.source_run_id !== currentRunId) parts.push(`source run ${executor.source_run_id}`)
  if (executor.invocation_id) parts.push(`invocation ${executor.invocation_id}`)
  if (executor.turn_number !== null) parts.push(`turn ${executor.turn_number}`)
  return parts.length > 0 ? parts.join(' · ') : null
}

function positionText(progress: RunProgressSummary): string {
  const phase = trimmed(progress.phase) || trimmed(progress.activity) || trimmed(progress.run_status)
  const ordinal = progress.current_checkpoint_ordinal
  const total = countValue(progress.total_checkpoints)
  const title = trimmed(progress.current_checkpoint_title)
  if (ordinal !== null && Number.isSafeInteger(ordinal) && ordinal > 0) {
    const position = `CP${ordinal}${total !== null ? ` of ${total}` : ''}`
    return `${position}${phase ? ` · ${formatMachineLabel(phase)}` : ''}${title ? ` — ${title}` : ''}`
  }
  return phase ? `${formatMachineLabel(phase)} · current checkpoint not reported` : 'Current checkpoint not reported'
}

function eventKindText(kind: string): string {
  return kind ? formatMachineLabel(kind) : 'History event'
}

function eventSourceText(event: RunProgressDetailEvent, runId: string): string | null {
  const sourceRun = trimmed(event.source_run_id) ?? trimmed(event.executor?.source_run_id)
  if (sourceRun && sourceRun !== runId) return `Inherited from run ${sourceRun}`
  return null
}

function eventAssociation(event: RunProgressDetailEvent): RunProgressEventAssociation {
  if (
    event.association === 'checkpoint'
    || event.association === 'whole_plan'
    || event.association === 'outside_returned'
    || event.association === 'unassigned'
  ) return event.association
  return event.checkpoint_id ? 'checkpoint' : 'unassigned'
}

function syntheticHistoryTitle(association: RunProgressEventAssociation): string {
  switch (association) {
    case 'whole_plan': return 'Whole-plan review history'
    case 'outside_returned': return 'History outside returned checkpoints'
    case 'unassigned': return 'Unassigned history'
    case 'checkpoint': return 'Checkpoint history'
  }
}

function syntheticHistoryStatus(association: RunProgressEventAssociation): string {
  switch (association) {
    case 'whole_plan': return 'Whole-plan review'
    case 'outside_returned': return 'Outside returned page'
    case 'unassigned': return 'Unassigned'
    case 'checkpoint': return 'Checkpoint history'
  }
}

function syntheticHistoryDescription(association: RunProgressEventAssociation, count: number): string {
  const events = `${count} event${count === 1 ? '' : 's'}`
  switch (association) {
    case 'whole_plan': return `${events} are run-level reviews; they do not prove checkpoint approval`
    case 'outside_returned': return `${events} reference checkpoints outside this returned page`
    case 'unassigned': return `${events} have no proven checkpoint association`
    case 'checkpoint': return events
  }
}

function referenceValue(reference: Record<string, unknown> | null, ...keys: string[]): string | null {
  if (!reference) return null
  for (const key of keys) {
    const value = reference[key]
    if (typeof value === 'string' && value.trim()) return value.trim()
    if (typeof value === 'number' && Number.isSafeInteger(value)) return String(value)
  }
  return null
}

function eventRetryReference(event: RunProgressDetailEvent): string | null {
  if (event.kind !== 'runtime_retry') return null
  return referenceValue(event.source_reference, 'retry_of', 'parent_event_id', 'attempt_event_id', 'event_id')
}

function retryCompletionTurn(event: RunProgressDetailEvent): number | null {
  if (event.kind !== 'runtime_retry') return null
  const completionTurn = event.source_reference?.completion_turn_number
  return typeof completionTurn === 'number' && Number.isSafeInteger(completionTurn) && completionTurn > 0
    ? completionTurn
    : null
}

const RETRIED_INVOCATION_KINDS = new Set(['worker_attempt', 'repair_attempt', 'review', 'review_rejection'])

function retryCompletionTarget(
  event: RunProgressDetailEvent,
  events: RunProgressDetailEvent[],
  completionTurn: number,
): RunProgressDetailEvent | null {
  const role = event.executor?.role
  if (role !== 'worker' && role !== 'reviewer' || event.source_run_id === null) return null
  const matches = events.filter(candidate => (
    RETRIED_INVOCATION_KINDS.has(candidate.kind)
    && candidate.source_run_id === event.source_run_id
    && candidate.turn_number === completionTurn
    && candidate.executor?.role === role
  ))
  return matches.length === 1 ? matches[0] : null
}

function changeValue(value: string | null): string {
  return trimmed(value) ?? '-'
}

function changeTransition(oldValue: string | null, newValue: string | null): string {
  if (!oldValue && !newValue) return '-'
  return `${changeValue(oldValue)} → ${changeValue(newValue)}`
}

function sourceReferenceText(reference: Record<string, unknown> | null): string | null {
  if (!reference) return null
  const run = referenceValue(reference, 'run_id', 'source_run_id')
  const artifact = referenceValue(reference, 'artifact', 'relative_path')
  const turn = referenceValue(reference, 'turn_number')
  const decision = referenceValue(reference, 'decision_number')
  const parts = [
    run ? `run ${run}` : null,
    artifact ? `evidence ${artifact}` : null,
    turn ? `turn ${turn}` : null,
    decision ? `decision ${decision}` : null,
  ].filter((value): value is string => Boolean(value))
  return parts.length > 0 ? parts.join(' · ') : null
}

function eventMatchesCheckpoint(event: RunProgressDetailEvent, checkpoint: RunProgressDetailCheckpoint): boolean {
  return checkpoint.checkpoint_id !== null && event.checkpoint_id === checkpoint.checkpoint_id
}

function sortCheckpoints(checkpoints: RunProgressDetailCheckpoint[]): RunProgressDetailCheckpoint[] {
  return checkpoints
    .map((checkpoint, index) => ({ checkpoint, index }))
    .sort((left, right) => {
      const leftOrdinal = left.checkpoint.ordinal ?? Number.MAX_SAFE_INTEGER
      const rightOrdinal = right.checkpoint.ordinal ?? Number.MAX_SAFE_INTEGER
      return leftOrdinal - rightOrdinal || left.index - right.index
    })
    .map(item => item.checkpoint)
}

function uniqueEvents(events: RunProgressDetailEvent[]): RunProgressDetailEvent[] {
  const byId = new Map<string, { event: RunProgressDetailEvent; index: number }>()
  events.forEach((event, index) => {
    if (!byId.has(event.event_id)) byId.set(event.event_id, { event, index })
  })
  return [...byId.values()]
    .sort((left, right) => {
      const leftTime = Date.parse(left.event.started_at ?? left.event.ended_at ?? '')
      const rightTime = Date.parse(right.event.started_at ?? right.event.ended_at ?? '')
      const normalizedLeftTime = Number.isFinite(leftTime) ? leftTime : Number.MAX_SAFE_INTEGER
      const normalizedRightTime = Number.isFinite(rightTime) ? rightTime : Number.MAX_SAFE_INTEGER
      return normalizedLeftTime - normalizedRightTime
        || (left.event.turn_number ?? Number.MAX_SAFE_INTEGER) - (right.event.turn_number ?? Number.MAX_SAFE_INTEGER)
        || left.index - right.index
    })
    .map(item => item.event)
}

function CheckpointRow({ entry, selected, onSelect }: { entry: HistoryEntry; selected: boolean; onSelect: (key: string) => void }): JSX.Element {
  const checkpoint = entry.checkpoint
  return <button
    type="button"
    className={`sidebar-entry checkpoint-history-entry${selected ? ' selected' : ''}`}
    data-sidebar-editor-item={entry.key}
    aria-pressed={selected}
    aria-current={selected ? 'true' : undefined}
    onClick={() => onSelect(entry.key)}
  >
    <span className="checkpoint-history-entry-title">
      <strong>{entry.title}</strong>
      <span className="status-pill">{checkpoint ? checkpointStatusText(checkpoint) : syntheticHistoryStatus(entry.association)}</span>
    </span>
    {checkpoint
      ? <span className="text-xs text-dim">{countShort(checkpoint.worker_attempts)} worker attempts · {countShort(checkpoint.repair_passes)} repairs · {countShort(checkpoint.reviews)} reviews</span>
      : <span className="text-xs text-dim">{syntheticHistoryDescription(entry.association, entry.events.length)}</span>}
  </button>
}

function ExecutorRow({ label, executor, runId }: { label: string; executor: RunProgressExecutor | null; runId?: string }): JSX.Element | null {
  const duration = formatDuration(executor?.duration_seconds)
  const identity = executorText(executor, runId)
  if (!identity) return null
  return <div>
    <dt>{label}</dt>
    <dd>{identity}{duration ? ` · ${duration}` : ''}</dd>
  </div>
}

function eventExecutorSummary(executor: RunProgressExecutor | null): string | null {
  if (!executor) return null
  const model = trimmed(executor.model_display) || trimmed(executor.model)
  const readable = [trimmed(executor.selector), model, trimmed(executor.team)]
    .filter((value): value is string => Boolean(value))
  if (readable.length > 0) return [...new Set(readable)].join(' · ')
  return executor.role ? formatMachineLabel(executor.role) : null
}

function eventRecordedText(event: RunProgressDetailEvent): string | null {
  const started = formatTimestamp(event.started_at)
  const ended = formatTimestamp(event.ended_at)
  const parts = [
    started ? `started ${started}` : null,
    ended ? `ended ${ended}` : null,
  ].filter((value): value is string => Boolean(value))
  return parts.length > 0 ? parts.join(' · ') : null
}

function EventTimeline({
  events,
  runId,
  openEventIds,
  onEventToggle,
}: {
  events: RunProgressDetailEvent[]
  runId: string
  openEventIds: Set<string>
  onEventToggle: (eventId: string, open: boolean) => void
}): JSX.Element {
  if (events.length === 0) return <p className="text-sm text-dim">No attempt or review events are reported for this selection.</p>
  const eventIds = new Set(events.map(event => event.event_id))
  return <ol className="checkpoint-history-timeline">
    {events.map((event) => {
      const retryReference = eventRetryReference(event)
      const retryTarget = retryReference && eventIds.has(retryReference) ? retryReference : null
      const completionTurn = retryReference ? null : retryCompletionTurn(event)
      const completionTarget = completionTurn === null ? null : retryCompletionTarget(event, events, completionTurn)
      const source = eventSourceText(event, runId)
      const duration = formatDuration(event.duration_seconds) ?? formatDuration(event.executor?.duration_seconds)
      const recordedAt = eventRecordedText({
        ...event,
        started_at: event.started_at ?? event.executor?.started_at ?? null,
        ended_at: event.ended_at ?? event.executor?.ended_at ?? null,
      })
      const sourceReference = sourceReferenceText(event.source_reference)
      const outcome = trimmed(event.outcome) ? formatMachineLabel(event.outcome as string) : null
      const role = event.executor?.role ? formatMachineLabel(event.executor.role) : null
      const executorSummary = eventExecutorSummary(event.executor)
      const executorDetails = executorText(event.executor, runId)
      const provenance = [source, sourceReference].filter((value): value is string => Boolean(value)).join(' · ')
      return <li className="checkpoint-history-event" id={`checkpoint-event-${event.event_id}`} key={event.event_id}>
        <details
          className="checkpoint-history-event-disclosure"
          open={openEventIds.has(event.event_id)}
          onToggle={toggleEvent => {
            onEventToggle(event.event_id, toggleEvent.currentTarget.open)
          }}
        >
          <summary className="checkpoint-history-event-summary">
            {event.turn_number !== null && <span>Turn {event.turn_number}</span>}
            {role && <span>{role}</span>}
            <strong>{eventKindText(event.kind)}</strong>
            {executorSummary && <span className="checkpoint-history-event-executor">{executorSummary}</span>}
            {duration && <span>{duration}</span>}
            {outcome && <span className="status-pill">{outcome}</span>}
          </summary>
          <div className="checkpoint-history-event-body">
            <dl className="checkpoint-history-event-meta">
              <div><dt>Event identity</dt><dd className="mono">{event.event_id}</dd></div>
              {event.checkpoint_id && <div><dt>Checkpoint</dt><dd className="mono">{event.checkpoint_id}</dd></div>}
              {event.scope_id && <div><dt>Scope</dt><dd className="mono">{event.scope_id}</dd></div>}
              {event.turn_number !== null && <div><dt>Turn</dt><dd>{event.turn_number}</dd></div>}
              {event.decision_number !== null && <div><dt>Decision</dt><dd>{event.decision_number}</dd></div>}
              {executorDetails && <div><dt>Executor</dt><dd>{executorDetails}</dd></div>}
              {duration && <div><dt>Duration</dt><dd>{duration}</dd></div>}
              {recordedAt && <div><dt>Recorded</dt><dd>{recordedAt}</dd></div>}
              {provenance && <div><dt>Provenance</dt><dd>{provenance}</dd></div>}
            </dl>
            {trimmed(event.reason) && <p className="text-sm">Reason: {trimmed(event.reason)}</p>}
            {retryTarget
              ? <p className="text-xs text-dim"><a href={`#checkpoint-event-${retryTarget}`}>Retry of recorded event</a></p>
              : completionTurn !== null
                ? completionTarget
                  ? <p className="text-xs text-dim"><a href={`#checkpoint-event-${completionTarget.event_id}`}>Retried invocation: turn {completionTurn}</a></p>
                  : <p className="text-xs text-dim">Retried invocation: turn {completionTurn} (no unique invocation returned in this view).</p>
                : event.kind === 'runtime_retry' && <p className="text-xs text-dim">Retry relationship not established by the evidence.</p>}
          </div>
        </details>
      </li>
    })}
  </ol>
}

function ChangeItem({ change }: { change: RunProgressChange }): JSX.Element {
  const transition = [
    change.old_team || change.new_team ? `team ${changeTransition(change.old_team, change.new_team)}` : null,
    change.old_selector || change.new_selector ? `selector ${changeTransition(change.old_selector, change.new_selector)}` : null,
    change.old_model || change.new_model ? `model ${changeTransition(change.old_model, change.new_model)}` : null,
    change.old_effort || change.new_effort ? `effort ${changeTransition(change.old_effort, change.new_effort)}` : null,
  ].filter((value): value is string => Boolean(value))
  return <li className="checkpoint-history-change">
    <div className="checkpoint-history-event-heading">
      <strong>{formatMachineLabel(change.kind)}</strong>
      <span className="status-pill">{formatMachineLabel(change.status)}</span>
    </div>
    {transition.length > 0 && <p>{transition.join(' · ')}</p>}
    {(change.roles.length > 0 || change.turn_number !== null || change.checkpoint_id || change.generation_id) && <p className="text-xs text-dim">{change.roles.length > 0 ? `Roles: ${change.roles.map(formatMachineLabel).join(', ')}` : null}{change.turn_number !== null ? ` · turn ${change.turn_number}` : ''}{change.checkpoint_id ? ` · ${change.checkpoint_id}` : ''}{change.generation_id ? ` · generation ${change.generation_id}` : ''}</p>}
    {change.reason && <p className="text-xs text-dim">Reason: {change.reason}</p>}
    {change.recorded_at && <p className="text-xs text-dim">Recorded: {formatTimestamp(change.recorded_at)}</p>}
  </li>
}

function ChangeDisclosure({ title, changes, emptyText }: { title: string; changes: RunProgressChange[]; emptyText: string }): JSX.Element {
  return <section>
    <h6>{title}</h6>
    {changes.length > 0
      ? <ul className="checkpoint-history-change-list">{changes.map(change => <ChangeItem change={change} key={change.change_id} />)}</ul>
      : <p className="text-sm text-dim">{emptyText}</p>}
  </section>
}

const DELIVERY_SUMMARY_STAGES = [
  { key: 'final_review', label: 'Final review' },
  { key: 'merge', label: 'Merge' },
  { key: 'publish', label: 'Publication' },
  { key: 'ci', label: 'CI' },
  { key: 'live', label: 'Live' },
] as const

function normalizeDeliveryStage(value: string): string {
  return value.trim().toLowerCase().replace(/[_-]+/g, ' ').replace(/\s+/g, ' ')
}

function deliveryStageKey(value: string): string | null {
  const normalized = normalizeDeliveryStage(value)
  if (normalized === 'final review') return 'final_review'
  if (normalized === 'merge') return 'merge'
  if (normalized === 'publish' || normalized === 'publication') return 'publish'
  if (normalized === 'ci' || normalized === 'continuous integration') return 'ci'
  if (normalized === 'live' || normalized === 'live verification') return 'live'
  return null
}

function deliveryStageLabel(value: string): string {
  const key = deliveryStageKey(value)
  return DELIVERY_SUMMARY_STAGES.find(stage => stage.key === key)?.label ?? formatMachineLabel(value)
}

function DeliveryDisclosure({ stages }: { stages: RunProgressDeliveryStage[] }): JSX.Element {
  return <section>
    <h6>Delivery stages</h6>
    {stages.length > 0
      ? <ul className="checkpoint-history-delivery-list">{stages.map((stage, index) => {
        const sourceReference = sourceReferenceText(stage.source_reference)
        return <li key={`${stage.stage}-${index}`}>
          <span><strong>{deliveryStageLabel(stage.stage)}</strong> · {stage.status === 'unknown' ? '-' : formatMachineLabel(stage.status)}</span>
          {stage.reason && <span className="text-xs text-dim">{stage.reason}</span>}
          {stage.recorded_at && <span className="text-xs text-dim">{formatTimestamp(stage.recorded_at)}</span>}
          {sourceReference && <span className="text-xs text-dim">Receipt: <span className="mono">{sourceReference}</span></span>}
        </li>
      })}</ul>
      : <p className="text-sm text-dim">Delivery evidence not reported.</p>}
  </section>
}

function invocationLabel(timing: InvocationTiming, runId: string): string {
  const invocationId = trimmed(timing.executor.invocation_id)
  const identity = invocationId
    ? `Invocation ${invocationId}`
    : timing.turnNumber !== null
      ? `Turn ${timing.turnNumber} invocation`
      : 'Invocation'
  const source = timing.sourceRunId !== null && timing.sourceRunId !== runId
    ? ` · source run ${timing.sourceRunId}`
    : ''
  return `${identity}${source}`
}

function timingDuration(timing: InvocationTiming): number | null {
  return timing.durationSeconds ?? intervalSeconds(timing)
}

function TimeDisclosure({
  run,
  detail,
  open,
  onOpenChange,
}: {
  run: RunStatus
  detail: RunProgressDetail
  open: boolean
  onOpenChange: (open: boolean) => void
}): JSX.Element {
  const breakdown = buildTimeBreakdown(run, detail)
  const currentIntervals = breakdown.runScopedRecords.filter(record => intervalSeconds(record) !== null)
  const coverage = breakdown.intervalCoverageSeconds === null
    ? '-'
    : formatDuration(breakdown.intervalCoverageSeconds) ?? '-'
  const partialReasons = [
    detail.availability !== 'complete' ? 'history is partial or unavailable' : null,
    breakdown.incompleteRunScopedRecords > 0
      ? `${breakdown.incompleteRunScopedRecords} current-run invocation${breakdown.incompleteRunScopedRecords === 1 ? '' : 's'} lack complete boundaries`
      : null,
    breakdown.unidentifiableRecords > 0
      ? `${breakdown.unidentifiableRecords} invocation record${breakdown.unidentifiableRecords === 1 ? '' : 's'} lack a proven identity`
      : null,
    breakdown.unscopedRecords > 0
      ? `${breakdown.unscopedRecords} invocation record${breakdown.unscopedRecords === 1 ? '' : 's'} lack a source run`
      : null,
  ].filter((value): value is string => Boolean(value))
  const hasRecords = Object.values(breakdown.groups).some(group => group.length > 0)
  const inheritedRecords = Object.values(breakdown.groups)
    .flat()
    .filter(record => record.sourceRunId !== null && record.sourceRunId !== run.run_id)
  const totalDurations = (records: InvocationTiming[]): number | null => {
    const values = records.map(timingDuration).filter((value): value is number => value !== null)
    return values.length > 0 ? values.reduce((total, value) => total + value, 0) : null
  }
  return <details className="checkpoint-history-disclosure" open={open} onToggle={event => onOpenChange(event.currentTarget.open)}>
    <summary>Time details</summary>
    <dl className="checkpoint-history-time-summary">
      <div><dt>Total run elapsed</dt><dd>{formatDuration(breakdown.runElapsedSeconds) ?? '-'}</dd></div>
      <div><dt>Known invocation coverage</dt><dd>{coverage}{currentIntervals.length > 0 && breakdown.incompleteRunScopedRecords > 0 ? ' · partial' : ''}</dd></div>
      {breakdown.unattributedSeconds !== null && <div><dt>Unattributed time</dt><dd>{formatDuration(breakdown.unattributedSeconds) ?? '-'}</dd></div>}
    </dl>
    {(['worker', 'reviewer'] as const).map(role => {
      const records = breakdown.groups[role]
      const total = totalDurations(records)
      return <section className="checkpoint-history-time-group" key={role}>
        <h6>{formatMachineLabel(role)} invocations</h6>
        {records.length > 0
          ? <>
            <ul className="checkpoint-history-time-list">{records.map(record => <li key={record.key}>
              <span>{[invocationLabel(record, run.run_id), eventExecutorSummary(record.executor)].filter((value): value is string => Boolean(value)).join(' · ')}</span>
              {timingDuration(record) !== null && <span>{formatDuration(timingDuration(record))}</span>}
            </li>)}</ul>
            {formatDuration(total) && <p className="text-xs text-dim">Recorded duration total: {formatDuration(total)}</p>}
          </>
          : <p className="text-sm text-dim">No {role} invocation durations are reported.</p>}
      </section>
    })}
    {!hasRecords && <p className="notice">No worker or reviewer invocation timing evidence was returned.</p>}
    {inheritedRecords.length > 0 && <p className="text-xs text-dim">Inherited invocation records remain listed, but are excluded from the selected run&apos;s elapsed coverage.</p>}
    {partialReasons.length > 0 && <p className="notice">Timing breakdown is partial: {partialReasons.join(' · ')}. Elapsed remainder is not computed from incomplete coverage.</p>}
    {breakdown.hasDurationOnlyRecords && <p className="notice">Individual durations are shown, but records without both boundaries are not subtracted from total elapsed.</p>}
    {breakdown.canComputeRemainder && <p className="text-xs text-dim">Unattributed time is the elapsed remainder after the union of complete, non-overlapping current-run invocation intervals.</p>}
    <p className="text-xs text-dim">Workflow runtime is separate from browser data-load latency; this view does not measure page-loading time.</p>
  </details>
}

function CheckpointDetail({
  entry,
  run,
  runId,
  openEventIds,
  onEventToggle,
}: {
  entry: HistoryEntry | null
  run: RunStatus
  runId: string
  openEventIds: Set<string>
  onEventToggle: (eventId: string, open: boolean) => void
}): JSX.Element {
  if (!entry) return <div className="checkpoint-history-empty-detail"><p className="text-sm text-dim">Select a returned checkpoint to inspect its evidence.</p></div>
  const checkpoint = entry.checkpoint
  return <div className="checkpoint-history-detail">
    {checkpoint && <dl className="checkpoint-history-checkpoint-meta">
      <div><dt>Worker attempts</dt><dd>{countText(checkpoint.worker_attempts, 'worker attempt', 'worker attempts')}</dd></div>
      <div><dt>Repair passes</dt><dd>{countText(checkpoint.repair_passes, 'repair pass', 'repair passes')}</dd></div>
      <div><dt>Reviews</dt><dd>{countText(checkpoint.reviews, 'review', 'reviews')}</dd></div>
      <div><dt>Runtime retries</dt><dd>{countText(checkpoint.runtime_retries, 'runtime retry', 'runtime retries')}</dd></div>
      <div><dt>Applied upgrades</dt><dd>{countText(checkpoint.applied_upgrades, 'upgrade', 'upgrades')}</dd></div>
      {formatDuration(checkpoint.duration_seconds) && <div><dt>Duration</dt><dd>{formatDuration(checkpoint.duration_seconds)}</dd></div>}
      {formatTimestamp(checkpoint.recorded_at) && <div><dt>Recorded</dt><dd>{formatTimestamp(checkpoint.recorded_at)}</dd></div>}
      {checkpoint.source_run_id && <div><dt>Source run</dt><dd className="mono">{checkpoint.source_run_id}</dd></div>}
      {checkpoint.parent_checkpoint_id && <div><dt>Parent checkpoint</dt><dd className="mono">{checkpoint.parent_checkpoint_id}</dd></div>}
      {checkpoint.generation_id && <div><dt>Generation</dt><dd className="mono">{checkpoint.generation_id}</dd></div>}
    </dl>}
    <section className="checkpoint-history-attempts">
      <div className="section-heading"><h6>Attempt and review timeline</h6><span className="text-xs text-dim">{entry.events.length} unique event{entry.events.length === 1 ? '' : 's'}</span></div>
      <EventTimeline events={entry.events} runId={runId} openEventIds={openEventIds} onEventToggle={onEventToggle} />
    </section>
    {entry.synthetic && entry.association === 'whole_plan' && <p className="notice">Whole-plan review history is kept at run level; it does not prove approval for a returned checkpoint.</p>}
    {entry.synthetic && entry.association === 'outside_returned' && <p className="notice">These events reference checkpoints outside the returned page; no placeholder checkpoint was created.</p>}
    {entry.synthetic && entry.association === 'unassigned' && <p className="notice">No stable checkpoint, scope, or generation reference proved an association for these events.</p>}
    {entry.synthetic && entry.association === 'unassigned' && entry.events.some(event => event.source_run_id && event.source_run_id !== runId) && <p className="notice">Some unassigned history is inherited from another run; each event keeps its source-run label.</p>}
    {run.restarted_from_run_id && <p className="text-xs text-dim">This run is a successor of <span className="mono">{run.restarted_from_run_id}</span>; inherited events remain labelled by their source run.</p>}
  </div>
}

function EvidenceDisclosure({
  run,
  progress,
  detail,
  recordedProgress,
  open,
  onOpenChange,
}: {
  run: RunStatus
  progress: RunProgressSummary
  detail: RunProgressDetail | null
  recordedProgress?: ReactNode
  open: boolean
  onOpenChange: (open: boolean) => void
}): JSX.Element {
  const truncation = detail?.truncation
  const responseLimitRecords = truncation?.response_limit_records ?? 0
  const responseLimitCheckpoints = truncation?.response_limit_checkpoints ?? 0
  const responseLimitParts = [
    responseLimitRecords > 0 ? `${responseLimitRecords} record${responseLimitRecords === 1 ? '' : 's'}` : null,
    responseLimitCheckpoints > 0 ? `${responseLimitCheckpoints} checkpoint${responseLimitCheckpoints === 1 ? '' : 's'}` : null,
  ].filter((value): value is string => Boolean(value))
  const responseLimitNotice = responseLimitParts.length > 0
    ? `History omitted by the response limit: ${responseLimitParts.join(' and ')}.`
    : null
  const notices = truncation?.notices ?? []
  const evidenceAge = relativeAge(progress.evidence_at)
  const evidenceTimestamp = progress.evidence_at ? Date.parse(progress.evidence_at) : NaN
  const evidenceIsStale = Number.isFinite(evidenceTimestamp) && Date.now() - evidenceTimestamp > 15 * 60 * 1000
  const evidenceTimestampInvalid = Boolean(progress.evidence_at) && !Number.isFinite(evidenceTimestamp)
  const terminalInactive = isTerminalInactiveRun(run)
  const currentExecutor = progress.current_executor
  const currentAttempt = currentExecutor
    ? [currentExecutor.turn_number !== null ? `turn ${currentExecutor.turn_number}` : null, formatDuration(currentExecutor.duration_seconds)].filter(Boolean).join(' · ')
    : null
  const activity = trimmed(progress.activity) || trimmed(progress.phase) || trimmed(progress.run_status)
  const finish = runFinishText(run)
  const notice = progressHistoryNotice(progress)
  return <details className="checkpoint-history-disclosure" open={open} onToggle={event => onOpenChange(event.currentTarget.open)}>
    <summary>Count definitions &amp; evidence</summary>
    <dl className="checkpoint-history-at-a-glance">
      <div><dt>Checkpoint result</dt><dd>{checkpointApprovalText(progress)}</dd></div>
      <div><dt>Turns</dt><dd>{runTurnBudgetText(run)}</dd></div>
      <div><dt>Elapsed</dt><dd>{executionDuration(run, Date.now()) ?? '-'}</dd></div>
      {terminalInactive
        ? <div><dt>Finished</dt><dd>{finish ?? '-'}</dd></div>
        : <div><dt>Evidence state</dt><dd>{activity ? formatMachineLabel(activity) : 'Active progress'}</dd></div>}
    </dl>
    {recordedProgress && <section className="checkpoint-history-recorded-progress">
      <h6>Recorded progress</h6>
      {recordedProgress}
    </section>}
    <p>Counts come from the bounded canonical evidence projection. Partial values are lower bounds; unavailable values are not zero.</p>
    {notice && <p className="notice" role="status">{notice}</p>}
    <dl className="checkpoint-history-evidence-meta">
      <div><dt>Availability</dt><dd>{availabilityText(progress.availability)}</dd></div>
      <div><dt>Recorded complete</dt><dd>{countText(progress.recorded_complete_checkpoints, 'checkpoint', 'checkpoints')}</dd></div>
      <div><dt>Worker attempts</dt><dd>{countText(progress.worker_attempts, 'attempt', 'attempts')}</dd></div>
      <div><dt>Repair passes</dt><dd>{countText(progress.repair_passes, 'repair pass', 'repair passes')}</dd></div>
      <div><dt>Reviews</dt><dd>{countText(progress.reviews, 'review', 'reviews')}</dd></div>
      <div><dt>Runtime retries</dt><dd>{countText(progress.runtime_retries, 'runtime retry', 'runtime retries')}</dd></div>
      <div><dt>Applied upgrades</dt><dd>{countText(progress.applied_upgrades, 'upgrade', 'upgrades')}</dd></div>
      <div><dt>Turn budget</dt><dd>{runTurnBudgetText(run)}</dd></div>
      {!terminalInactive && <>
        <div><dt>Checkpoint position</dt><dd>{positionText(progress)}</dd></div>
        {activity && <div><dt>Run activity</dt><dd>{formatMachineLabel(activity)}</dd></div>}
        <ExecutorRow label="Current executor" executor={currentExecutor} runId={run.run_id} />
        {currentExecutor && currentAttempt && <div><dt>Current attempt</dt><dd>{currentAttempt}</dd></div>}
      </>}
      <ExecutorRow label="Last executor" executor={progress.last_executor} runId={run.run_id} />
      {formatTimestamp(progress.observed_at) && <div><dt>Observed</dt><dd>{formatTimestamp(progress.observed_at)}</dd></div>}
      {formatTimestamp(progress.evidence_at) && <div><dt>Latest evidence</dt><dd>{formatTimestamp(progress.evidence_at)}</dd></div>}
      {evidenceAge && <div><dt>Evidence age</dt><dd>{evidenceAge}</dd></div>}
      {progress.original_plan_display_name && <div><dt>Original plan</dt><dd>{progress.original_plan_display_name}</dd></div>}
      {progress.original_plan_identity && <div><dt>Plan identity</dt><dd className="mono">{progress.original_plan_identity}</dd></div>}
      {progress.reason_codes.length > 0 && <div><dt>Reason codes</dt><dd>{progress.reason_codes.join(', ')}</dd></div>}
    </dl>
    {(progress.availability === 'partial' || responseLimitNotice !== null || notices.length > 0) && <div className="notice">
      {notices.length > 0 ? notices.join(' · ') : 'Some earlier progress history is unavailable in this bounded view.'}
      {responseLimitNotice && <span> {responseLimitNotice}</span>}
    </div>}
    {evidenceIsStale && <div className="notice" role="status">Latest evidence is older than 15 minutes; status may be stale. Refresh to request a newer snapshot.</div>}
    {evidenceTimestampInvalid && <div className="notice" role="status">The latest evidence timestamp could not be parsed; freshness is unknown.</div>}
  </details>
}

export function CheckpointHistory({ projectId, run, progress, detail, recordedProgress, parentInformationalDisclosuresOpen = true, onBulkInformationalDisclosureChange }: CheckpointHistoryProps): JSX.Element {
  const summary = progress ?? detail
  const runKey = `${projectId}/${run.run_id}`
  const detailEvents = useMemo(() => uniqueEvents(detail?.events ?? []), [detail?.events])
  const checkpoints = useMemo(() => sortCheckpoints(detail?.checkpoints ?? []), [detail?.checkpoints])
  const entries = useMemo(() => {
    const checkpointIds = new Set(checkpoints.map(checkpoint => checkpoint.checkpoint_id).filter((value): value is string => Boolean(value)))
    const checkpointEntries = checkpoints.map((checkpoint, index): HistoryEntry => ({
      key: checkpointKey(checkpoint, index),
      checkpoint,
      events: detailEvents.filter(event => eventAssociation(event) === 'checkpoint' && eventMatchesCheckpoint(event, checkpoint)),
      title: checkpointTitle(checkpoint),
      synthetic: false,
      association: 'checkpoint',
    }))
    const assignedEventIds = new Set(
      checkpointEntries.flatMap(entry => entry.events.map(event => event.event_id)),
    )
    const syntheticAssociations: RunProgressEventAssociation[] = ['unassigned', 'whole_plan', 'outside_returned']
    for (const association of syntheticAssociations) {
      const groupedEvents = detailEvents.filter(event => {
        if (assignedEventIds.has(event.event_id)) return false
        const kind = eventAssociation(event)
        if (kind === association) return true
        return kind === 'checkpoint' && (!event.checkpoint_id || !checkpointIds.has(event.checkpoint_id)) && association === 'unassigned'
      })
      if (groupedEvents.length > 0) checkpointEntries.push({
        key: `history:${association}`,
        checkpoint: null,
        events: groupedEvents,
        title: syntheticHistoryTitle(association),
        synthetic: true,
        association,
      })
    }
    return checkpointEntries
  }, [checkpoints, detailEvents])
  const currentEntryKey = useMemo(() => {
    if (!summary) return null
    const byId = summary.current_checkpoint_id
      ? entries.find(entry => entry.checkpoint?.checkpoint_id === summary.current_checkpoint_id)
      : undefined
    if (byId) return byId.key
    const byPosition = entries.find(entry => entry.checkpoint?.ordinal === summary.current_checkpoint_ordinal
      && (summary.current_checkpoint_title === null || entry.checkpoint?.title === summary.current_checkpoint_title))
    return byPosition?.key ?? null
  }, [entries, summary])
  const preferredEntryKey = useMemo(() => {
    if (currentEntryKey) return currentEntryKey
    const active = [...entries].reverse().find(entry => entry.checkpoint && ACTIVE_CHECKPOINT_STATUSES.has(entry.checkpoint.status))
    if (active) return active.key
    const approved = [...entries].reverse().find(entry => entry.checkpoint?.status === 'approved' || entry.checkpoint?.status === 'recorded_complete')
    return approved?.key ?? entries[0]?.key ?? null
  }, [currentEntryKey, entries])
  const [navigationVersion, setNavigationVersion] = useState(0)
  const [selection, setSelection] = useState<CheckpointSelectionState>({ runKey: null, key: null, notice: null })
  const [disclosureStates, setDisclosureStates] = useState<Record<string, HistoryDisclosureState>>({})
  const entryKeys = useMemo(() => new Set(entries.map(entry => entry.key)), [entries])

  useEffect(() => {
    setSelection(current => reconcileCheckpointSelection(current, {
      runKey,
      preferredKey: preferredEntryKey,
      availableKeys: entryKeys,
    }))
  }, [entryKeys, preferredEntryKey, runKey])

  const selectionIsCurrent = selection.runKey === runKey
  const selectedEntryKey = selectionIsCurrent ? selection.key : null
  const selectedEntry = entries.find(entry => entry.key === selectedEntryKey) ?? null
  const terminalInactive = isTerminalInactiveRun(run)
  const disclosureState = disclosureStates[runKey] ?? initialHistoryDisclosureState()
  const recordedDelivery = detail?.delivery.filter(stage =>
    !['unknown', 'not_applicable'].includes(stage.status)
    || stage.recorded_at !== null
    || stage.source_reference !== null,
  ) ?? []
  const deliveryRelevant = detail !== null && (
    recordedDelivery.length > 0
    || summary?.availability === 'partial'
  )
  const allInformationalDisclosuresOpen = parentInformationalDisclosuresOpen
    && disclosureState.open.checkpoints
    && disclosureState.open.changes
    && disclosureState.open.time
    && disclosureState.open.evidence
    && (!deliveryRelevant || disclosureState.open.delivery)
    && detailEvents.every(event => disclosureState.openEventIds.has(event.event_id))

  function updateDisclosure(key: HistoryDisclosureKey, open: boolean): void {
    setDisclosureStates(current => {
      const previous = current[runKey] ?? initialHistoryDisclosureState()
      if (previous.open[key] === open) return current
      return {
        ...current,
        [runKey]: {
          ...previous,
          open: { ...previous.open, [key]: open },
        },
      }
    })
  }

  function updateEventDisclosure(eventId: string, open: boolean): void {
    setDisclosureStates(current => {
      const previous = current[runKey] ?? initialHistoryDisclosureState()
      if (previous.openEventIds.has(eventId) === open) return current
      const openEventIds = new Set(previous.openEventIds)
      if (open) openEventIds.add(eventId)
      else openEventIds.delete(eventId)
      return { ...current, [runKey]: { ...previous, openEventIds } }
    })
  }

  function setAllInformationalDisclosure(open: boolean): void {
    onBulkInformationalDisclosureChange?.(open)
    setDisclosureStates(current => {
      return {
        ...current,
        [runKey]: {
          open: {
            checkpoints: open,
            changes: open,
            delivery: open && deliveryRelevant,
            time: open,
            evidence: open,
          },
          openEventIds: open ? new Set(detailEvents.map(event => event.event_id)) : new Set(),
        },
      }
    })
  }

  function selectEntry(key: string): void {
    setSelection(current => current.runKey === runKey && current.key === key && current.notice === null
      ? current
      : { runKey, key, notice: null })
    setNavigationVersion(value => value + 1)
  }

  function selectCurrent(): void {
    if (!currentEntryKey) return
    selectEntry(currentEntryKey)
  }

  if (!summary) {
    return <section className="checkpoint-history" aria-label="Checkpoint history">
      <div className="section-heading"><h4>Checkpoint progress</h4><span className="status-pill">Not reported</span></div>
      <p className="notice">Checkpoint progress is not available for this run. Existing status and diagnostics remain authoritative.</p>
    </section>
  }

  const detailAvailable = detail !== null
  return <section className="checkpoint-history" aria-label="Checkpoint history">
    {selectionIsCurrent && selection.notice && <p className="notice" role="status">{selection.notice}</p>}
    {!detailAvailable && <p className="notice" role="status">Detailed checkpoint history is loading or unavailable; summary facts remain from the selected-run record.</p>}
    {detailAvailable && <>
      <div className="checkpoint-history-disclosure-controls" aria-label="Informational disclosure controls">
        <span className="text-xs text-dim">Explore this run</span>
        <button type="button" className="btn btn-secondary btn-sm" onClick={() => setAllInformationalDisclosure(!allInformationalDisclosuresOpen)}>{allInformationalDisclosuresOpen ? 'Collapse all' : 'Expand all'}</button>
      </div>
      <details
        className="checkpoint-history-disclosure checkpoint-history-checkpoints"
        data-ui-fidelity-anchor="first-disclosure"
        open={disclosureState.open.checkpoints}
        onToggle={event => updateDisclosure('checkpoints', event.currentTarget.open)}
      >
        <summary>
          <span>Checkpoints</span>
          <span className="text-xs text-dim">{entries.length > 0 ? `${entries.length} returned` : 'No returned entries'}</span>
        </summary>
        <div className="checkpoint-history-layout">
          <SidebarEditorLayout
            key={runKey}
            selection={selectedEntryKey}
            navigationVersion={navigationVersion}
            listLabel="Checkpoints"
            navigation={<div className="checkpoint-history-navigation">
              <div className="checkpoint-history-navigation-tools">
                {!terminalInactive && <button type="button" className="btn btn-secondary btn-sm" disabled={!currentEntryKey} onClick={selectCurrent}>Current checkpoint</button>}
              </div>
              {entries.length > 0
                ? entries.map(entry => <CheckpointRow entry={entry} selected={entry.key === selectedEntryKey} onSelect={selectEntry} key={entry.key} />)
                : <p className="text-sm text-dim">No checkpoint entries were returned.</p>}
            </div>}
            detailHeading={<div className="checkpoint-history-detail-heading">
              <h5>{selectedEntry?.title ?? 'Checkpoint detail'}</h5>
              {selectedEntry && <p className="text-xs text-dim">{selectedEntry.checkpoint ? checkpointStatusText(selectedEntry.checkpoint) : syntheticHistoryStatus(selectedEntry.association)}{selectedEntry.checkpoint?.checkpoint_id ? ` · ${selectedEntry.checkpoint.checkpoint_id}` : ''}</p>}
            </div>}
          >
            <CheckpointDetail
              entry={selectedEntry}
              run={run}
              runId={run.run_id}
              openEventIds={disclosureState.openEventIds}
              onEventToggle={updateEventDisclosure}
            />
          </SidebarEditorLayout>
        </div>
      </details>
    </>}
    <div className="checkpoint-history-disclosures">
      {detail && <details
          className="checkpoint-history-disclosure"
          open={disclosureState.open.changes}
          onToggle={event => updateDisclosure('changes', event.currentTarget.open)}
        >
          <summary>Run settings &amp; changes</summary>
          <ChangeDisclosure title="Applied changes" changes={detail.applied_changes ?? []} emptyText="No applied team/profile changes were returned." />
          <ChangeDisclosure title="Pending changes" changes={detail.pending_changes ?? []} emptyText="No pending team/profile changes were returned." />
        </details>}
      {deliveryRelevant && detail && <details
          id="checkpoint-history-delivery-evidence"
          className="checkpoint-history-disclosure"
          open={disclosureState.open.delivery}
          onToggle={event => updateDisclosure('delivery', event.currentTarget.open)}
        >
          <summary>Delivery evidence</summary>
          <DeliveryDisclosure stages={recordedDelivery} />
        </details>}
      {detail && <TimeDisclosure
          run={run}
          detail={detail}
          open={disclosureState.open.time}
          onOpenChange={open => updateDisclosure('time', open)}
        />}
      <EvidenceDisclosure
        run={run}
        progress={summary}
        detail={detail}
        recordedProgress={recordedProgress}
        open={disclosureState.open.evidence}
        onOpenChange={open => updateDisclosure('evidence', open)}
      />
    </div>
  </section>
}
