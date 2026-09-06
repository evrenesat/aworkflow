import { useEffect, useRef, useState } from 'react'
import * as api from '../api'
import type {
  ConfigValidation,
  GuidedConfigAction,
  GuidedProfileSuggestion,
  GuidedProfileSummary,
  ProjectConfigFormResponse,
  ProjectInfo,
} from '../types'
import { Combobox } from './Combobox'

interface GuidedConfigFormProps {
  project: ProjectInfo
  /** Shared candidate pair owned by the enclosing editor. */
  aflowText: string
  workflowsText: string
  onDraftChange: (aflowToml: string, workflowsToml: string) => void
  /**
   * Reports each accepted response's candidate validation together with the
   * exact pair it describes, so the enclosing editor never keeps a ready
   * result for a draft it no longer matches.
   */
  onCandidateValidation: (
    validation: ConfigValidation,
    sourcePair: { aflow: string; workflows: string },
  ) => void
  /** Only mutating guided actions block parent save/navigation. */
  onActionPendingChange?: (pending: boolean) => void
  /** Switches the shared editor to Advanced TOML and focuses the document. */
  onRequestAdvanced: (document?: 'aflow.toml' | 'workflows.toml') => void
}

interface SubmittedTexts {
  aflow: string
  workflows: string
}

function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : 'The guided settings request failed'
}

function selectorSummary(
  selector: string,
  harnesses: Record<string, Record<string, GuidedProfileSummary>>,
): string {
  const dot = selector.indexOf('.')
  if (dot <= 0) return ''
  const harness = selector.slice(0, dot)
  const profile = selector.slice(dot + 1)
  const summary = harnesses[harness]?.[profile]
  if (!summary) return ''
  const parts = [`${harness}/${profile}`]
  if (summary.model) parts.push(summary.model)
  if (summary.effort) parts.push(`effort ${summary.effort}`)
  return parts.join(' · ')
}

/** Just the model and effort of a selector, without repeating the profile. */
function selectorModelEffort(
  selector: string,
  harnesses: Record<string, Record<string, GuidedProfileSummary>>,
): string {
  const summary = selectorSummary(selector, harnesses)
  if (!summary) return ''
  if (selector.startsWith('zcode.')) return 'Configured in ZCode'
  return summary.split(' · ').slice(1).join(' · ') || 'Not specified'
}

/**
 * Guided settings for the shared candidate pair.  Every control sends the
 * current pair plus at most one typed action to the pure form endpoint and
 * replaces the local texts only from a successful, still-current response; a
 * draft action never saves.  Advanced TOML remains the escape hatch.
 */
