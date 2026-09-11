import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api'
import * as api from '../api'
import type { PlanBackupPage, PlanBackupSummary, PlanDocument, ProjectInfo } from '../types'
import { PlanPanel } from './PlanPanel'

vi.mock('../api', async () => {
  const actual = await vi.importActual<typeof import('../api')>('../api')
  return {
    ...actual,
    listProjectPlans: vi.fn(),
    createProjectPlan: vi.fn(),
    readProjectPlan: vi.fn(),
    updateProjectPlan: vi.fn(),
    promoteProjectPlan: vi.fn(),
    listProjectPlanBackups: vi.fn(),
  }
})

const project: ProjectInfo = {
  id: 'alpha', display_name: 'Alpha Project', current_path: '/srv/code/alpha',
  is_git_root: true, registered_at: '2026-01-01T00:00:00Z', readiness: 'ready',
}

const todoPlan: PlanDocument = {
  project_id: 'alpha', name: 'plan-a.md', path: 'plans/todo/plan-a.md',
  status: 'todo', revision: 'a'.repeat(64), size_bytes: 10,
}
const inProgressPlan: PlanDocument = {
  project_id: 'alpha', name: 'plan-b.md', path: 'plans/in-progress/plan-b.md',
  status: 'in_progress', revision: 'b'.repeat(64), size_bytes: 12,
}

function mockList() {
  vi.mocked(api.listProjectPlans).mockResolvedValue([todoPlan, inProgressPlan])
}

async function openPlan(plan: PlanDocument, content: string) {
  vi.mocked(api.readProjectPlan).mockResolvedValue({ ...plan, content })
  fireEvent.click(await screen.findByRole('button', { name: new RegExp(plan.name) }))
  await screen.findByLabelText('Plan content')
}

function backupSummary(filename: string, overrides: Partial<PlanBackupSummary> = {}): PlanBackupSummary {
  return {
    backup_filename: filename,
    kind: 'snapshot',
    baseline_status: 'unknown',
    capture_event: 'startup_preparation',
    timestamp: '2026-09-11T10:00:00Z',
    run_id: null,
    turn_number: null,
    content_sha256: `${filename}-hash`,
    ...overrides,
  }
}

function backupPage(
  backups: PlanBackupSummary[],
  options: Partial<PlanBackupPage> = {},
): PlanBackupPage {
  return {
    backups,
    offset: 0,
    limit: 50,
    next_offset: null,
    total_items: backups.length,
    ...options,
  }
}

