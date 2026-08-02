import { useEffect, useMemo, useRef, useState } from 'react'
import * as api from '../api'
import type {
  Attachment,
  AttachmentKind,
  PendingApproval,
  PlanningSession,
  PlanningTurn,
  ProjectInfo,
  ProviderCapabilities,
  ProviderReadiness,
  SessionKey,
  TurnItem,
} from '../types'

interface SessionPanelProps {
  project: ProjectInfo
  onSavePlanDraft: (content: string) => void
}

type UploadState = {
  localId: string
  filename: string
  state: 'pending' | 'uploading' | 'uploaded' | 'failed'
  attachment?: Attachment
  error?: string
}

const TURN_POLL_INTERVAL_MS = 1000
const TURN_POLL_TIMEOUT_MS = 15000
const TERMINAL_TURN_STATUSES = new Set(['completed', 'failed', 'interrupted'])
const EMPTY_CAPABILITIES: ProviderCapabilities = {
  models: [],
  reasoning_levels: [],
  reasoning_summaries: [],
  attachments: false,
  attachment_kinds: [],
  output_schema: false,
  fork: false,
  archive: false,
  approvals: false,
  interruption: false,
  compaction: false,
  rollback: false,
}

function sameKey(left: SessionKey | null, right: SessionKey | null): boolean {
  return left?.provider_id === right?.provider_id && left?.provider_session_id === right?.provider_session_id
}

function reactKey(key: SessionKey): string {
  return JSON.stringify([key.provider_id, key.provider_session_id])
}

function stringifyItem(item: TurnItem): string {
  if (typeof item === 'string') return item
  if (item === null || typeof item !== 'object') return String(item)
  for (const field of ['text', 'content', 'message', 'summary', 'preview']) {
    const value = item[field]
    if (typeof value === 'string' && value.trim()) return value
  }
  if (Array.isArray(item.items)) {
    const nested = item.items.map((value) => stringifyItem(value as TurnItem)).filter(Boolean)
    if (nested.length) return nested.join('\n')
  }
  return JSON.stringify(item, null, 2)
}

function stringifyTurn(turn: PlanningTurn): string {
  const content = turn.items.map(stringifyItem).filter((value) => value.trim()).join('\n\n')
  return content || (turn.error ? turn.error.message : '')
}

function looksLikePlanMarkdown(content: string): boolean {
  return /^#\s+.+/m.test(content) && /##\s+/.test(content)
}

function sessionTitle(session: PlanningSession): string {
  return session.title || session.preview || session.key.provider_session_id
}

function sortSessions(sessions: PlanningSession[]): PlanningSession[] {
  return [...sessions].sort((left, right) => (right.updated_at || '').localeCompare(left.updated_at || ''))
}

function upsertSession(sessions: PlanningSession[], session: PlanningSession): PlanningSession[] {
  if (sessions.some((existing) => sameKey(existing.key, session.key))) {
    return sortSessions(sessions.map((existing) => sameKey(existing.key, session.key) ? session : existing))
  }
  return sortSessions([session, ...sessions])
}

function upsertTurn(session: PlanningSession, turn: PlanningTurn): PlanningSession {
  const exists = session.turns.some((existing) => existing.turn_id === turn.turn_id)
  return {
    ...session,
    turns: exists
      ? session.turns.map((existing) => existing.turn_id === turn.turn_id ? turn : existing)
      : [...session.turns, turn],
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms))
}

