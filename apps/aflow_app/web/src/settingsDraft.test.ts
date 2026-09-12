import { describe, expect, it } from 'vitest'
import { changedDocuments, createDraftPreviewCoordinator, reconcileCleanPreview, settingsActions } from './settingsDraft'
import type { GuidedFormProjection } from './types'

export const baseline: GuidedFormProjection = {
  default_workflow: 'demo', max_turns: 5,
  harnesses: { codex: { worker: { model: 'model', effort: 'high' } } },
  roles: { worker: 'codex.worker' }, teams: { crew: { roles: {} } },
  workflow_default_teams: { demo: null, 'demo-alias': null },
  workflows: {
    demo: { declared_steps: ['work'], first_step: 'work', executable_steps: ['work'], first_executable_step: 'work', manager_enabled: null, effective_manager_enabled: false, manager_enabled_source: 'defaults' },
    'demo-alias': { declared_steps: ['work'], first_step: 'work', executable_steps: ['work'], first_executable_step: 'work', manager_enabled: null, effective_manager_enabled: false, manager_enabled_source: 'base:demo' },
  },
  prompts: { work: 'hello' }, role_prompts: {},
  default_manager_enabled: null,
}
describe('changed-only settings', () => {
  it('sends only effort with the profile identity', () => {
    const draft = structuredClone(baseline)
    draft.harnesses.codex.worker.effort = 'custom'
    expect(settingsActions(baseline, draft)).toEqual([{ type: 'upsert_profile', harness: 'codex', profile: 'worker', effort: 'custom' }])
    draft.harnesses.codex.worker.effort = 'high'
    expect(settingsActions(baseline, draft)).toEqual([])
  })
  it('sends one prompt operation and explicit override deletions', () => {
    const draft = structuredClone(baseline)
    draft.prompts!.work = 'λ\n{literal}'
    expect(settingsActions(baseline, draft)).toEqual([{ type: 'set_prompt', name: 'work', text: 'λ\n{literal}' }])
    expect(settingsActions({ ...baseline, role_prompts: { worker: 'override' } }, baseline)).toEqual([{ type: 'set_role_prompt', role: 'worker', text: null }])
  })
  it('orders add_team before dependent edits and emits only changed upgrade links', () => {
    const draft = structuredClone(baseline)
    draft.teams['stage-two'] = { roles: { worker: 'codex.worker' }, prompts: {}, upgrade_to: null }
    draft.teams.crew = { roles: {}, prompts: {}, upgrade_to: 'stage-two' }
    const actions = settingsActions(baseline, draft)
    expect(actions[0]).toEqual({ type: 'add_team', team: 'stage-two' })
    expect(actions).toContainEqual({ type: 'set_team_role', team: 'stage-two', role: 'worker', selector: 'codex.worker' })
    expect(actions).toContainEqual({ type: 'set_team_upgrade', team: 'crew', upgrade_to: 'stage-two' })
    // No churn for unchanged or untouched links.
    expect(actions).not.toContainEqual({ type: 'set_team_upgrade', team: 'stage-two', upgrade_to: null })
    // Removing a link is an explicit null upgrade.
    const cleared = structuredClone(draft)
    cleared.teams.crew.upgrade_to = null
    expect(settingsActions(draft, cleared)).toEqual([{ type: 'set_team_upgrade', team: 'crew', upgrade_to: null }])
  })
  it('omits untouched Advanced documents', () => {
    expect(changedDocuments({ aflow_toml: 'a', workflows_toml: 'b' }, ['changed', 'b'])).toEqual({ 'aflow.toml': 'changed' })
  })
  it('emits no manager action merely from loading an omitted default', () => {
    expect(settingsActions(baseline, structuredClone(baseline))).toEqual([])
  })
  it('keeps read-only template help metadata out of save actions', () => {
    const draft = structuredClone(baseline)
    draft.template_variables = [{
      token: '{NEXT_CP}',
      description: 'Checkpoint index',
      scope: 'Workflow prompts',
      absent_value: '-',
      example: '2',
      applicable_prompt_types: ['workflow step prompt'],
    }]
    expect(settingsActions(baseline, draft)).toEqual([])
  })
  it('emits net manager changes with presence semantics, never truthiness', () => {
    const enabled = structuredClone(baseline)
    enabled.default_manager_enabled = true
    enabled.workflows.demo.manager_enabled = false
    expect(settingsActions(baseline, enabled)).toEqual([
      { type: 'set_default_manager_enabled', value: true },
      { type: 'set_workflow_manager_enabled', workflow: 'demo', value: false },
    ])
    // Explicit false survives: reverting the default to omitted is a null delete.
    const reverted = structuredClone(enabled)
    reverted.default_manager_enabled = null
    expect(settingsActions(enabled, reverted)).toEqual([{ type: 'set_default_manager_enabled', value: null }])
    // Inherit removes the override; effective/source preview fields never emit.
    const inherit = structuredClone(enabled)
    inherit.workflows.demo.manager_enabled = null
    inherit.workflows.demo.effective_manager_enabled = true
    inherit.workflows.demo.manager_enabled_source = 'defaults'
    expect(settingsActions(enabled, inherit)).toEqual([{ type: 'set_workflow_manager_enabled', workflow: 'demo', value: null }])
  })
  it('keeps the launch-default workflow unrelated to supervision inheritance', () => {
    const draft = structuredClone(baseline)
    draft.default_workflow = 'demo-alias'
    expect(settingsActions(baseline, draft)).toEqual([{ type: 'set_default_workflow', value: 'demo-alias' }])
  })

  it('rejects a released clean reconciliation after the coordinator receives a custom effort', () => {
    const coordinator = createDraftPreviewCoordinator(async () => { throw new Error('preview should not run') })
    const cleanActionsKey = JSON.stringify(settingsActions(baseline, baseline))
    coordinator.updateDraft(structuredClone(baseline), true)

    const edited = structuredClone(baseline)
    edited.harnesses.codex.worker.effort = 'new-effort'
    coordinator.updateDraft(edited)

    const latest = coordinator.state().draft
    const released = reconcileCleanPreview(baseline, latest, latest, cleanActionsKey, false)
    expect(released.stale).toBe(true)
    expect(released.nextDraft).toBe(latest)
    expect(latest?.harnesses.codex.worker.effort).toBe('new-effort')

    // The functional updater sees the newer React declaration independently
    // from the coordinator-entry check and must also leave it untouched.
    const newerUpdater = reconcileCleanPreview(baseline, structuredClone(baseline), latest, cleanActionsKey, false)
    expect(newerUpdater.stale).toBe(false)
    expect(newerUpdater.nextDraft).toBe(latest)

    // A real edit-to-baseline reversion remains eligible for the clean restore.
    coordinator.updateDraft(structuredClone(baseline))
    const reverted = coordinator.state().draft
    const restored = reconcileCleanPreview(baseline, reverted, reverted, cleanActionsKey, false)
    expect(restored.stale).toBe(false)
    expect(restored.nextDraft).toEqual(baseline)
  })
})
