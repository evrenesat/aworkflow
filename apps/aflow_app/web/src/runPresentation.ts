import type {
  GuidedFormProjection,
  GuidedTeamSummary,
  RunProgressCount,
  RunProgressDeliveryStage,
  RunProgressExecutor,
  RunProgressSummary,
  RunEvent,
  RunStatus,
} from './types'
import { formatMachineLabel } from './label'
import { groupTeamFamilies, type TeamFamilyGroup } from './teamFamilies'

export const terminalStatuses = new Set(['completed', 'done', 'failed', 'owner_stopped', 'interrupted'])

export interface RunPlanPresentation {
  label: string
  date: string | null
  machineDerived: boolean
}

export interface RunCanonicalIdentity {
  displayName: string | null
  path: string | null
}

function validCount(count: RunProgressCount | null | undefined): number | null {
  return count?.coverage !== 'unavailable'
    && typeof count?.value === 'number'
    && Number.isSafeInteger(count.value)
    && count.value >= 0
    ? count.value
    : null
}

function partialCount(count: RunProgressCount | null | undefined): boolean {
  return count?.coverage === 'partial'
}

function pluralizeCheckpoint(value: number): string {
  return value === 1 ? 'checkpoint' : 'checkpoints'
}

/**
 * Describe approval separately from recorded completion.  A missing approved
 * count is never rendered as zero or as an apparent numerator.
 */
export function checkpointApprovalText(progress: RunProgressSummary): string {
  if (progress.availability === 'not_applicable') return 'Non-checkpoint workflow'
  if (progress.availability === 'unavailable') return 'Checkpoint progress unavailable'

  const approved = validCount(progress.approved_checkpoints)
  const total = validCount(progress.total_checkpoints)
  const approvedLabel = approved === null ? null : `${partialCount(progress.approved_checkpoints) ? 'At least ' : ''}${approved}`
  const totalLabel = total === null ? null : `${partialCount(progress.total_checkpoints) ? 'at least ' : ''}${total}`

  if (approvedLabel !== null && totalLabel !== null) {
    return `${approvedLabel} of ${totalLabel} ${pluralizeCheckpoint(total ?? 0)} approved`
  }
  if (totalLabel !== null) return `${totalLabel} ${pluralizeCheckpoint(total ?? 0)} · approval unknown`
  if (approvedLabel !== null) return `${approvedLabel} approved · total unknown`
  return 'Approval progress unknown'
}

/** Keep the active checkpoint position concise enough for the run overview. */
export function runCurrentWorkText(progress: RunProgressSummary, phaseOverride: string | null = null): string {
  const phase = [phaseOverride, progress.phase, progress.activity, progress.run_status]
    .find((value): value is string => typeof value === 'string' && value.trim().length > 0)
  if (progress.availability === 'not_applicable') {
    return phase ? formatMachineLabel(phase) : 'Non-checkpoint workflow'
  }
  const ordinal = progress.current_checkpoint_ordinal
  const total = validCount(progress.total_checkpoints)
  const rawTitle = typeof progress.current_checkpoint_title === 'string' && progress.current_checkpoint_title.trim()
    ? progress.current_checkpoint_title.trim()
    : null
  if (typeof ordinal === 'number' && Number.isSafeInteger(ordinal) && ordinal > 0) {
    const position = `CP${ordinal}${total !== null ? ` of ${total}` : ''}`
    const title = rawTitle?.replace(new RegExp(`^checkpoint\\s+${ordinal}\\s*:\\s*`, 'i'), '') || rawTitle
    return `${position}${phase ? ` · ${formatMachineLabel(phase)}` : ''}${title ? ` — ${title}` : ''}`
  }
  return phase ? `${formatMachineLabel(phase)} · current checkpoint not reported` : 'Current checkpoint not reported'
}

export interface RunEventPresentation {
  event: RunEvent
  label: string
  summary: string | null
  detail: string
  recordedAt: string | null
  facts: string[]
  hasPayload: boolean
}

