import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { convertLegacyChain } from '../teamFamilies'
import type { GuidedFormProjection, GuidedTeamSummary } from '../types'
import type { LegacyConversionPreviewResult } from '../settingsDraft'
import { TeamFamiliesSettings } from './TeamFamiliesSettings'

const profile = (model: string, effort = 'high') => ({ model, effort })

function summary(overrides: Partial<GuidedTeamSummary> = {}): GuidedTeamSummary {
  return { roles: {}, prompts: {}, ...overrides }
}

function projection(teams: Record<string, GuidedTeamSummary>): GuidedFormProjection {
  return {
    default_workflow: 'demo',
    max_turns: 5,
    harnesses: {
      codex: {
        worker: profile('worker-model'),
        reviewer: profile('reviewer-model', 'low'),
        deep: profile('deep-model'),
        architect: profile('architect-model'),
      },
    },
    roles: {
      worker: 'codex.worker',
      reviewer: 'codex.reviewer',
      architect: 'codex.architect',
    },
    teams,
    workflow_default_teams: { demo: null },
    workflows: {},
    prompts: {},
    role_prompts: {
      worker: 'Global worker prompt',
      reviewer: 'Global reviewer prompt',
      architect: 'Global architect prompt',
    },
  }
}

function familyDraft(): GuidedFormProjection {
  return projection({
    base: summary({
      display_name: 'Product',
      roles: { worker: 'codex.worker', reviewer: 'codex.reviewer', architect: 'codex.architect' },
      prompts: { reviewer: 'Base reviewer prompt' },
      upgrade_to: 'fast',
      effective_roles: { worker: 'codex.worker', reviewer: 'codex.reviewer', architect: 'codex.architect' },
      effective_prompts: { worker: 'Global worker prompt', reviewer: 'Base reviewer prompt', architect: 'Global architect prompt' },
      role_sources: { worker: 'base', reviewer: 'base', architect: 'base' },
      prompt_sources: { worker: 'global', reviewer: 'base', architect: 'global' },
    }),
    fast: summary({
      display_name: 'Fast variant',
      extends: 'base',
      roles: { worker: 'codex.deep' },
      upgrade_to: null,
      effective_roles: { worker: 'codex.deep', reviewer: 'codex.reviewer', architect: 'codex.architect' },
      effective_prompts: { worker: 'Global worker prompt', reviewer: 'Base reviewer prompt', architect: 'Global architect prompt' },
      role_sources: { worker: 'fast', reviewer: 'base', architect: 'base' },
      prompt_sources: { worker: 'global', reviewer: 'base', architect: 'global' },
    }),
    legacy_base: summary({
      display_name: 'Legacy base',
      roles: { worker: 'codex.worker' },
      upgrade_to: 'legacy_review',
      effective_roles: { worker: 'codex.worker', reviewer: 'codex.reviewer', architect: 'codex.architect' },
      effective_prompts: { worker: 'Global worker prompt', reviewer: 'Legacy reviewer prompt', architect: 'Global architect prompt' },
    }),
    legacy_review: summary({
      display_name: 'Legacy review',
      roles: { worker: 'codex.deep' },
      prompts: { reviewer: 'Legacy reviewer prompt' },
      effective_roles: { worker: 'codex.deep', reviewer: 'codex.reviewer', architect: 'codex.architect' },
      effective_prompts: { worker: 'Global worker prompt', reviewer: 'Legacy reviewer prompt', architect: 'Global architect prompt' },
      upgrade_to: null,
    }),
    orphan: summary({ extends: 'missing-base' }),
  })
}

