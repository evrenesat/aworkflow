import { describe, expect, it, vi } from 'vitest'
import {
  addFamilyStage,
  convertLegacyChain,
  detectSimpleFamilyRoute,
  generateStageId,
  groupTeamFamilies,
  removeFamilyStage,
  reorderFamilyStages,
  setDeclaredPromptOverride,
  setDeclaredRoleOverride,
  setTeamBase,
  setTeamDisplayName,
  suggestTeamId,
  TeamFamilyError,
  tryAddFamilyStage,
  validateLegacyConversionPreview,
  validateStableTeamId,
} from './teamFamilies'
import {
  createDraftPreviewCoordinator,
  previewLegacyConversion,
  previewSettingsActions,
  settingsActions,
} from './settingsDraft'
import type { GuidedFormProjection, GuidedTeamSummary, ProjectConfigFormResponse } from './types'

const profile = (model: string, effort = 'high') => ({ model, effort })

function summary(overrides: Partial<GuidedTeamSummary> = {}): GuidedTeamSummary {
  return { roles: {}, prompts: {}, ...overrides }
}

function makeDraft(teams: Record<string, GuidedTeamSummary>): GuidedFormProjection {
  return {
    default_workflow: 'demo',
    max_turns: 5,
    harnesses: { codex: { fast: profile('fast-model'), deep: profile('deep-model'), review: profile('review-model') } },
    roles: { worker: 'codex.fast', reviewer: 'codex.review' },
    teams,
    workflow_default_teams: { demo: null },
    workflows: {},
    prompts: { work: 'Global work prompt' },
    role_prompts: { worker: 'Global worker prompt', reviewer: 'Global reviewer prompt' },
  }
}

function formResponse(form: GuidedFormProjection, aflow = 'after-aflow', workflows = 'after-workflows'): ProjectConfigFormResponse {
  return {
    aflow_toml: aflow,
    workflows_toml: workflows,
    changed: true,
    validation: { state: 'ready', issues: [], placeholders: [], workflows: Object.keys(form.workflows), teams: Object.keys(form.teams), roles: Object.keys(form.roles) },
    form,
    syntax_issues: [],
    choices: { harnesses: ['codex'], profiles: { codex: ['deep', 'fast', 'review'] }, selectors: ['codex.deep', 'codex.fast', 'codex.review'], roles: Object.keys(form.roles), teams: Object.keys(form.teams), workflows: Object.keys(form.workflows) },
    suggestions: { label: 'suggestion', harnesses: [], profiles: [], note: '' },
    starter_defaults: null,
  }
}

const familyDraft = makeDraft({
  product: summary({
    display_name: 'Product development',
    roles: { worker: 'codex.deep' },
    prompts: { reviewer: 'Base reviewer prompt' },
    upgrade_to: 'product_fast',
    effective_roles: { worker: 'codex.deep', reviewer: 'codex.review' },
    effective_prompts: { worker: 'Global worker prompt', reviewer: 'Base reviewer prompt' },
    role_sources: { worker: 'product', reviewer: 'global' },
    prompt_sources: { worker: 'global', reviewer: 'product' },
  }),
  product_fast: summary({
    display_name: 'Fast variant',
    extends: 'product',
    roles: { reviewer: 'codex.review' },
    prompts: {},
    upgrade_to: null,
    effective_roles: { worker: 'codex.deep', reviewer: 'codex.review' },
    effective_prompts: { worker: 'Global worker prompt', reviewer: 'Base reviewer prompt' },
    role_sources: { worker: 'product', reviewer: 'product_fast' },
    prompt_sources: { worker: 'global', reviewer: 'product' },
  }),
  fallback: summary({ backup_team: 'product' }),
})

