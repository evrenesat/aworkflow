import type { GuidedFormProjection, GuidedTeamSummary, RunStatus } from './types'
import { formatMachineLabel } from './label'
import { groupTeamFamilies, type TeamFamilyGroup } from './teamFamilies'

export const terminalStatuses = new Set(['completed', 'done', 'failed', 'owner_stopped', 'interrupted'])

/**
 * Return the short, user-facing name for a run's plan without changing the
 * exact path kept in the run identity or technical details.
 */
export function runPlanDisplayName(planPath: string | null | undefined, runId: string): string {
  const suppliedPath = typeof planPath === 'string' ? planPath.trim() : ''
  const basename = suppliedPath.split(/[\\/]+/).pop()?.trim() ?? ''
  const withoutMarkdown = basename.replace(/\.md$/i, '').trim()
  const readable = withoutMarkdown.replace(/[-_]+/g, ' ').replace(/\s+/g, ' ').trim()
  if (!readable) return runId
  return readable.charAt(0).toUpperCase() + readable.slice(1)
}

/** Resolve the exact plan path from the current run contract or its evidence. */
export function runPlanPath(run: RunStatus): string | null {
  if (typeof run.plan_path === 'string' && run.plan_path.trim()) return run.plan_path
  const evidencePath = run.evidence.plan_path
  if (typeof evidencePath === 'string' && evidencePath.trim()) return evidencePath
  const canonicalPath = run.progress?.original_plan_path
  return typeof canonicalPath === 'string' && canonicalPath.trim() ? canonicalPath : null
}

/** Use the canonical original identity when the legacy list fields are absent. */
export function runPlanDisplayNameForRun(run: RunStatus): string {
  const canonicalName = run.progress?.original_plan_display_name?.trim()
  return runPlanDisplayName(canonicalName || runPlanPath(run), run.run_id)
}

/**
 * The launch UI uses the same group projection as Team Families settings.
 * Capability-only teams are retained as standalone choices until a saved
 * guided projection can describe their family metadata.
 */
export function launchTeamFamilyGroups(
  projection: GuidedFormProjection | null,
  advertisedTeamIds: readonly string[] = [],
): TeamFamilyGroup[] {
  const groups = projection ? groupTeamFamilies(projection).groups.map((group) => ({
    ...group,
    memberIds: [...group.memberIds],
    route: [...group.route],
  })) : []
  const knownTeamIds = new Set(groups.flatMap((group) => group.memberIds))
  for (const teamId of [...new Set(advertisedTeamIds)].sort()) {
    if (knownTeamIds.has(teamId)) continue
    groups.push({
      kind: 'standalone',
      rootId: teamId,
      memberIds: [teamId],
      route: [teamId],
      simpleRoute: false,
      reason: null,
      displayName: null,
    })
    knownTeamIds.add(teamId)
  }
  return groups.sort((left, right) => left.rootId.localeCompare(right.rootId))
}

/** Return stable family member order for selectors, including invalid graphs. */
export function launchTeamFamilyRoute(group: TeamFamilyGroup): string[] {
  return [...group.route, ...group.memberIds.filter((teamId) => !group.route.includes(teamId))]
}

/**
 * Follow only declared upgrade_to links from the selected launch team. A
 * family membership or extends relationship never creates an escalation edge.
 */
export function launchTeamUpgradeRoute(
  projection: GuidedFormProjection | null,
  startTeamId: string,
): string[] | null {
  if (!projection?.teams[startTeamId]) return null
  const route: string[] = []
  const seen = new Set<string>()
  let current: string | null = startTeamId
  const limit = Object.keys(projection.teams).length + 1
  while (current && route.length < limit) {
    if (seen.has(current)) return null
    const summary: GuidedTeamSummary | undefined = projection.teams[current]
    if (!summary) return null
    seen.add(current)
    route.push(current)
    const next: string | null = summary.upgrade_to ?? null
    if (!next) return route
    if (!projection.teams[next]) return null
    current = next
  }
  return null
}

export function launchTeamFamilyLabel(group: TeamFamilyGroup): string {
  return group.displayName?.trim() || formatMachineLabel(group.rootId)
}

