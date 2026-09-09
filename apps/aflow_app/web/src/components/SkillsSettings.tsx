import { useState } from 'react'
import type { SkillInstallResult, SkillSummary } from '../types'
import { SidebarEditorLayout } from './SidebarEditorLayout'

function skillStatusLine(skill: SkillSummary, dirty: boolean): string {
  const parts = [skill.source === 'saved' ? 'saved' : 'bundled', skill.edited || dirty ? 'edited' : 'unmodified']
  parts.push(skill.installed ? 'installed' : 'not installed')
  if (dirty) parts.push('unsaved edits')
  return parts.join(' · ')
}

/**
 * Presentational Skills editor. GlobalSettings owns selection, drafts, and
 * baseline revisions indexed by exact skill name; this component renders the
 * shared sidebar/editor layout, the install action, and per-skill status.
 * Saving Markdown updates every installed link; installation itself runs
 * through the shared installer action owned by GlobalSettings.
 */
export function SkillsSettings({ skills, loadError, selected, onSelect, content, contentLoading, contentError, draft, unsavedNames, onEdit, hasUnsavedEdits, onInstall, installing, installResult, installError }: {
  skills: SkillSummary[] | null
  loadError: string | null
  selected: string
  onSelect: (name: string) => void
  content: string | null
  contentLoading: boolean
  contentError: string | null
  draft: string | null
  unsavedNames: readonly string[]
  onEdit: (name: string, text: string) => void
  hasUnsavedEdits: boolean
  onInstall: () => void
  installing: boolean
  installResult: SkillInstallResult | null
  installError: string | null
}) {
  const [navigationVersion, setNavigationVersion] = useState(0)
  function select(name: string) { onSelect(name); setNavigationVersion(value => value + 1) }
  const active = skills?.find(skill => skill.name === selected) ?? null
  const dirty = draft !== null && content !== null && draft !== content
  return <section className="settings-guided-content" aria-label="Skills settings">
    <div className="card settings-fields">
      <h3>Install skills</h3>
      <p className="text-sm text-dim">
        Install or reinstall the default bundled skills through the shared installer
        (the same action as <span className="mono">aflow install-skills --yes</span>).
        Saving Markdown already updates every linked destination, so edited skills
        need no reinstall. Optional skills install only through existing CLI options
        such as <span className="mono">--include-optional</span>.
      </p>
      {hasUnsavedEdits && <p role="note">Save your skill edits first; installation is unavailable while any skill has unsaved edits, and drafts are kept.</p>}
      <span className="inline-action">
        <button type="button" className="btn btn-secondary btn-sm" disabled={installing || hasUnsavedEdits || !skills || skills.length === 0} onClick={onInstall}>
          {installing ? 'Installing…' : 'Install/reinstall all'}
        </button>
      </span>
      {installError && <p role="alert" className="text-sm add-team-error">{installError}</p>}
      {installResult && <p role="status" className="text-sm">
        {installResult.succeeded ? 'Install finished.' : 'Install finished with failures.'}{' '}
        {installResult.operations.filter(op => op.status === 'linked').length} linked,{' '}
        {installResult.operations.filter(op => op.status === 'already_linked').length} already linked,{' '}
        {installResult.operations.filter(op => op.status !== 'linked' && op.status !== 'already_linked').length} pending or failed.
        {installResult.operations.filter(op => op.status === 'failed' || op.status === 'unattempted').map(op => ` ${op.harness}/${op.skill}: ${op.error_code ?? op.status}${op.error ? ` — ${op.error}` : ''}`).join('')}
      </p>}
    </div>
    {loadError && <p role="alert" className="error-message">Skills could not be loaded: {loadError}</p>}
    {!skills && !loadError && <p>Loading skills…</p>}
    {skills && skills.length === 0 && <p>No bundled skills are registered.</p>}
    {skills && skills.length > 0 && <SidebarEditorLayout selection={selected} navigationVersion={navigationVersion} navigation={<div>
      <h3>Skills</h3>
      {skills.map(skill => <button className={`btn sidebar-entry ${selected === skill.name ? 'btn-primary' : 'btn-secondary'}`} aria-pressed={selected === skill.name} key={skill.name} onClick={() => select(skill.name)}>
        {skill.name}{skill.default ? '' : ' (optional)'}{unsavedNames.includes(skill.name) ? ' · unsaved' : ''}
      </button>)}
    </div>}>
      <h3>{active ? <>{active.name}{active.default ? '' : ' (optional)'}</> : 'No skill selected'}</h3>
      {active && <p className="text-xs text-dim" role="status">
        {skillStatusLine(active, dirty)}{active.source === 'saved' && !active.installed ? ' — saved but not installed; run Install/reinstall all or the CLI to link it.' : ''}
        {!active.default ? ' An optional skill: the default install excludes it; use existing CLI options to install it.' : ''}
      </p>}
      {contentLoading && <p>Loading skill content…</p>}
      {contentError && <p role="alert" className="error-message">{contentError}</p>}
      {!contentLoading && !contentError && content !== null && active && <label>SKILL.md<textarea
        className="input mono config-textarea"
        rows={20}
        aria-label={`SKILL.md for ${active.name}`}
        value={draft ?? content}
        onChange={e => onEdit(active.name, e.target.value)}
      /></label>}
      {!contentLoading && !contentError && content !== null && active && dirty && <p role="note" className="text-sm">Unsaved edits — they save with Save all changes.</p>}
    </SidebarEditorLayout>}
  </section>
}
