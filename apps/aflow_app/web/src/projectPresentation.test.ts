import { describe, expect, it } from 'vitest'
import type { ProjectInfo } from './types'
import { groupProjectRegistrations, projectContextLabel, registeredParentFor } from './projectPresentation'

const project = (id: string, name: string, path: string, parent_project_id?: string | null): ProjectInfo => ({
  id,
  display_name: name,
  current_path: path,
  is_git_root: true,
  registered_at: '2026-01-01T00:00:00Z',
  readiness: 'ready',
  parent_project_id,
})

describe('project presentation relationships', () => {
  it('groups only verified one-level parents and preserves ungroupable registrations', () => {
    const projects = [
      project('primary', 'Primary', '/code/primary'),
      project('child', 'Feature worktree', '/code/child', 'primary'),
      project('unrelated', 'Unrelated clone', '/code/unrelated'),
      project('missing-parent', 'Missing parent', '/code/missing-parent', 'not-registered'),
      project('nested', 'Nested relationship', '/code/nested', 'child'),
    ]

    const groups = groupProjectRegistrations(projects, '', null)

    expect(groups.map(group => group.primary.id)).toEqual(['primary', 'unrelated', 'missing-parent', 'nested'])
    expect(groups[0].children.map(child => child.id)).toEqual(['child'])
    expect(registeredParentFor(projects[1], projects)?.id).toBe('primary')
    expect(registeredParentFor(projects[4], projects)).toBeNull()
  })

  it('retains parent context for matching and selected children', () => {
    const projects = [
      project('primary', 'Primary', '/code/primary'),
      project('child', 'Feature worktree', '/code/child', 'primary'),
    ]

    const matching = groupProjectRegistrations(projects, 'feature', null)[0]
    expect(matching.primary.id).toBe('primary')
    expect(matching.matchingChildIds).toEqual(['child'])

    const selected = groupProjectRegistrations(projects, 'no-match', 'child')[0]
    expect(selected.selectedChildId).toBe('child')
    expect(projectContextLabel(projects[1], projects)).toBe('Primary · Worktree: Feature worktree')
  })
})
