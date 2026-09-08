import { useEffect, useState } from 'react'
import * as api from './api'
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
export function isOngoing(run: RunStatus): boolean {
  if (run.ownership !== 'control_plane') return false
  if (run.evidence.unit_active === true) return true
  if (run.evidence.startup_failure) return false
  if (run.status === 'manifest_only' && run.evidence.startup_state === 'preparing') return true
  return ['running', 'paused', 'waiting_for_input', 'waiting_for_valid_override', 'awaiting_startup_answer', 'launch_requested', 'unit_started', 'launch_started'].includes(run.status)
}
function orderingTime(run: RunStatus): number {
  for (const value of [run.ended_at, run.evidence.manifest_created_at, run.started_at]) {
    if (typeof value === 'string' && Number.isFinite(Date.parse(value))) return Date.parse(value)
  }
  return 0
}
export function selectGlobalRuns(rows: GlobalRun[], limit: number): { ongoing: GlobalRun[]; recent: GlobalRun[] } {
  const unique = [...new Map(rows.map(row => [JSON.stringify([row.projectId, row.run.run_id]), row])).values()]
  unique.sort((a, b) => orderingTime(b.run) - orderingTime(a.run)
    || a.projectId.localeCompare(b.projectId) || a.run.run_id.localeCompare(b.run.run_id))
  return { ongoing: unique.filter(row => isOngoing(row.run)), recent: unique.filter(row => !isOngoing(row.run)).slice(0, limit) }
}
export async function fetchGlobalRuns(projectIds: string[], signal: AbortSignal) {
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
          const page = await api.listControlPlaneRuns(id, { limit: 100, ...(cursor ? { cursor } : {}) }, { signal })
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