const RUN_EVENT_TEXT_FIELDS = [
  'failure_reason',
  'end_reason',
  'summary',
  'outcome',
  'reason',
  'error',
  'message',
  'semantic_summary',
  'result',
] as const

function eventText(value: unknown, limit = 240): string | null {
  if (typeof value !== 'string' || !value.trim()) return null
  const text = value.trim().replace(/\s+/g, ' ')
  return text.length <= limit ? text : `${text.slice(0, limit - 1)}…`
}

function eventTextField(data: Record<string, unknown>, ...keys: string[]): string | null {
  for (const key of keys) {
    const value = eventText(data[key], 120)
    if (value) return value
  }
  return null
}

function eventTurnNumber(data: Record<string, unknown>): number | null {
  const value = data.turn_number
  return typeof value === 'number' && Number.isSafeInteger(value) && value > 0 ? value : null
}

/** Keep recorded activity facts compact without changing their raw values. */
export function runEventFacts(event: RunEvent): string[] {
  const data = event.data ?? {}
  const step = eventTextField(data, 'step_name', 'step', 'current_step')
  const role = eventTextField(data, 'step_role', 'role')
  const turn = eventTurnNumber(data)
  return [
    step ? formatMachineLabel(step) : null,
    role ? formatMachineLabel(role) : null,
    turn !== null ? `turn ${turn}` : null,
  ].filter((value): value is string => Boolean(value))
}

/** Format an event's recorded time for the short activity list. */
export function formatRunEventTime(value: string | null | undefined): string | null {
  if (typeof value !== 'string' || !value.trim()) return null
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return null
  return date.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })
}

function isPreferredRunEvent(event: RunEvent): boolean {
  return /(?:turn|checkpoint|review|repair|complet|finish|fail|error|reject|stop|interrupt|cancel|recover|control)/i.test(event.event_type)
}

function isRunEventNoise(event: RunEvent): boolean {
  return /(?:heartbeat|status[_ -]?(?:changed|observed|observation)|daemon[_ -]?(?:start|started|attempt|unit)|unit[_ -]?(?:start|started)|launch[_ -]?(?:start|started)|run[_ -]?started|health[_ -]?check|reconcile|poll)/i.test(event.event_type)
}

/** Return the newest bounded set while omitting lifecycle noise when work exists. */
export function meaningfulRunEvents(events: RunEvent[], limit = 3): RunEvent[] {
  if (limit <= 0) return []
  const valid = events
    .filter(event => typeof event.event_type === 'string' && event.event_type.trim().length > 0)
  const usable = valid.filter(event => !isRunEventNoise(event))
  const preferred = usable.filter(isPreferredRunEvent)
  const selected = preferred.length > 0 ? preferred : usable
  const withoutDuplicateLifecycle = selected.filter(event => {
    if (!/^run_(?:failed|completed|finished)$/.test(event.event_type)) return true
    const turn = eventTurnNumber(event.data ?? {})
    return turn === null || !selected.some(other => other !== event
      && other.event_type === 'turn_finished'
      && eventTurnNumber(other.data ?? {}) === turn)
  })
  return withoutDuplicateLifecycle
    .slice(-limit)
    .reverse()
}

type RunEventOutcome = 'failed' | 'stopped' | 'successful' | null

function runEventOutcome(event: RunEvent): RunEventOutcome {
  const eventType = event.event_type.toLowerCase()
  const status = typeof event.data?.status === 'string' ? event.data.status.toLowerCase() : ''
  if (/(?:fail|error|reject)/.test(eventType) || ['failed', 'error', 'rejected'].includes(status)) return 'failed'
  if (/(?:stop|interrupt|cancel)/.test(eventType) || ['stopped', 'interrupted', 'cancelled', 'canceled'].includes(status)) return 'stopped'
  if (/(?:finish|complet|approv|success|done)/.test(eventType) || ['completed', 'approved', 'succeeded', 'done', 'accepted'].includes(status)) return 'successful'
  return null
}

