import type {
  GuidedConfigAction,
  GuidedFormProjection,
  GuidedTeamSummary,
  ProjectConfigFormRequest,
  ProjectConfigFormResponse,
} from './types'
import {
  convertLegacyChain,
  TeamFamilyError,
  validateLegacyConversionPreview,
  type LegacyConversionPlan,
  type LegacyConversionValidation,
} from './teamFamilies'

export interface ConfigDocumentPair {
  aflow_toml: string
  workflows_toml: string
}

export type GuidedFormPreview = (request: ProjectConfigFormRequest) => Promise<ProjectConfigFormResponse>

function normalized(value: string | null | undefined): string | null {
  return value === undefined || value === null ? null : value
}

function normalizedDisplayName(value: string | null | undefined): string | null {
  const trimmed = value?.trim() ?? ''
  return trimmed ? trimmed : null
}

function sortedKeys(...maps: Array<Record<string, unknown> | undefined>): string[] {
  return [...new Set(maps.flatMap((map) => Object.keys(map ?? {})))].sort()
}

function textDiff(
  before: Record<string, string> | undefined,
  after: Record<string, string> | undefined,
  make: (key: string, text: string | null) => GuidedConfigAction,
): GuidedConfigAction[] {
  const actions: GuidedConfigAction[] = []
  for (const key of sortedKeys(before, after)) {
    if (before?.[key] !== after?.[key]) actions.push(make(key, after?.[key] ?? null))
  }
  return actions
}

function changedProfileActions(base: GuidedFormProjection, draft: GuidedFormProjection): GuidedConfigAction[] {
  const actions: GuidedConfigAction[] = []
  for (const harness of Object.keys(draft.harnesses).sort()) {
    const profiles = draft.harnesses[harness]
    for (const profile of Object.keys(profiles).sort()) {
      const value = profiles[profile]
      const old = base.harnesses[harness]?.[profile]
      const action: Extract<GuidedConfigAction, { type: 'upsert_profile' }> = { type: 'upsert_profile', harness, profile }
      if (harness !== 'zcode') {
        if (value.model !== (old?.model ?? null)) action.model = value.model
        if (value.effort !== (old?.effort ?? null)) action.effort = value.effort
      }
      if (!old || 'model' in action || 'effort' in action) actions.push(action)
    }
  }
  return actions
}

function changedTeamActions(base: GuidedFormProjection, draft: GuidedFormProjection): GuidedConfigAction[] {
  const actions: GuidedConfigAction[] = []
  const teamIds = Object.keys(draft.teams).sort()
  for (const teamId of teamIds) {
    const value = draft.teams[teamId]
    const old = base.teams[teamId]
    for (const role of sortedKeys(old?.roles, value.roles)) {
      if (old?.roles[role] !== value.roles[role]) {
        actions.push({ type: 'set_team_role', team: teamId, role, selector: value.roles[role] ?? null })
      }
    }
    const oldBase = normalized(old?.extends)
    const newBase = normalized(value.extends)
    // A newly added team needs no null-removal action, but a direct family
    // child must declare its base after add_team has established its identity.
    if (newBase !== oldBase && (old !== undefined || newBase !== null)) {
      actions.push({ type: 'set_team_base', team: teamId, extends: newBase })
    }
    const oldDisplayName = normalizedDisplayName(old?.display_name)
    const newDisplayName = normalizedDisplayName(value.display_name)
    if (newDisplayName !== oldDisplayName && (old !== undefined || newDisplayName !== null)) {
      actions.push({ type: 'set_team_display_name', team: teamId, display_name: newDisplayName })
    }
    const oldUpgrade = normalized(old?.upgrade_to)
    const newUpgrade = normalized(value.upgrade_to)
    if (newUpgrade !== oldUpgrade && (old !== undefined || newUpgrade !== null)) {
      actions.push({ type: 'set_team_upgrade', team: teamId, upgrade_to: newUpgrade })
    }
    actions.push(...textDiff(old?.prompts, value.prompts, (role, text) => ({ type: 'set_role_prompt', team: teamId, role, text })))
  }
  return actions
}

function removedTeamLinkActions(base: GuidedFormProjection, draft: GuidedFormProjection): GuidedConfigAction[] {
  const actions: GuidedConfigAction[] = []
  for (const teamId of Object.keys(base.teams).filter((id) => !(id in draft.teams)).sort()) {
    const value = base.teams[teamId]
    // Clear links on a team that is itself being removed when needed. This
    // makes multi-team draft deletions executable before remove_team while
    // leaving an independent team's deletion as one minimal action.
    if (normalized(value.extends) !== null) actions.push({ type: 'set_team_base', team: teamId, extends: null })
    if (normalized(value.upgrade_to) !== null) actions.push({ type: 'set_team_upgrade', team: teamId, upgrade_to: null })
  }
  return actions
}

