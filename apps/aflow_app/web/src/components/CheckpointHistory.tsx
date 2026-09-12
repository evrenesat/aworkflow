import { useEffect, useMemo, useState } from 'react'
import type {
  RunProgressChange,
  RunProgressCount,
  RunProgressDeliveryStage,
  RunProgressDetail,
  RunProgressDetailCheckpoint,
  RunProgressDetailEvent,
  RunProgressExecutor,
  RunProgressSummary,
  RunStatus,
} from '../types'
import { formatMachineLabel } from '../label'
import { executionDuration, statusLabel } from '../runPresentation'
import { SidebarEditorLayout } from './SidebarEditorLayout'

interface CheckpointHistoryProps {
  projectId: string
  run: RunStatus
  progress: RunProgressSummary | null
  detail: RunProgressDetail | null
}

interface HistoryEntry {
  key: string
  checkpoint: RunProgressDetailCheckpoint | null
  events: RunProgressDetailEvent[]
  title: string
  synthetic: boolean
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
  if (value === null) return `Unknown ${plural}`
  return `${count?.coverage === 'partial' ? 'At least ' : ''}${value} ${value === 1 ? singular : plural}`
}

function countShort(count: RunProgressCount | null | undefined): string {
  const value = countValue(count)
  if (value === null) return 'Unknown'
  return `${count?.coverage === 'partial' ? '≥' : ''}${value}`
}

