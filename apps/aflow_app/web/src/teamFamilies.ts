import type { GuidedFormProjection, GuidedTeamSummary } from './types'

/** The same starter-name grammar used by the server for team identifiers. */
export const TEAM_ID_PATTERN = /^[A-Za-z0-9_][A-Za-z0-9_-]{0,63}$/u
export const TEAM_DISPLAY_NAME_MAX_LENGTH = 128

export type TeamFamilyErrorCode =
  | 'unknown_team'
  | 'unknown_role'
  | 'unknown_profile'
  | 'unknown_reference'
  | 'self_reference'
  | 'invalid_team_id'
  | 'duplicate_team_id'
  | 'empty_slug'
  | 'invalid_display_name'
  | 'unsupported_topology'
  | 'ambiguous_legacy_chain'
  | 'confirmation_required'
  | 'canonical_preview_required'
  | 'conversion_preview_mismatch'
  | 'unrepresentable_absence'

/** A bounded, user-actionable error returned by a pure family operation. */
export class TeamFamilyError extends Error {
  readonly code: TeamFamilyErrorCode
  readonly team: string | undefined
  readonly reference: string | undefined

  constructor(code: TeamFamilyErrorCode, message: string, team?: string, reference?: string) {
    super(message)
    this.name = 'TeamFamilyError'
    this.code = code
    this.team = team
    this.reference = reference
  }
}

export type TeamFamilyResult<T> =
  | { ok: true; value: T; error: null }
  | { ok: false; value: null; error: TeamFamilyError }

export type TeamFamilyKind = 'family' | 'legacy_chain' | 'standalone'

export interface TeamFamilyGroup {
  kind: TeamFamilyKind
  rootId: string
  memberIds: string[]
  /** The configured upgrade route, or the valid prefix for an invalid graph. */
  route: string[]
  simpleRoute: boolean
  reason: string | null
  displayName: string | null
}

export interface TeamFamilyGrouping {
  groups: TeamFamilyGroup[]
  errors: TeamFamilyError[]
}

export interface SimpleFamilyRoute {
  ok: boolean
  route: string[]
  memberIds: string[]
  error: TeamFamilyError | null
}

export interface FamilyStageInput {
  id: string
  displayName?: string | null
  roles?: Record<string, string>
  prompts?: Record<string, string>
}

export interface RemoveFamilyStageOptions {
  /** A caller must perform the review/confirmation step before deletion. */
  confirmed?: boolean
  /** Alias useful to callers that use the UI wording. */
  confirm?: boolean
}

export interface LegacyConversionChange {
  teamId: string
  field: 'roles' | 'prompts'
  removed: string[]
  retained: string[]
}

export interface LegacyConversionPlan {
  rootId: string
  memberIds: string[]
  draft: GuidedFormProjection
  changes: LegacyConversionChange[]
}

export interface LegacyConversionValidation {
  valid: boolean
  memberIds: string[]
  warning: string
  error: TeamFamilyError | null
}

type TeamField = 'effective_roles' | 'effective_prompts'

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T
}

function own<T extends object>(value: T, key: PropertyKey): boolean {
  return Object.prototype.hasOwnProperty.call(value, key)
}

function team(draft: GuidedFormProjection, teamId: string): GuidedTeamSummary | undefined {
  return draft.teams[teamId]
}

function requireTeam(draft: GuidedFormProjection, teamId: string): GuidedTeamSummary {
  const value = team(draft, teamId)
  if (!value) throw new TeamFamilyError('unknown_team', `Team "${teamId}" is not configured.`, teamId)
  return value
}

function normalizedReference(value: string | null | undefined): string | null {
  return value === undefined || value === null ? null : value
}

function upgradeTarget(value: GuidedTeamSummary): string | null {
  return normalizedReference(value.upgrade_to)
}

function directChildren(draft: GuidedFormProjection, rootId: string): string[] {
  return Object.keys(draft.teams)
    .filter((teamId) => normalizedReference(draft.teams[teamId].extends) === rootId)
    .sort()
}

function descendants(draft: GuidedFormProjection, rootId: string): string[] {
  const found: string[] = []
  const queue = [...directChildren(draft, rootId)]
  const seen = new Set<string>()
  while (queue.length) {
    const current = queue.shift()!
    if (seen.has(current)) continue
    seen.add(current)
    found.push(current)
    queue.push(...directChildren(draft, current))
  }
  return found.sort()
}

function incomingUpgrades(draft: GuidedFormProjection, targetId: string): string[] {
  return Object.keys(draft.teams)
    .filter((sourceId) => upgradeTarget(draft.teams[sourceId]) === targetId)
    .sort()
}