export function SessionPanel({ project, onSavePlanDraft }: SessionPanelProps) {
  const [sessions, setSessions] = useState<PlanningSession[]>([])
  const [providers, setProviders] = useState<ProviderReadiness[]>([])
  const [showArchived, setShowArchived] = useState(false)
  const [selectedKey, setSelectedKey] = useState<SessionKey | null>(null)
  const [selectedSession, setSelectedSession] = useState<PlanningSession | null>(null)
  const [newProviderId, setNewProviderId] = useState('')
  const [newModel, setNewModel] = useState('')
  const [newReasoningLevel, setNewReasoningLevel] = useState('')
  const [turnInput, setTurnInput] = useState('')
  const [turnModel, setTurnModel] = useState('')
  const [turnReasoningLevel, setTurnReasoningLevel] = useState('')
  const [turnReasoningSummary, setTurnReasoningSummary] = useState('')
  const [attachments, setAttachments] = useState<Attachment[]>([])
  const [uploads, setUploads] = useState<UploadState[]>([])
  const [approvals, setApprovals] = useState<PendingApproval[]>([])
  const [loading, setLoading] = useState(true)
  const [loadingSession, setLoadingSession] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const listRequestRef = useRef(0)
  const loadRequestRef = useRef(0)
  const pollRef = useRef(0)
  const messagesEndRef = useRef<HTMLDivElement>(null)

  const readyProviders = providers.filter((provider) => provider.state === 'ready' || provider.state === 'degraded')
  const selectedProvider = providers.find((provider) => provider.provider_id === selectedKey?.provider_id) ?? null
  const selectedCapabilities = selectedProvider?.capabilities ?? EMPTY_CAPABILITIES
  const newProvider = providers.find((provider) => provider.provider_id === newProviderId) ?? null
  const supportsAnyAttachmentKind = selectedCapabilities.attachment_kinds.length > 0

  function clearSessionDetail() {
    loadRequestRef.current += 1
    pollRef.current += 1
    setSelectedKey(null)
    setSelectedSession(null)
    setAttachments([])
    setApprovals([])
    setUploads([])
    setTurnInput('')
    setLoadingSession(false)
  }

  useEffect(() => {
    clearSessionDetail()
    void loadSessions(null, showArchived)
    return () => {
      listRequestRef.current += 1
      loadRequestRef.current += 1
      pollRef.current += 1
    }
  }, [project.id, showArchived])

  useEffect(() => {
    loadRequestRef.current += 1
    pollRef.current += 1
    setSelectedSession(null)
    setAttachments([])
    setApprovals([])
    setUploads([])
    if (selectedKey) void loadSession(selectedKey)
  }, [selectedKey?.provider_id, selectedKey?.provider_session_id])

  useEffect(() => {
    if (!newProviderId && readyProviders.length) setNewProviderId(readyProviders[0].provider_id)
  }, [providers, newProviderId])

  useEffect(() => {
    setNewModel('')
    setNewReasoningLevel('')
  }, [newProviderId])

  useEffect(() => {
    setTurnModel(selectedSession?.model || '')
    setTurnReasoningLevel(selectedSession?.reasoning_level || '')
    setTurnReasoningSummary('')
  }, [selectedSession?.key.provider_id, selectedSession?.key.provider_session_id])

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [selectedSession])

  async function loadSessions(
    preferredKey: SessionKey | null = selectedKey,
    archived: boolean = showArchived,
  ) {
    const requestId = ++listRequestRef.current
    try {
      setLoading(true)
      setError(null)
      const page = await api.listProjectSessions(project.id, { archived })
      if (requestId !== listRequestRef.current) return
      const ordered = sortSessions(page.sessions)
      setSessions(ordered)
      setProviders(page.providers)
      const preferred = ordered.find((session) => sameKey(session.key, preferredKey)) ?? ordered[0]
      setSelectedKey(preferred?.key ?? null)
    } catch (err) {
      if (requestId === listRequestRef.current) {
        setError(err instanceof Error ? err.message : 'Failed to load planning sessions')
      }
    } finally {
      if (requestId === listRequestRef.current) setLoading(false)
    }
  }

  async function loadSession(key: SessionKey) {
    const requestId = ++loadRequestRef.current
    try {
      setLoadingSession(true)
      setError(null)
      const session = await api.getProjectSession(project.id, key)
      if (requestId !== loadRequestRef.current) return
      const provider = providers.find((item) => item.provider_id === key.provider_id)
      const [nextAttachments, nextApprovals] = await Promise.all([
        provider?.capabilities.attachments ? api.listAttachments(project.id, key) : Promise.resolve([]),
        provider?.capabilities.approvals ? api.listPendingApprovals(project.id, key) : Promise.resolve([]),
      ])
      if (requestId !== loadRequestRef.current) return
      setSelectedSession(session)
      setSessions((current) => upsertSession(current, session))
      setAttachments(nextAttachments)
      setApprovals(nextApprovals)
    } catch (err) {
      if (requestId === loadRequestRef.current) setError(err instanceof Error ? err.message : 'Failed to load planning session')
    } finally {
      if (requestId === loadRequestRef.current) setLoadingSession(false)
    }
  }

  async function handleStartSession() {
    if (!newProviderId) return
    try {
      setBusy(true)
      setError(null)
      const session = await api.startProjectSession(project.id, {
        provider_id: newProviderId,
        ...(newModel ? { model: newModel } : {}),
        ...(newReasoningLevel ? { reasoning_level: newReasoningLevel } : {}),
      })
      setSessions((current) => upsertSession(current, session))
      setSelectedKey(session.key)
      setSelectedSession(session)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to start planning session')
    } finally {
      setBusy(false)
    }
  }

  async function handleResumeOrFork(action: 'resume' | 'fork') {
    if (!selectedKey) return
    try {
      setBusy(true)
      const session = action === 'resume'
        ? await api.resumeProjectSession(project.id, selectedKey)
        : await api.forkProjectSession(project.id, selectedKey)
      setSessions((current) => upsertSession(current, session))
      setSelectedKey(session.key)
      setSelectedSession(session)
    } catch (err) {
      setError(err instanceof Error ? err.message : `Failed to ${action} planning session`)
    } finally {
      setBusy(false)
    }
  }

  async function handleArchive() {
    if (!selectedSession) return
    try {
      setBusy(true)
      await api.setProjectSessionArchived(project.id, selectedSession.key, !selectedSession.archived)
      clearSessionDetail()
      await loadSessions(null, showArchived)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update archive state')
    } finally {
      setBusy(false)
    }
  }

  async function handleFiles(files: FileList | null) {
    if (!selectedSession || !files) return
    const queued = Array.from(files).map((file) => ({
      file,
      localId: crypto.randomUUID(),
      filename: file.name,
      state: 'pending' as const,
    }))
    setUploads((current) => [...current, ...queued.map((upload) => ({
      localId: upload.localId,
      filename: upload.filename,
      state: upload.state,
    }))])
    for (const { file, localId } of queued) {
      setUploads((current) => current.map((upload) => upload.localId === localId ? { ...upload, state: 'uploading' } : upload))
      const kind: AttachmentKind = file.type.startsWith('image/') ? 'image' : 'file'
      if (!selectedCapabilities.attachment_kinds.includes(kind)) {
        setUploads((current) => current.map((upload) => upload.localId === localId ? {
          ...upload,
          state: 'failed',
          error: `This provider does not support ${kind} attachments.`,
        } : upload))
        continue
      }
      try {
        const attachment = await api.uploadAttachment(project.id, selectedSession.key, file, kind)
        setUploads((current) => current.map((upload) => upload.localId === localId ? { ...upload, state: 'uploaded', attachment } : upload))
        setAttachments((current) => [...current, attachment])
      } catch (err) {
        setUploads((current) => current.map((upload) => upload.localId === localId ? {
          ...upload,
          state: 'failed',
          error: err instanceof Error ? err.message : 'Upload failed',
        } : upload))
      }
    }
  }

  async function handleDeleteAttachment(attachment: Attachment) {
    if (!selectedSession) return
    try {
      await api.deleteAttachment(project.id, selectedSession.key, attachment.attachment_id)
      setAttachments((current) => current.filter((item) => item.attachment_id !== attachment.attachment_id))
      setUploads((current) => current.filter((item) => item.attachment?.attachment_id !== attachment.attachment_id))
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete attachment')
    }
  }

  async function handleSendTurn() {
    if (!selectedSession || !turnInput.trim() || uploads.some((upload) => upload.state !== 'uploaded')) return
    const session = selectedSession
    const pollId = ++pollRef.current
    try {
      setBusy(true)
      setError(null)
      const turn = await api.startProjectTurn(project.id, session.key, {
        text: turnInput.trim(),
        attachment_ids: uploads.flatMap((upload) => upload.attachment ? [upload.attachment.attachment_id] : []),
        ...(turnModel ? { model: turnModel } : {}),
        ...(turnReasoningLevel ? { reasoning_level: turnReasoningLevel } : {}),
        ...(turnReasoningSummary ? { reasoning_summary: turnReasoningSummary } : {}),
      })
      setTurnInput('')
      setUploads([])
      const updated = upsertTurn(session, turn)
      setSelectedSession(updated)
      setSessions((current) => upsertSession(current, updated))
      if (!TERMINAL_TURN_STATUSES.has(turn.status)) {
        const deadline = Date.now() + TURN_POLL_TIMEOUT_MS
        while (Date.now() < deadline && pollRef.current === pollId) {
          await sleep(TURN_POLL_INTERVAL_MS)
          const refreshed = await api.getProjectSession(project.id, session.key)
          if (pollRef.current !== pollId) return
          setSelectedSession(refreshed)
          setSessions((current) => upsertSession(current, refreshed))
          const currentTurn = refreshed.turns.find((item) => item.turn_id === turn.turn_id)
          if (selectedCapabilities.approvals) {
            setApprovals(await api.listPendingApprovals(project.id, session.key))
          }
          if (currentTurn?.status === 'waiting_for_approval') break
          if (currentTurn && TERMINAL_TURN_STATUSES.has(currentTurn.status)) break
        }
      }
      await loadSessions(session.key)
    } catch (err) {
      if (pollRef.current === pollId) setError(err instanceof Error ? err.message : 'Failed to send turn')
    } finally {
      if (pollRef.current === pollId) setBusy(false)
    }
  }

  async function handleInterrupt(turnId: string) {
    if (!selectedSession) return
    try {
      await api.interruptProjectTurn(project.id, selectedSession.key, turnId)
      await loadSession(selectedSession.key)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to interrupt turn')
    }
  }

  async function handleApproval(approval: PendingApproval, decision: 'accept' | 'decline' | 'cancel') {
    if (!selectedSession) return
    try {
      await api.respondToApproval(project.id, selectedSession.key, approval.approval_id, decision)
      setApprovals(await api.listPendingApprovals(project.id, selectedSession.key))
      await loadSession(selectedSession.key)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to respond to approval')
    }
  }

  const selectedSessionIsStale = selectedSession?.cwd !== project.current_path
  const activeTurn = [...(selectedSession?.turns ?? [])].reverse().find((turn) => !TERMINAL_TURN_STATUSES.has(turn.status))
  const uploadBlocksSend = uploads.some((upload) => upload.state !== 'uploaded')
  const turnModels = useMemo(() => {
    const advertised = selectedCapabilities.models
    return selectedSession?.model && !advertised.includes(selectedSession.model)
      ? [selectedSession.model, ...advertised]
      : advertised
  }, [selectedCapabilities.models, selectedSession?.model])

  if (loading) return <div style={{ padding: 'var(--spacing-lg)', textAlign: 'center' }}><div className="spinner" /></div>

  return (
    <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 310px) minmax(0, 1fr)', gap: 'var(--spacing-md)', height: '100%', minHeight: 0 }}>
      <aside style={{ display: 'flex', flexDirection: 'column', gap: 'var(--spacing-md)', minWidth: 0, minHeight: 0 }}>
        <div className="card" style={{ display: 'flex', flexDirection: 'column', gap: 'var(--spacing-sm)' }}>
          <div style={{ fontWeight: 600 }}>Planning Sessions</div>
          <div style={{ display: 'flex', gap: 'var(--spacing-sm)' }} aria-label="Session list mode">
            <button className={`btn btn-sm ${!showArchived ? 'btn-primary' : 'btn-secondary'}`} aria-pressed={!showArchived} onClick={() => setShowArchived(false)}>Active</button>
            <button className={`btn btn-sm ${showArchived ? 'btn-primary' : 'btn-secondary'}`} aria-pressed={showArchived} onClick={() => setShowArchived(true)}>Archived</button>
          </div>
          {!showArchived && <>
            <label className="text-xs text-dim">Provider</label>
            <select className="input" aria-label="Provider" value={newProviderId} onChange={(event) => setNewProviderId(event.target.value)}>
              {readyProviders.map((provider) => <option key={provider.provider_id} value={provider.provider_id}>{provider.display_name}</option>)}
            </select>
            {!!newProvider?.capabilities.models.length && (
              <select className="input" aria-label="New session model" value={newModel} onChange={(event) => setNewModel(event.target.value)}>
                <option value="">Provider default model</option>
                {newProvider.capabilities.models.map((model) => <option key={model} value={model}>{model}</option>)}
              </select>
            )}
            {!!newProvider?.capabilities.reasoning_levels.length && (
              <select className="input" aria-label="New session reasoning level" value={newReasoningLevel} onChange={(event) => setNewReasoningLevel(event.target.value)}>
                <option value="">Provider default reasoning</option>
                {newProvider.capabilities.reasoning_levels.map((level) => <option key={level} value={level}>{level}</option>)}
              </select>
            )}
          </>}
          <div style={{ display: 'flex', gap: 'var(--spacing-sm)' }}>
            {!showArchived && <button className="btn btn-primary btn-sm" disabled={busy || !newProviderId} onClick={() => void handleStartSession()}>New session</button>}
            <button className="btn btn-secondary btn-sm" onClick={() => void loadSessions(null, showArchived)}>Refresh</button>
          </div>
        </div>

        {providers.filter((provider) => provider.state !== 'ready').map((provider) => (
          <div className="error-message" key={provider.provider_id}>
            <strong>{provider.display_name}:</strong> {provider.error?.message || `Provider is ${provider.state}.`}
          </div>
        ))}

        <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--spacing-sm)', minHeight: 0, flex: 1, overflowY: 'auto' }}>
          {!sessions.length ? <div className="card text-dim text-sm">No planning sessions found for this project yet.</div> : sessions.map((session) => (
            <button
              key={reactKey(session.key)}
              className={`card card-interactive ${sameKey(selectedKey, session.key) ? 'selected' : ''}`}
              style={{ textAlign: 'left', borderColor: sameKey(selectedKey, session.key) ? 'var(--color-primary)' : undefined }}
              onClick={() => setSelectedKey(session.key)}
            >
              <div style={{ display: 'flex', justifyContent: 'space-between', gap: 'var(--spacing-sm)' }}>
                <strong>{sessionTitle(session)}</strong>
                <span className="text-xs">{session.key.provider_id}</span>
              </div>
              <div className="text-xs text-dim">{session.status}{session.archived ? ' • archived' : ''}</div>
            </button>
          ))}
        </div>
      </aside>

      <section className="card" style={{ display: 'flex', flexDirection: 'column', minWidth: 0, minHeight: 0 }}>
        {error && <div style={{ padding: 'var(--spacing-md)' }}><div className="error-message">{error}</div></div>}
        {!selectedSession ? (
          <div style={{ padding: 'var(--spacing-lg)' }} className="text-dim">Select a planning session or start a new one.</div>
        ) : (
          <>
            <div style={{ padding: 'var(--spacing-md)', borderBottom: '1px solid var(--color-border)', display: 'flex', justifyContent: 'space-between', gap: 'var(--spacing-sm)', flexWrap: 'wrap' }}>
              <div>
                <div style={{ fontWeight: 600 }}>{sessionTitle(selectedSession)} <span className="text-xs">{selectedProvider?.display_name || selectedSession.key.provider_id}</span></div>
                <div className="text-xs text-dim mono">{selectedSession.cwd}</div>
              </div>
              <div style={{ display: 'flex', gap: 'var(--spacing-sm)', flexWrap: 'wrap' }}>
                {selectedSessionIsStale && <button className="btn btn-secondary btn-sm" onClick={() => void handleResumeOrFork('resume')}>Resume here</button>}
                {selectedSessionIsStale && selectedCapabilities.fork && <button className="btn btn-secondary btn-sm" onClick={() => void handleResumeOrFork('fork')}>Fork here</button>}
                {selectedCapabilities.archive && <button className="btn btn-secondary btn-sm" onClick={() => void handleArchive()}>{selectedSession.archived ? 'Unarchive' : 'Archive'}</button>}
                {selectedCapabilities.interruption && activeTurn && <button className="btn btn-secondary btn-sm" onClick={() => void handleInterrupt(activeTurn.turn_id)}>Interrupt</button>}
                <button className="btn btn-secondary btn-sm" onClick={() => void loadSession(selectedSession.key)}>Reload</button>
              </div>
            </div>

            {approvals.length > 0 && <div style={{ padding: 'var(--spacing-md)', borderBottom: '1px solid var(--color-border)' }}>
              <strong>Pending approvals</strong>
              {approvals.map((approval) => <div key={approval.approval_id} className="card" style={{ marginTop: 'var(--spacing-sm)' }}>
                <div className="text-sm">{approval.kind}: {approval.reason || 'Provider action requires approval.'}</div>
                <div style={{ display: 'flex', gap: 'var(--spacing-sm)', marginTop: 'var(--spacing-sm)' }}>
                  <button className="btn btn-primary btn-sm" onClick={() => void handleApproval(approval, 'accept')}>Accept</button>
                  <button className="btn btn-secondary btn-sm" onClick={() => void handleApproval(approval, 'decline')}>Decline</button>
                  <button className="btn btn-secondary btn-sm" onClick={() => void handleApproval(approval, 'cancel')}>Cancel</button>
                </div>
              </div>)}
            </div>}

            <div style={{ flex: 1, overflowY: 'auto', padding: 'var(--spacing-md)', display: 'flex', flexDirection: 'column', gap: 'var(--spacing-md)', minHeight: 0 }}>
              {loadingSession ? <div className="spinner" /> : !selectedSession.turns.length ? <div className="text-sm text-dim">No turn history loaded for this session.</div> : selectedSession.turns.map((turn, index) => {
                const content = stringifyTurn(turn)
                return <div key={turn.turn_id}>
                  <div className="text-sm text-dim">Turn {index + 1} • {turn.status}</div>
                  <div className="card"><pre style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word', margin: 0 }}>{content || JSON.stringify(turn.items, null, 2)}</pre>
                    {content && looksLikePlanMarkdown(content) && <button className="btn btn-primary btn-sm" style={{ marginTop: 'var(--spacing-md)' }} onClick={() => onSavePlanDraft(content)}>Save plan draft</button>}
                  </div>
                </div>
              })}
              <div ref={messagesEndRef} />
            </div>

            <div style={{ padding: 'var(--spacing-md)', borderTop: '1px solid var(--color-border)', display: 'flex', flexDirection: 'column', gap: 'var(--spacing-sm)' }}>
              <div style={{ display: 'flex', gap: 'var(--spacing-sm)', flexWrap: 'wrap' }}>
                {!!turnModels.length && <select className="input" aria-label="Model" value={turnModel} onChange={(event) => setTurnModel(event.target.value)}>
                  <option value="">Provider default model</option>
                  {turnModels.map((model) => <option key={model} value={model}>{model}{model === selectedSession.model && !selectedCapabilities.models.includes(model) ? ' (current, unavailable)' : ''}</option>)}
                </select>}
                {!!selectedCapabilities.reasoning_levels.length && <select className="input" aria-label="Reasoning level" value={turnReasoningLevel} onChange={(event) => setTurnReasoningLevel(event.target.value)}>
                  <option value="">Provider default reasoning</option>
                  {selectedCapabilities.reasoning_levels.map((level) => <option key={level} value={level}>{level}</option>)}
                </select>}
                {!!selectedCapabilities.reasoning_summaries.length && <select className="input" aria-label="Reasoning summary" value={turnReasoningSummary} onChange={(event) => setTurnReasoningSummary(event.target.value)}>
                  <option value="">Provider default summary</option>
                  {selectedCapabilities.reasoning_summaries.map((summary) => <option key={summary} value={summary}>{summary}</option>)}
                </select>}
              </div>
              {selectedCapabilities.attachments && supportsAnyAttachmentKind && <div>
                <input aria-label="Add attachments" type="file" multiple onChange={(event) => void handleFiles(event.target.files)} />
                {[...attachments.filter((item) => !uploads.some((upload) => upload.attachment?.attachment_id === item.attachment_id)), ...uploads.flatMap((upload) => upload.attachment ? [upload.attachment] : [])].map((attachment) => (
                  <div key={attachment.attachment_id} className="text-xs">{attachment.filename} ({attachment.kind}){uploads.some((upload) => upload.attachment?.attachment_id === attachment.attachment_id) ? ' — uploaded' : ''} <button className="btn btn-secondary btn-sm" onClick={() => void handleDeleteAttachment(attachment)}>Remove</button></div>
                ))}
                {uploads.filter((upload) => !upload.attachment).map((upload) => <div key={upload.localId} className="text-xs">
                  {upload.filename}: {upload.state}{upload.error ? ` — ${upload.error}` : ''}
                  {upload.state === 'failed' && <button className="btn btn-secondary btn-sm" onClick={() => setUploads((current) => current.filter((item) => item.localId !== upload.localId))}>Dismiss</button>}
                </div>)}
              </div>}
              {selectedCapabilities.attachments && !supportsAnyAttachmentKind && <div className="text-xs text-dim">This provider does not advertise any supported attachment kinds.</div>}
              {!selectedCapabilities.attachments && <div className="text-xs text-dim">This provider does not support attachments.</div>}
              <textarea className="textarea" placeholder="Send a new turn to this session" value={turnInput} onChange={(event) => setTurnInput(event.target.value)} />
              {uploadBlocksSend && <div className="error-message">Resolve failed uploads or wait for uploads to finish before sending.</div>}
              <div style={{ display: 'flex', justifyContent: 'flex-end' }}><button className="btn btn-primary" disabled={busy || !turnInput.trim() || uploadBlocksSend} onClick={() => void handleSendTurn()}>{busy ? <div className="spinner" /> : 'Send turn'}</button></div>
            </div>
          </>
        )}
      </section>
    </div>
  )
}
