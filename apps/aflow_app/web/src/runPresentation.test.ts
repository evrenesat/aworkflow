import { describe, expect, it } from 'vitest'
import type { GuidedFormProjection, RunEvent, RunProgressCount, RunProgressSummary, RunStatus } from './types'
import {
  checkpointApprovalText,
  deliveryIssueText,
  executionDuration,
  formatLocalTimestamp,
  launchTeamFamilyGroups,
  launchTeamFamilyHint,
  launchTeamFamilyLabel,
  launchTeamFamilyRoute,
  launchTeamUpgradeRoute,
  launchTeamStageLabel,
  latestRunResultEvent,
  meaningfulRunEvents,
  presentRunEvent,
  progressHistoryNotice,
  runCurrentWorkText,
  runEventFacts,
  runExecutorFacts,
  runActivityText,
  runDisplayProjection,
  runFinishText,
  runPlanDisplayName,
  runPlanDisplayNameForRun,
  runPlanPresentation,
  runPlanPresentationForRun,
  runPlanPath,
  runTurnBudgetText,
  shortRunId,
  statusLabel,
} from './runPresentation'

const run = { status: 'manifest_only', ownership: 'control_plane', evidence: { manifest_created_at: '2026-09-08T10:00:00Z' } } as RunStatus
describe('honest run timing', () => {
  it('never uses submission time as execution time', () => {
    for (const status of ['manifest_only', 'awaiting_startup_answer', 'needs_attention', 'running', 'completed']) {
      for (const now of [Date.parse('2026-09-08T10:01:00Z'), Date.parse('2026-09-08T12:00:00Z')]) {
        expect(executionDuration({ ...run, status }, now)).toBeNull()
      }
    }
  })
  it('ticks only running execution and fixes terminal duration', () => {
    const started = { ...run, started_at: '2026-09-08T10:00:00Z', status: 'running' }
    expect(executionDuration(started, Date.parse('2026-09-08T10:01:00Z'))).toBe('1m 0s')
    expect(executionDuration(started, Date.parse('2026-09-08T10:02:00Z'))).toBe('2m 0s')
    const finished = { ...started, status: 'completed', ended_at: '2026-09-08T10:03:00Z' }
    expect(executionDuration(finished, Date.now())).toBe('3m 0s')
    expect(executionDuration(finished, Date.now() + 100000)).toBe('3m 0s')
    expect(executionDuration({ ...started, status: 'waiting_for_valid_override' }, Date.now())).toBeNull()
    expect(executionDuration({ ...finished, ended_at: null }, Date.now())).toBeNull()
  })
  it('distinguishes failure, questions, launch and runtime', () => {
    expect(statusLabel(run)).toBe('Needs attention')
    expect(statusLabel({ ...run, activity: 'active' })).toBe('Starting')
    expect(statusLabel({ ...run, status: 'awaiting_startup_answer' })).toBe('Input needed')
    expect(statusLabel({ ...run, status: 'needs_attention', evidence: { no_agent_started: true, startup_failure: { stage: 'preparation' } } })).toBe('Could not start')
    expect(statusLabel({ ...run, status: 'needs_attention', evidence: { startup_failure: { stage: 'unit_launch' } } })).toBe('Needs attention')
  })

  it('separates only the fully evidenced missing-outcome record from actionable attention', () => {
    const historicalGap: RunStatus = {
      run_id: '20260911t140026z-d06cc7d7',
      status: 'needs_attention',
      schema_version: 1,
      ownership: 'control_plane',
      revision: 0,
      status_reason_code: 'unit_missing',
      reason: 'No current workflow unit was found and no normal outcome was recorded.',
      activity: 'unknown',
      unit_name: 'aflow-run-20260911t140026z-d06cc7d7.service',
      launch_phase: 'unit_started',
      workflow_name: 'managed',
      team: 'base',
      current_step: null,
      turns_completed: null,
      max_turns: 8,
      selected_start_step: null,
      skipped_steps: [],
      restarted_from_run_id: null,
      plan_path: 'plans/in-progress/historical-plan.md',
      started_at: null,
      ended_at: null,
      evidence: {
        unit_observation: 'missing',
        has_run_metadata: false,
        can_resume: false,
      },
    }

    expect(runDisplayProjection(historicalGap)).toEqual({
      category: 'outcome-unrecorded',
      label: 'Outcome not recorded',
      tone: 'muted',
    })
    expect(statusLabel(historicalGap)).toBe('Outcome not recorded')
    expect(historicalGap.status).toBe('needs_attention')
    expect(historicalGap.status_reason_code).toBe('unit_missing')
    expect(historicalGap.reason).toContain('no normal outcome was recorded')
    expect(historicalGap.unit_name).toBe('aflow-run-20260911t140026z-d06cc7d7.service')
    expect(historicalGap.evidence.unit_observation).toBe('missing')
    expect(runDisplayProjection({ ...historicalGap, activity: 'inactive', evidence: { ...historicalGap.evidence, unit_active: false } }).category).toBe('outcome-unrecorded')

    const ambiguousVariants = [
      { ...historicalGap, status_reason_code: 'worker_attention' },
      { ...historicalGap, activity: 'active' },
      { ...historicalGap, evidence: { ...historicalGap.evidence, unit_observation: 'observed' } },
      { ...historicalGap, evidence: { ...historicalGap.evidence, unit_observation: 'unavailable' } },
      { ...historicalGap, evidence: { ...historicalGap.evidence, unit_observation: 'identity_mismatch' } },
      { ...historicalGap, evidence: { ...historicalGap.evidence, unit_observation: undefined } },
      { ...historicalGap, evidence: { ...historicalGap.evidence, unit_active: true } },
      { ...historicalGap, evidence: { ...historicalGap.evidence, can_resume: true } },
      { ...historicalGap, evidence: { ...historicalGap.evidence, has_run_metadata: true } },
      { ...historicalGap, activity: undefined },
    ] as RunStatus[]
    for (const variant of ambiguousVariants) {
      expect(runDisplayProjection(variant).category).toBe('actionable-attention')
      expect(statusLabel(variant)).toBe('Needs attention')
    }
    expect(runDisplayProjection({
      ...historicalGap,
      status_reason_code: 'startup_failed',
      evidence: { ...historicalGap.evidence, startup_failure: { stage: 'preparation' }, no_agent_started: true },
    })).toMatchObject({ category: 'failure', label: 'Could not start' })
  })

  it('keeps active phases, failures, input-needed runs, and legacy ownership distinct', () => {
    const reviewing = { ...run, status: 'running', activity: 'active', progress: { phase: 'reviewing' } } as RunStatus
    expect(runDisplayProjection(reviewing)).toMatchObject({ category: 'active', label: 'Running' })
    expect(runDisplayProjection({ ...run, status: 'launch_started', activity: 'active', evidence: { unit_active: true } })).toMatchObject({ category: 'active', label: 'Starting' })
    expect(runDisplayProjection({ ...run, status: 'failed', activity: 'inactive' })).toMatchObject({ category: 'failure', label: 'Failed' })
    expect(runDisplayProjection({ ...run, status: 'needs_attention', evidence: { no_agent_started: true, startup_failure: { stage: 'preparation' } } })).toMatchObject({ category: 'failure', label: 'Could not start' })
    expect(runDisplayProjection({ ...run, status: 'awaiting_startup_answer', ownership: 'legacy' })).toMatchObject({ category: 'input-needed', label: 'Input needed' })
    expect(runDisplayProjection({ ...run, status: 'running', ownership: 'legacy', activity: 'unknown' })).toMatchObject({ category: 'actionable-attention', label: 'Needs attention' })
  })
})