function failRoute(
  memberIds: string[],
  route: string[],
  error: TeamFamilyError,
): SimpleFamilyRoute {
  return { ok: false, route, memberIds, error }
}

function pushUniqueError(errors: TeamFamilyError[], error: TeamFamilyError): void {
  if (!errors.some((entry) => entry.code === error.code && entry.team === error.team && entry.reference === error.reference && entry.message === error.message)) {
    errors.push(error)
  }
}

function partialUpgradeRoute(draft: GuidedFormProjection, startId: string): string[] {
  const route: string[] = []
  const seen = new Set<string>()
  let current: string | null = startId
  while (current && !seen.has(current)) {
    if (!team(draft, current)) break
    seen.add(current)
    route.push(current)
    const currentTeam = team(draft, current)
    current = currentTeam ? upgradeTarget(currentTeam) : null
  }
  return route
}

/** Convert a label to the lower-case ASCII slug used for suggested IDs. */
export function teamIdSlug(label: string): string {
  return label
    .normalize('NFKD')
    .split('')
    .filter((character) => character.charCodeAt(0) <= 0x7f)
    .join('')
    .toLowerCase()
    .replace(/[^a-z0-9]+/gu, '_')
    .replace(/^_+|_+$/gu, '')
}

/** Return a suggestion only; collisions are never silently suffixed. */
export function suggestTeamId(label: string): string {
  return teamIdSlug(label)
}

/** Return a user-facing validation message, or null when the ID is usable. */
export function validateStableTeamId(teamId: string, occupied: Iterable<string> = []): string | null {
  if (!TEAM_ID_PATTERN.test(teamId)) {
    return 'Use 1–64 letters, numbers, underscores, or hyphens, starting with a letter, number, or underscore.'
  }
  if (new Set(occupied).has(teamId)) return `Team ID "${teamId}" is already in use.`
  return null
}

export function assertStableTeamId(teamId: string, occupied: Iterable<string> = []): void {
  const message = validateStableTeamId(teamId, occupied)
  if (message) {
    const code = new Set(occupied).has(teamId) ? 'duplicate_team_id' : 'invalid_team_id'
    throw new TeamFamilyError(code, message, teamId)
  }
}

/** Generate one child ID once, refusing both empty slugs and collisions. */
export function generateStageId(baseId: string, stageLabel: string, occupied: Iterable<string> = []): string {
  assertStableTeamId(baseId)
  const slug = teamIdSlug(stageLabel)
  if (!slug) throw new TeamFamilyError('empty_slug', 'This stage name has no usable ID slug; enter an explicit ID.')
  const candidate = `${baseId}_${slug}`
  assertStableTeamId(candidate, occupied)
  return candidate
}

function normalizedDisplayName(value: string | null | undefined): string | null {
  if (value === undefined || value === null || !value.trim()) return null
  const result = value.trim()
  if (result.length > TEAM_DISPLAY_NAME_MAX_LENGTH) {
    throw new TeamFamilyError('invalid_display_name', `Display names must be at most ${TEAM_DISPLAY_NAME_MAX_LENGTH} characters.`)
  }
  return result
}

/**
 * Detect the only topology that may use the compact stage editor. Inherited
 * values are deliberately not inspected here; this is an upgrade-graph check.
 */
