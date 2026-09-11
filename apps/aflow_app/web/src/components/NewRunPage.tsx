import { useId, type ReactNode, type Dispatch, type SetStateAction } from 'react'
import type { WorktreePreflight, WorktreeStatusItem } from '../types'
import { formatMachineChoice, formatMachineLabel } from '../label'
import { Combobox } from './Combobox'

export type WorktreePreflightLoadState = 'idle' | 'loading' | 'ready' | 'error'

interface WorktreePreflightPanelProps {
  status: WorktreePreflightLoadState
  result: WorktreePreflight | null
  error: string | null
  dirtyWorktreeConfirmed: boolean
  onDirtyWorktreeConfirmedChange: (confirmed: boolean) => void
  onRefresh: () => void
  onLoadMore: () => void
  dirtyQuestionMessage: string | null
}

function worktreeStatusName(status: string): string {
  return {
    A: 'added',
    C: 'copied',
    D: 'deleted',
    M: 'modified',
    R: 'renamed',
    T: 'type changed',
    U: 'unmerged',
  }[status] ?? 'changed'
}

function worktreeItemDescription(item: WorktreeStatusItem): string {
  const code = `${item.index_status}${item.worktree_status}`
  if (code === '??' || (item.index_status === '?' && item.worktree_status === '?')) return 'untracked'
  const descriptions: string[] = []
  if (item.index_status.trim() && item.index_status !== '?') descriptions.push(`staged ${worktreeStatusName(item.index_status)}`)
  if (item.worktree_status.trim() && item.worktree_status !== '?') descriptions.push(`unstaged ${worktreeStatusName(item.worktree_status)}`)
  return descriptions.join(' + ') || 'changed'
}

export function WorktreePreflightPanel({ status, result, error, dirtyWorktreeConfirmed, onDirtyWorktreeConfirmedChange, onRefresh, onLoadMore, dirtyQuestionMessage }: WorktreePreflightPanelProps) {
  const acknowledgmentRequired = status === 'ready' && Boolean(result?.requires_confirmation || dirtyQuestionMessage)
  const canShowResult = result !== null && status !== 'idle' && status !== 'error'
  return (
    <section
      className="dashboard-section worktree-preflight"
      aria-label="Working tree preflight"
      aria-busy={status === 'loading'}
      data-preflight-status={status}
    >
      <div className="section-heading">
        <h4>Working tree before start</h4>
        <button type="button" className="btn btn-secondary btn-sm" onClick={onRefresh} disabled={status === 'loading'}>
          {status === 'loading' ? 'Inspecting…' : 'Refresh worktree inspection'}
        </button>
      </div>
      {status === 'idle' && <p className="text-sm text-dim">Choose a Ready plan and valid workflow to inspect the working tree.</p>}
      {status === 'loading' && <p className="text-sm text-dim" role="status">Inspecting the selected checkout…</p>}
      {status === 'error' && <div className="error-message" role="alert">Working tree inspection failed: {error ?? 'Refresh to retry.'}</div>}
      {canShowResult && result && (
        <>
          <p className="text-sm text-dim">
            Checkout: <span className="mono">{result.checkout_path}</span>
          </p>
          <p className="text-sm text-dim">
            {result.execution_mode === 'new_worktree'
              ? 'A new worktree starts from the selected commit and leaves these changes in the current checkout.'
              : 'This run uses the existing checkout, so it sees the uncommitted changes listed here.'}
          </p>
          {result.blockers.length > 0 && (
            <div className="error-message" role="alert">
              <strong>Preflight blocks this start:</strong>
              <ul>{result.blockers.map((blocker) => <li key={blocker}>{blocker}</li>)}</ul>
            </div>
          )}
          {status === 'ready' && !result.dirty && <p>No uncommitted changes detected.</p>}
          {result.dirty && result.items.length === 0 && (
            <p className="notice">The checkout has uncommitted changes, but no individual paths were returned.</p>
          )}
          {result.items.length > 0 && (
            <ul className="worktree-preflight-list">
              {result.items.map((item) => (
                <li key={`${item.path}:${item.original_path ?? ''}`}>
                  <span className="status-pill">{worktreeItemDescription(item)}</span>{' '}
                  <span className="mono">{item.path}</span>
                  <span className="text-xs text-dim"> ({item.index_status}{item.worktree_status})</span>
                  {item.original_path && <span className="text-sm text-dim"> — renamed from <span className="mono">{item.original_path}</span></span>}
                </li>
              ))}
            </ul>
          )}
          {result.next_offset !== null && (
            <button type="button" className="btn btn-secondary btn-sm" onClick={onLoadMore} disabled={status === 'loading'}>
              {status === 'loading' ? 'Loading more…' : `Show more (${Math.max(result.total_items - result.next_offset, 0)} remaining)`}
            </button>
          )}
        </>
      )}
      {dirtyQuestionMessage && <div className="notice" role="alert">{dirtyQuestionMessage}</div>}
      {acknowledgmentRequired && (
        <label className="worktree-confirmation">
          <input
            type="checkbox"
            checked={dirtyWorktreeConfirmed}
            onChange={(event) => onDirtyWorktreeConfirmedChange(event.target.checked)}
          />
          Continue despite uncommitted changes
        </label>
      )}
    </section>
  )
}