function legacyDraft(): GuidedFormProjection {
  return projection({
    legacy_base: summary({
      display_name: 'Legacy base',
      roles: { worker: 'codex.worker' },
      prompts: { reviewer: 'Legacy reviewer prompt' },
      upgrade_to: 'legacy_review',
      backup_team: 'recovery',
      effective_roles: { worker: 'codex.worker', reviewer: 'codex.reviewer', architect: 'codex.architect' },
      effective_prompts: { worker: 'Global worker prompt', reviewer: 'Legacy reviewer prompt', architect: 'Global architect prompt' },
    }),
    legacy_review: summary({
      display_name: 'Legacy review',
      roles: { worker: 'codex.deep' },
      prompts: { reviewer: 'Legacy reviewer prompt' },
      upgrade_to: null,
      backup_team: 'recovery',
      effective_roles: { worker: 'codex.deep', reviewer: 'codex.reviewer', architect: 'codex.architect' },
      effective_prompts: { worker: 'Global worker prompt', reviewer: 'Legacy reviewer prompt', architect: 'Global architect prompt' },
    }),
    recovery: summary(),
  })
}

function routeDraft(withDefault = false): GuidedFormProjection {
  const draft = projection({
    base: summary({
      roles: { worker: 'codex.worker' },
      upgrade_to: 'first',
      effective_roles: { worker: 'codex.worker', reviewer: 'codex.reviewer', architect: 'codex.architect' },
    }),
    first: summary({
      extends: 'base',
      upgrade_to: 'second',
      effective_roles: { worker: 'codex.worker', reviewer: 'codex.reviewer', architect: 'codex.architect' },
    }),
    second: summary({
      extends: 'base',
      upgrade_to: null,
      effective_roles: { worker: 'codex.deep', reviewer: 'codex.reviewer', architect: 'codex.architect' },
    }),
  })
  if (withDefault) draft.workflow_default_teams.demo = 'second'
  return draft
}

function complexDraft(): GuidedFormProjection {
  return projection({
    complex_base: summary({
      display_name: 'Complex base',
      roles: { worker: 'codex.worker' },
      upgrade_to: 'complex_first',
      effective_roles: { worker: 'codex.worker', reviewer: 'codex.reviewer', architect: 'codex.architect' },
    }),
    complex_first: summary({
      extends: 'complex_base',
      upgrade_to: 'external',
      effective_roles: { worker: 'codex.deep', reviewer: 'codex.reviewer', architect: 'codex.architect' },
    }),
    external: summary({
      roles: { worker: 'codex.deep' },
      upgrade_to: null,
      effective_roles: { worker: 'codex.deep', reviewer: 'codex.reviewer', architect: 'codex.architect' },
    }),
  })
}

const rejectedPreview = async (): Promise<LegacyConversionPreviewResult> => {
  throw new Error('preview callback not configured')
}

function EditorHarness({
  initial,
  selectedTeam,
  onChange,
  onOpenPrompt,
  onPreviewConversion = rejectedPreview,
  onAddFamily = async () => {},
  onWizardDirtyChange,
}: {
  initial: GuidedFormProjection
  selectedTeam: string
  onChange: (draft: GuidedFormProjection) => void
  onOpenPrompt: (teamId: string, role: string) => void
  onPreviewConversion?: (rootId: string, draft: GuidedFormProjection) => Promise<LegacyConversionPreviewResult>
  onAddFamily?: (candidate: GuidedFormProjection, rootId: string) => Promise<void>
  onWizardDirtyChange?: (dirty: boolean) => void
}) {
  const [draft, setDraft] = useState(initial)
  const [selected, setSelected] = useState(selectedTeam)
  return <TeamFamiliesSettings
    draft={draft}
    baseline={initial}
    selectedTeam={selected}
    navigationVersion={0}
    onSelectTeam={setSelected}
    onNavigate={() => {}}
    onChange={next => { setDraft(next); onChange(next) }}
    newTeamName=""
    newTeamError={null}
    onNewTeamNameChange={() => {}}
    onAddTeam={() => {}}
    onOpenPrompt={onOpenPrompt}
    onPreviewConversion={onPreviewConversion}
    onAddFamily={onAddFamily}
    onWizardDirtyChange={onWizardDirtyChange}
  />
}