describe('run plan presentation', () => {
  it('uses a readable basename while preserving supplied casing after the initial character', () => {
    expect(runPlanDisplayName('/srv/work/plans/clear--Run_UI.md', 'run-1')).toBe('Clear Run UI')
    expect(runPlanDisplayName('C:\\work\\plans\\mixed_Name.MD', 'run-2')).toBe('Mixed Name')
  })

  it('falls back to the exact run id only when the plan name is absent', () => {
    expect(runPlanDisplayName(null, 'run-without-plan')).toBe('run-without-plan')
    expect(runPlanDisplayName('/srv/plans/.md', 'run-with-empty-name')).toBe('run-with-empty-name')
  })

  it('prefers canonical original identity when legacy list fields are absent', () => {
    const run = {
      run_id: 'run-canonical',
      status: 'running',
      ownership: 'control_plane',
      evidence: {},
      progress: {
        original_plan_display_name: 'canonical-plan.md',
        original_plan_path: '/srv/original/canonical-plan.md',
      },
    } as RunStatus
    expect(runPlanPath(run)).toBe('/srv/original/canonical-plan.md')
    expect(runPlanDisplayNameForRun(run)).toBe('Canonical plan')
  })

  it('uses the raw canonical identity before legacy plan fallback', () => {
    const run = {
      run_id: 'run-raw-canonical',
      status: 'completed',
      ownership: 'control_plane',
      evidence: { plan_path: 'plans/evidence-overlay.md' },
      plan_path: 'plans/active-overlay.md',
      original_plan_display_name: 'Hidden original title',
      original_plan_path: 'plans/in-progress/original.md',
    } as RunStatus
    expect(runPlanPath(run)).toBe('plans/active-overlay.md')
    expect(runPlanPresentationForRun(run)).toMatchObject({ label: 'Hidden original title', date: null, machineDerived: false })

    const fallback = { ...run, plan_path: null } as RunStatus
    expect(runPlanPath(fallback)).toBe('plans/evidence-overlay.md')
  })

  it('separates a valid machine date suffix from a readable title', () => {
    const presentation = runPlanPresentation('/srv/plans/run-history-readable-evidence-20260912.md', 'run-1')
    expect(presentation.label).toBe('Run history readable evidence')
    expect(presentation.date).toContain('2026')
    expect(runPlanPresentation('/srv/plans/title-20261301.md', 'run-2').label).toBe('Title 20261301')
  })

  it('keeps canonical custom titles and exact identities distinct', () => {
    const custom = {
      run_id: 'run-custom',
      status: 'completed',
      ownership: 'control_plane',
      evidence: {},
      progress: {
        original_plan_display_name: 'Quarterly review',
        original_plan_path: '/srv/plans/quarterly-review-20260912.md',
      },
    } as RunStatus
    expect(runPlanPresentationForRun(custom)).toMatchObject({ label: 'Quarterly review', date: null, machineDerived: false })
    expect(shortRunId('abcdefghijklmnop-qrstuvwxyz')).toBe('abcdefgh…stuvwxyz')
  })
})

