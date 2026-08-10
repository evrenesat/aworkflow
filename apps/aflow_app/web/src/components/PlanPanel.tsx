import { useEffect, useState } from 'react'
import type { PlanInfo, ProjectInfo } from '../types'
import * as api from '../api'

interface PlanPanelProps {
  project: ProjectInfo
  onOpenRunDashboard: (planPath: string) => void
}

export function PlanPanel({ project, onOpenRunDashboard }: PlanPanelProps) {
  const [plans, setPlans] = useState<PlanInfo[]>([])
  const [drafts, setDrafts] = useState<string[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [selectedDraft, setSelectedDraft] = useState<string | null>(null)
  const [draftContent, setDraftContent] = useState('')
  const [promoteTargetName, setPromoteTargetName] = useState('')
  const [viewingDraft, setViewingDraft] = useState(false)

  useEffect(() => {
    void loadPlans()
    void loadDrafts()
  }, [project.id])

  async function loadPlans() {
    try {
      setLoading(true)
      setError(null)
      const data = await api.listProjectPlans(project.id)
      setPlans(data)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load plans')
    } finally {
      setLoading(false)
    }
  }

  async function loadDrafts() {
    try {
      const data = await api.listPlanDrafts(project.id)
      setDrafts(data)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load drafts')
    }
  }

  async function handleViewDraft(name: string) {
    try {
      setError(null)
      const draft = await api.loadPlanDraft(project.id, name)
      setDraftContent(draft.content)
      setSelectedDraft(draft.name)
      setPromoteTargetName(draft.name)
      setViewingDraft(true)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load draft')
    }
  }

  async function handlePromoteDraft() {
    if (!selectedDraft) return

    try {
      setError(null)
      await api.promotePlanDraft(project.id, {
        draft_name: selectedDraft,
        target_name: promoteTargetName.trim() || null,
      })
      setViewingDraft(false)
      setSelectedDraft(null)
      setDraftContent('')
      setPromoteTargetName('')
      await loadPlans()
      await loadDrafts()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to promote draft')
    }
  }

  async function handleDeleteDraft() {
    if (!selectedDraft) return

    try {
      setError(null)
      await api.deletePlanDraft(project.id, selectedDraft)
      setViewingDraft(false)
      setSelectedDraft(null)
      setDraftContent('')
      setPromoteTargetName('')
      await loadDrafts()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete draft')
    }
  }

  if (loading) {
    return (
      <div style={{ padding: 'var(--spacing-lg)', textAlign: 'center' }}>
        <div className="spinner" />
      </div>
    )
  }

  if (viewingDraft && selectedDraft) {
    return (
      <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
        <div
          style={{
            padding: 'var(--spacing-md)',
            borderBottom: '1px solid var(--color-border)',
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
            gap: 'var(--spacing-sm)',
          }}
        >
          <button className="btn btn-secondary btn-sm" onClick={() => setViewingDraft(false)}>
            ← Back
          </button>
          <div className="text-sm truncate" style={{ flex: 1, marginLeft: 'var(--spacing-md)' }}>
            {selectedDraft}
          </div>
        </div>

        {error && (
          <div style={{ padding: 'var(--spacing-md)' }}>
            <div className="error-message">{error}</div>
          </div>
        )}

        <div style={{ flex: 1, overflowY: 'auto', padding: 'var(--spacing-md)', display: 'flex', flexDirection: 'column', gap: 'var(--spacing-md)' }}>
          <div className="card" style={{ minHeight: '320px', overflowY: 'auto' }}>
            <div className="text-xs text-dim" style={{ marginBottom: 'var(--spacing-sm)' }}>
              Draft content
            </div>
            <pre className="mono" style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word', margin: 0 }}>
              {draftContent}
            </pre>
          </div>
          <label style={{ display: 'flex', flexDirection: 'column', gap: 'var(--spacing-xs)' }}>
            <span className="text-xs text-dim">Promote as</span>
            <input className="input" value={promoteTargetName} onChange={(e) => setPromoteTargetName(e.target.value)} />
          </label>
        </div>

        <div style={{ padding: 'var(--spacing-md)', borderTop: '1px solid var(--color-border)', display: 'flex', gap: 'var(--spacing-sm)' }}>
          <button className="btn btn-primary" style={{ flex: 1 }} onClick={() => void handlePromoteDraft()}>
            Promote to in-progress
          </button>
          <button className="btn btn-danger" onClick={() => void handleDeleteDraft()}>
            Delete
          </button>
        </div>
      </div>
    )
  }

  const inProgressPlans = plans.filter((plan) => plan.status === 'in_progress')

  return (
    <div style={{ padding: 'var(--spacing-md)', display: 'flex', flexDirection: 'column', gap: 'var(--spacing-lg)' }}>
      {error && <div className="error-message">{error}</div>}

      <div>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', gap: 'var(--spacing-sm)', marginBottom: 'var(--spacing-md)' }}>
          <h3 style={{ fontSize: '1.125rem', fontWeight: 600 }}>Plan drafts</h3>
          <span className="text-xs text-dim mono">{project.current_path}</span>
        </div>
        {drafts.length === 0 ? (
          <div className="card text-dim text-sm">No drafts</div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--spacing-sm)' }}>
            {drafts.map((draft) => (
              <button
                key={draft}
                className="card card-interactive"
                style={{
                  textAlign: 'left',
                  width: '100%',
                  border: 'none',
                  background: 'transparent',
                  color: 'inherit',
                }}
                onClick={() => void handleViewDraft(draft)}
              >
                <div className="mono text-sm" style={{ fontWeight: 500 }}>
                  {draft}
                </div>
              </button>
            ))}
          </div>
        )}
      </div>

      <div>
        <h3 style={{ fontSize: '1.125rem', fontWeight: 600, marginBottom: 'var(--spacing-md)' }}>In-progress plans</h3>
        {inProgressPlans.length === 0 ? (
          <div className="card text-dim text-sm">No in-progress plans</div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--spacing-sm)' }}>
            {inProgressPlans.map((plan) => (
              <div key={plan.path} className="card" style={{ display: 'flex', flexDirection: 'column', gap: 'var(--spacing-sm)' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 'var(--spacing-sm)' }}>
                  <div className="mono text-sm" style={{ fontWeight: 500 }}>
                    {plan.name}
                  </div>
                  {plan.is_complete && <span className="text-xs" style={{ color: 'var(--color-success)' }}>Complete</span>}
                </div>
                <div className="text-xs text-dim">
                  {plan.checkpoint_count} checkpoints, {plan.unchecked_count} remaining
                </div>
                <button className="btn btn-primary btn-sm" onClick={() => onOpenRunDashboard(plan.path)}>
                  Open run dashboard
                </button>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
