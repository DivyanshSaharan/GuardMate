import { memo, useEffect, useRef, useState, type FormEvent } from 'react'
import { VoiceRecorder } from './speech/VoiceRecorder'

export const ConversationComposer = memo(function ConversationComposer({
  pending,
  disabled,
  onSend,
  sessionId,
  draftStore,
  speechReady = false,
  recordingBlocked = false,
  onVoiceBusyChange,
}: {
  pending: boolean
  disabled: boolean
  onSend: (text: string) => Promise<boolean>
  sessionId: string
  draftStore?: Map<string, string>
  speechReady?: boolean
  recordingBlocked?: boolean
  onVoiceBusyChange?: (busy: boolean) => void
}) {
  const [draft, setDraft] = useState(() => draftStore?.get(sessionId) ?? '')
  function updateDraft(value: string) {
    setDraft(value)
    if (value) draftStore?.set(sessionId, value)
    else draftStore?.delete(sessionId)
  }
  const inputRef = useRef<HTMLInputElement>(null)
  const submitting = useRef(false)
  const submitted = useRef(false)
  useEffect(() => {
    if (!pending && !disabled && submitted.current) inputRef.current?.focus()
  }, [pending, disabled])

  async function submit(event: FormEvent) {
    event.preventDefault()
    const text = draft.trim()
    if (!text || pending || disabled || submitting.current) return
    submitting.current = true
    submitted.current = true
    updateDraft('')
    try {
      if (!(await onSend(text))) updateDraft(text)
    } finally {
      submitting.current = false
    }
  }
  return (
    <form
      className="conversation-composer"
      onSubmit={submit}
      aria-busy={pending}
    >
      <label htmlFor="courier-message">Courier message</label>
      <div>
        <input
          ref={inputRef}
          id="courier-message"
          value={draft}
          maxLength={600}
          disabled={pending || disabled}
          placeholder={
            pending ? 'Waiting for GuardMate…' : 'Type your delivery question…'
          }
          onChange={(event) => updateDraft(event.target.value)}
        />
        <button
          className="button primary"
          disabled={pending || disabled || !draft.trim()}
        >
          {pending ? 'Waiting…' : 'Send'}
        </button>
      </div>
      <VoiceRecorder
        sessionId={sessionId}
        blocked={pending || disabled || recordingBlocked}
        ready={speechReady}
        draft={draft}
        onUse={updateDraft}
        onBusyChange={onVoiceBusyChange}
      />
    </form>
  )
})
