import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import type { GuidedFormProjection, GuidedTeamSummary } from '../types'
import {
  generateStageId,
  suggestTeamId,
  TEAM_DISPLAY_NAME_MAX_LENGTH,
  teamIdSlug,
  TeamFamilyError,
  validateStableTeamId,
} from '../teamFamilies'
import { formatMachineChoice, formatMachineLabel } from '../label'
import { Combobox } from './Combobox'
import { TextEditor } from './TextEditor'

type WizardStep = 1 | 2 | 3
type CopySource = string | null

interface WizardStage {
  /** UI-only identity; persisted team IDs remain editable and are never used as keys. */
  uiId: string
  id: string
  displayName: string
  roles: Record<string, string>
}

interface WizardState {
  step: WizardStep
  familyName: string
  baseId: string
  baseIdEdited: boolean
  copySource: CopySource
  copyEdited: boolean
  baseRoles: Record<string, string>
  basePrompts: Record<string, string>
  stages: WizardStage[]
}

export interface TeamFamilyWizardProps {
  draft: GuidedFormProjection
  resetVersion?: number
  onAddFamily: (candidate: GuidedFormProjection, rootId: string) => Promise<void>
  onClose: () => void
  previewPending?: boolean
  previewError?: string | null
  previewReady?: boolean
  onDirtyChange: (dirty: boolean) => void
}

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T
}

function own<T extends object>(value: T, key: PropertyKey): boolean {
  return Object.prototype.hasOwnProperty.call(value, key)
}

function sortedUnique(values: Iterable<string>): string[] {
  return [...new Set(values)].sort()
}

function initialState(): WizardState {
  return {
    step: 1,
    familyName: '',
    baseId: '',
    baseIdEdited: false,
    copySource: null,
    copyEdited: false,
    baseRoles: {},
    basePrompts: {},
    stages: [],
  }
}

function teamLabel(teamId: string, draft: GuidedFormProjection, teamIds: string[]): string {
  const displayName = draft.teams[teamId]?.display_name?.trim()
  const label = displayName || formatMachineLabel(teamId)
  return teamIds.filter(id => id !== teamId).some(id => (draft.teams[id]?.display_name?.trim() || formatMachineLabel(id)) === label)
    ? `${label} (${teamId})`
    : label
}

function selectorSummary(draft: GuidedFormProjection, selector: string | undefined): string {
  if (!selector) return 'No canonical assignment'
  const separator = selector.indexOf('.')
  const profile = separator > 0 ? draft.harnesses[selector.slice(0, separator)]?.[selector.slice(separator + 1)] : undefined
  if (!profile) return selector
  const details = [profile.model, profile.effort ? `effort ${profile.effort}` : null].filter(Boolean).join(', ')
  return details ? `${selector} · ${details}` : selector
}

function copyDifferences(effective: Record<string, string>, defaults: Record<string, string> | undefined): Record<string, string> {
  return Object.fromEntries(Object.entries(effective).filter(([key, value]) => value !== defaults?.[key]))
}

function tomlString(value: string): string {
  return JSON.stringify(value)
}

function appendTeamToml(lines: string[], teamId: string, summary: GuidedTeamSummary): void {
  lines.push(`[teams.${tomlString(teamId)}]`)
  for (const [key, value] of [
    ['display_name', summary.display_name],
    ['extends', summary.extends],
    ['upgrade_to', summary.upgrade_to],
  ] as const) {
    if (value !== undefined && value !== null) lines.push(`${key} = ${tomlString(value)}`)
  }
  if (Object.keys(summary.roles).length) {
    lines.push('', `[teams.${tomlString(teamId)}.roles]`)
    for (const role of Object.keys(summary.roles).sort()) lines.push(`${role} = ${tomlString(summary.roles[role])}`)
  }
  if (Object.keys(summary.prompts ?? {}).length) {
    lines.push('', `[teams.${tomlString(teamId)}.prompts]`)
    for (const role of Object.keys(summary.prompts ?? {}).sort()) lines.push(`${role} = ${tomlString(summary.prompts![role])}`)
  }
}