const legacyDraft = makeDraft({
  product: summary({
    display_name: 'Product development',
    roles: { worker: 'codex.deep' },
    prompts: { reviewer: 'Base reviewer prompt' },
    upgrade_to: 'product_fast',
    backup_team: 'fallback',
    effective_roles: { worker: 'codex.deep', reviewer: 'codex.review' },
    effective_prompts: { worker: 'Global work prompt', reviewer: 'Base reviewer prompt' },
  }),
  product_fast: summary({
    display_name: 'Fast variant',
    roles: { worker: 'codex.fast' },
    prompts: { reviewer: 'Base reviewer prompt' },
    upgrade_to: 'product_review',
    backup_team: 'fallback',
    effective_roles: { worker: 'codex.fast', reviewer: 'codex.review' },
    effective_prompts: { worker: 'Global work prompt', reviewer: 'Base reviewer prompt' },
  }),
  product_review: summary({
    display_name: 'Review-only variant',
    roles: { worker: 'codex.deep' },
    prompts: { reviewer: 'Base reviewer prompt' },
    upgrade_to: null,
    backup_team: 'fallback',
    effective_roles: { worker: 'codex.deep', reviewer: 'codex.review' },
    effective_prompts: { worker: 'Global work prompt', reviewer: 'Base reviewer prompt' },
  }),
  fallback: summary(),
})

describe('team family topology and immutable draft operations', () => {
  it('groups explicit families and keeps every member reachable', () => {
    const result = groupTeamFamilies(familyDraft)
    const family = result.groups.find((entry) => entry.rootId === 'product')
    expect(family).toMatchObject({ kind: 'family', memberIds: ['product', 'product_fast'], route: ['product', 'product_fast'], simpleRoute: true })
    expect(result.groups.flatMap((entry) => entry.memberIds).sort()).toEqual(Object.keys(familyDraft.teams).sort())
    expect(family?.displayName).toBe('Product development')
  })

  it('does not hide shared legacy targets or external graph members', () => {
    const ambiguous = makeDraft({
      first: summary({ upgrade_to: 'shared' }),
      second: summary({ upgrade_to: 'shared' }),
      shared: summary(),
    })
    const result = groupTeamFamilies(ambiguous)
    expect(result.errors.some((error) => error.code === 'ambiguous_legacy_chain')).toBe(true)
    expect(result.groups.filter((entry) => entry.kind === 'legacy_chain')).toHaveLength(0)
    expect(result.groups.flatMap((entry) => entry.memberIds).sort()).toEqual(['first', 'second', 'shared'])
    expect(result.groups.every((entry) => entry.kind === 'standalone')).toBe(true)
  })

  it('reports unsupported simple-route topology without changing the draft', () => {
    const complex = structuredClone(familyDraft)
    complex.teams.product_fast.upgrade_to = 'fallback'
    const before = JSON.stringify(complex)
    const route = detectSimpleFamilyRoute(complex, 'product')
    expect(route.ok).toBe(false)
    expect(route.error?.code).toBe('unsupported_topology')
    expect(tryAddFamilyStage(complex, 'product', { id: 'product_final', displayName: 'Final' })).toMatchObject({ ok: false, error: { code: 'unsupported_topology' } })
    expect(JSON.stringify(complex)).toBe(before)
  })

  it('suggests stable ASCII IDs and refuses empty or colliding IDs', () => {
    expect(suggestTeamId(' Strong étage / v2 ')).toBe('strong_etage_v2')
    expect(generateStageId('product', 'Stronger worker')).toBe('product_stronger_worker')
    expect(validateStableTeamId('bad id')).not.toBeNull()
    expect(() => generateStageId('product', 'Stronger worker', ['product_stronger_worker'])).toThrowError(TeamFamilyError)
    expect(() => generateStageId('product', '!!!')).toThrowError(TeamFamilyError)
  })

  it('adds reviewer-only variants, reorders links, and removes a confirmed stage immutably', () => {
    const added = addFamilyStage(familyDraft, 'product', { id: 'product_review', displayName: 'Review-only', roles: { reviewer: 'codex.review' } })
    expect(added.teams.product_fast.upgrade_to).toBe('product_review')
    expect(added.teams.product_review).toMatchObject({ extends: 'product', roles: { reviewer: 'codex.review' }, upgrade_to: null })
    expect(familyDraft.teams.product_fast.upgrade_to).toBeNull()

    const reordered = reorderFamilyStages(added, 'product', ['product_review', 'product_fast'])
    expect(reordered.teams.product.upgrade_to).toBe('product_review')
    expect(reordered.teams.product_review.upgrade_to).toBe('product_fast')
    expect(reordered.teams.product_fast.upgrade_to).toBeNull()

    const removed = removeFamilyStage(reordered, 'product', 'product_review', { confirmed: true })
    expect(removed.teams.product.upgrade_to).toBe('product_fast')
    expect(removed.teams.product_review).toBeUndefined()
    expect(() => removeFamilyStage(reordered, 'product', 'product_review')).toThrowError(TeamFamilyError)
  })

  it('edits only declared overrides and metadata, preserving effective projection fields', () => {
    const withoutWorker = setDeclaredRoleOverride(familyDraft, 'product_fast', 'reviewer', null)
    expect(withoutWorker.teams.product_fast.roles).toEqual({})
    expect(withoutWorker.teams.product_fast.effective_roles).toEqual(familyDraft.teams.product_fast.effective_roles)
    const withoutPrompt = setDeclaredPromptOverride(withoutWorker, 'product', 'reviewer', null)
    expect(withoutPrompt.teams.product.prompts).toEqual({})
    expect(withoutPrompt.teams.product.effective_prompts).toEqual(familyDraft.teams.product.effective_prompts)
    const labelled = setTeamDisplayName(withoutPrompt, 'product', '  Product  ')
    expect(labelled.teams.product.display_name).toBe('Product')
    const detached = setTeamBase(labelled, 'product_fast', null)
    expect(detached.teams.product_fast.extends).toBeUndefined()
    expect(familyDraft.teams.product_fast.extends).toBe('product')
  })
})

