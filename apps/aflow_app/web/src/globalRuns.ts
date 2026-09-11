import { useEffect, useState } from 'react'
import * as api from './api'
import { runPlanDisplayNameForRun, runPlanPath, statusLabel } from './runPresentation'
import type { RunStatus } from './types'

export const RECENT_LIMIT_KEY = 'aflow.recentRunsLimit'
let memoryLimit = 10
let storageUnavailable = false
export function validRecentLimit(value: unknown): number {
  if (typeof value !== 'number' && (typeof value !== 'string' || !/^\d+$/.test(value))) return 10
  const number = Number(value)
  return Number.isSafeInteger(number) && number > 0 ? number : 10
}
export function useRecentRunsLimit(): [number, (value: number) => void] {
  const read = () => { if (storageUnavailable) return memoryLimit; try { return validRecentLimit(localStorage.getItem(RECENT_LIMIT_KEY)) } catch { return memoryLimit } }
  const [value, setValue] = useState(read)
  useEffect(() => {
    const update = () => setValue(read())
    window.addEventListener('storage', update)
    window.addEventListener('aflow-recent-limit', update)
    return () => { window.removeEventListener('storage', update); window.removeEventListener('aflow-recent-limit', update) }
  }, [])
  return [value, next => {
    memoryLimit = validRecentLimit(next)
    try { localStorage.setItem(RECENT_LIMIT_KEY, String(memoryLimit)) } catch { storageUnavailable = true }
    setValue(memoryLimit)
    window.dispatchEvent(new Event('aflow-recent-limit'))
  }]
}

export interface GlobalRun { projectId: string; run: RunStatus }

/** Match every loaded, presentation-relevant field without changing records. */
export function matchesGlobalRun(row: GlobalRun, query: string, projectLabel = ''): boolean {
  const terms = query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean)
  if (terms.length === 0) return true
  const { run } = row
  const planPath = runPlanPath(run)
  const searchable = [
    projectLabel,
    row.projectId,
    runPlanDisplayNameForRun(run),
    planPath,
    run.run_id,
    statusLabel(run),
    run.workflow_name,
    run.team,
    run.current_step,
  ].filter((value): value is string => typeof value === 'string' && value.length > 0)
    .join(' ')
    .toLocaleLowerCase()
  return terms.every(term => searchable.includes(term))
}

export function isOngoing(run: RunStatus): boolean {
  if (['Completed', 'Failed', 'Could not start', 'Stopped', 'Interrupted', 'Needs attention'].includes(statusLabel(run))) return false
  return run.activity === 'active' || run.evidence.unit_active === true || ['paused', 'waiting_for_input', 'waiting_for_valid_override', 'awaiting_startup_answer'].includes(run.status)
}
function orderingTime(run: RunStatus): number {
  for (const value of [run.ended_at, run.evidence.manifest_created_at, run.started_at]) {
    if (typeof value === 'string' && Number.isFinite(Date.parse(value))) return Date.parse(value)
  }
  return 0
}
export function selectGlobalRuns(rows: GlobalRun[], limit: number, history: 'visible' | 'archived' | 'all' = 'visible'): { ongoing: GlobalRun[]; attention: GlobalRun[]; recent: GlobalRun[] } {
  const unique = [...new Map(rows.filter(row => row.run.history_state !== 'deleted' && (history === 'all' || (row.run.history_state ?? 'visible') === history)).map(row => [JSON.stringify([row.projectId, row.run.run_id]), row])).values()]
  unique.sort((a, b) => orderingTime(b.run) - orderingTime(a.run)
    || a.projectId.localeCompare(b.projectId) || a.run.run_id.localeCompare(b.run.run_id))
  return { ongoing: unique.filter(row => isOngoing(row.run)), attention: unique.filter(row => !isOngoing(row.run) && statusLabel(row.run) === 'Needs attention'), recent: unique.filter(row => !isOngoing(row.run) && statusLabel(row.run) !== 'Needs attention').slice(0, limit) }
}
export async function fetchGlobalRuns(projectIds: string[], signal: AbortSignal, history: 'visible' | 'archived' | 'all' = 'visible') {
  const byProject: Record<string, RunStatus[]> = {}
  const errors: string[] = []
  let next = 0
  await Promise.all(Array.from({ length: Math.min(4, projectIds.length) }, async () => {
    while (next < projectIds.length && !signal.aborted) {
      const id = projectIds[next++]
      try {
        const runs: RunStatus[] = []
        let cursor: string | undefined
        const seen = new Set<string>()
        do {
          const page = await api.listControlPlaneRuns(id, { limit: 100, history, ...(cursor ? { cursor } : {}) }, { signal })
          if (signal.aborted) return
          runs.push(...page.runs)
          cursor = page.next_cursor ?? undefined
          if (cursor && seen.has(cursor)) throw new Error('Repeated cursor')
          if (cursor) seen.add(cursor)
        } while (cursor)
        byProject[id] = runs
      } catch { if (!signal.aborted) errors.push(id) }
    }
  }))
  return { byProject, errors }
}