export function detectSimpleFamilyRoute(draft: GuidedFormProjection, rootId: string): SimpleFamilyRoute {
  const root = team(draft, rootId)
  if (!root) return failRoute([], [], new TeamFamilyError('unknown_team', `Team "${rootId}" is not configured.`, rootId))
  const allDescendants = descendants(draft, rootId)
  const children = directChildren(draft, rootId)
  const memberIds = [rootId, ...allDescendants]
  if (normalizedReference(root.extends) !== null) {
    return failRoute(memberIds, [], new TeamFamilyError('unsupported_topology', `Team "${rootId}" is not a family root.`, rootId))
  }
  if (allDescendants.length !== children.length) {
    return failRoute(memberIds, [], new TeamFamilyError('unsupported_topology', `Family "${rootId}" contains a nested inheritance child; only direct base stages can use the simple editor.`, rootId))
  }

  const route: string[] = []
  const seen = new Set<string>()
  let current: string | null = rootId
  while (current !== null) {
    if (seen.has(current)) {
      return failRoute(memberIds, route, new TeamFamilyError('unsupported_topology', `Family "${rootId}" has a cycle in its upgrade route at "${current}".`, rootId, current))
    }
    seen.add(current)
    const currentTeam = team(draft, current)
    if (!currentTeam) {
      return failRoute(memberIds, route, new TeamFamilyError('unknown_reference', `Upgrade route references unknown team "${current}".`, rootId, current))
    }
    route.push(current)
    const next = upgradeTarget(currentTeam)
    if (next === null) break
    if (!team(draft, next)) {
      return failRoute(memberIds, route, new TeamFamilyError('unknown_reference', `Team "${current}" upgrades to unknown team "${next}".`, current, next))
    }
    if (!memberIds.includes(next)) {
      return failRoute(memberIds, route, new TeamFamilyError('unsupported_topology', `Team "${current}" upgrades outside family "${rootId}" to "${next}".`, current, next))
    }
    current = next
  }

  if (route.length !== memberIds.length || memberIds.some((teamId) => !route.includes(teamId))) {
    return failRoute(memberIds, route, new TeamFamilyError('unsupported_topology', `Family "${rootId}" does not have one upgrade path visiting every stage exactly once.`, rootId))
  }
  for (const [index, teamId] of route.entries()) {
    const expected = index + 1 < route.length ? route[index + 1] : null
    if (upgradeTarget(draft.teams[teamId]) !== expected) {
      return failRoute(memberIds, route, new TeamFamilyError('unsupported_topology', `Family "${rootId}" has an upgrade branch or gap near "${teamId}".`, teamId))
    }
  }
  for (const memberId of memberIds) {
    const externalIncoming = incomingUpgrades(draft, memberId).filter((sourceId) => !memberIds.includes(sourceId))
    if (externalIncoming.length) {
      return failRoute(memberIds, route, new TeamFamilyError('unsupported_topology', `Team "${memberId}" has an external upgrade incoming from "${externalIncoming[0]}"; the route must remain explicit.`, memberId, externalIncoming[0]))
    }
  }
  return { ok: true, route, memberIds, error: null }
}

function familyGroup(
  kind: TeamFamilyKind,
  rootId: string,
  memberIds: string[],
  route: string[],
  simpleRoute: boolean,
  reason: string | null,
  draft: GuidedFormProjection,
): TeamFamilyGroup {
  return {
    kind,
    rootId,
    memberIds,
    route,
    simpleRoute,
    reason,
    displayName: normalizedReference(draft.teams[rootId]?.display_name),
  }
}

/**
 * Group explicit families and only disjoint, unambiguous legacy chains.
 * Every configured team appears in exactly one returned group, even when an
 * invalid or ambiguous graph must remain a standalone explicit route.
 */
