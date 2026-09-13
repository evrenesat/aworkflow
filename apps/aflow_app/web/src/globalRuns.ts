import { useEffect, useState } from 'react'
import * as api from './api'
import { runCanonicalIdentity, runPlanDisplayNameForRun, runPlanPath, statusLabel } from './runPresentation'
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

export type GlobalRunProgressState = 'loading' | 'complete' | 'failed'

export interface GlobalRunProgressUpdate {
  projectId: string
  runs: RunStatus[]
  state: GlobalRunProgressState
}

export type GlobalRunProgressCallback = (update: GlobalRunProgressUpdate) => void

export interface GlobalRunSnapshot {
  run_id: string
  status: string | null
  activity: 'active' | 'inactive' | 'unknown'
  status_reason_code: string | null
  ownership: string | null
  revision: number | null
  current_step: string | null
  turns_completed: number | null
  max_turns: number | null
  started_at: string | null
  ended_at: string | null
  launch_phase: string | null
  plan_path: string | null
  workflow_name: string | null
  team: string | null
  original_plan_display_name: string | null
  original_plan_path: string | null
  history_state: 'visible' | 'archived' | 'deleted'
  history_revision: number
  unit_active: boolean | null
}

function nullable<T extends string | number | boolean>(value: T | null | undefined): T | null {
  return value ?? null
}

function identityValue(value: string | null | undefined): string | null {
  return typeof value === 'string' && value.trim() ? value : null
}

/** Explicitly compare only the raw/detail fields owned by the overview. */
export function globalRunSnapshot(run: RunStatus): GlobalRunSnapshot {
  const canonical = runCanonicalIdentity(run)
  const activity = run.activity === 'active' || run.activity === 'inactive' ? run.activity : 'unknown'
  const unitActive = run.evidence?.unit_active
  return {
    run_id: run.run_id,
    status: nullable(run.status),
    activity,
    status_reason_code: nullable(run.status_reason_code),
    ownership: nullable(run.ownership),
    revision: nullable(run.revision),
    current_step: nullable(run.current_step),
    turns_completed: nullable(run.turns_completed),
    max_turns: nullable(run.max_turns),
    started_at: nullable(run.started_at),
    ended_at: nullable(run.ended_at),
    launch_phase: nullable(run.launch_phase),
    plan_path: nullable(run.plan_path),
    workflow_name: nullable(run.workflow_name),
    team: nullable(run.team),
    original_plan_display_name: canonical.displayName,
    original_plan_path: canonical.path,
    history_state: run.history_state ?? 'visible',
    history_revision: run.history_revision ?? 0,
    unit_active: unitActive === true || unitActive === false ? unitActive : null,
  }
}

/** Reject a detail response whose authoritative snapshot no longer matches. */
export function matchesGlobalRunSnapshot(expected: RunStatus, actual: RunStatus): boolean {
  const expectedSnapshot = globalRunSnapshot(expected)
  const actualSnapshot = globalRunSnapshot(actual)
  return (Object.keys(expectedSnapshot) as Array<keyof GlobalRunSnapshot>).every(key => (
    expectedSnapshot[key] === actualSnapshot[key]
  ))
}

/** A rich response may not replace the raw row's canonical original identity. */
export function matchesGlobalRunProgressIdentity(expected: RunStatus, actual: RunStatus): boolean {
  const canonical = runCanonicalIdentity(expected)
  return (
    identityValue(actual.progress?.original_plan_display_name) === canonical.displayName
    && identityValue(actual.progress?.original_plan_path) === canonical.path
  )
}

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
    run.original_plan_path,
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
export async function fetchGlobalRuns(
  projectIds: string[],
  signal: AbortSignal,
  history: 'visible' | 'archived' | 'all' = 'visible',
  onProgress?: GlobalRunProgressCallback,
) {
  const byProject: Record<string, RunStatus[]> = {}
  const errors: string[] = []
  let next = 0
  await Promise.all(Array.from({ length: Math.min(4, projectIds.length) }, async () => {
    while (next < projectIds.length && !signal.aborted) {
      const id = projectIds[next++]
      const runs: RunStatus[] = []
      try {
        let cursor: string | null = null
        const seen = new Set<string>()
        do {
          const page = await api.listControlPlaneRuns(id, { limit: 100, history, include_progress: false, ...(cursor !== null ? { cursor } : {}) }, { signal })
          if (signal.aborted) return
          runs.push(...page.runs)
          cursor = page.next_cursor
          onProgress?.({ projectId: id, runs: [...runs], state: cursor === null ? 'complete' : 'loading' })
          if (cursor !== null && seen.has(cursor)) throw new Error('Repeated cursor')
          if (cursor !== null) seen.add(cursor)
        } while (cursor !== null)
        byProject[id] = runs
      } catch {
        if (!signal.aborted) {
          onProgress?.({ projectId: id, runs: [...runs], state: 'failed' })
          errors.push(id)
        }
      }
    }
  }))
  return { byProject, errors }
}