function terminalResultUnavailable(outcome: RunEventOutcome): string | null {
  if (outcome === 'failed') return 'Failure reason unavailable.'
  if (outcome === 'stopped') return 'Stop reason unavailable.'
  if (outcome === 'successful') return 'Recorded result unavailable.'
  return null
}

/** Present one recorded event using only text already stored in its payload. */
export function presentRunEvent(event: RunEvent): RunEventPresentation {
  const label = formatMachineLabel(event.event_type) || 'Recorded update'
  const storedSummary = RUN_EVENT_TEXT_FIELDS
    .map(field => {
      const value = eventText(event.data?.[field])
      return value && (field === 'failure_reason' || field === 'end_reason') ? formatMachineLabel(value) : value
    })
    .find((value): value is string => value !== null && !/^#|\s#+\s/.test(value)) ?? null
  const summary = storedSummary ?? terminalResultUnavailable(runEventOutcome(event))
  return {
    event,
    label,
    summary,
    detail: JSON.stringify({ event_type: event.event_type, sequence: event.sequence, timestamp: event.timestamp, data: event.data }, null, 2),
    recordedAt: formatRunEventTime(event.timestamp),
    facts: runEventFacts(event),
    hasPayload: Object.keys(event.data ?? {}).length > 0,
  }
}

/** Keep the current executor identity available without exposing raw payloads. */
export function runExecutorFacts(executor: RunProgressExecutor | null): string[] {
  if (!executor) return []
  const role = executor.role?.trim() ? formatMachineLabel(executor.role) : null
  const team = executor.team?.trim() ? formatMachineLabel(executor.team) : null
  const model = (executor.model_display?.trim() || executor.model?.trim()) || null
  const turn = executor.turn_number !== null && Number.isSafeInteger(executor.turn_number) && executor.turn_number > 0
    ? `turn ${executor.turn_number}`
    : null
  return [role, team, model, turn].filter((value): value is string => Boolean(value))
}

/** Only a recorded failed stage is consequential enough for the collapsed view. */
export function deliveryIssueText(stages: RunProgressDeliveryStage[]): string | null {
  const failed = stages.find(stage => stage.status === 'failed')
  if (!failed) return null
  const stage = formatMachineLabel(failed.stage) || 'Delivery stage'
  const reason = eventText(failed.reason, 200)
  return `${stage} failed${reason ? ` — ${reason}` : ''}`
}

/** Find the latest event that records a finished or otherwise terminal result. */
export function latestRunResultEvent(events: RunEvent[]): RunEvent | null {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index]
    const eventType = event.event_type.toLowerCase()
    const status = typeof event.data?.status === 'string' ? event.data.status.toLowerCase() : ''
    if (
      /(?:finished|completed|failed|rejected|approved|error|stopped|interrupted)/.test(eventType)
      || ['completed', 'failed', 'rejected', 'approved', 'stopped', 'interrupted', 'done'].includes(status)
    ) return event
  }
  return null
}

/** Keep a nullable turn limit explicit without turning missing usage into zero. */
export function runTurnBudgetText(run: RunStatus): string {
  const used = typeof run.turns_completed === 'number'
    && Number.isSafeInteger(run.turns_completed)
    && run.turns_completed >= 0
    ? run.turns_completed
    : null
  const limit = typeof run.max_turns === 'number'
    && Number.isSafeInteger(run.max_turns)
    && run.max_turns > 0
    ? run.max_turns
    : null

  if (used !== null && limit !== null) return `${used} of ${limit} turns used`
  if (used !== null) return `${used} turns used · turn limit unknown`
  if (limit !== null) return `${limit}-turn limit · turns used unknown`
  return 'Turns used unknown · turn limit unknown'
}

