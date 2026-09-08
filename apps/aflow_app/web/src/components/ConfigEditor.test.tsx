import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api'
import * as api from '../api'
import type { ProjectInfo } from '../types'
import { ConfigEditor } from './ConfigEditor'

vi.mock('../api', async () => {
  const actual = await vi.importActual<typeof import('../api')>('../api')
  return {
    ...actual,
    getGlobalConfig: vi.fn(),
    saveGlobalConfig: vi.fn(),
    validateGlobalConfig: vi.fn(),
    postGlobalConfigForm: vi.fn(),
  }
})

const project: ProjectInfo = {
  id: 'beta', display_name: 'Beta Project', current_path: '/srv/code/beta',
  is_git_root: true, registered_at: '2026-01-01T00:00:00Z', readiness: 'configuration_required',
}

const revisionA = 'a'.repeat(64)
const revisionB = 'b'.repeat(64)

function configPayload(state: 'ready' | 'configuration_required' | 'invalid' = 'configuration_required', revision = revisionA) {
  return {
    project_id: 'beta',
    revision,
    documents: ['aflow.toml', 'workflows.toml'],
    aflow_toml: '# aflow config\n',
    workflows_toml: '# workflows\n',
    validation: {
      state,
      issues: state === 'invalid'
        ? [{ document: 'workflows.toml', line: 3, message: 'unknown team reference' }]
        : [],
      placeholders: state === 'ready' ? [] : ['harness.starter.profiles.default.model'],
      workflows: ['starter'],
      teams: [],
      roles: ['worker'],
    },
  }
}

function formPayload(overrides: Record<string, unknown> = {}) {
  return {
    aflow_toml: '# aflow config\n',
    workflows_toml: '# workflows\n',
    changed: false,
    validation: configPayload().validation,
    form: {
      default_workflow: null,
      max_turns: null,
      harnesses: {},
      roles: {},
      teams: {},
      workflow_default_teams: {},
      workflows: {},
    },
    syntax_issues: [],
    choices: { harnesses: [], profiles: {}, selectors: [], roles: [], teams: [], workflows: [] },
    suggestions: {
      label: 'suggestion',
      harnesses: [{ name: 'codex', supports_effort: false, custom_model_supported: true }],
      profiles: [{ harness: 'codex', profile: 'default', model: 'gpt-5', effort: null }],
      note: 'Bundled values are labeled suggestions.',
    },
    starter_defaults: null,
    ...overrides,
  }
}

function renderEditor() {
  return render(
    <ConfigEditor project={project} onDirtyChange={vi.fn()} onReady={vi.fn()} />,
  )
}