export function launchTeamStageLabel(
  projection: GuidedFormProjection | null,
  group: TeamFamilyGroup,
  teamId: string,
): string {
  if (group.kind === 'family' && teamId === group.rootId) return 'Base'
  return projection?.teams[teamId]?.display_name?.trim() || formatMachineLabel(teamId)
}

function effectiveTeamRoles(projection: GuidedFormProjection | null, teamId: string): Record<string, string> {
  const summary = projection?.teams[teamId]
  return summary?.effective_roles ?? summary?.roles ?? {}
}

/** Compact role text used beside a family choice; identity stays in the value. */
export function launchTeamRoleSummary(
  projection: GuidedFormProjection | null,
  teamId: string,
): string {
  const roles = effectiveTeamRoles(projection, teamId)
  const globalRoles = projection?.roles ?? {}
  const worker = roles.worker ?? globalRoles.worker
  const reviewer = roles.reviewer ?? globalRoles.reviewer
  return [
    `Worker: ${worker ?? 'not assigned'}`,
    `Reviewer: ${reviewer ?? 'not assigned'}`,
  ].join(' · ')
}

/** Compact family metadata for searchable launch options. */
export function launchTeamFamilyHint(
  projection: GuidedFormProjection | null,
  group: TeamFamilyGroup,
): string {
  const roles = launchTeamRoleSummary(projection, group.rootId)
  if (group.kind === 'family' && !group.simpleRoute) {
    const members = group.memberIds
      .map((teamId) => launchTeamStageLabel(projection, group, teamId))
      .join(', ')
    const routes = [...new Map(group.memberIds.map((teamId) => {
      const route = launchTeamUpgradeRoute(projection, teamId)
      return [route?.join('\u0000') ?? '', route] as const
    })).values()]
      .filter((route): route is string[] => Boolean(route && route.length > 1))
      .map((route) => route.map((teamId) => launchTeamStageLabel(projection, group, teamId)).join(' → '))
    return `${roles} · Members: ${members}${routes.length ? ` · Configured routes: ${routes.join('; ')}` : ''}`
  }
  const route = launchTeamUpgradeRoute(projection, group.rootId)
    ?? (group.route.length ? group.route : group.memberIds)
  return `${roles} · Upgrade route: ${route.map((teamId) => launchTeamStageLabel(projection, group, teamId)).join(' → ')}`
}

export function statusLabel(run: RunStatus): string {
  if (run.status === 'failed' && run.worker_exit && !run.evidence.has_run_metadata) return 'Could not start'
  if (run.status_reason_code === 'startup_failed') return 'Could not start'
  if (run.ownership === 'legacy' && !terminalStatuses.has(run.status)) return 'Needs attention'
  if (run.status === 'needs_attention') {
    const failure = run.evidence.startup_failure as { stage?: string } | null
    return failure?.stage === 'preparation' && run.evidence.no_agent_started ? 'Could not start' : 'Needs attention'
  }
  if (run.status === 'awaiting_startup_answer') return 'Input needed'
  if (['manifest_only', 'launch_requested', 'unit_started', 'launch_started'].includes(run.status)) return run.activity === 'active' || run.evidence.unit_active === true ? 'Starting' : 'Needs attention'
  if (run.status === 'owner_stopped') return 'Stopped'
  if (run.status === 'done') return 'Completed'
  return formatMachineLabel(run.status)
}

export function executionDuration(run: RunStatus, now: number): string | null {
  if (!run.started_at) return null
  const start = Date.parse(run.started_at)
  const end = terminalStatuses.has(run.status)
    ? Date.parse(run.ended_at ?? '')
    : run.status === 'running' && run.ownership === 'control_plane' ? now : NaN
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return null
  const seconds = Math.floor((end - start) / 1000)
  return seconds < 60 ? `${seconds}s` : seconds < 3600 ? `${Math.floor(seconds / 60)}m ${seconds % 60}s` : `${Math.floor(seconds / 3600)}h ${Math.floor(seconds % 3600 / 60)}m`
}
