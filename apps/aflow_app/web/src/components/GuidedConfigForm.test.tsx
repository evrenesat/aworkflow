import { useState } from 'react'
import { Combobox } from './Combobox'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import type { ProjectConfigFormResponse } from '../types'
import { GuidedConfigForm } from './GuidedConfigForm'

vi.mock('../api', async () => {
  const actual = await vi.importActual<typeof import('../api')>('../api')
  return { ...actual, postProjectConfigForm: vi.fn() }
})

const project = {
  id: 'beta', display_name: 'Beta Project', current_path: '/srv/code/beta',
  is_git_root: true, registered_at: '2026-01-01T00:00:00Z', readiness: 'configuration_required' as const,
}

const suggestions = {
  label: 'suggestion',
  harnesses: [
    { name: 'claude', supports_effort: true, custom_model_supported: true },
    { name: 'codex', supports_effort: false, custom_model_supported: true },
    { name: 'zcode', supports_effort: true, custom_model_supported: false },
  ],
  profiles: [
    { harness: 'claude', profile: 'deep', model: null, effort: 'high' },
    { harness: 'codex', profile: 'default', model: 'gpt-5.1', effort: null },
    { harness: 'codex', profile: 'fast', model: 'gpt-5-mini', effort: null },
  ],
  note: 'Bundled values are suggestions, not verified accounts.',
}

const configuredForm = {
  default_workflow: 'implement',
  max_turns: null,
  harnesses: {
    claude: { tuned: { model: 'deploy-custom', effort: 'high' } },
    codex: { default: { model: 'gpt-5.1', effort: null } },
  },
  roles: { worker: 'codex.default' },
  teams: { core: { roles: { reviewer: 'codex.fast' } } },
  workflow_default_teams: { implement: 'core' },
  workflows: {
    implement: {
      declared_steps: ['prepare', 'implement', 'review'],
      first_step: 'prepare',
      executable_steps: ['prepare', 'implement', 'review'],
      first_executable_step: 'prepare',
    },
  },
}

const configuredChoices = {
  harnesses: ['claude', 'codex'],
  profiles: { claude: ['tuned'], codex: ['default', 'fast'] },
  selectors: ['claude.tuned', 'codex.default', 'codex.fast'],
  roles: ['worker'],
  teams: ['core'],
  workflows: ['implement'],
}

const emptyChoices = { harnesses: [], profiles: {}, selectors: [], roles: [], teams: [], workflows: [] }

function formResponse(overrides: Record<string, unknown> = {}): ProjectConfigFormResponse {
  return {
    aflow_toml: '# aflow config\n',
    workflows_toml: '# workflows\n',
    changed: false,
    validation: {
      state: 'configuration_required', issues: [], placeholders: [],
      workflows: ['implement'], teams: ['core'], roles: ['worker'],
    },
    form: configuredForm,
    syntax_issues: [],
    choices: configuredChoices,
    suggestions,
    starter_defaults: null,
    ...overrides,
  } as ProjectConfigFormResponse
}

function setup(texts: { aflow?: string; workflows?: string } = {}) {
  const props = {
    project,
    aflowText: texts.aflow ?? '# aflow config\n',
    workflowsText: texts.workflows ?? '# workflows\n',
    onDraftChange: vi.fn(),
    onCandidateValidation: vi.fn(),
    onActionPendingChange: vi.fn(),
    onRequestAdvanced: vi.fn(),
  }
  const view = render(<GuidedConfigForm {...props} />)
  return { props, onDraftChange: props.onDraftChange, onCandidateValidation: props.onCandidateValidation, onRequestAdvanced: props.onRequestAdvanced, view }
}

function pickOption(combobox: HTMLElement, name: RegExp | string) {
  fireEvent.focus(combobox)
  const option = screen.getAllByRole('option', { name }).find(
    (node) => node.closest('.combobox-listbox') !== null,
  ) as HTMLElement
  fireEvent.click(option)
}

function commitComboboxValue(combobox: HTMLElement, value: string) {
  fireEvent.focus(combobox)
  fireEvent.change(combobox, { target: { value } })
  fireEvent.blur(combobox)
}