function teamRemovalOrder(base: GuidedFormProjection, draft: GuidedFormProjection): string[] {
  const remaining = new Set(Object.keys(base.teams).filter((teamId) => !(teamId in draft.teams)))
  const result: string[] = []
  // Remove sources before targets so the server's reference check observes the
  // same final graph as the draft. If a remaining team still references a
  // removed target, no ordering can make the batch valid and the server gives
  // the explicit reference error.
  while (remaining.size) {
    const candidate = [...remaining].sort().find((teamId) => [...remaining].every((otherId) => {
      if (otherId === teamId) return true
      const other = base.teams[otherId]
      return normalized(other.extends) !== teamId
        && normalized(other.upgrade_to) !== teamId
        && normalized(other.backup_team) !== teamId
    }))
    const next = candidate ?? [...remaining].sort()[0]
    result.push(next)
    remaining.delete(next)
  }
  return result
}

/**
 * Net semantic changes only. Action categories are deliberately ordered so a
 * single server PATCH can add identities, edit declarations/routes, and only
 * then remove teams whose final references have already been cleared.
 */
export function settingsActions(base: GuidedFormProjection, draft: GuidedFormProjection): GuidedConfigAction[] {
  const additions: GuidedConfigAction[] = Object.keys(draft.teams)
    .filter((teamId) => !(teamId in base.teams))
    .sort()
    .map((team) => ({ type: 'add_team', team }))
  const declarations: GuidedConfigAction[] = []

  declarations.push(...changedProfileActions(base, draft))
  for (const role of Object.keys(draft.roles).sort()) {
    if (base.roles[role] !== draft.roles[role]) declarations.push({ type: 'set_global_role', role, selector: draft.roles[role] })
  }
  declarations.push(...changedTeamActions(base, draft))
  declarations.push(...removedTeamLinkActions(base, draft))
  declarations.push(...textDiff(base.prompts, draft.prompts, (name, text) => ({ type: 'set_prompt', name, text })))
  declarations.push(...textDiff(base.role_prompts, draft.role_prompts, (role, text) => ({ type: 'set_role_prompt', role, text })))

  if (draft.default_workflow !== base.default_workflow && draft.default_workflow) declarations.push({ type: 'set_default_workflow', value: draft.default_workflow })
  if (draft.max_turns !== base.max_turns) declarations.push({ type: 'set_max_turns', value: draft.max_turns })
  for (const workflow of sortedKeys(base.workflow_default_teams, draft.workflow_default_teams)) {
    if (base.workflow_default_teams[workflow] !== draft.workflow_default_teams[workflow]) {
      declarations.push({ type: 'set_workflow_default_team', workflow, team: draft.workflow_default_teams[workflow] ?? null })
    }
  }
  if ((draft.default_manager_enabled ?? null) !== (base.default_manager_enabled ?? null)) {
    declarations.push({ type: 'set_default_manager_enabled', value: draft.default_manager_enabled ?? null })
  }
  for (const workflow of sortedKeys(base.workflows, draft.workflows)) {
    const declared = draft.workflows[workflow]?.manager_enabled ?? null
    if ((base.workflows[workflow]?.manager_enabled ?? null) !== declared) {
      declarations.push({ type: 'set_workflow_manager_enabled', workflow, value: declared })
    }
  }

  const deletions: GuidedConfigAction[] = teamRemovalOrder(base, draft)
    .map((team) => ({ type: 'remove_team', team }))
  return [...additions, ...declarations, ...deletions]
}

export function changedDocuments(base: ConfigDocumentPair, texts: [string, string]): Partial<Record<'aflow.toml' | 'workflows.toml', string>> {
  return {
    ...(texts[0] !== base.aflow_toml ? { 'aflow.toml': texts[0] } : {}),
    ...(texts[1] !== base.workflows_toml ? { 'workflows.toml': texts[1] } : {}),
  }
}

export interface PreviewActionsResult {
  pair: ConfigDocumentPair
  response: ProjectConfigFormResponse
}

/**
 * Ask the existing pure server form endpoint to apply a candidate action list
 * in order. No client-side inheritance calculation is performed. The PATCH
 * path remains the atomic persistence boundary; these requests are read-only
 * previews of the same ordered actions.
 */