function candidateToml(candidate: GuidedFormProjection, rootId: string, stageIds: string[]): string {
  const lines: string[] = []
  appendTeamToml(lines, rootId, candidate.teams[rootId])
  for (const stageId of stageIds) appendTeamToml(lines, stageId, candidate.teams[stageId])
  return lines.join('\n')
}

function errorMessage(reason: unknown): string {
  if (reason instanceof TeamFamilyError) return reason.message
  if (reason instanceof Error) return reason.message
  return 'The family could not be added to the draft.'
}

function stageDefaultName(index: number): string {
  return ['Stronger worker', 'Strongest worker'][index] ?? `Stage ${index + 1}`
}

export function TeamFamilyWizard({ draft, resetVersion = 0, onAddFamily, onClose, previewPending = false, previewError = null, previewReady = true, onDirtyChange }: TeamFamilyWizardProps) {
  const [wizard, setWizard] = useState<WizardState>(initialState)
  const [pendingCopySource, setPendingCopySource] = useState<CopySource | undefined>(undefined)
  const [confirmRemoveIndex, setConfirmRemoveIndex] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const errorRef = useRef<HTMLParagraphElement>(null)
  const requestRef = useRef(0)
  const nextStageUiId = useRef(0)
  const previousResetVersion = useRef(resetVersion)

  const teamIds = useMemo(() => Object.keys(draft.teams).sort(), [draft.teams])
  const selectors = useMemo(() => Object.entries(draft.harnesses)
    .flatMap(([harness, profiles]) => Object.keys(profiles).map(profile => `${harness}.${profile}`))
    .sort(), [draft.harnesses])
  const roleNames = useMemo(() => sortedUnique([
    ...Object.keys(draft.roles),
    ...Object.keys(wizard.baseRoles),
    ...(wizard.copySource ? Object.keys(draft.teams[wizard.copySource]?.effective_roles ?? {}) : []),
  ]), [draft.roles, draft.teams, wizard.baseRoles, wizard.copySource])
  const promptNames = useMemo(() => sortedUnique([
    ...Object.keys(draft.role_prompts ?? {}),
    ...Object.keys(wizard.basePrompts),
    ...(wizard.copySource ? Object.keys(draft.teams[wizard.copySource]?.effective_prompts ?? {}) : []),
  ]), [draft.role_prompts, draft.teams, wizard.basePrompts, wizard.copySource])
  const preferredRoles = ['worker', 'reviewer'].filter(role => roleNames.includes(role))
  const otherRoles = roleNames.filter(role => !preferredRoles.includes(role))
  const sourceTeam = wizard.copySource ? draft.teams[wizard.copySource] : undefined

  const wizardDirty = Boolean(
    wizard.familyName || wizard.baseId || wizard.baseIdEdited || wizard.copySource || wizard.copyEdited
      || Object.keys(wizard.baseRoles).length || Object.keys(wizard.basePrompts).length || wizard.stages.length || wizard.step !== 1,
  )

  useEffect(() => {
    onDirtyChange(wizardDirty)
  }, [onDirtyChange, wizardDirty])

  useEffect(() => {
    if (previousResetVersion.current === resetVersion) return
    previousResetVersion.current = resetVersion
    requestRef.current += 1
    setWizard(initialState())
    setPendingCopySource(undefined)
    setConfirmRemoveIndex(null)
    setError(null)
    setBusy(false)
  }, [resetVersion])

  useEffect(() => {
    if (error) errorRef.current?.focus()
  }, [error])

  function clearError(): void {
    setError(null)
  }

  function fail(message: string): null {
    setError(message)
    return null
  }

  function roleLabel(role: string): string {
    return formatMachineChoice(role, roleNames)
  }

  function baseEffectiveRole(role: string): string | undefined {
    return wizard.baseRoles[role] ?? draft.roles[role]
  }

  function baseEffectivePrompt(role: string): string | undefined {
    return own(wizard.basePrompts, role) ? wizard.basePrompts[role] : draft.role_prompts?.[role]
  }

  function stageEffectiveRole(stage: WizardStage, role: string): string | undefined {
    return stage.roles[role] ?? baseEffectiveRole(role)
  }

  function updateFamilyName(value: string): void {
    clearError()
    setWizard(previous => ({
      ...previous,
      familyName: value,
      baseId: previous.baseIdEdited ? previous.baseId : suggestTeamId(value),
    }))
  }

  function updateBaseId(value: string): void {
    clearError()
    setWizard(previous => ({ ...previous, baseId: value, baseIdEdited: true }))
  }

  function applyCopySource(next: CopySource): void {
    if (next !== null && previewPending) {
      fail('Wait for the current settings preview to finish before copying a team.')
      return
    }
    if (next !== null && previewError) {
      fail(`The current settings preview is unavailable: ${previewError}`)
      return
    }
    if (next !== null && !previewReady) {
      fail('Wait for a current settings preview to finish before copying a team.')
      return
    }
    if (next === null) {
      setWizard(previous => ({
        ...previous,
        copySource: null,
        copyEdited: false,
        baseRoles: {},
        basePrompts: {},
      }))
      setPendingCopySource(undefined)
      clearError()
      return
    }
    const source = draft.teams[next]
    if (!source?.effective_roles || !source.effective_prompts) {
      fail(`Team "${next}" has no canonical effective roles and prompts to copy yet.`)
      return
    }
    setWizard(previous => ({
      ...previous,
      copySource: next,
      copyEdited: false,
      baseRoles: copyDifferences(source.effective_roles!, draft.roles),
      basePrompts: copyDifferences(source.effective_prompts!, draft.role_prompts),
    }))
    setPendingCopySource(undefined)
    clearError()
  }

  function requestCopySource(next: CopySource): void {
    if (next === wizard.copySource) return
    if (next !== null && previewPending) {
      fail('Wait for the current settings preview to finish before copying a team.')
      return
    }
    if (next !== null && previewError) {
      fail(`The current settings preview is unavailable: ${previewError}`)
      return
    }
    if (next !== null && !previewReady) {
      fail('Wait for a current settings preview to finish before copying a team.')
      return
    }
    if (wizard.copyEdited) {
      setPendingCopySource(next)
      clearError()
      return
    }
    applyCopySource(next)
  }

  function updateBaseRole(role: string, selector: string): void {
    clearError()
    setWizard(previous => ({
      ...previous,
      baseRoles: { ...previous.baseRoles, [role]: selector },
      copyEdited: true,
    }))
  }

  function restoreBaseRole(role: string): void {
    clearError()
    setWizard(previous => {
      const baseRoles = { ...previous.baseRoles }
      delete baseRoles[role]
      return { ...previous, baseRoles, copyEdited: previous.copySource !== null || previous.copyEdited }
    })
  }

  function updateBasePrompt(role: string, text: string): void {
    clearError()
    setWizard(previous => ({
      ...previous,
      basePrompts: { ...previous.basePrompts, [role]: text },
      copyEdited: true,
    }))
  }

  function restoreBasePrompt(role: string): void {
    clearError()
    setWizard(previous => {
      const basePrompts = { ...previous.basePrompts }
      delete basePrompts[role]
      return { ...previous, basePrompts, copyEdited: previous.copySource !== null || previous.copyEdited }
    })
  }

  function validateRoleMap(map: Record<string, string>): string | null {
    for (const [role, selector] of Object.entries(map)) {
      if (!(role in draft.roles)) return `Role "${role}" is not configured in the current draft.`
      if (!selectors.includes(selector)) return `Selector "${selector}" for ${roleLabel(role)} is not a configured profile.`
    }
    return null
  }

  function validateBase(): string | null {
    const familyName = wizard.familyName.trim()
    if (!familyName) return 'Enter a family display name.'
    if (familyName.length > TEAM_DISPLAY_NAME_MAX_LENGTH) return `Display names must be at most ${TEAM_DISPLAY_NAME_MAX_LENGTH} characters.`
    const baseId = wizard.baseId.trim()
    if (!baseId) {
      return teamIdSlug(familyName) ? 'Enter a stable Base ID.' : 'This name has no usable ID slug; enter an explicit stable Base ID.'
    }
    const idError = validateStableTeamId(baseId, teamIds)
    if (idError) return idError
    const roleError = validateRoleMap(wizard.baseRoles)
    if (roleError) return roleError
    for (const role of Object.keys(wizard.basePrompts)) {
      if (!(role in draft.roles)) return `Role "${role}" is not configured in the current draft.`
    }
    if (wizard.copySource && (!sourceTeam?.effective_roles || !sourceTeam.effective_prompts)) {
      return `Team "${wizard.copySource}" has no canonical effective roles and prompts to copy yet.`
    }
    return null
  }

  function validateStages(): string | null {
    const baseError = validateBase()
    if (baseError) return baseError
    const occupied = new Set([...teamIds, wizard.baseId.trim()])
    for (const [index, stage] of wizard.stages.entries()) {
      const label = stage.displayName.trim()
      if (!label) return `Enter a display name for stage ${index + 1}.`
      if (label.length > TEAM_DISPLAY_NAME_MAX_LENGTH) return `Stage display names must be at most ${TEAM_DISPLAY_NAME_MAX_LENGTH} characters.`
      const id = stage.id.trim()
      const idError = validateStableTeamId(id, occupied)
      if (idError) return `Stage ${index + 1}: ${idError}`
      occupied.add(id)
      const roleError = validateRoleMap(stage.roles)
      if (roleError) return `Stage ${index + 1}: ${roleError}`
    }
    return null
  }

  function buildCandidate(): GuidedFormProjection {
    const baseId = wizard.baseId.trim()
    const next = clone(draft)
    next.teams[baseId] = {
      roles: { ...wizard.baseRoles },
      prompts: { ...wizard.basePrompts },
      display_name: wizard.familyName.trim(),
      upgrade_to: wizard.stages[0]?.id.trim() ?? null,
    }
    for (const [index, stage] of wizard.stages.entries()) {
      next.teams[stage.id.trim()] = {
        roles: { ...stage.roles },
        prompts: {},
        display_name: stage.displayName.trim(),
        extends: baseId,
        upgrade_to: wizard.stages[index + 1]?.id.trim() ?? null,
      }
    }
    return next
  }

  function addStage(): void {
    const baseError = validateBase()
    if (baseError) {
      fail(baseError)
      return
    }
    const label = stageDefaultName(wizard.stages.length)
    const occupied = [...teamIds, wizard.baseId.trim(), ...wizard.stages.map(stage => stage.id.trim())]
    const suggestedId = `${wizard.baseId.trim()}_${teamIdSlug(label)}`
    nextStageUiId.current += 1
    const uiId = `stage-${nextStageUiId.current}`
    try {
      const id = generateStageId(wizard.baseId.trim(), label, occupied)
      setWizard(previous => ({
        ...previous,
        stages: [...previous.stages, { uiId, id, displayName: label, roles: {} }],
      }))
      clearError()
    } catch (reason) {
      // Keep the suggested value visible and editable. Validation remains in
      // force for review/add-to-draft, so collisions and overlong suggestions
      // have a direct recovery path without renaming an existing team.
      setWizard(previous => ({
        ...previous,
        stages: [...previous.stages, { uiId, id: suggestedId, displayName: label, roles: {} }],
      }))
      fail(errorMessage(reason))
    }
  }

  function updateStage(index: number, update: (stage: WizardStage) => WizardStage): void {
    clearError()
    setConfirmRemoveIndex(null)
    setWizard(previous => ({
      ...previous,
      stages: previous.stages.map((stage, stageIndex) => stageIndex === index ? update(stage) : stage),
    }))
  }

  function updateStageRole(index: number, role: string, selector: string): void {
    updateStage(index, stage => ({ ...stage, roles: { ...stage.roles, [role]: selector } }))
  }

  function restoreStageRole(index: number, role: string): void {
    updateStage(index, stage => {
      const roles = { ...stage.roles }
      delete roles[role]
      return { ...stage, roles }
    })
  }

  function moveStage(index: number, delta: -1 | 1): void {
    const target = index + delta
    if (target < 0 || target >= wizard.stages.length) return
    setWizard(previous => {
      const stages = [...previous.stages]
      const current = stages[index]
      stages[index] = stages[target]
      stages[target] = current
      return { ...previous, stages }
    })
    setConfirmRemoveIndex(null)
    clearError()
  }

  function removeStage(index: number): void {
    if (confirmRemoveIndex !== index) {
      setConfirmRemoveIndex(index)
      clearError()
      return
    }
    setWizard(previous => ({ ...previous, stages: previous.stages.filter((_, stageIndex) => stageIndex !== index) }))
    setConfirmRemoveIndex(null)
    clearError()
  }

  function goNext(): void {
    const message = wizard.step === 1 ? validateBase() : validateStages()
    if (message) {
      fail(message)
      return
    }
    setWizard(previous => ({ ...previous, step: Math.min(3, previous.step + 1) as WizardStep }))
    clearError()
  }

  function goBack(): void {
    if (wizard.step === 1) {
      onClose()
      return
    }
    setWizard(previous => ({ ...previous, step: Math.max(1, previous.step - 1) as WizardStep }))
    clearError()
  }

  async function addFamily(): Promise<void> {
    const message = validateStages()
    if (message) {
      fail(message)
      return
    }
    const candidate = buildCandidate()
    const rootId = wizard.baseId.trim()
    const request = ++requestRef.current
    setBusy(true)
    clearError()
    try {
      await onAddFamily(candidate, rootId)
      if (request !== requestRef.current) return
      setWizard(initialState())
      setPendingCopySource(undefined)
      setBusy(false)
      onClose()
    } catch (reason) {
      if (request !== requestRef.current) return
      setError(errorMessage(reason))
      setBusy(false)
    }
  }

  function renderBaseRole(role: string): ReactNode {
    const declared = wizard.baseRoles[role]
    const effective = baseEffectiveRole(role)
    return <div className="team-family-wizard-role" key={role}>
      <div className="team-family-wizard-role-heading">
        <strong>{roleLabel(role)}</strong>
        <span className="text-xs text-dim">{declared ? `Stored Base override${wizard.copySource ? ` · copied from ${teamLabel(wizard.copySource, draft, teamIds)}` : ''}` : effective ? (wizard.copySource ? `Inherited from global defaults · equal to copied source` : 'Inherited from global defaults') : 'No configured assignment'}</span>
      </div>
      <Combobox
        label={roleLabel(role)}
        visibleLabel="Configured profile"
        value={declared ?? ''}
        options={selectors}
        optionLabel={selector => selectorSummary(draft, selector)}
        resolvedDisplay={!declared && effective ? selectorSummary(draft, effective) : null}
        resolvedBadge={!declared && effective ? (wizard.copySource ? 'Copied' : 'Global') : undefined}
        onChange={selector => updateBaseRole(role, selector)}
        emptyOption="No configured profiles"
      />
      <div className="team-family-wizard-role-actions">
        <span className="mono text-sm">{selectorSummary(draft, effective)}</span>
        {declared && <button type="button" className="btn btn-secondary btn-sm" onClick={() => restoreBaseRole(role)}>Restore global</button>}
      </div>
    </div>
  }

  function renderBasePrompt(role: string): ReactNode {
    const declared = own(wizard.basePrompts, role)
    const effective = baseEffectivePrompt(role) ?? ''
    return <div className="team-family-wizard-prompt" key={role}>
      <div className="team-family-wizard-role-heading">
        <strong>{roleLabel(role)}</strong>
        <span className="text-xs text-dim">{declared ? 'Stored Base prompt override' : 'Inherited from global defaults'}</span>
      </div>
      <TextEditor
        rows={4}
        aria-label={`Prompt text for ${roleLabel(role)}`}
        value={effective}
        onChange={event => updateBasePrompt(role, event.target.value)}
      />
      {declared && <button type="button" className="btn btn-secondary btn-sm" onClick={() => restoreBasePrompt(role)}>Restore global prompt</button>}
    </div>
  }

  function renderStageRole(stage: WizardStage, stageIndex: number, role: string): ReactNode {
    const declared = stage.roles[role]
    const effective = stageEffectiveRole(stage, role)
    const source = declared ? `Stored override · declared by ${stage.displayName || `Stage ${stageIndex + 1}`} (${stage.id})` : `Inherited from Base (${wizard.baseId.trim() || 'Base ID'})`
    return <div className="team-family-wizard-role" key={`${stage.uiId}-${role}`}>
      <div className="team-family-wizard-role-heading">
        <strong>{roleLabel(role)}</strong>
        <span className="text-xs text-dim">{source}</span>
      </div>
      <Combobox
        label={`${roleLabel(role)} for stage ${stage.id}`}
        visibleLabel="Configured profile"
        value={declared ?? ''}
        options={selectors}
        optionLabel={selector => selectorSummary(draft, selector)}
        resolvedDisplay={!declared && effective ? selectorSummary(draft, effective) : null}
        resolvedBadge={!declared && effective ? 'Inherited' : undefined}
        onChange={selector => updateStageRole(stageIndex, role, selector)}
        emptyOption="No configured profiles"
      />
      <div className="team-family-wizard-role-actions">
        <span className="mono text-sm">{selectorSummary(draft, effective)}</span>
        {declared && <button type="button" className="btn btn-secondary btn-sm" onClick={() => restoreStageRole(stageIndex, role)}>Restore inheritance</button>}
      </div>
    </div>
  }

  function renderStage(stage: WizardStage, index: number): ReactNode {
    const effectiveWorker = stageEffectiveRole(stage, 'worker')
    const precedingWorker = index === 0
      ? baseEffectiveRole('worker')
      : stageEffectiveRole(wizard.stages[index - 1], 'worker')
    const workerUnchanged = Boolean(effectiveWorker && precedingWorker && effectiveWorker === precedingWorker)
    return <fieldset className="team-family-wizard-stage" key={stage.uiId}>
      <legend><span>{stage.displayName || `Stage ${index + 1}`}</span> <span className="mono text-xs">{stage.id}</span></legend>
      <div className="team-family-wizard-stage-fields">
        <label>Stage display name<input className="input" aria-label={`Stage ${index + 1} display name`} value={stage.displayName} onChange={event => updateStage(index, current => ({ ...current, displayName: event.target.value }))} /></label>
        <details>
          <summary>Technical details</summary>
          <label>Stable stage ID<input className="input mono" aria-label={`Stable ID for stage ${index + 1}`} value={stage.id} onChange={event => updateStage(index, current => ({ ...current, id: event.target.value }))} /></label>
          <p className="text-xs text-dim">This ID is generated once and remains stable when the stage label or order changes.</p>
        </details>
      </div>
      <div className="team-family-wizard-stage-summary">
        <strong>Inherits from Base</strong>
        <span className="text-sm">{roleNames.length ? `${roleNames.length} configured role${roleNames.length === 1 ? '' : 's'} available; new stages start with no overrides.` : 'No configured roles are available.'}</span>
        {workerUnchanged && <span className="notice" role="status">This stage keeps the same implementation worker as its preceding stage; worker-quality escalation is ineligible.</span>}
      </div>
      {roleNames.length > 0 && <div className="team-family-wizard-role-list"><h5>Role overrides</h5>{roleNames.map(role => renderStageRole(stage, index, role))}</div>}
      <div className="team-family-wizard-actions">
        <button type="button" className="btn btn-secondary btn-sm" disabled={index === 0} onClick={() => moveStage(index, -1)}>Move earlier</button>
        <button type="button" className="btn btn-secondary btn-sm" disabled={index === wizard.stages.length - 1} onClick={() => moveStage(index, 1)}>Move later</button>
        <button type="button" className="btn btn-danger btn-sm" onClick={() => removeStage(index)}>Remove stage</button>
      </div>
      {confirmRemoveIndex === index && <div className="confirmation" role="alertdialog" aria-label={`Confirm removal of ${stage.displayName || `Stage ${index + 1}`}`}>
        <p>Remove <strong>{stage.displayName || `Stage ${index + 1}`}</strong> from this unsaved family? Its route link will be rewired on review.</p>
        <div className="team-family-wizard-actions"><button type="button" className="btn btn-danger btn-sm" onClick={() => removeStage(index)}>Confirm remove stage</button><button type="button" className="btn btn-secondary btn-sm" onClick={() => setConfirmRemoveIndex(null)}>Cancel</button></div>
      </div>}
    </fieldset>
  }

  const candidate = wizard.step === 3 && !validateStages() ? buildCandidate() : null
  const stepNames = ['Base', 'Stages', 'Review']

  return <section className="team-family-wizard" aria-label="Create team family">
    <div className="team-family-wizard-intro">
      <h3>Create team family</h3>
      <p className="text-sm text-dim">Build the family in the unsaved settings draft. Add family to draft previews the complete configuration; Save all remains the only persistence action.</p>
    </div>
    <ol className="team-family-wizard-steps" aria-label="Family creation steps">
      {stepNames.map((name, index) => <li className={wizard.step === index + 1 ? 'current' : wizard.step > index + 1 ? 'complete' : ''} key={name}><span>{index + 1}</span> {name}</li>)}
    </ol>
    {error && <p className="error-message" role="alert" tabIndex={-1} ref={errorRef}>{error}</p>}

    {wizard.step === 1 && <>
      <section className="team-family-wizard-section" aria-labelledby="family-wizard-base-heading">
        <h4 id="family-wizard-base-heading">Base</h4>
        <label>Family display name<input className="input" aria-label="Family display name" value={wizard.familyName} placeholder="Product development" onChange={event => updateFamilyName(event.target.value)} /></label>
        <details>
          <summary>Technical details</summary>
          <label>Stable Base ID<input className="input mono" aria-label="Stable Base ID" value={wizard.baseId} placeholder="product_development" onChange={event => updateBaseId(event.target.value)} /></label>
          <p className="text-xs text-dim">Suggested from the display name as a lower-case ASCII slug. IDs are stable technical identities and are never silently suffixed.</p>
        </details>
      </section>
      <section className="team-family-wizard-section" aria-labelledby="family-wizard-source-heading">
        <h4 id="family-wizard-source-heading">Base assignments</h4>
        <label>Start with<select className="input" aria-label="Base assignment source" value={wizard.copySource ?? ''} onChange={event => requestCopySource(event.target.value || null)}><option value="">Use global defaults</option>{teamIds.map(teamId => <option value={teamId} key={teamId}>{teamLabel(teamId, draft, teamIds)}</option>)}</select></label>
        {wizard.copySource && sourceTeam && <p className="notice" role="status">Copied from <strong>{teamLabel(wizard.copySource, draft, teamIds)}</strong> (<span className="mono">{wizard.copySource}</span>) as a snapshot. Only values different from current global defaults are stored in the new Base; there is no extends link to the source.</p>}
        {!wizard.copySource && <p className="text-sm text-dim">The new Base inherits current global role and prompt defaults until you add a local override.</p>}
        {pendingCopySource !== undefined && <div className="confirmation" role="alertdialog" aria-label="Replace copied assignments">
          <p>Replace the Base assignments and prompts with the newly selected source? Existing edits to this Base will be lost.</p>
          <div className="team-family-wizard-actions"><button type="button" className="btn btn-danger btn-sm" onClick={() => applyCopySource(pendingCopySource)}>Replace copied values</button><button type="button" className="btn btn-secondary btn-sm" onClick={() => setPendingCopySource(undefined)}>Keep current copy</button></div>
        </div>}
      </section>
      <section className="team-family-wizard-section" aria-labelledby="family-wizard-roles-heading">
        <h4 id="family-wizard-roles-heading">Roles</h4>
        {preferredRoles.length > 0 && <div className="team-family-wizard-role-list">{preferredRoles.map(renderBaseRole)}</div>}
        {otherRoles.length > 0 && <details className="team-family-wizard-disclosure"><summary>Other roles ({otherRoles.length}) — Show all roles</summary><div className="team-family-wizard-role-list">{otherRoles.map(renderBaseRole)}</div></details>}
        {!roleNames.length && <p className="text-sm text-dim">No configured roles are available in this draft.</p>}
      </section>
      {promptNames.length > 0 && <details className="team-family-wizard-section team-family-wizard-prompts"><summary>Role prompts ({promptNames.length})</summary><p className="text-xs text-dim">Copied prompts remain sparse declarations on the new Base. Empty text is preserved exactly.</p><div className="team-family-wizard-prompt-list">{promptNames.map(renderBasePrompt)}</div></details>}
    </>}

    {wizard.step === 2 && <section className="team-family-wizard-section" aria-labelledby="family-wizard-stages-heading">
      <h4 id="family-wizard-stages-heading">Stages</h4>
      <div className="team-family-wizard-base-summary card"><strong>Base · {wizard.familyName.trim() || 'Unnamed family'}</strong><span className="mono text-sm">{wizard.baseId.trim() || 'Base ID pending'}</span><span className="text-sm">{roleNames.length} role{roleNames.length === 1 ? '' : 's'} configured; stages inherit Base until an override is selected.</span></div>
      {wizard.stages.length ? wizard.stages.map(renderStage) : <p className="notice" role="status">No upgrade stages yet. A zero-stage family is a valid standalone Base.</p>}
      <button type="button" className="btn btn-secondary" onClick={addStage}>Add upgrade stage</button>
    </section>}

    {wizard.step === 3 && candidate && <section className="team-family-wizard-section" aria-labelledby="family-wizard-review-heading">
      <h4 id="family-wizard-review-heading">Review family</h4>
      <p className="text-sm text-dim">Review declarations before adding this family to the shared draft. Effective values are shown for comparison; only sparse overrides are stored.</p>
      <div className="team-family-wizard-review card">
        <div><h5>Base · {wizard.familyName.trim()}</h5><p className="mono text-sm">{wizard.baseId.trim()}</p><p>{Object.keys(wizard.baseRoles).length} stored role override{Object.keys(wizard.baseRoles).length === 1 ? '' : 's'}; {Object.keys(wizard.basePrompts).length} stored prompt override{Object.keys(wizard.basePrompts).length === 1 ? '' : 's'}.</p></div>
        {wizard.stages.map(stage => {
          const changed = roleNames.filter(role => stageEffectiveRole(stage, role) !== baseEffectiveRole(role)).length
          return <div className="team-family-wizard-review-stage" key={stage.uiId}><h5>{stage.displayName} <span className="mono text-sm">{stage.id}</span></h5><p>extends <span className="mono">{wizard.baseId.trim()}</span> · {changed} effective role difference{changed === 1 ? '' : 's'} · {Object.keys(stage.roles).length} stored override{Object.keys(stage.roles).length === 1 ? '' : 's'}</p></div>
        })}
      </div>
      <p className="team-family-wizard-route"><strong>Upgrade route:</strong> {[wizard.baseId.trim(), ...wizard.stages.map(stage => stage.id.trim())].map((id, index, route) => <span key={id}><span className="mono">{id}</span>{index < route.length - 1 && ' → '}</span>)}</p>
      <details className="team-family-wizard-toml"><summary>Proposed TOML (read-only)</summary><pre className="mono">{candidateToml(candidate, wizard.baseId.trim(), wizard.stages.map(stage => stage.id.trim()))}</pre></details>
      <button type="button" className="btn btn-primary" disabled={busy} onClick={() => void addFamily()}>{busy ? 'Previewing family…' : 'Add family to draft'}</button>
    </section>}

    <div className="team-family-wizard-actions team-family-wizard-footer">
      <button type="button" className="btn btn-secondary" disabled={busy} onClick={goBack}>{wizard.step === 1 ? 'Back to family editor' : 'Back'}</button>
      {wizard.step < 3 && <button type="button" className="btn btn-primary" disabled={busy} onClick={goNext}>{wizard.step === 1 ? 'Next: stages' : 'Next: review'}</button>}
    </div>
  </section>
}
