import { describe, expect, it } from 'vitest'
import type { GuidedFormProjection, RunStatus } from './types'
import {
  executionDuration,
  launchTeamFamilyGroups,
  launchTeamFamilyHint,
  launchTeamFamilyLabel,
  launchTeamFamilyRoute,
  launchTeamUpgradeRoute,
  launchTeamStageLabel,
  runPlanDisplayName,
  runPlanDisplayNameForRun,
  runPlanPath,
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
