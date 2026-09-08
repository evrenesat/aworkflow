import type { GuidedConfigAction, GuidedFormProjection } from './types'

/**
 * Net semantic changes only; identities accompany edited fields, never loaded
 * tables. New teams are emitted (add_team) before any edit that depends on
 * them, so one actions batch applies in order without unknown-team rejections.
 */
export function settingsActions(base: GuidedFormProjection, draft: GuidedFormProjection): GuidedConfigAction[] {
  const actions: GuidedConfigAction[] = []
  for (const team of Object.keys(draft.teams)) {
    if (!base.teams[team]) actions.push({ type: 'add_team', team })
  }
  for (const [harness, profiles] of Object.entries(draft.harnesses)) {
    for (const [profile, value] of Object.entries(profiles)) {
      const old = base.harnesses[harness]?.[profile]
      const action: Extract<GuidedConfigAction, { type: 'upsert_profile' }> = { type: 'upsert_profile', harness, profile }
      if (harness !== 'zcode') {
        if (value.model !== (old?.model ?? null)) action.model = value.model
        if (value.effort !== (old?.effort ?? null)) action.effort = value.effort
      }
      if (!old || 'model' in action || 'effort' in action) actions.push(action)
    }
  }
  for (const [role, selector] of Object.entries(draft.roles)) {
    if (base.roles[role] !== selector) actions.push({ type: 'set_global_role', role, selector })
  }
  for (const [team, value] of Object.entries(draft.teams)) {
    for (const [role, selector] of Object.entries(value.roles)) {
      if (base.teams[team]?.roles[role] !== selector) actions.push({ type: 'set_team_role', team, role, selector })
    }
    // Only changed links are emitted; null removes an existing link and a
    // missing base link means the team is new (add_team precedes this).
    const draftUpgrade = value.upgrade_to ?? null
    const baseUpgrade = base.teams[team]?.upgrade_to ?? null
    if (draftUpgrade !== baseUpgrade) actions.push({ type: 'set_team_upgrade', team, upgrade_to: draftUpgrade })
  }
  if (draft.default_workflow !== base.default_workflow && draft.default_workflow) actions.push({ type: 'set_default_workflow', value: draft.default_workflow })
  if (draft.max_turns !== base.max_turns) actions.push({ type: 'set_max_turns', value: draft.max_turns })
  for (const [workflow, team] of Object.entries(draft.workflow_default_teams)) {
    if (base.workflow_default_teams[workflow] !== team) actions.push({ type: 'set_workflow_default_team', workflow, team })
  }
  const diffText = (before: Record<string, string>, after: Record<string, string>, make: (key: string, text: string | null) => GuidedConfigAction) => {
    for (const key of new Set([...Object.keys(before), ...Object.keys(after)])) {
      if (before[key] !== after[key]) actions.push(make(key, after[key] ?? null))
    }
  }
  diffText(base.prompts ?? {}, draft.prompts ?? {}, (name, text) => ({ type: 'set_prompt', name, text }))
  diffText(base.role_prompts ?? {}, draft.role_prompts ?? {}, (role, text) => ({ type: 'set_role_prompt', role, text }))
  for (const [team, value] of Object.entries(draft.teams)) diffText(base.teams[team]?.prompts ?? {}, value.prompts ?? {}, (role, text) => ({ type: 'set_role_prompt', team, role, text }))
  return actions
}

export function changedDocuments(base: { aflow_toml: string; workflows_toml: string }, texts: [string, string]) {
  return {
    ...(texts[0] !== base.aflow_toml ? { 'aflow.toml': texts[0] } : {}),
    ...(texts[1] !== base.workflows_toml ? { 'workflows.toml': texts[1] } : {}),
  }
}
