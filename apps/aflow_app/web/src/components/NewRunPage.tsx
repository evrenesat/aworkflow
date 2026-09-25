import { useEffect, useId, useRef, type ReactNode, type Dispatch, type SetStateAction } from 'react'
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
  const acknowledgmentRequired = status !== 'idle' && result !== null && Boolean(result.requires_confirmation || dirtyQuestionMessage)
  const canShowResult = result !== null && status !== 'idle'
  const returnedPathCount = result ? result.items.length : 0
  const reportedChangeCount = result ? Math.max(result.total_items, returnedPathCount) : 0
  const changeLabel = reportedChangeCount === 1 ? 'change' : 'changes'
  const changedFilesSummary = result?.dirty
    ? returnedPathCount === 0
      ? reportedChangeCount > 0
        ? `${reportedChangeCount} uncommitted ${changeLabel} detected; file names were not returned.`
        : 'Uncommitted changes detected; file names were not returned.'
      : returnedPathCount < reportedChangeCount
        ? `${reportedChangeCount} uncommitted ${changeLabel} detected; ${returnedPathCount} returned so far.`
        : `${reportedChangeCount} uncommitted ${changeLabel} detected.`
    : 'No uncommitted changes detected.'
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
      {status === 'error' && <div className="error-message" role="alert">Working tree inspection failed: {error ?? 'Refresh to retry.'} Start is blocked until inspection succeeds.</div>}
      {canShowResult && result && (
        <>
          <p className="text-sm text-dim">
            {result.execution_mode === 'new_worktree'
              ? 'Execution mode: new worktree — starts from the selected commit; any uncommitted changes stay in the current checkout.'
              : 'Execution mode: existing checkout — uses the current checkout; acknowledge any uncommitted changes before continuing.'}
          </p>
          <details className="worktree-checkout-details">
            <summary>Checkout details</summary>
            <p className="text-sm text-dim">
              Checkout: <span className="mono">{result.checkout_path}</span>
            </p>
          </details>
          {result.blockers.length > 0 && (
            <div className="error-message" role="alert">
              <strong>{status === 'error' ? 'Last successful inspection reported blockers:' : 'Preflight blocks this start:'}</strong>
              <ul>{result.blockers.map((blocker) => <li key={blocker}>{blocker}</li>)}</ul>
            </div>
          )}
          {(status === 'ready' || status === 'error') && <p className={result.dirty ? 'notice' : 'text-sm'}>{status === 'error' ? 'Last successful inspection: ' : ''}{changedFilesSummary}</p>}
          {result.items.length > 0 && (
            <details className="worktree-changed-files">
              <summary>Show changed files ({returnedPathCount}{returnedPathCount < reportedChangeCount ? ` of ${reportedChangeCount}` : ''})</summary>
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
              {result.next_offset !== null && (
                <button type="button" className="btn btn-secondary btn-sm" onClick={onLoadMore} disabled={status === 'loading'}>
                  {status === 'loading' ? 'Loading more…' : `Show more (${Math.max(result.total_items - result.next_offset, 0)} remaining)`}
                </button>
              )}
            </details>
          )}
        </>
      )}
      {dirtyQuestionMessage && <div className="notice" role="alert">{dirtyQuestionMessage}</div>}
      {acknowledgmentRequired && (
        <label className="worktree-confirmation">
          <input
            type="checkbox"
            checked={dirtyWorktreeConfirmed}
            disabled={status !== 'ready'}
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
  startTeamFamily: string
  setStartTeamFamily: (value: string) => void
  teamFamilyOptions: string[]
  teamFamilyBadges: Record<string, string>
  teamFamilyOptionLabel: (value: string) => string
  teamFamilyOptionHint?: (value: string) => string | null | undefined
  teamFamilyPresentation: LaunchSelectorPresentation
  startTeamStage: string
  setStartTeamStage: (value: string) => void
  teamStageOptions: string[]
  teamStageBadges: Record<string, string>
  teamStageOptionLabel: (value: string) => string
  teamStageOptionHint?: (value: string) => string | null | undefined
  teamStagePresentation: LaunchSelectorPresentation
  startMaxTurns: string
  setStartMaxTurns: (value: string) => void
  startMaxTurnsProblem: string | null
  configuredMaxTurns: number | null
  serverDefaultMaxTurns: number | null
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
  reviewOpen: boolean
  review: ReactNode
  onOpenStartReview: (trigger: HTMLElement) => void
  onCancelStartReview: () => void
  onConfirmStart: () => Promise<void>
  startActionLabel: string
  reviewReady: boolean
  startDisabled: boolean
  startRevalidationPending: boolean
  busyAction: string | null
  /** The hosted shell renders Start/Cancel in its shared row-2 slots. */
  hideActions?: boolean
}

/** Presentation only; the workspace retains request and answer identity across navigation. */
export function NewRunPage({ startPlanPath, setStartPlanPath, planOptions, planBadges, restartDraftFrozen, startWorkflow, changeStartWorkflow, workflowOptions, workflowBadges, workflowPresentation, startTeamFamily, setStartTeamFamily, teamFamilyOptions, teamFamilyBadges, teamFamilyOptionLabel, teamFamilyOptionHint, teamFamilyPresentation, startTeamStage, setStartTeamStage, teamStageOptions, teamStageBadges, teamStageOptionLabel, teamStageOptionHint, teamStagePresentation, startMaxTurns, setStartMaxTurns, startMaxTurnsProblem, configuredMaxTurns, serverDefaultMaxTurns, preview, worktreePreflight, restartActions, onCancel, advancedOpen, setAdvancedOpen, startStep, setStartStep, effectiveWorkflow, runSteps, skippedByDraft, startExtraInstructions, setStartExtraInstructions, extraInstructionProblem, launchBlocker, onOpenSettings, reviewOpen, review, onOpenStartReview, onCancelStartReview, onConfirmStart, startActionLabel, reviewReady, startDisabled, startRevalidationPending, busyAction, hideActions = false }: NewRunPageProps) {
  const advancedId = useId()
  const reviewHeadingRef = useRef<HTMLHeadingElement>(null)
  const advancedIndicators = [
    startTeamStage.trim() ? `Baseline stage: ${teamStageOptionLabel(startTeamStage.trim())}` : null,
    startStep.trim() ? `Start at: ${formatMachineLabel(startStep.trim())}` : null,
    startExtraInstructions.trim() ? 'Extra instructions added' : null,
  ].filter((indicator): indicator is string => Boolean(indicator))

  useEffect(() => {
    if (!reviewOpen) return
    const heading = reviewHeadingRef.current
    if (!heading) return
    const reducedMotion = typeof window.matchMedia === 'function'
      && window.matchMedia('(prefers-reduced-motion: reduce)').matches
    heading.scrollIntoView?.({ behavior: reducedMotion ? 'auto' : 'smooth', block: 'start' })
    heading.focus({ preventScroll: true })
  }, [reviewOpen])

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
                    visibleLabel="Team family"
                    value={startTeamFamily}
                    onChange={setStartTeamFamily}
                    options={teamFamilyOptions}
                    optionBadges={teamFamilyBadges}
                    optionLabel={teamFamilyOptionLabel}
                    optionHint={teamFamilyOptionHint}
                    disabled={restartDraftFrozen}
                    placeholder="Search team families"
                    resolvedDisplay={teamFamilyPresentation.resolvedDisplay}
                    resolvedBadge={teamFamilyPresentation.resolvedBadge}
                    defaultOption={{ value: '', label: teamFamilyPresentation.defaultLabel, hint: teamFamilyPresentation.defaultHint }}
                  />
                </div>
                <label className="dashboard-field">
                  <span>Maximum turns</span>
                  <input className="input" aria-label="Run max turns" type="number" min="1" value={startMaxTurns} disabled={restartDraftFrozen} onChange={(event) => setStartMaxTurns(event.target.value)} />
                  {startMaxTurnsProblem
                    ? <span className="error-message" role="alert">{startMaxTurnsProblem}</span>
                    : <span className="text-xs text-dim">{configuredMaxTurns !== null
                      ? `Global default: ${configuredMaxTurns}.`
                      : serverDefaultMaxTurns !== null
                        ? `Server default: ${serverDefaultMaxTurns}.`
                        : 'No global default max turns is configured.'}</span>}
                </label>

              </div>
              <section className="dashboard-section launch-advanced-options">
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
                {advancedIndicators.length > 0 && <p className="text-xs text-dim launch-advanced-indicator">Changed: {advancedIndicators.join(' · ')}</p>}
                {advancedOpen && (
                  <div id={advancedId} className="start-run-form">
                    {teamStageOptions.length > 1 && <div className="dashboard-field">
                      <Combobox
                        label="Run team stage"
                        visibleLabel="Baseline stage"
                        value={startTeamStage}
                        onChange={setStartTeamStage}
                        options={teamStageOptions}
                        optionBadges={teamStageBadges}
                        optionLabel={teamStageOptionLabel}
                        optionHint={teamStageOptionHint}
                        disabled={restartDraftFrozen}
                        placeholder="Search stages"
                        resolvedDisplay={teamStagePresentation.resolvedDisplay}
                        resolvedBadge={teamStagePresentation.resolvedBadge}
                        defaultOption={{ value: '', label: teamStagePresentation.defaultLabel, hint: teamStagePresentation.defaultHint }}
                      />
                      <span className="text-xs text-dim">The selected stage submits its exact configured team ID.</span>
                    </div>}
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
              {preview}
              {worktreePreflight}
              {restartActions}
              {reviewOpen && <section className="dashboard-section launch-review" aria-label="Review start" role="region">
                <div className="section-heading">
                  <h4 ref={reviewHeadingRef} className="launch-review-heading" tabIndex={-1}>Review before starting</h4>
                  <span className="text-xs text-dim">Read-only check — no run has been allocated.</span>
                </div>
                {review}
                <div className="dashboard-actions">
                  {!hideActions && <button
                    className="btn btn-primary"
                    onClick={() => void onConfirmStart()}
                    disabled={startDisabled || !reviewReady || startRevalidationPending || Boolean(restartActions)}
                  >
                    {busyAction === 'start' ? 'Starting…' : startRevalidationPending ? 'Checking…' : 'Start run'}
                  </button>}
                  <button className="btn btn-secondary" onClick={onCancelStartReview} disabled={busyAction === 'start'}>Cancel review</button>
                </div>
              </section>}
              {!reviewOpen && !hideActions && <div className="dashboard-actions">
                <button
                  data-launch-review-trigger="true"
                  className="btn btn-primary"
                  onClick={(event) => onOpenStartReview(event.currentTarget)}
                  disabled={startDisabled || Boolean(restartActions)}
                >
                  {startActionLabel}
                </button>
                <button className="btn btn-secondary" onClick={onCancel}>Cancel</button>
              </div>}
            </div>
        </section>
  )
}