interface LaunchSelectorPresentation {
  /** Resolved value shown before focus when the stored value is empty. */
  resolvedDisplay: string | null
  /** Badge beside the resolved value (for example "Default"). */
  resolvedBadge?: string
  /** Label/hint of the explicit default row in the open list. */
  defaultLabel: string
  defaultHint: string
}

interface NewRunPageProps {
  startPlanPath: string
  setStartPlanPath: (value: string) => void
  planOptions: string[]
  planBadges: Record<string, string>
  restartDraftFrozen: boolean
  startWorkflow: string
  changeStartWorkflow: (value: string) => void
  workflowOptions: string[]
  workflowBadges: Record<string, string>
  workflowPresentation: LaunchSelectorPresentation
  startTeam: string
  setStartTeam: (value: string) => void
  teamOptions: string[]
  teamBadges: Record<string, string>
  teamPresentation: LaunchSelectorPresentation
  startMaxTurns: string
  setStartMaxTurns: (value: string) => void
  startMaxTurnsProblem: string | null
  configuredMaxTurns: number | null
  preview: ReactNode
  worktreePreflight: ReactNode
  restartActions: ReactNode
  onCancel: () => void
  advancedOpen: boolean
  setAdvancedOpen: Dispatch<SetStateAction<boolean>>
  startStep: string
  setStartStep: (value: string) => void
  effectiveWorkflow: string
  runSteps: string[]
  skippedByDraft: string[]
  startExtraInstructions: string
  setStartExtraInstructions: (value: string) => void
  extraInstructionProblem: string | null
  launchBlocker: string | null
  onOpenSettings: (() => void) | undefined
  handleStart: () => Promise<void>
  startDisabled: boolean
  busyAction: string | null
  /** The hosted shell renders Start/Cancel in its shared row-2 slots. */
  hideActions?: boolean
}

