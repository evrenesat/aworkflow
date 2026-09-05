import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api'
import * as api from '../api'
import type { PlanDocument, ProjectInfo } from '../types'
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

describe('PlanPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockList()
  })

  it('groups contained plans by lifecycle status and reads the selected plan', async () => {
    render(<PlanPanel project={project} onDirtyChange={vi.fn()} onOpenRunDashboard={vi.fn()} />)
    await screen.findByText('Todo')
    expect(screen.getByText('In progress')).toBeDefined()
    expect(screen.getByText('Done')).toBeDefined()
    await openPlan(todoPlan, '# Plan A\n')
    expect(api.readProjectPlan).toHaveBeenCalledWith('alpha', 'todo', 'plan-a.md')
    expect((screen.getByLabelText('Plan content') as HTMLTextAreaElement).value).toBe('# Plan A\n')
    expect(screen.getByText(todoPlan.path)).toBeDefined()
  })

  it('creates a plan in the todo lifecycle and opens it', async () => {
    const created: PlanDocument = { ...todoPlan, size_bytes: 8 }
    vi.mocked(api.createProjectPlan).mockResolvedValue(created)
    vi.mocked(api.readProjectPlan).mockResolvedValue({ ...created, content: '# Plan\n\n' })
    render(<PlanPanel project={project} onDirtyChange={vi.fn()} onOpenRunDashboard={vi.fn()} />)
    fireEvent.change(screen.getByLabelText('New plan filename'), { target: { value: 'plan-a.md' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create plan' }))

    await waitFor(() => expect(api.createProjectPlan).toHaveBeenCalledWith('alpha', {
      name: 'plan-a.md',
      content: '# Plan\n\n',
    }))
    await screen.findByLabelText('Plan content')
    expect((screen.getByLabelText('Plan content') as HTMLTextAreaElement).value).toBe('# Plan\n\n')
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
    fireEvent.click(screen.getByRole('button', { name: '← All plans' }))

    expect(screen.getByRole('alertdialog', { name: 'Unsaved plan edits' })).toBeDefined()
    expect((screen.getByLabelText('Plan content') as HTMLTextAreaElement).value).toBe('# Draft\n')
    fireEvent.click(screen.getByRole('button', { name: 'Discard edits' }))
    await screen.findByLabelText('New plan filename')
  })

  it('does not promote a plan while its current draft is unsaved', async () => {
    render(<PlanPanel project={project} onDirtyChange={vi.fn()} onOpenRunDashboard={vi.fn()} />)
    await openPlan(todoPlan, '# Original\n')
    fireEvent.change(screen.getByLabelText('Plan content'), { target: { value: '# Draft\n' } })

    const promote = screen.getByRole('button', { name: 'Move to in-progress' })
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
    fireEvent.click(screen.getByRole('button', { name: 'Move to in-progress' }))

    await waitFor(() => expect(api.promoteProjectPlan).toHaveBeenCalledWith(
      'alpha', 'todo', 'plan-a.md', { expected_revision: todoPlan.revision },
    ))
    await screen.findByText(promoted.path)
    await waitFor(() => expect(api.listProjectPlans).toHaveBeenCalledTimes(2))
  })
})
