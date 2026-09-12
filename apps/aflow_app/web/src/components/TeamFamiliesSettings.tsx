import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import type { GuidedFormProjection, GuidedTeamSummary } from '../types'
import type { LegacyConversionPreviewResult } from '../settingsDraft'
import {
  removeFamilyStage,
  reorderFamilyStages,
  setDeclaredPromptOverride,
  setDeclaredRoleOverride,
  setTeamDisplayName,
  setTeamUpgrade,
  TeamFamilyError,
  type TeamFamilyGroup,
  groupTeamFamilies,
} from '../teamFamilies'
import { formatMachineChoice, formatMachineLabel } from '../label'
import { Combobox } from './Combobox'
import { SidebarEditorLayout } from './SidebarEditorLayout'
import { TeamFamilyWizard } from './TeamFamilyWizard'

type DraftChange = (next: GuidedFormProjection) => void
type DisplayNameDraft = { value: string; source: string }

export interface TeamFamiliesSettingsProps {
  draft: GuidedFormProjection
  baseline: GuidedFormProjection | null
  selectedTeam: string
  navigationVersion: number
  active?: boolean
  wizardResetVersion?: number
  onSelectTeam: (teamId: string) => void
  onNavigate: () => void
  onChange: DraftChange
  newTeamName: string
  newTeamError: string | null
  onNewTeamNameChange: (value: string) => void
  onAddTeam: () => void
  onOpenPrompt: (teamId: string, role: string) => void
  onPreviewConversion: (rootId: string, draft: GuidedFormProjection) => Promise<LegacyConversionPreviewResult>
  onAddFamily: (candidate: GuidedFormProjection, rootId: string) => Promise<void>
  previewPending?: boolean
  previewError?: string | null
  previewReady?: boolean
  onWizardDirtyChange?: (dirty: boolean) => void
}

function own<T extends object>(value: T, key: PropertyKey): boolean {
  return Object.prototype.hasOwnProperty.call(value, key)
}

function normalized(value: string | null | undefined): string | null {
  return value === undefined || value === null || !value.trim() ? null : value.trim()
}

function sortedUnique(values: Iterable<string>): string[] {
  return [...new Set(values)].sort()
}

function stageLabel(draft: GuidedFormProjection, rootId: string, teamId: string, kind: TeamFamilyGroup['kind'] = 'family'): string {
  if (teamId === rootId && kind === 'family') return 'Base'
  return normalized(draft.teams[teamId]?.display_name) ?? formatMachineLabel(teamId)
}

function rootLabel(group: TeamFamilyGroup): string {
  return group.displayName ?? formatMachineLabel(group.rootId)
}

function hasGroupLabelCollision(group: TeamFamilyGroup, groups: TeamFamilyGroup[]): boolean {
  const label = rootLabel(group)
  return groups.some(other => other.rootId !== group.rootId && rootLabel(other) === label)
}

function groupButtonLabel(group: TeamFamilyGroup, groups: TeamFamilyGroup[]): string {
  const label = rootLabel(group)
  return hasGroupLabelCollision(group, groups) ? `${label} (${group.rootId})` : label
}

function kindLabel(group: TeamFamilyGroup): string {
  if (group.kind === 'legacy_chain') return 'Legacy chain'
  if (group.kind === 'family') return 'Family'
  return 'Standalone'
}

function routeForDisplay(group: TeamFamilyGroup): string[] {
  const route = group.route.length ? [...group.route] : []
  return [...route, ...group.memberIds.filter(teamId => !route.includes(teamId))]
}

function mapsDiffer(left: Record<string, string> | undefined, right: Record<string, string> | undefined): number | null {
  if (!left || !right) return null
  const roles = sortedUnique([...Object.keys(left), ...Object.keys(right)])
  return roles.filter(role => left[role] !== right[role]).length
}

function changedRoleCount(draft: GuidedFormProjection, group: TeamFamilyGroup): number | null {
  const base = draft.teams[group.rootId]?.effective_roles
  if (!base) return null
  let count = 0
  for (const teamId of group.memberIds.filter(id => id !== group.rootId)) {
    const changed = mapsDiffer(base, draft.teams[teamId]?.effective_roles)
    if (changed === null) return null
    count += changed
  }
  return count
}

function sourceLabel(draft: GuidedFormProjection, rootId: string, source: string | undefined): string {
  if (!source) return 'canonical source unavailable'
  if (source === 'global') return 'global'
  if (source === rootId) return `declared by Base (${rootId})`
  return draft.teams[source]
    ? `declared by ${stageLabel(draft, rootId, source)} (${source})`
    : source
}