describe('GuidedConfigForm', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(api.postProjectConfigForm).mockResolvedValue(formResponse())
  })

  it('distinguishes externally configured and unspecified models from missing profiles', async () => {
    vi.mocked(api.postProjectConfigForm).mockResolvedValue(formResponse({
      form: {
        ...configuredForm,
        harnesses: {
          zcode: { default: { model: null, effort: null } },
          codex: { default: { model: null, effort: null } },
        },
        roles: { worker: 'zcode.default', reviewer: 'codex.default', missing: 'codex.absent' },
        teams: {},
        workflow_default_teams: {},
      },
    }))
    setup()
    expect((await screen.findAllByText('Configured in ZCode')).length).toBeGreaterThan(0)
    expect(screen.getAllByText('Not specified').length).toBeGreaterThan(0)
    expect(screen.getAllByText('unknown profile').length).toBeGreaterThan(0)
  })

  it('builds a starter draft from the empty pair using detected Git defaults', async () => {
    vi.mocked(api.postProjectConfigForm).mockResolvedValue(formResponse({
      aflow_toml: '',
      workflows_toml: '',
      form: { default_workflow: null, max_turns: null, harnesses: {}, roles: {}, teams: {}, workflow_default_teams: {}, workflows: {} },
      choices: emptyChoices,
      starter_defaults: { workflow: 'implement', team: null, main_branch: 'trunk', main_branch_source: 'git_head' },
    }))
    const { onDraftChange } = setup({ aflow: '', workflows: '' })
    const build = await screen.findByRole('button', { name: 'Build starter draft' })
    expect((screen.getByLabelText('Workflow') as HTMLInputElement).value).toBe('implement')
    expect((screen.getByLabelText('Main branch') as HTMLInputElement).value).toBe('trunk')

    vi.mocked(api.postProjectConfigForm).mockResolvedValue(formResponse({
      aflow_toml: '# starter aflow\n',
      workflows_toml: '# starter workflows\n',
      changed: true,
      form: { ...configuredForm, harnesses: {}, roles: {}, teams: {}, workflow_default_teams: {} },
      choices: { ...emptyChoices, workflows: ['implement'] },
      starter_defaults: null,
    }))
    fireEvent.click(build)
    await waitFor(() => expect(api.postProjectConfigForm).toHaveBeenCalledWith('beta', {
      aflow_toml: '',
      workflows_toml: '',
      action: { type: 'build_starter', workflow: 'implement', main_branch: 'trunk', team: null },
    }, expect.anything()))
    await waitFor(() => expect(onDraftChange).toHaveBeenCalledWith('# starter aflow\n', '# starter workflows\n'))
  })

  it('renders configured sections with role/profile summaries and labeled suggestions', async () => {
    setup()
    expect(await screen.findByText('Defaults')).toBeDefined()
    expect((screen.getByLabelText('Default workflow') as HTMLSelectElement).value).toBe('implement')
    // The selector appears beside role and workflow assignments with its model.
    expect(screen.getAllByText('codex.default').length).toBeGreaterThanOrEqual(2)
    expect(screen.getAllByText('gpt-5.1').length).toBeGreaterThanOrEqual(2)
    expect(screen.getByText(/team core overrides/)).toBeDefined()
    // Suggestions are labeled wherever they are offered.
    expect(screen.getAllByText(/suggestions/i).length).toBeGreaterThanOrEqual(2)
    // Workflow steps appear in executable order with resolved role sources.
    expect(screen.getByText('prepare')).toBeDefined()
    expect(screen.getByText('global')).toBeDefined()
  })

  it('hydrates an existing profile and preserves its values when applied unchanged', async () => {
    const { onDraftChange } = setup()
    await screen.findByText('Agents and models')
    fireEvent.change(screen.getByLabelText('Harness'), { target: { value: 'claude' } })
    const nameBox = screen.getByRole('combobox', { name: 'Profile name' }) as HTMLInputElement
    // Configured and suggested names are offered and visibly distinguished.
    fireEvent.focus(nameBox)
    const tunedOption = screen.getAllByRole('option', { name: /tuned/ }).find(
      (node) => node.closest('.combobox-listbox') !== null,
    ) as HTMLElement
    expect(within(tunedOption).getByText('configured')).toBeDefined()
    fireEvent.click(tunedOption)
    expect(nameBox.value).toBe('tuned')
    // The configured model and effort hydrate the visible fields.
    expect((screen.getByRole('combobox', { name: 'Model' }) as HTMLInputElement).value).toBe('deploy-custom')
    expect((screen.getByLabelText('Effort') as HTMLSelectElement).value).toBe('high')

    vi.mocked(api.postProjectConfigForm).mockResolvedValue(formResponse({
      changed: true,
      aflow_toml: '# tuned keeps model deploy-custom and effort high\n',
    }))
    fireEvent.click(screen.getByRole('button', { name: 'Apply profile to draft' }))
    await waitFor(() => expect(api.postProjectConfigForm).toHaveBeenLastCalledWith('beta', {
      aflow_toml: '# aflow config\n',
      workflows_toml: '# workflows\n',
      // Neither value was edited, so neither may travel as an explicit null:
      // the server would read that as deletion.
      action: { type: 'upsert_profile', harness: 'claude', profile: 'tuned' },
    }, expect.anything()))
    await waitFor(() => expect(onDraftChange).toHaveBeenCalledWith('# tuned keeps model deploy-custom and effort high\n', '# workflows\n'))
  })

  it('sends an explicit null only through the labeled clear controls', async () => {
    setup()
    await screen.findByText('Agents and models')
    fireEvent.change(screen.getByLabelText('Harness'), { target: { value: 'claude' } })
    pickOption(screen.getByRole('combobox', { name: 'Profile name' }), /tuned/)
    fireEvent.click(screen.getByRole('button', { name: 'Clear model' }))
    expect((screen.getByRole('combobox', { name: 'Model' }) as HTMLInputElement).value).toBe('')
    fireEvent.change(screen.getByLabelText('Effort'), { target: { value: '' } })
    fireEvent.click(screen.getByRole('button', { name: 'Apply profile to draft' }))
    await waitFor(() => expect(api.postProjectConfigForm).toHaveBeenLastCalledWith('beta', expect.objectContaining({
      action: { type: 'upsert_profile', harness: 'claude', profile: 'tuned', model: null, effort: null },
    }), expect.anything()))
  })

  it('offers bundled suggestion values for a suggested profile without claiming availability', async () => {
    setup()
    await screen.findByText('Agents and models')
    fireEvent.change(screen.getByLabelText('Harness'), { target: { value: 'claude' } })
    const nameBox = screen.getByRole('combobox', { name: 'Profile name' }) as HTMLInputElement
    fireEvent.focus(nameBox)
    const deepOption = screen.getAllByRole('option', { name: /deep/ }).find(
      (node) => node.closest('.combobox-listbox') !== null,
    ) as HTMLElement
    expect(within(deepOption).getByText('suggested')).toBeDefined()
    fireEvent.click(deepOption)
    expect(nameBox.value).toBe('deep')
    expect((screen.getByLabelText('Effort') as HTMLSelectElement).value).toBe('high')
    expect(screen.getByText(/not verified as available/)).toBeDefined()

    fireEvent.click(screen.getByRole('button', { name: 'Apply profile to draft' }))
    await waitFor(() => expect(api.postProjectConfigForm).toHaveBeenLastCalledWith('beta', expect.objectContaining({
      action: { type: 'upsert_profile', harness: 'claude', profile: 'deep', effort: 'high' },
    }), expect.anything()))
  })

  it('reseeds dependent fields when the profile identity changes', async () => {
    setup()
    await screen.findByText('Agents and models')
    fireEvent.change(screen.getByLabelText('Harness'), { target: { value: 'claude' } })
    const nameBox = screen.getByRole('combobox', { name: 'Profile name' }) as HTMLInputElement
    pickOption(nameBox, /tuned/)
    expect((screen.getByRole('combobox', { name: 'Model' }) as HTMLInputElement).value).toBe('deploy-custom')
    // The user clears the committed value to browse the full suggestion list,
    // then picks another profile; the tuned values must not leak into it.
    fireEvent.focus(nameBox)
    fireEvent.change(nameBox, { target: { value: '' } })
    const deepOption = screen.getAllByRole('option', { name: /deep/ }).find(
      (node) => node.closest('.combobox-listbox') !== null,
    ) as HTMLElement
    fireEvent.click(deepOption)
    expect((screen.getByRole('combobox', { name: 'Model' }) as HTMLInputElement).value).toBe('')
    expect((screen.getByLabelText('Effort') as HTMLSelectElement).value).toBe('high')
  })

  it('applies a profile with a keyboard-selected suggested model', async () => {
    setup()
    await screen.findByText('Agents and models')
    fireEvent.change(screen.getByLabelText('Harness'), { target: { value: 'codex' } })
    const nameBox = screen.getByRole('combobox', { name: 'Profile name' })
    commitComboboxValue(nameBox, 'default')
    const modelBox = screen.getByRole('combobox', { name: 'Model' }) as HTMLInputElement
    // The existing profile's model hydrates the field before any edit.
    expect(modelBox.value).toBe('gpt-5.1')
    fireEvent.focus(modelBox)
    expect(screen.getByRole('listbox', { name: 'Model suggestions' })).toBeDefined()
    // Typing narrows the filtered suggestions; Enter applies the active one.
    fireEvent.change(modelBox, { target: { value: 'mini' } })
    fireEvent.keyDown(modelBox, { key: 'Enter' })
    expect(modelBox.value).toBe('gpt-5-mini')

    fireEvent.click(screen.getByRole('button', { name: 'Apply profile to draft' }))
    await waitFor(() => expect(api.postProjectConfigForm).toHaveBeenCalledWith('beta', {
      aflow_toml: '# aflow config\n',
      workflows_toml: '# workflows\n',
      // codex does not support an effort setting, so no effort field is sent.
      action: { type: 'upsert_profile', harness: 'codex', profile: 'default', model: 'gpt-5-mini' },
    }, expect.anything()))
  })

  it('accepts a custom model only when the harness contract allows it', async () => {
    setup()
    await screen.findByText('Agents and models')
    fireEvent.change(screen.getByLabelText('Harness'), { target: { value: 'codex' } })
    const modelBox = screen.getByRole('combobox', { name: 'Model' }) as HTMLInputElement
    fireEvent.focus(modelBox)
    fireEvent.change(modelBox, { target: { value: 'my-own-deployment' } })
    fireEvent.blur(modelBox)
    expect(modelBox.value).toBe('my-own-deployment')

    // ZCode owns its model; the control is replaced by an explanation.
    fireEvent.change(screen.getByLabelText('Harness'), { target: { value: 'zcode' } })
    expect(screen.queryByRole('combobox', { name: 'Model' })).toBeNull()
    expect(screen.getByText(/configured in ZCode/)).toBeDefined()
    commitComboboxValue(screen.getByRole('combobox', { name: 'Profile name' }), 'default')
    fireEvent.click(screen.getByRole('button', { name: 'Apply profile to draft' }))
    await waitFor(() => expect(api.postProjectConfigForm).toHaveBeenCalledWith('beta', {
      aflow_toml: '# aflow config\n',
      workflows_toml: '# workflows\n',
      action: { type: 'upsert_profile', harness: 'zcode', profile: 'default' },
    }, expect.anything()))
  })

  it('assigns the worker role to a configured selector and keeps selectors strict', async () => {
    setup()
    await screen.findByText('Roles')
    const roleBox = screen.getByRole('combobox', { name: 'Role' }) as HTMLInputElement
    const selectorBoxes = screen.getAllByRole('combobox', { name: 'Profile (configured choices only)' })
    const selectorBox = selectorBoxes[0] as HTMLInputElement

    fireEvent.focus(selectorBox)
    fireEvent.change(selectorBox, { target: { value: 'codex.fa' } })
    pickOption(selectorBox, /codex\.fast/)
    expect(selectorBox.value).toBe('codex.fast')

    fireEvent.focus(roleBox)
    fireEvent.change(roleBox, { target: { value: 'worker' } })
    fireEvent.blur(roleBox)
    fireEvent.click(screen.getByRole('button', { name: 'Apply role to draft' }))
    await waitFor(() => expect(api.postProjectConfigForm).toHaveBeenCalledWith('beta', expect.objectContaining({
      action: { type: 'set_global_role', role: 'worker', selector: 'codex.fast' },
    }), expect.anything()))

    // A typed selector outside the configured set reverts and never submits.
    fireEvent.focus(selectorBox)
    fireEvent.change(selectorBox, { target: { value: 'codex.ghost' } })
    fireEvent.blur(selectorBox)
    expect(selectorBox.value).toBe('codex.fast')
    // An empty assignment is rejected locally without a request.
    fireEvent.focus(selectorBox)
    fireEvent.change(selectorBox, { target: { value: '' } })
    fireEvent.blur(selectorBox)
    expect(selectorBox.value).toBe('codex.fast')
    const callsBefore = vi.mocked(api.postProjectConfigForm).mock.calls.length
    fireEvent.focus(roleBox)
    fireEvent.change(roleBox, { target: { value: 'reviewer' } })
    fireEvent.blur(roleBox)
    fireEvent.click(screen.getByRole('button', { name: 'Apply role to draft' }))
    await waitFor(() => expect(api.postProjectConfigForm).toHaveBeenCalledTimes(callsBefore + 1))
    expect(api.postProjectConfigForm).toHaveBeenLastCalledWith('beta', expect.objectContaining({
      action: { type: 'set_global_role', role: 'reviewer', selector: 'codex.fast' },
    }), expect.anything())
  })

  it('rejects a missing selector locally and preserves the entries', async () => {
    setup()
    await screen.findByText('Roles')
    const roleBox = screen.getByRole('combobox', { name: 'Role' }) as HTMLInputElement
    fireEvent.focus(roleBox)
    fireEvent.change(roleBox, { target: { value: 'worker' } })
    fireEvent.blur(roleBox)
    const callsBefore = vi.mocked(api.postProjectConfigForm).mock.calls.length
    fireEvent.click(screen.getByRole('button', { name: 'Apply role to draft' }))
    await screen.findByText(/Choose one of the configured profiles/)
    expect(api.postProjectConfigForm).toHaveBeenCalledTimes(callsBefore)
    // The typed role survives the rejected action.
    expect((screen.getByRole('combobox', { name: 'Role' }) as HTMLInputElement).value).toBe('worker')
  })

  it('adds a team and applies a team role override', async () => {
    setup()
    await screen.findByText('Teams')
    fireEvent.change(screen.getByLabelText('New team name'), { target: { value: 'release' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add team' }))
    await waitFor(() => expect(api.postProjectConfigForm).toHaveBeenCalledWith('beta', expect.objectContaining({
      action: { type: 'add_team', team: 'release' },
    }), expect.anything()))

    fireEvent.change(screen.getByLabelText('Team'), { target: { value: 'core' } })
    fireEvent.change(screen.getByLabelText('Team role'), { target: { value: 'worker' } })
    const selectorBox = screen.getAllByRole('combobox', { name: 'Profile (configured choices only)' })[1] as HTMLInputElement
    pickOption(selectorBox, /codex\.default/)
    fireEvent.click(screen.getByRole('button', { name: 'Apply team override to draft' }))
    await waitFor(() => expect(api.postProjectConfigForm).toHaveBeenLastCalledWith('beta', expect.objectContaining({
      action: { type: 'set_team_role', team: 'core', role: 'worker', selector: 'codex.default' },
    }), expect.anything()))
  })

  it('sets a workflow default team, treating empty as no team', async () => {
    setup()
    const select = await screen.findByLabelText('Default team for implement')
    expect((select as HTMLSelectElement).value).toBe('core')
    fireEvent.change(select, { target: { value: '' } })
    await waitFor(() => expect(api.postProjectConfigForm).toHaveBeenCalledWith('beta', expect.objectContaining({
      action: { type: 'set_workflow_default_team', workflow: 'implement', team: null },
    }), expect.anything()))
  })

  it('keeps both texts and points to Advanced TOML on a syntax error', async () => {
    vi.mocked(api.postProjectConfigForm).mockResolvedValue(formResponse({
      aflow_toml: '= broken\n',
      form: null,
      syntax_issues: [{ document: 'workflows.toml', line: 4, message: 'invalid key' }],
      choices: emptyChoices,
    }))
    const { onDraftChange, onRequestAdvanced } = setup({ aflow: '= broken\n' })
    expect(await screen.findByText(/Guided settings are unavailable until the TOML syntax is fixed/)).toBeDefined()
    expect(screen.getByText('workflows.toml:4')).toBeDefined()
    expect(onDraftChange).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Open Advanced TOML to fix the syntax' }))
    expect(onRequestAdvanced).toHaveBeenCalledWith('workflows.toml')
  })

  it('propagates the candidate validation of each accepted response with its pair', async () => {
    const { onCandidateValidation } = setup()
    await screen.findByText('Defaults')
    expect(onCandidateValidation).toHaveBeenLastCalledWith(
      formResponse().validation,
      { aflow: '# aflow config\n', workflows: '# workflows\n' },
    )
  })

  it('shows a bounded updating state instead of stale choices while the draft reprojects', async () => {
    const { props, onCandidateValidation, view } = setup()
    await screen.findByText('Defaults')
    let release!: (value: ProjectConfigFormResponse) => void
    vi.mocked(api.postProjectConfigForm).mockImplementationOnce(
      () => new Promise<ProjectConfigFormResponse>((resolve) => { release = resolve }),
    )
    view.rerender(<GuidedConfigForm {...props} aflowText={'# edited\n'} />)
    expect(await screen.findByText('Updating guided settings…')).toBeDefined()
    // The previous choices are not rendered (or actionable) for the new draft.
    expect(screen.queryByText('Defaults')).toBeNull()
    expect(screen.queryByText('Apply profile to draft')).toBeNull()

    release(formResponse({ aflow_toml: '# edited\n' }))
    expect(await screen.findByText('Defaults')).toBeDefined()
    expect(onCandidateValidation).toHaveBeenLastCalledWith(
      formResponse({ aflow_toml: '# edited\n' }).validation,
      { aflow: '# edited\n', workflows: '# workflows\n' },
    )
  })

  it('replaces a failed reprojection with retry guidance and preserves entries', async () => {
    const { props, view } = setup()
    await screen.findByText('Defaults')
    const roleBox = screen.getByRole('combobox', { name: 'Role' }) as HTMLInputElement
    fireEvent.focus(roleBox)
    fireEvent.change(roleBox, { target: { value: 'worker' } })
    fireEvent.blur(roleBox)
    expect(roleBox.value).toBe('worker')

    vi.mocked(api.postProjectConfigForm).mockRejectedValueOnce(new Error('reprojection failed'))
    view.rerender(<GuidedConfigForm {...props} aflowText={'# next\n'} />)
    expect(await screen.findByText(/Guided settings could not be updated/)).toBeDefined()
    // The stale configured choices are neither rendered nor enabled.
    expect(screen.queryByText('Defaults')).toBeNull()
    expect(screen.queryByText('Apply role to draft')).toBeNull()

    vi.mocked(api.postProjectConfigForm).mockResolvedValue(formResponse({ aflow_toml: '# next\n' }))
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByText('Defaults')).toBeDefined()
    // The draft texts and the typed entry survive the failure and retry.
    expect((screen.getByRole('combobox', { name: 'Role' }) as HTMLInputElement).value).toBe('worker')
  })

  it('hides configured choices when a projection fails after switching projects', async () => {
    const { props, view } = setup()
    await screen.findByText('Defaults')
    vi.mocked(api.postProjectConfigForm).mockRejectedValueOnce(new Error('reprojection failed'))
    view.rerender(
      <GuidedConfigForm {...props} project={{ ...project, id: 'kilo', display_name: 'Kilo' }} />,
    )
    expect(await screen.findByText(/Guided settings could not be updated/)).toBeDefined()
    expect(screen.queryByText('Apply profile to draft')).toBeNull()

    vi.mocked(api.postProjectConfigForm).mockResolvedValue(formResponse())
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByText('Defaults')).toBeDefined()
    // Choices from the previous project are not actionable here.
    expect((screen.getByRole('combobox', { name: 'Role' }) as HTMLInputElement).value).toBe('')
  })

  it('ignores a response whose source draft is no longer current', async () => {
    let releaseStale!: (value: ProjectConfigFormResponse) => void
    // The first (initial) projection is held: it was submitted for the
    // original pair, before the Advanced TOML edit superseded it.
    vi.mocked(api.postProjectConfigForm).mockImplementationOnce(
      () => new Promise<ProjectConfigFormResponse>((resolve) => { releaseStale = resolve }),
    )
    const { props, onDraftChange, view } = setup()
    // The newer projection of the edited pair resolves first.
    vi.mocked(api.postProjectConfigForm).mockResolvedValueOnce(
      formResponse({ aflow_toml: '# edited in advanced\n' }),
    )
    view.rerender(<GuidedConfigForm {...props} aflowText={'# edited in advanced\n'} />)
    expect(await screen.findByText('Defaults')).toBeDefined()

    releaseStale(formResponse({
      aflow_toml: '# stale\n',
      workflows_toml: '# stale\n',
      changed: true,
    }))
    await waitFor(() => new Promise((resolve) => setTimeout(resolve, 20)))
    // The stale response is dropped entirely: no draft replacement, and the
    // form keeps showing only the edited pair's projection.
    expect(onDraftChange).not.toHaveBeenCalled()
    expect(screen.getByText('Defaults')).toBeDefined()
    expect(screen.queryByText('Updating guided settings…')).toBeNull()
  })

  it('clears dependent choices when the project changes', async () => {
    const { props, view } = setup()
    await screen.findByText('Roles')
    const roleBox = screen.getByRole('combobox', { name: 'Role' }) as HTMLInputElement
    fireEvent.focus(roleBox)
    fireEvent.change(roleBox, { target: { value: 'worker' } })
    fireEvent.blur(roleBox)
    expect(roleBox.value).toBe('worker')

    vi.mocked(api.postProjectConfigForm).mockResolvedValue(formResponse({
      form: { ...configuredForm, roles: {}, teams: {}, harnesses: {}, workflow_default_teams: {} },
      choices: emptyChoices,
    }))
    view.rerender(<GuidedConfigForm {...props} project={{ ...project, id: 'kilo', display_name: 'Kilo' }} />)
    await waitFor(() => {
      expect((screen.getByRole('combobox', { name: 'Role' }) as HTMLInputElement).value).toBe('')
    })
  })

  it('preserves the draft and offers retry when the form endpoint fails', async () => {
    vi.mocked(api.postProjectConfigForm).mockRejectedValue(new Error('form endpoint down'))
    const { onDraftChange } = setup()
    expect(await screen.findByText(/Guided settings are temporarily unavailable/)).toBeDefined()
    expect(screen.getByRole('button', { name: 'Retry' })).toBeDefined()
    expect(onDraftChange).not.toHaveBeenCalled()
  })

  it('applies max turns with validation and treats empty as clearing the limit', async () => {
    setup()
    await screen.findByText('Defaults')
    fireEvent.change(screen.getByLabelText('Max turns'), { target: { value: '3' } })
    fireEvent.click(screen.getByRole('button', { name: 'Apply max turns' }))
    await waitFor(() => expect(api.postProjectConfigForm).toHaveBeenCalledWith('beta', expect.objectContaining({
      action: { type: 'set_max_turns', value: 3 },
    }), expect.anything()))

    // A number input cannot hold "zero"; 0 is rejected as below the minimum.
    fireEvent.change(screen.getByLabelText('Max turns'), { target: { value: '0' } })
    const callsBefore = vi.mocked(api.postProjectConfigForm).mock.calls.length
    fireEvent.click(screen.getByRole('button', { name: 'Apply max turns' }))
    await screen.findByText(/whole number of 1 or more/)
    expect(api.postProjectConfigForm).toHaveBeenCalledTimes(callsBefore)

    fireEvent.change(screen.getByLabelText('Max turns'), { target: { value: '' } })
    fireEvent.click(screen.getByRole('button', { name: 'Apply max turns' }))
    await waitFor(() => expect(api.postProjectConfigForm).toHaveBeenLastCalledWith('beta', expect.objectContaining({
      action: { type: 'set_max_turns', value: null },
    }), expect.anything()))
  })
})

