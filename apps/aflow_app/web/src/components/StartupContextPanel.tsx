import { useState } from 'react'
import type { StartupContext, StartupRelatedRun } from '../types'
import { formatMachineLabel } from '../label'

interface StartupContextPanelProps {
  context: StartupContext | null | undefined
  mode: 'preparation' | 'review' | 'pending'
  fallbackPlanPath?: string | null
  planAvailable?: boolean
  onOpenPlan?: (path: string) => void
  onOpenRun?: (runId: string) => void
  onCancelPendingStart?: (runId: string) => void
  pendingCancellationAvailable?: boolean
}

function relatedFacts(run: StartupRelatedRun): string[] {
  return [
    run.unmerged_work === true ? 'Work is not merged into the starting branch' : null,
    run.uncommitted_work === true ? 'Uncommitted implementation work is preserved' : null,
    run.worktree_verified && run.worktree_path ? `Verified worktree: ${run.worktree_path}` : null,
    run.branch_verified && run.branch ? `Verified branch: ${run.branch}` : null,
    run.failure_reason ? `Previously reported: ${run.failure_reason}` : null,
  ].filter((fact): fact is string => fact !== null)
}

export function StartupContextPanel({ context, mode, fallbackPlanPath = null, planAvailable = false, onOpenPlan, onOpenRun, onCancelPendingStart, pendingCancellationAvailable = false }: StartupContextPanelProps) {
  const [outlineOpen, setOutlineOpen] = useState(false)
  const planPath = context?.plan_path ?? fallbackPlanPath
  if (!context) return <div className="startup-context-panel">{mode === 'pending' && <p>No agent started.</p>}<p className="text-sm text-dim">Plan startup context is unavailable from this server. {mode === 'pending' ? 'Review the recorded plan and startup question before continuing.' : 'Review the selected plan before starting.'}</p>{planPath && planAvailable && onOpenPlan && <button type="button" className="btn btn-secondary btn-sm" onClick={() => onOpenPlan(planPath)}>Open plan</button>}</div>

  const checkpoint = context.next_checkpoint
  const singleRelated = context.related_runs.length === 1 ? context.related_runs[0] : null
  const recovery = context.recommendation === 'review_previous_run'
  const active = context.recommendation === 'open_existing_run'
  const hasPlan = context.availability === 'available' || context.availability === 'partial'
  const checkpointTitle = checkpoint?.title.replace(/^Checkpoint\s+\d+\s*:\s*/i, '')
  const heading = recovery ? 'Previous work needs recovery'
    : active ? 'This plan is already running'
      : context.recommendation === 'inspect_previous_runs' ? 'Inspect earlier runs before starting'
        : context.recommendation === 'blocked' ? 'Startup needs attention'
          : 'Plan before execution'

  return <section className="startup-context-panel" aria-label={`Startup context ${mode}`}>
    <h4>{heading}</h4>
    {mode === 'pending' && <p className="text-sm text-dim">No agent started.</p>}
    {context.recommendation_reason && <p className={recovery || context.recommendation === 'blocked' ? 'notice' : 'text-sm'}>{context.recommendation_reason}</p>}
    {hasPlan && checkpoint && <>
      <p className="startup-context-next"><strong>Next in plan: Checkpoint {checkpoint.ordinal}{context.total_checkpoints !== null ? ` of ${context.total_checkpoints}` : ''} — {checkpointTitle}</strong></p>
      {context.recorded_complete_checkpoints !== null && <p className="text-sm">{context.recorded_complete_checkpoints} checkpoints marked complete in the plan.</p>}
      {context.pending_tasks.length > 0 && <ul className="startup-context-tasks">{context.pending_tasks.map((task, index) => <li key={`${index}:${task}`}>{task}</li>)}</ul>}
      {context.pending_tasks_truncated && <p className="text-xs text-dim">More tasks remain in the plan.</p>}
    </>}
    {hasPlan && !checkpoint && context.total_checkpoints !== null && <p className="text-sm">{context.recorded_complete_checkpoints !== null ? `${context.recorded_complete_checkpoints} of ${context.total_checkpoints} checkpoints are marked complete in the plan.` : `The plan has ${context.total_checkpoints} checkpoints; a completion count is not available.`} Review the plan before starting another run.</p>}
    {!hasPlan && <p className="notice">{context.reason ?? 'The plan could not be read safely.'} {context.availability === 'partial' ? 'The available facts may be incomplete.' : ''}</p>}
    {hasPlan && context.reason_codes.includes('inconsistent_checkpoint_state') && <p className="notice" role="alert">{context.reason ?? 'A completed checkpoint has unchecked tasks. Correct the plan before starting.'} Explicit recovery confirmation is required before starting.</p>}
    {context.availability === 'partial' && <p className="text-xs text-dim">This summary is partial; inspect the plan and earlier runs before acting.</p>}
    {context.selected_step && <p className="text-sm">{context.step_source === 'workflow_default' ? 'Configured starting step' : context.step_source === 'resume' ? 'Saved resume step' : 'Selected starting step'}: <strong>{formatMachineLabel(context.selected_step)}</strong>.</p>}
    {singleRelated && (recovery || active) && <div className="startup-context-related">
      <p className="text-sm"><strong>Earlier run:</strong> <span className="mono">{singleRelated.run_id}</span>{singleRelated.status ? ` · ${formatMachineLabel(singleRelated.status)}` : ''}</p>
      {relatedFacts(singleRelated).map(fact => <p key={fact} className="text-sm">{fact}</p>)}
      <div className="dashboard-actions">
        {onOpenRun && <button type="button" className="btn btn-primary" onClick={() => onOpenRun(singleRelated.run_id)}>{active ? 'Open running run' : 'Review previous run…'}</button>}
        {recovery && pendingCancellationAvailable && onCancelPendingStart && <button type="button" className="btn btn-secondary" onClick={() => onCancelPendingStart(singleRelated.run_id)}>Cancel pending start and open previous run…</button>}
      </div>
    </div>}
    {(context.recommendation === 'inspect_previous_runs' || (!singleRelated && context.related_runs.length > 0)) && <div className="startup-context-related">
      <p className="text-sm">Earlier run evidence is {context.related_runs_complete === false ? 'incomplete' : 'not enough to select a continuation automatically'}.</p>
      <ul>{context.related_runs.map(run => <li key={run.run_id}><span className="mono">{run.run_id}</span>{run.status ? ` · ${formatMachineLabel(run.status)}` : ''}{onOpenRun && <button type="button" className="btn btn-secondary btn-sm" onClick={() => onOpenRun(run.run_id)}>Inspect run</button>}</li>)}</ul>
    </div>}
    {planPath && <div className="dashboard-actions">
      {planAvailable && onOpenPlan ? <button type="button" className="btn btn-secondary btn-sm" onClick={() => onOpenPlan(planPath)}>Open plan</button>
        : <p className="text-xs text-dim">The plan is not available in this project's editable Plans list. Its recorded path is in Details.</p>}
    </div>}
    {context.checkpoints.length > 0 && <details open={outlineOpen} onToggle={event => setOutlineOpen(event.currentTarget.hasAttribute('open'))}><summary>Checkpoints ({context.checkpoints.length}{context.checkpoint_outline_truncated ? ' shown' : ''})</summary>
      <ol className="startup-context-outline">{context.checkpoints.map(item => <li key={item.ordinal}>{item.heading_checked ? 'Marked complete' : 'Pending'} · {item.title} · {item.checked_tasks} of {item.total_tasks} tasks checked</li>)}</ol>
    </details>}
    <details><summary>Details</summary><p className="text-xs text-dim">Plan path: <span className="mono">{planPath ?? 'Unavailable'}</span></p>{context.reason_codes.length > 0 && <p className="text-xs text-dim">Evidence: {context.reason_codes.join(', ')}</p>}</details>
  </section>
}
