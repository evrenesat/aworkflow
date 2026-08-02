import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { PlanPanel } from './PlanPanel'
import { SessionPanel } from './SessionPanel'

vi.mock('../api', () => ({
  listProjectPlans: vi.fn(), listPlanDrafts: vi.fn(), loadPlanDraft: vi.fn(),
  promotePlanDraft: vi.fn(), deletePlanDraft: vi.fn(),
  listProjectSessions: vi.fn(), getProjectSession: vi.fn(), startProjectSession: vi.fn(),
  resumeProjectSession: vi.fn(), forkProjectSession: vi.fn(), setProjectSessionArchived: vi.fn(),
  startProjectTurn: vi.fn(), interruptProjectTurn: vi.fn(),
  listPendingApprovals: vi.fn(), respondToApproval: vi.fn(),
  listAttachments: vi.fn(), uploadAttachment: vi.fn(), deleteAttachment: vi.fn(),
}))

const project = {
  id: 'project-1', display_name: 'Alpha Project', current_path: '/workspace/alpha',
  historical_aliases: [], detection_source: 'local_git_root', linked_session_count: 2,
  is_git_root: true, registered_at: '2024-01-01T00:00:00Z',
}

const capabilities = {
  models: ['gpt-5', 'gpt-5-mini'], reasoning_levels: ['low', 'high'],
  reasoning_summaries: ['concise', 'detailed'], attachments: true,
  attachment_kinds: ['file', 'image'] as const, output_schema: false, fork: true,
  archive: true, approvals: true, interruption: true, compaction: false, rollback: false,
}

const providers = [
  { provider_id: 'codex', display_name: 'Codex', state: 'ready' as const, capabilities, error: null },
  { provider_id: 'alternate', display_name: 'Alternate', state: 'ready' as const, capabilities: { ...capabilities, attachments: false, attachment_kinds: [] }, error: null },
]

const planMarkdown = '# Session Plan\n\n## Summary\nShip the update.'

const codexSession = {
  key: { provider_id: 'codex', provider_session_id: 'shared-id' }, project_id: 'project-1',
  cwd: '/workspace/alpha', title: 'Codex planning', preview: '', status: 'idle' as const,
  model: 'retired-model', reasoning_level: 'low', archived: false,
  created_at: '2024-01-01T00:00:00Z', updated_at: '2024-01-02T00:00:00Z',
  turns: [{ turn_id: 'turn-1', status: 'completed' as const, items: [{ type: 'text', text: planMarkdown }], error: null, created_at: null, completed_at: null, attachment_ids: [] }],
}

const alternateSession = {
  ...codexSession,
  key: { provider_id: 'alternate', provider_session_id: 'shared-id' },
  title: 'Alternate planning', model: 'gpt-5', updated_at: '2024-01-01T00:00:00Z', turns: [],
}

const archivedCodexSession = {
  ...codexSession,
  title: 'Archived Codex planning', status: 'archived' as const, archived: true,
}

const archivedAlternateSession = {
  ...alternateSession,
  title: 'Archived alternate planning', status: 'archived' as const, archived: true,
}