function EditableForm() {
  const [pair, setPair] = useState({ aflow: '# aflow config\n', workflows: '# workflows\n' })
  return <GuidedConfigForm project={project} aflowText={pair.aflow} workflowsText={pair.workflows}
    onDraftChange={(aflow, workflows) => setPair({ aflow, workflows })}
    onCandidateValidation={() => {}} onRequestAdvanced={() => {}} />
}

function profileResponse(model: string, effort: string | null) {
  return formResponse({
    changed: true, aflow_toml: `# ${model} ${effort}\n`,
    form: { ...configuredForm, harnesses: { ...configuredForm.harnesses, claude: { tuned: { model, effort } } } },
  })
}

describe('guided edit regressions', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(api.postProjectConfigForm).mockResolvedValue(formResponse())
  })

  async function selectTunedProfile() {
    await screen.findByText('Defaults')
    fireEvent.change(screen.getByLabelText('Harness'), { target: { value: 'claude' } })
    commitComboboxValue(screen.getByRole('combobox', { name: 'Profile name' }), 'tuned')
  }

  it('applies model and effort changes back to their original values after an accepted update', async () => {
    vi.mocked(api.postProjectConfigForm).mockResolvedValueOnce(formResponse({ suggestions: { ...suggestions, profiles: [...suggestions.profiles, { harness: 'claude', profile: 'balanced', model: null, effort: 'medium' }] } }))
    render(<EditableForm />)
    await selectTunedProfile()
    vi.mocked(api.postProjectConfigForm).mockResolvedValueOnce(profileResponse('model-B', 'medium'))
    commitComboboxValue(screen.getByRole('combobox', { name: 'Model' }), 'model-B')
    fireEvent.change(screen.getByLabelText('Effort'), { target: { value: 'medium' } })
    fireEvent.click(screen.getByRole('button', { name: 'Apply profile to draft' }))
    await waitFor(() => expect((screen.getByLabelText('Effort') as HTMLSelectElement).value).toBe('medium'))
    vi.mocked(api.postProjectConfigForm).mockResolvedValueOnce(profileResponse('deploy-custom', 'high'))
    commitComboboxValue(screen.getByRole('combobox', { name: 'Model' }), 'deploy-custom')
    fireEvent.change(screen.getByLabelText('Effort'), { target: { value: 'high' } })
    fireEvent.click(screen.getByRole('button', { name: 'Apply profile to draft' }))
    await waitFor(() => expect(api.postProjectConfigForm).toHaveBeenLastCalledWith('beta',
      expect.objectContaining({ action: { type: 'upsert_profile', harness: 'claude', profile: 'tuned', model: 'deploy-custom', effort: 'high' } }),
      expect.anything()))
    await waitFor(() => expect((screen.getByLabelText('Effort') as HTMLSelectElement).value).toBe('high'))
  })

  it('keeps newer profile entry text when an earlier update returns', async () => {
    render(<EditableForm />)
    await selectTunedProfile()
    let release!: (value: ProjectConfigFormResponse) => void
    vi.mocked(api.postProjectConfigForm).mockImplementationOnce(() => new Promise((resolve) => { release = resolve }))
    const model = screen.getByRole('combobox', { name: 'Model' })
    commitComboboxValue(model, 'model-B')
    fireEvent.click(screen.getByRole('button', { name: 'Apply profile to draft' }))
    commitComboboxValue(model, 'model-C')
    release(profileResponse('model-B', 'high'))
    await waitFor(() => expect((screen.getByRole('button', { name: 'Apply profile to draft' }) as HTMLButtonElement).disabled).toBe(false))
    expect((screen.getByRole('combobox', { name: 'Model' }) as HTMLInputElement).value).toBe('model-C')
    vi.mocked(api.postProjectConfigForm).mockResolvedValueOnce(profileResponse('model-C', 'high'))
    fireEvent.click(screen.getByRole('button', { name: 'Apply profile to draft' }))
    await waitFor(() => expect(api.postProjectConfigForm).toHaveBeenLastCalledWith('beta',
      expect.objectContaining({ action: { type: 'upsert_profile', harness: 'claude', profile: 'tuned', model: 'model-C' } }),
      expect.anything()))
    await waitFor(() => expect((screen.getByRole('button', { name: 'Apply profile to draft' }) as HTMLButtonElement).disabled).toBe(false))
  })

  it('keeps bundled models labeled suggested after selection and distinguishes harness sources', async () => {
    setup()
    await screen.findByText('Defaults')
    expect(screen.getByRole('option', { name: 'codex — configured' })).toBeDefined()
    expect(screen.getByRole('option', { name: 'zcode — suggested' })).toBeDefined()
    fireEvent.change(screen.getByLabelText('Harness'), { target: { value: 'codex' } })
    commitComboboxValue(screen.getByRole('combobox', { name: 'Profile name' }), 'fast')
    const model = screen.getByRole('combobox', { name: 'Model' })
    pickOption(model, 'gpt-5-mini suggested')
    fireEvent.focus(model)
    expect(screen.getByRole('option', { name: 'gpt-5-mini suggested' })).toBeDefined()
    expect(screen.queryByRole('option', { name: 'gpt-5-mini configured' })).toBeNull()
  })

  it('shows custom configured effort, preserves it unchanged, and supports deliberate clearing', async () => {
    const custom = formResponse({ form: { ...configuredForm, harnesses: { ...configuredForm.harnesses, claude: { tuned: { model: 'deploy-custom', effort: 'ultra' } } } } })
    vi.mocked(api.postProjectConfigForm).mockResolvedValue(custom)
    render(<EditableForm />)
    await selectTunedProfile()
    const effort = screen.getByLabelText('Effort') as HTMLSelectElement
    expect(effort.value).toBe('ultra')
    expect(within(effort).getByRole('option', { name: 'ultra — configured' })).toBeDefined()
    fireEvent.click(screen.getByRole('button', { name: 'Apply profile to draft' }))
    await waitFor(() => expect(api.postProjectConfigForm).toHaveBeenLastCalledWith('beta',
      expect.objectContaining({ action: { type: 'upsert_profile', harness: 'claude', profile: 'tuned' } }), expect.anything()))
    await waitFor(() => expect((screen.getByRole('button', { name: 'Apply profile to draft' }) as HTMLButtonElement).disabled).toBe(false))
    vi.mocked(api.postProjectConfigForm).mockResolvedValueOnce(profileResponse('deploy-custom', null))
    fireEvent.change(effort, { target: { value: '' } })
    fireEvent.click(screen.getByRole('button', { name: 'Apply profile to draft' }))
    await waitFor(() => expect(api.postProjectConfigForm).toHaveBeenLastCalledWith('beta',
      expect.objectContaining({ action: { type: 'upsert_profile', harness: 'claude', profile: 'tuned', effort: null } }), expect.anything()))
    await screen.findByRole('row', { name: 'claude.tuned deploy-custom —' })
  })

  it('announces the filtered keyboard option without nested interactive elements', () => {
    const onChange = vi.fn()
    const view = render(<Combobox label="Choice" value="" onChange={onChange} options={['alpha', 'beta']} allowCustom />)
    const input = screen.getByRole('combobox', { name: 'Choice' })
    fireEvent.focus(input)
    fireEvent.keyDown(input, { key: 'ArrowDown' })
    expect(document.getElementById(input.getAttribute('aria-activedescendant')!)?.textContent).toBe('beta')
    fireEvent.change(input, { target: { value: 'alp' } })
    expect(document.getElementById(input.getAttribute('aria-activedescendant')!)?.textContent).toBe('alpha')
    expect(within(screen.getByRole('option', { name: 'alpha' })).queryByRole('button')).toBeNull()
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onChange).toHaveBeenLastCalledWith('alpha')
    fireEvent.focus(input)
    view.rerender(<Combobox label="Choice" value="" onChange={onChange} options={['alpha', 'beta']} disabled />)
    expect(input.getAttribute('aria-expanded')).toBe('false')
    expect(input.hasAttribute('aria-activedescendant')).toBe(false)
    expect(screen.queryByRole('listbox')).toBeNull()
  })
  it.each(['failure', 'unmount', 'project change'])('clears action pending state on %s', async (ending) => {
    const { props, view } = setup()
    await screen.findByText('Defaults')
    let reject!: (reason: Error) => void
    vi.mocked(api.postProjectConfigForm).mockImplementationOnce(() => new Promise((_resolve, rejectPromise) => { reject = rejectPromise }))
    fireEvent.change(screen.getByLabelText('Max turns'), { target: { value: '8' } })
    fireEvent.click(screen.getByRole('button', { name: 'Apply max turns' }))
    expect(props.onActionPendingChange).toHaveBeenLastCalledWith(true)
    const call = vi.mocked(api.postProjectConfigForm).mock.calls.at(-1)!
    const signal = call[2]!.signal!
    if (ending === 'failure') {
      reject(new Error('action unavailable'))
      await screen.findByText('action unavailable')
    } else if (ending === 'unmount') {
      view.unmount()
      expect(signal.aborted).toBe(true)
    } else {
      view.rerender(<GuidedConfigForm {...props} project={{ ...project, id: 'gamma' }} />)
      expect(signal.aborted).toBe(true)
      await screen.findByText('Defaults')
    }
    await waitFor(() => expect(props.onActionPendingChange).toHaveBeenLastCalledWith(false))
  })

})
