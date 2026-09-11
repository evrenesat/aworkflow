import type { RunProgressCount, RunProgressExecutor, RunProgressSummary, RunStatus } from '../types'
import { formatMachineLabel } from '../label'
import { executionDuration, statusLabel } from '../runPresentation'

const MAX_SEGMENTS = 30
const TERMINAL_LABELS = new Set(['Completed', 'Failed', 'Could not start', 'Stopped', 'Interrupted', 'Needs attention'])

function trimmed(value: string | null | undefined): string | null {
  const text = value?.trim()
  return text ? text : null
}

function countValue(count: RunProgressCount | null | undefined): number | null {
  return count?.coverage !== 'unavailable' && typeof count?.value === 'number' && Number.isSafeInteger(count.value) && count.value >= 0
    ? count.value
    : null
}

function countIsPartial(count: RunProgressCount | null | undefined): boolean {
  return count?.coverage === 'partial'
}

function countText(count: RunProgressCount | null | undefined, singular: string, plural: string): string {
  const value = countValue(count)
  if (value === null) return `Unknown ${plural}`
  return `${countIsPartial(count) ? 'At least ' : ''}${value} ${value === 1 ? singular : plural}`
}

function approvalText(progress: RunProgressSummary): string {
  if (progress.availability === 'unavailable' || progress.availability === 'not_applicable') return 'Checkpoint progress unavailable'
  const approved = countValue(progress.approved_checkpoints)
  const total = countValue(progress.total_checkpoints)
  const partial = countIsPartial(progress.approved_checkpoints) || countIsPartial(progress.total_checkpoints)
  if (approved !== null && total !== null) return `${partial ? 'At least ' : ''}${approved} / ${total} approved`
  if (approved !== null) return `${partial ? 'At least ' : ''}${approved} approved · total unknown`
  if (total !== null) return `Unknown / ${total} approved`
  return 'Approval progress unknown'
}

function checkpointStateLabel(progress: RunProgressSummary, run: RunStatus): string {
  const status = statusLabel(run)
  if (TERMINAL_LABELS.has(status)) return status
  const phase = trimmed(progress.phase) || trimmed(progress.activity)
  return phase ? formatMachineLabel(phase) : status
}

function checkpointPositionText(progress: RunProgressSummary, run: RunStatus): string {
  const ordinal = progress.current_checkpoint_ordinal
  const total = countValue(progress.total_checkpoints)
  const title = trimmed(progress.current_checkpoint_title)
  if (ordinal !== null && Number.isSafeInteger(ordinal) && ordinal > 0) {
    const checkpoint = `CP${ordinal}${total !== null ? ` of ${total}` : ''}`
    return `${checkpointStateLabel(progress, run)} ${checkpoint}${title ? ` — ${title}` : ''}`
  }
  return `${checkpointStateLabel(progress, run)} · No current checkpoint reported`
}

function formatDuration(seconds: number | null): string | null {
  if (seconds === null || !Number.isFinite(seconds) || seconds < 0) return null
  const whole = Math.floor(seconds)
  if (whole < 60) return `${whole}s`
  if (whole < 3600) return `${Math.floor(whole / 60)}m ${whole % 60}s`
  return `${Math.floor(whole / 3600)}h ${Math.floor(whole % 3600 / 60)}m`
}

function executorText(executor: RunProgressExecutor | null): string {
  if (!executor) return 'Executor not reported'
  const role = trimmed(executor.role)
  const model = trimmed(executor.model_display) || trimmed(executor.model)
  const selector = trimmed(executor.selector)
  const team = trimmed(executor.team)
  const identity = [model, selector && selector !== model ? selector : null].filter(Boolean)
  const detail = identity.length > 0 ? identity.join(' · ') : team || trimmed(executor.harness)
  return [role ? formatMachineLabel(role) : null, detail].filter(Boolean).join(' · ') || 'Executor not reported'
}

