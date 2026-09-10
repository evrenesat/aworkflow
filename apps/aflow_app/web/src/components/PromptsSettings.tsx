import { useState } from 'react'
import type { GuidedFormProjection } from '../types'
import { SidebarEditorLayout } from './SidebarEditorLayout'
import { MenuItem, MoreMenu } from './MoreMenu'
import { TemplateVariablesHelp } from './TemplateVariablesHelp'
import { formatMachineChoice, formatMachineLabel } from '../label'

type ChangeFn = (update: (value: GuidedFormProjection) => void) => void

function NamedPromptCard({ name, text, displayName, usages, templateVariables, rename, change, onDelete }: {
  name: string
  text: string
  displayName: string
  usages: readonly string[]
  templateVariables: GuidedFormProjection['template_variables']
  rename: (name: string, target: string) => void
  change: ChangeFn
  onDelete: () => void
}) {
  const [confirming, setConfirming] = useState(false)
  const referenced = usages.length > 0
  const label = formatMachineLabel(displayName)
  return <div className="card settings-fields prompt-card">
    <div className="prompt-card-header">
      <strong className="mono truncate" title={displayName}>{label}</strong>
      {!referenced && !confirming && <MoreMenu label={`More actions for prompt ${label}`}>
        <MenuItem onClick={() => setConfirming(true)}>Delete prompt…</MenuItem>
      </MoreMenu>}
    </div>
    {confirming ? (
      <div className="confirmation" role="alertdialog" aria-label={`Delete prompt ${label}`}>
        <span>
          Delete the prompt <strong className="mono">{label}</strong> from the unsaved draft?
          The removal takes effect when you Save all; Undo is available until then.
        </span>
        <div className="dashboard-actions">
          <button type="button" className="btn btn-danger btn-sm" onClick={onDelete}>Delete prompt</button>
          <button type="button" className="btn btn-secondary btn-sm" onClick={() => setConfirming(false)}>Cancel</button>
        </div>
      </div>
    ) : (
      <>
        <label>Prompt key <input className="input" value={displayName} aria-label="Prompt key" onChange={e => rename(name, e.target.value)} /></label>
        <label>Prompt text <textarea className="input" rows={7} aria-label="Prompt text" value={text} onChange={e => change(value => { value.prompts![name] = e.target.value })} /></label>
        <TemplateVariablesHelp variables={templateVariables} context="named" />
        {referenced
          ? <details className="prompt-usages">
              <summary className="text-xs">Used by {usages.length} configured reference{usages.length === 1 ? '' : 's'}</summary>
              <ul className="prompt-usage-list">{usages.map(path => <li key={path} className="text-xs text-dim mono">{path}</li>)}</ul>
            </details>
          : <span className="text-xs text-dim">No configured references</span>}
      </>
    )}
  </div>
}

export interface DeletedPrompt { name: string; text: string; pendingName?: string }