export function GuidedConfigForm({
  project,
  aflowText,
  workflowsText,
  onDraftChange,
  onCandidateValidation,
  onActionPendingChange,
  onRequestAdvanced,
}: GuidedConfigFormProps) {
  const [form, setForm] = useState<ProjectConfigFormResponse | null>(null)
  const [formError, setFormError] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [actionBusy, setActionBusy] = useState(false)
  const [starter, setStarter] = useState({ workflow: '', mainBranch: '', team: '' })
  const [profileDraft, setProfileDraft] = useState({ harness: '', profile: '', model: '', effort: '' })
  const [roleDraft, setRoleDraft] = useState({ role: '', selector: '' })
  const [teamDraft, setTeamDraft] = useState({ team: '', role: '', selector: '' })
  const [newTeamName, setNewTeamName] = useState('')
  const [maxTurnsDraft, setMaxTurnsDraft] = useState('')

  const seqRef = useRef(0)
  const abortRef = useRef<AbortController | null>(null)
  const projectRef = useRef(project.id)
  const draftRef = useRef<SubmittedTexts>({ aflow: aflowText, workflows: workflowsText })
  draftRef.current = { aflow: aflowText, workflows: workflowsText }
  const formSourceRef = useRef<SubmittedTexts | null>(null)
  const starterTouchedRef = useRef(false)
  const pendingCallbackRef = useRef(onActionPendingChange)
  pendingCallbackRef.current = onActionPendingChange

  useEffect(() => () => {
    ++seqRef.current
    abortRef.current?.abort()
    pendingCallbackRef.current?.(false)
  }, [])

  if (projectRef.current !== project.id) {
    // Switching projects clears every dependent choice before any render.
    // (The shell only reaches this re-render after its dirty confirmation.)
    projectRef.current = project.id
    formSourceRef.current = null
    starterTouchedRef.current = false
    setStarter({ workflow: '', mainBranch: '', team: '' })
    setProfileDraft({ harness: '', profile: '', model: '', effort: '' })
    setRoleDraft({ role: '', selector: '' })
    setTeamDraft({ team: '', role: '', selector: '' })
    setNewTeamName('')
    setMaxTurnsDraft('')
    setFormError(null)
    setActionError(null)
  }

  function isStaleResponse(projectId: string, seq: number, submitted: SubmittedTexts): boolean {
    return projectRef.current !== projectId
      || seq !== seqRef.current
      || draftRef.current.aflow !== submitted.aflow
      || draftRef.current.workflows !== submitted.workflows
  }

  async function runRequest(action: GuidedConfigAction | null) {
    const submitted: SubmittedTexts = { aflow: draftRef.current.aflow, workflows: draftRef.current.workflows }
    abortRef.current?.abort()
    const controller = new AbortController()
    abortRef.current = controller
    const seq = ++seqRef.current
    const projectId = project.id
    const submittedProfile = action?.type === 'upsert_profile' ? { ...profileDraft } : null
    setActionBusy(true)
    pendingCallbackRef.current?.(action !== null)
    try {
      const response = await api.postProjectConfigForm(projectId, {
        aflow_toml: submitted.aflow,
        workflows_toml: submitted.workflows,
        action,
      }, { signal: controller.signal })
      if (isStaleResponse(projectId, seq, submitted)) return
      setForm(response)
      if (action?.type === 'upsert_profile' && submittedProfile) {
        const accepted = response.form?.harnesses[action.harness]?.[action.profile]
        if (accepted) {
          setProfileDraft((current) => (
            current.harness === submittedProfile.harness
            && current.profile === submittedProfile.profile
            && current.model === submittedProfile.model
            && current.effort === submittedProfile.effort
          ) ? { ...current, model: accepted.model ?? '', effort: accepted.effort ?? '' } : current)
        }
      }
      setFormError(null)
      formSourceRef.current = { aflow: response.aflow_toml, workflows: response.workflows_toml }
      // The response validation describes the returned pair, so propagate the
      // two together; a dirty candidate must never inherit the committed
      // snapshot's ready result.
      onCandidateValidation(response.validation, {
        aflow: response.aflow_toml,
        workflows: response.workflows_toml,
      })
      if (action) {
        setActionError(null)
        if (response.changed) {
          onDraftChange(response.aflow_toml, response.workflows_toml)
        }
      }
    } catch (err) {
      if (controller.signal.aborted) return
      if (isStaleResponse(projectId, seq, submitted)) return
      if (action) setActionError(errorMessage(err))
      else setFormError(errorMessage(err))
    } finally {
      if (seq === seqRef.current) {
        setActionBusy(false)
        pendingCallbackRef.current?.(false)
      }
    }
  }

  useEffect(() => {
    const source = formSourceRef.current
    if (
      form
      && source
      && source.aflow === aflowText
      && source.workflows === workflowsText
      && projectRef.current === project.id
    ) {
      return
    }
    void runRequest(null)
    // The shared pair is the single input: any change re-projects the draft.
  }, [project.id, aflowText, workflowsText]) // eslint-disable-line react-hooks/exhaustive-deps

  const defaults = form?.starter_defaults
  useEffect(() => {
    if (!defaults || starterTouchedRef.current) return
    setStarter({ workflow: defaults.workflow, mainBranch: defaults.main_branch, team: '' })
  }, [defaults])

  const emptyPair = aflowText === '' && workflowsText === ''
  const projection = form?.form ?? null
  const choices = form?.choices
  const suggestions = form?.suggestions
  const profileIsConfigured = Boolean(projection?.harnesses[profileDraft.harness]?.[profileDraft.profile])
  const profileIsSuggested = !profileIsConfigured && (suggestions?.profiles.some(
    (item) => item.harness === profileDraft.harness && item.profile === profileDraft.profile,
  ) ?? false)

  const syntaxIssues = form?.syntax_issues ?? []
  // The form endpoint returns no projection only while a document has a
  // syntax error; both texts are then echoed back untouched.
  const syntaxBlocked = projection === null && syntaxIssues.length > 0
  // The held projection is only trustworthy while it was computed from the
  // exact pair (and project) currently shown; anything else is pending or
  // failed reprojection and must not render its configured choices.
  const projectionCurrent = form !== null
    && formSourceRef.current !== null
    && projectRef.current === project.id
    && formSourceRef.current.aflow === aflowText
    && formSourceRef.current.workflows === workflowsText

  function localReject(message: string) {
    setActionError(message)
    return false
  }

  function buildStarter() {
    if (!starter.workflow.trim()) return localReject('Choose a workflow for the starter draft.')
    if (!starter.mainBranch.trim()) return localReject('Name the main branch for the starter draft.')
    void runRequest({
      type: 'build_starter',
      workflow: starter.workflow.trim(),
      main_branch: starter.mainBranch.trim(),
      team: starter.team.trim() || null,
    })
  }

  function applyMaxTurns() {
    const raw = maxTurnsDraft.trim()
    if (raw === '') {
      void runRequest({ type: 'set_max_turns', value: null })
      return
    }
    const parsed = Number.parseInt(raw, 10)
    if (!Number.isInteger(parsed) || parsed < 1) {
      localReject('Max turns must be a whole number of 1 or more.')
      return
    }
    void runRequest({ type: 'set_max_turns', value: parsed })
  }

  /**
   * Values to show for a harness/profile: the configured profile wins, then a
   * bundled suggestion (offered, never claimed as available), then blank.
   */
  function profileSeed(harness: string, profile: string): { model: string; effort: string } {
    const configured = form?.form?.harnesses[harness]?.[profile]
    if (configured) return { model: configured.model ?? '', effort: configured.effort ?? '' }
    const suggested = suggestions?.profiles.find(
      (item: GuidedProfileSuggestion) => item.harness === harness && item.profile === profile,
    )
    if (suggested) return { model: suggested.model ?? '', effort: suggested.effort ?? '' }
    return { model: '', effort: '' }
  }

  /** Rehydrates dependent fields so one profile's values never leak into another. */
  function changeProfileIdentity(patch: { harness?: string; profile?: string }) {
    const harness = patch.harness ?? profileDraft.harness
    const profile = patch.profile ?? profileDraft.profile
    const seed = profileSeed(harness, profile)
    setProfileDraft({ ...profileDraft, ...patch, model: seed.model, effort: seed.effort })
  }

  function applyProfile() {
    const harness = profileDraft.harness.trim()
    const profile = profileDraft.profile.trim()
    if (!harness) return localReject('Choose a harness for the profile.')
    if (!profile) return localReject('Name the profile.')
    if (harness === 'zcode') {
      void runRequest({ type: 'upsert_profile', harness, profile })
      return
    }
    // The server preserves fields the action omits and reads an explicit null
    // as deletion, so only fields the user visibly changed (or cleared via a
    // labeled control) travel with the action.
    const action: GuidedConfigAction = { type: 'upsert_profile', harness, profile }
    const configured = Boolean(form?.form?.harnesses[harness]?.[profile])
    // The accepted projection is the baseline, including prior edits to this same profile.
    const baseline = form?.form?.harnesses[harness]?.[profile]
    const supportsEffort = suggestions?.harnesses.find((item) => item.name === harness)?.supports_effort ?? false
    const model = profileDraft.model.trim()
    if (!configured) {
      if (model) action.model = model
      if (supportsEffort && profileDraft.effort) action.effort = profileDraft.effort
    } else {
      if (model !== (baseline?.model ?? '').trim()) action.model = model || null
      if (supportsEffort && profileDraft.effort !== (baseline?.effort ?? '')) {
        action.effort = profileDraft.effort || null
      }
    }
    void runRequest(action)
  }

  function applyGlobalRole() {
    const role = roleDraft.role.trim()
    const selector = roleDraft.selector.trim()
    if (!role) return localReject('Name the role to assign.')
    if (!selector) return localReject('Choose one of the configured profiles for this role.')
    if (choices && !choices.selectors.includes(selector)) {
      return localReject('Role selectors must name one of the configured profiles.')
    }
    void runRequest({ type: 'set_global_role', role, selector })
  }

  function applyTeamRole() {
    const team = teamDraft.team.trim()
    const role = teamDraft.role.trim()
    const selector = teamDraft.selector.trim()
    if (!team) return localReject('Choose the team.')
    if (!role) return localReject('Choose the role to override.')
    if (!selector) return localReject('Choose one of the configured profiles for this role.')
    if (choices && !choices.selectors.includes(selector)) {
      return localReject('Team role selectors must name one of the configured profiles.')
    }
    void runRequest({ type: 'set_team_role', team, role, selector })
  }

  function addTeam() {
    const team = newTeamName.trim()
    if (!team) return localReject('Name the new team.')
    void runRequest({ type: 'add_team', team })
  }

  function applyWorkflowTeam(workflow: string, value: string) {
    void runRequest({ type: 'set_workflow_default_team', workflow, team: value || null })
  }

  function harnessOptions(): string[] {
    const names = new Set<string>([
      ...(choices?.harnesses ?? []),
      ...(suggestions?.harnesses.map((item) => item.name) ?? []),
    ])
    return [...names].sort()
  }

  /** Configured profiles for the harness first, then bundled suggestions. */
  function profileNameOptions(harness: string): string[] {
    const names = new Set<string>([
      ...Object.keys(form?.form?.harnesses[harness] ?? {}),
      ...(suggestions?.profiles.filter((item) => item.harness === harness).map((item) => item.profile) ?? []),
    ])
    return [...names].sort()
  }

  /** Marks each suggestion as configured (in the draft) or a bundled suggestion. */
  function profileNameBadges(harness: string): Record<string, string> {
    const badges: Record<string, string> = {}
    for (const name of Object.keys(form?.form?.harnesses[harness] ?? {})) badges[name] = 'configured'
    for (const item of suggestions?.profiles ?? []) {
      if (item.harness === harness && badges[item.profile] === undefined) badges[item.profile] = 'suggested'
    }
    return badges
  }

  function modelSuggestions(harness: string): string[] {
    const models = (suggestions?.profiles ?? [])
      .filter((item: GuidedProfileSuggestion) => item.harness === harness && item.model)
      .map((item) => item.model as string)
    const configured = Object.values(projection?.harnesses[harness] ?? {}).flatMap((item) => item.model ? [item.model] : [])
    return [...new Set([...configured, ...models])].sort()
  }

  function effortOptions(harness: string): string[] {
    const efforts = (suggestions?.profiles ?? [])
      .filter((item) => item.harness === harness && item.effort)
      .map((item) => item.effort as string)
    const configured = Object.values(projection?.harnesses[harness] ?? {}).flatMap((item) => item.effort ? [item.effort] : [])
    return [...new Set([...configured, ...efforts, ...(profileDraft.effort ? [profileDraft.effort] : [])])].sort()
  }

  if (formError && !form) {
    return (
      <div className="card guided-section" aria-label="Guided settings unavailable">
        <h4 style={{ fontWeight: 600 }}>Guided settings are temporarily unavailable</h4>
        <p className="text-sm text-dim">{formError}</p>
        <div className="dashboard-actions">
          <button className="btn btn-secondary btn-sm" onClick={() => void runRequest(null)}>Retry</button>
          <button className="btn btn-secondary btn-sm" onClick={() => onRequestAdvanced()}>Open Advanced TOML</button>
        </div>
      </div>
    )
  }

  if (!form) {
    return <div className="card dashboard-loading"><div className="spinner" />Loading guided settings…</div>
  }

  if (!projectionCurrent) {
    if (actionBusy || !formError) {
      // A newer draft or project projection is still in flight: never show the
      // previous configured choices as if they belonged to the current draft.
      return (
        <div className="card guided-section dashboard-loading" aria-label="Updating guided settings">
          <div className="spinner" />Updating guided settings…
        </div>
      )
    }
    return (
      <div className="card guided-section" role="alert" aria-label="Guided settings need a retry">
        <h4 style={{ fontWeight: 600 }}>Guided settings could not be updated for the current draft</h4>
        <p className="text-sm text-dim">{formError}</p>
        <p className="text-xs text-dim">
          The previous settings view no longer matches your draft, so it was hidden. Your texts
          and entries are preserved — retry, or edit the pair in Advanced TOML.
        </p>
        <div className="dashboard-actions">
          <button className="btn btn-secondary btn-sm" onClick={() => void runRequest(null)}>Retry</button>
          <button className="btn btn-secondary btn-sm" onClick={() => onRequestAdvanced()}>Open Advanced TOML</button>
        </div>
      </div>
    )
  }

  if (syntaxBlocked) {
    const first = syntaxIssues[0]
    return (
      <div className="card guided-section" role="alert" aria-label="Guided settings blocked by TOML syntax errors">
        <h4 style={{ fontWeight: 600 }}>Guided settings are unavailable until the TOML syntax is fixed</h4>
        <p className="text-sm">
          One of the documents has a TOML syntax error, so its settings cannot be shown as controls.
          Both texts are preserved exactly as they are — nothing was repaired or discarded.
        </p>
        <ul className="validation-issues">
          {syntaxIssues.map((issue, index) => (
            <li key={index} className="text-sm">
              <span className="mono">
                {issue.document ?? 'config'}{issue.line !== null ? `:${issue.line}` : ''}
              </span>{' '}
              {issue.message}
            </li>
          ))}
        </ul>
        <div className="dashboard-actions">
          <button
            className="btn btn-primary btn-sm"
            onClick={() => onRequestAdvanced(
              first?.document === 'workflows.toml' ? 'workflows.toml' : 'aflow.toml',
            )}
          >
            Open Advanced TOML to fix the syntax
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="guided-form">
      {actionError && (
        <div className="error-message" role="alert">
          {actionError}
          <p className="text-xs text-dim">Your entries and the draft are unchanged.</p>
        </div>
      )}
      {formError && (
        <div className="error-message" role="alert">
          {formError}
          <button className="btn btn-secondary btn-sm" onClick={() => void runRequest(null)}>Retry</button>
        </div>
      )}

      {emptyPair && (
        <section className="card guided-section" aria-labelledby="guided-setup-heading">
          <h4 id="guided-setup-heading" style={{ fontWeight: 600 }}>Set up this project</h4>
          <p className="text-sm text-dim">
            This project has no configuration yet. Build a starter draft here, review it, then use
            Save to commit it. Nothing is written to the project until you Save.
          </p>
          <div className="dashboard-form-grid">
            <div className="dashboard-field">
              <label className="text-xs text-dim" htmlFor="starter-workflow">Workflow</label>
              <input
                id="starter-workflow"
                className="input mono"
                value={starter.workflow}
                onChange={(event) => {
                  starterTouchedRef.current = true
                  setStarter({ ...starter, workflow: event.target.value })
                }}
              />
              <span className="text-xs text-dim">The workflow decides which steps run.</span>
            </div>
            <div className="dashboard-field">
              <label className="text-xs text-dim" htmlFor="starter-main-branch">Main branch</label>
              <input
                id="starter-main-branch"
                className="input mono"
                value={starter.mainBranch}
                onChange={(event) => {
                  starterTouchedRef.current = true
                  setStarter({ ...starter, mainBranch: event.target.value })
                }}
              />
              <span className="text-xs text-dim">
                Detected from the registered Git root
                {defaults?.main_branch_source === 'fallback' && ' (fallback: HEAD was unavailable)'}.
              </span>
            </div>
            <div className="dashboard-field">
              <label className="text-xs text-dim" htmlFor="starter-team">Team (optional)</label>
              <input
                id="starter-team"
                className="input mono"
                value={starter.team}
                onChange={(event) => {
                  starterTouchedRef.current = true
                  setStarter({ ...starter, team: event.target.value })
                }}
              />
              <span className="text-xs text-dim">Leave empty to use global role defaults.</span>
            </div>
          </div>
          <div className="dashboard-actions">
            <button className="btn btn-primary btn-sm" disabled={actionBusy} onClick={buildStarter}>
              Build starter draft
            </button>
          </div>
        </section>
      )}

      {projection && !emptyPair && (
        <>
          <section className="card guided-section" aria-labelledby="guided-defaults-heading">
            <h4 id="guided-defaults-heading" style={{ fontWeight: 600 }}>Defaults</h4>
            <div className="dashboard-form-grid">
              <div className="dashboard-field">
                <label className="text-xs text-dim" htmlFor="default-workflow">Default workflow</label>
                <select
                  id="default-workflow"
                  className="input"
                  value={projection.default_workflow ?? ''}
                  disabled={actionBusy}
                  onChange={(event) => {
                    if (event.target.value) void runRequest({ type: 'set_default_workflow', value: event.target.value })
                  }}
                >
                  <option value="">Not set</option>
                  {(choices?.workflows ?? []).map((workflow) => (
                    <option key={workflow} value={workflow}>{workflow}</option>
                  ))}
                  {projection.default_workflow
                    && !(choices?.workflows ?? []).includes(projection.default_workflow) && (
                    <option value={projection.default_workflow}>{projection.default_workflow} (configured)</option>
                  )}
                </select>
                <span className="text-xs text-dim">
                  The default workflow decides which steps run; each plan can still choose another.
                </span>
              </div>
              <div className="dashboard-field">
                <label className="text-xs text-dim" htmlFor="max-turns">Max turns</label>
                <div className="inline-action">
                  <input
                    id="max-turns"
                    className="input"
                    type="number"
                    min={1}
                    placeholder={projection.max_turns === null ? 'No limit configured' : String(projection.max_turns)}
                    value={maxTurnsDraft}
                    onChange={(event) => setMaxTurnsDraft(event.target.value)}
                  />
                  <button className="btn btn-secondary btn-sm" disabled={actionBusy} onClick={applyMaxTurns}>
                    Apply max turns
                  </button>
                </div>
                <span className="text-xs text-dim">
                  Max turns is a limit for future runs. Empty applies no limit.
                </span>
              </div>
            </div>
          </section>

          <section className="card guided-section" aria-labelledby="guided-agents-heading">
            <h4 id="guided-agents-heading" style={{ fontWeight: 600 }}>Agents and models</h4>
            <p className="text-xs text-dim">
              A profile chooses the agent tool (harness) and the model it uses for a role.
            </p>
            {Object.keys(projection.harnesses).length === 0 ? (
              <p className="text-sm text-dim">No profiles are configured in this draft yet.</p>
            ) : (
              <table className="guided-table">
                <caption className="text-xs text-dim">Configured profiles (from your draft)</caption>
                <thead>
                  <tr><th scope="col">Profile</th><th scope="col">Model</th><th scope="col">Effort</th></tr>
                </thead>
                <tbody>
                  {Object.entries(projection.harnesses).flatMap(([harness, profiles]) =>
                    Object.entries(profiles).map(([profile, summary]) => (
                      <tr key={`${harness}.${profile}`}>
                        <td className="mono">{harness}.{profile}</td>
                        <td className="mono">{summary.model ?? '—'}</td>
                        <td className="mono">{summary.effort ?? '—'}</td>
                      </tr>
                    )))}
                </tbody>
              </table>
            )}
            <div className="dashboard-form-grid" aria-label="Add or update a profile">
              <div className="dashboard-field">
                <label className="text-xs text-dim" htmlFor="profile-harness">Harness</label>
                <select
                  id="profile-harness"
                  className="input"
                  value={profileDraft.harness}
                  onChange={(event) => changeProfileIdentity({ harness: event.target.value })}
                >
                  <option value="">Choose a harness…</option>
                  {harnessOptions().map((harness) => <option key={harness} value={harness}>{harness} — {choices?.harnesses.includes(harness) ? 'configured' : 'suggested'}</option>)}
                </select>
              </div>
              <div className="dashboard-field">
                <Combobox
                  label="Profile name"
                  value={profileDraft.profile}
                  onChange={(profile) => changeProfileIdentity({ profile })}
                  options={profileNameOptions(profileDraft.harness)}
                  optionBadges={profileNameBadges(profileDraft.harness)}
                  allowCustom
                  placeholder="for example default"
                />
              </div>
              {profileDraft.harness === 'zcode' ? (
                <p className="text-xs text-dim" role="note">
                  ZCode model and reasoning effort are configured in ZCode's project configuration;
                  they cannot be set or cleared here.
                </p>
              ) : (
                <>
                  <div className="dashboard-field">
                    <Combobox
                      label="Model"
                      value={profileDraft.model}
                      onChange={(model) => setProfileDraft({ ...profileDraft, model })}
                      options={modelSuggestions(profileDraft.harness)}
                      optionBadges={Object.fromEntries(modelSuggestions(profileDraft.harness).map((model) => [
                        model,
                        Object.values(projection.harnesses[profileDraft.harness] ?? {}).some((item) => item.model === model)
                          ? 'configured' : 'suggested',
                      ]))}
                      allowCustom={suggestions?.harnesses.find((item) => item.name === profileDraft.harness)?.custom_model_supported ?? false}
                      placeholder="Model for this profile"
                    />
                    {profileDraft.model !== '' && (
                      <button
                        type="button"
                        className="btn btn-secondary btn-sm"
                        disabled={actionBusy}
                        onClick={() => setProfileDraft({ ...profileDraft, model: '' })}
                      >
                        Clear model
                      </button>
                    )}
                    <span className="text-xs text-dim">
                      Apply changes only the fields you edited; Clear model (or Unset effort)
                      removes that value from the profile.
                    </span>
                  </div>
                  {(suggestions?.harnesses.find((item) => item.name === profileDraft.harness)?.supports_effort ?? false) && (
                    <div className="dashboard-field">
                      <label className="text-xs text-dim" htmlFor="profile-effort">Effort</label>
                      <select
                        id="profile-effort"
                        className="input"
                        value={profileDraft.effort}
                        onChange={(event) => setProfileDraft({ ...profileDraft, effort: event.target.value })}
                      >
                        <option value="">Unset</option>
                        {effortOptions(profileDraft.harness).map((effort) => (
                          <option key={effort} value={effort}>{effort}{Object.values(projection.harnesses[profileDraft.harness] ?? {}).some((item) => item.effort === effort) ? ' — configured' : ' — suggested'}</option>
                        ))}
                      </select>
                    </div>
                  )}
                  {profileIsSuggested && (
                    <p className="text-xs text-dim" role="note">
                      “{profileDraft.profile}” comes from the bundled suggestions; its values are
                      offered here but are not verified as available on this server.
                    </p>
                  )}
                </>
              )}
            </div>
            <div className="dashboard-actions">
              <button className="btn btn-secondary btn-sm" disabled={actionBusy} onClick={applyProfile}>
                Apply profile to draft
              </button>
            </div>
            <p className="text-xs text-dim" role="note">{suggestions?.note}</p>
          </section>

          <section className="card guided-section" aria-labelledby="guided-roles-heading">
            <h4 id="guided-roles-heading" style={{ fontWeight: 600 }}>Roles</h4>
            <p className="text-xs text-dim">
              Roles map work to a profile. Global roles apply everywhere; teams can override them.
            </p>
            {Object.keys(projection.roles).length === 0 ? (
              <p className="text-sm text-dim">No global roles are configured in this draft yet.</p>
            ) : (
              <table className="guided-table">
                <caption className="text-xs text-dim">Configured global roles (from your draft)</caption>
                <thead><tr><th scope="col">Role</th><th scope="col">Profile</th><th scope="col">Model / effort</th></tr></thead>
                <tbody>
                  {Object.entries(projection.roles).map(([role, selector]) => (
                    <tr key={role}>
                      <td className="mono">{role}</td>
                      <td className="mono">{selector}</td>
                      <td>{selectorModelEffort(selector, projection.harnesses) || <span className="text-dim">unknown profile</span>}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            <div className="dashboard-form-grid" aria-label="Assign a global role">
              <Combobox
                label="Role"
                value={roleDraft.role}
                onChange={(role) => setRoleDraft({ ...roleDraft, role })}
                options={choices?.roles ?? []}
                optionBadges={Object.fromEntries((choices?.roles ?? []).map((role) => [role, 'configured']))}
                allowCustom
                placeholder="for example worker"
              />
              <Combobox
                label="Profile (configured choices only)"
                value={roleDraft.selector}
                onChange={(selector) => setRoleDraft({ ...roleDraft, selector })}
                options={choices?.selectors ?? []}
                optionBadges={Object.fromEntries((choices?.selectors ?? []).map((selector) => [selector, 'configured']))}
                emptyOption="Configure a profile above first"
              />
            </div>
            <div className="dashboard-actions">
              <button className="btn btn-secondary btn-sm" disabled={actionBusy} onClick={applyGlobalRole}>
                Apply role to draft
              </button>
            </div>
          </section>

          <section className="card guided-section" aria-labelledby="guided-teams-heading">
            <h4 id="guided-teams-heading" style={{ fontWeight: 600 }}>Teams</h4>
            <p className="text-xs text-dim">
              A team overrides global roles for the runs that select it. An empty team inherits the
              global defaults.
            </p>
            {Object.keys(projection.teams).length === 0 ? (
              <p className="text-sm text-dim">No teams are configured in this draft yet.</p>
            ) : (
              Object.entries(projection.teams).map(([team, summary]) => (
                <div key={team} className="guided-team card" aria-label={`Team ${team}`}>
                  <strong className="mono text-sm">{team}</strong>
                  {Object.keys(summary.roles).length === 0 ? (
                    <p className="text-xs text-dim">No role overrides — this team inherits the global defaults.</p>
                  ) : (
                    <table className="guided-table">
                      <caption className="text-xs text-dim">Team overrides</caption>
                      <thead><tr><th scope="col">Role</th><th scope="col">Profile</th><th scope="col">Model / effort</th></tr></thead>
                      <tbody>
                        {Object.entries(summary.roles).map(([role, selector]) => (
                          <tr key={role}>
                            <td className="mono">{role}</td>
                            <td className="mono">{selector}</td>
                            <td>{selectorModelEffort(selector, projection.harnesses) || <span className="text-dim">unknown profile</span>}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  )}
                </div>
              ))
            )}
            <div className="dashboard-form-grid" aria-label="Add a team">
              <div className="dashboard-field">
                <label className="text-xs text-dim" htmlFor="new-team-name">New team name</label>
                <div className="inline-action">
                  <input
                    id="new-team-name"
                    className="input mono"
                    value={newTeamName}
                    onChange={(event) => setNewTeamName(event.target.value)}
                  />
                  <button className="btn btn-secondary btn-sm" disabled={actionBusy} onClick={addTeam}>Add team</button>
                </div>
              </div>
            </div>
            {Object.keys(projection.teams).length > 0 && (
              <div className="dashboard-form-grid" aria-label="Override a role inside a team">
                <div className="dashboard-field">
                  <label className="text-xs text-dim" htmlFor="team-role-team">Team</label>
                  <select
                    id="team-role-team"
                    className="input"
                    value={teamDraft.team}
                    onChange={(event) => setTeamDraft({ ...teamDraft, team: event.target.value })}
                  >
                    <option value="">Choose a team…</option>
                    {(choices?.teams ?? []).map((team) => <option key={team} value={team}>{team}</option>)}
                  </select>
                </div>
                <div className="dashboard-field">
                  <label className="text-xs text-dim" htmlFor="team-role-role">Team role</label>
                  <select
                    id="team-role-role"
                    className="input"
                    value={teamDraft.role}
                    onChange={(event) => setTeamDraft({ ...teamDraft, role: event.target.value })}
                  >
                    <option value="">Choose a role…</option>
                    {(choices?.roles ?? []).map((role) => <option key={role} value={role}>{role}</option>)}
                  </select>
                </div>
                <Combobox
                  label="Profile (configured choices only)"
                  value={teamDraft.selector}
                  onChange={(selector) => setTeamDraft({ ...teamDraft, selector })}
                  options={choices?.selectors ?? []}
                optionBadges={Object.fromEntries((choices?.selectors ?? []).map((selector) => [selector, 'configured']))}
                />
              </div>
            )}
            {Object.keys(projection.teams).length > 0 && (
              <div className="dashboard-actions">
                <button className="btn btn-secondary btn-sm" disabled={actionBusy} onClick={applyTeamRole}>
                  Apply team override to draft
                </button>
              </div>
            )}
          </section>

          <section className="card guided-section" aria-labelledby="guided-workflows-heading">
            <h4 id="guided-workflows-heading" style={{ fontWeight: 600 }}>Workflows</h4>
            <p className="text-xs text-dim">
              Each workflow runs its steps in order. Uncommon graph and prompt settings remain in
              Advanced TOML.
            </p>
            {Object.keys(projection.workflows).length === 0 ? (
              <p className="text-sm text-dim">No workflows are declared in this draft yet.</p>
            ) : (
              Object.entries(projection.workflows).map(([workflow, summary]) => {
                const defaultTeam = projection.workflow_default_teams[workflow] ?? null
                const teamRoles = defaultTeam ? projection.teams[defaultTeam]?.roles ?? {} : {}
                return (
                  <div key={workflow} className="guided-team card" aria-label={`Workflow ${workflow}`}>
                    <div className="section-heading">
                      <strong className="mono text-sm">{workflow}</strong>
                      <div className="dashboard-field" style={{ minWidth: '200px' }}>
                        <label className="text-xs text-dim" htmlFor={`workflow-team-${workflow}`}>Default team</label>
                        <select
                          id={`workflow-team-${workflow}`}
                          className="input"
                          aria-label={`Default team for ${workflow}`}
                          value={defaultTeam ?? ''}
                          disabled={actionBusy}
                          onChange={(event) => applyWorkflowTeam(workflow, event.target.value)}
                        >
                          <option value="">No default team</option>
                          {(choices?.teams ?? []).map((team) => <option key={team} value={team}>{team}</option>)}
                          {defaultTeam && !(choices?.teams ?? []).includes(defaultTeam) && (
                            <option value={defaultTeam}>{defaultTeam} (configured)</option>
                          )}
                        </select>
                      </div>
                    </div>
                    <ol className="guided-steps">
                      {(summary.executable_steps ?? summary.declared_steps).map((step) => (
                        <li key={step} className="text-sm mono">{step}</li>
                      ))}
                    </ol>
                    {summary.executable_steps === null && (
                      <p className="text-xs text-dim">
                        Executable steps become available once the draft is semantically complete.
                      </p>
                    )}
                    <table className="guided-table">
                      <caption className="text-xs text-dim">
                        Role assignments ({defaultTeam ? `team ${defaultTeam} overrides → global` : 'global'})
                      </caption>
                      <thead><tr><th scope="col">Role</th><th scope="col">Profile</th><th scope="col">Model / effort</th><th scope="col">Source</th></tr></thead>
                      <tbody>
                        {(choices?.roles ?? []).map((role) => {
                          const override = teamRoles[role]
                          const global = projection.roles[role]
                          const selector = override ?? global
                          if (!selector) {
                            return (
                              <tr key={role}>
                                <td className="mono">{role}</td>
                                <td colSpan={3}><span className="text-dim">missing — assign it in Roles</span></td>
                              </tr>
                            )
                          }
                          return (
                            <tr key={role}>
                              <td className="mono">{role}</td>
                              <td className="mono">{selector}</td>
                              <td>{selectorModelEffort(selector, projection.harnesses) || <span className="text-dim">unknown profile</span>}</td>
                              <td>{override ? `team ${defaultTeam}` : 'global'}</td>
                            </tr>
                          )
                        })}
                        {(choices?.roles ?? []).length === 0 && (
                          <tr><td colSpan={4} className="text-dim">No roles configured yet.</td></tr>
                        )}
                      </tbody>
                    </table>
                  </div>
                )
              })
            )}
            <div className="dashboard-actions">
              <button className="btn btn-secondary btn-sm" onClick={() => onRequestAdvanced()}>
                Open Advanced TOML for graph and prompt settings
              </button>
            </div>
          </section>
        </>
      )}

      <p className="text-xs text-dim" role="note">
        Guided edits only change your draft. Save applies the pair to later runs and may be blocked
        by current or resumable runs. Choices labeled as configured come from your draft; the rest
        are bundled suggestions.
      </p>
    </div>
  )
}
