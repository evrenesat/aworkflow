import type { ProjectInfo } from './types'

/**
 * Returns the registered primary for a read-only relationship projection.
 * Only one-level relationships are presentable; malformed or nested links
 * remain independent rows.
 */
export function registeredParentFor(
  project: ProjectInfo,
  projects: readonly ProjectInfo[],
): ProjectInfo | null {
  const parentId = project.parent_project_id
  if (!parentId || parentId === project.id) return null
  const parent = projects.find(candidate => candidate.id === parentId)
  if (!parent || parent.parent_project_id) return null
  return parent
}

export function projectContextLabel(
  project: ProjectInfo,
  projects: readonly ProjectInfo[],
): string {
  const parent = registeredParentFor(project, projects)
  return parent
    ? `${parent.display_name} · Worktree: ${project.display_name}`
    : project.display_name
}

export interface ProjectGroup {
  primary: ProjectInfo
  children: ProjectInfo[]
  matchingChildIds: string[]
  selectedChildId: string | null
  primaryMatches: boolean
}

function projectMatches(project: ProjectInfo, query: string): boolean {
  return !query
    || project.display_name.toLowerCase().includes(query)
    || project.current_path.toLowerCase().includes(query)
}

/**
 * Build the presentation-only, one-level project tree while retaining the
 * registry's original order and every ungroupable registration.
 */
export function groupProjectRegistrations(
  projects: readonly ProjectInfo[],
  rawQuery: string,
  selectedProjectId: string | null,
): ProjectGroup[] {
  const query = rawQuery.trim().toLowerCase()
  const primaryById = new Map(
    projects
      .filter(project => !project.parent_project_id)
      .map(project => [project.id, project]),
  )
  const childrenByParent = new Map<string, ProjectInfo[]>()

  for (const project of projects) {
    const parentId = project.parent_project_id
    if (!parentId || parentId === project.id || !primaryById.has(parentId)) continue
    const children = childrenByParent.get(parentId) ?? []
    children.push(project)
    childrenByParent.set(parentId, children)
  }

  return projects.flatMap(project => {
    const parent = registeredParentFor(project, projects)
    if (parent) return []

    const children = childrenByParent.get(project.id) ?? []
    const matchingChildIds = query
      ? children.filter(child => projectMatches(child, query)).map(child => child.id)
      : []
    const selectedChildId = children.some(child => child.id === selectedProjectId)
      ? selectedProjectId
      : null
    const primaryMatches = projectMatches(project, query)
    if (!primaryMatches && matchingChildIds.length === 0 && selectedChildId === null) return []

    return [{ primary: project, children, matchingChildIds, selectedChildId, primaryMatches }]
  })
}