describe('PlanPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockList()
  })

  it('groups contained plans by lifecycle status and reads the selected plan', async () => {
    render(<PlanPanel project={project} onDirtyChange={vi.fn()} onOpenRunDashboard={vi.fn()} />)
    await screen.findByText('Draft (todo)')
    expect(screen.getByText('Ready (in progress)')).toBeDefined()
    expect(screen.getByText('Done')).toBeDefined()
    await openPlan(todoPlan, '# Plan A\n')
    expect(api.readProjectPlan).toHaveBeenCalledWith('alpha', 'todo', 'plan-a.md')
    expect((screen.getByLabelText('Plan content') as HTMLTextAreaElement).value).toBe('# Plan A\n')
    expect(screen.getByText(todoPlan.path)).toBeDefined()
  })

  it('explains the lifecycle and offers Run this plan only for a saved Ready plan', async () => {
    const onOpenRunDashboard = vi.fn()
    const donePlan: PlanDocument = {
      project_id: 'alpha', name: 'plan-c.md', path: 'plans/done/plan-c.md',
      status: 'done', revision: 'c'.repeat(64), size_bytes: 14,
    }
    vi.mocked(api.listProjectPlans).mockResolvedValue([todoPlan, inProgressPlan, donePlan])
    render(<PlanPanel project={project} onDirtyChange={vi.fn()} onOpenRunDashboard={onOpenRunDashboard} />)

    await screen.findByText('Draft (todo)')
    expect(screen.getByText(/A draft is not runnable yet/)).toBeDefined()
    expect(screen.getByText(/Runnable plans/)).toBeDefined()
    expect(screen.getByText(/kept for the record/)).toBeDefined()

    // A saved Ready plan offers the exact relative path handoff.
    await openPlan(inProgressPlan, '# Ready plan\n')
    expect(screen.getByText(/startup checks run when you start the plan/)).toBeDefined()
    expect(screen.queryByText('Ready — runnable')).toBeNull()
    const run = screen.getByRole('button', { name: 'Run this plan' })
    expect(run.getAttribute('disabled')).toBeNull()
    fireEvent.click(run)
    expect(onOpenRunDashboard).toHaveBeenCalledWith('plans/in-progress/plan-b.md')

    // A dirty Ready draft must be saved before it can run.
    fireEvent.change(screen.getByLabelText('Plan content'), { target: { value: '# Unsaved\n' } })
    expect(screen.getByRole('button', { name: 'Run this plan' }).getAttribute('disabled')).not.toBeNull()
    expect(screen.getByText(/Save this draft before running the plan/)).toBeDefined()
    fireEvent.click(screen.getByRole('button', { name: '← Back to Plans' }))
    expect(screen.getByRole('alertdialog', { name: 'Unsaved plan edits' })).toBeDefined()
    fireEvent.click(screen.getByRole('button', { name: 'Discard edits' }))

    // Drafts explain how to become runnable instead of offering a no-op.
    await openPlan(todoPlan, '# Draft plan\n')
    expect(screen.queryByRole('button', { name: 'Run this plan' })).toBeNull()
    expect(screen.getByText(/not runnable yet\. Save it and move it to Ready/)).toBeDefined()

    // Done plans explain that they are archival.
    fireEvent.click(screen.getByRole('button', { name: '← Back to Plans' }))
    await openPlan(donePlan, '# Done plan\n')
    expect(screen.queryByRole('button', { name: 'Run this plan' })).toBeNull()
    expect(screen.getByText(/kept for the record and cannot run/)).toBeDefined()
  })

  it('creates a plan in the todo lifecycle and opens it', async () => {
    const template = '# Plan\n\n## Summary\n\nDescribe the desired behavior.\n\n## Git Tracking\n\n- Plan Branch: ``\n- Pre-Handoff Base HEAD: ``\n\n### [ ] Checkpoint 1: Describe the first deliverable\n'
    const created: PlanDocument = { ...todoPlan, size_bytes: 8 }
    vi.mocked(api.createProjectPlan).mockResolvedValue(created)
    vi.mocked(api.readProjectPlan).mockResolvedValue({ ...created, content: template })
    render(<PlanPanel project={project} onDirtyChange={vi.fn()} onOpenRunDashboard={vi.fn()} />)
    fireEvent.change(screen.getByLabelText('New plan filename'), { target: { value: 'plan-a.md' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create plan' }))

    await waitFor(() => expect(api.createProjectPlan).toHaveBeenCalledWith('alpha', {
      name: 'plan-a.md',
    }))
    await screen.findByLabelText('Plan content')
    expect((screen.getByLabelText('Plan content') as HTMLTextAreaElement).value).toBe(template)
    expect((screen.getByLabelText('Plan content') as HTMLTextAreaElement).value).toContain('### [ ] Checkpoint 1:')
    await waitFor(() => expect(api.listProjectPlans).toHaveBeenCalledTimes(2))
  })

  it('saves edits with the expected revision and refreshes lifecycle status', async () => {
    const updated: PlanDocument = { ...todoPlan, revision: 'c'.repeat(64) }
    vi.mocked(api.updateProjectPlan).mockResolvedValue(updated)
    render(<PlanPanel project={project} onDirtyChange={vi.fn()} onOpenRunDashboard={vi.fn()} />)
    await openPlan(todoPlan, '# Original\n')
    fireEvent.change(screen.getByLabelText('Plan content'), { target: { value: '# Edited\n' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(api.updateProjectPlan).toHaveBeenCalledWith('alpha', 'todo', 'plan-a.md', {
      content: '# Edited\n',
      expected_revision: todoPlan.revision,
    }))
    await screen.findByText(new RegExp('c'.repeat(12)))
    await waitFor(() => expect(api.listProjectPlans).toHaveBeenCalledTimes(2))
  })

  it('preserves local edits on a revision conflict and reloads only after confirmation', async () => {
    vi.mocked(api.updateProjectPlan).mockRejectedValue(
      new ApiError(409, 'conflict', 'revision_conflict', { current_revision: 'd'.repeat(64) }),
    )
    render(<PlanPanel project={project} onDirtyChange={vi.fn()} onOpenRunDashboard={vi.fn()} />)
    await openPlan(todoPlan, '# Original\n')
    fireEvent.change(screen.getByLabelText('Plan content'), { target: { value: '# Mine\n' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await screen.findByText(/plan changed on the server/)
    expect((screen.getByLabelText('Plan content') as HTMLTextAreaElement).value).toBe('# Mine\n')

    vi.mocked(api.readProjectPlan).mockResolvedValue({ ...todoPlan, content: '# Server copy\n' })
    fireEvent.click(screen.getByRole('button', { name: 'Reload from server…' }))
    expect(screen.getByText(/Discard the local edits and load the server copy/)).toBeDefined()
    fireEvent.click(screen.getByRole('button', { name: 'Discard edits and reload' }))
    await waitFor(() => expect((screen.getByLabelText('Plan content') as HTMLTextAreaElement).value).toBe('# Server copy\n'))
    expect(screen.queryByText(/plan changed on the server/)).toBeNull()
  })

  it('keeps the draft text when the network fails during save', async () => {
    vi.mocked(api.updateProjectPlan).mockRejectedValue(new Error('connection lost'))
    render(<PlanPanel project={project} onDirtyChange={vi.fn()} onOpenRunDashboard={vi.fn()} />)
    await openPlan(todoPlan, '# Original\n')
    fireEvent.change(screen.getByLabelText('Plan content'), { target: { value: '# Unsent\n' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await screen.findByText(/connection lost/)
    expect((screen.getByLabelText('Plan content') as HTMLTextAreaElement).value).toBe('# Unsent\n')
  })

  it('requires an explicit discard before returning to all plans with a dirty draft', async () => {
    render(<PlanPanel project={project} onDirtyChange={vi.fn()} onOpenRunDashboard={vi.fn()} />)
    await openPlan(todoPlan, '# Original\n')
    fireEvent.change(screen.getByLabelText('Plan content'), { target: { value: '# Draft\n' } })
    fireEvent.click(screen.getByRole('button', { name: '← Back to Plans' }))

    expect(screen.getByRole('alertdialog', { name: 'Unsaved plan edits' })).toBeDefined()
    expect((screen.getByLabelText('Plan content') as HTMLTextAreaElement).value).toBe('# Draft\n')
    fireEvent.click(screen.getByRole('button', { name: 'Discard edits' }))
    await screen.findByLabelText('New plan filename')
  })

  it('does not promote a plan while its current draft is unsaved', async () => {
    render(<PlanPanel project={project} onDirtyChange={vi.fn()} onOpenRunDashboard={vi.fn()} />)
    await openPlan(todoPlan, '# Original\n')
    fireEvent.change(screen.getByLabelText('Plan content'), { target: { value: '# Draft\n' } })

    const promote = screen.getByRole('button', { name: 'Move to Ready' })
    expect(promote).toHaveProperty('disabled', true)
    fireEvent.click(promote)
    expect(api.promoteProjectPlan).not.toHaveBeenCalled()
    expect(screen.getByText(/Save this draft before moving it/)).toBeDefined()
  })

  it('promotes a todo plan through the lifecycle with its current revision', async () => {
    const promoted: PlanDocument = { ...todoPlan, status: 'in_progress', path: 'plans/in-progress/plan-a.md' }
    vi.mocked(api.promoteProjectPlan).mockResolvedValue(promoted)
    render(<PlanPanel project={project} onDirtyChange={vi.fn()} onOpenRunDashboard={vi.fn()} />)
    await openPlan(todoPlan, '# Plan A\n')
    fireEvent.click(screen.getByRole('button', { name: 'Move to Ready' }))

    await waitFor(() => expect(api.promoteProjectPlan).toHaveBeenCalledWith(
      'alpha', 'todo', 'plan-a.md', { expected_revision: todoPlan.revision },
    ))
    await screen.findByText(promoted.path)
    await waitFor(() => expect(api.listProjectPlans).toHaveBeenCalledTimes(2))
  })

  it('loads readable backup labels without changing an unsaved draft', async () => {
    const baseline = backupSummary('plan-a.md', {
      baseline_status: 'known',
      capture_event: 'ready_promotion',
    })
    const snapshot = backupSummary('plan-a.md.1', {
      capture_event: 'startup_preparation',
    })
    const followUp = backupSummary('plan-a.md.2', {
      kind: 'follow_up',
      capture_event: 'before_followup_turn',
      run_id: 'run-7',
      turn_number: 3,
    })
    const unknown = backupSummary('legacy.md', {
      kind: 'unclassified',
      baseline_status: 'unknown',
      capture_event: null,
      timestamp: null,
    })
    vi.mocked(api.listProjectPlanBackups).mockResolvedValue(
      backupPage([baseline, snapshot, followUp, unknown]),
    )
    render(<PlanPanel project={project} onDirtyChange={vi.fn()} onOpenRunDashboard={vi.fn()} />)
    await openPlan(todoPlan, '# Original\n')
    const editor = screen.getByLabelText('Plan content') as HTMLTextAreaElement
    fireEvent.change(editor, { target: { value: '# Keep my draft\n' } })

    const summary = screen.getByText('Backup history', { exact: true })
    expect(summary.closest('details')?.hasAttribute('open')).toBe(false)
    fireEvent.click(summary)
    await screen.findByText('Baseline', { exact: true })

    expect(api.listProjectPlanBackups).toHaveBeenCalledWith('alpha', 'todo', 'plan-a.md', {
      offset: 0,
      limit: 50,
    })
    expect(screen.getByText('Initial Ready baseline', { exact: true })).toBeDefined()
    expect(screen.getByText('Snapshot', { exact: true })).toBeDefined()
    expect(screen.getByText('Follow-up', { exact: true })).toBeDefined()
    expect(screen.getByText('Unknown origin', { exact: true })).toBeDefined()
    expect(screen.getByText('Captured plan snapshot', { exact: true })).toBeDefined()
    expect(screen.queryByText('Later snapshot; original baseline is unknown', { exact: true })).toBeNull()
    expect(screen.getByText('run-7', { exact: true })).toBeDefined()
    expect(editor.value).toBe('# Keep my draft\n')
    expect(screen.queryByRole('button', { name: /reset/i })).toBeNull()
  })

  it('paginates backup history and keeps the editor draft intact', async () => {
    const baseline = backupSummary('plan-a.md', { baseline_status: 'known', capture_event: 'ready_promotion' })
    const followUp = backupSummary('plan-a.md.1', { kind: 'follow_up' })
    vi.mocked(api.listProjectPlanBackups)
      .mockResolvedValueOnce(backupPage([baseline], { next_offset: 1, total_items: 2 }))
      .mockResolvedValueOnce(backupPage([followUp], { offset: 1, total_items: 2 }))
    render(<PlanPanel project={project} onDirtyChange={vi.fn()} onOpenRunDashboard={vi.fn()} />)
    await openPlan(todoPlan, '# Original\n')
    const editor = screen.getByLabelText('Plan content') as HTMLTextAreaElement
    fireEvent.change(editor, { target: { value: '# Preserve through pages\n' } })
    fireEvent.click(screen.getByText('Backup history', { exact: true }))
    await screen.findByText('Baseline', { exact: true })

    fireEvent.click(screen.getByRole('button', { name: 'Next' }))
    await screen.findByText('Follow-up', { exact: true })
    expect(screen.getByRole('button', { name: 'Previous' })).toBeDefined()
    expect(screen.getByText('Showing 2–2 of 2', { exact: true })).toBeDefined()
    expect(api.listProjectPlanBackups).toHaveBeenLastCalledWith('alpha', 'todo', 'plan-a.md', {
      offset: 1,
      limit: 50,
    })
    expect(editor.value).toBe('# Preserve through pages\n')
  })

  it('reports unavailable history and ignores stale source-plan responses', async () => {
    vi.mocked(api.listProjectPlanBackups).mockRejectedValueOnce(new Error('history service unavailable'))
    render(<PlanPanel project={project} onDirtyChange={vi.fn()} onOpenRunDashboard={vi.fn()} />)
    await openPlan(todoPlan, '# Draft\n')
    fireEvent.click(screen.getByText('Backup history', { exact: true }))
    await screen.findByText('Backup history unavailable: history service unavailable', { exact: true })
    expect(screen.queryByRole('button', { name: /reset/i })).toBeNull()

    let resolveStale!: (page: PlanBackupPage) => void
    vi.mocked(api.listProjectPlanBackups).mockImplementationOnce(() => new Promise((resolve) => {
      resolveStale = resolve
    }))
    fireEvent.click(screen.getByRole('button', { name: '← Back to Plans', exact: true }))
    await openPlan(inProgressPlan, '# Ready\n')
    fireEvent.click(screen.getByText('Backup history', { exact: true }))
    await waitFor(() => expect(api.listProjectPlanBackups).toHaveBeenLastCalledWith('alpha', 'in_progress', 'plan-b.md', {
      offset: 0,
      limit: 50,
    }))

    fireEvent.click(screen.getByRole('button', { name: '← Back to Plans', exact: true }))
    await openPlan(todoPlan, '# Fresh source\n')
    resolveStale(backupPage([backupSummary('stale.md', { baseline_status: 'known' })]))
    await waitFor(() => expect((screen.getByLabelText('Plan content') as HTMLTextAreaElement).value).toBe('# Fresh source\n'))
    expect(screen.queryByText('stale.md', { exact: true })).toBeNull()
    expect(screen.getByText('Backup history', { exact: true }).closest('details')?.hasAttribute('open')).toBe(false)
  })
})
