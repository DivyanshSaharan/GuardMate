import { memo, useRef, useState, type FormEvent } from 'react'
import { sessionName } from './sessionState'
import type { Conversation, ConversationSummary } from './types'

export const SessionControls = memo(function SessionControls({
  sessions,
  conversation,
  pending,
  initializing,
  configured,
  onStart,
  onSelect,
  onEnd,
  onRefresh,
}: {
  sessions: ConversationSummary[]
  conversation: Conversation | null
  pending: boolean
  initializing: boolean
  configured: boolean
  onStart: (label: string) => Promise<boolean>
  onSelect: (id: string) => Promise<boolean>
  onEnd: () => Promise<boolean>
  onRefresh: () => Promise<unknown>
}) {
  const [label, setLabel] = useState('')
  const submitting = useRef(false)
  const busy = pending || initializing
  async function submit(event: FormEvent) {
    event.preventDefault()
    const trimmed = label.trim()
    if (busy || !configured || !trimmed || submitting.current) return
    submitting.current = true
    try {
      if (await onStart(trimmed)) setLabel('')
    } finally {
      submitting.current = false
    }
  }
  return (
    <section className="session-controls" aria-label="Courier sessions">
      <div className="session-fields">
        <label>
          Saved role-play
          <select
            value={conversation?.id ?? ''}
            disabled={busy || !sessions.length}
            onChange={(event) => void onSelect(event.target.value)}
          >
            <option value="" disabled>
              {initializing
                ? 'Loading saved role-plays…'
                : 'Choose a role-play'}
            </option>
            {sessions.map((session) => (
              <option key={session.id} value={session.id}>
                {sessionName(session)} · {session.status.replaceAll('_', ' ')}
                {session.has_pending_approval ? ' · approval pending' : ''}
                {session.courier_label ? ` · ${session.id.slice(0, 8)}` : ''}
              </option>
            ))}
          </select>
        </label>
        <form id="new-courier-session" onSubmit={submit}>
          <label>
            New courier label
            <input
              value={label}
              maxLength={80}
              disabled={busy}
              placeholder="For example: Courier A · parcel 1"
              onChange={(event) => setLabel(event.target.value)}
            />
          </label>
        </form>
      </div>
      <p className="muted session-note">
        Start another courier without ending this one. Each role-play has its
        own history and approvals. Labels are for testing, not verified callers.
      </p>
      <div className="lab-actions" role="group" aria-label="Role-play controls">
        <button
          className="button secondary"
          form="new-courier-session"
          type="submit"
          disabled={busy || !configured || !label.trim()}
        >
          Start role-play
        </button>
        <button
          className="button role-play-end"
          type="button"
          disabled={busy || !conversation || conversation.status === 'ended'}
          title="End only the selected role-play. Saved history is kept."
          onClick={() => void onEnd()}
        >
          <span className="stop-mark" aria-hidden="true" />
          End role-play
        </button>
        <button
          className="text-button"
          type="button"
          disabled={busy}
          onClick={() => void onRefresh()}
        >
          Refresh status
        </button>
        {conversation && (
          <span className="muted">
            {conversation.status.replaceAll('_', ' ')} ·{' '}
            {conversation.turn_count}/20 turns
          </span>
        )}
      </div>
    </section>
  )
})
