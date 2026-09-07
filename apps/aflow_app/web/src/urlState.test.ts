import { describe, expect, it } from 'vitest'
import {
  normalizeWorkspaceQuery,
  parseWorkspaceQuery,
  sameWorkspaceQuery,
  workspaceHref,
} from './urlState'

describe('workspace URL state', () => {
  it('parses the three known public parameters', () => {
    expect(parseWorkspaceQuery('?project=alpha&view=runs&run=run-1')).toEqual({
      project: 'alpha', view: 'runs', run: 'run-1',
    })
    expect(parseWorkspaceQuery('?project=alpha&view=settings')).toEqual({
      project: 'alpha', view: 'settings', run: null,
    })
    expect(parseWorkspaceQuery('')).toEqual({ project: null, view: null, run: null })
  })

  it('drops invalid parameters instead of interpreting them', () => {
    // Unknown view values are not workspace views.
    expect(parseWorkspaceQuery('?project=alpha&view=widgets&run=run-1')).toEqual({
      project: 'alpha', view: null, run: null,
    })
    // A run id is meaningful only on the Runs view.
    expect(parseWorkspaceQuery('?project=alpha&view=settings&run=run-1')).toEqual({
      project: 'alpha', view: 'settings', run: null,
    })
    expect(parseWorkspaceQuery('?project=alpha&run=run-1')).toEqual({
      project: 'alpha', view: null, run: null,
    })
    // Empty identifiers are absent, and unknown keys are ignored.
    expect(parseWorkspaceQuery('?project=&view=runs&token=secret')).toEqual({
      project: null, view: 'runs', run: null,
    })
  })

  it('normalizes to concrete workspace state', () => {
    expect(normalizeWorkspaceQuery({ project: null, view: 'runs', run: 'run-1' })).toEqual({
      project: null, view: 'projects', run: null,
    })
    expect(normalizeWorkspaceQuery({ project: 'alpha', view: null, run: null })).toEqual({
      project: 'alpha', view: 'overview', run: null,
    })
    expect(normalizeWorkspaceQuery({ project: 'alpha', view: 'plans', run: null })).toEqual({
      project: 'alpha', view: 'plans', run: null,
    })
  })

  it('serializes in a fixed order and round-trips', () => {
    expect(workspaceHref({ project: null, view: 'projects', run: null })).toBe('')
    expect(workspaceHref({ project: 'alpha', view: 'overview', run: null })).toBe('?project=alpha&view=overview')
    expect(workspaceHref({ project: 'alpha', view: 'runs', run: 'run-1' })).toBe('?project=alpha&view=runs&run=run-1')
    expect(workspaceHref({ project: 'alpha', view: 'runs', run: null })).toBe('?project=alpha&view=runs')
    // A run id can never leak into another view's URL.
    expect(workspaceHref({ project: 'alpha', view: 'settings', run: 'run-1' as never })).toBe('?project=alpha&view=settings')

    const href = workspaceHref({ project: 'alpha', view: 'runs', run: 'run-1' })
    expect(normalizeWorkspaceQuery(parseWorkspaceQuery(href))).toEqual({
      project: 'alpha', view: 'runs', run: 'run-1',
    })
  })

  it('compares queries structurally', () => {
    expect(sameWorkspaceQuery(
      { project: 'alpha', view: 'runs', run: 'run-1' },
      { project: 'alpha', view: 'runs', run: 'run-1' },
    )).toBe(true)
    expect(sameWorkspaceQuery(
      { project: 'alpha', view: 'runs', run: 'run-1' },
      { project: 'alpha', view: 'runs', run: null },
    )).toBe(false)
  })
})