export function groupTeamFamilies(draft: GuidedFormProjection): TeamFamilyGrouping {
  const ids = Object.keys(draft.teams).sort()
  const errors: TeamFamilyError[] = []
  const groups: TeamFamilyGroup[] = []
  const assigned = new Set<string>()

  for (const teamId of ids) {
    const value = draft.teams[teamId]
    for (const [field, target] of [['extends', value.extends], ['upgrade_to', value.upgrade_to], ['backup_team', value.backup_team] ] as const) {
      if (target !== undefined && target !== null && !team(draft, target)) {
        pushUniqueError(errors, new TeamFamilyError('unknown_reference', `Team "${teamId}" references unknown team "${target}" in ${field}.`, teamId, target))
      }
    }
  }

  const roots = ids.filter((teamId) => normalizedReference(draft.teams[teamId].extends) === null)
  for (const rootId of roots) {
    const children = directChildren(draft, rootId)
    const incoming = incomingUpgrades(draft, rootId)
    const isExplicitBase = children.length > 0 || (upgradeTarget(draft.teams[rootId]) === null && incoming.length === 0)
    if (!isExplicitBase || assigned.has(rootId)) continue
    const memberIds = [rootId, ...descendants(draft, rootId)]
    for (const memberId of memberIds) assigned.add(memberId)
    const route = detectSimpleFamilyRoute(draft, rootId)
    if (!route.ok && route.error) pushUniqueError(errors, route.error)
    groups.push(familyGroup('family', rootId, memberIds, route.route, route.ok, route.error?.message ?? null, draft))
  }

  const remaining = ids.filter((teamId) => !assigned.has(teamId))
  const remainingSet = new Set(remaining)
  const adjacency = new Map<string, Set<string>>(remaining.map((teamId) => [teamId, new Set<string>()]))
  for (const sourceId of remaining) {
    const targetId = upgradeTarget(draft.teams[sourceId])
    if (targetId && remainingSet.has(targetId)) {
      adjacency.get(sourceId)!.add(targetId)
      adjacency.get(targetId)!.add(sourceId)
    }
  }

  const visited = new Set<string>()
  for (const startId of remaining) {
    if (visited.has(startId)) continue
    const component: string[] = []
    const queue = [startId]
    while (queue.length) {
      const current = queue.shift()!
      if (visited.has(current)) continue
      visited.add(current)
      component.push(current)
      queue.push(...adjacency.get(current) ?? [])
    }
    component.sort()

    const incomingWithin = new Map(component.map((teamId) => [teamId, incomingUpgrades(draft, teamId).filter((sourceId) => component.includes(sourceId))]))
    const externalIncoming = component.some((teamId) => incomingUpgrades(draft, teamId).some((sourceId) => !component.includes(sourceId)))
    const hasInheritance = component.some((teamId) => normalizedReference(draft.teams[teamId].extends) !== null)
    const hasExternalOutgoing = component.some((teamId) => {
      const targetId = upgradeTarget(draft.teams[teamId])
      return targetId !== null && (!remainingSet.has(targetId) || !component.includes(targetId))
    })
    const heads = component.filter((teamId) => (incomingWithin.get(teamId)?.length ?? 0) === 0)
    let legacyRoute: string[] = []
    if (component.length > 1 && !hasInheritance && !externalIncoming && !hasExternalOutgoing && heads.length === 1 && component.every((teamId) => (incomingWithin.get(teamId)?.length ?? 0) <= 1)) {
      legacyRoute = partialUpgradeRoute(draft, heads[0])
    }
    const isLegacy = legacyRoute.length === component.length && legacyRoute.every((teamId) => component.includes(teamId)) && upgradeTarget(draft.teams[legacyRoute[legacyRoute.length - 1]]) === null
    if (isLegacy) {
      for (const teamId of component) assigned.add(teamId)
      groups.push(familyGroup('legacy_chain', heads[0], legacyRoute, legacyRoute, true, null, draft))
      continue
    }

    const ambiguous = component.length > 1 || externalIncoming || hasExternalOutgoing || hasInheritance
    if (ambiguous) {
      const error = new TeamFamilyError('ambiguous_legacy_chain', `Teams ${component.map((teamId) => `"${teamId}"`).join(', ')} stay standalone because their upgrade graph is not one disjoint chain.`, component[0])
      pushUniqueError(errors, error)
      for (const teamId of component) {
        assigned.add(teamId)
        groups.push(familyGroup('standalone', teamId, [teamId], partialUpgradeRoute(draft, teamId), false, error.message, draft))
      }
    } else {
      assigned.add(startId)
      groups.push(familyGroup('standalone', startId, [startId], [startId], false, null, draft))
    }
  }

  // Teams with an invalid `extends` reference do not belong to any root.
  for (const teamId of ids) {
    if (assigned.has(teamId)) continue
    const error = new TeamFamilyError('unknown_reference', `Team "${teamId}" has an unresolved inheritance base.`, teamId, normalizedReference(draft.teams[teamId].extends) ?? undefined)
    pushUniqueError(errors, error)
    assigned.add(teamId)
    groups.push(familyGroup('standalone', teamId, [teamId], partialUpgradeRoute(draft, teamId), false, error.message, draft))
  }
  return { groups, errors }
}

/** Convenience view for consumers that only need the complete group list. */
export function familyGroups(draft: GuidedFormProjection): TeamFamilyGroup[] {
  return groupTeamFamilies(draft).groups
}

function simpleFamilyOrThrow(draft: GuidedFormProjection, rootId: string): SimpleFamilyRoute {
  const grouped = groupTeamFamilies(draft).groups.find((entry) => entry.rootId === rootId)
  const route = detectSimpleFamilyRoute(draft, rootId)
  if (!grouped || grouped.kind !== 'family') {
    throw new TeamFamilyError('unsupported_topology', `Team "${rootId}" is not an explicit family root; convert an eligible legacy chain before editing stages.`, rootId)
  }
  if (!route.ok || route.error) throw route.error ?? new TeamFamilyError('unsupported_topology', `Family "${rootId}" is not a simple upgrade route.`, rootId)
  return route
}

function tryOperation<T>(operation: () => T): TeamFamilyResult<T> {
  try {
    return { ok: true, value: operation(), error: null }
  } catch (error) {
    if (!(error instanceof TeamFamilyError)) throw error
    return { ok: false, value: null, error }
  }
}

function assertRole(draft: GuidedFormProjection, role: string): void {
  if (!role || role === 'prompts' || !own(draft.roles, role)) {
    throw new TeamFamilyError('unknown_role', `Role "${role}" is not configured.`, role)
  }
}

function assertSelector(draft: GuidedFormProjection, selector: string): void {
  const separator = selector.indexOf('.')
  const harness = separator > 0 ? selector.slice(0, separator) : ''
  const profile = separator > 0 ? selector.slice(separator + 1) : ''
  if (!harness || !profile || !draft.harnesses[harness]?.[profile]) {
    throw new TeamFamilyError('unknown_profile', `Selector "${selector}" does not name a configured profile.`, undefined, selector)
  }
}

