import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { GuidedFormProjection } from '../types'
import { TeamFamilyWizard } from './TeamFamilyWizard'

function projection(): GuidedFormProjection {
  return {
    default_workflow: 'demo',
    max_turns: 5,
    harnesses: {
      codex: {
        worker: { model: 'worker-model', effort: 'high' },
        reviewer: { model: 'reviewer-model', effort: 'low' },
        deep: { model: 'deep-model', effort: 'high' },
      },
    },
    roles: {
      worker: 'codex.worker',
      reviewer: 'codex.reviewer',
      architect: 'codex.reviewer',
    },
    teams: {
      source: {
        display_name: 'Copy source',
        roles: { worker: 'codex.deep' },
        prompts: { reviewer: 'Source reviewer prompt' },
        effective_roles: { worker: 'codex.deep', reviewer: 'codex.reviewer', architect: 'codex.reviewer' },
        effective_prompts: { worker: 'Global worker prompt', reviewer: 'Source reviewer prompt' },
      },
      incomplete: {
        display_name: 'Incomplete source',
        roles: {},
        prompts: {},
      },
      missing_profile: {
        display_name: 'Missing profile source',
        roles: {},
        prompts: {},
        effective_roles: { worker: 'codex.missing', reviewer: 'codex.reviewer', architect: 'codex.reviewer' },
        effective_prompts: { worker: 'Global worker prompt', reviewer: 'Missing profile reviewer prompt' },
      },
      other: {
        display_name: 'Other source',
        roles: { worker: 'codex.worker' },
        prompts: { reviewer: 'Other reviewer prompt' },
        effective_roles: { worker: 'codex.worker', reviewer: 'codex.reviewer', architect: 'codex.reviewer' },
        effective_prompts: { worker: 'Global worker prompt', reviewer: 'Other reviewer prompt' },
      },
    },
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

function renderWizard(options: {
  draft?: GuidedFormProjection
  onAddFamily?: (candidate: GuidedFormProjection, rootId: string) => Promise<void>
  onClose?: () => void
  onDirtyChange?: (dirty: boolean) => void
  previewPending?: boolean
  previewError?: string | null
  resetVersion?: number
} = {}) {
  const onAddFamily = options.onAddFamily ?? vi.fn(async () => {})
  const onClose = options.onClose ?? vi.fn()
  const onDirtyChange = options.onDirtyChange ?? vi.fn()
  const view = render(<TeamFamilyWizard
    draft={options.draft ?? projection()}
    resetVersion={options.resetVersion}
    onAddFamily={onAddFamily}
    onClose={onClose}
    previewPending={options.previewPending}
    previewError={options.previewError}
    onDirtyChange={onDirtyChange}
  />)
  return { ...view, onAddFamily, onClose, onDirtyChange }
}

function enterFamilyName(name = 'Product development'): void {
  fireEvent.change(screen.getByLabelText('Family display name'), { target: { value: name } })
}

function chooseSource(source: string): void {
  fireEvent.change(screen.getByLabelText('Base assignment source'), { target: { value: source } })
}

function stageById(id: string): HTMLElement {
  const stage = [...document.querySelectorAll('fieldset.team-family-wizard-stage')].find(fieldset => fieldset.textContent?.includes(id))
  if (!stage) throw new Error(`Stage ${id} was not rendered`)
  return stage as HTMLElement
}

describe('TeamFamilyWizard', () => {
  it('suggests a stable ID and copies effective values sparsely without an extends link', async () => {
    const { onAddFamily } = renderWizard()
    enterFamilyName()
    expect((screen.getByLabelText('Stable Base ID') as HTMLInputElement).value).toBe('product_development')

    chooseSource('source')
    expect(screen.getByRole('status').textContent).toMatch(/Copied from\s+Copy source/)
    expect((screen.getByLabelText('Worker') as HTMLInputElement).value).toContain('codex.deep')
    expect((screen.getByLabelText('Prompt text for Reviewer') as HTMLTextAreaElement).value).toBe('Source reviewer prompt')
    expect(screen.getByText(/there is no extends link to the source/)).toBeTruthy()
    expect(screen.getByText(/Other roles \(1\)/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Next: stages' }))
    fireEvent.click(screen.getByRole('button', { name: 'Next: review' }))
    fireEvent.click(screen.getByRole('button', { name: 'Add family to draft' }))
    await waitFor(() => expect(onAddFamily).toHaveBeenCalledTimes(1))
    const [candidate] = onAddFamily.mock.calls[0]
    expect(candidate.teams.product_development).toMatchObject({
      roles: { worker: 'codex.deep' },
      prompts: { reviewer: 'Source reviewer prompt' },
    })
    expect(candidate.teams.product_development.extends).toBeUndefined()
  })

  it('requires an explicit valid ID for empty slugs and keeps edited copies until replacement is confirmed', () => {
    renderWizard()
    enterFamilyName('!!!')
    fireEvent.click(screen.getByRole('button', { name: 'Next: stages' }))
    expect(screen.getByRole('alert').textContent).toMatch(/no usable ID slug/i)

    fireEvent.change(screen.getByLabelText('Stable Base ID'), { target: { value: 'bad id' } })
    fireEvent.click(screen.getByRole('button', { name: 'Next: stages' }))
    expect(screen.getByRole('alert').textContent).toMatch(/1–64 letters/i)

    fireEvent.change(screen.getByLabelText('Stable Base ID'), { target: { value: 'source' } })
    fireEvent.click(screen.getByRole('button', { name: 'Next: stages' }))
    expect(screen.getByRole('alert').textContent).toMatch(/already in use/i)

    enterFamilyName()
    chooseSource('source')
    const worker = screen.getByLabelText('Worker')
    fireEvent.focus(worker)
    fireEvent.change(worker, { target: { value: 'codex.reviewer' } })
    fireEvent.keyDown(worker, { key: 'Enter' })
    chooseSource('other')
    const replacement = screen.getByRole('alertdialog', { name: 'Replace copied assignments' })
    expect(replacement).toBeTruthy()
    fireEvent.click(within(replacement).getByRole('button', { name: 'Keep current copy' }))
    expect((screen.getByLabelText('Base assignment source') as HTMLSelectElement).value).toBe('source')
    chooseSource('other')
    fireEvent.click(screen.getByRole('button', { name: 'Replace copied values' }))
    expect((screen.getByLabelText('Base assignment source') as HTMLSelectElement).value).toBe('other')
    expect((screen.getByLabelText('Worker') as HTMLInputElement).value).toContain('codex.worker')
  })

  it('protects a global-based role edit before copy replacement', () => {
    renderWizard()
    enterFamilyName()
    const worker = screen.getByLabelText('Worker')
    fireEvent.focus(worker)
    fireEvent.change(worker, { target: { value: 'codex.reviewer' } })
    fireEvent.keyDown(worker, { key: 'Enter' })

    chooseSource('source')
    const replacement = screen.getByRole('alertdialog', { name: 'Replace copied assignments' })
    expect(replacement.textContent).toMatch(/Existing edits to this Base will be lost/)
    fireEvent.click(within(replacement).getByRole('button', { name: 'Keep current copy' }))
    expect((screen.getByLabelText('Base assignment source') as HTMLSelectElement).selectedIndex).toBe(0)
    expect((screen.getByLabelText('Worker') as HTMLInputElement).value).toContain('codex.reviewer')

    chooseSource('source')
    fireEvent.click(screen.getByRole('button', { name: 'Replace copied values' }))
    expect((screen.getByLabelText('Base assignment source') as HTMLSelectElement).value).toBe('source')
    expect((screen.getByLabelText('Worker') as HTMLInputElement).value).toContain('codex.deep')
  })

  it('protects a global-based prompt edit before copy replacement', () => {
    renderWizard()
    enterFamilyName()
    fireEvent.click(screen.getByText('Role prompts (3)', { exact: true }))
    const prompt = screen.getByLabelText('Prompt text for Reviewer') as HTMLTextAreaElement
    fireEvent.change(prompt, { target: { value: 'Prompt-only edit' } })

    chooseSource('source')
    expect(screen.getByRole('alertdialog', { name: 'Replace copied assignments' })).toBeTruthy()
    expect((screen.getByLabelText('Base assignment source') as HTMLSelectElement).selectedIndex).toBe(0)
    expect(prompt.value).toBe('Prompt-only edit')
  })

  it('warns when a later stage keeps its preceding worker', () => {
    renderWizard()
    enterFamilyName()
    fireEvent.click(screen.getByRole('button', { name: 'Next: stages' }))
    fireEvent.click(screen.getByRole('button', { name: 'Add upgrade stage' }))
    fireEvent.click(screen.getByRole('button', { name: 'Add upgrade stage' }))
    const firstId = (screen.getByLabelText('Stable ID for stage 1') as HTMLInputElement).value
    const secondId = (screen.getByLabelText('Stable ID for stage 2') as HTMLInputElement).value
    for (const id of [firstId, secondId]) {
      const worker = screen.getByLabelText(`Worker for stage ${id}`)
      fireEvent.focus(worker)
      fireEvent.change(worker, { target: { value: 'codex.deep' } })
      fireEvent.keyDown(worker, { key: 'Enter' })
    }
    expect(within(stageById(secondId)).getByText(/worker-quality escalation is ineligible/)).toBeTruthy()
  })

  it('does not warn when a later stage returns to the Base worker', () => {
    renderWizard()
    enterFamilyName()
    fireEvent.click(screen.getByRole('button', { name: 'Next: stages' }))
    fireEvent.click(screen.getByRole('button', { name: 'Add upgrade stage' }))
    const firstId = (screen.getByLabelText('Stable ID for stage 1') as HTMLInputElement).value
    const firstWorker = screen.getByLabelText(`Worker for stage ${firstId}`)
    fireEvent.focus(firstWorker)
    fireEvent.change(firstWorker, { target: { value: 'codex.deep' } })
    fireEvent.keyDown(firstWorker, { key: 'Enter' })
    fireEvent.click(screen.getByRole('button', { name: 'Add upgrade stage' }))
    const secondId = (screen.getByLabelText('Stable ID for stage 2') as HTMLInputElement).value
    expect(within(stageById(secondId)).queryByText(/worker-quality escalation is ineligible/)).toBeNull()
  })

  it('warns when the first stage keeps the Base worker', () => {
    renderWizard()
    enterFamilyName()
    fireEvent.click(screen.getByRole('button', { name: 'Next: stages' }))
    fireEvent.click(screen.getByRole('button', { name: 'Add upgrade stage' }))
    const stageId = (screen.getByLabelText('Stable ID for stage 1') as HTMLInputElement).value
    expect(within(stageById(stageId)).getByText(/worker-quality escalation is ineligible/)).toBeTruthy()
  })

  it('follows the preceding stage after reordering and removal without changing IDs or Base inheritance', () => {
    renderWizard()
    enterFamilyName()
    fireEvent.click(screen.getByRole('button', { name: 'Next: stages' }))
    fireEvent.click(screen.getByRole('button', { name: 'Add upgrade stage' }))
    fireEvent.click(screen.getByRole('button', { name: 'Add upgrade stage' }))
    const firstId = (screen.getByLabelText('Stable ID for stage 1') as HTMLInputElement).value
    const secondId = (screen.getByLabelText('Stable ID for stage 2') as HTMLInputElement).value
    for (const id of [firstId, secondId]) {
      const worker = screen.getByLabelText(`Worker for stage ${id}`)
      fireEvent.focus(worker)
      fireEvent.change(worker, { target: { value: 'codex.deep' } })
      fireEvent.keyDown(worker, { key: 'Enter' })
    }
    expect(within(stageById(secondId)).getByText(/worker-quality escalation is ineligible/)).toBeTruthy()

    fireEvent.click(within(stageById(secondId)).getByRole('button', { name: 'Move earlier' }))
    expect(within(stageById(secondId)).queryByText(/worker-quality escalation is ineligible/)).toBeNull()
    expect(within(stageById(firstId)).getByText(/worker-quality escalation is ineligible/)).toBeTruthy()
    expect((within(stageById(secondId)).getByLabelText('Stable ID for stage 1') as HTMLInputElement).value).toBe(secondId)
    expect((within(stageById(firstId)).getByLabelText('Stable ID for stage 2') as HTMLInputElement).value).toBe(firstId)
    expect(within(stageById(firstId)).getByText('Inherits from Base')).toBeTruthy()
    expect(within(stageById(secondId)).getByText('Inherits from Base')).toBeTruthy()

    fireEvent.click(within(stageById(secondId)).getByRole('button', { name: 'Remove stage' }))
    const confirmation = screen.getByRole('alertdialog', { name: /Confirm removal of/ })
    fireEvent.click(within(confirmation).getByRole('button', { name: 'Confirm remove stage' }))
    expect((screen.getByLabelText('Stable ID for stage 1') as HTMLInputElement).value).toBe(firstId)
    expect(screen.getByText('Inherits from Base')).toBeTruthy()
    expect(screen.queryByText(/worker-quality escalation is ineligible/)).toBeNull()
  })

  it('rejects a copy source without canonical profiles while retaining the entered base', async () => {
    renderWizard()
    enterFamilyName()
    chooseSource('incomplete')
    await waitFor(() => expect(screen.getByRole('alert').textContent).toMatch(/no canonical effective roles and prompts/i))
    expect((screen.getByLabelText('Family display name') as HTMLInputElement).value).toBe('Product development')
    expect(document.activeElement).toBe(screen.getByRole('alert'))
  })

  it('rejects copied assignments that reference a missing configured profile', async () => {
    renderWizard()
    enterFamilyName()
    chooseSource('missing_profile')
    fireEvent.click(screen.getByRole('button', { name: 'Next: stages' }))
    await waitFor(() => expect(screen.getByRole('alert').textContent).toMatch(/codex\.missing.*not a configured profile/i))
    expect((screen.getByLabelText('Family display name') as HTMLInputElement).value).toBe('Product development')
    expect(document.activeElement).toBe(screen.getByRole('alert'))
  })

  it('keeps child IDs stable while supporting role overrides, reorder, and confirmed removal', () => {
    renderWizard()
    enterFamilyName()
    fireEvent.click(screen.getByRole('button', { name: 'Next: stages' }))
    fireEvent.click(screen.getByRole('button', { name: 'Add upgrade stage' }))
    const firstId = (screen.getByLabelText('Stable ID for stage 1') as HTMLInputElement).value
    fireEvent.change(screen.getByLabelText('Stage 1 display name'), { target: { value: 'Reviewer only' } })
    expect((screen.getByLabelText('Stable ID for stage 1') as HTMLInputElement).value).toBe(firstId)

    const reviewer = screen.getByLabelText(`Reviewer for stage ${firstId}`)
    fireEvent.focus(reviewer)
    fireEvent.change(reviewer, { target: { value: 'codex.deep' } })
    fireEvent.keyDown(reviewer, { key: 'Enter' })
    expect(screen.getByText(/worker.*escalation is ineligible/i)).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Add upgrade stage' }))
    const secondId = (screen.getByLabelText('Stable ID for stage 2') as HTMLInputElement).value
    fireEvent.click(screen.getAllByRole('button', { name: 'Move earlier' })[1])
    expect((screen.getAllByLabelText(/Stable ID for stage/)[0] as HTMLInputElement).value).toBe(secondId)

    fireEvent.click(screen.getAllByRole('button', { name: 'Remove stage' })[0])
    const confirmation = screen.getByRole('alertdialog', { name: /Confirm removal of/ })
    expect(confirmation).toBeTruthy()
    fireEvent.click(within(confirmation).getByRole('button', { name: 'Confirm remove stage' }))
    expect(screen.getAllByLabelText(/Stable ID for stage/)).toHaveLength(1)
  })

  it('retains stage focus and technical disclosure while editing an ID and reordering', () => {
    renderWizard()
    enterFamilyName()
    fireEvent.click(screen.getByRole('button', { name: 'Next: stages' }))
    fireEvent.click(screen.getByRole('button', { name: 'Add upgrade stage' }))
    const firstId = screen.getByLabelText('Stable ID for stage 1') as HTMLInputElement
    const firstDetails = firstId.closest('details') as HTMLDetailsElement
    // happy-dom does not toggle native details from a summary click reliably;
    // set the same DOM state a real browser produces before checking React's
    // keyed update preserves that node and its disclosure.
    firstDetails.setAttribute('open', '')
    firstId.focus()
    fireEvent.change(firstId, { target: { value: `${firstId.value}-edited` } })
    expect(document.activeElement).toBe(firstId)
    expect(firstDetails.hasAttribute('open')).toBe(true)

    fireEvent.click(screen.getByRole('button', { name: 'Add upgrade stage' }))
    const secondId = screen.getByLabelText('Stable ID for stage 2') as HTMLInputElement
    const secondDetails = secondId.closest('details') as HTMLDetailsElement
    secondDetails.setAttribute('open', '')
    fireEvent.click(screen.getAllByRole('button', { name: 'Move earlier' })[1])
    const reorderedStages = [...document.querySelectorAll('fieldset.team-family-wizard-stage')]
    expect((reorderedStages[1].querySelector('details') as HTMLDetailsElement).hasAttribute('open')).toBe(true)
    expect(reorderedStages[1].querySelector('details')).toBe(firstDetails)
    expect((reorderedStages[1].querySelector('input[aria-label="Stable ID for stage 2"]') as HTMLInputElement).value).toBe(`${firstId.value}`)
  })

  it('keeps a colliding generated stage editable after remove and re-add', () => {
    renderWizard()
    enterFamilyName()
    fireEvent.click(screen.getByRole('button', { name: 'Next: stages' }))
    fireEvent.click(screen.getByRole('button', { name: 'Add upgrade stage' }))
    fireEvent.click(screen.getByRole('button', { name: 'Add upgrade stage' }))
    const remainingId = (screen.getByLabelText('Stable ID for stage 2') as HTMLInputElement).value
    fireEvent.click(screen.getAllByRole('button', { name: 'Remove stage' })[0])
    const confirmation = screen.getByRole('alertdialog', { name: /Confirm removal of/ })
    fireEvent.click(within(confirmation).getByRole('button', { name: 'Confirm remove stage' }))
    fireEvent.click(screen.getByRole('button', { name: 'Add upgrade stage' }))
    expect(screen.getByRole('alert').textContent).toMatch(/already in use/i)
    const duplicate = screen.getByLabelText('Stable ID for stage 2') as HTMLInputElement
    expect(duplicate.value).toBe(remainingId)
    fireEvent.change(duplicate, { target: { value: 'recovered_stage' } })
    fireEvent.click(screen.getByRole('button', { name: 'Next: review' }))
    expect(screen.getByRole('heading', { name: 'Review family' })).toBeTruthy()
  })

  it('keeps an existing-team collision editable and accepts a shorter explicit ID', () => {
    const draft = projection()
    draft.teams.product_development_stronger_worker = { roles: {}, prompts: {} }
    renderWizard({ draft })
    enterFamilyName()
    fireEvent.click(screen.getByRole('button', { name: 'Next: stages' }))
    fireEvent.click(screen.getByRole('button', { name: 'Add upgrade stage' }))
    expect(screen.getByRole('alert').textContent).toMatch(/already in use/i)
    const stageId = screen.getByLabelText('Stable ID for stage 1') as HTMLInputElement
    fireEvent.change(stageId, { target: { value: 'short_child' } })
    fireEvent.click(screen.getByRole('button', { name: 'Next: review' }))
    expect(screen.getByRole('heading', { name: 'Review family' })).toBeTruthy()
  })

  it('keeps an overlong generated ID editable for explicit recovery', () => {
    renderWizard()
    enterFamilyName('aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa')
    fireEvent.click(screen.getByRole('button', { name: 'Next: stages' }))
    fireEvent.click(screen.getByRole('button', { name: 'Add upgrade stage' }))
    expect(screen.getByRole('alert').textContent).toMatch(/1–64 letters/i)
    const stageId = screen.getByLabelText('Stable ID for stage 1') as HTMLInputElement
    expect(stageId.value.length).toBeGreaterThan(64)
    fireEvent.change(stageId, { target: { value: 'short_child' } })
    fireEvent.click(screen.getByRole('button', { name: 'Next: review' }))
    expect(screen.getByRole('heading', { name: 'Review family' })).toBeTruthy()
  })

  it('waits for a current canonical preview before copying an edited source', async () => {
    renderWizard({ previewPending: true })
    enterFamilyName()
    chooseSource('source')
    expect(screen.getByRole('alert').textContent).toMatch(/wait for the current settings preview/i)
    await waitFor(() => expect((screen.getByLabelText('Base assignment source') as HTMLSelectElement).selectedIndex).toBe(0))
  })

  it('reviews a zero-stage family and adds one complete candidate to the shared draft', async () => {
    const onAddFamily = vi.fn(async () => {})
    const onClose = vi.fn()
    renderWizard({ onAddFamily, onClose })
    enterFamilyName()
    fireEvent.click(screen.getByRole('button', { name: 'Next: stages' }))
    expect(screen.getByText(/zero-stage family is a valid standalone Base/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Next: review' }))
    fireEvent.click(screen.getByText('Proposed TOML (read-only)'))
    expect(screen.getByText(/\[teams\."product_development"\]/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Add family to draft' }))
    await waitFor(() => expect(onAddFamily).toHaveBeenCalledTimes(1))
    const [candidate, rootId] = onAddFamily.mock.calls[0]
    expect(rootId).toBe('product_development')
    expect(candidate.teams.product_development).toMatchObject({
      display_name: 'Product development',
      roles: {},
      prompts: {},
      upgrade_to: null,
    })
    expect(candidate.teams.product_development.extends).toBeUndefined()
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('reviews a two-stage route with fixed IDs and explicit direct-base links', async () => {
    let candidate: GuidedFormProjection | undefined
    const onAddFamily = vi.fn(async (value: GuidedFormProjection) => { candidate = value })
    renderWizard({ onAddFamily })
    enterFamilyName()
    fireEvent.click(screen.getByRole('button', { name: 'Next: stages' }))
    fireEvent.click(screen.getByRole('button', { name: 'Add upgrade stage' }))
    fireEvent.click(screen.getByRole('button', { name: 'Add upgrade stage' }))
    const firstId = (screen.getByLabelText('Stable ID for stage 1') as HTMLInputElement).value
    const secondId = (screen.getByLabelText('Stable ID for stage 2') as HTMLInputElement).value
    fireEvent.click(screen.getByRole('button', { name: 'Next: review' }))
    const reviewStages = [...document.querySelectorAll('.team-family-wizard-review-stage p')]
    expect(reviewStages).toHaveLength(2)
    expect(reviewStages.every(stage => stage.textContent?.includes('extends product_development'))).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: 'Add family to draft' }))
    await waitFor(() => expect(onAddFamily).toHaveBeenCalledTimes(1))
    expect(candidate?.teams.product_development).toMatchObject({ upgrade_to: firstId })
    expect(candidate?.teams[firstId]).toMatchObject({ extends: 'product_development', upgrade_to: secondId, roles: {} })
    expect(candidate?.teams[secondId]).toMatchObject({ extends: 'product_development', upgrade_to: null, roles: {} })
  })

  it('adds direct-base stages with sparse overrides and retains the wizard after preview failure', async () => {
    let candidate: GuidedFormProjection | undefined
    const onAddFamily = vi.fn(async (value: GuidedFormProjection) => { candidate = value; throw new Error('canonical preview failed') })
    renderWizard({ onAddFamily })
    enterFamilyName()
    fireEvent.click(screen.getByRole('button', { name: 'Next: stages' }))
    fireEvent.click(screen.getByRole('button', { name: 'Add upgrade stage' }))
    const stageId = (screen.getByLabelText('Stable ID for stage 1') as HTMLInputElement).value
    const worker = screen.getByLabelText(`Worker for stage ${stageId}`)
    fireEvent.focus(worker)
    fireEvent.change(worker, { target: { value: 'codex.deep' } })
    fireEvent.keyDown(worker, { key: 'Enter' })
    fireEvent.click(screen.getByRole('button', { name: 'Next: review' }))
    expect(screen.getByText(/1 stored override/)).toBeTruthy()
    fireEvent.click(screen.getByText('Proposed TOML (read-only)'))
    expect(screen.getByText(new RegExp(`extends = "product_development"`))).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Add family to draft' }))
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('canonical preview failed'))
    expect(screen.getByText(/Base · Product development/)).toBeTruthy()
    expect(screen.getByRole('heading', { name: 'Review family' })).toBeTruthy()
    expect(onAddFamily).toHaveBeenCalledTimes(1)
    expect(candidate?.teams[stageId]).toMatchObject({ extends: 'product_development', roles: { worker: 'codex.deep' } })
  })

  it('retains progress when its parent changes presentation without resetting the wizard', () => {
    const { rerender } = renderWizard()
    enterFamilyName()
    fireEvent.click(screen.getByRole('button', { name: 'Next: stages' }))
    rerender(<TeamFamilyWizard draft={projection()} resetVersion={0} onAddFamily={async () => {}} onClose={() => {}} onDirtyChange={() => {}} />)
    expect(screen.getByRole('heading', { name: 'Stages' })).toBeTruthy()
    expect(screen.getByText(/Base · Product development/)).toBeTruthy()
  })
})
