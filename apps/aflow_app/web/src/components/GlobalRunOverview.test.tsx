import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import type { ProjectInfo, RunStatus } from '../types'
import { GlobalRunOverview } from './GlobalRunOverview'

vi.mock('../api', async () => {
  const actual = await vi.importActual<typeof import('../api')>('../api')
  return { ...actual, listControlPlaneRuns: vi.fn() }
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
const childRun = {
  run_id: 'child-run',
  status: 'done',
  schema_version: 1,
  ownership: 'control_plane',
  revision: 1,
  history_state: 'visible',
  status_reason_code: 'completed',
  activity: 'inactive',
  evidence: {},
  plan_path: 'plans/done/child.md',
} as RunStatus

describe('GlobalRunOverview project context', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(api.listControlPlaneRuns).mockResolvedValue({ runs: [childRun], next_cursor: null, schema_version: 1 })
  })

  it('labels child history with the parent while opening the exact child ID', async () => {
    const onOpen = vi.fn()
    render(<GlobalRunOverview projects={[primary, child]} onOpen={onOpen} />)
    const row = await screen.findByRole('button', { name: /Primary project · Worktree: Feature worktree · Completed/ })
    fireEvent.click(row)
    expect(onOpen).toHaveBeenCalledWith('child', 'child-run')
    expect(api.listControlPlaneRuns).toHaveBeenCalledWith('child', expect.anything(), expect.anything())
  })
})