function draftUpgradeChain(draft: GuidedFormProjection, start: string): { chain: string[]; error: string | null } {
  const chain = [start]
  const seen = new Set([start])
  let current = draft.teams[start]?.upgrade_to ?? null
  while (current) {
    if (seen.has(current)) return { chain, error: `Upgrade chain has a cycle at "${current}". Choose a different target.` }
    if (!draft.teams[current]) return { chain, error: `Upgrade target "${current}" does not exist. Choose a configured team.` }
    seen.add(current)
    chain.push(current)
    current = draft.teams[current]?.upgrade_to ?? null
  }
  return { chain, error: null }
}

function workerText(draft: GuidedFormProjection, teamId: string): string {
  const summary = draft.teams[teamId]
  const selector = summary?.effective_roles?.worker ?? summary?.roles.worker ?? draft.roles.worker
  if (!selector) return ''
  const separator = selector.indexOf('.')
  const profile = separator > 0 ? draft.harnesses[selector.slice(0, separator)]?.[selector.slice(separator + 1)] : undefined
  const modelEffort = profile ? [profile.model, profile.effort ? `effort ${profile.effort}` : null].filter(Boolean).join(', ') : ''
  return [selector, modelEffort].filter(Boolean).join(' · ')
}

function selectorSummary(draft: GuidedFormProjection, selector: string | undefined): string {
  if (!selector) return 'No canonical assignment'
  const separator = selector.indexOf('.')
  const profile = separator > 0 ? draft.harnesses[selector.slice(0, separator)]?.[selector.slice(separator + 1)] : undefined
  if (!profile) return selector
  const details = [profile.model, profile.effort ? `effort ${profile.effort}` : null].filter(Boolean).join(', ')
  return details ? `${selector} · ${details}` : selector
}

function tomlString(value: string): string {
  return JSON.stringify(value)
}

function declaredToml(teamId: string, summary: GuidedTeamSummary): string {
  const lines = [`[teams.${tomlString(teamId)}]`]
  for (const [key, value] of [
    ['display_name', summary.display_name],
    ['extends', summary.extends],
    ['upgrade_to', summary.upgrade_to],
    ['backup_team', summary.backup_team],
  ] as const) {
    if (value !== undefined && value !== null) lines.push(`${key} = ${tomlString(value)}`)
  }
  const roles = summary.roles ?? {}
  if (Object.keys(roles).length) {
    lines.push('', `[teams.${tomlString(teamId)}.roles]`)
    for (const role of Object.keys(roles).sort()) lines.push(`${role} = ${tomlString(roles[role])}`)
  }
  const prompts = summary.prompts ?? {}
  if (Object.keys(prompts).length) {
    lines.push('', `[teams.${tomlString(teamId)}.prompts]`)
    for (const role of Object.keys(prompts).sort()) lines.push(`${role} = ${tomlString(prompts[role])}`)
  }
  if (lines.length === 1) lines.push('# no local declarations')
  return lines.join('\n')
}

function errorFrom(reason: unknown): TeamFamilyError {
  if (reason instanceof TeamFamilyError) return reason
  return new TeamFamilyError('unsupported_topology', reason instanceof Error ? reason.message : 'The family edit could not be applied.')
}

function stageRouteIds(draft: GuidedFormProjection, group: TeamFamilyGroup): string[] {
  const route = routeForDisplay(group)
  return route.filter(teamId => teamId in draft.teams)
}

