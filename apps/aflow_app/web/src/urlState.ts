/**
 * Public workspace URL state.  The query carries only validated, nonsecret
 * identifiers — the registered project id, one workspace view, and an
 * optional run id that is meaningful only on the Runs view.  Tokens,
 * prompts, context, idempotency keys, and plan contents never belong here.
 */
export type WorkspaceView = 'overview' | 'settings' | 'plans' | 'runs'

export interface WorkspaceQuery {
  project: string | null
  view: 'projects' | WorkspaceView
  run: string | null
}

/** What the raw query string literally contained, before normalization. */
export interface RawWorkspaceQuery {
  project: string | null
  view: WorkspaceView | null
  run: string | null
}

const WORKSPACE_VIEWS: readonly WorkspaceView[] = ['overview', 'settings', 'plans', 'runs']

function nonEmpty(value: string | null): string | null {
  return value !== null && value.trim() !== '' ? value : null
}

/** Reads only the three known parameters; `run` counts only beside `view=runs`. */
export function parseWorkspaceQuery(search: string): RawWorkspaceQuery {
  const params = new URLSearchParams(search)
  const rawView = params.get('view')
  const view = WORKSPACE_VIEWS.includes(rawView as WorkspaceView) ? (rawView as WorkspaceView) : null
  return {
    project: nonEmpty(params.get('project')),
    view,
    run: view === 'runs' ? nonEmpty(params.get('run')) : null,
  }
}

/** Fills in the concrete workspace state a raw link means: no project is the Projects view, a project without a usable view is Overview. */
export function normalizeWorkspaceQuery(raw: RawWorkspaceQuery): WorkspaceQuery {
  if (raw.project === null) return { project: null, view: 'projects', run: null }
  return {
    project: raw.project,
    view: raw.view ?? 'overview',
    run: raw.view === 'runs' ? raw.run : null,
  }
}

/** Serializes back to a query string (`''` when nothing is scoped), in a fixed parameter order. */
export function workspaceHref(query: WorkspaceQuery): string {
  const params = new URLSearchParams()
  if (query.project !== null) params.set('project', query.project)
  if (query.view !== 'projects') params.set('view', query.view)
  if (query.view === 'runs' && query.run !== null) params.set('run', query.run)
  const text = params.toString()
  return text ? `?${text}` : ''
}

export function sameWorkspaceQuery(left: WorkspaceQuery, right: WorkspaceQuery): boolean {
  return left.project === right.project && left.view === right.view && left.run === right.run
}