export function PromptsSettings({ draft, change, rename, names, deleted, onDelete, onUndo, selected: suppliedSelected, onSelect }: {
  selected?: string
  onSelect?: (id: string) => void
  draft: GuidedFormProjection
  change: ChangeFn
  rename: (name: string, target: string) => void
  names: Record<string, string>
  deleted: DeletedPrompt[]
  onDelete: (name: string, text: string) => void
  onUndo: (name: string, restoreAs: string) => void
}) {
  const [localSelected, setLocalSelected] = useState('')
  const [navigationVersion, setNavigationVersion] = useState(0)
  const entries = [
    ...Object.keys(draft.prompts ?? {}).sort().map(name => ({ id: `named:${name}`, rawLabel: names[name] ?? name, label: formatMachineLabel(names[name] ?? name) })),
    ...Object.keys(draft.roles).sort().map(role => ({ id: JSON.stringify(['role', '', role]), rawLabel: `Global / ${role}`, label: `Global / ${formatMachineLabel(role)}` })),
    ...Object.keys(draft.teams).sort().flatMap(team => [...new Set([...Object.keys(draft.roles), ...Object.keys(draft.teams[team].roles), ...Object.keys(draft.teams[team].prompts ?? {})])].sort().map(role => ({ id: JSON.stringify(['role', team, role]), rawLabel: `${team} / ${role}`, label: `${formatMachineLabel(team)} / ${formatMachineLabel(role)}` }))),
  ]
  const roleNames = Object.keys(draft.roles).sort()
  const teamNames = Object.keys(draft.teams).sort()
  const entryLabelCounts = entries.reduce((counts, entry) => counts.set(entry.label, (counts.get(entry.label) ?? 0) + 1), new Map<string, number>())
  const requested = suppliedSelected ?? localSelected
  const selected = entries.some(entry => entry.id === requested) ? requested : entries[0]?.id ?? ''
  function select(id: string) { setLocalSelected(id); onSelect?.(id); setNavigationVersion(value => value + 1) }
  const [, team = '', role = ''] = selected.startsWith('[') ? JSON.parse(selected) as string[] : []
  const [targetRole, setTargetRole] = useState('')
  const [targetTeam, setTargetTeam] = useState('')
  const [moveError, setMoveError] = useState<string | null>(null)
  const [restoreKeys, setRestoreKeys] = useState<Record<string, string>>({})
  const map = team ? draft.teams[team]?.prompts ?? {} : draft.role_prompts ?? {}
  const inherited = team ? draft.role_prompts?.[role] : undefined
  const setRoleText = (text: string | null) => change(value => {
    const prompts = team ? (value.teams[team].prompts ??= {}) : (value.role_prompts ??= {})
    if (text === null) delete prompts[role]
    else prompts[role] = text
  })
  return <section className="settings-guided-content">
    <button className="btn btn-secondary" onClick={() => change(value => {
      const prompts = value.prompts ??= {}
      let key = 'new_prompt'
      for (let index = 2; key in prompts; index++) key = `new_prompt_${index}`
      prompts[key] = ''; select(`named:${key}`)
    })}>New prompt</button>
    {deleted.map(item => <div className="notice" role="status" key={item.name}>
      Prompt <strong className="mono">{formatMachineLabel(item.name)}</strong> was removed from the unsaved draft; the deletion happens when you Save all.
      <label>Restore under key <input className="input" aria-label="Restore key" value={restoreKeys[item.name] ?? item.name} onChange={event => setRestoreKeys(keys => ({ ...keys, [item.name]: event.target.value }))} /></label>
      <button type="button" className="btn btn-secondary btn-sm" aria-label={`Undo deletion of ${formatMachineLabel(item.name)}`} onClick={() => onUndo(item.name, restoreKeys[item.name] ?? item.name)}>Undo</button>
    </div>)}
    <SidebarEditorLayout selection={selected} navigationVersion={navigationVersion} listLabel="Prompts" navigation={<div><h3>Prompts</h3>{entries.map(entry => <button data-sidebar-editor-item={entry.id} className={`btn sidebar-entry ${selected === entry.id ? 'btn-primary' : 'btn-secondary'}`} aria-pressed={selected === entry.id} key={entry.id} onClick={() => select(entry.id)}><span>{entry.label}</span>{entryLabelCounts.get(entry.label)! > 1 && entry.rawLabel !== entry.label && <span className="mono text-xs text-dim">{entry.rawLabel}</span>}</button>)}</div>}>
    <h3>{entries.find(entry => entry.id === selected)?.label ?? 'No prompts'}{(() => { const entry = entries.find(item => item.id === selected); return entry && entryLabelCounts.get(entry.label)! > 1 && entry.rawLabel !== entry.label ? <span className="mono text-xs text-dim"> {entry.rawLabel}</span> : null })()}</h3>
    {Object.entries(draft.prompts ?? {}).filter(([name]) => selected === `named:${name}`).map(([name, text]) => <NamedPromptCard
      key={name}
      name={name}
      text={text}
      displayName={names[name] ?? name}
      usages={draft.prompt_usages?.[name] ?? []}
      templateVariables={draft.template_variables}
      rename={rename}
      change={change}
      onDelete={() => onDelete(name, text)}
    />)}
    <div className="settings-fields">
      {role && <>
        <span>{role in map ? 'Explicit override' : team ? 'Inherited global text' : 'Default role prompt'}</span>
        <textarea className="input" rows={7} aria-label="Role prompt text" value={map[role] ?? inherited ?? ''} onChange={e => setRoleText(e.target.value)} />
        <TemplateVariablesHelp variables={draft.template_variables} context="role" />
        {role in map && <button className="btn btn-secondary" onClick={() => setRoleText(null)}>Remove override</button>}
        {role in map && <details><summary>Move override</summary>
          <label>Target role<select className="input" value={targetRole} onChange={e => setTargetRole(e.target.value)}><option value="">Choose role</option>{roleNames.map(key => <option key={key} value={key}>{formatMachineChoice(key, roleNames)}</option>)}</select></label>
          <label>Target team<select className="input" value={targetTeam} onChange={e => setTargetTeam(e.target.value)}><option value="">Global roles</option>{teamNames.map(key => <option key={key} value={key}>{formatMachineChoice(key, teamNames)}</option>)}</select></label>
          {moveError && <p role="alert">{moveError}</p>}
          <button className="btn btn-secondary" onClick={() => {
            const destination = targetTeam ? draft.teams[targetTeam]?.prompts ?? {} : draft.role_prompts ?? {}
            if (!targetRole || targetRole in destination) { setMoveError('Choose an existing role with no override at the target.'); return }
            change(value => {
              const source = team ? value.teams[team].prompts! : value.role_prompts!
              const target = targetTeam ? (value.teams[targetTeam].prompts ??= {}) : (value.role_prompts ??= {})
              target[targetRole] = source[role]; delete source[role]
            })
            setMoveError(null); select(JSON.stringify(['role', targetTeam, targetRole]))
          }}>Move override to target</button>
        </details>}
      </>}
    </div>
    </SidebarEditorLayout>
  </section>
}
