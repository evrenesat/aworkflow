import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api'
import * as api from '../api'
import type { PlanBackupPage, PlanBackupSummary, PlanDocument, ProjectInfo, ProjectQueue } from '../types'
import { PlanPanel } from './PlanPanel'
import { HeaderSlotsProvider } from './HeaderSlots'

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
    getProjectQueue: vi.fn(),
    requeueProjectPlan: vi.fn(),
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

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: Error) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

function hosted(projectInfo: ProjectInfo) {
  return <HeaderSlotsProvider renderHeader={slots => <div>{slots.local}{slots.primary}{slots.more}</div>}>
    <PlanPanel project={projectInfo} onDirtyChange={vi.fn()} onOpenRunDashboard={vi.fn()} />
  </HeaderSlotsProvider>
}

const queueEvidence: ProjectQueue = {
  project_id: 'alpha',
  settings: { auto_consume_plans: true, max_concurrent_implementations: 2, revision: 'q'.repeat(64), persisted: false, source: 'defaults' },
  capacity: { limit: 2, available_slots: 2, reserved_count: 0, starting_count: 0, active_count: 0, uncertain_count: 0 },
  plans: [],
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
    vi.resetAllMocks()
    mockList()
    vi.mocked(api.getProjectQueue).mockResolvedValue(queueEvidence)
  })

  it('publishes the list while queue evidence is held and keeps an edited document on queue failure', async () => {
    const held = deferred<ProjectQueue>()
    vi.mocked(api.getProjectQueue).mockReturnValueOnce(held.promise)
    vi.mocked(api.readProjectPlan).mockResolvedValue({ ...todoPlan, content: '# Original' })
    render(<PlanPanel project={project} onDirtyChange={vi.fn()} onOpenRunDashboard={vi.fn()} />)
    const row = await screen.findByRole('button', { name: /plan-a.md/ })
    expect(screen.queryByText('Loading plans…')).toBeNull()
    expect(screen.getByText('Queue evidence loading…')).toBeDefined()
    expect(screen.queryByText(/Implementation slots:/)).toBeNull()
    fireEvent.click(row)
    await screen.findByLabelText('Plan content')
    const editor = screen.getByLabelText('Plan content') as HTMLTextAreaElement
    fireEvent.change(editor, { target: { value: '# Local draft' } })
    held.reject(new Error('queue offline'))
    await screen.findByText(/Current queue reasons are unavailable/)
    expect(editor.value).toBe('# Local draft')
    expect(screen.queryByText(/Implementation slots:/)).toBeNull()
  })

  it('keeps list failure visible when queue succeeds', async () => {
    const list = deferred<PlanDocument[]>()
    const queue = deferred<ProjectQueue>()
    vi.mocked(api.listProjectPlans).mockReturnValueOnce(list.promise)
    vi.mocked(api.getProjectQueue).mockReturnValueOnce(queue.promise)
    render(<PlanPanel project={project} onDirtyChange={vi.fn()} onOpenRunDashboard={vi.fn()} />)
    await act(async () => queue.resolve(queueEvidence))
    expect(screen.getByText(/Implementation slots:/)).toBeDefined()
    await act(async () => list.reject(new Error('list offline')))
    expect(screen.getByRole('alert').textContent).toContain('list offline')
    expect(screen.queryByText('Loading plans…')).toBeNull()

  })

  it('rejects late list and queue responses after a project switch', async () => {
    const oldList = deferred<PlanDocument[]>()
    const oldQueue = deferred<ProjectQueue>()
    vi.mocked(api.listProjectPlans).mockReturnValueOnce(oldList.promise).mockResolvedValueOnce([])
    vi.mocked(api.getProjectQueue).mockReturnValueOnce(oldQueue.promise).mockResolvedValueOnce({ ...queueEvidence, project_id: 'beta' })
    const view = render(<PlanPanel project={project} onDirtyChange={vi.fn()} onOpenRunDashboard={vi.fn()} />)
    view.rerender(<PlanPanel project={{ ...project, id: 'beta' }} onDirtyChange={vi.fn()} onOpenRunDashboard={vi.fn()} />)
    await screen.findByText('No plans yet. Create a draft to begin.')
    await act(async () => { oldList.resolve([todoPlan]); oldQueue.resolve(queueEvidence) })
    expect(screen.queryByRole('button', { name: /plan-a.md/ })).toBeNull()
    expect(screen.getByText(/Implementation slots: 2 of 2/)).toBeDefined()
  })

  const betaProject: ProjectInfo = { ...project, id: 'beta', display_name: 'Beta Project', current_path: '/srv/code/beta' }
  const betaPlan: PlanDocument = {
    project_id: 'beta', name: 'plan-beta.md', path: 'plans/in-progress/plan-beta.md',
    status: 'in_progress', revision: 'c'.repeat(64), size_bytes: 14,
  }

  it('keeps the newer project list loading after a stale success from the previous project', async () => {
    const oldList = deferred<PlanDocument[]>()
    const oldQueue = deferred<ProjectQueue>()
    const newList = deferred<PlanDocument[]>()
    const newQueue = deferred<ProjectQueue>()
    vi.mocked(api.listProjectPlans)
      .mockReturnValueOnce(oldList.promise)
      .mockReturnValueOnce(newList.promise)
    vi.mocked(api.getProjectQueue)
      .mockReturnValueOnce(oldQueue.promise)
      .mockReturnValueOnce(newQueue.promise)
    const view = render(<PlanPanel project={project} onDirtyChange={vi.fn()} onOpenRunDashboard={vi.fn()} />)
    view.rerender(<PlanPanel project={betaProject} onDirtyChange={vi.fn()} onOpenRunDashboard={vi.fn()} />)
    expect(screen.getByText('Loading plans…')).toBeDefined()
    // Alpha's stale success must not publish rows, an error, or clear beta's loading.
    await act(async () => { oldList.resolve([todoPlan]) })
    expect(screen.getByText('Loading plans…')).toBeDefined()
    expect(screen.queryByRole('button', { name: /plan-a.md/ })).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.queryByText('No plans yet. Create a draft to begin.')).toBeNull()
    await act(async () => { newList.resolve([betaPlan]) })
    expect(screen.queryByText('Loading plans…')).toBeNull()
    expect(await screen.findByRole('button', { name: /plan-beta.md/ })).toBeDefined()
    // Queue completion stays independent of the list assertions.
    await act(async () => { oldQueue.resolve(queueEvidence); newQueue.resolve({ ...queueEvidence, project_id: 'beta' }) })
    expect(screen.getByText(/Implementation slots: 2 of 2/)).toBeDefined()
  })

  it('keeps the newer project list loading after a stale rejection from the previous project', async () => {
    const oldList = deferred<PlanDocument[]>()
    const oldQueue = deferred<ProjectQueue>()
    const newList = deferred<PlanDocument[]>()
    const newQueue = deferred<ProjectQueue>()
    vi.mocked(api.listProjectPlans)
      .mockReturnValueOnce(oldList.promise)
      .mockReturnValueOnce(newList.promise)
    vi.mocked(api.getProjectQueue)
      .mockReturnValueOnce(oldQueue.promise)
      .mockReturnValueOnce(newQueue.promise)
    const view = render(<PlanPanel project={project} onDirtyChange={vi.fn()} onOpenRunDashboard={vi.fn()} />)
    view.rerender(<PlanPanel project={betaProject} onDirtyChange={vi.fn()} onOpenRunDashboard={vi.fn()} />)
    expect(screen.getByText('Loading plans…')).toBeDefined()
    // Alpha's stale rejection must not publish an error or clear beta's loading.
    await act(async () => { oldList.reject(new Error('alpha list offline')) })
    expect(screen.getByText('Loading plans…')).toBeDefined()
    expect(screen.queryByRole('button', { name: /plan-a.md/ })).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.queryByText('No plans yet. Create a draft to begin.')).toBeNull()
    await act(async () => { newList.resolve([betaPlan]) })
    expect(screen.queryByText('Loading plans…')).toBeNull()
    expect(await screen.findByRole('button', { name: /plan-beta.md/ })).toBeDefined()
    await act(async () => { oldQueue.resolve(queueEvidence); newQueue.resolve({ ...queueEvidence, project_id: 'beta' }) })
    expect(screen.getByText(/Implementation slots: 2 of 2/)).toBeDefined()
  })

  it('retains equal plan rows and capacity while a repeat queue request is pending', async () => {
    render(hosted(project))
    const row = await screen.findByRole('button', { name: /plan-a.md/ })
    await screen.findByText(/Implementation slots: 2 of 2/)
    const list = deferred<PlanDocument[]>()
    const queue = deferred<ProjectQueue>()
    vi.mocked(api.listProjectPlans).mockReturnValueOnce(list.promise)
    vi.mocked(api.getProjectQueue).mockReturnValueOnce(queue.promise)
    fireEvent.click(screen.getByRole('button', { name: 'More' }))
    fireEvent.click(screen.getByRole('menuitem', { name: 'Refresh plans' }))
    expect(screen.getByRole('button', { name: /plan-a.md/ })).toBe(row)
    await act(async () => list.resolve([{ ...todoPlan }, { ...inProgressPlan }]))
    expect(screen.getByRole('button', { name: /plan-a.md/ })).toBe(row)
    await act(async () => queue.resolve({ ...queueEvidence }))
    expect(screen.getByRole('button', { name: /plan-a.md/ })).toBe(row)
    expect(screen.getByText(/Implementation slots: 2 of 2/)).toBeDefined()
  })

  it('applies only the newest refresh and preserves equal rows and a dirty editor', async () => {
    render(hosted(project))
    await screen.findByRole('button', { name: /plan-a.md/ })
    const firstList = deferred<PlanDocument[]>()
    const firstQueue = deferred<ProjectQueue>()
    const secondList = deferred<PlanDocument[]>()
    const secondQueue = deferred<ProjectQueue>()
    vi.mocked(api.listProjectPlans).mockReturnValueOnce(firstList.promise).mockReturnValueOnce(secondList.promise)
    vi.mocked(api.getProjectQueue).mockReturnValueOnce(firstQueue.promise).mockReturnValueOnce(secondQueue.promise)
    fireEvent.click(screen.getByRole('button', { name: 'More' }))
    fireEvent.click(screen.getByRole('menuitem', { name: 'Refresh plans' }))
    await openPlan(todoPlan, '# Original')
    const editor = screen.getByLabelText('Plan content') as HTMLTextAreaElement
    fireEvent.change(editor, { target: { value: '# Edited' } })
    // A save starts a newer refresh while the first queue and list are pending.
    vi.mocked(api.updateProjectPlan).mockResolvedValue({ ...todoPlan, content: '# Edited' })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(api.listProjectPlans).toHaveBeenCalledTimes(3))
    await act(async () => {
      secondList.resolve([{ ...todoPlan }, { ...inProgressPlan }])
      secondQueue.resolve({ ...queueEvidence, capacity: { ...queueEvidence.capacity, available_slots: 1 } })
    })
    await act(async () => {
      firstList.resolve([])
      firstQueue.resolve({ ...queueEvidence, capacity: { ...queueEvidence.capacity, available_slots: 0 } })
    })
    expect(editor.value).toBe('# Edited')
    fireEvent.click(screen.getByRole('button', { name: '← Back to Plans' }))
    expect(screen.getByText(/Implementation slots: 1 of 2/)).toBeDefined()
    expect(screen.getByRole('button', { name: /plan-a.md/ })).toBeDefined()
  })

  it('shows failed and needs-change plans with queue evidence and reviews requeue before acting', async () => {
    const failed: PlanDocument = {
      project_id: 'alpha', name: 'failed.md', path: 'plans/failed/failed.md',
      status: 'failed', revision: 'f'.repeat(64), size_bytes: 20,
      lifecycle: { reason_code: 'execution_failed', source_run_id: 'source-run' },
    }
    const needs: PlanDocument = {
      project_id: 'alpha', name: 'needs.md', path: 'plans/needs-plan-change/needs.md',
      status: 'needs_plan_change', revision: 'n'.repeat(64), size_bytes: 20,
    }
    vi.mocked(api.listProjectPlans).mockResolvedValue([failed, needs])
    vi.mocked(api.getProjectQueue).mockResolvedValue({
      project_id: 'alpha',
      settings: { auto_consume_plans: true, max_concurrent_implementations: 2, revision: 'q'.repeat(64), persisted: false, source: 'defaults' },
      capacity: { limit: 2, available_slots: 1, reserved_count: 1, starting_count: 0, active_count: 0, uncertain_count: 0 },
      plans: [{ name: failed.name, path: failed.path, status: 'failed', identity: 'identity-1', outcome: 'held', reason: 'execution_failed', dependency: null, run_id: 'source-run', revision: failed.revision }],
    })
    const onOpenRun = vi.fn()
    render(<PlanPanel project={project} onDirtyChange={vi.fn()} onOpenRunDashboard={vi.fn()} onOpenRun={onOpenRun} />)
    await screen.findByRole('heading', { name: 'Failed' })
    expect(screen.getByRole('heading', { name: 'Needs plan change' })).toBeDefined()
    expect(screen.queryByRole('heading', { name: 'Draft' })).toBeNull()
    expect(screen.queryByRole('heading', { name: 'Done' })).toBeNull()
    expect(screen.getByText(/Implementation slots: 1 of 2 available/)).toBeDefined()
    expect(screen.getByText('Execution failed')).toBeDefined()
    await openPlan(failed, '# Corrected plan\n')
    fireEvent.click(screen.getByRole('button', { name: 'View recorded run' }))
    expect(onOpenRun).toHaveBeenCalledWith('source-run')
    fireEvent.click(screen.getByRole('button', { name: 'Review requeue…' }))
    expect(screen.getByRole('alertdialog', { name: 'Review plan requeue' })).toBeDefined()
    expect(api.requeueProjectPlan).not.toHaveBeenCalled()
    vi.mocked(api.requeueProjectPlan).mockResolvedValue({ plan: { ...failed, status: 'in_progress', path: 'plans/in-progress/failed.md', content: '# Corrected plan\n' } })
    fireEvent.click(screen.getByRole('button', { name: 'Confirm requeue and resume' }))
    await waitFor(() => expect(api.requeueProjectPlan).toHaveBeenCalledWith('alpha', 'failed', 'failed.md', {
      expected_revision: failed.revision, source_run_id: 'source-run',
    }))
    await screen.findByText('The corrected plan was requeued to Ready.')
  })

  it('keeps the verified recorded-run action after a partial requeue and revisit without a queue claim', async () => {
    const failed: PlanDocument = {
      project_id: 'alpha', name: 'failed.md', path: 'plans/failed/failed.md',
      status: 'failed', revision: 'f'.repeat(64), size_bytes: 20,
      lifecycle: { reason_code: 'execution_failed', source_run_id: 'source-run' },
    }
    const ready: PlanDocument = {
      ...failed, path: 'plans/in-progress/failed.md', status: 'in_progress',
      lifecycle: { reason_code: 'explicit_requeue', source_run_id: 'source-run' },
    }
    let moved = false
    vi.mocked(api.listProjectPlans).mockImplementation(async () => [moved ? ready : failed])
    vi.mocked(api.readProjectPlan).mockImplementation(async (_project, status) => ({
      ...(status === 'in_progress' ? ready : failed), content: '# Corrected plan\n',
    }))
    vi.mocked(api.requeueProjectPlan).mockImplementation(async () => {
      moved = true
      throw new ApiError(409, 'Recorded run could not resume', 'plan_requeue_resume_conflict', {
        plan_path: ready.path,
      })
    })
    const onOpenRun = vi.fn()
    render(<PlanPanel project={project} onDirtyChange={vi.fn()} onOpenRunDashboard={vi.fn()} onOpenRun={onOpenRun} />)
    fireEvent.click(await screen.findByRole('button', { name: /failed\.md/ }))
    await screen.findByLabelText('Plan content')
    fireEvent.click(screen.getByRole('button', { name: 'Review requeue…' }))
    fireEvent.click(screen.getByRole('button', { name: 'Confirm requeue and resume' }))
    await screen.findByText(/Use View recorded run to resolve its attention state/)
    await waitFor(() => expect(api.listProjectPlans).toHaveBeenCalledTimes(2))
    fireEvent.click(screen.getByRole('button', { name: 'View recorded run' }))
    expect(onOpenRun).toHaveBeenCalledWith('source-run')

    fireEvent.click(screen.getByRole('button', { name: '← Back to Plans' }))
    await screen.findByRole('heading', { name: 'Ready' })
    fireEvent.click(screen.getByRole('button', { name: /failed\.md/ }))
    await screen.findByLabelText('Plan content')
    fireEvent.click(screen.getByRole('button', { name: 'View recorded run' }))
    expect(onOpenRun).toHaveBeenCalledTimes(2)
    expect(api.getProjectQueue).toHaveBeenCalled()
  })

  it('groups contained plans by lifecycle status and reads the selected plan', async () => {
    render(<PlanPanel project={project} onDirtyChange={vi.fn()} onOpenRunDashboard={vi.fn()} />)
    await screen.findByRole('heading', { name: 'Draft', exact: true })
    expect(screen.getByRole('heading', { name: 'Ready', exact: true })).toBeDefined()
    expect(screen.queryByRole('heading', { name: 'Done' })).toBeNull()
    await openPlan(todoPlan, '# Plan A\n')
    expect(api.readProjectPlan).toHaveBeenCalledWith('alpha', 'todo', 'plan-a.md')
    expect((screen.getByLabelText('Plan content') as HTMLTextAreaElement).value).toBe('# Plan A\n')
    expect(screen.getAllByText(todoPlan.path, { exact: true })).toHaveLength(2)
  })

  it('explains the lifecycle and offers Configure run only for a saved Ready plan', async () => {
    const onOpenRunDashboard = vi.fn()
    const donePlan: PlanDocument = {
      project_id: 'alpha', name: 'plan-c.md', path: 'plans/done/plan-c.md',
      status: 'done', revision: 'c'.repeat(64), size_bytes: 14,
    }
    vi.mocked(api.listProjectPlans).mockResolvedValue([todoPlan, inProgressPlan, donePlan])
    render(<PlanPanel project={project} onDirtyChange={vi.fn()} onOpenRunDashboard={onOpenRunDashboard} />)

    await screen.findByRole('heading', { name: 'Draft', exact: true })
    expect(screen.getByText(/Working notes/)).toBeDefined()
    expect(screen.getByText(/Saved plans available/)).toBeDefined()
    expect(screen.getByText(/kept for the record/)).toBeDefined()

    // A saved Ready plan offers the exact relative path handoff.
    await openPlan(inProgressPlan, '# Ready plan\n')
    expect(screen.getByText(/startup checks run when you start the plan/)).toBeDefined()
    expect(screen.queryByText('Ready — runnable')).toBeNull()
    const run = screen.getByRole('button', { name: 'Configure run…' })
    expect(run.getAttribute('disabled')).toBeNull()
    fireEvent.click(run)
    expect(onOpenRunDashboard).toHaveBeenCalledWith('plans/in-progress/plan-b.md')

    // A dirty Ready draft must be saved before it can run.
    fireEvent.change(screen.getByLabelText('Plan content'), { target: { value: '# Unsaved\n' } })
    expect(screen.getByRole('button', { name: 'Configure run…' }).getAttribute('disabled')).not.toBeNull()
    expect(screen.getByText(/Save this draft before running the plan/)).toBeDefined()
    fireEvent.click(screen.getByRole('button', { name: '← Back to Plans' }))
    expect(screen.getByRole('alertdialog', { name: 'Unsaved plan edits' })).toBeDefined()
    fireEvent.click(screen.getByRole('button', { name: 'Discard edits' }))

    // Drafts explain how to become runnable instead of offering a no-op.
    await openPlan(todoPlan, '# Draft plan\n')
    expect(screen.queryByRole('button', { name: 'Configure run…' })).toBeNull()
    expect(screen.getByText(/not runnable yet\. Save it and move it to Ready/)).toBeDefined()

    // Done plans explain that they are archival.
    fireEvent.click(screen.getByRole('button', { name: '← Back to Plans' }))
    await openPlan(donePlan, '# Done plan\n')
    expect(screen.queryByRole('button', { name: 'Configure run…' })).toBeNull()
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

  it('opens an exact handed-off draft and keeps the unsaved navigation guard', async () => {
    const handedOff: PlanDocument = {
      project_id: 'alpha', name: 'followup-run-7.md', path: 'plans/todo/followup-run-7.md',
      status: 'todo', revision: 'd'.repeat(64), size_bytes: 24,
    }
    const laterPlan: PlanDocument = {
      project_id: 'alpha', name: 'later.md', path: 'plans/todo/later.md',
      status: 'todo', revision: 'e'.repeat(64), size_bytes: 16,
    }
    const onInitialPlanHandled = vi.fn()
    vi.mocked(api.listProjectPlans).mockResolvedValue([handedOff, laterPlan])
    vi.mocked(api.readProjectPlan).mockImplementation(async (_projectId, _status, name) => ({
      ...(name === handedOff.name ? handedOff : laterPlan),
      content: name === handedOff.name ? '# Follow-up evidence\n' : '# Later\n',
    }))
    const view = render(
      <PlanPanel
        project={project}
        onDirtyChange={vi.fn()}
        onOpenRunDashboard={vi.fn()}
        initialPlanPath={handedOff.path}
        onInitialPlanHandled={onInitialPlanHandled}
      />,
    )

    await screen.findByLabelText('Plan content')
    expect(api.readProjectPlan).toHaveBeenCalledWith('alpha', 'todo', handedOff.name)
    expect((screen.getByLabelText('Plan content') as HTMLTextAreaElement).value).toBe('# Follow-up evidence\n')
    expect(onInitialPlanHandled).toHaveBeenCalledTimes(1)

    fireEvent.change(screen.getByLabelText('Plan content'), { target: { value: '# Keep this local\n' } })
    view.rerender(
      <PlanPanel
        project={project}
        onDirtyChange={vi.fn()}
        onOpenRunDashboard={vi.fn()}
        initialPlanPath={laterPlan.path}
        onInitialPlanHandled={onInitialPlanHandled}
      />,
    )
    await screen.findByRole('alertdialog', { name: 'Unsaved plan edits' })
    expect((screen.getByLabelText('Plan content') as HTMLTextAreaElement).value).toBe('# Keep this local\n')
    fireEvent.click(screen.getByRole('button', { name: 'Discard edits', exact: true }))
    await waitFor(() => expect(api.readProjectPlan).toHaveBeenLastCalledWith('alpha', 'todo', laterPlan.name))
    expect((screen.getByLabelText('Plan content') as HTMLTextAreaElement).value).toBe('# Later\n')
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
    await screen.findAllByText(promoted.path, { exact: true })
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