/** Keep only a known turn fact when a compact preview has room for it. */
export function runKnownTurnBudgetText(run: RunStatus): string | null {
  const used = typeof run.turns_completed === 'number'
    && Number.isSafeInteger(run.turns_completed)
    && run.turns_completed >= 0
    ? run.turns_completed
    : null
  const limit = typeof run.max_turns === 'number'
    && Number.isSafeInteger(run.max_turns)
    && run.max_turns > 0
    ? run.max_turns
    : null

  if (used !== null && limit !== null) return `${used} of ${limit} turns used`
  if (used !== null) return `${used} turns used`
  if (limit !== null) return `${limit}-turn limit`
  return null
}

/** Format a valid timestamp in the browser's local timezone with its zone visible. */
export function formatLocalTimestamp(value: string | null | undefined): string | null {
  if (typeof value !== 'string' || !value.trim()) return null
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
    second: '2-digit',
    timeZoneName: 'short',
  })
}

function validDateSuffix(value: string): { prefix: string; date: string } | null {
  const match = /^(.*?)(?:[-_\s])(\d{8})$/.exec(value)
  if (!match || !match[1].trim()) return null
  const [, prefix, rawDate] = match
  const year = Number(rawDate.slice(0, 4))
  const month = Number(rawDate.slice(4, 6))
  const day = Number(rawDate.slice(6, 8))
  const date = new Date(Date.UTC(year, month - 1, day))
  if (date.getUTCFullYear() !== year || date.getUTCMonth() !== month - 1 || date.getUTCDate() !== day) return null
  return {
    prefix: prefix.trim(),
    date: date.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric', timeZone: 'UTC' }),
  }
}

function planNamePresentation(planName: string | null | undefined, runId: string, machineDerived: boolean): RunPlanPresentation {
  const suppliedName = typeof planName === 'string' ? planName.trim() : ''
  const basename = suppliedName.split(/[\\/]+/).pop()?.trim() ?? ''
  const withoutMarkdown = basename.replace(/\.md$/i, '').trim()
  if (!withoutMarkdown) return { label: runId, date: null, machineDerived }

  const suffix = machineDerived ? validDateSuffix(withoutMarkdown) : null
  const readableSource = suffix?.prefix ?? withoutMarkdown
  const readable = readableSource.replace(/[-_]+/g, ' ').replace(/\s+/g, ' ').trim()
  if (!readable) return { label: runId, date: suffix?.date ?? null, machineDerived }
  return {
    label: readable.charAt(0).toUpperCase() + readable.slice(1),
    date: suffix?.date ?? null,
    machineDerived,
  }
}

function planBasename(planPath: string | null | undefined): string | null {
  if (typeof planPath !== 'string' || !planPath.trim()) return null
  return planPath.split(/[\\/]+/).pop()?.trim() || null
}

function canonicalIdentityValue(value: string | null | undefined): string | null {
  return typeof value === 'string' && value.trim() ? value : null
}

/** Prefer the full progress identity, then the compatible raw-list identity. */
export function runCanonicalIdentity(run: RunStatus): RunCanonicalIdentity {
  const progressName = canonicalIdentityValue(run.progress?.original_plan_display_name)
  const progressPath = canonicalIdentityValue(run.progress?.original_plan_path)
  if (progressName || progressPath) {
    return { displayName: progressName, path: progressPath }
  }
  return {
    displayName: canonicalIdentityValue(run.original_plan_display_name),
    path: canonicalIdentityValue(run.original_plan_path),
  }
}

/**
 * Return the short, user-facing name for a run's plan without changing the
 * exact path kept in the run identity or technical details.
 */
export function runPlanDisplayName(planPath: string | null | undefined, runId: string): string {
  return runPlanPresentation(planPath, runId).label
}

/** Present a path-derived plan name and its validated machine date suffix. */
export function runPlanPresentation(planPath: string | null | undefined, runId: string): RunPlanPresentation {
  return planNamePresentation(planPath, runId, true)
}

