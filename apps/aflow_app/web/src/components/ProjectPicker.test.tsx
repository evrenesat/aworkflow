import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import type { ComponentProps } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import type { ProjectInfo } from '../types'
import { ProjectPicker } from './ProjectPicker'

vi.mock('../api', async () => {
  const actual = await vi.importActual<typeof import('../api')>('../api')
  return { ...actual, getProjectDiscovery: vi.fn() }
})

const primary: ProjectInfo = {
  id: 'primary', display_name: 'Primary project', current_path: '/srv/primary',
  is_git_root: true, registered_at: '2026-01-01T00:00:00Z', readiness: 'ready',
}
const child: ProjectInfo = {
  id: 'child', display_name: 'Feature worktree', current_path: '/srv/feature',
  is_git_root: true, registered_at: '2026-01-01T00:00:00Z', readiness: 'ready',
  parent_project_id: 'primary',
}
const ungroupable: ProjectInfo = {
  id: 'orphan', display_name: 'Unverified relationship', current_path: '/srv/orphan',
  is_git_root: false, registered_at: '2026-01-01T00:00:00Z', readiness: 'blocked',
  parent_project_id: 'missing-parent',
}

const discovery = {
  schema_version: 1,
  managed_root: '/srv',
  candidates: [],
  visited_entries: 0,
  skipped_unreadable: 0,
  truncated: false,
  limits: { max_visited_entries: 500, max_candidates: 100 },
}

function renderPicker(overrides: Partial<ComponentProps<typeof ProjectPicker>> = {}) {
  return render(<ProjectPicker
    projects={[primary, child, ungroupable]}
    selectedProjectId={null}
    loading={false}
    error={null}
    onSelectProject={vi.fn()}
    onRefresh={vi.fn()}
    onCreate={vi.fn()}
    onUnregister={vi.fn().mockResolvedValue(undefined)}
    {...overrides}
  />)
}

describe('ProjectPicker worktree disclosure', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(api.getProjectDiscovery).mockResolvedValue(discovery)
  })

  it('starts collapsed with a keyboard disclosure and keeps ungroupable rows independent', async () => {
    renderPicker()
    await screen.findByText('Worktrees (1)')
    const details = screen.getByText('Worktrees (1)').closest('details')
    expect(details).not.toBeNull()
    expect(details?.hasAttribute('open')).toBe(false)
    const addedList = screen.getByRole('list', { name: 'Added projects' })
    const orphanRow = Array.from(addedList.children).find(item => item.textContent?.includes('Unverified relationship'))
    expect(orphanRow).toBeDefined()
    expect(within(orphanRow!).getByRole('button', { name: /^Unverified relationship/ })).toBeDefined()
    expect(screen.getByText('Worktrees (1)').tagName).toBe('SUMMARY')
  })

  it('reveals a selected or matching child and routes its exact action ID', async () => {
    const onSelectProject = vi.fn()
    const onUnregister = vi.fn().mockResolvedValue(undefined)
    renderPicker({ selectedProjectId: 'child', onSelectProject, onUnregister })
    const childButton = await screen.findByRole('button', { name: /^Feature worktree/ })
    expect(childButton.getAttribute('aria-pressed')).toBe('true')
    fireEvent.click(childButton)
    expect(onSelectProject).toHaveBeenCalledWith(child)

    const childRow = childButton.closest('li')
    expect(childRow).not.toBeNull()
    fireEvent.click(within(childRow!).getByRole('button', { name: 'More actions for worktree Feature worktree' }))
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Unregister…' }))
    fireEvent.click(screen.getByRole('button', { name: 'Unregister (keeps files)' }))
    await waitFor(() => expect(onUnregister).toHaveBeenCalledWith('child'))
  })

  it('matches a child without duplicating it as a top-level project', async () => {
    renderPicker()
    await screen.findByText('Worktrees (1)')
    const search = screen.getByRole('textbox', { name: 'Search projects and available candidates' })
    fireEvent.change(search, { target: { value: 'feature' } })
    const details = screen.getByText('Worktrees (1)').closest('details')
    expect(details?.hasAttribute('open')).toBe(true)
    expect(screen.getByRole('button', { name: /^Feature worktree/ })).toBeDefined()
    const addedList = screen.getByRole('list', { name: 'Added projects' })
    expect(Array.from(addedList.children).filter(item => item.tagName === 'LI')).toHaveLength(1)
  })
})
