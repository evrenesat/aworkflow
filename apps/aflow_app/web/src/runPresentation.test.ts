import { describe, expect, it } from 'vitest'
import type { GuidedFormProjection, RunProgressCount, RunProgressSummary, RunStatus } from './types'
import {
  checkpointApprovalText,
  executionDuration,
  formatLocalTimestamp,
  launchTeamFamilyGroups,
  launchTeamFamilyHint,
  launchTeamFamilyLabel,
  launchTeamFamilyRoute,
  launchTeamUpgradeRoute,
  launchTeamStageLabel,
  progressHistoryNotice,
  runActivityText,
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