describe('ConfigEditor', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(api.getGlobalConfig).mockResolvedValue(configPayload())
    vi.mocked(api.postGlobalConfigForm).mockResolvedValue(formPayload())
  })

  it('loads both documents under one shared revision with accessible tabs', async () => {
    renderEditor()
    await screen.findByText('Defaults')
    // Guided settings are the default view; the documents open under Advanced TOML.
    fireEvent.click(screen.getByRole('button', { name: 'Advanced TOML' }))
    expect((screen.getByLabelText('aflow.toml contents') as HTMLTextAreaElement).value).toBe('# aflow config\n')
    expect(screen.getByText(`Revision ${revisionA.slice(0, 12)}`)).toBeDefined()
    const aflowTab = screen.getByRole('tab', { name: 'aflow.toml' })
    expect(aflowTab.getAttribute('aria-selected')).toBe('true')
    expect(screen.getByLabelText('workflows.toml contents').parentElement?.getAttribute('hidden')).not.toBeNull()

    fireEvent.click(screen.getByRole('tab', { name: 'workflows.toml' }))
    expect(screen.getByRole('tab', { name: 'workflows.toml' }).getAttribute('aria-selected')).toBe('true')
    expect((screen.getByLabelText('workflows.toml contents') as HTMLTextAreaElement).value).toBe('# workflows\n')
    expect(screen.getByLabelText('aflow.toml contents').parentElement?.getAttribute('hidden')).not.toBeNull()
  })

  it('keeps missing saved files unready when the pure empty draft validates', async () => {
    const empty = {
      ...configPayload(),
      documents: [],
      aflow_toml: '',
      workflows_toml: '',
      validation: {
        ...configPayload().validation, placeholders: [], workflows: [], roles: [],
      },
    }
    vi.mocked(api.getGlobalConfig).mockResolvedValue(empty)
    const candidate = { ...empty.validation, state: 'ready' as const }
    vi.mocked(api.postGlobalConfigForm).mockResolvedValue(formPayload({
      aflow_toml: '', workflows_toml: '', validation: candidate,
    }))
    vi.mocked(api.validateGlobalConfig).mockResolvedValue(candidate)
    renderEditor()
    await screen.findByText('Set up this project')
    expect(screen.getByText(/build a starter draft, then save both files/)).toBeDefined()
    expect(screen.queryByRole('button', { name: 'Go to plans' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Validate' }))
    await waitFor(() => expect(api.validateGlobalConfig).toHaveBeenCalled())
    expect(screen.queryByText(/the project configuration is ready/)).toBeNull()
    expect(screen.queryByRole('button', { name: 'Go to plans' })).toBeNull()
  })

  it('shows a load error with retry instead of inventing configuration text', async () => {
    vi.mocked(api.getGlobalConfig).mockRejectedValue(new Error('operation_rejected'))
    renderEditor()
    await screen.findByText(/operation_rejected/)
    expect(screen.queryByLabelText('aflow.toml contents')).toBeNull()
    vi.mocked(api.getGlobalConfig).mockResolvedValue(configPayload())
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await screen.findByLabelText('aflow.toml contents')
  })

  it('validates the candidate pair without saving and renders bounded diagnostics', async () => {
    vi.mocked(api.validateGlobalConfig).mockResolvedValue(configPayload('invalid').validation)
    renderEditor()
    await screen.findByLabelText('aflow.toml contents')
    fireEvent.change(screen.getByLabelText('aflow.toml contents'), { target: { value: '# broken\n' } })
    fireEvent.click(screen.getByRole('button', { name: 'Validate' }))

    await waitFor(() => expect(api.validateGlobalConfig).toHaveBeenCalledWith({
      aflow_toml: '# broken\n',
      workflows_toml: '# workflows\n',
    }))
    await screen.findByText(/Invalid configuration/)
    expect(screen.getByText(/unknown team reference/)).toBeDefined()
    expect(screen.getByText(/workflows\.toml:3/)).toBeDefined()
    expect(api.saveGlobalConfig).not.toHaveBeenCalled()
  })

  it('saves both files atomically with the expected revision and refreshes the snapshot', async () => {
    vi.mocked(api.saveGlobalConfig).mockResolvedValue(configPayload('ready', revisionB))
    // The server validates one pair one way: the projection of the committed
    // texts agrees with the save result instead of contradicting it.
    vi.mocked(api.postGlobalConfigForm).mockResolvedValue(
      formPayload({ validation: configPayload('ready').validation }),
    )
    const onReady = vi.fn()
    const onDirty = vi.fn()
    render(<ConfigEditor onDirtyChange={onDirty} onReady={onReady} />)
    await screen.findByLabelText('aflow.toml contents')
    fireEvent.change(screen.getByLabelText('aflow.toml contents'), { target: { value: '# configured\n' } })
    await waitFor(() => expect(onDirty).toHaveBeenCalledWith(true))
    fireEvent.click(screen.getByRole('button', { name: 'Save both files' }))

    await waitFor(() => expect(api.saveGlobalConfig).toHaveBeenCalledWith({
      aflow_toml: '# configured\n',
      workflows_toml: '# workflows\n',
      expected_revision: revisionA,
    }))
    await screen.findByText(`Saved revision ${revisionB.slice(0, 12)}.`)
    expect((screen.getByLabelText('aflow.toml contents') as HTMLTextAreaElement).value).toBe('# aflow config\n')
    fireEvent.click(screen.getByRole('button', { name: 'Go to plans' }))
    expect(onReady).toHaveBeenCalledTimes(1)
  })

  it('preserves local text on a stale revision and reloads only after confirmation', async () => {
    vi.mocked(api.saveGlobalConfig).mockRejectedValue(
      new ApiError(409, 'conflict', 'revision_conflict', { current_revision: revisionB }),
    )
    renderEditor()
    await screen.findByLabelText('aflow.toml contents')
    fireEvent.change(screen.getByLabelText('aflow.toml contents'), { target: { value: '# mine\n' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save both files' }))

    await screen.findByText(/changed on the server/)
    expect(screen.getByText(new RegExp(revisionB.slice(0, 12)))).toBeDefined()
    expect((screen.getByLabelText('aflow.toml contents') as HTMLTextAreaElement).value).toBe('# mine\n')

    vi.mocked(api.getGlobalConfig).mockResolvedValue(configPayload('ready', revisionB))
    fireEvent.click(screen.getByRole('button', { name: 'Reload server copy…' }))
    expect(screen.getByText(/Discard the local edits in both tabs/)).toBeDefined()
    fireEvent.click(screen.getByRole('button', { name: 'Discard edits and reload' }))
    await waitFor(() => expect(api.getGlobalConfig).toHaveBeenCalledTimes(2))
    await screen.findByText(`Revision ${revisionB.slice(0, 12)}`)
    expect((screen.getByLabelText('aflow.toml contents') as HTMLTextAreaElement).value).toBe('# aflow config\n')
    expect(screen.queryByText(/changed on the server/)).toBeNull()
  })

  it('shows guided settings by default and shares one draft pair with Advanced TOML', async () => {
    renderEditor()
    expect((await screen.findByRole('button', { name: 'Guided settings' })).getAttribute('aria-pressed')).toBe('true')
    // Both views stay mounted; only one is visible. The guided content arrives
    // from a second asynchronous projection, so it must be awaited too.
    expect(await screen.findByText('Defaults')).toBeDefined()
    expect(screen.getByLabelText('aflow.toml contents').closest('.toml-panel')?.hidden).toBe(true)

    // The reprojection of the manual edit echoes the edited pair.
    vi.mocked(api.postGlobalConfigForm).mockResolvedValueOnce(
      formPayload({ aflow_toml: '# manual\n' }),
    )
    fireEvent.click(screen.getByRole('button', { name: 'Advanced TOML' }))
    expect(screen.getByRole('button', { name: 'Advanced TOML' }).getAttribute('aria-pressed')).toBe('true')
    expect(screen.getByLabelText('aflow.toml contents').closest('.toml-panel')?.hidden).toBe(false)
    // An Advanced TOML edit is immediately visible to the shared draft.
    fireEvent.change(screen.getByLabelText('aflow.toml contents'), { target: { value: '# manual\n' } })
    fireEvent.click(screen.getByRole('button', { name: 'Guided settings' }))
    expect((screen.getByLabelText('aflow.toml contents') as HTMLTextAreaElement).value).toBe('# manual\n')
    // The manual edit's reprojection settles before the test ends.
    expect(await screen.findByText('Defaults')).toBeDefined()
  })

  it('saves a guided draft with the last committed revision and continues to Plans once ready', async () => {
    vi.mocked(api.saveGlobalConfig).mockResolvedValue(configPayload('ready', revisionB))
    const onReady = vi.fn()
    const onDirty = vi.fn()
    render(<ConfigEditor onDirtyChange={onDirty} onReady={onReady} />)
    await screen.findByText('Defaults')
    // The draft action result is shared: an Advanced TOML edit becomes the
    // candidate pair that guided Save commits.
    fireEvent.click(screen.getByRole('button', { name: 'Advanced TOML' }))
    fireEvent.change(screen.getByLabelText('aflow.toml contents'), { target: { value: '# starter pair\n' } })
    fireEvent.click(screen.getByRole('button', { name: 'Guided settings' }))
    await waitFor(() => expect(onDirty).toHaveBeenCalledWith(true))
    expect((screen.getByLabelText('aflow.toml contents') as HTMLTextAreaElement).value).toBe('# starter pair\n')

    fireEvent.click(screen.getByRole('button', { name: 'Save and continue to Plans' }))
    await waitFor(() => expect(api.saveGlobalConfig).toHaveBeenCalledWith({
      aflow_toml: '# starter pair\n',
      workflows_toml: '# workflows\n',
      expected_revision: revisionA,
    }))
    await waitFor(() => expect(onReady).toHaveBeenCalledTimes(1))
    expect(onReady).toHaveBeenCalledWith(expect.objectContaining({ revision: revisionB }))
  })

  it('reports unsaved state to the shell and guards page reload while dirty', async () => {
    const addSpy = vi.spyOn(window, 'addEventListener')
    const removeSpy = vi.spyOn(window, 'removeEventListener')
    renderEditor()
    await screen.findByLabelText('aflow.toml contents')
    fireEvent.change(screen.getByLabelText('aflow.toml contents'), { target: { value: '# dirty\n' } })
    await waitFor(() => expect(
      addSpy.mock.calls.filter(([type]) => type === 'beforeunload'),
    ).toHaveLength(1))
    fireEvent.change(screen.getByLabelText('aflow.toml contents'), { target: { value: '# aflow config\n' } })
    await waitFor(() => expect(
      removeSpy.mock.calls.filter(([type]) => type === 'beforeunload'),
    ).toHaveLength(1))
    addSpy.mockRestore()
    removeSpy.mockRestore()
  })

  it('replaces a committed ready report when the guided candidate is no longer ready', async () => {
    vi.mocked(api.getGlobalConfig).mockResolvedValue(configPayload('ready'))
    renderEditor()
    // The committed snapshot is ready, but the guided candidate response is
    // not: the visible report must follow the candidate, never the snapshot.
    expect(await screen.findByText('Configuration required — set explicit model selectors before starting workflows.')).toBeDefined()
    expect(screen.queryByText('Valid — the project configuration is ready.')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Go to plans' })).toBeNull()
    // The guided draft stays mounted for editing.
    expect(screen.getByText('Defaults')).toBeDefined()
  })

  it('marks the report stale during raw Advanced TOML edits and shows bounded guided updating', async () => {
    vi.mocked(api.getGlobalConfig).mockResolvedValue(configPayload('ready'))
    vi.mocked(api.postGlobalConfigForm).mockResolvedValue(
      formPayload({ validation: configPayload('ready').validation }),
    )
    renderEditor()
    await screen.findByText('Defaults')
    expect(screen.getByRole('button', { name: 'Go to plans' })).toBeDefined()

    vi.mocked(api.postGlobalConfigForm).mockImplementationOnce(() => new Promise(() => {}))
    fireEvent.click(screen.getByRole('button', { name: 'Advanced TOML' }))
    fireEvent.change(screen.getByLabelText('aflow.toml contents'), { target: { value: '# edited\n' } })
    // Until the reprojection succeeds the ready result is marked stale and the
    // shortcut is gone; the guided view hides stale choices behind a bounded
    // updating state instead of leaving them actionable.
    expect(await screen.findByText(/out of date for the edited draft/)).toBeDefined()
    expect(screen.queryByRole('button', { name: 'Go to plans' })).toBeNull()
    expect(screen.getByText('Updating guided settings…')).toBeDefined()
    expect(screen.queryByText('Defaults')).toBeNull()
  })

  it('never offers Go to plans for a dirty candidate; only a ready save navigates', async () => {
    const onReady = vi.fn()
    render(<ConfigEditor onDirtyChange={vi.fn()} onReady={onReady} />)
    await screen.findByText('Defaults')
    // The reprojection of the edited draft reports it ready and echoes the
    // edited pair, so the report is fresh — but the candidate is unsaved and
    // no path may bypass the dirty navigation guard.
    vi.mocked(api.postGlobalConfigForm).mockResolvedValueOnce(
      formPayload({ aflow_toml: '# candidate\n', validation: configPayload('ready').validation }),
    )
    fireEvent.click(screen.getByRole('button', { name: 'Advanced TOML' }))
    fireEvent.change(screen.getByLabelText('aflow.toml contents'), { target: { value: '# candidate\n' } })
    fireEvent.click(screen.getByRole('button', { name: 'Guided settings' }))
    expect(await screen.findByText('Valid — the shared configuration is ready.')).toBeDefined()
    expect(screen.queryByRole('button', { name: 'Go to plans' })).toBeNull()
    expect(screen.getByText('Defaults')).toBeDefined()
    expect((screen.getByLabelText('aflow.toml contents') as HTMLTextAreaElement).value).toBe('# candidate\n')

    // After the commit the projection of the saved pair stays ready too.
    vi.mocked(api.postGlobalConfigForm).mockResolvedValue(
      formPayload({ validation: configPayload('ready').validation }),
    )
    vi.mocked(api.saveGlobalConfig).mockResolvedValue(configPayload('ready', revisionB))
    fireEvent.click(screen.getByRole('button', { name: 'Save and continue to Plans' }))
    await waitFor(() => expect(onReady).toHaveBeenCalledTimes(1))
    expect(onReady).toHaveBeenCalledWith(expect.objectContaining({ revision: revisionB }))
    // After the clean commit the shortcut exists for the committed ready pair.
    expect(await screen.findByRole('button', { name: 'Go to plans' })).toBeDefined()
  })
})

describe('pending guided actions', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(api.getGlobalConfig).mockResolvedValue(configPayload('ready'))
    vi.mocked(api.postGlobalConfigForm).mockResolvedValue(formPayload({ validation: configPayload('ready').validation }))
  })

  it.each([false, true])('does not navigate or save ahead of a pending action (dirty=%s)', async (dirty) => {
    const onReady = vi.fn()
    const onDirtyChange = vi.fn()
    render(<ConfigEditor onDirtyChange={onDirtyChange} onReady={onReady} />)
    await screen.findByText('Defaults')
    if (dirty) {
      vi.mocked(api.postGlobalConfigForm).mockResolvedValueOnce(formPayload({
        changed: true, aflow_toml: '# first edit\n', validation: configPayload('ready').validation,
      }))
      fireEvent.change(screen.getByLabelText('Max turns'), { target: { value: '2' } })
      fireEvent.click(screen.getByRole('button', { name: 'Apply max turns' }))
      await waitFor(() => expect((screen.getByRole('button', { name: 'Save both files' }) as HTMLButtonElement).disabled).toBe(false))
    }
    let release!: (value: ReturnType<typeof formPayload>) => void
    vi.mocked(api.postGlobalConfigForm).mockImplementationOnce(() => new Promise((resolve) => { release = resolve }))
    fireEvent.change(screen.getByLabelText('Max turns'), { target: { value: '3' } })
    fireEvent.click(screen.getByRole('button', { name: 'Apply max turns' }))
    expect(onDirtyChange).toHaveBeenLastCalledWith(true)
    for (const label of ['Save both files', 'Save and continue to Plans']) {
      const button = screen.getByRole('button', { name: label }) as HTMLButtonElement
      expect(button.disabled).toBe(true)
      fireEvent.click(button)
    }
    if (!dirty) {
      const shortcut = screen.getByRole('button', { name: 'Go to plans' }) as HTMLButtonElement
      expect(shortcut.disabled).toBe(true)
      fireEvent.click(shortcut)
    }
    expect(onReady).not.toHaveBeenCalled()
    expect(api.saveGlobalConfig).not.toHaveBeenCalled()
    release(formPayload({ changed: true, aflow_toml: '# latest action\n', validation: configPayload('ready').validation }))
    await waitFor(() => expect((screen.getByRole('button', { name: 'Save and continue to Plans' }) as HTMLButtonElement).disabled).toBe(false))
    vi.mocked(api.saveGlobalConfig).mockResolvedValue({ ...configPayload('ready', revisionB), aflow_toml: '# latest action\n' })
    fireEvent.click(screen.getByRole('button', { name: 'Save and continue to Plans' }))
    await waitFor(() => expect(onReady).toHaveBeenCalledTimes(1))
    expect(api.saveGlobalConfig).toHaveBeenCalledWith(expect.objectContaining({ aflow_toml: '# latest action\n' }))
  })

  it('lets Advanced TOML save after a failed read-only projection', async () => {
    renderEditor()
    await screen.findByText('Defaults')
    vi.mocked(api.postGlobalConfigForm).mockRejectedValueOnce(new Error('projection unavailable'))
    fireEvent.click(screen.getByRole('button', { name: 'Advanced TOML' }))
    fireEvent.change(screen.getByLabelText('aflow.toml contents'), { target: { value: '# manual edit\n' } })
    await screen.findByText('projection unavailable')
    expect((screen.getByRole('button', { name: 'Save both files' }) as HTMLButtonElement).disabled).toBe(false)
  })
})