function approvalText(progress: RunProgressSummary): string {
  const approved = countShort(progress.approved_checkpoints)
  const total = countShort(progress.total_checkpoints)
  return `${approved} / ${total} approved`
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
  if (!value) return null
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
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

function executorText(executor: RunProgressExecutor | null): string {
  if (!executor) return 'Not reported'
  const parts = executorParts(executor)
  if (executor.source_run_id) parts.push(`source run ${executor.source_run_id}`)
  if (executor.invocation_id) parts.push(`invocation ${executor.invocation_id}`)
  if (executor.turn_number !== null) parts.push(`turn ${executor.turn_number}`)
  return parts.length > 0 ? parts.join(' · ') : 'Identity not reported'
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
  const sourceRun = trimmed(event.source_run_id)
  if (sourceRun && sourceRun !== runId) return `Inherited from run ${sourceRun}`
  if (sourceRun) return `Recorded by run ${sourceRun}`
  return null
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
  return trimmed(value) ?? 'Not reported'
}

function changeTransition(oldValue: string | null, newValue: string | null): string {
  if (!oldValue && !newValue) return 'Not reported'
  return `${changeValue(oldValue)} → ${changeValue(newValue)}`
}

function sourceReferenceText(reference: Record<string, unknown> | null): string | null {
  if (!reference) return null
  const artifact = referenceValue(reference, 'artifact', 'relative_path')
  const turn = referenceValue(reference, 'turn_number')
  const decision = referenceValue(reference, 'decision_number')
  const parts = [
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
    onClick={() => onSelect(entry.key)}
  >
    <span className="checkpoint-history-entry-title">
      <strong>{entry.title}</strong>
      <span className="status-pill">{checkpoint ? checkpointStatusText(checkpoint) : 'History not assigned'}</span>
    </span>
    {checkpoint
      ? <span className="text-xs text-dim">{countShort(checkpoint.worker_attempts)} worker attempts · {countShort(checkpoint.repair_passes)} repairs · {countShort(checkpoint.reviews)} reviews</span>
      : <span className="text-xs text-dim">{entry.events.length} event{entry.events.length === 1 ? '' : 's'} outside a returned checkpoint</span>}
  </button>
}

function ExecutorRow({ label, executor }: { label: string; executor: RunProgressExecutor | null }): JSX.Element {
  const duration = formatDuration(executor?.duration_seconds)
  return <div>
    <dt>{label}</dt>
    <dd>{executorText(executor)}{duration ? ` · ${duration}` : ''}</dd>
  </div>
}

function EventTimeline({ events, runId }: { events: RunProgressDetailEvent[]; runId: string }): JSX.Element {
  if (events.length === 0) return <p className="text-sm text-dim">No attempt or review events are reported for this selection.</p>
  const eventIds = new Set(events.map(event => event.event_id))
  return <ol className="checkpoint-history-timeline">
    {events.map((event) => {
      const retryReference = eventRetryReference(event)
      const retryTarget = retryReference && eventIds.has(retryReference) ? retryReference : null
      const completionTurn = retryReference ? null : retryCompletionTurn(event)
      const completionTarget = completionTurn === null ? null : retryCompletionTarget(event, events, completionTurn)
      const source = eventSourceText(event, runId)
      const duration = formatDuration(event.duration_seconds)
      const recordedAt = formatTimestamp(event.started_at ?? event.ended_at)
      const sourceReference = sourceReferenceText(event.source_reference)
      return <li className="checkpoint-history-event" id={`checkpoint-event-${event.event_id}`} key={event.event_id}>
        <div className="checkpoint-history-event-heading">
          <strong>{eventKindText(event.kind)}</strong>
          <span className="status-pill">{trimmed(event.outcome) ? formatMachineLabel(event.outcome as string) : 'Outcome not reported'}</span>
        </div>
        <dl className="checkpoint-history-event-meta">
          {event.turn_number !== null && <div><dt>Turn</dt><dd>{event.turn_number}</dd></div>}
          {event.decision_number !== null && <div><dt>Decision</dt><dd>{event.decision_number}</dd></div>}
          <div><dt>Executor</dt><dd>{executorText(event.executor)}</dd></div>
          <div><dt>Duration</dt><dd>{duration ?? 'Not reported'}</dd></div>
          {recordedAt && <div><dt>Recorded</dt><dd>{recordedAt}</dd></div>}
          {source && <div><dt>History</dt><dd>{source}</dd></div>}
        </dl>
        <p className="text-sm">Reason: {trimmed(event.reason) ?? 'Not reported'}</p>
        {retryTarget
          ? <p className="text-xs text-dim"><a href={`#checkpoint-event-${retryTarget}`}>Retry of recorded event</a></p>
          : completionTurn !== null
            ? completionTarget
              ? <p className="text-xs text-dim"><a href={`#checkpoint-event-${completionTarget.event_id}`}>Retried invocation: turn {completionTurn}</a></p>
              : <p className="text-xs text-dim">Retried invocation: turn {completionTurn} (no unique invocation returned in this view).</p>
            : event.kind === 'runtime_retry' && <p className="text-xs text-dim">Retry relationship not established by the evidence.</p>}
        {sourceReference && <p className="text-xs text-dim">Source reference: <span className="mono">{sourceReference}</span></p>}
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
    <p>{transition.length > 0 ? transition.join(' · ') : 'Team/profile values not reported'}</p>
    <p className="text-xs text-dim">Roles: {change.roles.length > 0 ? change.roles.map(formatMachineLabel).join(', ') : 'not reported'}{change.turn_number !== null ? ` · turn ${change.turn_number}` : ''}{change.checkpoint_id ? ` · ${change.checkpoint_id}` : ''}{change.generation_id ? ` · generation ${change.generation_id}` : ''}</p>
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

function DeliveryDisclosure({ stages }: { stages: RunProgressDeliveryStage[] }): JSX.Element {
  return <section>
    <h6>Delivery stages</h6>
    {stages.length > 0
      ? <ul className="checkpoint-history-delivery-list">{stages.map((stage, index) => <li key={`${stage.stage}-${index}`}>
        <span><strong>{stage.stage === 'ci' ? 'CI' : formatMachineLabel(stage.stage)}</strong> · {formatMachineLabel(stage.status)}</span>
        {stage.reason && <span className="text-xs text-dim">{stage.reason}</span>}
        {stage.recorded_at && <span className="text-xs text-dim">{formatTimestamp(stage.recorded_at)}</span>}
      </li>)}</ul>
      : <p className="text-sm text-dim">Delivery evidence not reported.</p>}
  </section>
}

function CheckpointDetail({ entry, run, runId, allEvents }: { entry: HistoryEntry | null; run: RunStatus; runId: string; allEvents: RunProgressDetailEvent[] }): JSX.Element {
  if (!entry) return <div className="checkpoint-history-empty-detail"><p className="text-sm text-dim">Select a returned checkpoint to inspect its evidence.</p></div>
  const checkpoint = entry.checkpoint
  return <div className="checkpoint-history-detail">
    {checkpoint && <dl className="checkpoint-history-checkpoint-meta">
      <div><dt>Worker attempts</dt><dd>{countText(checkpoint.worker_attempts, 'worker attempt', 'worker attempts')}</dd></div>
      <div><dt>Repair passes</dt><dd>{countText(checkpoint.repair_passes, 'repair pass', 'repair passes')}</dd></div>
      <div><dt>Reviews</dt><dd>{countText(checkpoint.reviews, 'review', 'reviews')}</dd></div>
      <div><dt>Runtime retries</dt><dd>{countText(checkpoint.runtime_retries, 'runtime retry', 'runtime retries')}</dd></div>
      <div><dt>Applied upgrades</dt><dd>{countText(checkpoint.applied_upgrades, 'upgrade', 'upgrades')}</dd></div>
      <div><dt>Duration</dt><dd>{formatDuration(checkpoint.duration_seconds) ?? 'Not reported'}</dd></div>
      <div><dt>Recorded</dt><dd>{formatTimestamp(checkpoint.recorded_at) ?? 'Not reported'}</dd></div>
      <div><dt>Source run</dt><dd className="mono">{checkpoint.source_run_id ?? 'Not reported'}</dd></div>
      {checkpoint.parent_checkpoint_id && <div><dt>Parent checkpoint</dt><dd className="mono">{checkpoint.parent_checkpoint_id}</dd></div>}
      {checkpoint.generation_id && <div><dt>Generation</dt><dd className="mono">{checkpoint.generation_id}</dd></div>}
    </dl>}
    <section className="checkpoint-history-attempts">
      <div className="section-heading"><h6>Attempt and review timeline</h6><span className="text-xs text-dim">{entry.events.length} unique event{entry.events.length === 1 ? '' : 's'}</span></div>
      <EventTimeline events={entry.events} runId={runId} />
    </section>
    {entry.synthetic && allEvents.some(event => event.source_run_id && event.source_run_id !== runId) && <p className="notice">Some history is inherited from another run or could not be assigned to a returned checkpoint.</p>}
    {run.restarted_from_run_id && <p className="text-xs text-dim">This run is a successor of <span className="mono">{run.restarted_from_run_id}</span>; inherited events remain labelled by their source run.</p>}
  </div>
}

function EvidenceDisclosure({ progress, detail }: { progress: RunProgressSummary; detail: RunProgressDetail | null }): JSX.Element {
  const truncation = detail?.truncation
  const omitted = (truncation?.omitted_records ?? 0) + (truncation?.omitted_checkpoints ?? 0)
  const notices = truncation?.notices ?? []
  return <details className="checkpoint-history-disclosure">
    <summary>Count definitions &amp; evidence</summary>
    <p>Counts come from the bounded canonical evidence projection. Partial values are lower bounds; unavailable values are not zero.</p>
    <dl className="checkpoint-history-evidence-meta">
      <div><dt>Availability</dt><dd>{availabilityText(progress.availability)}</dd></div>
      <div><dt>Observed</dt><dd>{formatTimestamp(progress.observed_at) ?? 'Not reported'}</dd></div>
      <div><dt>Latest evidence</dt><dd>{formatTimestamp(progress.evidence_at) ?? 'Not reported'}</dd></div>
      <div><dt>Original plan</dt><dd>{progress.original_plan_display_name ?? 'Not reported'}</dd></div>
      <div><dt>Plan identity</dt><dd className="mono">{progress.original_plan_identity ?? 'Not reported'}</dd></div>
      <div><dt>Reason codes</dt><dd>{progress.reason_codes.length > 0 ? progress.reason_codes.join(', ') : 'None reported'}</dd></div>
    </dl>
    {(progress.availability === 'partial' || omitted > 0 || notices.length > 0) && <div className="notice">
      {notices.length > 0 ? notices.join(' · ') : 'Some earlier progress history is unavailable in this bounded view.'}
      {omitted > 0 ? ` Omitted records/checkpoints: ${omitted}.` : ''}
    </div>}
  </details>
}

export function CheckpointHistory({ projectId, run, progress, detail }: CheckpointHistoryProps): JSX.Element {
  const summary = progress ?? detail
  const runKey = `${projectId}/${run.run_id}`
  const detailEvents = useMemo(() => uniqueEvents(detail?.events ?? []), [detail?.events])
  const checkpoints = useMemo(() => sortCheckpoints(detail?.checkpoints ?? []), [detail?.checkpoints])
  const entries = useMemo(() => {
    const checkpointIds = new Set(checkpoints.map(checkpoint => checkpoint.checkpoint_id).filter((value): value is string => Boolean(value)))
    const checkpointEntries = checkpoints.map((checkpoint, index): HistoryEntry => ({
      key: checkpointKey(checkpoint, index),
      checkpoint,
      events: detailEvents.filter(event => eventMatchesCheckpoint(event, checkpoint)),
      title: checkpointTitle(checkpoint),
      synthetic: false,
    }))
    const unassignedEvents = detailEvents.filter(event => event.checkpoint_id === null || !checkpointIds.has(event.checkpoint_id))
    if (unassignedEvents.length > 0) checkpointEntries.push({
      key: 'history:unassigned',
      checkpoint: null,
      events: unassignedEvents,
      title: 'Unassigned or omitted history',
      synthetic: true,
    })
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
  const runElapsed = executionDuration(run, Date.now())
  const currentExecutor = summary?.current_executor ?? null
  const lastExecutor = summary?.last_executor ?? null
  const currentAttempt = currentExecutor
    ? [currentExecutor.turn_number !== null ? `turn ${currentExecutor.turn_number}` : null, formatDuration(currentExecutor.duration_seconds)].filter(Boolean).join(' · ')
    : null

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
    <div className="section-heading checkpoint-history-heading">
      <div>
        <h4>Checkpoint progress</h4>
        <p className="text-xs text-dim">{approvalText(summary)} · {positionText(summary)}</p>
      </div>
      <span className="status-pill">{availabilityText(summary.availability)}</span>
    </div>
    <dl className="checkpoint-history-summary">
      <div><dt>Checkpoint position</dt><dd>{positionText(summary)}</dd></div>
      <div><dt>Approved / total</dt><dd>{approvalText(summary)}</dd></div>
      <div><dt>Recorded complete</dt><dd>{countText(summary.recorded_complete_checkpoints, 'checkpoint', 'checkpoints')}</dd></div>
      <div><dt>Worker attempts</dt><dd>{countText(summary.worker_attempts, 'attempt', 'attempts')}</dd></div>
      <div><dt>Repair passes</dt><dd>{countText(summary.repair_passes, 'repair pass', 'repair passes')}</dd></div>
      <div><dt>Reviews</dt><dd>{countText(summary.reviews, 'review', 'reviews')}</dd></div>
      <div><dt>Runtime retries</dt><dd>{countText(summary.runtime_retries, 'runtime retry', 'runtime retries')}</dd></div>
      <div><dt>Applied upgrades</dt><dd>{countText(summary.applied_upgrades, 'upgrade', 'upgrades')}</dd></div>
      <div><dt>Run activity</dt><dd>{summary.activity ? formatMachineLabel(summary.activity) : statusLabel(run)}</dd></div>
      <div><dt>Run elapsed</dt><dd>{runElapsed ?? 'Not reported'}</dd></div>
      <div><dt>Turn budget</dt><dd>{run.turns_completed !== null ? `${run.turns_completed} completed` : 'Completed turns unknown'}{run.max_turns !== null ? ` · ceiling ${run.max_turns}` : ' · ceiling not reported'}</dd></div>
      <ExecutorRow label="Current executor" executor={currentExecutor} />
      <ExecutorRow label="Last executor" executor={lastExecutor} />
      <div><dt>Current attempt</dt><dd>{currentAttempt ?? 'Not reported'}</dd></div>
    </dl>
    {selectionIsCurrent && selection.notice && <p className="notice" role="status">{selection.notice}</p>}
    {!detailAvailable && <p className="notice" role="status">Detailed checkpoint history is loading or unavailable; summary facts remain from the selected-run record.</p>}
    {detailAvailable && <div className="checkpoint-history-layout">
      <SidebarEditorLayout
        key={runKey}
        selection={selectedEntryKey}
        navigationVersion={navigationVersion}
        listLabel="Checkpoints"
        navigation={<div className="checkpoint-history-navigation">
          <div className="section-heading"><h5>Checkpoints</h5><button type="button" className="btn btn-secondary btn-sm" disabled={!currentEntryKey} onClick={selectCurrent}>Current checkpoint</button></div>
          {entries.length > 0
            ? entries.map(entry => <CheckpointRow entry={entry} selected={entry.key === selectedEntryKey} onSelect={selectEntry} key={entry.key} />)
            : <p className="text-sm text-dim">No checkpoint entries were returned.</p>}
        </div>}
        detailHeading={<div className="checkpoint-history-detail-heading">
          <h5>{selectedEntry?.title ?? 'Checkpoint detail'}</h5>
          {selectedEntry && <p className="text-xs text-dim">{selectedEntry.checkpoint ? checkpointStatusText(selectedEntry.checkpoint) : 'Unassigned or omitted checkpoint association'}{selectedEntry.checkpoint?.checkpoint_id ? ` · ${selectedEntry.checkpoint.checkpoint_id}` : ''}</p>}
        </div>}
      >
        <CheckpointDetail entry={selectedEntry} run={run} runId={run.run_id} allEvents={detailEvents} />
      </SidebarEditorLayout>
    </div>}
    {detail && <div className="checkpoint-history-disclosures">
      <details className="checkpoint-history-disclosure">
        <summary>Team &amp; change history</summary>
        <ChangeDisclosure title="Applied changes" changes={detail.applied_changes ?? []} emptyText="No applied team/profile changes were returned." />
        <ChangeDisclosure title="Pending changes" changes={detail.pending_changes ?? []} emptyText="No pending team/profile changes were returned." />
      </details>
      <details className="checkpoint-history-disclosure">
        <summary>Delivery evidence</summary>
        <DeliveryDisclosure stages={detail.delivery ?? []} />
      </details>
      <EvidenceDisclosure progress={summary} detail={detail} />
    </div>}
  </section>
}