export async function previewSettingsActions(
  pair: ConfigDocumentPair,
  actions: readonly GuidedConfigAction[],
  preview: GuidedFormPreview,
): Promise<PreviewActionsResult> {
  let current = { ...pair }
  let response: ProjectConfigFormResponse | undefined
  if (!actions.length) {
    response = await preview({ ...current })
    return { pair: current, response }
  }
  for (const action of actions) {
    response = await preview({ ...current, action })
    current = { aflow_toml: response.aflow_toml, workflows_toml: response.workflows_toml }
  }
  return { pair: current, response: response! }
}

export function retainServerProjection(declarations: GuidedFormProjection, projected: GuidedFormProjection): GuidedFormProjection {
  const next = JSON.parse(JSON.stringify(declarations)) as GuidedFormProjection
  for (const [teamId, current] of Object.entries(next.teams)) {
    const serverTeam: GuidedTeamSummary | undefined = projected.teams[teamId]
    if (!serverTeam) continue
    next.teams[teamId] = {
      ...current,
      // Only these fields are read-only semantic projection data. Declared
      // roles/prompts, metadata, links, and IDs stay with the live draft.
      effective_roles: serverTeam.effective_roles ?? current.effective_roles,
      effective_prompts: serverTeam.effective_prompts ?? current.effective_prompts,
      role_sources: serverTeam.role_sources ?? current.role_sources,
      prompt_sources: serverTeam.prompt_sources ?? current.prompt_sources,
    }
  }
  for (const [workflow, current] of Object.entries(next.workflows)) {
    const serverWorkflow = projected.workflows[workflow]
    if (!serverWorkflow) continue
    next.workflows[workflow] = {
      ...current,
      effective_manager_enabled: serverWorkflow.effective_manager_enabled ?? current.effective_manager_enabled,
      manager_enabled_source: serverWorkflow.manager_enabled_source ?? current.manager_enabled_source,
    }
  }
  return next
}

function previewValidationError(response: ProjectConfigFormResponse, message: string): TeamFamilyError | null {
  if (response.validation.state === 'invalid') {
    return new TeamFamilyError('canonical_preview_required', message, undefined, response.validation.issues[0]?.message)
  }
  if (response.validation.placeholders.length) {
    return new TeamFamilyError('canonical_preview_required', `${message} Replace placeholder selectors before accepting it.`, undefined, response.validation.placeholders[0])
  }
  return null
}

export interface DraftPreviewInput {
  pair: ConfigDocumentPair
  actions: readonly GuidedConfigAction[]
  declarations: GuidedFormProjection
}

export interface DraftPreviewState {
  pending: boolean
  /** True only when the draft has a successful projection for its current source. */
  current: boolean
  draft: GuidedFormProjection | null
  response: ProjectConfigFormResponse | null
  error: Error | null
}

export interface DraftPreviewResult {
  stale: boolean
  draft: GuidedFormProjection
  pair: ConfigDocumentPair | null
  response: ProjectConfigFormResponse | null
  error: Error | null
}

export interface DraftPreviewCoordinator {
  request(input: DraftPreviewInput): Promise<DraftPreviewResult>
  /** Update declarations without invalidating a matching request or result. */
  updateDraft(draft: GuidedFormProjection, current?: boolean): void
  invalidate(): void
  state(): DraftPreviewState
}

/**
 * Keep server projections revisioned while a user continues editing. A late
 * response can never overwrite the latest declarations, and a failed preview
 * leaves the candidate draft available for correction or retry.
 */