/** Resolve the exact plan path from the current run contract or its evidence. */
export function runPlanPath(run: RunStatus): string | null {
  if (typeof run.plan_path === 'string' && run.plan_path.trim()) return run.plan_path
  const evidencePath = run.evidence.plan_path
  if (typeof evidencePath === 'string' && evidencePath.trim()) return evidencePath
  return runCanonicalIdentity(run).path
}

/** Use the canonical original identity when the legacy list fields are absent. */
export function runPlanDisplayNameForRun(run: RunStatus): string {
  return runPlanPresentationForRun(run).label
}

/**
 * Prefer the canonical display name, but only normalize it when it is the
 * path-derived basename.  A custom/user-authored name remains byte-for-byte
 * unchanged at this presentation boundary.
 */
export function runPlanPresentationForRun(run: RunStatus): RunPlanPresentation {
  const canonical = runCanonicalIdentity(run)
  const canonicalName = canonical.displayName || ''
  const canonicalPath = canonical.path
  if (canonicalName) {
    const canonicalBasename = planBasename(canonicalPath)
    const isPathDerived = canonicalBasename !== null
      && canonicalBasename.localeCompare(canonicalName, undefined, { sensitivity: 'case' }) === 0
    if (isPathDerived) return planNamePresentation(canonicalName, run.run_id, true)
    if (canonicalBasename === null) return { label: canonicalName, date: null, machineDerived: false }
    return { label: canonicalName, date: null, machineDerived: false }
  }
  return runPlanPresentation(runPlanPath(run), run.run_id)
}

