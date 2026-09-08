import type { RunStatus } from './types'

export const terminalStatuses = new Set(['completed', 'done', 'failed', 'owner_stopped', 'interrupted'])

export function statusLabel(run: RunStatus): string {
  if (run.status === 'failed' && run.worker_exit && !run.evidence.has_run_metadata) return 'Could not start'
  if (run.status_reason_code === 'startup_failed') return 'Could not start'
  if (run.ownership === 'legacy' && !terminalStatuses.has(run.status)) return 'Needs attention'
  if (run.status === 'needs_attention') {
    const failure = run.evidence.startup_failure as { stage?: string } | null
    return failure?.stage === 'preparation' && run.evidence.no_agent_started ? 'Could not start' : 'Needs attention'
  }
  if (run.status === 'awaiting_startup_answer') return 'Input needed'
  if (['manifest_only', 'launch_requested', 'unit_started', 'launch_started'].includes(run.status)) return run.activity === 'active' || run.evidence.unit_active === true ? 'Starting' : 'Needs attention'
  if (run.status === 'owner_stopped') return 'Stopped'
  if (run.status === 'done') return 'Completed'
  const label = run.status.replace(/_/g, ' ')
  return label.charAt(0).toUpperCase() + label.slice(1)
}

export function executionDuration(run: RunStatus, now: number): string | null {
  if (!run.started_at) return null
  const start = Date.parse(run.started_at)
  const end = terminalStatuses.has(run.status)
    ? Date.parse(run.ended_at ?? '')
    : run.status === 'running' && run.ownership === 'control_plane' ? now : NaN
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return null
  const seconds = Math.floor((end - start) / 1000)
  return seconds < 60 ? `${seconds}s` : seconds < 3600 ? `${Math.floor(seconds / 60)}m ${seconds % 60}s` : `${Math.floor(seconds / 3600)}h ${Math.floor(seconds % 3600 / 60)}m`
}