function copiedRecord(value: Record<string, string> | undefined): Record<string, string> {
  return { ...(value ?? {}) }
}

/** Add a direct child and append it to a currently simple family route. */
export function addFamilyStage(draft: GuidedFormProjection, rootId: string, stage: FamilyStageInput): GuidedFormProjection {
  const route = simpleFamilyOrThrow(draft, rootId)
  if (!stage || typeof stage.id !== 'string') throw new TeamFamilyError('invalid_team_id', 'A stage requires an explicit stable ID.')
  assertStableTeamId(stage.id, Object.keys(draft.teams))
  const displayName = normalizedDisplayName(stage.displayName)
  const roles = copiedRecord(stage.roles)
  const prompts = copiedRecord(stage.prompts)
  for (const [role, selector] of Object.entries(roles)) {
    assertRole(draft, role)
    assertSelector(draft, selector)
  }
  for (const role of Object.keys(prompts)) assertRole(draft, role)
  const next = clone(draft)
  const tail = route.route[route.route.length - 1]
  next.teams[tail] = { ...next.teams[tail], upgrade_to: stage.id }
  next.teams[stage.id] = {
    roles,
    prompts,
    extends: rootId,
    upgrade_to: null,
    ...(displayName === null ? {} : { display_name: displayName }),
  }
  return next
}

export function tryAddFamilyStage(draft: GuidedFormProjection, rootId: string, stage: FamilyStageInput): TeamFamilyResult<GuidedFormProjection> {
  return tryOperation(() => addFamilyStage(draft, rootId, stage))
}

/** Reorder only the direct children; every base/extends declaration survives. */
export function reorderFamilyStages(draft: GuidedFormProjection, rootId: string, childOrder: readonly string[]): GuidedFormProjection {
  const route = simpleFamilyOrThrow(draft, rootId)
  const expected = route.memberIds.filter((teamId) => teamId !== rootId).sort()
  const actual = [...childOrder]
  if (new Set(actual).size !== actual.length || actual.length !== expected.length || [...actual].sort().join('\u0000') !== expected.join('\u0000')) {
    throw new TeamFamilyError('unsupported_topology', `Reorder for family "${rootId}" must include every direct stage exactly once.`, rootId)
  }
  const ordered = [rootId, ...actual]
  const next = clone(draft)
  for (const [index, teamId] of ordered.entries()) {
    next.teams[teamId] = { ...next.teams[teamId], upgrade_to: index + 1 < ordered.length ? ordered[index + 1] : null }
  }
  return next
}

export function tryReorderFamilyStages(draft: GuidedFormProjection, rootId: string, childOrder: readonly string[]): TeamFamilyResult<GuidedFormProjection> {
  return tryOperation(() => reorderFamilyStages(draft, rootId, childOrder))
}

/** Remove one reviewed stage, rewiring only its predecessor/successor edge. */
export function removeFamilyStage(
  draft: GuidedFormProjection,
  rootId: string,
  stageId: string,
  options: RemoveFamilyStageOptions = {},
): GuidedFormProjection {
  if (!(options.confirmed || options.confirm)) {
    throw new TeamFamilyError('confirmation_required', `Review and confirm removal of stage "${stageId}" before deleting it.`, stageId)
  }
  const route = simpleFamilyOrThrow(draft, rootId)
  if (stageId === rootId) throw new TeamFamilyError('unsupported_topology', 'The family Base cannot be removed by the stage editor.', rootId)
  const index = route.route.indexOf(stageId)
  if (index < 0) throw new TeamFamilyError('unknown_team', `Stage "${stageId}" is not a member of family "${rootId}".`, stageId, rootId)
  for (const [teamId, value] of Object.entries(draft.teams)) {
    if (teamId === stageId) continue
    if (normalizedReference(value.extends) === stageId) throw new TeamFamilyError('unknown_reference', `Stage "${stageId}" still has inheritance child "${teamId}".`, stageId, teamId)
    if (normalizedReference(value.backup_team) === stageId) throw new TeamFamilyError('unknown_reference', `Stage "${stageId}" is still referenced as backup_team by "${teamId}".`, stageId, teamId)
    if (upgradeTarget(value) === stageId && teamId !== route.route[index - 1]) throw new TeamFamilyError('unknown_reference', `Stage "${stageId}" still has external upgrade incoming from "${teamId}".`, stageId, teamId)
  }
  for (const [workflow, configuredTeam] of Object.entries(draft.workflow_default_teams)) {
    if (configuredTeam === stageId) throw new TeamFamilyError('unknown_reference', `Workflow "${workflow}" still uses stage "${stageId}" as its default team.`, stageId, workflow)
  }
  const next = clone(draft)
  const predecessor = route.route[index - 1]
  next.teams[predecessor] = { ...next.teams[predecessor], upgrade_to: route.route[index + 1] ?? null }
  delete next.teams[stageId]
  return next
}

