import { useEffect, useRef, useState } from 'react'
import * as api from '../api'
import type { GuidedConfigAction, GuidedFormProjection, ProjectConfig, ProjectConfigFormResponse, SettingsResponse, SettingsSaveRequest } from '../types'
import { changedDocuments, settingsActions } from '../settingsDraft'
import { AppearanceSelector } from './AppearanceSelector'
import { RecentRunsLimit } from './GlobalRunOverview'
import { Combobox } from './Combobox'
import { PromptsSettings, type DeletedPrompt } from './PromptsSettings'

const tabs = ['Agents & Roles', 'Teams', 'Workflows', 'Prompts', 'General'] as const
const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value))

export function GlobalSettings({ onDirtyChange, onSaved }: { onDirtyChange: (dirty: boolean) => void; onSaved: (saved: ProjectConfig) => void }) {
  const [tab, setTab] = useState<typeof tabs[number]>('Agents & Roles')
  const [snapshot, setSnapshot] = useState<ProjectConfig | null>(null)
  const [projection, setProjection] = useState<ProjectConfigFormResponse | null>(null)
  const [projectionError, setProjectionError] = useState<string | null>(null)
  const [baseline, setBaseline] = useState<GuidedFormProjection | null>(null)
  const [draft, setDraft] = useState<GuidedFormProjection | null>(null)
  const [pendingNames, setPendingNames] = useState<Record<string, string>>({})
  const [deletedPrompts, setDeletedPrompts] = useState<DeletedPrompt[]>([])
  const [texts, setTexts] = useState<[string, string]>(['', ''])
  const [advanced, setAdvanced] = useState(false)
  const [rawEdited, setRawEdited] = useState(false)
  const [server, setServer] = useState<SettingsResponse | null>(null)
  const [serverDraft, setServerDraft] = useState({ bind_host: '', bind_port: '', managed_projects_root: '' })
  const [serverText, setServerText] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [newProfile, setNewProfile] = useState({ harness: '', profile: '', model: '', effort: '' })
  const [newRole, setNewRole] = useState({ role: '', selector: '' })
  const [newTeamName, setNewTeamName] = useState('')
  const [newTeamError, setNewTeamError] = useState<string | null>(null)
  const [pendingFocusTeam, setPendingFocusTeam] = useState<string | null>(null)
  const [starter, setStarter] = useState({ workflow: 'cp', main_branch: 'main' })
  // Bumped by every load/discard so a response that resolves after an
  // explicit discard can never restore cleared edits.
  const epochRef = useRef(0)

  async function acceptConfig(saved: ProjectConfig, epoch: number) {
    // The snapshot and raw texts are kept before the projection is attempted,
    // so a mistyped prompt table (no guided projection) still leaves the
    // documents inspectable and editable through Advanced TOML.
    setSnapshot(saved); setTexts([saved.aflow_toml, saved.workflows_toml])
    let form: ProjectConfigFormResponse | null = null
    let failure: string | null = null
    try {
      form = await api.postGlobalConfigForm({ aflow_toml: saved.aflow_toml, workflows_toml: saved.workflows_toml })
    } catch (reason) {
      failure = reason instanceof Error ? reason.message : 'Could not build the guided view of the saved configuration.'
    }
    if (epochRef.current !== epoch) return
    setProjection(form)
    setBaseline(form?.form ?? null); setDraft(form?.form ? clone(form.form) : null); setPendingNames({})
    setRawEdited(false); setProjectionError(failure)
  }
  function acceptServer(saved: SettingsResponse, epoch: number) {
    if (epochRef.current !== epoch) return
    setServer(saved); setServerText(saved.advanced_toml)
    setServerDraft({ bind_host: saved.bind_host, bind_port: String(saved.bind_port), managed_projects_root: saved.managed_projects_root })
  }
  async function load() {
    const epoch = ++epochRef.current
    setBusy(true); setError(null)
    const results = await Promise.allSettled([
      api.getGlobalConfig().then(saved => acceptConfig(saved, epoch)), api.getSettings().then(saved => acceptServer(saved, epoch)),
    ])
    if (epochRef.current !== epoch) return
    const failures = results.filter(result => result.status === 'rejected')
    if (failures.length) setError('Some settings could not be loaded. Reload to retry.')
    setBusy(false)
  }
  /** Explicit confirmed discard: every pending edit is dropped up front. */
  function discardAndReload() {
    setDeletedPrompts([])
    epochRef.current += 1
    setPassword(''); setNewProfile({ harness: '', profile: '', model: '', effort: '' }); setNewRole({ role: '', selector: '' }); setNewTeamName(''); setNewTeamError(null); setPendingFocusTeam(null)
    setPendingNames({}); setRawEdited(false); setError(null); setNotice(null); setProjectionError(null)
    setTexts(['', '']); setSnapshot(null); setProjection(null); setBaseline(null); setDraft(null)
    setServer(null); setServerText(''); setServerDraft({ bind_host: '', bind_port: '', managed_projects_root: '' })
    void load()
  }
  async function retryProjection() {
    if (!snapshot) return
    setBusy(true); setError(null); setProjectionError(null)
    try {
      const form = await api.postGlobalConfigForm({ aflow_toml: snapshot.aflow_toml, workflows_toml: snapshot.workflows_toml })
      setProjection(form); setBaseline(form.form); setDraft(form.form ? clone(form.form) : null)
    } catch (reason) {
      setProjectionError(reason instanceof Error ? reason.message : 'Could not build the guided view.')
    } finally { setBusy(false) }
  }
  useEffect(() => { void load() }, []) // eslint-disable-line react-hooks/exhaustive-deps
  const actions = baseline && draft ? settingsActions(baseline, draft) : []
  const documents = snapshot ? changedDocuments(snapshot, texts) : {}
  const configDirty = (rawEdited && Object.keys(documents).length > 0) || actions.length > 0 || Object.entries(pendingNames).some(([name, target]) => name !== target && name in (draft?.prompts ?? {}))
  const pendingCreation = Object.values(newProfile).some(Boolean) || Object.values(newRole).some(Boolean)
  const serverDirty = Boolean(server && (password || serverText !== server.advanced_toml || serverDraft.bind_host !== server.bind_host || serverDraft.bind_port !== String(server.bind_port) || serverDraft.managed_projects_root !== server.managed_projects_root))
  const dirty = configDirty || pendingCreation || serverDirty
  useEffect(() => { onDirtyChange(dirty); return () => onDirtyChange(false) }, [dirty, onDirtyChange])
  useEffect(() => {
    if (!dirty) return
    const guard = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = '' }
    window.addEventListener('beforeunload', guard)
    return () => window.removeEventListener('beforeunload', guard)
  }, [dirty])
  /** Creates the team in the shared unsaved draft immediately; Save all persists it. */
  function addTeam() {
    setNewTeamError(null)
    const name = newTeamName.trim()
    if (!draft) return
    if (!name) { setNewTeamError('Enter a team name.'); return }
    if (name in draft.teams) { setNewTeamError(`Team "${name}" already exists.`); return }
    change(value => { value.teams[name] = { roles: {}, prompts: {} } })
    setNewTeamName('')
    setPendingFocusTeam(name)
  }

  useEffect(() => {
    if (!pendingFocusTeam) return
    document.getElementById(`team-editor-${pendingFocusTeam}`)?.focus()
    setPendingFocusTeam(null)
  }, [pendingFocusTeam, draft])

  /** Follows the draft upgrade links from a team; cycles and missing targets surface as errors. */
  function draftUpgradeChain(start: string): { chain: string[]; error: string | null } {
    const chain = [start]
    const seen = new Set([start])
    let current = draft?.teams[start]?.upgrade_to ?? null
    while (current) {
      if (seen.has(current)) return { chain, error: `Upgrade chain has a cycle at "${current}". Choose a different target.` }
      if (!draft?.teams[current]) return { chain, error: `Upgrade target "${current}" does not exist. Choose a configured team.` }
      seen.add(current)
      chain.push(current)
      current = draft.teams[current]?.upgrade_to ?? null
    }
    return { chain, error: null }
  }

  /** Worker selector plus model/effort of one team stage, from the current draft. */
  function workerText(team: string): string {
    const teamRoles = draft?.teams[team]?.roles ?? {}
    const selector = (typeof teamRoles.worker === 'string' && teamRoles.worker.trim()) || draft?.roles.worker || null
    if (!selector) return ''
    const dot = selector.indexOf('.')
    const profile = dot > 0 ? draft?.harnesses[selector.slice(0, dot)]?.[selector.slice(dot + 1)] : null
    const modelEffort = profile ? [profile.model, profile.effort ? `effort ${profile.effort}` : null].filter(Boolean).join(', ') : ''
    return [selector, modelEffort].filter(Boolean).join(' · ')
  }

  function change(update: (value: GuidedFormProjection) => void) {
    if (!draft) return
    const value = clone(draft); update(value); setDraft(value)
  }
  function candidate() {
    if (!draft || !baseline) return { form: draft, actions: [] as GuidedConfigAction[] }
    const value = clone(draft)
    if (Object.values(newProfile).some(Boolean)) {
      if (!newProfile.harness || !newProfile.profile) throw new Error('New profile needs a harness and profile name.')
      if (value.harnesses[newProfile.harness]?.[newProfile.profile]) throw new Error('That profile already exists; edit its fields above.')
      ;(value.harnesses[newProfile.harness] ??= {})[newProfile.profile] = { model: newProfile.model || null, effort: newProfile.effort || null }
    }
    if (newRole.role || newRole.selector) {
      if (!newRole.role || !newRole.selector) throw new Error('New role needs a name and profile selector.')
      if (newRole.role in value.roles) throw new Error('That role already exists.')
      value.roles[newRole.role] = newRole.selector
    }
    const compared = clone(baseline)
    const renameActions: GuidedConfigAction[] = []
    for (const [name, target] of Object.entries(pendingNames)) {
      if (name === target || !(name in (value.prompts ?? {}))) continue
      if (!target.trim() || target in value.prompts!) throw new Error('Choose a nonempty, unused prompt key.')
      value.prompts![target] = value.prompts![name]; delete value.prompts![name]
      if (name in (compared.prompts ?? {})) {
        compared.prompts![target] = compared.prompts![name]; delete compared.prompts![name]
        renameActions.push({ type: 'rename_prompt', name, new_name: target })
      }
    }
    return { form: value, actions: [...renameActions, ...settingsActions(compared, value)] }
  }
  async function previewActions(operations: GuidedConfigAction[]) {
    if (!snapshot) throw new Error('Workflow settings have not loaded.')
    let result: ProjectConfigFormResponse | null = null
    let pair = { aflow_toml: rawEdited ? texts[0] : snapshot.aflow_toml, workflows_toml: rawEdited ? texts[1] : snapshot.workflows_toml }
    for (const action of operations) {
      result = await api.postGlobalConfigForm({ ...pair, action })
      pair = { aflow_toml: result.aflow_toml, workflows_toml: result.workflows_toml }
    }
    return { pair, result }
  }
  async function toggleAdvanced() {
    setBusy(true); setError(null)
    try {
      if (!advanced) {
        const next = candidate()
        const { pair } = await previewActions(next.actions)
        setTexts([pair.aflow_toml, pair.workflows_toml])
        if (rawEdited) {
          setDraft(next.form); setBaseline(next.form); setPendingNames({})
          setNewProfile({ harness: '', profile: '', model: '', effort: '' }); setNewRole({ role: '', selector: '' }); setNewTeamName(''); setNewTeamError(null); setPendingFocusTeam(null)
        }
      } else if (advanced && rawEdited) {
        const form = await api.postGlobalConfigForm({ aflow_toml: texts[0], workflows_toml: texts[1] })
        if (!form.form) throw new Error('Correct the TOML syntax before switching to guided settings.')
        setDraft(form.form); setProjection(form)
        setBaseline(form.form); setPendingNames({})
      }
      setAdvanced(!advanced)
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Could not switch editors') }
    finally { setBusy(false) }
  }
  async function save() {
    if (!snapshot || busy) return
    setBusy(true); setError(null); setNotice(null)
    let configSaved = false
    try {
      const operations = candidate().actions
      // Validate all dirty domains before the first write; password is in the final write.
      const serverUpdates: SettingsSaveRequest = { expected_revision: server?.revision ?? '' }
      if (serverDirty && server) {
        const projected = serverText !== server.advanced_toml ? await api.projectSettingsText(serverText) : {}
        for (const key of ['bind_host', 'bind_port', 'managed_projects_root'] as const) {
          const field = key === 'bind_port' ? Number(serverDraft[key]) : serverDraft[key]
          const typedChanged = String(serverDraft[key]) !== String(server[key])
          const next = typedChanged ? field : projected[key]
          if (next !== undefined && next !== server[key]) Object.assign(serverUpdates, { [key]: next })
        }
        if (serverUpdates.bind_port !== undefined && (!Number.isInteger(serverUpdates.bind_port) || Number(serverUpdates.bind_port) < 1 || Number(serverUpdates.bind_port) > 65535)) throw new Error('Server port must be between 1 and 65535.')
        if (password) serverUpdates.password = password
      }
      if (configDirty || pendingCreation) {
        const pair = (await previewActions(operations)).pair
        const validation = await api.validateGlobalConfig(pair)
        if (validation.state === 'invalid' || validation.placeholders.length) throw new Error(validation.issues.map(issue => issue.message).join(' ') || 'Replace placeholder selectors before saving.')
        const saved = await api.patchGlobalConfig({ expected_revision: snapshot.revision, ...(rawEdited ? { documents: changedDocuments(snapshot, [pair.aflow_toml, pair.workflows_toml]) } : { actions: operations }) })
        // Acknowledged writes are cleared even if a later projection or server write fails.
        configSaved = true; setDeletedPrompts([]); setSnapshot(saved); setTexts([saved.aflow_toml, saved.workflows_toml]); setBaseline(candidate().form); setDraft(candidate().form); setPendingNames({}); setRawEdited(false)
        setNewProfile({ harness: '', profile: '', model: '', effort: '' }); setNewRole({ role: '', selector: '' }); setNewTeamName(''); setNewTeamError(null); setPendingFocusTeam(null)
        onSaved(saved)
        await acceptConfig(saved, epochRef.current)
      }
      if (Object.keys(serverUpdates).length > 1) { acceptServer(await api.saveSettings(serverUpdates), epochRef.current); setPassword('') }
      else if (server) setServerText(server.advanced_toml)
      setNotice('All changes saved. New runs use the saved configuration; existing runs keep their snapshot.')
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : 'Save failed'
      setError(`${configSaved ? 'Workflow configuration saved. Remaining settings were not saved. ' : ''}${message}. Your remaining edits are retained; reload explicitly to reapply after a conflict.`)
    } finally { setBusy(false) }
  }
  const selectors = draft ? Object.entries(draft.harnesses).flatMap(([h, profiles]) => Object.keys(profiles).map(p => `${h}.${p}`)) : []
  function profileFields(harness: string, profile: string, model: string, effort: string, update: (field: 'model' | 'effort', text: string) => void) {
    const suggestions = projection?.suggestions.profiles.filter(p => p.harness === harness) ?? []
    const adapter = projection?.suggestions.harnesses.find(h => h.name === harness)
    return harness === 'zcode' ? <p>Model and effort are configured in ZCode.</p> : <>
      <Combobox label={`Model ${harness}.${profile}`} value={model} allowCustom options={[...new Set(suggestions.flatMap(p => p.model ? [p.model] : []))]} onChange={value => update('model', value)} />
      {adapter?.supports_effort && <><Combobox label={`Effort ${harness}.${profile}`} value={effort} allowCustom options={[...new Set(suggestions.flatMap(p => p.effort ? [p.effort] : []))]} onChange={value => update('effort', value)} /><button className="btn btn-secondary btn-sm" onClick={() => update('effort', '')}>Unset effort {harness}.{profile}</button></>}
    </>
  }
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([])
  useEffect(() => {
    // Keep the selected tab visible in the single-row mobile tablist.
    try { tabRefs.current[tabs.indexOf(tab)]?.scrollIntoView?.({ block: 'nearest', inline: 'nearest' }) } catch { /* layout scrolling is best-effort */ }
  }, [tab])
  return <div className="workspace-content global-settings">
    <div className="section-heading"><h2>Settings</h2></div>
    {error && <p className="error-message" role="alert">{error}</p>}
    {notice && <p className="success-message" role="status">{notice}</p>}
    <button className="btn btn-secondary btn-sm" disabled={busy} onClick={() => { if (!dirty || window.confirm('Discard unsaved settings and reload?')) void discardAndReload() }}>Reload server settings</button>
    {projectionError && <div className="error-message" role="alert">The guided settings view is unavailable: {projectionError} <button className="btn btn-secondary btn-sm" onClick={() => void retryProjection()} disabled={busy || !snapshot}>Retry</button> The saved documents stay editable under Advanced TOML.</div>}
    <div className="settings-toolbar">
      <div className="config-tabs" role="tablist" aria-label="Settings sections">{tabs.map((name, index) => <button key={name} ref={el => { tabRefs.current[index] = el }} role="tab" id={`settings-tab-${index}`} aria-controls="settings-domain-panel" aria-selected={tab === name} tabIndex={tab === name ? 0 : -1} className={`btn ${tab === name ? 'btn-primary' : 'btn-secondary'}`} onClick={() => setTab(name)} onKeyDown={event => {
        const next = event.key === 'ArrowRight' ? (index + 1) % tabs.length : event.key === 'ArrowLeft' ? (index + tabs.length - 1) % tabs.length : event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : -1
        if (next >= 0) { event.preventDefault(); setTab(tabs[next]); (event.currentTarget.parentElement?.children[next] as HTMLElement).focus() }
      }}>{name}</button>)}</div>
      {dirty && <span className="text-xs text-dim">Unsaved changes</span>}
      <button className="btn btn-primary btn-sm" disabled={!dirty || busy} onClick={() => void save()}>{busy ? 'Working…' : 'Save all changes'}</button>
    </div>
    <fieldset disabled={busy} className="settings-body" id="settings-domain-panel" role="tabpanel" aria-labelledby={`settings-tab-${tabs.indexOf(tab)}`}>
    {tab !== 'General' && <button className="btn btn-secondary btn-sm" disabled={busy || !snapshot} onClick={() => void toggleAdvanced()}>{advanced ? 'Guided settings' : 'Advanced TOML'}</button>}
    {tab === 'General' ? <div className="settings-fields">
      <AppearanceSelector /><RecentRunsLimit />
      <h3>Server settings</h3>
      {server && Object.values(server.restart).some(Boolean) && <p role="status">Saved server binding or root changes require a server restart.</p>}
      {(['bind_host', 'bind_port', 'managed_projects_root'] as const).map(key => <label key={key}>{({ bind_host: 'Bind host', bind_port: 'Bind port', managed_projects_root: 'Projects root' })[key]}<input className="input" value={serverDraft[key]} onChange={e => setServerDraft({ ...serverDraft, [key]: e.target.value })} /></label>)}
      <label>New password (leave empty to keep current)<input className="input" type="password" autoComplete="new-password" value={password} onChange={e => setPassword(e.target.value)} /></label>
      {password && <p>Saving the password ends existing sessions.</p>}
      <details><summary>Advanced TOML (connection settings)</summary><textarea className="input config-textarea" aria-label="Advanced connection settings TOML" value={serverText} onChange={e => setServerText(e.target.value)} /></details>
    </div> : advanced ? <div className="settings-fields">{texts.map((text, index) => <label key={index}>{index ? 'workflows.toml' : 'aflow.toml'}<textarea className="input mono config-textarea" aria-label={index ? 'workflows.toml contents' : 'aflow.toml contents'} value={text} onChange={e => { const next: [string, string] = [...texts]; next[index] = e.target.value; setTexts(next); if (!rawEdited) { const form = candidate().form; setBaseline(form); setDraft(form); setPendingNames({}); setNewProfile({ harness: '', profile: '', model: '', effort: '' }); setNewRole({ role: '', selector: '' }); setNewTeamName(''); setNewTeamError(null); setPendingFocusTeam(null) }; setRawEdited(true) }} /></label>)}</div> : draft ? <div>
      {rawEdited && <p className="notice">Advanced document edits are pending. Saving replaces only the edited documents, including subsequent guided changes.</p>}
      <fieldset className="settings-body">
      {tab === 'Agents & Roles' && <>
        <h3>Profiles</h3>
        {Object.entries(draft.harnesses).flatMap(([harness, profiles]) => Object.entries(profiles).map(([profile, value]) => <div className="card settings-fields" key={`${harness}.${profile}`}><strong>{harness}.{profile}</strong>{profileFields(harness, profile, value.model ?? '', value.effort ?? '', (field, text) => change(next => { next.harnesses[harness][profile][field] = text || null }))}</div>))}
        <div className="card settings-fields"><h4>New profile</h4><label>Harness<select className="input" aria-label="Harness" value={newProfile.harness} onChange={e => setNewProfile({ ...newProfile, harness: e.target.value, model: '', effort: '' })}><option value="">Choose harness</option>{projection?.suggestions.harnesses.map(h => <option key={h.name}>{h.name}</option>)}</select></label><Combobox label="New profile name" value={newProfile.profile} allowCustom options={projection?.suggestions.profiles.filter(p => p.harness === newProfile.harness).map(p => p.profile) ?? []} onChange={profile => setNewProfile({ ...newProfile, profile })} />{newProfile.harness && profileFields(newProfile.harness, newProfile.profile, newProfile.model, newProfile.effort, (field, text) => setNewProfile({ ...newProfile, [field]: text }))}</div>
        <h3>Global roles</h3>{Object.entries(draft.roles).map(([role, selector]) => <Combobox key={role} label={`Role ${role}`} value={selector} options={selectors} onChange={value => change(next => { next.roles[role] = value })} />)}
        <div className="settings-fields"><label>New role name<input className="input" value={newRole.role} onChange={e => setNewRole({ ...newRole, role: e.target.value })} /></label><Combobox label="New role profile" value={newRole.selector} options={selectors} allowCustom onChange={selector => setNewRole({ ...newRole, selector })} /></div>
      </>}
      {tab === 'Teams' && <>
        <div className="add-team-form">
          <label>Add team<input className="input" aria-label="New team name" placeholder="team-name" value={newTeamName} onChange={e => { setNewTeamName(e.target.value); setNewTeamError(null) }} onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); addTeam() } }} /></label>
          <span className="inline-action"><button type="button" className="btn btn-secondary" onClick={addTeam}>Add team</button>{newTeamError && <span role="alert" className="text-sm add-team-error">{newTeamError}</span>}</span>
        </div>
        {Object.entries(draft.teams).map(([team, value]) => {
          const chainInfo = draftUpgradeChain(team)
          const isNew = !baseline?.teams?.[team]
          return <fieldset className="card settings-fields team-editor" key={team} id={`team-editor-${team}`} tabIndex={-1}>
            <legend><span className="mono">{team}</span>{isNew && <span className="status-pill status-awaiting">new — saved with Save all</span>}</legend>
            <label>Upgrade to<select className="input" aria-label={`Upgrade to for team ${team}`} value={value.upgrade_to ?? ''} onChange={e => change(next => { const edited = next.teams[team]; if (e.target.value) edited.upgrade_to = e.target.value; else delete edited.upgrade_to })}>
              <option value="">None — no further upgrade</option>
              {Object.keys(draft.teams).filter(other => other !== team).map(other => <option key={other} value={other}>{other}</option>)}
            </select></label>
            {chainInfo.error && <span role="alert" className="text-sm add-team-error">{chainInfo.error}</span>}
            <p className="text-xs text-dim">Worker chain: {chainInfo.chain.map((stage, index) => <span key={stage}>{index > 0 && ' → '}<span className="mono">{stage}</span>{workerText(stage) ? <> ({workerText(stage)})</> : null}</span>)}{chainInfo.chain.length === 1 && !chainInfo.error ? ' — no further upgrade configured' : ''}</p>
            {Object.keys(draft.roles).map(role => <Combobox key={role} label={role.charAt(0).toUpperCase() + role.slice(1)} value={value.roles[role] ?? ''} placeholder={`Inherited: ${draft.roles[role]}`} options={selectors} onChange={selector => change(next => { next.teams[team].roles[role] = selector })} />)}
          </fieldset>
        })}
      </>}
      {tab === 'Workflows' && <div className="settings-fields"><Combobox label="Default workflow" value={draft.default_workflow ?? ''} options={Object.keys(draft.workflows)} onChange={value => change(next => { next.default_workflow = value })} /><label>Max turns<input className="input" type="number" min="1" value={draft.max_turns ?? ''} onChange={e => change(next => { next.max_turns = e.target.value === '' ? null : Number(e.target.value) })} /></label>{Object.entries(draft.workflows).map(([workflow, value]) => <div className="card" key={workflow}><h4>{workflow}</h4><p>{(value.executable_steps ?? value.declared_steps).join(' → ')}</p><label>Default team<select className="input" value={draft.workflow_default_teams[workflow] ?? ''} onChange={e => change(next => { next.workflow_default_teams[workflow] = e.target.value || null })}><option value="">Unset</option>{Object.keys(draft.teams).map(team => <option key={team}>{team}</option>)}</select></label></div>)}</div>}
      {tab === 'Prompts' && <PromptsSettings draft={draft} change={change} names={pendingNames} rename={(name, target) => setPendingNames({ ...pendingNames, [name]: target })}
        deleted={deletedPrompts}
        onDelete={(name, text) => {
          if (deletedPrompts.some(item => item.name === name)) { setError('Undo the earlier deletion of this key before deleting it again.'); return }
          setDeletedPrompts(items => [...items, { name, text, pendingName: pendingNames[name] }])
          setPendingNames(names => { const next = { ...names }; delete next[name]; return next })
          change(value => { delete value.prompts![name] })
        }}
        onUndo={(name, restoreAs) => {
          const item = deletedPrompts.find(entry => entry.name === name)
          if (!item) return
          const targetName = restoreAs === name ? item.pendingName : undefined
          if (!restoreAs.trim() || restoreAs in (draft.prompts ?? {}) || (targetName && targetName !== restoreAs && targetName in (draft.prompts ?? {})) || Object.entries(pendingNames).some(([key, target]) => key !== name && key in (draft.prompts ?? {}) && (target === restoreAs || target === targetName))) {
            setError(`Cannot restore ${name}: its key is in use or invalid. Choose an unused restore key, then retry Undo.`); return
          }
          change(value => { (value.prompts ??= {})[restoreAs] = item.text })
          if (targetName !== undefined) setPendingNames(names => ({ ...names, [restoreAs]: targetName }))
          setDeletedPrompts(items => items.filter(entry => entry.name !== name)); setError(null)
        }} />}
      </fieldset>
    </div> : <p>Loading configuration, or the saved documents contain invalid TOML or mistyped values. Use Advanced TOML to inspect and repair them.</p>}
    {snapshot?.aflow_toml === '' && snapshot.workflows_toml === '' && <div className="card settings-fields"><h3>Starter setup</h3>{(['workflow', 'main_branch'] as const).map(key => <label key={key}>{key}<input className="input" value={starter[key]} onChange={e => setStarter({ ...starter, [key]: e.target.value })} /></label>)}<button className="btn btn-secondary" onClick={async () => {
      try { const form = await api.postGlobalConfigForm({ aflow_toml: '', workflows_toml: '', action: { type: 'build_starter', ...starter } }); setTexts([form.aflow_toml, form.workflows_toml]); setDraft(form.form); setProjection(form); setRawEdited(true); setAdvanced(true) } catch (reason) { setError(reason instanceof Error ? reason.message : 'Starter setup failed') }
    }}>Build starter draft</button></div>}
    </fieldset>
  </div>
}