export function TeamFamiliesSettings({
  draft,
  baseline,
  selectedTeam,
  navigationVersion,
  active = true,
  wizardResetVersion = 0,
  onSelectTeam,
  onNavigate,
  onChange,
  newTeamName,
  newTeamError,
  onNewTeamNameChange,
  onAddTeam,
  onOpenPrompt,
  onPreviewConversion,
  onAddFamily,
  previewPending = false,
  previewError = null,
  previewReady = true,
  onWizardDirtyChange,
}: TeamFamiliesSettingsProps) {
  const grouping = useMemo(() => groupTeamFamilies(draft), [draft])
  const groups = grouping.groups
  const selectedGroup = groups.find(group => group.memberIds.includes(selectedTeam)) ?? groups[0] ?? null
  const activeTeamId = selectedGroup && selectedGroup.memberIds.includes(selectedTeam)
    ? selectedTeam
    : selectedGroup?.rootId ?? ''
  const activeSummary = activeTeamId ? draft.teams[activeTeamId] : undefined
  const selectors = useMemo(() => Object.entries(draft.harnesses)
    .flatMap(([harness, profiles]) => Object.keys(profiles).map(profile => `${harness}.${profile}`))
    .sort(), [draft.harnesses])
  const teamNames = Object.keys(draft.teams).sort()
  const [operationError, setOperationError] = useState<TeamFamilyError | null>(null)
  const [confirmRemove, setConfirmRemove] = useState<string | null>(null)
  const [conversionPreview, setConversionPreview] = useState<LegacyConversionPreviewResult | null>(null)
  const [conversionBusy, setConversionBusy] = useState(false)
  const [wizardOpen, setWizardOpen] = useState(false)
  const [wizardMounted, setWizardMounted] = useState(false)
  const [displayNameDrafts, setDisplayNameDrafts] = useState<Record<string, DisplayNameDraft>>({})
  const conversionRequestRef = useRef(0)

  useEffect(() => {
    conversionRequestRef.current += 1
    setConversionPreview(null)
    setConfirmRemove(null)
    setOperationError(null)
    setConversionBusy(false)
  }, [draft, selectedGroup?.rootId])

  useEffect(() => {
    if (!activeTeamId || !activeSummary) return
    const source = activeSummary.display_name ?? ''
    setDisplayNameDrafts(current => {
      const existing = current[activeTeamId]
      if (existing?.source === source) return current
      return { ...current, [activeTeamId]: { value: source, source } }
    })
  }, [activeTeamId, activeSummary, displayNameDrafts])

  function apply(operation: () => GuidedFormProjection): boolean {
    try {
      onChange(operation())
      setOperationError(null)
      return true
    } catch (reason) {
      setOperationError(errorFrom(reason))
      return false
    }
  }

  function selectGroup(group: TeamFamilyGroup): void {
    setWizardOpen(false)
    const nextTeam = group.memberIds.includes(selectedTeam) ? selectedTeam : group.rootId
    onSelectTeam(nextTeam)
    onNavigate()
  }

  function selectStage(teamId: string): void {
    onSelectTeam(teamId)
  }

  function startWizard(): void {
    setWizardMounted(true)
    setWizardOpen(true)
    onNavigate()
  }

  function stageDisplay(teamId: string): string {
    return stageLabel(draft, selectedGroup?.rootId ?? teamId, teamId, selectedGroup?.kind ?? 'standalone')
  }

  function previewConversion(): void {
    if (!selectedGroup || selectedGroup.kind !== 'legacy_chain') return
    const request = ++conversionRequestRef.current
    setConversionBusy(true)
    setConversionPreview(null)
    setOperationError(null)
    void onPreviewConversion(selectedGroup.rootId, draft).then(result => {
      if (conversionRequestRef.current !== request) return
      setConversionPreview(result)
    }).catch(reason => {
      if (conversionRequestRef.current !== request) return
      setOperationError(errorFrom(reason))
    }).finally(() => {
      if (conversionRequestRef.current === request) setConversionBusy(false)
    })
  }

  function applyConversion(): void {
    if (!conversionPreview?.valid) return
    onChange(conversionPreview.conversion.draft)
    setConversionPreview(null)
    setOperationError(null)
  }

  const activeRootId = selectedGroup?.rootId ?? activeTeamId
  const activeRoute = selectedGroup ? stageRouteIds(draft, selectedGroup) : []
  const activeUpgradeChain = activeTeamId ? draftUpgradeChain(draft, activeTeamId) : { chain: [], error: null }
  const rootUpgradeChain = selectedGroup ? draftUpgradeChain(draft, selectedGroup.rootId) : { chain: [], error: null }
  const workerRoute = selectedGroup?.kind === 'family' && !selectedGroup.simpleRoute
    ? rootUpgradeChain.chain
    : activeUpgradeChain.error
      ? activeUpgradeChain.chain
      : selectedGroup?.route.length
        ? selectedGroup.route
        : activeUpgradeChain.chain
  const fullRoute = selectedGroup?.kind === 'family' && !selectedGroup.simpleRoute
    ? rootUpgradeChain.chain
    : selectedGroup ? routeForDisplay(selectedGroup) : []
  const roleNames = activeSummary ? sortedUnique([
    ...Object.keys(draft.roles),
    ...Object.keys(activeSummary.roles),
    ...Object.keys(activeSummary.effective_roles ?? {}),
    ...Object.keys(activeSummary.role_sources ?? {}),
  ]) : []
  const explicitRoles = activeSummary ? Object.keys(activeSummary.roles).sort() : []
  const inheritedRoles = roleNames.filter(role => !own(activeSummary?.roles ?? {}, role))
  const promptNames = activeSummary ? sortedUnique([
    ...roleNames,
    ...Object.keys(activeSummary.prompts ?? {}),
    ...Object.keys(activeSummary.effective_prompts ?? {}),
    ...Object.keys(activeSummary.prompt_sources ?? {}),
  ]) : []
  const preferredRoles = ['worker', 'reviewer'].filter(role => roleNames.includes(role))
  const otherRoles = roleNames.filter(role => !preferredRoles.includes(role))
  const isSimpleFamily = selectedGroup?.kind === 'family' && selectedGroup.simpleRoute
  const activeRouteIndex = activeRoute.indexOf(activeTeamId)
  const precedingTeamId = activeRouteIndex > 0 ? activeRoute[activeRouteIndex - 1] : null
  const workerUnchanged = Boolean(
    precedingTeamId
      && activeSummary?.effective_roles?.worker
      && activeSummary.effective_roles.worker === draft.teams[precedingTeamId]?.effective_roles?.worker,
  )

  function roleLabel(role: string): string {
    return formatMachineChoice(role, roleNames)
  }

  function updateRole(role: string, selector: string | null): void {
    apply(() => setDeclaredRoleOverride(draft, activeTeamId, role, selector))
  }

  function updatePrompt(role: string, text: string | null): void {
    apply(() => setDeclaredPromptOverride(draft, activeTeamId, role, text))
  }

  function renderOverrideRole(role: string): ReactNode {
    const declared = activeSummary?.roles[role] ?? ''
    return <div className="team-family-role-row" key={`override-${role}`}>
      <div className="team-family-role-heading">
        <strong>{roleLabel(role)}</strong>
        <span className="text-xs text-dim">Stored override · declared by {stageDisplay(activeTeamId)} ({activeTeamId})</span>
      </div>
      <Combobox
        label={roleLabel(role)}
        visibleLabel="Configured profile"
        value={declared}
        options={selectors}
        optionLabel={selector => selectorSummary(draft, selector)}
        onChange={selector => updateRole(role, selector)}
        emptyOption="No configured profiles"
      />
      <div className="team-family-role-actions">
        <span className="mono text-sm">{selectorSummary(draft, declared)}</span>
        <button type="button" className="btn btn-secondary btn-sm" onClick={() => updateRole(role, null)}>Restore inheritance</button>
      </div>
    </div>
  }

  function renderInheritedRole(role: string): ReactNode {
    const effective = activeSummary?.effective_roles?.[role]
    const source = sourceLabel(draft, activeRootId, activeSummary?.role_sources?.[role])
    return <div className="team-family-role-row team-family-inherited-row" key={`inherited-${role}`}>
      <div className="team-family-role-heading">
        <strong>{roleLabel(role)}</strong>
        <span className="text-xs text-dim">Inherited from {source}</span>
      </div>
      <span className="mono text-sm team-family-effective-value">{selectorSummary(draft, effective)}</span>
      <button
        type="button"
        className="btn btn-secondary btn-sm"
        disabled={!effective}
        onClick={() => effective && updateRole(role, effective)}
      >Override role
      </button>
    </div>
  }

  function renderBaseRole(role: string): ReactNode {
    if (own(activeSummary?.roles ?? {}, role)) return renderOverrideRole(role)
    const effective = activeSummary?.effective_roles?.[role]
    const source = sourceLabel(draft, activeRootId, activeSummary?.role_sources?.[role])
    return <div className="team-family-role-row team-family-inherited-row" key={`base-${role}`}>
      <div className="team-family-role-heading">
        <strong>{roleLabel(role)}</strong>
        <span className="text-xs text-dim">Inherited from {source} · choose a local override to diverge</span>
      </div>
      <Combobox
        label={roleLabel(role)}
        visibleLabel="Configured profile"
        value=""
        options={selectors}
        optionLabel={selector => selectorSummary(draft, selector)}
        resolvedDisplay={effective ? selectorSummary(draft, effective) : null}
        resolvedBadge={effective ? 'Inherited' : undefined}
        onChange={selector => updateRole(role, selector)}
        emptyOption="No configured profiles"
      />
      <div className="team-family-role-actions">
        <span className="mono text-sm">{selectorSummary(draft, effective)}</span>
      </div>
    </div>
  }

  function renderPrompt(role: string): ReactNode {
    const declared = own(activeSummary?.prompts ?? {}, role)
    const text = declared ? activeSummary?.prompts?.[role] : activeSummary?.effective_prompts?.[role]
    const source = declared
      ? `declared by ${stageDisplay(activeTeamId)} (${activeTeamId})`
      : sourceLabel(draft, activeRootId, activeSummary?.prompt_sources?.[role])
    return <div className="team-family-prompt-row" key={role}>
      <div>
        <strong>{roleLabel(role)}</strong>
        <span className="text-xs text-dim">{declared ? 'Stored override' : `Inherited from ${source}`}</span>
      </div>
      <p className="team-family-prompt-preview">{text ?? 'No canonical prompt text'}</p>
      <div className="team-family-role-actions">
        <button type="button" className="btn btn-secondary btn-sm" onClick={() => onOpenPrompt(activeTeamId, role)}>Edit in Prompts</button>
        {declared && <button type="button" className="btn btn-secondary btn-sm" onClick={() => updatePrompt(role, null)}>Restore inheritance</button>}
      </div>
    </div>
  }

  function reorder(delta: -1 | 1): void {
    if (!selectedGroup || !isSimpleFamily || activeTeamId === selectedGroup.rootId) return
    const children = selectedGroup.route.slice(1)
    const index = children.indexOf(activeTeamId)
    const target = index + delta
    if (index < 0 || target < 0 || target >= children.length) return
    const next = [...children]
    const current = next[index]
    next[index] = next[target]
    next[target] = current
    apply(() => reorderFamilyStages(draft, selectedGroup.rootId, next))
  }

  function updateDisplayName(value: string): void {
    setDisplayNameDrafts(current => ({
      ...current,
      [activeTeamId]: {
        value,
        source: current[activeTeamId]?.source ?? activeSummary?.display_name ?? '',
      },
    }))
  }

  function commitDisplayName(): void {
    const value = displayNameDrafts[activeTeamId]?.value ?? activeSummary?.display_name ?? ''
    const committed = value.trim()
    if (!apply(() => setTeamDisplayName(draft, activeTeamId, committed || null))) return
    setDisplayNameDrafts(current => {
      const next = { ...current }
      delete next[activeTeamId]
      return next
    })
  }

  function updateUpgrade(value: string): void {
    apply(() => setTeamUpgrade(draft, activeTeamId, value || null))
  }

  function removeStage(): void {
    if (!selectedGroup || !isSimpleFamily || activeTeamId === selectedGroup.rootId) return
    if (confirmRemove !== activeTeamId) {
      setConfirmRemove(activeTeamId)
      setOperationError(null)
      return
    }
    apply(() => removeFamilyStage(draft, selectedGroup.rootId, activeTeamId, { confirmed: true }))
    setConfirmRemove(null)
  }

  const detailHeading = wizardOpen ? <h3>Create team family</h3> : selectedGroup ? <div className="team-family-detail-heading-content">
    <div>
      <h3>{rootLabel(selectedGroup)}</h3>
      <p className="text-xs text-dim"><span className="mono">{selectedGroup.rootId}</span> · {kindLabel(selectedGroup)}</p>
    </div>
    <span className="team-family-summary-route">{routeForDisplay(selectedGroup).map(teamId => stageDisplay(teamId)).join(' → ')}</span>
  </div> : <h3>Teams</h3>

  return <section className="team-families-settings" aria-label="Team families">
    <SidebarEditorLayout
      selection={selectedGroup?.rootId ?? null}
      navigationVersion={navigationVersion}
      listLabel="Team families"
      active={active}
      detailHeading={detailHeading}
      navigation={<div className="team-family-navigation">
        <h3>Team families</h3>
        <button type="button" data-sidebar-editor-item="new-family" className="btn btn-primary" onClick={startWizard}>New family</button>
        <form className="add-team-form" onSubmit={event => { event.preventDefault(); onAddTeam() }}>
          <label>Add team<input className="input" aria-label="New team name" placeholder="team-name" value={newTeamName} onChange={event => onNewTeamNameChange(event.target.value)} onKeyDown={event => { if (event.key === 'Enter') { event.preventDefault(); onAddTeam() } }} /></label>
          <span className="inline-action"><button type="submit" className="btn btn-secondary">Add team</button>{newTeamError && <span role="alert" className="text-sm add-team-error">{newTeamError}</span>}</span>
        </form>
        {groups.map(group => {
          const count = changedRoleCount(draft, group)
          const route = routeForDisplay(group).map(teamId => stageLabel(draft, group.rootId, teamId, group.kind)).join(' → ')
          const labelCollision = hasGroupLabelCollision(group, groups)
          return <button
            data-sidebar-editor-item={group.rootId}
            className={`btn sidebar-entry team-family-list-entry ${selectedGroup?.rootId === group.rootId ? 'btn-primary' : 'btn-secondary'}`}
            aria-pressed={selectedGroup?.rootId === group.rootId}
            aria-label={groupButtonLabel(group, groups)}
            key={group.rootId}
            onClick={() => selectGroup(group)}
          >
            <span className="team-family-list-title">{rootLabel(group)}</span>
            <span className="team-family-list-kind">{kindLabel(group)}</span>
            {labelCollision && <span className="mono text-xs text-dim">{group.rootId}</span>}
            <span className="team-family-list-route">{route}</span>
            <span className="team-family-list-meta">{count === null ? 'Effective differences unavailable' : `${count} changed role${count === 1 ? '' : 's'}`}</span>
          </button>
        })}
        {!groups.length && <p className="text-sm text-dim">No teams are configured in this draft yet.</p>}
      </div>}
    >
      {wizardMounted && <div className="team-family-wizard-surface" hidden={!wizardOpen} aria-hidden={!wizardOpen || undefined}>
        <TeamFamilyWizard
          draft={draft}
          resetVersion={wizardResetVersion}
          onAddFamily={onAddFamily}
          onClose={() => setWizardOpen(false)}
          previewPending={previewPending}
          previewError={previewError}
          previewReady={previewReady}
          onDirtyChange={onWizardDirtyChange ?? (() => {})}
        />
      </div>}
      <div className="team-family-editor-surface" hidden={wizardOpen} aria-hidden={wizardOpen || undefined}>
      {selectedGroup && activeSummary ? <fieldset className="team-family-detail" id={`team-editor-${activeTeamId}`} tabIndex={-1}>
        <legend><span className="mono">{groupButtonLabel({ ...selectedGroup, rootId: activeTeamId, displayName: activeSummary.display_name ?? null }, groups)}</span>{!baseline?.teams[activeTeamId] && <span className="status-pill status-awaiting">new — saved with Save all</span>}</legend>
        {selectedGroup.reason && <p className="notice" role="alert">{selectedGroup.reason}</p>}
        {grouping.errors.filter(error => error.team && selectedGroup.memberIds.includes(error.team) && error.message !== selectedGroup.reason).map(error => <p className="notice" role="alert" key={`${error.code}-${error.team}-${error.reference}`}>{error.message}</p>)}
        <div className="team-family-stage-selector" aria-label={`Stages in ${rootLabel(selectedGroup)}`}>
          {selectedGroup.memberIds.map(teamId => <button
            type="button"
            aria-pressed={activeTeamId === teamId}
            aria-label={teamId === selectedGroup.rootId && selectedGroup.kind === 'family' ? `Base ${teamId}` : stageLabel(draft, selectedGroup.rootId, teamId, selectedGroup.kind)}
            className={`btn ${activeTeamId === teamId ? 'btn-primary' : 'btn-secondary'}`}
            key={teamId}
            onClick={() => selectStage(teamId)}
          >
            <span>{stageLabel(draft, selectedGroup.rootId, teamId, selectedGroup.kind)}</span>
            <span className="mono text-xs">{teamId}</span>
          </button>)}
        </div>

        {selectedGroup.kind === 'legacy_chain' && <div className="team-family-conversion card">
          <div>
            <h4>Legacy chain</h4>
            <p className="text-sm">This is a display grouping of standalone teams. Conversion adds direct Base inheritance only after a server preview confirms every effective role and prompt is unchanged.</p>
          </div>
          <button type="button" className="btn btn-secondary" disabled={conversionBusy} onClick={previewConversion}>{conversionBusy ? 'Previewing conversion…' : 'Preview convert to family'}</button>
        </div>}

        {conversionPreview && conversionPreview.conversion.rootId === selectedGroup.rootId && <div className="team-family-conversion-preview card" role="region" aria-label="Legacy conversion preview">
          <h4>Conversion preview</h4>
          <p className="text-sm">{conversionPreview.validation.warning}</p>
          {conversionPreview.conversion.changes.length > 0 && <ul className="team-family-change-list">
            {conversionPreview.conversion.changes.filter(change => change.removed.length || change.retained.length).map(change => <li key={`${change.teamId}-${change.field}`}><span className="mono">{change.teamId}.{change.field}</span>: {change.removed.length ? `remove ${change.removed.join(', ')}` : 'no removals'}{change.retained.length ? `; retain ${change.retained.join(', ')}` : ''}</li>)}
          </ul>}
          {conversionPreview.error && <p className="error-message" role="alert">{conversionPreview.error.message}</p>}
          {conversionPreview.valid && <button type="button" className="btn btn-primary" onClick={applyConversion}>Add conversion to draft</button>}
          {!conversionPreview.valid && <p className="text-xs text-dim">The candidate remains preview-only; no draft values were changed.</p>}
        </div>}

        <div className="team-family-stage-summary">
          <div>
            <h4>{stageDisplay(activeTeamId)} assignments</h4>
            <p className="text-xs text-dim"><span className="mono">{activeTeamId}</span> is a stable team ID. Display-name edits and route reordering never rename it.</p>
          </div>
          {activeTeamId !== selectedGroup.rootId && activeSummary.extends && <p className="text-sm">Inherits declared roles and prompts from <strong>{stageLabel(draft, selectedGroup.rootId, activeSummary.extends)}</strong> (<span className="mono">{activeSummary.extends}</span>).</p>}
          {activeTeamId === selectedGroup.rootId && selectedGroup.kind === 'family' && <p className="text-sm">Base assignments are the family source; stages inherit them unless they store an explicit override.</p>}
          {activeTeamId === selectedGroup.rootId && selectedGroup.kind !== 'family' && <p className="text-sm">This {kindLabel(selectedGroup).toLowerCase()} keeps its own declared assignments; no family inheritance is implied by this display grouping.</p>}
          {activeSummary.backup_team && <p className="text-sm">Backup team: <span className="mono">{activeSummary.backup_team}</span> <span className="text-xs text-dim">(separate recovery route)</span></p>}
          {workerUnchanged && <p className="notice" role="status">This stage does not change the implementation worker. Worker-quality escalation from the preceding stage is not eligible.</p>}
        </div>

        <div className="team-family-fields">
          <label>Display name<input className="input" value={displayNameDrafts[activeTeamId]?.value ?? activeSummary.display_name ?? ''} placeholder={activeTeamId === selectedGroup.rootId ? 'Family label (optional)' : 'Stage label (optional)'} onChange={event => updateDisplayName(event.target.value)} onBlur={commitDisplayName} /></label>
          <p className="text-xs text-dim">Blank removes the label. The raw ID remains <span className="mono">{activeTeamId}</span>.</p>
          {activeSummary.extends && <p className="text-sm">Base relationship: <span className="mono">{activeSummary.extends}</span> (direct declaration)</p>}
          {!activeSummary.extends && activeTeamId !== selectedGroup.rootId && <p className="text-sm">No inheritance declaration. This team remains a standalone route until conversion or an explicit base assignment.</p>}
        </div>

        <section className="team-family-section" aria-labelledby="team-family-roles-heading">
          <h4 id="team-family-roles-heading">Role assignments</h4>
          {activeTeamId !== selectedGroup.rootId && explicitRoles.length > 0 && <div className="team-family-role-list"><h5>Stored overrides</h5>{explicitRoles.map(renderOverrideRole)}</div>}
          {activeTeamId !== selectedGroup.rootId && explicitRoles.length === 0 && <p className="text-sm text-dim">No stored overrides. This stage is a valid inherited variant.</p>}
          {activeTeamId === selectedGroup.rootId && selectedGroup.kind !== 'family' && roleNames.length > 0 && <div className="team-family-role-list"><h5>Declared roles</h5>{roleNames.map(renderOverrideRole)}</div>}
          {activeTeamId === selectedGroup.rootId && selectedGroup.kind === 'family' && preferredRoles.length > 0 && <div className="team-family-role-list"><h5>Base roles</h5>{preferredRoles.map(renderBaseRole)}</div>}
          {activeTeamId === selectedGroup.rootId && selectedGroup.kind === 'family' && preferredRoles.length === 0 && <p className="text-sm text-dim">Worker and reviewer roles are not configured in this draft.</p>}
          {activeTeamId === selectedGroup.rootId && selectedGroup.kind === 'family' && otherRoles.length > 0 && <details className="team-family-disclosure" open={preferredRoles.length === 0}><summary>Other roles ({otherRoles.length}) — Show all roles</summary><div className="team-family-role-list">{otherRoles.map(renderBaseRole)}</div></details>}
          {activeTeamId !== selectedGroup.rootId && inheritedRoles.length > 0 && <details className="team-family-disclosure"><summary>Inherited roles ({inheritedRoles.length}) — Show all roles</summary><div className="team-family-role-list">{inheritedRoles.map(renderInheritedRole)}</div></details>}
          {!roleNames.length && <p className="text-sm text-dim">No configured roles are available.</p>}
        </section>

        <section className="team-family-section" aria-labelledby="team-family-prompts-heading">
          <div className="team-family-section-heading"><div><h4 id="team-family-prompts-heading">Role prompts</h4><p className="text-xs text-dim">Prompt text stays with the existing Prompts editor; this view shows its declared/effective source.</p></div></div>
          {promptNames.length ? <div className="team-family-prompt-list">{promptNames.map(renderPrompt)}</div> : <p className="text-sm text-dim">No role prompt declarations or canonical values are available.</p>}
        </section>

        <section className="team-family-section" aria-labelledby="team-family-route-heading">
          <h4 id="team-family-route-heading">Upgrade route</h4>
          <p className="text-xs text-dim">Worker chain: {workerRoute.map((teamId, index) => <span key={teamId}>{index > 0 && ' → '}<span className="mono">{formatMachineLabel(teamId)}</span>{workerText(draft, teamId) ? <> ({workerText(draft, teamId)})</> : null}</span>)}{workerRoute.length === 1 && !activeUpgradeChain.error ? ' — no further upgrade configured' : ''}</p>
          {activeUpgradeChain.error && <p className="text-sm add-team-error" role="alert">{activeUpgradeChain.error}</p>}
          <p className="team-family-full-route">{fullRoute.map(teamId => <span key={teamId}><span className="mono">{teamId}</span> <span>{stageLabel(draft, selectedGroup.rootId, teamId, selectedGroup.kind)}</span>{teamId !== fullRoute[fullRoute.length - 1] && <span className="team-family-route-arrow">→</span>}</span>)}</p>
          <label>Next upgrade<select className="input" aria-label={`Upgrade to for team ${formatMachineChoice(activeTeamId, teamNames)}`} value={activeSummary.upgrade_to ?? ''} onChange={event => updateUpgrade(event.target.value)}><option value="">None — no further upgrade</option>{teamNames.filter(teamId => teamId !== activeTeamId).map(teamId => <option key={teamId} value={teamId}>{formatMachineChoice(teamId, teamNames)}</option>)}</select></label>
          {selectedGroup.kind === 'family' && !selectedGroup.simpleRoute && <p className="notice" role="status">This family has an explicit complex route. Reorder and stage removal are disabled; use the next-upgrade controls and the full route above.</p>}
          {selectedGroup.kind === 'legacy_chain' && <p className="text-xs text-dim">Upgrade links remain standalone until an accepted conversion is added to the draft.</p>}
          {isSimpleFamily && activeTeamId !== selectedGroup.rootId && <div className="team-family-action-row">
            <button type="button" className="btn btn-secondary" disabled={activeRouteIndex <= 1} onClick={() => reorder(-1)}>Move stage earlier</button>
            <button type="button" className="btn btn-secondary" disabled={activeRouteIndex < 0 || activeRouteIndex >= activeRoute.length - 1} onClick={() => reorder(1)}>Move stage later</button>
            <button type="button" className="btn btn-danger" onClick={removeStage}>Remove stage…</button>
          </div>}
          {confirmRemove === activeTeamId && <div className="confirmation" role="alertdialog" aria-label={`Confirm removal of ${stageDisplay(activeTeamId)}`}>
            <p>Remove <strong>{stageDisplay(activeTeamId)}</strong> from the draft? The preceding upgrade link will be rewired to its successor; Save all is still required.</p>
            <div className="team-family-action-row"><button type="button" className="btn btn-danger" onClick={removeStage}>Confirm remove stage</button><button type="button" className="btn btn-secondary" onClick={() => setConfirmRemove(null)}>Cancel</button></div>
          </div>}
        </section>

        <details className="team-family-declared-toml">
          <summary>Declared TOML for this team (read-only)</summary>
          <p className="text-xs text-dim">Effective assignments and provenance are server projections and are not written here. Use Advanced TOML for the complete source documents.</p>
          <pre className="mono">{declaredToml(activeTeamId, activeSummary)}</pre>
        </details>
        {operationError && <p className="error-message" role="alert">{operationError.message}</p>}
        {baseline?.teams[activeTeamId] && !activeSummary.extends && selectedGroup.kind === 'legacy_chain' && <p className="text-xs text-dim">This legacy team is unchanged until you explicitly accept a conversion preview.</p>}
      </fieldset> : <p className="text-sm text-dim">Select a team family to inspect its declarations.</p>}
      </div>
    </SidebarEditorLayout>
  </section>
}