const progressCount = (value: number | null, coverage: RunProgressCount['coverage'] = 'complete'): RunProgressCount => ({ value, coverage })
const progressSummary = (overrides: Partial<RunProgressSummary> = {}): RunProgressSummary => ({
  schema_version: 1,
  availability: 'complete',
  observed_at: null,
  evidence_at: null,
  reason_codes: [],
  original_plan_identity: null,
  original_plan_display_name: null,
  original_plan_path: null,
  total_checkpoints: progressCount(8),
  approved_checkpoints: progressCount(2),
  recorded_complete_checkpoints: progressCount(3),
  current_checkpoint_id: null,
  current_checkpoint_ordinal: null,
  current_checkpoint_title: null,
  activity: null,
  phase: null,
  run_status: null,
  current_executor: null,
  last_executor: null,
  worker_attempts: progressCount(0),
  repair_passes: progressCount(0),
  reviews: progressCount(0),
  runtime_retries: progressCount(0),
  applied_upgrades: progressCount(0),
  ...overrides,
})

describe('readable run evidence labels', () => {
  it('never turns unknown approval into a zero or slash placeholder', () => {
    expect(checkpointApprovalText(progressSummary())).toBe('2 of 8 checkpoints approved')
    expect(checkpointApprovalText(progressSummary({ approved_checkpoints: progressCount(null), total_checkpoints: progressCount(8) }))).toBe('8 checkpoints · approval unknown')
    expect(checkpointApprovalText(progressSummary({ approved_checkpoints: progressCount(2), total_checkpoints: progressCount(null) }))).toBe('2 approved · total unknown')
    expect(checkpointApprovalText(progressSummary({ availability: 'not_applicable' }))).toBe('Non-checkpoint workflow')
    expect(checkpointApprovalText(progressSummary({ approved_checkpoints: progressCount(2, 'partial'), total_checkpoints: progressCount(8, 'partial') }))).toBe('At least 2 of at least 8 checkpoints approved')
  })

  it('keeps turn limits, finish boundaries, and freshness explicit', () => {
    expect(runTurnBudgetText({ ...run, turns_completed: 2, max_turns: 8 })).toBe('2 of 8 turns used')
    expect(runTurnBudgetText({ ...run, turns_completed: 0, max_turns: 8 })).toBe('0 of 8 turns used')
    expect(runTurnBudgetText({ ...run, turns_completed: 2, max_turns: null })).toBe('2 turns used · turn limit unknown')
    expect(runTurnBudgetText({ ...run, turns_completed: 2, max_turns: 0 })).toBe('2 turns used · turn limit unknown')
    expect(runTurnBudgetText({ ...run, turns_completed: null, max_turns: 8 })).toBe('8-turn limit · turns used unknown')
    expect(runTurnBudgetText({ ...run, turns_completed: null, max_turns: null })).toBe('Turns used unknown · turn limit unknown')
    const failed = { ...run, status: 'failed', activity: 'inactive' as const, ended_at: '2026-09-12T12:00:00Z' }
    expect(runFinishText(failed)).toMatch(/^Finished /)
    expect(runActivityText(failed)).toMatch(/^Finished /)
    expect(formatLocalTimestamp('2026-09-12T12:00:00Z')).toMatch(/2026.*(?:UTC|GMT|[A-Z]{2,5})/)
    expect(progressHistoryNotice(progressSummary({ availability: 'partial' }))).toContain('partial')
  })
})