describe('legacy conversion', () => {
  it('keeps effective differences, removes only equal declarations, and preserves links', () => {
    const plan = convertLegacyChain(legacyDraft, 'product')
    expect(plan.memberIds).toEqual(['product', 'product_fast', 'product_review'])
    expect(plan.draft.teams.product_fast).toMatchObject({
      extends: 'product',
      roles: { worker: 'codex.fast' },
      prompts: {},
      upgrade_to: 'product_review',
      backup_team: 'fallback',
    })
    expect(plan.draft.teams.product_review).toMatchObject({ extends: 'product', roles: {}, prompts: {}, upgrade_to: null, backup_team: 'fallback' })
    expect(plan.changes).toContainEqual({ teamId: 'product_fast', field: 'roles', removed: [], retained: ['worker'] })
    expect(legacyDraft.teams.product_fast.extends).toBeUndefined()

    const after = structuredClone(plan.draft)
    const validation = validateLegacyConversionPreview(legacyDraft, after, plan)
    expect(validation.valid).toBe(true)
    expect(validation.warning).toMatch(/later edits/i)
    after.teams.product_review.effective_prompts!.reviewer = 'changed'
    expect(validateLegacyConversionPreview(legacyDraft, after, plan).error?.code).toBe('conversion_preview_mismatch')
  })

  it('refuses conversion when the old absence cannot be represented', () => {
    const absent = structuredClone(legacyDraft)
    delete absent.teams.product_fast.effective_roles!.reviewer
    expect(() => convertLegacyChain(absent, 'product')).toThrowError(TeamFamilyError)
    try {
      convertLegacyChain(absent, 'product')
    } catch (error) {
      expect((error as TeamFamilyError).code).toBe('unrepresentable_absence')
    }
  })

  it('requires canonical server effective maps instead of resolving inheritance in the client', () => {
    const noProjection = structuredClone(legacyDraft)
    delete noProjection.teams.product_fast.effective_roles
    expect(() => convertLegacyChain(noProjection, 'product')).toThrowError(TeamFamilyError)
    try {
      convertLegacyChain(noProjection, 'product')
    } catch (error) {
      expect((error as TeamFamilyError).code).toBe('canonical_preview_required')
    }
  })
})