export function tryRemoveFamilyStage(
  draft: GuidedFormProjection,
  rootId: string,
  stageId: string,
  options: RemoveFamilyStageOptions = {},
): TeamFamilyResult<GuidedFormProjection> {
  return tryOperation(() => removeFamilyStage(draft, rootId, stageId, options))
}

/** Set or remove one sparse role declaration; effective values stay server-owned. */
export function setDeclaredRoleOverride(draft: GuidedFormProjection, teamId: string, role: string, selector: string | null): GuidedFormProjection {
  requireTeam(draft, teamId)
  assertRole(draft, role)
  if (selector !== null) {
    if (!selector.trim()) throw new TeamFamilyError('unknown_profile', `Role "${role}" needs a profile selector.`, teamId, selector)
    assertSelector(draft, selector)
  }
  const next = clone(draft)
  const roles = copiedRecord(next.teams[teamId].roles)
  if (selector === null) delete roles[role]
  else roles[role] = selector
  next.teams[teamId] = { ...next.teams[teamId], roles }
  return next
}

export function trySetDeclaredRoleOverride(draft: GuidedFormProjection, teamId: string, role: string, selector: string | null): TeamFamilyResult<GuidedFormProjection> {
  return tryOperation(() => setDeclaredRoleOverride(draft, teamId, role, selector))
}

/** Set or remove one sparse prompt declaration, including an explicit empty string. */
export function setDeclaredPromptOverride(draft: GuidedFormProjection, teamId: string, role: string, text: string | null): GuidedFormProjection {
  requireTeam(draft, teamId)
  assertRole(draft, role)
  if (text !== null && typeof text !== 'string') throw new TeamFamilyError('conversion_preview_mismatch', `Prompt override for "${role}" must be text.`, teamId, role)
  const next = clone(draft)
  const prompts = copiedRecord(next.teams[teamId].prompts)
  if (text === null) delete prompts[role]
  else prompts[role] = text
  next.teams[teamId] = { ...next.teams[teamId], prompts }
  return next
}

export function trySetDeclaredPromptOverride(draft: GuidedFormProjection, teamId: string, role: string, text: string | null): TeamFamilyResult<GuidedFormProjection> {
  return tryOperation(() => setDeclaredPromptOverride(draft, teamId, role, text))
}

/** Set/remove direct inheritance while rejecting deeper or cyclic bases. */
export function setTeamBase(draft: GuidedFormProjection, teamId: string, baseId: string | null): GuidedFormProjection {
  requireTeam(draft, teamId)
  if (baseId === teamId) throw new TeamFamilyError('self_reference', `Team "${teamId}" cannot extend itself.`, teamId, baseId)
  if (baseId !== null) {
    const base = requireTeam(draft, baseId)
    if (normalizedReference(base.extends) !== null) throw new TeamFamilyError('unsupported_topology', `Team "${teamId}" cannot extend child team "${baseId}".`, teamId, baseId)
    if (descendants(draft, teamId).includes(baseId)) throw new TeamFamilyError('self_reference', `Team "${teamId}" would create an inheritance cycle through "${baseId}".`, teamId, baseId)
    const children = directChildren(draft, teamId)
    if (children.length) throw new TeamFamilyError('unsupported_topology', `Team "${teamId}" has direct stages; changing its base would create nested inheritance.`, teamId)
  }
  const next = clone(draft)
  if (baseId === null) {
    const updated = { ...next.teams[teamId] }
    delete updated.extends
    next.teams[teamId] = updated
  } else next.teams[teamId] = { ...next.teams[teamId], extends: baseId }
  return next
}

export function trySetTeamBase(draft: GuidedFormProjection, teamId: string, baseId: string | null): TeamFamilyResult<GuidedFormProjection> {
  return tryOperation(() => setTeamBase(draft, teamId, baseId))
}

export function setTeamDisplayName(draft: GuidedFormProjection, teamId: string, displayName: string | null): GuidedFormProjection {
  requireTeam(draft, teamId)
  const next = clone(draft)
  const updated = { ...next.teams[teamId] }
  const normalized = normalizedDisplayName(displayName)
  if (normalized === null) delete updated.display_name
  else updated.display_name = normalized
  next.teams[teamId] = updated
  return next
}

export function trySetTeamDisplayName(draft: GuidedFormProjection, teamId: string, displayName: string | null): TeamFamilyResult<GuidedFormProjection> {
  return tryOperation(() => setTeamDisplayName(draft, teamId, displayName))
}