function renderEditor(initial: GuidedFormProjection, options: {
  selectedTeam?: string
  onChange?: (draft: GuidedFormProjection) => void
  onOpenPrompt?: (teamId: string, role: string) => void
  onPreviewConversion?: (rootId: string, draft: GuidedFormProjection) => Promise<LegacyConversionPreviewResult>
  onAddFamily?: (candidate: GuidedFormProjection, rootId: string) => Promise<void>
  onWizardDirtyChange?: (dirty: boolean) => void
} = {}) {
  const onChange = options.onChange ?? vi.fn()
  const onOpenPrompt = options.onOpenPrompt ?? vi.fn()
  const view = render(<EditorHarness
    initial={initial}
    selectedTeam={options.selectedTeam ?? Object.keys(initial.teams).sort()[0]}
    onChange={onChange}
    onOpenPrompt={onOpenPrompt}
    onPreviewConversion={options.onPreviewConversion}
    onAddFamily={options.onAddFamily}
    onWizardDirtyChange={options.onWizardDirtyChange}
  />)
  return { ...view, onChange, onOpenPrompt }
}

describe('TeamFamiliesSettings', () => {
  it('opens the new-family wizard lazily and retains its draft across Back', () => {
    const onWizardDirtyChange = vi.fn()
    renderEditor(familyDraft(), { selectedTeam: 'base', onWizardDirtyChange })

    fireEvent.click(screen.getByRole('button', { name: 'New family' }))
    fireEvent.change(screen.getByLabelText('Family display name'), { target: { value: 'Future product' } })
    fireEvent.click(screen.getByRole('button', { name: 'Next: stages' }))
    expect(screen.getByRole('heading', { name: 'Stages' })).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Back' }))
    fireEvent.click(screen.getByRole('button', { name: 'Back to family editor' }))
    expect(screen.getByRole('heading', { name: 'Product' })).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'New family' }))
    expect(screen.getByRole('heading', { name: 'Base' })).toBeTruthy()
    expect((screen.getByLabelText('Family display name') as HTMLInputElement).value).toBe('Future product')
    expect(onWizardDirtyChange).toHaveBeenLastCalledWith(true)
  })

  it('groups families and legacy chains while keeping canonical differences and inherited origins visible', () => {
    const draft = familyDraft()
    renderEditor(draft, { selectedTeam: 'base' })

    const navigation = screen.getByRole('navigation', { name: 'Team families' })
    expect(within(navigation).getByRole('button', { name: 'Product', exact: true })).toBeTruthy()
    expect(within(navigation).getByRole('button', { name: 'Legacy base', exact: true })).toBeTruthy()
    expect(within(navigation).getByRole('button', { name: 'Orphan', exact: true })).toBeTruthy()
    expect(within(navigation).getAllByText('1 changed role')).toHaveLength(2)

    fireEvent.click(screen.getByRole('button', { name: 'Fast variant', exact: true }))
    expect(screen.getByText('Stored overrides')).toBeTruthy()
    expect(screen.getByText(/Inherited roles \(2\)/)).toBeTruthy()
    const inheritedRoles = screen.getByText(/Inherited roles \(2\)/).closest('details') as HTMLElement
    expect(within(inheritedRoles).getAllByText(/Inherited from declared by Base \(base\)/)).toHaveLength(2)
    expect(screen.getByText('Base reviewer prompt')).toBeTruthy()
  })

  it('edits declared role and display metadata, hands prompts to Prompts, and shows declared TOML only', () => {
    const onChange = vi.fn()
    const onOpenPrompt = vi.fn()
    renderEditor(familyDraft(), { selectedTeam: 'base', onChange, onOpenPrompt })

    const worker = screen.getByLabelText('Worker')
    fireEvent.focus(worker)
    fireEvent.change(worker, { target: { value: 'codex.deep' } })
    fireEvent.keyDown(worker, { key: 'Enter' })
    expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({ teams: expect.objectContaining({ base: expect.objectContaining({ roles: expect.objectContaining({ worker: 'codex.deep' }) }) }) }))

    fireEvent.change(screen.getByLabelText('Display name'), { target: { value: 'Product renamed' } })
    fireEvent.blur(screen.getByLabelText('Display name'))
    expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({ teams: expect.objectContaining({ base: expect.objectContaining({ display_name: 'Product renamed' }) }) }))

    const promptRow = screen.getByText('Base reviewer prompt').closest('.team-family-prompt-row') as HTMLElement
    fireEvent.click(within(promptRow).getByRole('button', { name: 'Edit in Prompts' }))
    expect(onOpenPrompt).toHaveBeenCalledWith('base', 'reviewer')

    fireEvent.click(screen.getByText('Declared TOML for this team (read-only)'))
    const declared = screen.getByText(/\[teams\."base"\]/).closest('pre') as HTMLElement
    expect(declared.textContent).toContain('worker = "codex.deep"')
    expect(declared.textContent).not.toContain('effective_roles')
    expect(declared.textContent).not.toContain('role_sources')
  })

  it('restores an explicit child role to inheritance without changing canonical projections', () => {
    const onChange = vi.fn()
    renderEditor(familyDraft(), { selectedTeam: 'base', onChange })
    fireEvent.click(screen.getByRole('button', { name: 'Fast variant', exact: true }))
    const override = screen.getByText(/Stored override · declared by Fast variant \(fast\)/).closest('.team-family-role-row') as HTMLElement
    fireEvent.click(within(override).getByRole('button', { name: 'Restore inheritance' }))
    const next = onChange.mock.lastCall?.[0] as GuidedFormProjection
    expect(next.teams.fast.roles).toEqual({})
    expect(next.teams.fast.effective_roles).toEqual(familyDraft().teams.fast.effective_roles)
  })

  it('keeps spaces while family and stage labels are being typed, then normalizes on blur', () => {
    const onChange = vi.fn()
    renderEditor(familyDraft(), { selectedTeam: 'base', onChange })
    const familyName = screen.getByLabelText('Display name') as HTMLInputElement
    fireEvent.change(familyName, { target: { value: 'Product ' } })
    expect(familyName.value).toBe('Product ')
    expect(onChange).not.toHaveBeenCalled()
    fireEvent.change(familyName, { target: { value: 'Product development ' } })
    expect(familyName.value).toBe('Product development ')
    fireEvent.blur(familyName)
    expect(familyName.value).toBe('Product development')
    expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({
      teams: expect.objectContaining({ base: expect.objectContaining({ display_name: 'Product development' }) }),
    }))

    fireEvent.click(screen.getByRole('button', { name: 'Fast variant', exact: true }))
    const stageName = screen.getByLabelText('Display name') as HTMLInputElement
    fireEvent.change(stageName, { target: { value: 'Fast variant ' } })
    expect(stageName.value).toBe('Fast variant ')
    fireEvent.click(screen.getByRole('button', { name: 'Base base', exact: true }))
    expect((screen.getByLabelText('Display name') as HTMLInputElement).value).toBe('Product development')
    fireEvent.click(screen.getByRole('button', { name: 'Fast variant', exact: true }))
    expect((screen.getByLabelText('Display name') as HTMLInputElement).value).toBe('Fast variant ')
    fireEvent.blur(screen.getByLabelText('Display name'))
    expect((screen.getByLabelText('Display name') as HTMLInputElement).value).toBe('Fast variant')
  })

  it('keeps complex routes read-only for ordering and removal while retaining explicit route controls', () => {
    renderEditor(complexDraft(), { selectedTeam: 'complex_base' })
    expect(screen.getByText(/explicit complex route/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Move stage earlier' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Move stage later' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Remove stage…' })).toBeNull()
    expect(screen.getAllByText('complex_first')).toHaveLength(2)
    expect(screen.getByLabelText('Upgrade to for team Complex base')).toBeTruthy()
  })

  it('reorders simple stages and requires confirmation before removing one', () => {
    const onChange = vi.fn()
    renderEditor(routeDraft(), { selectedTeam: 'second', onChange })
    fireEvent.click(screen.getByRole('button', { name: 'Move stage earlier' }))
    let next = onChange.mock.lastCall?.[0] as GuidedFormProjection
    expect(next.teams.base.upgrade_to).toBe('second')
    expect(next.teams.second.upgrade_to).toBe('first')
    expect(next.teams.first.upgrade_to).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Remove stage…' }))
    expect(screen.getByRole('alertdialog', { name: 'Confirm removal of Second' })).toBeTruthy()
    expect(onChange).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByRole('button', { name: 'Confirm remove stage' }))
    next = onChange.mock.lastCall?.[0] as GuidedFormProjection
    expect(next.teams.second).toBeUndefined()
    expect(next.teams.base.upgrade_to).toBe('first')
  })

  it('reports removal dependencies without dropping the draft', () => {
    const onChange = vi.fn()
    renderEditor(routeDraft(true), { selectedTeam: 'second', onChange })
    fireEvent.click(screen.getByRole('button', { name: 'Remove stage…' }))
    fireEvent.click(screen.getByRole('button', { name: 'Confirm remove stage' }))
    expect(onChange).not.toHaveBeenCalled()
    expect(screen.getByRole('alert').textContent).toMatch(/default team/)
  })

  it('previews legacy conversion, shows propagation consequences, and adds only an accepted candidate', async () => {
    const draft = legacyDraft()
    const conversion = convertLegacyChain(draft, 'legacy_base')
    const preview: LegacyConversionPreviewResult = {
      valid: true,
      conversion,
      draft: conversion.draft,
      pair: { aflow_toml: 'converted-aflow', workflows_toml: 'converted-workflows' },
      response: null,
      validation: { valid: true, memberIds: conversion.memberIds, warning: 'Later Base edits will propagate to converted stages.', error: null },
      error: null,
    }
    const onChange = vi.fn()
    const onPreviewConversion = vi.fn(async () => preview)
    renderEditor(draft, { selectedTeam: 'legacy_base', onChange, onPreviewConversion })

    fireEvent.click(screen.getByRole('button', { name: 'Preview convert to family' }))
    await waitFor(() => expect(screen.getByRole('region', { name: 'Legacy conversion preview' })).toBeTruthy())
    expect(onPreviewConversion).toHaveBeenCalledWith('legacy_base', draft)
    expect(screen.getByText(/Later Base edits will propagate/)).toBeTruthy()
    expect(screen.getByText(/legacy_review\.roles/)).toBeTruthy()
    expect(onChange).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: 'Add conversion to draft' }))
    expect(onChange).toHaveBeenCalledTimes(1)
    expect(onChange.mock.lastCall?.[0].teams.legacy_review.extends).toBe('legacy_base')
  })

  it('keeps an invalid conversion preview reviewable without mutating unsaved values', async () => {
    const draft = legacyDraft()
    const conversion = convertLegacyChain(draft, 'legacy_base')
    const onChange = vi.fn()
    const onPreviewConversion = vi.fn(async (): Promise<LegacyConversionPreviewResult> => ({
      valid: false,
      conversion,
      draft: conversion.draft,
      pair: null,
      response: null,
      validation: { valid: false, memberIds: conversion.memberIds, warning: 'Preview failed; inspect the server validation.', error: null },
      error: new Error('Preview failed'),
    }))
    renderEditor(draft, { selectedTeam: 'legacy_base', onChange, onPreviewConversion })
    fireEvent.click(screen.getByRole('button', { name: 'Preview convert to family' }))
    await waitFor(() => expect(screen.getByText('The candidate remains preview-only; no draft values were changed.')).toBeTruthy())
    expect(onChange).not.toHaveBeenCalled()
  })
})