describe('run overview presentation', () => {
  const event = (sequence: number, eventType: string, data: Record<string, unknown> = {}): RunEvent => ({
    sequence,
    event_type: eventType,
    data,
    schema_version: 1,
    timestamp: `2026-09-12T12:0${sequence}:00Z`,
  })

  it('keeps current checkpoint position distinct from the recorded result', () => {
    expect(runCurrentWorkText(progressSummary({
      current_checkpoint_ordinal: 4,
      current_checkpoint_title: 'Repair the worker',
      phase: 'reviewing',
      total_checkpoints: progressCount(8),
    }))).toBe('CP4 of 8 · Reviewing — Repair the worker')
    expect(runCurrentWorkText(progressSummary({ phase: 'running', current_checkpoint_ordinal: null }))).toBe('Running · current checkpoint not reported')
    expect(runCurrentWorkText(progressSummary({ phase: 'unit_started', current_checkpoint_ordinal: 4, current_checkpoint_title: 'Repair the worker', total_checkpoints: progressCount(8) }), 'review')).toBe('CP4 of 8 · Review — Repair the worker')
    expect(runCurrentWorkText(progressSummary({ phase: 'reviewing', current_checkpoint_ordinal: 4, current_checkpoint_title: 'Checkpoint 4: Repair the worker', total_checkpoints: progressCount(8) }))).toBe('CP4 of 8 · Reviewing — Repair the worker')
  })

  it('describes non-checkpoint work without inventing a missing checkpoint', () => {
    expect(runCurrentWorkText(progressSummary({ availability: 'not_applicable', phase: 'reviewing' }))).toBe('Reviewing')
    expect(runCurrentWorkText(progressSummary({ availability: 'not_applicable', phase: null }))).toBe('Non-checkpoint workflow')
  })

  it('bounds recent activity newest first and uses stored event text', () => {
    const events = [
      event(1, 'run_started'),
      event(2, 'turn_finished', { status: 'completed', outcome: 'first result' }),
      event(3, 'turn_started', { status: 'running' }),
      event(4, 'control_changed', { reason: 'owner changed the team' }),
      event(5, 'turn_finished', { status: 'failed', reason: 'provider unavailable' }),
      event(6, 'turn_started', { status: 'running' }),
    ]
    expect(meaningfulRunEvents(events, 5).map(item => item.sequence)).toEqual([6, 5, 4, 3, 2])
    expect(presentRunEvent(events[4])).toMatchObject({ label: 'Turn finished', summary: 'provider unavailable' })
    expect(latestRunResultEvent(events)?.sequence).toBe(5)
  })

  it('keeps terminal failure, stop and success results distinct', () => {
    expect(presentRunEvent(event(7, 'run_failed', { failure_reason: 'worker crashed' })).summary).toBe('worker crashed')
    expect(presentRunEvent(event(8, 'owner_stopped', { end_reason: 'owner_stopped' })).summary).toBe('Owner stopped')
    expect(presentRunEvent(event(9, 'run_completed', { end_reason: 'completed' })).summary).toBe('Completed')
    expect(presentRunEvent(event(10, 'run_failed')).summary).toBe('Failure reason unavailable.')
    expect(presentRunEvent(event(11, 'owner_stopped')).summary).toBe('Stop reason unavailable.')
    expect(presentRunEvent(event(12, 'run_completed')).summary).toBe('Recorded result unavailable.')
  })

  it('omits lifecycle noise when meaningful work evidence is available', () => {
    const events = [
      event(1, 'run_started'),
      event(2, 'daemon_unit_started'),
      event(3, 'status_changed', { status: 'running' }),
      event(4, 'heartbeat'),
      event(5, 'turn_started', { step_name: 'implement', step_role: 'worker', turn_number: 2 }),
      event(6, 'review_rejected', { step_name: 'review', step_role: 'reviewer', turn_number: 3, reason: 'Needs a repair' }),
    ]

    expect(meaningfulRunEvents(events).map(item => item.sequence)).toEqual([6, 5])
    expect(runEventFacts(events[4])).toEqual(['Implement', 'Worker', 'turn 2'])
    expect(presentRunEvent(events[4])).toMatchObject({ recordedAt: '12:05 PM', hasPayload: true })
  })

  it('keeps executor facts and delivery failures concise', () => {
    expect(runExecutorFacts({
      role: 'reviewer', team: 'full', selector: 'codex.review', harness: 'codex', model: 'gpt-5', model_display: 'GPT-5', effort: 'high',
      source_run_id: 'run-1', invocation_id: 'inv-1', turn_number: 4, started_at: null, ended_at: null, duration_seconds: null,
    })).toEqual(['Reviewer', 'Full', 'GPT-5', 'turn 4'])
    expect(deliveryIssueText([
      { stage: 'ci', status: 'unknown', recorded_at: null, reason: 'No receipt', source_reference: null },
      { stage: 'publish', status: 'failed', recorded_at: '2026-09-12T12:00:00Z', reason: 'push rejected', source_reference: null },
    ])).toBe('Publish failed — push rejected')
    expect(deliveryIssueText([{ stage: 'ci', status: 'unknown', recorded_at: null, reason: null, source_reference: null }])).toBeNull()
  })
})