function relativeAge(timestamp: string | null, now: number): string | null {
  if (!timestamp) return null
  const value = Date.parse(timestamp)
  if (!Number.isFinite(value)) return null
  const seconds = Math.max(0, Math.floor((now - value) / 1000))
  if (seconds < 60) return 'just now'
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`
  return `${Math.floor(seconds / 86400)}d ago`
}

function evidenceNote(progress: RunProgressSummary): string | null {
  if (progress.availability === 'partial') return 'Progress evidence partial'
  if (progress.reason_codes.includes('history_partial') || progress.reason_codes.includes('evidence_truncated')) {
    return 'Earlier progress history unavailable'
  }
  return null
}

function recordedCompleteNote(progress: RunProgressSummary, run: RunStatus): string | null {
  const recorded = countValue(progress.recorded_complete_checkpoints)
  const approved = countValue(progress.approved_checkpoints)
  if (recorded === null || approved === null
    || progress.recorded_complete_checkpoints.coverage !== 'complete'
    || progress.approved_checkpoints.coverage !== 'complete'
    || recorded <= approved) return null
  const activity = `${progress.phase ?? ''} ${progress.activity ?? ''} ${run.status}`.toLowerCase()
  return activity.includes('review') || activity.includes('input')
    ? 'Recorded complete · awaiting review'
    : 'Recorded-complete evidence present'
}

function segmentState(progress: RunProgressSummary, ordinal: number): 'approved' | 'recorded' | 'current' | 'pending' | 'unknown' {
  if (progress.current_checkpoint_ordinal === ordinal) return 'current'
  switch (progress.checkpoint_states?.[String(ordinal)]) {
    case 'approved': return 'approved'
    case 'recorded_complete': return 'recorded'
    case 'pending': return 'pending'
    default: return 'unknown'
  }
}

export function RunProgressStrip({ progress }: { progress: RunProgressSummary }): JSX.Element | null {
  const total = countValue(progress.total_checkpoints)
  if (total === null || total <= 0 || progress.availability === 'unavailable' || progress.availability === 'not_applicable') return null
  if (total > MAX_SEGMENTS) {
    const approved = countValue(progress.approved_checkpoints)
    const width = approved === null ? 0 : Math.min(100, (approved / total) * 100)
    return <span className={`compact-run-progress-bar ${approved === null ? 'unknown' : countIsPartial(progress.approved_checkpoints) ? 'partial' : ''}`} aria-hidden="true">
      <span style={{ width: `${width}%` }} />
    </span>
  }
  return <span className="compact-run-progress-strip" aria-hidden="true">
    {Array.from({ length: total }, (_, index) => {
      const state = segmentState(progress, index + 1)
      return <span className={`compact-run-progress-segment ${state}`} key={index} />
    })}
  </span>
}

export function RunProgress({ run, now = Date.now() }: { run: RunStatus; now?: number }): JSX.Element {
  const progress = run.progress
  if (!progress) {
    return <span className="compact-run-progress unavailable" data-progress-availability="unavailable">
      <span className="compact-run-progress-line"><strong>Checkpoint progress unavailable</strong></span>
    </span>
  }

  const executor = progress.current_executor || progress.last_executor
  const elapsed = executionDuration(run, now)
  const executorDuration = formatDuration(executor?.duration_seconds ?? null)
  const age = relativeAge(progress.evidence_at, now)
  const note = evidenceNote(progress)
  const recordedNote = recordedCompleteNote(progress, run)
  const metrics = [
    countText(progress.worker_attempts, 'worker attempt', 'worker attempts'),
    countText(progress.repair_passes, 'repair pass', 'repair passes'),
    countText(progress.reviews, 'review', 'reviews'),
    countText(progress.runtime_retries, 'runtime retry', 'runtime retries'),
    countText(progress.applied_upgrades, 'upgrade', 'upgrades'),
  ]

  return <span className={`compact-run-progress ${progress.availability}`} data-progress-availability={progress.availability}>
    <span className="compact-run-progress-line">
      <strong>{approvalText(progress)}</strong>
      <span>{progress.availability === 'not_applicable' ? 'Non-checkpoint workflow' : checkpointPositionText(progress, run)}</span>
    </span>
    <RunProgressStrip progress={progress} />
    <span className="compact-run-progress-meta">
      {elapsed && <span>Elapsed {elapsed}</span>}
      {executor && <span>Executor {executorText(executor)}{executorDuration ? ` · ${executorDuration}` : ''}</span>}
      {age && <span>Latest evidence {age}</span>}
      {note && <span>{note}</span>}
      {recordedNote && <span>{recordedNote}</span>}
    </span>
    <span className="compact-run-progress-metrics">{metrics.join(' · ')}{progress.recorded_complete_checkpoints.coverage !== 'unavailable' ? ` · ${countText(progress.recorded_complete_checkpoints, 'recorded-complete checkpoint', 'recorded-complete checkpoints')}` : ''}</span>
  </span>
}
