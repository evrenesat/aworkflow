import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { GlobalSettings } from './GlobalSettings'
import type { GuidedFormProjection, ProjectConfigFormResponse } from '../types'

vi.mock('../api', () => ({ getGlobalConfig: vi.fn(), postGlobalConfigForm: vi.fn(), getSettings: vi.fn(), patchGlobalConfig: vi.fn(), saveSettings: vi.fn(), validateGlobalConfig: vi.fn(), projectSettingsText: vi.fn(), listSkills: vi.fn(), readSkill: vi.fn(), saveSkill: vi.fn(), validateSkills: vi.fn(), installSkills: vi.fn() }))
const validation = { state: 'ready' as const, issues: [], placeholders: [], workflows: ['demo'], teams: [], roles: ['worker'] }
const config = { project_id: 'global', revision: 'a'.repeat(64), aflow_toml: 'config', workflows_toml: 'workflows', documents: ['aflow.toml', 'workflows.toml'], validation }
const server = { revision: 'b'.repeat(64), bind_host: 'localhost', bind_port: 8766, managed_projects_root: '/code', password_set: true, advanced_toml: 'server', restart: { bind_host: false, bind_port: false, managed_projects_root: false } }
const form: GuidedFormProjection = { default_workflow: 'demo', max_turns: 5, harnesses: { codex: { worker: { model: 'model', effort: 'high' } } }, roles: { worker: 'codex.worker' }, teams: {}, workflows: {}, workflow_default_teams: {}, prompts: { work: 'Original' }, role_prompts: {} }
const response: ProjectConfigFormResponse = { ...config, changed: false, syntax_issues: [], form, choices: { harnesses: ['codex'], profiles: { codex: ['worker'] }, selectors: ['codex.worker'], roles: ['worker'], teams: [], workflows: ['demo'] }, suggestions: { label: 'suggestion', note: '', harnesses: [{ name: 'codex', supports_effort: true, custom_model_supported: true }], profiles: [{ harness: 'codex', profile: 'worker', model: 'model', effort: 'high' }] } }
const supervisedWorkflows = {
  demo: { declared_steps: ['work'], first_step: 'work', executable_steps: ['work'], first_executable_step: 'work', manager_enabled: null, effective_manager_enabled: false, manager_enabled_source: 'defaults' },
  'demo-alias': { declared_steps: ['work'], first_step: 'work', executable_steps: ['work'], first_executable_step: 'work', manager_enabled: null, effective_manager_enabled: false, manager_enabled_source: 'base:demo' },
}
const supervisedForm: GuidedFormProjection = { ...form, default_manager_enabled: null, workflows: supervisedWorkflows }
const supervisedResponse: ProjectConfigFormResponse = { ...response, form: supervisedForm }
const collisionTeams: GuidedFormProjection['teams'] = {
  base_team: { roles: {} },
  fast_team: { roles: {} },
  fast__team: { roles: {} },
  slow_team: { roles: {} },
}
const collisionWorkflows: GuidedFormProjection['workflows'] = {
  implementation_plans: { declared_steps: ['work'], first_step: 'work', executable_steps: ['work'], first_executable_step: 'work' },
  implementation__plans: { declared_steps: ['work'], first_step: 'work', executable_steps: ['work'], first_executable_step: 'work' },
}
const collisionForm: GuidedFormProjection = {
  ...form,
  default_workflow: 'implementation_plans',
  teams: collisionTeams,
  workflows: collisionWorkflows,
  workflow_default_teams: { implementation_plans: 'base_team', 'implementation__plans': 'base_team' },
}
const collisionResponse: ProjectConfigFormResponse = {
  ...response,
  form: collisionForm,
  validation: { ...validation, workflows: Object.keys(collisionWorkflows), teams: Object.keys(collisionTeams) },
  choices: { ...response.choices!, teams: Object.keys(collisionTeams), workflows: Object.keys(collisionWorkflows) },
}
const skillSummaries = [
  { name: 'aflow-manager', default: true, revision: 'c'.repeat(64), source: 'bundled' as const, edited: false, installed: true, links: [], detected_harnesses: [] },
  { name: 'aflow-assistant', default: false, revision: 'd'.repeat(64), source: 'bundled' as const, edited: false, installed: false, links: [], detected_harnesses: [] },
]
const skillContent = (name: string) => `---\nname: ${name}\ndescription: Test skill.\n---\n\n# ${name}\n`
const skillDetail = (name: string, revision: string) => ({ ...skillSummaries.find(skill => skill.name === name)!, revision, content: skillContent(name) })
describe('GlobalSettings', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    vi.mocked(api.getGlobalConfig).mockResolvedValue(config)
    vi.mocked(api.getSettings).mockResolvedValue(server)
    vi.mocked(api.postGlobalConfigForm).mockResolvedValue(response)
    vi.mocked(api.validateGlobalConfig).mockResolvedValue(validation)
    vi.mocked(api.patchGlobalConfig).mockResolvedValue(config)
    vi.mocked(api.saveSettings).mockResolvedValue(server)
    vi.mocked(api.listSkills).mockResolvedValue(skillSummaries)
    vi.mocked(api.readSkill).mockImplementation(async (name: string) => skillDetail(name, skillSummaries.find(skill => skill.name === name)!.revision))
    vi.mocked(api.validateSkills).mockImplementation(async (entries: { name: string }[]) => entries.map(entry => ({ name: entry.name, ok: true, current_revision: 'c'.repeat(64), error_code: null, error: null })))
    vi.mocked(api.saveSkill).mockImplementation(async (name: string, request: { content: string }) => ({ ...skillSummaries.find(skill => skill.name === name)!, source: 'saved' as const, edited: true, revision: 'e'.repeat(64), content: request.content }))
    vi.mocked(api.installSkills).mockResolvedValue({ mode: 'auto', succeeded: true, cancelled: false, refresh: [], operations: [] })
  })
  it.each(['save', 'blur', 'Enter'])('clears custom effort with %s and has no redundant unset button', async mode => {
    render(<GlobalSettings onDirtyChange={() => {}} onSaved={() => {}} />)
    const effort = await screen.findByLabelText('Effort codex.worker')
    expect(screen.queryByRole('button', { name: /Unset effort/ })).toBeNull()
    fireEvent.change(effort, { target: { value: '' } })
    if (mode === 'blur') fireEvent.blur(effort)
    if (mode === 'Enter') fireEvent.keyDown(effort, { key: 'Enter' })
    fireEvent.click(screen.getByRole('button', { name: 'Save all changes' }))
    await waitFor(() => expect(api.patchGlobalConfig).toHaveBeenCalled())
    expect(vi.mocked(api.patchGlobalConfig).mock.calls[0][0].actions).toContainEqual({ type: 'upsert_profile', harness: 'codex', profile: 'worker', effort: null })
  })
  it('hides all guided navigation in raw mode and retains invalid raw text', async () => {
    render(<GlobalSettings onDirtyChange={() => {}} onSaved={() => {}} />)
    await screen.findByLabelText('Effort codex.worker')
    fireEvent.click(screen.getByRole('tab', { name: 'General' }))
    fireEvent.click(screen.getByRole('button', { name: 'Advanced TOML', exact: true }))
    const raw = await screen.findByLabelText('aflow.toml contents')
    expect(screen.queryAllByRole('tab')).toHaveLength(0)
    fireEvent.change(raw, { target: { value: 'invalid = [' } })
    vi.mocked(api.postGlobalConfigForm).mockResolvedValueOnce({ ...response, form: null })
    fireEvent.click(screen.getByRole('button', { name: 'Guided settings' }))
    await screen.findByText(/Correct the TOML syntax/)
    expect((screen.getByLabelText('aflow.toml contents') as HTMLTextAreaElement).value).toBe('invalid = [')
    expect(screen.queryAllByRole('tab')).toHaveLength(0)
  })
  it('keeps Changelog available when guided configuration projection fails', async () => {
    vi.mocked(api.postGlobalConfigForm).mockRejectedValueOnce(new Error('projection unavailable'))
    render(<GlobalSettings onDirtyChange={() => {}} onSaved={() => {}} />)
    await screen.findByText(/guided settings view is unavailable: projection unavailable/)
    fireEvent.click(screen.getByRole('tab', { name: 'Changelog', exact: true }))
    expect(screen.getByRole('heading', { name: 'Changelog', exact: true })).toBeTruthy()
    expect(screen.getByText('Changes included in this version')).toBeTruthy()
  })
  it('keeps Changelog in the existing keyboard tab sequence', async () => {
    render(<GlobalSettings onDirtyChange={() => {}} onSaved={() => {}} />)
    await screen.findByLabelText('Effort codex.worker')
    const general = screen.getByRole('tab', { name: 'General', exact: true })
    const changelog = screen.getByRole('tab', { name: 'Changelog', exact: true })
    expect(general.getAttribute('aria-controls')).toBe('settings-domain-panel')
    general.focus()
    fireEvent.keyDown(general, { key: 'ArrowRight' })
    expect(document.activeElement).toBe(changelog)
    expect(changelog.getAttribute('aria-selected')).toBe('true')
  })
  it('preserves a dirty General draft across a Changelog round trip', async () => {
    render(<GlobalSettings onDirtyChange={() => {}} onSaved={() => {}} />)
    await screen.findByLabelText('Effort codex.worker')
    fireEvent.click(screen.getByRole('tab', { name: 'General', exact: true }))
    const bindHost = screen.getByLabelText('Bind host') as HTMLInputElement
    fireEvent.change(bindHost, { target: { value: 'unsaved.example' } })
    expect((screen.getByRole('button', { name: 'Save all changes' }) as HTMLButtonElement).disabled).toBe(false)
    fireEvent.click(screen.getByRole('tab', { name: 'Changelog', exact: true }))
    expect(screen.getByRole('heading', { name: 'Changelog', exact: true })).toBeTruthy()
    expect(screen.queryByLabelText('Bind host')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Save all changes', exact: true })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Reload server settings', exact: true })).toBeNull()
    expect(api.patchGlobalConfig).not.toHaveBeenCalled()
    expect(api.saveSettings).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('tab', { name: 'General', exact: true }))
    expect((screen.getByLabelText('Bind host') as HTMLInputElement).value).toBe('unsaved.example')
    expect((screen.getByRole('button', { name: 'Save all changes', exact: true }) as HTMLButtonElement).disabled).toBe(false)
  })
  it('preserves a dirty Skills draft while Changelog has no editing actions', async () => {
    render(<GlobalSettings onDirtyChange={() => {}} onSaved={() => {}} />)
    await screen.findByLabelText('Effort codex.worker')
    fireEvent.click(screen.getByRole('tab', { name: 'Skills', exact: true }))
    const edited = `${skillContent('aflow-manager')}\nChangelog round-trip draft.\n`
    fireEvent.change(await screen.findByLabelText('SKILL.md for aflow-manager'), { target: { value: edited } })
    fireEvent.click(screen.getByRole('tab', { name: 'Changelog', exact: true }))
    expect(screen.getByRole('heading', { name: 'Changelog', exact: true })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Save all changes', exact: true })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Reload server settings', exact: true })).toBeNull()
    expect(api.saveSkill).not.toHaveBeenCalled()
    expect(api.saveSettings).not.toHaveBeenCalled()
    expect(api.patchGlobalConfig).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('tab', { name: 'Skills', exact: true }))
    expect((await screen.findByLabelText('SKILL.md for aflow-manager') as HTMLTextAreaElement).value).toBe(edited)
    expect((screen.getByRole('button', { name: 'Save all changes', exact: true }) as HTMLButtonElement).disabled).toBe(false)
  })
  it('retains the mounted Skills editor and its wrap preference while Changelog is active', async () => {
    const view = render(<GlobalSettings onDirtyChange={() => {}} onSaved={() => {}} />)
    await screen.findByLabelText('Effort codex.worker')
    fireEvent.click(screen.getByRole('tab', { name: 'Skills', exact: true }))
    const area = await screen.findByLabelText('SKILL.md for aflow-manager') as HTMLTextAreaElement
    const edited = `${skillContent('aflow-manager')}Retained editor draft.\n`
    fireEvent.change(area, { target: { value: edited } })
    const wrap = screen.getByRole('checkbox', { name: 'Wrap lines', exact: true }) as HTMLInputElement
    fireEvent.click(wrap)
    expect(wrap.checked).toBe(false)

    fireEvent.click(screen.getByRole('tab', { name: 'Changelog', exact: true }))
    const retained = view.container.querySelector('.settings-retained-skills') as HTMLElement
    expect(retained.hidden).toBe(true)
    expect((retained.querySelector('textarea') as HTMLTextAreaElement).value).toBe(edited)
    expect((retained.querySelector('input[type="checkbox"]') as HTMLInputElement).checked).toBe(false)

    fireEvent.click(screen.getByRole('tab', { name: 'Skills', exact: true }))
    expect((await screen.findByLabelText('SKILL.md for aflow-manager') as HTMLTextAreaElement).value).toBe(edited)
    expect((screen.getByRole('checkbox', { name: 'Wrap lines', exact: true }) as HTMLInputElement).checked).toBe(false)
  })
  it('keeps Advanced TOML editing actions available from a retained Changelog tab', async () => {
    render(<GlobalSettings onDirtyChange={() => {}} onSaved={() => {}} />)
    await screen.findByLabelText('Effort codex.worker')
    fireEvent.click(screen.getByRole('tab', { name: 'Changelog', exact: true }))
    fireEvent.click(screen.getByRole('button', { name: 'Advanced TOML', exact: true }))
    await screen.findByLabelText('aflow.toml contents')
    expect(screen.getByRole('button', { name: 'Save all changes', exact: true })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Guided settings', exact: true }))
    await screen.findByRole('heading', { name: 'Changelog', exact: true })
    expect(screen.queryByRole('button', { name: 'Save all changes', exact: true })).toBeNull()
  })
  it('preserves all deleted prompts and unsaved text and rename across tabs', async () => {
    vi.mocked(api.postGlobalConfigForm).mockResolvedValue({ ...response, form: { ...form, prompts: { work: 'Original', second: 'Second' } } })
    render(<GlobalSettings onDirtyChange={() => {}} onSaved={() => {}} />)
    await screen.findByLabelText('Effort codex.worker')
    fireEvent.click(screen.getByRole('tab', { name: 'Prompts' }))
    fireEvent.click(screen.getByRole('button', { name: 'Work', exact: true }))
    fireEvent.change(screen.getByLabelText('Prompt text'), { target: { value: 'unsaved text' } })
    fireEvent.change(screen.getByLabelText('Prompt key'), { target: { value: 'renamed' } })
    for (const key of ['Renamed', 'Second']) {
      fireEvent.click(screen.getByRole('button', { name: `More actions for prompt ${key}` }))
      fireEvent.click(screen.getByRole('menuitem', { name: 'Delete prompt…' }))
      fireEvent.click(screen.getByRole('button', { name: 'Delete prompt' }))
    }
    fireEvent.click(screen.getByRole('tab', { name: 'General' }))
    fireEvent.click(screen.getByRole('tab', { name: 'Prompts' }))
    fireEvent.click(screen.getByRole('button', { name: 'Undo deletion of Work' }))
    expect((screen.getByLabelText('Prompt text') as HTMLTextAreaElement).value).toBe('unsaved text')
    expect((screen.getByLabelText('Prompt key') as HTMLInputElement).value).toBe('renamed')
    expect(screen.getByRole('button', { name: 'Undo deletion of Second' })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Undo deletion of Second' }))
    expect((screen.getByLabelText('Prompt text') as HTMLTextAreaElement).value).toBe('Second')
  })
  it('retains Undo on save failure and clears it after acknowledged save', async () => {
    vi.mocked(api.patchGlobalConfig).mockRejectedValueOnce(new Error('conflict'))
    render(<GlobalSettings onDirtyChange={() => {}} onSaved={() => {}} />)
    await screen.findByLabelText('Effort codex.worker')
    fireEvent.click(screen.getByRole('tab', { name: 'Prompts' }))
    fireEvent.click(screen.getByRole('button', { name: 'More actions for prompt Work' }))
    fireEvent.click(screen.getByRole('menuitem', { name: 'Delete prompt…' }))
    fireEvent.click(screen.getByRole('button', { name: 'Delete prompt' }))
    fireEvent.click(screen.getByRole('button', { name: 'Save all changes' }))
    await screen.findByText(/conflict.*Your remaining edits/)
    expect(screen.getByRole('button', { name: 'Undo deletion of Work' })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Save all changes' }))
    await screen.findByText(/Workflow settings saved; new runs use the saved configuration/)
    expect(screen.getByText(/existing control-plane runs can select updated teams and profiles/)).toBeDefined()
    expect(screen.queryByRole('button', { name: 'Undo deletion of Work' })).toBeNull()
  })
  it('keeps an Undo collision recoverable and clears recovery on explicit discard', async () => {
    vi.mocked(api.postGlobalConfigForm).mockResolvedValue({ ...response, form: { ...form, prompts: { new_prompt: 'old text' } } })
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
    render(<GlobalSettings onDirtyChange={() => {}} onSaved={() => {}} />)
    await screen.findByLabelText('Effort codex.worker')
    fireEvent.click(screen.getByRole('tab', { name: 'Prompts' }))
    fireEvent.click(screen.getByRole('button', { name: 'More actions for prompt New prompt' }))
    fireEvent.click(screen.getByRole('menuitem', { name: 'Delete prompt…' }))
    fireEvent.click(screen.getByRole('button', { name: 'Delete prompt' }))
    fireEvent.click(screen.getAllByRole('button', { name: 'New prompt', exact: true })[0])
    fireEvent.change(screen.getByLabelText('Prompt text'), { target: { value: 'new text' } })
    fireEvent.click(screen.getByRole('button', { name: 'Undo deletion of New prompt' }))
    expect(screen.getByText(/Cannot restore new_prompt/)).toBeTruthy()
    fireEvent.click(screen.getAllByRole('button', { name: 'New prompt', exact: true })[1])
    expect((screen.getByLabelText('Prompt text') as HTMLTextAreaElement).value).toBe('new text')
    expect(screen.getByRole('button', { name: 'Undo deletion of New prompt' })).toBeTruthy()
    fireEvent.change(screen.getByLabelText('Restore key'), { target: { value: 'recovered_prompt' } })
    fireEvent.click(screen.getByRole('button', { name: 'Undo deletion of New prompt' }))
    expect((screen.getByLabelText('Prompt text') as HTMLTextAreaElement).value).toBe('old text')
    fireEvent.click(screen.getAllByRole('button', { name: 'New prompt', exact: true })[1])
    expect((screen.getByLabelText('Prompt text') as HTMLTextAreaElement).value).toBe('new text')
    fireEvent.click(screen.getByRole('button', { name: 'More actions for prompt New prompt' }))
    fireEvent.click(screen.getByRole('menuitem', { name: 'Delete prompt…' }))
    fireEvent.click(screen.getByRole('button', { name: 'Delete prompt' }))
    fireEvent.click(screen.getByRole('button', { name: 'Reload server settings' }))
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Undo deletion of New prompt' })).toBeNull())
    confirm.mockRestore()
  })
  it('commits recent count only after editing, without configuration writes', async () => {
    localStorage.setItem('aflow.recentRunsLimit', '10')
    render(<GlobalSettings onDirtyChange={() => {}} onSaved={() => {}} />)
    await screen.findByLabelText('Effort codex.worker')
    fireEvent.click(screen.getByRole('tab', { name: 'General' }))
    const input = screen.getByLabelText('Recent non-running runs shown') as HTMLInputElement
    fireEvent.change(input, { target: { value: '' } })
    expect(input.value).toBe('')
    expect(localStorage.getItem('aflow.recentRunsLimit')).toBe('10')
    fireEvent.change(input, { target: { value: '25' } })
    fireEvent.blur(input)
    expect(localStorage.getItem('aflow.recentRunsLimit')).toBe('25')
    fireEvent.change(input, { target: { value: '0' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(screen.getByText('Enter a positive whole number.')).toBeTruthy()
    expect(localStorage.getItem('aflow.recentRunsLimit')).toBe('25')
    expect(api.patchGlobalConfig).not.toHaveBeenCalled()
    expect(api.saveSettings).not.toHaveBeenCalled()
  })
  it('saves typed custom effort without Enter, omitting every untouched field', async () => {
    render(<GlobalSettings onDirtyChange={() => {}} onSaved={() => {}} />)
    fireEvent.change(await screen.findByLabelText('Effort codex.worker'), { target: { value: 'new-effort' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save all changes' }))
    await waitFor(() => expect(api.patchGlobalConfig).toHaveBeenCalledWith({ expected_revision: config.revision, actions: [{ type: 'upsert_profile', harness: 'codex', profile: 'worker', effort: 'new-effort' }] }))
    expect(api.saveSettings).not.toHaveBeenCalled()
  })
  it('drops reverted edits and preserves prompt and server drafts across tabs', async () => {
    render(<GlobalSettings onDirtyChange={() => {}} onSaved={() => {}} />)
    const effort = await screen.findByLabelText('Effort codex.worker')
    fireEvent.change(effort, { target: { value: 'custom' } }); fireEvent.change(effort, { target: { value: 'high' } })
    expect((screen.getByRole('button', { name: 'Save all changes' }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(screen.getByRole('tab', { name: 'Prompts' }))
    fireEvent.change(screen.getByLabelText('Prompt text'), { target: { value: 'λ\n{task}' } })
    fireEvent.click(screen.getByRole('tab', { name: 'General' }))
    fireEvent.change(screen.getByLabelText('Bind host'), { target: { value: '0.0.0.0' } })
    fireEvent.click(screen.getByRole('tab', { name: 'Prompts' }))
    expect((screen.getByLabelText('Prompt text') as HTMLTextAreaElement).value).toBe('λ\n{task}')
    fireEvent.click(screen.getByRole('button', { name: 'Save all changes' }))
    await waitFor(() => expect(api.saveSettings).toHaveBeenCalledWith({ expected_revision: server.revision, bind_host: '0.0.0.0' }))
    expect(api.patchGlobalConfig).toHaveBeenCalledWith({ expected_revision: config.revision, actions: [{ type: 'set_prompt', name: 'work', text: 'λ\n{task}' }] })
    expect(vi.mocked(api.patchGlobalConfig).mock.invocationCallOrder[0]).toBeLessThan(vi.mocked(api.saveSettings).mock.invocationCallOrder[0])
  })
  it('retains failed server edits without resending acknowledged workflow changes', async () => {
    vi.mocked(api.saveSettings).mockRejectedValueOnce(new Error('conflict'))
    render(<GlobalSettings onDirtyChange={() => {}} onSaved={() => {}} />)
    fireEvent.change(await screen.findByLabelText('Effort codex.worker'), { target: { value: 'custom' } })
    fireEvent.click(screen.getByRole('tab', { name: 'General' }))
    fireEvent.change(screen.getByLabelText('Bind host'), { target: { value: '0.0.0.0' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save all changes' }))
    await screen.findByText(/Workflow configuration saved. Remaining settings were not saved/)
    fireEvent.click(screen.getByRole('button', { name: 'Save all changes' }))
    await waitFor(() => expect(api.saveSettings).toHaveBeenCalledTimes(2))
    expect(api.patchGlobalConfig).toHaveBeenCalledTimes(1)
  })
  it('creates a custom profile in the save action without a separate Apply', async () => {
    render(<GlobalSettings onDirtyChange={() => {}} onSaved={() => {}} />)
    fireEvent.change(await screen.findByLabelText('Harness'), { target: { value: 'codex' } })
    fireEvent.change(screen.getByLabelText('New profile name'), { target: { value: 'brand-new' } })
    fireEvent.change(screen.getByLabelText('Model codex.brand-new'), { target: { value: 'new-model' } })
    fireEvent.change(screen.getByLabelText('Effort codex.brand-new'), { target: { value: 'new-effort' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save all changes' }))
    await waitFor(() => expect(api.patchGlobalConfig).toHaveBeenCalledWith({ expected_revision: config.revision, actions: [{ type: 'upsert_profile', harness: 'codex', profile: 'brand-new', model: 'new-model', effort: 'new-effort' }] }))
  })

  it('includes an in-progress prompt rename in Save and preserves it across tabs', async () => {
    render(<GlobalSettings onDirtyChange={() => {}} onSaved={() => {}} />)
    await screen.findByLabelText('Effort codex.worker')
    fireEvent.click(screen.getByRole('tab', { name: 'Prompts' }))
    fireEvent.change(screen.getByLabelText('Prompt key'), { target: { value: 'renamed' } })
    fireEvent.click(screen.getByRole('tab', { name: 'General' }))
    fireEvent.click(screen.getByRole('tab', { name: 'Prompts' }))
    expect((screen.getByLabelText('Prompt key') as HTMLInputElement).value).toBe('renamed')
    fireEvent.click(screen.getByRole('button', { name: 'Save all changes' }))
    await waitFor(() => expect(api.patchGlobalConfig).toHaveBeenCalledWith({ expected_revision: config.revision, actions: [{ type: 'rename_prompt', name: 'work', new_name: 'renamed' }] }))
  })

  it('keeps Advanced TOML usable and saving when the guided projection fails', async () => {
    vi.mocked(api.postGlobalConfigForm).mockRejectedValue(new Error('projection failed'))
    render(<GlobalSettings onDirtyChange={() => {}} onSaved={() => {}} />)
    await screen.findByText(/guided settings view is unavailable/)
    await waitFor(() => expect((screen.getByRole('button', { name: 'Advanced TOML' }) as HTMLButtonElement).disabled).toBe(false))
    fireEvent.click(screen.getByRole('button', { name: 'Advanced TOML' }))
    const aflowArea = (await screen.findByLabelText('aflow.toml contents')) as HTMLTextAreaElement
    expect((screen.getByLabelText('workflows.toml contents') as HTMLTextAreaElement).value).toBe('workflows')
    fireEvent.change(aflowArea, { target: { value: '[roles]\nworker = "codex.worker"\n' } })
    vi.mocked(api.validateGlobalConfig).mockResolvedValue({ ...validation, state: 'ready' })
    fireEvent.click(screen.getByRole('button', { name: 'Save all changes' }))
    await waitFor(() => expect(api.patchGlobalConfig).toHaveBeenCalledWith({ expected_revision: config.revision, documents: { 'aflow.toml': '[roles]\nworker = "codex.worker"\n' } }))
  })

  it('confirmed reload discards every pending edit including the password', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
    render(<GlobalSettings onDirtyChange={() => {}} onSaved={() => {}} />)
    fireEvent.change(await screen.findByLabelText('Effort codex.worker'), { target: { value: 'custom' } })
    fireEvent.click(screen.getByRole('tab', { name: 'General' }))
    fireEvent.change(screen.getByLabelText('Bind host'), { target: { value: '0.0.0.0' } })
    fireEvent.change(screen.getByLabelText('New password (leave empty to keep current)'), { target: { value: 'discarded-secret' } })
    fireEvent.click(screen.getByRole('button', { name: 'Reload server settings' }))
    expect(confirm).toHaveBeenCalledWith('Discard unsaved settings and reload?')
    // The reloaded draft replaces every discarded edit.
    fireEvent.click(await screen.findByRole('tab', { name: 'Agents & Roles' }))
    await waitFor(() => expect(((screen.getByLabelText('Effort codex.worker') as HTMLInputElement | null)?.value)).toBe('high'))
    fireEvent.click(screen.getByRole('tab', { name: 'General' }))
    expect((screen.getByLabelText('New password (leave empty to keep current)') as HTMLInputElement).value).toBe('')
    expect((screen.getByLabelText('Bind host') as HTMLInputElement).value).toBe('localhost')
    expect((screen.getByRole('button', { name: 'Save all changes' }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: 'Save all changes' }))
    await waitFor(() => expect(api.getGlobalConfig).toHaveBeenCalledTimes(2))
    expect(api.saveSettings).not.toHaveBeenCalled()
    expect(api.patchGlobalConfig).not.toHaveBeenCalled()
  })

  it('cancelled reload retains unsaved drafts', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(false)
    render(<GlobalSettings onDirtyChange={() => {}} onSaved={() => {}} />)
    fireEvent.change(await screen.findByLabelText('Effort codex.worker'), { target: { value: 'kept' } })
    fireEvent.click(screen.getByRole('button', { name: 'Reload server settings' }))
    await waitFor(() => expect(api.getGlobalConfig).toHaveBeenCalledTimes(1))
    expect((screen.getByLabelText('Effort codex.worker') as HTMLInputElement).value).toBe('kept')
  })

  it('adds a team to the draft by button and Enter, rejecting blank and duplicate names', async () => {
    const onDirty = vi.fn()
    render(<GlobalSettings onDirtyChange={onDirty} onSaved={() => {}} />)
    await screen.findByLabelText('Effort codex.worker')
    fireEvent.click(screen.getByRole('tab', { name: 'Teams' }))
    const input = screen.getByLabelText('New team name')
    // A blank name is rejected without mutating anything.
    fireEvent.click(screen.getByRole('button', { name: 'Add team' }))
    expect(screen.getByRole('alert').textContent).toContain('Enter a team name')
    expect(api.patchGlobalConfig).not.toHaveBeenCalled()
    // Enter in the form invokes the same handler as the button.
    fireEvent.change(input, { target: { value: 'ds4-stage2' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(screen.getByText(/new — saved with Save all/)).toBeDefined()
    expect(api.patchGlobalConfig).not.toHaveBeenCalled()
    expect(onDirty).toHaveBeenLastCalledWith(true)
    expect((screen.getByLabelText('New team name') as HTMLInputElement).value).toBe('')
    // A duplicate name is rejected.
    fireEvent.change(input, { target: { value: 'ds4-stage2' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add team' }))
    expect(screen.getByRole('alert').textContent).toContain('already exists')
  })

  it('creates two teams, assigns a role, links the chain, and sends add_team first in one Save', async () => {
    render(<GlobalSettings onDirtyChange={() => {}} onSaved={() => {}} />)
    await screen.findByLabelText('Effort codex.worker')
    fireEvent.click(screen.getByRole('tab', { name: 'Teams' }))
    const input = screen.getByLabelText('New team name')
    fireEvent.change(input, { target: { value: 'stage-one' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add team' }))
    fireEvent.change(screen.getByLabelText('New team name'), { target: { value: 'stage-two' } })
    fireEvent.keyDown(screen.getByLabelText('New team name'), { key: 'Enter' })

    // The new teams are editable immediately: assign a role before saving.
    fireEvent.click(screen.getByRole('button', { name: 'stage-one', exact: true }))
    const stageOne = screen.getByRole('group', { name: /stage-one/ })
    const worker = within(stageOne).getByLabelText('Worker')
    fireEvent.focus(worker)
    fireEvent.change(worker, { target: { value: 'codex.worker' } })
    fireEvent.keyDown(worker, { key: 'Enter' })
    // Link the chain through the existing upgrade_to field.
    fireEvent.change(screen.getByLabelText('Upgrade to for team stage-one'), { target: { value: 'stage-two' } })
    const chainText = screen.getAllByText(/Worker chain:/).map(el => el.textContent).join(' ')
    expect(chainText).toContain('stage-one (')
    expect(chainText).toContain('→ stage-two (')

    fireEvent.click(screen.getByRole('button', { name: 'Save all changes' }))
    await waitFor(() => expect(api.patchGlobalConfig).toHaveBeenCalled())
    const actions = vi.mocked(api.patchGlobalConfig).mock.calls[0][0].actions as Array<Record<string, unknown>>
    expect(actions).toEqual([
      { type: 'add_team', team: 'stage-one' },
      { type: 'add_team', team: 'stage-two' },
      { type: 'set_team_role', team: 'stage-one', role: 'worker', selector: 'codex.worker' },
      { type: 'set_team_upgrade', team: 'stage-one', upgrade_to: 'stage-two' },
    ])
  })

  it('disambiguates colliding team and workflow labels while saving the raw upgrade key', async () => {
    vi.mocked(api.postGlobalConfigForm).mockResolvedValue(collisionResponse)
    render(<GlobalSettings onDirtyChange={() => {}} onSaved={() => {}} />)
    await screen.findByLabelText('Effort codex.worker')

    fireEvent.click(screen.getByRole('tab', { name: 'Teams' }))
    expect(screen.getByRole('button', { name: 'Fast team (fast__team)', exact: true })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Fast team (fast_team)', exact: true })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Slow team', exact: true })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Base team', exact: true })).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Fast team (fast_team)', exact: true }))
    const upgrade = screen.getByLabelText('Upgrade to for team Fast team (fast_team)') as HTMLSelectElement
    expect(within(upgrade).getByRole('option', { name: 'Fast team (fast__team)', exact: true })).toBeTruthy()
    fireEvent.change(upgrade, { target: { value: 'fast__team' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save all changes' }))
    await waitFor(() => expect(api.patchGlobalConfig).toHaveBeenCalled())
    expect((vi.mocked(api.patchGlobalConfig).mock.calls[0][0] as { actions: unknown[] }).actions)
      .toContainEqual({ type: 'set_team_upgrade', team: 'fast_team', upgrade_to: 'fast__team' })

    fireEvent.click(screen.getByRole('tab', { name: 'Workflows' }))
    expect(screen.getByRole('button', { name: 'Implementation plans (implementation__plans)', exact: true })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Implementation plans (implementation_plans)', exact: true })).toBeTruthy()
  })

  it('disambiguates colliding global and team roles while saving only the exact raw role key', async () => {
    const roleCollisionForm: GuidedFormProjection = {
      ...form,
      harnesses: { codex: { worker: { model: 'model', effort: 'high' }, reviewer: { model: 'review-model', effort: 'low' } } },
      roles: { code_review: 'codex.worker', code__review: 'codex.worker', unique_role: 'codex.worker' },
      teams: { base_team: { roles: { team_only: 'codex.worker' } } },
    }
    const roleCollisionResponse: ProjectConfigFormResponse = {
      ...response,
      form: roleCollisionForm,
      validation: { ...validation, teams: ['base_team'], roles: Object.keys(roleCollisionForm.roles) },
      choices: { ...response.choices!, profiles: { codex: ['worker', 'reviewer'] }, selectors: ['codex.worker', 'codex.reviewer'], teams: ['base_team'], roles: Object.keys(roleCollisionForm.roles) },
    }
    vi.mocked(api.postGlobalConfigForm).mockResolvedValue(roleCollisionResponse)
    render(<GlobalSettings onDirtyChange={() => {}} onSaved={() => {}} />)
    await screen.findByLabelText('Role Code review (code_review)')
    expect(screen.getByLabelText('Role Code review (code__review)')).toBeTruthy()
    expect(screen.getByLabelText('Role Unique role')).toBeTruthy()

    const codeReview = screen.getByLabelText('Role Code review (code__review)')
    fireEvent.focus(codeReview)
    fireEvent.change(codeReview, { target: { value: 'codex.reviewer' } })
    fireEvent.keyDown(codeReview, { key: 'Enter' })
    fireEvent.click(screen.getByRole('tab', { name: 'Teams' }))
    await screen.findByLabelText('Code review (code_review)')
    expect(screen.getByLabelText('Code review (code__review)')).toBeTruthy()
    expect(screen.getByLabelText('Team only')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Save all changes' }))
    await waitFor(() => expect(api.patchGlobalConfig).toHaveBeenCalled())
    const actions = (vi.mocked(api.patchGlobalConfig).mock.calls[0][0] as { actions: Array<Record<string, unknown>> }).actions
    expect(actions).toContainEqual({ type: 'set_global_role', role: 'code__review', selector: 'codex.reviewer' })
    expect(actions).not.toContainEqual({ type: 'set_global_role', role: 'code_review', selector: 'codex.reviewer' })
  })

  it('lists Skills between Prompts and General with optional labels', async () => {
    render(<GlobalSettings onDirtyChange={() => {}} onSaved={() => {}} />)
    await screen.findByLabelText('Effort codex.worker')
    expect(screen.getAllByRole('tab').map(tab => tab.textContent)).toEqual(['Agents & Roles', 'Teams', 'Workflows', 'Prompts', 'Skills', 'General', 'Changelog'])
    fireEvent.click(screen.getByRole('tab', { name: 'Skills' }))
    await screen.findByLabelText('SKILL.md for aflow-manager')
    expect(screen.getByRole('button', { name: 'aflow-manager', exact: true })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'aflow-assistant (optional)', exact: true })).toBeTruthy()
  })
  it('edits two skills and saves them in sorted order after prevalidation', async () => {
    render(<GlobalSettings onDirtyChange={() => {}} onSaved={() => {}} />)
    await screen.findByLabelText('Effort codex.worker')
    fireEvent.click(screen.getByRole('tab', { name: 'Skills' }))
    const managerArea = await screen.findByLabelText('SKILL.md for aflow-manager')
    fireEvent.change(managerArea, { target: { value: `${skillContent('aflow-manager')}\nManager edit.\n` } })
    fireEvent.click(screen.getByRole('button', { name: 'aflow-assistant (optional)', exact: true }))
    const assistantArea = await screen.findByLabelText('SKILL.md for aflow-assistant')
    fireEvent.change(assistantArea, { target: { value: `${skillContent('aflow-assistant')}\nAssistant edit.\n` } })
    fireEvent.click(screen.getByRole('button', { name: 'Save all changes' }))
    await waitFor(() => expect(api.saveSkill).toHaveBeenCalledTimes(2))
    expect(vi.mocked(api.validateSkills).mock.calls[0][0].map(entry => entry.name)).toEqual(['aflow-assistant', 'aflow-manager'])
    expect(vi.mocked(api.saveSkill).mock.calls.map(call => call[0])).toEqual(['aflow-assistant', 'aflow-manager'])
    // Request bodies carry only content and the baseline revision: no paths or homes.
    expect(vi.mocked(api.saveSkill).mock.calls[0][1]).toEqual({ content: `${skillContent('aflow-assistant')}\nAssistant edit.\n`, expected_revision: 'd'.repeat(64) })
    expect(api.patchGlobalConfig).not.toHaveBeenCalled()
    expect(api.saveSettings).not.toHaveBeenCalled()
    await screen.findByText(/Skill text saved for aflow-assistant, aflow-manager; the next manager invocation uses it/)
    expect((screen.getByRole('button', { name: 'Save all changes' }) as HTMLButtonElement).disabled).toBe(true)
  })
  it('preserves skill drafts across skill, tab, and Advanced TOML navigation', async () => {
    render(<GlobalSettings onDirtyChange={() => {}} onSaved={() => {}} />)
    await screen.findByLabelText('Effort codex.worker')
    fireEvent.click(screen.getByRole('tab', { name: 'Skills' }))
    const edited = `${skillContent('aflow-manager')}\nKept draft.\n`
    fireEvent.change(await screen.findByLabelText('SKILL.md for aflow-manager'), { target: { value: edited } })
    fireEvent.click(screen.getByRole('button', { name: 'aflow-assistant (optional)', exact: true }))
    await screen.findByLabelText('SKILL.md for aflow-assistant')
    fireEvent.click(screen.getByRole('button', { name: 'aflow-manager · unsaved' }))
    expect((await screen.findByLabelText('SKILL.md for aflow-manager') as HTMLTextAreaElement).value).toBe(edited)
    fireEvent.click(screen.getByRole('tab', { name: 'Prompts' }))
    fireEvent.click(screen.getByRole('tab', { name: 'Skills' }))
    expect((await screen.findByLabelText('SKILL.md for aflow-manager') as HTMLTextAreaElement).value).toBe(edited)
    fireEvent.click(screen.getByRole('button', { name: 'Advanced TOML', exact: true }))
    await screen.findByLabelText('aflow.toml contents')
    fireEvent.click(screen.getByRole('button', { name: 'Guided settings' }))
    fireEvent.click(await screen.findByRole('tab', { name: 'Agents & Roles' }))
    await screen.findByLabelText('Effort codex.worker')
    fireEvent.click(screen.getByRole('tab', { name: 'Skills' }))
    expect((await screen.findByLabelText('SKILL.md for aflow-manager') as HTMLTextAreaElement).value).toBe(edited)
    expect((screen.getByRole('button', { name: 'Save all changes' }) as HTMLButtonElement).disabled).toBe(false)
  })
  it('keeps Skills usable when the guided projection fails', async () => {
    vi.mocked(api.postGlobalConfigForm).mockRejectedValue(new Error('projection failed'))
    render(<GlobalSettings onDirtyChange={() => {}} onSaved={() => {}} />)
    await screen.findByText(/guided settings view is unavailable/)
    fireEvent.click(screen.getByRole('tab', { name: 'Skills' }))
    const area = await screen.findByLabelText('SKILL.md for aflow-manager')
    expect((area as HTMLTextAreaElement).value).toBe(skillContent('aflow-manager'))
  })
  it('shows empty and error states for the skill registry', async () => {
    vi.mocked(api.listSkills).mockResolvedValueOnce([])
    render(<GlobalSettings onDirtyChange={() => {}} onSaved={() => {}} />)
    await screen.findByLabelText('Effort codex.worker')
    fireEvent.click(screen.getByRole('tab', { name: 'Skills' }))
    await screen.findByText('No bundled skills are registered.')
  })
  it('reports a skill conflict and keeps the draft', async () => {
    vi.mocked(api.saveSkill).mockRejectedValueOnce(Object.assign(new Error('revision conflict'), { status: 409, detail: { code: 'revision_conflict', current_revision: 'f'.repeat(64) } }))
    render(<GlobalSettings onDirtyChange={() => {}} onSaved={() => {}} />)
    await screen.findByLabelText('Effort codex.worker')
    fireEvent.click(screen.getByRole('tab', { name: 'Skills' }))
    const edited = `${skillContent('aflow-manager')}\nConflicting edit.\n`
    fireEvent.change(await screen.findByLabelText('SKILL.md for aflow-manager'), { target: { value: edited } })
    fireEvent.click(screen.getByRole('button', { name: 'Save all changes' }))
    await screen.findByText(/Skill aflow-manager was not saved/)
    expect((screen.getByLabelText('SKILL.md for aflow-manager') as HTMLTextAreaElement).value).toBe(edited)
    expect((screen.getByRole('button', { name: 'Save all changes' }) as HTMLButtonElement).disabled).toBe(false)
  })
  it('clears only the acknowledged skill when a later skill save fails', async () => {
    vi.mocked(api.saveSkill).mockImplementation(async (name: string, request: { content: string }) => {
      if (name === 'aflow-manager') throw new Error('write failed')
      return { ...skillSummaries.find(skill => skill.name === name)!, source: 'saved' as const, edited: true, revision: 'e'.repeat(64), content: request.content }
    })
    render(<GlobalSettings onDirtyChange={() => {}} onSaved={() => {}} />)
    await screen.findByLabelText('Effort codex.worker')
    fireEvent.click(screen.getByRole('tab', { name: 'Skills' }))
    fireEvent.change(await screen.findByLabelText('SKILL.md for aflow-manager'), { target: { value: `${skillContent('aflow-manager')}\nManager edit.\n` } })
    fireEvent.click(screen.getByRole('button', { name: 'aflow-assistant (optional)', exact: true }))
    const assistantEdited = `${skillContent('aflow-assistant')}\nAssistant edit.\n`
    fireEvent.change(await screen.findByLabelText('SKILL.md for aflow-assistant'), { target: { value: assistantEdited } })
    fireEvent.click(screen.getByRole('button', { name: 'Save all changes' }))
    await screen.findByText(/Skills saved: aflow-assistant\..*remaining drafts are kept/)
    // The acknowledged assistant draft cleared; the failed manager draft stays dirty.
    expect(screen.queryByRole('button', { name: 'aflow-assistant (optional) · unsaved' })).toBeNull()
    expect(screen.getByRole('button', { name: 'aflow-manager · unsaved' })).toBeTruthy()
  })
  it('keeps acknowledged workflow settings when a later skill save fails', async () => {
    vi.mocked(api.saveSkill).mockRejectedValueOnce(new Error('write failed'))
    render(<GlobalSettings onDirtyChange={() => {}} onSaved={() => {}} />)
    fireEvent.change(await screen.findByLabelText('Effort codex.worker'), { target: { value: 'custom' } })
    fireEvent.click(screen.getByRole('tab', { name: 'Skills' }))
    const edited = `${skillContent('aflow-manager')}\nManager edit.\n`
    fireEvent.change(await screen.findByLabelText('SKILL.md for aflow-manager'), { target: { value: edited } })
    fireEvent.click(screen.getByRole('button', { name: 'Save all changes' }))
    await screen.findByText(/Workflow configuration saved\. Remaining settings were not saved\..*Skill aflow-manager was not saved/)
    expect((screen.getByLabelText('SKILL.md for aflow-manager') as HTMLTextAreaElement).value).toBe(edited)
    expect(api.patchGlobalConfig).toHaveBeenCalledTimes(1)
  })
  it('disables Install while edits are unsaved and installs once when clean', async () => {
    render(<GlobalSettings onDirtyChange={() => {}} onSaved={() => {}} />)
    await screen.findByLabelText('Effort codex.worker')
    fireEvent.click(screen.getByRole('tab', { name: 'Skills' }))
    fireEvent.change(await screen.findByLabelText('SKILL.md for aflow-manager'), { target: { value: `${skillContent('aflow-manager')}\nDraft.\n` } })
    const install = screen.getByRole('button', { name: 'Install/reinstall all' })
    expect((install as HTMLButtonElement).disabled).toBe(true)
    expect(screen.getByText(/Save your skill edits first/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Save all changes' }))
    await waitFor(() => expect(api.saveSkill).toHaveBeenCalled())
    await waitFor(() => expect((screen.getByRole('button', { name: 'Install/reinstall all' }) as HTMLButtonElement).disabled).toBe(false))
    fireEvent.click(screen.getByRole('button', { name: 'Install/reinstall all' }))
    await waitFor(() => expect(api.installSkills).toHaveBeenCalledTimes(1))
    // Unrelated TOML drafts are not required: no config write happened.
    expect(api.patchGlobalConfig).not.toHaveBeenCalled()
    await screen.findByText(/Install finished/)
  })
  it('reloads skill baselines after install so edits save against fresh revisions', async () => {
    let managerRevision = 'c'.repeat(64)
    vi.mocked(api.listSkills).mockImplementation(async () => [{ ...skillSummaries[0], revision: managerRevision }, { ...skillSummaries[1] }])
    vi.mocked(api.readSkill).mockImplementation(async (name: string) => skillDetail(name, name === 'aflow-manager' ? managerRevision : 'd'.repeat(64)))
    render(<GlobalSettings onDirtyChange={() => {}} onSaved={() => {}} />)
    await screen.findByLabelText('Effort codex.worker')
    fireEvent.click(screen.getByRole('tab', { name: 'Skills' }))
    await screen.findByLabelText('SKILL.md for aflow-manager')
    managerRevision = 'f'.repeat(64)
    fireEvent.click(screen.getByRole('button', { name: 'Install/reinstall all' }))
    await screen.findByText(/Install finished/)
    await waitFor(() => expect(api.readSkill).toHaveBeenCalledTimes(2))
    fireEvent.change(screen.getByLabelText('SKILL.md for aflow-manager'), { target: { value: `${skillContent('aflow-manager')}\nPost-refresh edit.\n` } })
    fireEvent.click(screen.getByRole('button', { name: 'Save all changes' }))
    await waitFor(() => expect(api.saveSkill).toHaveBeenCalledWith('aflow-manager', { content: `${skillContent('aflow-manager')}\nPost-refresh edit.\n`, expected_revision: 'f'.repeat(64) }))
  })
  it('keeps skill editing disabled until the install refresh settles', async () => {
    let resolveRefreshList!: (value: typeof skillSummaries) => void
    let resolveRefreshDetail!: (value: ReturnType<typeof skillDetail>) => void
    const refreshList = new Promise<typeof skillSummaries>(resolve => { resolveRefreshList = resolve })
    const refreshDetail = new Promise<ReturnType<typeof skillDetail>>(resolve => { resolveRefreshDetail = resolve })
    let listCalls = 0
    vi.mocked(api.listSkills).mockImplementation(async () => {
      listCalls += 1
      return listCalls === 2 ? refreshList : skillSummaries
    })
    let contentReads = 0
    vi.mocked(api.readSkill).mockImplementation((name: string) => {
      contentReads += 1
      return contentReads === 1
        ? Promise.resolve(skillDetail(name, skillSummaries.find(skill => skill.name === name)!.revision))
        : refreshDetail
    })
    render(<GlobalSettings onDirtyChange={() => {}} onSaved={() => {}} />)
    await screen.findByLabelText('Effort codex.worker')
    fireEvent.click(screen.getByRole('tab', { name: 'Skills' }))
    const area = await screen.findByLabelText('SKILL.md for aflow-manager') as HTMLTextAreaElement
    const install = screen.getByRole('button', { name: 'Install/reinstall all' })
    fireEvent.click(install)
    await waitFor(() => expect(api.listSkills).toHaveBeenCalledTimes(2))
    expect(area.disabled).toBe(true)
    expect(screen.queryByText(/Install finished/)).toBeNull()

    resolveRefreshList(skillSummaries.map(skill => skill.name === 'aflow-manager' ? { ...skill, revision: 'f'.repeat(64) } : skill))
    await waitFor(() => expect(api.readSkill.mock.calls.length).toBeGreaterThan(1))
    expect(screen.queryByLabelText('SKILL.md for aflow-manager')).toBeNull()
    expect(screen.queryByText(/Install finished/)).toBeNull()

    resolveRefreshDetail(skillDetail('aflow-manager', 'f'.repeat(64)))
    await screen.findByText(/Install finished/)
    const refreshed = screen.getByLabelText('SKILL.md for aflow-manager') as HTMLTextAreaElement
    expect(refreshed.disabled).toBe(false)
    fireEvent.change(refreshed, { target: { value: `${skillContent('aflow-manager')}\nAfter refresh.\n` } })
    expect(refreshed.value).toContain('After refresh.')
    expect((screen.getByRole('button', { name: 'Install/reinstall all' }) as HTMLButtonElement).disabled).toBe(true)
  })
  it('reports a failed install refresh and restores skill editing', async () => {
    vi.mocked(api.listSkills).mockResolvedValueOnce(skillSummaries).mockRejectedValueOnce(new Error('refresh failed'))
    render(<GlobalSettings onDirtyChange={() => {}} onSaved={() => {}} />)
    await screen.findByLabelText('Effort codex.worker')
    fireEvent.click(screen.getByRole('tab', { name: 'Skills' }))
    const area = await screen.findByLabelText('SKILL.md for aflow-manager') as HTMLTextAreaElement
    fireEvent.click(screen.getByRole('button', { name: 'Install/reinstall all' }))
    await screen.findByText(/Install finished/)
    await screen.findByText(/refresh failed/)
    expect(area.disabled).toBe(false)
    fireEvent.change(area, { target: { value: `${skillContent('aflow-manager')}\nRecovered edit.\n` } })
    expect(area.value).toContain('Recovered edit.')
  })
  it('shows saved-but-uninstalled and optional guidance without an edited-reinstall button', async () => {
    const listed = [
      { ...skillSummaries[0] },
      { ...skillSummaries[1], source: 'saved' as const, edited: true },
    ]
    vi.mocked(api.listSkills).mockResolvedValueOnce(listed)
    vi.mocked(api.readSkill).mockImplementation(async (name: string) => {
      const entry = listed.find(skill => skill.name === name)!
      return { ...entry, links: [], detected_harnesses: [], content: skillContent(name) }
    })
    render(<GlobalSettings onDirtyChange={() => {}} onSaved={() => {}} />)
    await screen.findByLabelText('Effort codex.worker')
    fireEvent.click(screen.getByRole('tab', { name: 'Skills' }))
    fireEvent.click(screen.getByRole('button', { name: 'aflow-assistant (optional)', exact: true }))
    await screen.findByLabelText('SKILL.md for aflow-assistant')
    expect(screen.getByText(/saved but not installed/)).toBeTruthy()
    expect(screen.getByText(/default install excludes it/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: /reinstall edited/i })).toBeNull()
  })
  it('discards skill drafts on explicit reload and ignores late content', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
    let resolveAssistant!: (value: ReturnType<typeof skillDetail>) => void
    vi.mocked(api.readSkill).mockImplementation((name: string): Promise<ReturnType<typeof skillDetail>> => name === 'aflow-assistant'
      ? new Promise<ReturnType<typeof skillDetail>>(resolve => { resolveAssistant = resolve })
      : Promise.resolve(skillDetail(name, skillSummaries.find(skill => skill.name === name)!.revision)))
    render(<GlobalSettings onDirtyChange={() => {}} onSaved={() => {}} />)
    await screen.findByLabelText('Effort codex.worker')
    fireEvent.click(screen.getByRole('tab', { name: 'Skills' }))
    const area = await screen.findByLabelText('SKILL.md for aflow-manager')
    fireEvent.change(area, { target: { value: `${skillContent('aflow-manager')}\nDiscarded.\n` } })
    fireEvent.click(screen.getByRole('button', { name: 'aflow-assistant (optional)', exact: true }))
    await screen.findByText('Loading skill content…')
    fireEvent.click(screen.getByRole('button', { name: 'Reload server settings' }))
    expect(confirm).toHaveBeenCalledWith('Discard unsaved settings and reload?')
    resolveAssistant(skillDetail('aflow-assistant', 'd'.repeat(64)))
    await waitFor(() => expect(api.listSkills).toHaveBeenCalledTimes(2))
    // The stale assistant payload never populates the reloaded state.
    expect((await screen.findByLabelText('SKILL.md for aflow-manager') as HTMLTextAreaElement).value).toBe(skillContent('aflow-manager'))
    expect(screen.queryByRole('button', { name: /unsaved/ })).toBeNull()
    expect((screen.getByRole('button', { name: 'Save all changes' }) as HTMLButtonElement).disabled).toBe(true)
    confirm.mockRestore()
  })
  it('shows an omitted supervision default as disabled with no dirty save', async () => {
    vi.mocked(api.postGlobalConfigForm).mockResolvedValue(supervisedResponse)
    render(<GlobalSettings onDirtyChange={() => {}} onSaved={() => {}} />)
    await screen.findByLabelText('Effort codex.worker')
    fireEvent.click(screen.getByRole('tab', { name: 'Workflows' }))
    expect((screen.getByLabelText('Default manager supervision') as HTMLSelectElement).value).toBe('unset')
    expect(screen.getByRole('option', { name: 'Disabled (default)' })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Demo', exact: true }))
    expect((screen.getByLabelText('Manager supervision for workflow Demo') as HTMLSelectElement).value).toBe('inherit')
    expect(screen.getByText('Effective supervision: Disabled (defaults). Applies to new runs; the launch-default workflow does not affect inheritance.')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'demo-alias', exact: true }))
    expect((screen.getByLabelText('Manager supervision for workflow demo-alias') as HTMLSelectElement).value).toBe('inherit')
    expect(screen.getByText('Effective supervision: Disabled (base Demo). Applies to new runs; the launch-default workflow does not affect inheritance.')).toBeTruthy()
    expect((screen.getByRole('button', { name: 'Save all changes' }) as HTMLButtonElement).disabled).toBe(true)
    expect(api.patchGlobalConfig).not.toHaveBeenCalled()
  })
  it('saves the supervision default and an explicit workflow override as net actions', async () => {
    vi.mocked(api.postGlobalConfigForm).mockResolvedValue(supervisedResponse)
    render(<GlobalSettings onDirtyChange={() => {}} onSaved={() => {}} />)
    await screen.findByLabelText('Effort codex.worker')
    fireEvent.click(screen.getByRole('tab', { name: 'Workflows' }))
    fireEvent.change(screen.getByLabelText('Default manager supervision'), { target: { value: 'enabled' } })
    fireEvent.click(screen.getByRole('button', { name: 'Demo', exact: true }))
    fireEvent.change(screen.getByLabelText('Manager supervision for workflow Demo'), { target: { value: 'disabled' } })
    // Explicit false is a real declaration, not a silent inherit.
    expect((screen.getByLabelText('Manager supervision for workflow Demo') as HTMLSelectElement).value).toBe('disabled')
    fireEvent.click(screen.getByRole('button', { name: 'Save all changes' }))
    await waitFor(() => expect(api.patchGlobalConfig).toHaveBeenCalled())
    const body = vi.mocked(api.patchGlobalConfig).mock.calls[0][0] as { actions: unknown[] }
    expect(body.actions).toContainEqual({ type: 'set_default_manager_enabled', value: true })
    expect(body.actions).toContainEqual({ type: 'set_workflow_manager_enabled', workflow: 'demo', value: false })
    await screen.findByText(/Workflow settings saved; new runs use the saved configuration/)
  })
  it('keeps explicit false on an alias through save', async () => {
    vi.mocked(api.postGlobalConfigForm).mockResolvedValue(supervisedResponse)
    render(<GlobalSettings onDirtyChange={() => {}} onSaved={() => {}} />)
    await screen.findByLabelText('Effort codex.worker')
    fireEvent.click(screen.getByRole('tab', { name: 'Workflows' }))
    fireEvent.click(screen.getByRole('button', { name: 'demo-alias', exact: true }))
    fireEvent.change(screen.getByLabelText('Manager supervision for workflow demo-alias'), { target: { value: 'disabled' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save all changes' }))
    await waitFor(() => expect(api.patchGlobalConfig).toHaveBeenCalled())
    expect((vi.mocked(api.patchGlobalConfig).mock.calls[0][0] as { actions: unknown[] }).actions)
      .toContainEqual({ type: 'set_workflow_manager_enabled', workflow: 'demo-alias', value: false })
  })
  it('removes only the alias flag on inherit', async () => {
    const explicitResponse: ProjectConfigFormResponse = {
      ...supervisedResponse,
      form: {
        ...supervisedForm,
        workflows: {
          ...supervisedWorkflows,
          'demo-alias': { ...supervisedWorkflows['demo-alias'], manager_enabled: false, effective_manager_enabled: false, manager_enabled_source: 'workflow' },
        },
      },
    }
    vi.mocked(api.postGlobalConfigForm).mockResolvedValue(explicitResponse)
    render(<GlobalSettings onDirtyChange={() => {}} onSaved={() => {}} />)
    await screen.findByLabelText('Effort codex.worker')
    fireEvent.click(screen.getByRole('tab', { name: 'Workflows' }))
    fireEvent.click(screen.getByRole('button', { name: 'demo-alias', exact: true }))
    expect((screen.getByLabelText('Manager supervision for workflow demo-alias') as HTMLSelectElement).value).toBe('disabled')
    expect(screen.getByText(/Effective supervision: Disabled \(this workflow\)/)).toBeTruthy()
    fireEvent.change(screen.getByLabelText('Manager supervision for workflow demo-alias'), { target: { value: 'inherit' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save all changes' }))
    await waitFor(() => expect(api.patchGlobalConfig).toHaveBeenCalled())
    expect((vi.mocked(api.patchGlobalConfig).mock.calls[0][0] as { actions: unknown[] }).actions)
      .toContainEqual({ type: 'set_workflow_manager_enabled', workflow: 'demo-alias', value: null })
  })
  it('applies only the latest supervision preview and drops reverted edits', async () => {
    vi.mocked(api.postGlobalConfigForm).mockResolvedValue(supervisedResponse)
    render(<GlobalSettings onDirtyChange={() => {}} onSaved={() => {}} />)
    await screen.findByLabelText('Effort codex.worker')
    fireEvent.click(screen.getByRole('tab', { name: 'Workflows' }))
    fireEvent.click(screen.getByRole('button', { name: 'Demo', exact: true }))
    let resolveFirst!: (value: ProjectConfigFormResponse) => void
    let resolveSecond!: (value: ProjectConfigFormResponse) => void
    vi.mocked(api.postGlobalConfigForm)
      .mockReturnValueOnce(new Promise<ProjectConfigFormResponse>(resolve => { resolveFirst = resolve }))
      .mockReturnValueOnce(new Promise<ProjectConfigFormResponse>(resolve => { resolveSecond = resolve }))
    const preview = (enabled: boolean): ProjectConfigFormResponse => ({
      ...supervisedResponse,
      form: {
        ...supervisedForm,
        workflows: {
          ...supervisedWorkflows,
          demo: { ...supervisedWorkflows.demo, effective_manager_enabled: enabled, manager_enabled_source: 'workflow' },
        },
      },
    })
    fireEvent.change(screen.getByLabelText('Manager supervision for workflow Demo'), { target: { value: 'enabled' } })
    fireEvent.change(screen.getByLabelText('Manager supervision for workflow Demo'), { target: { value: 'disabled' } })
    // The superseded first preview resolves late with stale effective values.
    resolveFirst(preview(true))
    resolveSecond(preview(false))
    await waitFor(() => expect(screen.getByText(/Effective supervision: Disabled \(this workflow\)/)).toBeTruthy())
    // The pending declaration survives every preview reply.
    expect((screen.getByLabelText('Manager supervision for workflow Demo') as HTMLSelectElement).value).toBe('disabled')
    expect(screen.queryByText(/Effective supervision: Enabled/)).toBeNull()
    // Reverting to inherit leaves no net change to save.
    fireEvent.change(screen.getByLabelText('Manager supervision for workflow Demo'), { target: { value: 'inherit' } })
    await waitFor(() => expect((screen.getByRole('button', { name: 'Save all changes' }) as HTMLButtonElement).disabled).toBe(true))
  })
  it('carries the supervision declaration into Advanced TOML previews', async () => {
    vi.mocked(api.postGlobalConfigForm).mockResolvedValue(supervisedResponse)
    render(<GlobalSettings onDirtyChange={() => {}} onSaved={() => {}} />)
    await screen.findByLabelText('Effort codex.worker')
    fireEvent.click(screen.getByRole('tab', { name: 'Workflows' }))
    fireEvent.change(screen.getByLabelText('Default manager supervision'), { target: { value: 'enabled' } })
    fireEvent.click(screen.getByRole('button', { name: 'Advanced TOML', exact: true }))
    await screen.findByLabelText('aflow.toml contents')
    const previews = vi.mocked(api.postGlobalConfigForm).mock.calls.map(call => call[0].action)
    expect(previews).toContainEqual({ type: 'set_default_manager_enabled', value: true })
  })
  it('surfaces an invalid enabled-manager role setup and keeps the draft', async () => {
    vi.mocked(api.postGlobalConfigForm).mockResolvedValue(supervisedResponse)
    vi.mocked(api.validateGlobalConfig).mockResolvedValue({
      ...validation,
      state: 'invalid' as const,
      issues: [{ document: 'workflows.toml', line: 3, message: 'manager.full_role is required for enabled workflow demo.' }],
    })
    render(<GlobalSettings onDirtyChange={() => {}} onSaved={() => {}} />)
    await screen.findByLabelText('Effort codex.worker')
    fireEvent.click(screen.getByRole('tab', { name: 'Workflows' }))
    fireEvent.change(screen.getByLabelText('Default manager supervision'), { target: { value: 'enabled' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save all changes' }))
    await screen.findByText(/manager\.full_role is required.*Your remaining edits are retained/)
    expect((screen.getByLabelText('Default manager supervision') as HTMLSelectElement).value).toBe('enabled')
    expect(api.patchGlobalConfig).not.toHaveBeenCalled()
  })
  it('reports a supervision save conflict and keeps the draft', async () => {
    vi.mocked(api.postGlobalConfigForm).mockResolvedValue(supervisedResponse)
    vi.mocked(api.patchGlobalConfig).mockRejectedValueOnce(new Error('revision conflict'))
    render(<GlobalSettings onDirtyChange={() => {}} onSaved={() => {}} />)
    await screen.findByLabelText('Effort codex.worker')
    fireEvent.click(screen.getByRole('tab', { name: 'Workflows' }))
    fireEvent.change(screen.getByLabelText('Default manager supervision'), { target: { value: 'enabled' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save all changes' }))
    await screen.findByText(/revision conflict.*Your remaining edits are retained/)
    expect((screen.getByLabelText('Default manager supervision') as HTMLSelectElement).value).toBe('enabled')
    expect((screen.getByRole('button', { name: 'Save all changes' }) as HTMLButtonElement).disabled).toBe(false)
  })
  it('shows a field-level cycle error on the chain and never mutates the server on Add', async () => {
    render(<GlobalSettings onDirtyChange={() => {}} onSaved={() => {}} />)
    await screen.findByLabelText('Effort codex.worker')
    fireEvent.click(screen.getByRole('tab', { name: 'Teams' }))
    const input = screen.getByLabelText('New team name')
    fireEvent.change(input, { target: { value: 'alpha-team' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add team' }))
    fireEvent.change(screen.getByLabelText('New team name'), { target: { value: 'beta-team' } })
    fireEvent.keyDown(screen.getByLabelText('New team name'), { key: 'Enter' })

    fireEvent.change(screen.getByLabelText('Upgrade to for team beta-team'), { target: { value: 'alpha-team' } })
    fireEvent.click(screen.getByRole('button', { name: 'alpha-team', exact: true }))
    fireEvent.change(screen.getByLabelText('Upgrade to for team alpha-team'), { target: { value: 'beta-team' } })
    expect(screen.getAllByRole('alert').some(el => el.textContent.includes('cycle'))).toBe(true)
    expect(api.patchGlobalConfig).not.toHaveBeenCalled()
  })
})
