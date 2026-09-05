import { useEffect, useState } from 'react'
import type { ProjectInfo } from '../types'

interface ProjectEditorProps {
  project: ProjectInfo
  onSave: (request: { display_name: string }) => Promise<void>
  onCancel: () => void
}

export function ProjectEditor({ project, onSave, onCancel }: ProjectEditorProps) {
  const [displayName, setDisplayName] = useState(project.display_name)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => { setDisplayName(project.display_name); setError(null) }, [project])
  async function handleSave() {
    if (!displayName.trim()) return
    try { setSaving(true); setError(null); await onSave({ display_name: displayName.trim() }) }
    catch (err) { setError(err instanceof Error ? err.message : 'Failed to save project') }
    finally { setSaving(false) }
  }
  return (
    <div className="card" style={{ display: 'flex', flexDirection: 'column', gap: 'var(--spacing-md)' }}>
      {error && <div className="error-message">{error}</div>}
      <label><span className="text-xs text-dim">Display name</span>
        <input className="input" value={displayName} onChange={(event) => setDisplayName(event.target.value)} />
      </label>
      <div style={{ display: 'flex', gap: 'var(--spacing-sm)' }}>
        <button className="btn btn-primary" onClick={() => void handleSave()} disabled={saving || !displayName.trim()}>Save project</button>
        <button className="btn btn-secondary" onClick={onCancel} disabled={saving}>Cancel</button>
      </div>
    </div>
  )
}