/** A compact visual identity that never changes the exact run ID being copied or requested. */
export function shortRunId(runId: string, maxLength = 18): string {
  if (runId.length <= maxLength) return runId
  const sideLength = Math.max(4, Math.floor((maxLength - 1) / 2))
  return `${runId.slice(0, sideLength)}…${runId.slice(-sideLength)}`
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

export type RunDisplayCategory =
  | 'active'
  | 'completed'
  | 'failure'
  | 'input-needed'
  | 'actionable-attention'
  | 'outcome-unrecorded'
  | 'stopped'
  | 'interrupted'
  | 'other'

export interface RunDisplayProjection {
  category: RunDisplayCategory
  label: string
  tone: 'neutral' | 'attention' | 'danger' | 'muted'
}

/**
 * Match only the fully evidenced historical gap; unknown cases stay visible as attention.
 * `evidence.can_resume` is an optional list preview: history list rows omit it because
 * computing it costs one project-wide admission scan per row. An omitted preview is
 * unknown, not a denial, so the missing-outcome shape stands on its own evidence while an
 * explicit `can_resume === true` keeps the row actionable. This projection never
 * authorizes a resume action; detail and action admission recheck eligibility.
 */
function isUnrecordedOutcome(run: RunStatus): boolean {
  const activityIsInactiveOrUnknown = run.activity === 'inactive' || run.activity === 'unknown'
  const resumePreviewIsNotActionable = run.evidence.can_resume !== true
  return run.status === 'needs_attention'
    && activityIsInactiveOrUnknown
    && run.evidence.unit_observation === 'missing'
    && run.evidence.unit_active !== true
    && run.evidence.has_run_metadata === false
    && resumePreviewIsNotActionable
    && run.status_reason_code === 'unit_missing'
}

function rawStatusLabel(run: RunStatus): string {
  if (run.status === 'failed' && run.worker_exit && !run.evidence.has_run_metadata) return 'Could not start'
  if (run.status_reason_code === 'startup_failed') return 'Could not start'
  if (run.status === 'awaiting_startup_answer') return 'Input needed'
  if (run.ownership === 'legacy' && !terminalStatuses.has(run.status)) return 'Needs attention'
  if (run.status === 'needs_attention') {
    const failure = run.evidence.startup_failure as { stage?: string } | null
    return failure?.stage === 'preparation' && run.evidence.no_agent_started ? 'Could not start' : 'Needs attention'
  }
  if (['manifest_only', 'launch_requested', 'unit_started', 'launch_started'].includes(run.status)) return run.activity === 'active' || run.evidence.unit_active === true ? 'Starting' : 'Needs attention'
  if (run.status === 'owner_stopped') return 'Stopped'
  if (run.status === 'done') return 'Completed'
  return formatMachineLabel(run.status)
}

/** Shared display projection for project lists, global history, and run details. */
export function runDisplayProjection(run: RunStatus): RunDisplayProjection {
  if (isUnrecordedOutcome(run)) {
    return { category: 'outcome-unrecorded', label: 'Outcome not recorded', tone: 'muted' }
  }

  const label = rawStatusLabel(run)
  let category: RunDisplayCategory
  if (label === 'Could not start' || label === 'Failed') category = 'failure'
  else if (label === 'Input needed' || ['waiting_for_input', 'waiting_for_valid_override'].includes(run.status)) category = 'input-needed'
  else if (label === 'Needs attention') category = 'actionable-attention'
  else if (label === 'Completed') category = 'completed'
  else if (label === 'Stopped') category = 'stopped'
  else if (label === 'Interrupted') category = 'interrupted'
  else if (isRunActive(run)) category = 'active'
  else category = 'other'

  const tone = category === 'failure'
    ? 'danger'
    : category === 'input-needed' || category === 'actionable-attention'
      ? 'attention'
      : 'neutral'
  return { category, label, tone }
}

export function statusLabel(run: RunStatus): string {
  return runDisplayProjection(run).label
}

export function isRunActive(run: RunStatus): boolean {
  return run.activity === 'active'
    || run.evidence.unit_active === true
    || ['running', 'paused', 'waiting_for_input', 'waiting_for_valid_override', 'awaiting_startup_answer'].includes(run.status)
}

/** Terminal records may retain historical evidence, but no current work. */
export function isTerminalInactiveRun(run: RunStatus): boolean {
  return terminalStatuses.has(run.status) && !isRunActive(run)
}

/** Prefer the canonical run finish boundary; worker exit is a failure boundary fallback. */
export function runFinishTimestamp(run: RunStatus): string | null {
  const candidates = [run.ended_at, run.worker_exit?.exited_at]
  for (const candidate of candidates) {
    if (typeof candidate === 'string' && Number.isFinite(Date.parse(candidate))) return candidate
  }
  return null
}

export function runFinishText(run: RunStatus): string | null {
  const finishedAt = runFinishTimestamp(run)
  const formatted = formatLocalTimestamp(finishedAt)
  return formatted ? `Finished ${formatted}` : null
}

/** One compact line for a collapsed row; detailed evidence belongs in Details. */
export function runActivityText(run: RunStatus): string {
  if (isTerminalInactiveRun(run)) return runFinishText(run) ?? 'Finish time not reported'
  const context = [
    run.workflow_name ? formatMachineLabel(run.workflow_name) : null,
    run.team ? formatMachineLabel(run.team) : null,
    run.current_step ? formatMachineLabel(run.current_step) : null,
  ].filter((value): value is string => Boolean(value))
  if (context.length > 0) return context.join(' · ')
  return isRunActive(run) ? 'Active work in progress' : 'Activity not reported'
}

export function runDurationText(run: RunStatus, now = Date.now()): string {
  const duration = executionDuration(run, now)
  return duration ? `Duration ${duration}` : 'Duration not reported'
}

/** A single compact notice for the collapsed surface; reason detail stays below. */
export function progressHistoryNotice(progress: RunProgressSummary): string | null {
  if (progress.availability !== 'partial'
    && !progress.reason_codes.includes('history_partial')
    && !progress.reason_codes.includes('evidence_truncated')) return null
  return 'Some progress history is partial · see Details for limits'
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