export function setTeamUpgrade(draft: GuidedFormProjection, teamId: string, upgradeTo: string | null): GuidedFormProjection {
  requireTeam(draft, teamId)
  if (upgradeTo === teamId) throw new TeamFamilyError('self_reference', `Team "${teamId}" cannot upgrade to itself.`, teamId, upgradeTo)
  if (upgradeTo !== null) requireTeam(draft, upgradeTo)
  const next = clone(draft)
  next.teams[teamId] = { ...next.teams[teamId], upgrade_to: upgradeTo }
  const route = partialUpgradeRoute(next, teamId)
  if (new Set(route).size !== route.length) throw new TeamFamilyError('unsupported_topology', `Changing "${teamId}" would create an upgrade cycle.`, teamId, upgradeTo ?? undefined)
  return next
}

export function trySetTeamUpgrade(draft: GuidedFormProjection, teamId: string, upgradeTo: string | null): TeamFamilyResult<GuidedFormProjection> {
  return tryOperation(() => setTeamUpgrade(draft, teamId, upgradeTo))
}

function effectiveMap(summary: GuidedTeamSummary, field: TeamField, teamId: string): Record<string, string> {
  const value = summary[field]
  if (!value) throw new TeamFamilyError('canonical_preview_required', `Server preview did not provide effective ${field === 'effective_roles' ? 'roles' : 'prompts'} for team "${teamId}".`, teamId)
  return value
}

function equalStringMap(left: Record<string, string>, right: Record<string, string>): boolean {
  const leftEntries = Object.entries(left).sort(([a], [b]) => a.localeCompare(b))
  const rightEntries = Object.entries(right).sort(([a], [b]) => a.localeCompare(b))
  return JSON.stringify(leftEntries) === JSON.stringify(rightEntries)
}

function convertedDeclarations(
  base: Record<string, string>,
  child: GuidedTeamSummary,
  field: 'roles' | 'prompts',
  effectiveField: TeamField,
  teamId: string,
): { values: Record<string, string>; removed: string[]; retained: string[] } {
  const childDeclared = copiedRecord(child[field])
  const childEffective = effectiveMap(child, effectiveField, teamId)
  const keys = new Set([...Object.keys(childDeclared), ...Object.keys(childEffective), ...Object.keys(base)])
  const values: Record<string, string> = {}
  const removed: string[] = []
  const retained: string[] = []
  for (const key of [...keys].sort()) {
    const hasOldEffective = own(childEffective, key)
    const hasBaseEffective = own(base, key)
    if (!hasOldEffective) {
      if (own(childDeclared, key) || hasBaseEffective) {
        throw new TeamFamilyError('unrepresentable_absence', `Conversion cannot preserve the absent ${field.slice(0, -1)} "${key}" on team "${teamId}" after it inherits from the base.`, teamId, key)
      }
      continue
    }
    if (hasBaseEffective && childEffective[key] === base[key]) {
      if (own(childDeclared, key)) removed.push(key)
      continue
    }
    values[key] = childEffective[key]
    if (own(childDeclared, key)) retained.push(key)
    else retained.push(key)
  }
  return { values, removed, retained }
}

/**
 * Build the opt-in conversion candidate from an unambiguous legacy chain.
 * The input projection must already contain server effective maps; this
 * function never recreates the inheritance resolver in the browser.
 */
export function convertLegacyChain(draft: GuidedFormProjection, rootId: string): LegacyConversionPlan {
  const grouping = groupTeamFamilies(draft)
  const group = grouping.groups.find((entry) => entry.rootId === rootId)
  if (!group || group.kind !== 'legacy_chain' || !group.simpleRoute) {
    throw new TeamFamilyError('ambiguous_legacy_chain', `Only an unambiguous simple legacy chain can be converted to family "${rootId}".`, rootId)
  }
  const root = requireTeam(draft, rootId)
  const baseRoles = effectiveMap(root, 'effective_roles', rootId)
  const basePrompts = effectiveMap(root, 'effective_prompts', rootId)
  const next = clone(draft)
  const changes: LegacyConversionChange[] = []
  for (const teamId of group.route.slice(1)) {
    const child = requireTeam(draft, teamId)
    if (normalizedReference(child.extends) !== null) throw new TeamFamilyError('unsupported_topology', `Team "${teamId}" already has an inheritance declaration and cannot be legacy-converted.`, teamId)
    const roles = convertedDeclarations(baseRoles, child, 'roles', 'effective_roles', teamId)
    const prompts = convertedDeclarations(basePrompts, child, 'prompts', 'effective_prompts', teamId)
    next.teams[teamId] = {
      ...next.teams[teamId],
      extends: rootId,
      roles: roles.values,
      prompts: prompts.values,
    }
    changes.push({ teamId, field: 'roles', removed: roles.removed, retained: roles.retained })
    changes.push({ teamId, field: 'prompts', removed: prompts.removed, retained: prompts.retained })
  }
  return { rootId, memberIds: [...group.route], draft: next, changes }
}