describe('minimal settings action batches and server preview coordination', () => {
  it('orders additions before declarations and deletions, including nullable removals', () => {
    const base = makeDraft({
      product: summary({ upgrade_to: 'product_fast' }),
      product_fast: summary({ roles: { worker: 'codex.fast' }, prompts: { reviewer: 'Base reviewer prompt' } }),
      retired: summary(),
    })
    const draft = structuredClone(base)
    delete draft.teams.retired
    draft.teams.product_fast.roles = { reviewer: 'codex.review' }
    draft.teams.product_fast.prompts = {}
    draft.teams.product_fast.upgrade_to = 'product_final'
    draft.teams.product_final = summary({ extends: 'product', display_name: 'Final', roles: { reviewer: 'codex.review' }, upgrade_to: null })
    draft.teams.product.effective_roles = { worker: 'codex.deep' }
    const actions = settingsActions(base, draft)
    const firstNonAdd = actions.findIndex((action) => action.type !== 'add_team')
    expect(actions.slice(0, firstNonAdd).every((action) => action.type === 'add_team')).toBe(true)
    expect(actions.at(-1)).toEqual({ type: 'remove_team', team: 'retired' })
    expect(actions).toContainEqual({ type: 'set_team_role', team: 'product_fast', role: 'worker', selector: null })
    expect(actions).toContainEqual({ type: 'set_role_prompt', team: 'product_fast', role: 'reviewer', text: null })
    expect(actions).toContainEqual({ type: 'set_team_base', team: 'product_final', extends: 'product' })
    expect(actions).toContainEqual({ type: 'set_team_display_name', team: 'product_final', display_name: 'Final' })
    expect(actions).toContainEqual({ type: 'set_team_upgrade', team: 'product_fast', upgrade_to: 'product_final' })
    expect(settingsActions(base, structuredClone(base))).toEqual([])
  })

  it('emits no actions after a declaration is edited and reversed', () => {
    const edited = structuredClone(familyDraft)
    edited.teams.product.roles.worker = 'codex.fast'
    edited.teams.product.display_name = 'Temporary label'
    const reverted = structuredClone(edited)
    reverted.teams.product.roles = structuredClone(familyDraft.teams.product.roles)
    reverted.teams.product.display_name = familyDraft.teams.product.display_name
    reverted.teams.product.effective_roles = { worker: 'server projection' }
    reverted.teams.product.role_sources = { worker: 'product' }
    expect(settingsActions(familyDraft, reverted)).toEqual([])
  })

  it('previews the ordered actions through the server callback', async () => {
    const calls: ProjectConfigFormResponse[] = []
    const preview = vi.fn(async (request) => {
      const form = structuredClone(familyDraft)
      calls.push(formResponse(form))
      return formResponse(form, request.action ? `${request.action.type}-aflow` : 'initial-aflow')
    })
    const result = await previewSettingsActions(
      { aflow_toml: 'initial-aflow', workflows_toml: 'initial-workflows' },
      [{ type: 'set_team_base', team: 'product_fast', extends: 'product' }, { type: 'set_team_upgrade', team: 'product', upgrade_to: null }],
      preview,
    )
    expect(preview).toHaveBeenCalledTimes(2)
    expect(result.pair.aflow_toml).toBe('set_team_upgrade-aflow')
    expect(calls).toHaveLength(2)
  })

  it('discards stale responses while retaining the latest declarations', async () => {
    type Pending = { resolve: (response: ProjectConfigFormResponse) => void; reject: (reason: Error) => void }
    const pending: Pending[] = []
    const preview = vi.fn((_request) => new Promise<ProjectConfigFormResponse>((resolve, reject) => pending.push({ resolve, reject })))
    const coordinator = createDraftPreviewCoordinator(preview)
    const firstDraft = structuredClone(familyDraft)
    firstDraft.teams.product.roles.worker = 'codex.deep'
    const secondDraft = structuredClone(familyDraft)
    secondDraft.teams.product.roles.worker = 'codex.fast'
    const first = coordinator.request({ pair: { aflow_toml: 'a', workflows_toml: 'w' }, actions: [{ type: 'set_team_role', team: 'product', role: 'worker', selector: 'codex.deep' }], declarations: firstDraft })
    coordinator.updateDraft(secondDraft)
    const second = coordinator.request({ pair: { aflow_toml: 'a', workflows_toml: 'w' }, actions: [{ type: 'set_team_role', team: 'product', role: 'worker', selector: 'codex.fast' }], declarations: secondDraft })
    expect(pending).toHaveLength(2)
    const projected = structuredClone(familyDraft)
    projected.teams.product.roles.worker = 'server-declaration-that-must-not-win'
    projected.teams.product.effective_roles = { worker: 'server-effective' }
    pending[1].resolve(formResponse(projected))
    const latest = await second
    expect(latest.stale).toBe(false)
    expect(latest.draft.teams.product.roles.worker).toBe('codex.fast')
    expect(latest.draft.teams.product.effective_roles?.worker).toBe('server-effective')
    pending[0].resolve(formResponse(familyDraft))
    expect((await first).stale).toBe(true)
    expect(coordinator.state().draft?.teams.product.roles.worker).toBe('codex.fast')
  })

  it('retains the candidate draft and reports a failed preview', async () => {
    const preview = vi.fn(async () => { throw new Error('preview unavailable') })
    const coordinator = createDraftPreviewCoordinator(preview)
    const candidate = structuredClone(familyDraft)
    candidate.teams.product.display_name = 'Unsaved family label'
    const result = await coordinator.request({ pair: { aflow_toml: 'a', workflows_toml: 'w' }, actions: [], declarations: candidate })
    expect(result.stale).toBe(false)
    expect(result.error?.message).toBe('preview unavailable')
    expect(result.draft.teams.product.display_name).toBe('Unsaved family label')
    expect(coordinator.state().draft?.teams.product.display_name).toBe('Unsaved family label')
  })

  it('validates a conversion only after the authoritative preview returns', async () => {
    const converted = convertLegacyChain(legacyDraft, 'product').draft
    const preview = vi.fn(async (request) => formResponse(structuredClone(converted), request.action ? 'converted' : 'baseline'))
    const result = await previewLegacyConversion({ pair: { aflow_toml: 'a', workflows_toml: 'w' }, draft: legacyDraft, rootId: 'product', preview })
    expect(preview).toHaveBeenCalled()
    expect(result.valid).toBe(true)
    expect(result.validation.warning).toMatch(/propagate/i)

    const failed = await previewLegacyConversion({
      pair: { aflow_toml: 'a', workflows_toml: 'w' },
      draft: legacyDraft,
      rootId: 'product',
      preview: vi.fn(async () => ({ ...formResponse(legacyDraft), form: null })),
    })
    expect(failed.valid).toBe(false)
    expect(failed.draft.teams.product_fast.extends).toBe('product')
    expect(failed.validation.error?.code).toBe('canonical_preview_required')

    const mismatched = structuredClone(converted)
    mismatched.teams.product_fast.effective_roles!.worker = 'server changed the result'
    const mismatch = await previewLegacyConversion({
      pair: { aflow_toml: 'a', workflows_toml: 'w' },
      draft: legacyDraft,
      rootId: 'product',
      preview: vi.fn(async () => formResponse(mismatched)),
    })
    expect(mismatch.valid).toBe(false)
    expect(mismatch.validation.error?.code).toBe('conversion_preview_mismatch')
  })
})
