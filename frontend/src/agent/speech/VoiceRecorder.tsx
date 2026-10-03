import { useEffect, useId } from 'react'
import { useVoiceRecorder } from './useVoiceRecorder'

export function VoiceRecorder({
  sessionId,
  blocked,
  ready,
  draft,
  onUse,
  onBusyChange,
}: {
  sessionId: string
  blocked: boolean
  ready: boolean
  draft: string
  onUse: (text: string) => void
  onBusyChange?: (busy: boolean) => void
}) {
  const voice = useVoiceRecorder(sessionId, blocked)
  const labelId = useId()
  const combined = [draft.trim(), voice.transcript.trim()]
    .filter(Boolean)
    .join(' ')
  const busy = ['requesting', 'recording', 'transcribing'].includes(voice.phase)
  useEffect(() => {
    onBusyChange?.(voice.phase !== 'idle')
  }, [voice.phase, onBusyChange])
  return (
    <div className="voice-recorder">
      {voice.phase === 'idle' && (
        <button
          type="button"
          className="button secondary"
          disabled={blocked || !ready}
          onClick={() => void voice.start()}
        >
          Record courier message
        </button>
      )}
      {busy && (
        <div className="voice-actions">
          <span role="status">
            {voice.phase === 'requesting'
              ? 'Requesting microphone permission…'
              : voice.phase === 'recording'
                ? 'Recording — stops after 30 seconds.'
                : 'Transcribing locally…'}
          </span>
          {voice.phase === 'recording' && (
            <button
              type="button"
              className="button secondary"
              onClick={() => void voice.stop()}
            >
              Stop recording
            </button>
          )}
          <button
            type="button"
            className="button secondary"
            onClick={voice.cancel}
          >
            Cancel recording
          </button>
        </div>
      )}
      {voice.phase === 'review' && (
        <div className="voice-review">
          <label htmlFor={labelId}>Review the local transcript</label>
          <textarea
            id={labelId}
            value={voice.transcript}
            maxLength={600}
            onChange={(event) => voice.setTranscript(event.target.value)}
            disabled={blocked}
            rows={3}
          />
          <p>
            {draft.trim()
              ? 'Use transcript appends it to your existing draft. Nothing is sent yet.'
              : 'Edit any mistakes, then use the transcript in your draft. Nothing is sent yet.'}
          </p>
          {combined.length > 600 && (
            <p className="voice-error">
              The combined draft is longer than 600 characters. Shorten it
              before using the transcript.
            </p>
          )}
          <div className="voice-actions">
            <button
              type="button"
              className="button secondary"
              disabled={
                blocked || !voice.transcript.trim() || combined.length > 600
              }
              onClick={() => {
                onUse(combined)
                voice.cancel()
              }}
            >
              Use transcript
            </button>
            <button
              type="button"
              className="button secondary"
              onClick={voice.cancel}
            >
              Discard transcript
            </button>
          </div>
        </div>
      )}
      {voice.error && (
        <p className="voice-error" role="alert">
          {voice.error}
        </p>
      )}
    </div>
  )
}