export function createDraftPreviewCoordinator(preview: GuidedFormPreview): DraftPreviewCoordinator {
  let sequence = 0
  let latestDraft: GuidedFormProjection | null = null
  let currentState: DraftPreviewState = { pending: false, current: false, draft: null, response: null, error: null }

  const snapshot = (): DraftPreviewState => ({
    pending: currentState.pending,
    current: currentState.current,
    draft: currentState.draft ? JSON.parse(JSON.stringify(currentState.draft)) as GuidedFormProjection : null,
    response: currentState.response,
    error: currentState.error,
  })
  const staleResult = (): DraftPreviewResult => ({
    stale: true,
    draft: latestDraft ? JSON.parse(JSON.stringify(latestDraft)) as GuidedFormProjection : currentState.draft ?? ({ } as GuidedFormProjection),
    pair: null,
    response: null,
    error: null,
  })

  return {
    updateDraft(draft, current) {
      latestDraft = JSON.parse(JSON.stringify(draft)) as GuidedFormProjection
      currentState = {
        ...currentState,
        draft: latestDraft,
        ...(current === undefined ? {} : { current }),
      }
    },
    invalidate() {
      sequence += 1
      currentState = { ...currentState, pending: false, current: false, error: null }
    },
    state() {
      return snapshot()
    },
    async request(input) {
      sequence += 1
      const requestSequence = sequence
      latestDraft = JSON.parse(JSON.stringify(input.declarations)) as GuidedFormProjection
      currentState = { pending: true, current: false, draft: latestDraft, response: currentState.response, error: null }
      try {
        const result = await previewSettingsActions(input.pair, input.actions, preview)
        if (requestSequence !== sequence) return staleResult()
        const form = result.response.form
        const invalid = previewValidationError(result.response, 'Server preview did not produce a valid candidate.')
        if (invalid) {
          currentState = { pending: false, current: false, draft: latestDraft, response: result.response, error: invalid }
          return { stale: false, draft: latestDraft!, pair: result.pair, response: result.response, error: invalid }
        }
        if (!form) {
          const error = new Error('Server preview did not return a guided projection.')
          currentState = { pending: false, current: false, draft: latestDraft, response: result.response, error }
          return { stale: false, draft: latestDraft!, pair: result.pair, response: result.response, error }
        }
        const draft = retainServerProjection(latestDraft!, form)
        currentState = { pending: false, current: true, draft, response: result.response, error: null }
        latestDraft = draft
        return { stale: false, draft, pair: result.pair, response: result.response, error: null }
      } catch (reason) {
        if (requestSequence !== sequence) return staleResult()
        const error = reason instanceof Error ? reason : new Error('Server preview failed.')
        currentState = { pending: false, current: false, draft: latestDraft, response: null, error }
        return { stale: false, draft: latestDraft!, pair: null, response: null, error }
      }
    },
  }
}

export interface LegacyConversionPreviewInput {
  pair: ConfigDocumentPair
  draft: GuidedFormProjection
  rootId: string
  preview: GuidedFormPreview
}

export interface LegacyConversionPreviewResult {
  valid: boolean
  conversion: LegacyConversionPlan
  draft: GuidedFormProjection
  pair: ConfigDocumentPair | null
  response: ProjectConfigFormResponse | null
  validation: LegacyConversionValidation
  error: Error | null
}

/** Preview an opt-in legacy conversion through the authoritative server form. */
export async function previewLegacyConversion(input: LegacyConversionPreviewInput): Promise<LegacyConversionPreviewResult> {
  const conversion = convertLegacyChain(input.draft, input.rootId)
  const actions = settingsActions(input.draft, conversion.draft)
  try {
    const result = await previewSettingsActions(input.pair, actions, input.preview)
    const invalid = previewValidationError(result.response, 'Server preview did not produce a valid conversion candidate.')
    if (invalid) {
      return {
        valid: false,
        conversion,
        draft: conversion.draft,
        pair: result.pair,
        response: result.response,
        validation: {
          valid: false,
          memberIds: conversion.memberIds,
          warning: 'Later edits to the family Base will propagate to converted stages.',
          error: invalid,
        },
        error: invalid,
      }
    }
    if (!result.response.form) {
      const error = new Error('Server preview did not return a guided projection for the conversion.')
      return {
        valid: false,
        conversion,
        draft: conversion.draft,
        pair: result.pair,
        response: result.response,
        validation: {
          valid: false,
          memberIds: conversion.memberIds,
          warning: 'Later edits to the family Base will propagate to converted stages.',
          error: new TeamFamilyError('canonical_preview_required', error.message),
        },
        error,
      }
    }
    const validation = validateLegacyConversionPreview(input.draft, result.response.form, conversion)
    return {
      valid: validation.valid,
      conversion,
      draft: conversion.draft,
      pair: result.pair,
      response: result.response,
      validation,
      error: validation.error,
    }
  } catch (reason) {
    const error = reason instanceof Error ? reason : new Error('Server preview failed for the conversion.')
    return {
      valid: false,
      conversion,
      draft: conversion.draft,
      pair: null,
      response: null,
      validation: {
        valid: false,
        memberIds: conversion.memberIds,
        warning: 'Later edits to the family Base will propagate to converted stages.',
        error: new TeamFamilyError('canonical_preview_required', error.message),
      },
      error,
    }
  }
}