const familyProjection: GuidedFormProjection = {
  default_workflow: 'managed',
  max_turns: null,
  harnesses: {},
  roles: { worker: 'codex.global', reviewer: 'codex.review' },
  teams: {
    product: {
      roles: { worker: 'codex.base' },
      display_name: 'Product',
      upgrade_to: 'product_fast',
      extends: null,
      effective_roles: { worker: 'codex.base', reviewer: 'codex.review' },
      role_sources: { worker: 'product', reviewer: 'global' },
    },
    product_fast: {
      roles: { worker: 'codex.fast' },
      display_name: 'Fast',
      upgrade_to: null,
      extends: 'product',
      effective_roles: { worker: 'codex.fast', reviewer: 'codex.review' },
      role_sources: { worker: 'product_fast', reviewer: 'global' },
    },
  },
  workflow_default_teams: { managed: 'product_fast' },
  workflows: {},
}

describe('launch team family presentation', () => {
  it('keeps the server team IDs while presenting the ordered family route', () => {
    const groups = launchTeamFamilyGroups(familyProjection, ['server_only'])
    const product = groups.find((group) => group.rootId === 'product')!
    const standalone = groups.find((group) => group.rootId === 'server_only')!

    expect(product.memberIds).toEqual(['product', 'product_fast'])
    expect(launchTeamFamilyRoute(product)).toEqual(['product', 'product_fast'])
    expect(launchTeamFamilyLabel(product)).toBe('Product')
    expect(launchTeamStageLabel(familyProjection, product, 'product')).toBe('Base')
    expect(launchTeamStageLabel(familyProjection, product, 'product_fast')).toBe('Fast')
    expect(launchTeamFamilyHint(familyProjection, product)).toContain('Worker: codex.base')
    expect(launchTeamFamilyHint(familyProjection, product)).toContain('Reviewer: codex.review')
    expect(launchTeamFamilyHint(familyProjection, product)).toContain('Upgrade route: Base → Fast')
    expect(standalone.kind).toBe('standalone')
    expect(standalone.memberIds).toEqual(['server_only'])
  })

  it('retains every configured team as a reachable standalone when no projection is available', () => {
    const groups = launchTeamFamilyGroups(null, ['legacy', 'complex_child'])
    expect(groups.map((group) => group.rootId)).toEqual(['complex_child', 'legacy'])
    expect(groups.every((group) => group.kind === 'standalone')).toBe(true)
  })

  it('does not turn family membership into an upgrade route', () => {
    const noEdges = structuredClone(familyProjection)
    noEdges.teams.product.upgrade_to = null
    noEdges.teams.product_fast.upgrade_to = null
    const product = launchTeamFamilyGroups(noEdges).find((group) => group.rootId === 'product')!

    expect(launchTeamUpgradeRoute(noEdges, 'product_fast')).toEqual(['product_fast'])
    expect(launchTeamFamilyHint(noEdges, product)).toContain('Members: Base, Fast')
    expect(launchTeamFamilyHint(noEdges, product)).not.toContain('Base → Fast')
  })

  it('follows declared upgrades from a selected child, including an external target', () => {
    const withExternal = structuredClone(familyProjection)
    withExternal.teams.product_fast.upgrade_to = 'external'
    withExternal.teams.external = {
      roles: { worker: 'codex.external' },
      display_name: 'External',
      upgrade_to: null,
      extends: null,
    }

    expect(launchTeamUpgradeRoute(withExternal, 'product_fast')).toEqual(['product_fast', 'external'])
    const product = launchTeamFamilyGroups(withExternal).find((group) => group.rootId === 'product')!
    expect(launchTeamFamilyHint(withExternal, product)).toContain('Fast → External')
  })
})
