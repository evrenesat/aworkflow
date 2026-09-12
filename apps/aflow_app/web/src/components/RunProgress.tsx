import type { RunProgressCount, RunProgressSummary, RunStatus } from '../types'
import { formatMachineLabel } from '../label'
import {
  checkpointApprovalText,
  isTerminalInactiveRun,
  progressHistoryNotice,
  runTurnBudgetText,
} from '../runPresentation'

const MAX_SEGMENTS = 30

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

function checkpointPositionText(progress: RunProgressSummary, run: RunStatus): string | null {
  if (isTerminalInactiveRun(run) || progress.availability === 'not_applicable') return null
  const phase = trimmed(progress.phase) || trimmed(progress.activity)
  const ordinal = progress.current_checkpoint_ordinal
  const total = countValue(progress.total_checkpoints)
  const title = trimmed(progress.current_checkpoint_title)
  if (ordinal !== null && Number.isSafeInteger(ordinal) && ordinal > 0) {
    const checkpoint = `CP${ordinal}${total !== null ? ` of ${total}` : ''}`
    return `${checkpoint}${phase ? ` · ${formatMachineLabel(phase)}` : ''}${title ? ` — ${title}` : ''}`
  }
  return phase ? `${formatMachineLabel(phase)} · current checkpoint not reported` : 'Current checkpoint not reported'
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

function stateLabel(state: ReturnType<typeof segmentState>): string {
  switch (state) {
    case 'recorded': return 'recorded complete'
    default: return state
  }
}

function checkpointStripText(progress: RunProgressSummary, total: number): string {
  const counts = new Map<string, number>()
  for (let ordinal = 1; ordinal <= total; ordinal += 1) {
    const state = stateLabel(segmentState(progress, ordinal))
    counts.set(state, (counts.get(state) ?? 0) + 1)
  }
  const orderedStates = ['approved', 'recorded complete', 'current', 'pending', 'unknown']
  const breakdown = orderedStates
    .filter(state => (counts.get(state) ?? 0) > 0)
    .map(state => `${counts.get(state)} ${state}`)
    .join(' · ')
  const approved = countValue(progress.approved_checkpoints)
  const approvedText = approved === null
    ? 'aggregate approval count unknown'
    : `${countIsPartial(progress.approved_checkpoints) ? 'at least ' : ''}${approved} approved by aggregate count`
  return `Checkpoint state bar: ${breakdown || 'no states reported'}; ${approvedText}; it does not represent approval by fill alone.`
}

export function RunProgressStrip({ progress }: { progress: RunProgressSummary }): JSX.Element | null {
  const total = countValue(progress.total_checkpoints)
  if (total === null || total <= 0 || progress.availability === 'unavailable' || progress.availability === 'not_applicable') return null
  const stripText = checkpointStripText(progress, total)
  if (total > MAX_SEGMENTS) {
    const approved = countValue(progress.approved_checkpoints)
    const width = approved === null ? 0 : Math.min(100, (approved / total) * 100)
    return <span className={`compact-run-progress-bar ${approved === null ? 'unknown' : countIsPartial(progress.approved_checkpoints) ? 'partial' : ''}`} role="img" aria-label={stripText} title={stripText}>
      <span style={{ width: `${width}%` }} />
      <span className="sr-only">{stripText}</span>
    </span>
  }
  return <span className="compact-run-progress-strip" role="img" aria-label={stripText} title={stripText}>
    {Array.from({ length: total }, (_, index) => {
      const state = segmentState(progress, index + 1)
      return <span className={`compact-run-progress-segment ${state}`} title={`Checkpoint ${index + 1}: ${stateLabel(state)}`} key={index} />
    })}
    <span className="sr-only">{stripText}</span>
  </span>
}

export function RunProgress({ run, now: _now = Date.now() }: { run: RunStatus; now?: number }): JSX.Element {
  const progress = run.progress
  if (!progress) {
    return <span className="compact-run-progress unavailable" data-progress-availability="unavailable">
      <span className="compact-run-progress-line"><strong>Checkpoint progress unavailable</strong></span>
    </span>
  }

  const checkpointPosition = checkpointPositionText(progress, run)
  const notice = progressHistoryNotice(progress)

  return <span className={`compact-run-progress ${progress.availability}`} data-progress-availability={progress.availability}>
    <span className="compact-run-progress-line">
      <strong>{checkpointApprovalText(progress)}</strong>
      {checkpointPosition && <span>{checkpointPosition}</span>}
    </span>
    <RunProgressStrip progress={progress} />
    <span className="compact-run-progress-meta">
      <span>{runTurnBudgetText(run)}</span>
      {notice && <span>{notice}</span>}
    </span>
  </span>
}