/** Presentation only; the workspace retains request and answer identity across navigation. */
export function NewRunPage({ startPlanPath, setStartPlanPath, planOptions, planBadges, restartDraftFrozen, startWorkflow, changeStartWorkflow, workflowOptions, workflowBadges, workflowPresentation, startTeam, setStartTeam, teamOptions, teamBadges, teamPresentation, startMaxTurns, setStartMaxTurns, startMaxTurnsProblem, configuredMaxTurns, preview, worktreePreflight, restartActions, onCancel, advancedOpen, setAdvancedOpen, startStep, setStartStep, effectiveWorkflow, runSteps, skippedByDraft, startExtraInstructions, setStartExtraInstructions, extraInstructionProblem, launchBlocker, onOpenSettings, handleStart, startDisabled, busyAction, hideActions = false }: NewRunPageProps) {
  const advancedId = useId()
  return (
        <section className="card start-run-form">
            <div className="start-run-form">
              <div className="dashboard-field">
                <Combobox
                  label="Run plan"
                  visibleLabel="Plan"
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
                    visibleLabel="Workflow"
                    value={startWorkflow}
                    onChange={changeStartWorkflow}
                    options={workflowOptions}
                    optionBadges={workflowBadges}
                    optionLabel={(workflow) => formatMachineChoice(workflow, workflowOptions)}
                    disabled={restartDraftFrozen}
                    placeholder="Search workflows"
                    resolvedDisplay={workflowPresentation.resolvedDisplay}
                    resolvedBadge={workflowPresentation.resolvedBadge}
                    defaultOption={{ value: '', label: workflowPresentation.defaultLabel, hint: workflowPresentation.defaultHint }}
                  />
                </div>
                <div className="dashboard-field">
                  <Combobox
                    label="Run team"
                    visibleLabel="Team"
                    value={startTeam}
                    onChange={setStartTeam}
                    options={teamOptions}
                    optionBadges={teamBadges}
                    optionLabel={(team) => formatMachineChoice(team, teamOptions)}
                    disabled={restartDraftFrozen}
                    placeholder="Search teams"
                    resolvedDisplay={teamPresentation.resolvedDisplay}
                    resolvedBadge={teamPresentation.resolvedBadge}
                    defaultOption={{ value: '', label: teamPresentation.defaultLabel, hint: teamPresentation.defaultHint }}
                  />
                </div>

              </div>
              {preview}
              {worktreePreflight}
              <section className="dashboard-section">
                <h4>
                  <button
                    type="button"
                    className="disclosure-toggle"
                    aria-label="Advanced options"
                    aria-expanded={advancedOpen}
                    aria-controls={advancedId}
                    onClick={() => setAdvancedOpen((open) => !open)}
                  >
                    Advanced options
                  </button>
                </h4>
                {advancedOpen && (
                  <div id={advancedId} className="start-run-form">
                <label className="dashboard-field"><span>Max turns</span>
                  <input className="input" aria-label="Run max turns" type="number" min="1" value={startMaxTurns} disabled={restartDraftFrozen} onChange={(event) => setStartMaxTurns(event.target.value)} />
                  {startMaxTurnsProblem
                    ? <span className="error-message" role="alert">{startMaxTurnsProblem}</span>
                    : <span className="text-xs text-dim">{configuredMaxTurns !== null ? `Global default: ${configuredMaxTurns}.` : 'No global default max turns is configured.'}</span>}
                </label>
                    <label className="dashboard-field"><span>Start step</span>
                      <select className="input" aria-label="Run start step" value={startStep} onChange={(event) => setStartStep(event.target.value)} disabled={!effectiveWorkflow || restartDraftFrozen}>
                        <option value="">{effectiveWorkflow ? 'Workflow first step (default)' : 'Select a workflow first'}</option>
                        {runSteps.map((step, index) => (
                          <option key={step} value={step}>{index + 1} · {formatMachineLabel(step)}</option>
                        ))}
                      </select>
                    </label>
                    {skippedByDraft.length > 0 && (
                      <div className="notice">
                        Starting at <strong>{formatMachineLabel(startStep)}</strong> skips the earlier executable steps:{' '}
                        <span className="mono">{skippedByDraft.map(formatMachineLabel).join(', ')}</span>. They are recorded as skipped, not executed.
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
              {restartActions}
              {!hideActions && <div className="dashboard-actions">
                <button
                  className="btn btn-primary"
                  onClick={() => void handleStart()}
                  disabled={startDisabled || Boolean(restartActions)}
                >
                  {busyAction === 'start' ? 'Starting…' : 'Start run'}
                </button>
                <button className="btn btn-secondary" onClick={onCancel}>Cancel</button>
              </div>}
            </div>
        </section>
  )
}