describe('PlanPanel and SessionPanel interactions', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { configurable: true, value: vi.fn() })
    vi.mocked(api.listAttachments).mockResolvedValue([])
    vi.mocked(api.listPendingApprovals).mockResolvedValue([])
  })

  it('renders draft content read-only while keeping the promote target editable', async () => {
    vi.mocked(api.listProjectPlans).mockResolvedValue([])
    vi.mocked(api.listPlanDrafts).mockResolvedValue(['draft-1'])
    vi.mocked(api.loadPlanDraft).mockResolvedValue({ name: 'draft-1', content: planMarkdown })
    const { container } = render(<PlanPanel project={project} onStartExecution={vi.fn()} />)
    await waitFor(() => expect(screen.getByText('draft-1')).toBeDefined())
    fireEvent.click(screen.getByText('draft-1'))
    await waitFor(() => expect(screen.getByText('Draft content')).toBeDefined())
    expect(container.querySelectorAll('textarea')).toHaveLength(0)
    expect(screen.getByDisplayValue('draft-1')).toBeDefined()
  })

  it('keeps identical provider-local ids independently selectable', async () => {
    vi.mocked(api.listProjectSessions).mockResolvedValue({ sessions: [codexSession, alternateSession], providers, next_cursor: null } as never)
    vi.mocked(api.getProjectSession).mockImplementation(async (_projectId, key) =>
      (key.provider_id === 'codex' ? codexSession : alternateSession) as never
    )
    render(<SessionPanel project={project} onSavePlanDraft={vi.fn()} />)

    await waitFor(() => expect(screen.getByText('Codex planning')).toBeDefined())
    fireEvent.click(screen.getByText('Alternate planning'))
    await waitFor(() => expect(api.getProjectSession).toHaveBeenCalledWith('project-1', alternateSession.key))
    expect(screen.getByText('No turn history loaded for this session.')).toBeDefined()
  })

  it('requests active sessions initially and keeps provider-qualified ids selectable in archived mode', async () => {
    vi.mocked(api.listProjectSessions).mockImplementation(async (_projectId, request) => ({
      sessions: request?.archived ? [archivedCodexSession, archivedAlternateSession] : [codexSession, alternateSession],
      providers,
      next_cursor: null,
    } as never))
    vi.mocked(api.getProjectSession).mockImplementation(async (_projectId, key) => {
      if (key.provider_id === 'alternate') return archivedAlternateSession as never
      return archivedCodexSession as never
    })
    render(<SessionPanel project={project} onSavePlanDraft={vi.fn()} />)

    await waitFor(() => expect(api.listProjectSessions).toHaveBeenCalledWith('project-1', { archived: false }))
    fireEvent.click(screen.getByRole('button', { name: 'Archived' }))
    await waitFor(() => expect(api.listProjectSessions).toHaveBeenCalledWith('project-1', { archived: true }))
    await waitFor(() => expect(screen.getByText('Archived alternate planning')).toBeDefined())
    fireEvent.click(screen.getByText('Archived alternate planning'))
    await waitFor(() => expect(api.getProjectSession).toHaveBeenCalledWith('project-1', archivedAlternateSession.key))
    expect(screen.getByText('No turn history loaded for this session.')).toBeDefined()
  })

  it('archives from the active view and removes stale selected detail', async () => {
    vi.mocked(api.listProjectSessions)
      .mockResolvedValueOnce({ sessions: [codexSession], providers, next_cursor: null } as never)
      .mockResolvedValueOnce({ sessions: [], providers, next_cursor: null } as never)
    vi.mocked(api.getProjectSession).mockResolvedValue(codexSession as never)
    vi.mocked(api.setProjectSessionArchived).mockResolvedValue({ archived: true })
    render(<SessionPanel project={project} onSavePlanDraft={vi.fn()} />)

    await waitFor(() => expect(screen.getByRole('button', { name: 'Archive' })).toBeDefined())
    fireEvent.click(screen.getByRole('button', { name: 'Archive' }))
    await waitFor(() => expect(api.setProjectSessionArchived).toHaveBeenCalledWith('project-1', codexSession.key, true))
    await waitFor(() => expect(api.listProjectSessions).toHaveBeenLastCalledWith('project-1', { archived: false }))
    await waitFor(() => expect(screen.queryByText('Codex planning')).toBeNull())
    expect(screen.getByText('Select a planning session or start a new one.')).toBeDefined()
  })

  it('unarchives from the archived view and removes stale selected detail', async () => {
    vi.mocked(api.listProjectSessions).mockImplementation(async (_projectId, request) => ({
      sessions: request?.archived && vi.mocked(api.setProjectSessionArchived).mock.calls.length === 0 ? [archivedCodexSession] : [],
      providers,
      next_cursor: null,
    } as never))
    vi.mocked(api.getProjectSession).mockResolvedValue(archivedCodexSession as never)
    vi.mocked(api.setProjectSessionArchived).mockResolvedValue({ archived: false })
    render(<SessionPanel project={project} onSavePlanDraft={vi.fn()} />)

    await waitFor(() => expect(screen.getByRole('button', { name: 'Active' })).toBeDefined())
    fireEvent.click(screen.getByRole('button', { name: 'Archived' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Unarchive' })).toBeDefined())
    fireEvent.click(screen.getByRole('button', { name: 'Unarchive' }))
    await waitFor(() => expect(api.setProjectSessionArchived).toHaveBeenCalledWith('project-1', archivedCodexSession.key, false))
    await waitFor(() => expect(api.listProjectSessions).toHaveBeenLastCalledWith('project-1', { archived: true }))
    await waitFor(() => expect(screen.queryByText('Archived Codex planning')).toBeNull())
    expect(screen.getByText('Select a planning session or start a new one.')).toBeDefined()
  })

  it('starts a session with provider-advertised selections', async () => {
    vi.mocked(api.listProjectSessions).mockResolvedValue({ sessions: [], providers, next_cursor: null } as never)
    vi.mocked(api.startProjectSession).mockResolvedValue(alternateSession as never)
    render(<SessionPanel project={project} onSavePlanDraft={vi.fn()} />)
    await waitFor(() => expect(screen.getByRole('button', { name: 'New session' })).toBeDefined())
    fireEvent.change(screen.getByLabelText('Provider'), { target: { value: 'alternate' } })
    fireEvent.change(screen.getByLabelText('New session model'), { target: { value: 'gpt-5' } })
    fireEvent.change(screen.getByLabelText('New session reasoning level'), { target: { value: 'high' } })
    fireEvent.click(screen.getByRole('button', { name: 'New session' }))
    await waitFor(() => expect(api.startProjectSession).toHaveBeenCalledWith('project-1', {
      provider_id: 'alternate', model: 'gpt-5', reasoning_level: 'high',
    }))
  })

  it('shows capability-aware controls, preserves a historical model, and sends uploaded attachment ids', async () => {
    vi.mocked(api.listProjectSessions).mockResolvedValue({ sessions: [codexSession], providers, next_cursor: null } as never)
    vi.mocked(api.getProjectSession).mockResolvedValue(codexSession as never)
    vi.mocked(api.uploadAttachment).mockResolvedValue({
      attachment_id: 'attachment-1', filename: 'notes.txt', kind: 'file', media_type: 'text/plain', size_bytes: 5, created_at: null,
    })
    vi.mocked(api.startProjectTurn).mockResolvedValue({
      turn_id: 'turn-2', status: 'completed', items: [{ type: 'text', text: 'Done' }], error: null,
      created_at: null, completed_at: null, attachment_ids: ['attachment-1'],
    } as never)
    render(<SessionPanel project={project} onSavePlanDraft={vi.fn()} />)

    await waitFor(() => expect(screen.getByPlaceholderText('Send a new turn to this session')).toBeDefined())
    expect(screen.getByRole('option', { name: 'retired-model (current, unavailable)' })).toBeDefined()
    expect(screen.getByRole('button', { name: 'Archive' })).toBeDefined()

    const file = new File(['notes'], 'notes.txt', { type: 'text/plain' })
    fireEvent.change(screen.getByLabelText('Add attachments'), { target: { files: [file] } })
    await waitFor(() => expect(api.uploadAttachment).toHaveBeenCalledWith('project-1', codexSession.key, file, 'file'))
    fireEvent.change(screen.getByLabelText('Model'), { target: { value: 'gpt-5' } })
    fireEvent.change(screen.getByLabelText('Reasoning level'), { target: { value: 'high' } })
    fireEvent.change(screen.getByLabelText('Reasoning summary'), { target: { value: 'concise' } })
    fireEvent.change(screen.getByPlaceholderText('Send a new turn to this session'), { target: { value: 'Continue' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send turn' }))

    await waitFor(() => expect(api.startProjectTurn).toHaveBeenCalledWith('project-1', codexSession.key, {
      text: 'Continue', attachment_ids: ['attachment-1'], model: 'gpt-5', reasoning_level: 'high', reasoning_summary: 'concise',
    }))
  })

  it('does not silently submit a failed attachment upload', async () => {
    vi.mocked(api.listProjectSessions).mockResolvedValue({ sessions: [codexSession], providers, next_cursor: null } as never)
    vi.mocked(api.getProjectSession).mockResolvedValue(codexSession as never)
    vi.mocked(api.uploadAttachment).mockRejectedValue(new Error('Upload rejected'))
    render(<SessionPanel project={project} onSavePlanDraft={vi.fn()} />)
    await waitFor(() => expect(screen.getByLabelText('Add attachments')).toBeDefined())
    fireEvent.change(screen.getByLabelText('Add attachments'), { target: { files: [new File(['x'], 'bad.txt')] } })
    await waitFor(() => expect(screen.getByText(/bad.txt: failed/)).toBeDefined())
    fireEvent.change(screen.getByPlaceholderText('Send a new turn to this session'), { target: { value: 'Continue' } })
    expect(screen.getByRole('button', { name: 'Send turn' }).getAttribute('disabled')).not.toBeNull()
    expect(api.startProjectTurn).not.toHaveBeenCalled()
  })

  it('rejects a non-image file before upload for an image-only provider', async () => {
    const imageOnlyProviders = [{
      ...providers[0],
      capabilities: { ...capabilities, attachment_kinds: ['image'] as const },
    }]
    vi.mocked(api.listProjectSessions).mockResolvedValue({ sessions: [codexSession], providers: imageOnlyProviders, next_cursor: null } as never)
    vi.mocked(api.getProjectSession).mockResolvedValue(codexSession as never)
    render(<SessionPanel project={project} onSavePlanDraft={vi.fn()} />)

    await waitFor(() => expect(screen.getByLabelText('Add attachments')).toBeDefined())
    const file = new File(['notes'], 'notes.txt', { type: 'text/plain' })
    fireEvent.change(screen.getByLabelText('Add attachments'), { target: { files: [file] } })
    await waitFor(() => expect(screen.getByText(/notes.txt: failed — This provider does not support file attachments/)).toBeDefined())
    expect(api.uploadAttachment).not.toHaveBeenCalled()
    fireEvent.change(screen.getByPlaceholderText('Send a new turn to this session'), { target: { value: 'Continue' } })
    expect(screen.getByRole('button', { name: 'Send turn' }).getAttribute('disabled')).not.toBeNull()
    expect(api.startProjectTurn).not.toHaveBeenCalled()
  })

  it('does not expose an upload control when no attachment kinds are advertised', async () => {
    const noKindProviders = [{
      ...providers[0],
      capabilities: { ...capabilities, attachment_kinds: [] },
    }]
    vi.mocked(api.listProjectSessions).mockResolvedValue({ sessions: [codexSession], providers: noKindProviders, next_cursor: null } as never)
    vi.mocked(api.getProjectSession).mockResolvedValue(codexSession as never)
    render(<SessionPanel project={project} onSavePlanDraft={vi.fn()} />)

    await waitFor(() => expect(screen.getByText('This provider does not advertise any supported attachment kinds.')).toBeDefined())
    expect(screen.queryByLabelText('Add attachments')).toBeNull()
    expect(api.uploadAttachment).not.toHaveBeenCalled()
  })

  it('exposes pending approval decisions only for an advertising provider', async () => {
    const approval = {
      approval_id: 'approval-1', key: codexSession.key, turn_id: 'turn-2',
      kind: 'command' as const, reason: 'Run the project tests',
    }
    vi.mocked(api.listProjectSessions).mockResolvedValue({ sessions: [codexSession], providers, next_cursor: null } as never)
    vi.mocked(api.getProjectSession).mockResolvedValue(codexSession as never)
    vi.mocked(api.listPendingApprovals).mockResolvedValueOnce([approval]).mockResolvedValueOnce([])
    vi.mocked(api.respondToApproval).mockResolvedValue(undefined)
    render(<SessionPanel project={project} onSavePlanDraft={vi.fn()} />)

    await waitFor(() => expect(screen.getByText(/Run the project tests/)).toBeDefined())
    fireEvent.click(screen.getByRole('button', { name: 'Accept' }))
    await waitFor(() => expect(api.respondToApproval).toHaveBeenCalledWith(
      'project-1', codexSession.key, 'approval-1', 'accept'
    ))
  })
})