export function tryConvertLegacyChain(draft: GuidedFormProjection, rootId: string): TeamFamilyResult<LegacyConversionPlan> {
  return tryOperation(() => convertLegacyChain(draft, rootId))
}

/** Compare two server projections and verify that conversion changed no effective behavior. */
export function validateLegacyConversionPreview(
  before: GuidedFormProjection,
  after: GuidedFormProjection,
  conversion: Pick<LegacyConversionPlan, 'rootId' | 'memberIds'>,
): LegacyConversionValidation {
  const fail = (error: TeamFamilyError): LegacyConversionValidation => ({
    valid: false,
    memberIds: [...conversion.memberIds],
    warning: 'Later edits to the family Base will propagate to converted stages.',
    error,
  })
  const beforeIds = Object.keys(before.teams).sort()
  const afterIds = Object.keys(after.teams).sort()
  if (JSON.stringify(beforeIds) !== JSON.stringify(afterIds)) {
    return fail(new TeamFamilyError('conversion_preview_mismatch', 'Conversion preview changed the configured team IDs.', conversion.rootId))
  }
  for (const teamId of Object.keys(before.teams)) {
    if (!after.teams[teamId]) return fail(new TeamFamilyError('conversion_preview_mismatch', `Conversion preview removed team "${teamId}" unexpectedly.`, teamId))
    for (const field of ['upgrade_to', 'backup_team'] as const) {
      if (normalizedReference(before.teams[teamId][field]) !== normalizedReference(after.teams[teamId][field])) {
        return fail(new TeamFamilyError('conversion_preview_mismatch', `Conversion preview changed teams.${teamId}.${field}; links must be preserved.`, teamId, field))
      }
    }
    if (normalizedReference(before.teams[teamId].display_name) !== normalizedReference(after.teams[teamId].display_name)) {
      return fail(new TeamFamilyError('conversion_preview_mismatch', `Conversion preview changed teams.${teamId}.display_name; labels must be preserved.`, teamId, 'display_name'))
    }
  }
  const root = after.teams[conversion.rootId]
  if (!root) return fail(new TeamFamilyError('conversion_preview_mismatch', `Conversion preview is missing family Base "${conversion.rootId}".`, conversion.rootId))
  if (normalizedReference(before.teams[conversion.rootId].extends) !== normalizedReference(root.extends)) {
    return fail(new TeamFamilyError('conversion_preview_mismatch', `Conversion preview changed the Base inheritance declaration.`, conversion.rootId, 'extends'))
  }
  for (const teamId of conversion.memberIds) {
    const beforeTeam = before.teams[teamId]
    const afterTeam = after.teams[teamId]
    if (!beforeTeam || !afterTeam) return fail(new TeamFamilyError('conversion_preview_mismatch', `Conversion preview is missing team "${teamId}".`, teamId))
    if (teamId !== conversion.rootId && normalizedReference(afterTeam.extends) !== conversion.rootId) {
      return fail(new TeamFamilyError('conversion_preview_mismatch', `Converted stage "${teamId}" does not extend Base "${conversion.rootId}".`, teamId, conversion.rootId))
    }
    if (!equalStringMap(effectiveMap(beforeTeam, 'effective_roles', teamId), effectiveMap(afterTeam, 'effective_roles', teamId))) {
      return fail(new TeamFamilyError('conversion_preview_mismatch', `Conversion changed effective roles for team "${teamId}".`, teamId))
    }
    if (!equalStringMap(effectiveMap(beforeTeam, 'effective_prompts', teamId), effectiveMap(afterTeam, 'effective_prompts', teamId))) {
      return fail(new TeamFamilyError('conversion_preview_mismatch', `Conversion changed effective prompts for team "${teamId}".`, teamId))
    }
  }
  return {
    valid: true,
    memberIds: [...conversion.memberIds],
    warning: 'Later edits to the family Base will propagate to converted stages.',
    error: null,
  }
}

export function tryValidateLegacyConversionPreview(
  before: GuidedFormProjection,
  after: GuidedFormProjection,
  conversion: Pick<LegacyConversionPlan, 'rootId' | 'memberIds'>,
): TeamFamilyResult<LegacyConversionValidation> {
  return tryOperation(() => {
    const result = validateLegacyConversionPreview(before, after, conversion)
    if (!result.valid && result.error) throw result.error
    return result
  })
}
