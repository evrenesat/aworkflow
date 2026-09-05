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
    getProjectConfig: vi.fn(),
    saveProjectConfig: vi.fn(),
    validateProjectConfig: vi.fn(),
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

function renderEditor() {
  return render(
    <ConfigEditor project={project} onDirtyChange={vi.fn()} onReady={vi.fn()} />,
  )
}

describe('ConfigEditor', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(api.getProjectConfig).mockResolvedValue(configPayload())
  })

  it('loads both documents under one shared revision with accessible tabs', async () => {
    renderEditor()
    await screen.findByLabelText('aflow.toml contents')
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

  it('shows a load error with retry instead of inventing configuration text', async () => {
    vi.mocked(api.getProjectConfig).mockRejectedValue(new Error('operation_rejected'))
    renderEditor()
    await screen.findByText(/operation_rejected/)
    expect(screen.queryByLabelText('aflow.toml contents')).toBeNull()
    vi.mocked(api.getProjectConfig).mockResolvedValue(configPayload())
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await screen.findByLabelText('aflow.toml contents')
  })

  it('validates the candidate pair without saving and renders bounded diagnostics', async () => {
    vi.mocked(api.validateProjectConfig).mockResolvedValue(configPayload('invalid').validation)
    renderEditor()
    await screen.findByLabelText('aflow.toml contents')
    fireEvent.change(screen.getByLabelText('aflow.toml contents'), { target: { value: '# broken\n' } })
    fireEvent.click(screen.getByRole('button', { name: 'Validate' }))

    await waitFor(() => expect(api.validateProjectConfig).toHaveBeenCalledWith('beta', {
      aflow_toml: '# broken\n',
      workflows_toml: '# workflows\n',
    }))
    await screen.findByText(/Invalid configuration/)
    expect(screen.getByText(/unknown team reference/)).toBeDefined()
    expect(screen.getByText(/workflows\.toml:3/)).toBeDefined()
    expect(api.saveProjectConfig).not.toHaveBeenCalled()
  })

  it('saves both files atomically with the expected revision and refreshes the snapshot', async () => {
    vi.mocked(api.saveProjectConfig).mockResolvedValue(configPayload('ready', revisionB))
    const onReady = vi.fn()
    const onDirty = vi.fn()
    render(<ConfigEditor project={project} onDirtyChange={onDirty} onReady={onReady} />)
    await screen.findByLabelText('aflow.toml contents')
    fireEvent.change(screen.getByLabelText('aflow.toml contents'), { target: { value: '# configured\n' } })
    await waitFor(() => expect(onDirty).toHaveBeenCalledWith(true))
    fireEvent.click(screen.getByRole('button', { name: 'Save both files' }))

    await waitFor(() => expect(api.saveProjectConfig).toHaveBeenCalledWith('beta', {
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
    vi.mocked(api.saveProjectConfig).mockRejectedValue(
      new ApiError(409, 'conflict', 'revision_conflict', { current_revision: revisionB }),
    )
    renderEditor()
    await screen.findByLabelText('aflow.toml contents')
    fireEvent.change(screen.getByLabelText('aflow.toml contents'), { target: { value: '# mine\n' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save both files' }))

    await screen.findByText(/changed on the server/)
    expect(screen.getByText(new RegExp(revisionB.slice(0, 12)))).toBeDefined()
    expect((screen.getByLabelText('aflow.toml contents') as HTMLTextAreaElement).value).toBe('# mine\n')

    vi.mocked(api.getProjectConfig).mockResolvedValue(configPayload('ready', revisionB))
    fireEvent.click(screen.getByRole('button', { name: 'Reload server copy…' }))
    expect(screen.getByText(/Discard the local edits in both tabs/)).toBeDefined()
    fireEvent.click(screen.getByRole('button', { name: 'Discard edits and reload' }))
    await waitFor(() => expect(api.getProjectConfig).toHaveBeenCalledTimes(2))
    await screen.findByText(`Revision ${revisionB.slice(0, 12)}`)
    expect((screen.getByLabelText('aflow.toml contents') as HTMLTextAreaElement).value).toBe('# aflow config\n')
    expect(screen.queryByText(/changed on the server/)).toBeNull()
  })

  it('lists active-run blockers and keeps the draft editable when saving is blocked', async () => {
    vi.mocked(api.saveProjectConfig).mockRejectedValue(
      new ApiError(409, 'blocked', 'config_save_blocked', {
        blocking_runs: [{ run_id: 'run-9', status: 'running' }],
      }),
    )
    renderEditor()
    await screen.findByLabelText('aflow.toml contents')
    fireEvent.change(screen.getByLabelText('workflows.toml contents'), { target: { value: '# tuned\n' } })
    fireEvent.click(screen.getByRole('tab', { name: 'workflows.toml' }))
    fireEvent.click(screen.getByRole('button', { name: 'Save both files' }))

    await screen.findByText(/Saving is blocked while these runs/)
    expect(screen.getByText('run-9 — running')).toBeDefined()
    expect(screen.getByText(/future runs only/)).toBeDefined()
    expect((screen.getByLabelText('workflows.toml contents') as HTMLTextAreaElement).value).toBe('# tuned\n')
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
})
