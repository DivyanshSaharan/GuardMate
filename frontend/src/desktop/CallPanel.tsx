import { useEffect, useRef, useState } from 'react'
import { request } from '../api'
import { ResidentControls } from '../agent/ResidentControls'
import type { Conversation, ResidentDecision } from '../agent/types'
import {
  desktopRequest,
  type CallState,
  type DesktopStatus,
  type Endpoint,
} from './client'

const confirmations = [
  [
    'call_answered',
    'I manually answered this call in Phone Link, and it is still active.',
  ],
  [
    'consenting_call',
    'Both participants agree to this supervised call test and local audio processing.',
  ],
  [
    'hosted_text_consent',
    'Both agree that reviewed text, conversation history and resident context go to hosted Qwen/Tinker.',
  ],
  [
    'exclusive_audio_risk',
    'I understand exclusive phone audio can interrupt human audio; I will stay present.',
  ],
  [
    'phone_endpoints_confirmed',
    'Both selected audio endpoints belong to this paired phone, not the laptop microphone or speakers.',
  ],
] as const

export function CallPanel({ status }: { status: DesktopStatus }) {
  const [devices, setDevices] = useState<Endpoint[]>([])
  const [phoneName, setPhoneName] = useState('')
  const [input, setInput] = useState('')
  const [output, setOutput] = useState('')
  const [label, setLabel] = useState('')
  const [confirmed, setConfirmed] = useState<Record<string, boolean>>({})
  const [call, setCall] = useState<CallState | null>(null)
  const [callLoaded, setCallLoaded] = useState(false)
  const callRevision = useRef(0)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const [draft, setDraft] = useState('')
  useEffect(() => {
    setDraft(call?.draft ?? '')
  }, [call?.draft])
  useEffect(() => {
    let cancelled = false
    const revision = callRevision.current
    void desktopRequest<CallState>('/call')
      .then((current) => {
        if (cancelled || revision !== callRevision.current) return
        setCall(current)
        setCallLoaded(true)
      })
      .catch(() => {
        if (!cancelled && revision === callRevision.current)
          setError(
            'Call status is unavailable. Refresh this page before using the call assistant.',
          )
      })
    return () => {
      cancelled = true
    }
  }, [])
  useEffect(() => {
    if (!pending && (!call?.conversation || call.stopped)) return
    const timer = setInterval(() => {
      callRevision.current++
      void desktopRequest<CallState>('/call')
        .then(setCall)
        .catch(() => {
          setError(
            'Local call status is unavailable. Handle the physical call manually.',
          )
        })
    }, 1000)
    return () => clearInterval(timer)
  }, [pending, call?.conversation?.id, call?.stopped])

  const busy = !callLoaded || pending || !!call?.busy
  const active = !!call?.conversation && !call.stopped
  const paused = call?.conversation?.status !== 'active'
  const ready =
    status.windows &&
    status.phone_link_installed &&
    status.setup_complete &&
    status.delivery_mode_active &&
    status.model_configured &&
    status.stt_ready &&
    status.tts_ready
  const phoneDevices =
    phoneName.trim().length >= 3
      ? devices.filter((device) =>
          device.name.toLowerCase().includes(phoneName.trim().toLowerCase()),
        )
      : []
  const source = phoneDevices.find(
    (device) => device.direction === 'input' && String(device.id) === input,
  )
  const target = phoneDevices.find(
    (device) => device.direction === 'output' && String(device.id) === output,
  )

  async function perform(path: string, body: unknown = {}) {
    callRevision.current++
    setPending(true)
    setError('')
    try {
      setCall(await desktopRequest<CallState>(`/call/${path}`, body))
      setCallLoaded(true)
      return true
    } catch (failure) {
      setError(
        failure instanceof Error ? failure.message : 'The call action failed.',
      )
      try {
        setCall(await desktopRequest<CallState>('/call'))
      } catch {
        /* Keep the last known session. */
      }
      return false
    } finally {
      setPending(false)
    }
  }

  async function scan() {
    setPending(true)
    setError('')
    setInput('')
    setOutput('')
    try {
      const inventory = await desktopRequest<{ devices: Endpoint[] }>(
        '/devices/scan',
        {},
      )
      setDevices(inventory.devices)
    } catch (failure) {
      setDevices([])
      setError(
        failure instanceof Error
          ? failure.message
          : 'Phone endpoint inspection failed.',
      )
    } finally {
      setPending(false)
    }
  }

  async function stop() {
    callRevision.current++
    try {
      setCall(await desktopRequest<CallState>('/call/stop', {}))
      setCallLoaded(true)
    } catch {
      setError(
        'Stop could not be confirmed. End the physical call in Phone Link.',
      )
    }
  }

  async function decide(decision: ResidentDecision) {
    const session = call?.conversation
    if (!session || busy) return false
    callRevision.current++
    setPending(true)
    try {
      await request<Conversation>(`/conversations/${session.id}/resident`, {
        decision,
        revision: session.revision,
        approval_id: session.approval?.id ?? null,
      })
      setCall(await desktopRequest<CallState>('/call/refresh', {}))
      return true
    } catch (failure) {
      setError(
        failure instanceof Error
          ? failure.message
          : 'Resident decision could not be saved.',
      )
      return false
    } finally {
      setPending(false)
    }
  }

  return (
    <section aria-label="Supervised phone call" className="desktop-call-panel">
      <h3>Supervised phone call</h3>
      <p>
        Answer in Phone Link yourself. Recording, sending reviewed text and
        playing a reply are separate actions. There is no automatic answering or
        hangup.
      </p>
      {!callLoaded && !error && (
        <p role="status">Checking local call status…</p>
      )}
      {!ready && (
        <p role="status">
          Complete the items marked above before starting. You can also try a
          text conversation below while you finish phone setup.
        </p>
      )}
      <details className="desktop-call-setup">
        <summary>Configure phone audio and call consent</summary>
        <div className="desktop-device-grid">
          <label>
            Paired phone name
            <input
              value={phoneName}
              maxLength={120}
              disabled={active || busy}
              placeholder="Name shown in the phone audio endpoints"
              onChange={(event) => {
                setPhoneName(event.target.value)
                setInput('')
                setOutput('')
              }}
            />
          </label>
          <label>
            Call label
            <input
              value={label}
              maxLength={80}
              disabled={active || busy}
              placeholder="For example: Fictional courier call"
              onChange={(event) => setLabel(event.target.value)}
            />
          </label>
        </div>
        <div className="desktop-discovery">
          <button
            className="button secondary"
            disabled={busy || active}
            onClick={() => void scan()}
          >
            Inspect phone audio endpoints
          </button>
          <p className="muted">
            Lists audio devices without recording or placing a call. Select both
            endpoints for your paired phone.
          </p>
        </div>
        {devices.length > 0 && phoneDevices.length === 0 && (
          <p className="muted" role="status">
            Enter at least three characters of the phone name shown in Windows
            audio settings to find its endpoints.
          </p>
        )}
        <div className="desktop-audio-grid">
          {(['input', 'output'] as const).map((direction) => (
            <label key={direction}>
              {direction === 'input'
                ? 'Phone input (caller audio)'
                : 'Phone output (reply to caller)'}
              <select
                value={direction === 'input' ? input : output}
                disabled={active || busy}
                onChange={(event) =>
                  direction === 'input'
                    ? setInput(event.target.value)
                    : setOutput(event.target.value)
                }
              >
                <option value="">Select the paired phone endpoint</option>
                {phoneDevices
                  .filter((device) => device.direction === direction)
                  .map((device) => (
                    <option key={device.id} value={device.id}>
                      {device.name} · {device.host_api} · {device.id}
                    </option>
                  ))}
              </select>
            </label>
          ))}
        </div>
        {!active && (
          <fieldset>
            <legend>Confirm this answered call</legend>
            {confirmations.map(([key, text]) => (
              <label key={key} className="desktop-consent">
                <input
                  type="checkbox"
                  checked={!!confirmed[key]}
                  disabled={busy}
                  onChange={(event) =>
                    setConfirmed((current) => ({
                      ...current,
                      [key]: event.target.checked,
                    }))
                  }
                />
                <span>{text}</span>
              </label>
            ))}
          </fieldset>
        )}
      </details>
      <div className="lab-actions">
        <button
          className="button primary"
          disabled={
            active ||
            busy ||
            !ready ||
            !source ||
            !target ||
            !label.trim() ||
            confirmations.some(([key]) => !confirmed[key])
          }
          onClick={() => {
            void perform('start', {
              phone_name: phoneName.trim(),
              input: source,
              output: target,
              label: label.trim(),
              seconds: 5,
              max_turns: 5,
              ...confirmed,
            })
            setConfirmed({})
          }}
        >
          Start answered call
        </button>
        <button
          className="button secondary"
          disabled={!active && !busy}
          onClick={() => void stop()}
        >
          Stop call assistant
        </button>
      </div>
      <p>
        <strong>Physical hangup stays manual.</strong> Stop halts local audio
        and prevents further actions. A hosted request already running can
        finish; it will not be retried.
      </p>
      {(error || call?.error) && <p role="alert">{error || call?.error}</p>}
      {call?.stopping && (
        <p role="status">Stopping. Handle the physical call in Phone Link.</p>
      )}
      {call?.uncertain && (
        <p role="alert">
          An operation has an uncertain result. Do not resend or replay it.
          Handle the call manually and stop the assistant.
        </p>
      )}
      {call?.conversation && (
        <>
          <p role="status">
            {call.conversation.courier_label} ·{' '}
            {call.conversation.status.replaceAll('_', ' ')} ·{' '}
            {call.model_attempts}/{call.max_turns} model turns
          </p>
          <ol aria-label="Phone conversation" className="conversation-messages">
            {call.conversation.messages.map((message, index) => (
              <li key={index} className={`message-${message.role}`}>
                <span>
                  {message.role === 'assistant' ? 'GuardMate' : message.role}
                </span>
                <p>{message.content}</p>
              </li>
            ))}
          </ol>
          <div className="lab-actions">
            <button
              className="button secondary"
              disabled={
                !active ||
                busy ||
                paused ||
                call.uncertain ||
                call.draft !== null
              }
              onClick={() => void perform('capture')}
            >
              Capture 5-second caller turn
            </button>
            <button
              className="text-button"
              disabled={!active || busy || call.uncertain}
              onClick={() => void perform('refresh')}
            >
              Refresh call and discard draft
            </button>
          </div>
          {call.draft !== null && (
            <>
              <label>
                Review captured transcript
                <textarea
                  value={draft}
                  maxLength={600}
                  rows={3}
                  disabled={busy || call.uncertain}
                  onChange={(event) => setDraft(event.target.value)}
                />
              </label>
              <p>
                Correct any transcription errors. Sending shares the reviewed
                text and saved context with Tinker.
              </p>
              <div className="lab-actions">
                <button
                  className="button primary"
                  disabled={busy || paused || call.uncertain || !draft.trim()}
                  onClick={() => void perform('send', { text: draft.trim() })}
                >
                  Send reviewed text
                </button>
                <button
                  className="button secondary"
                  disabled={busy || call.uncertain}
                  onClick={() => void perform('discard')}
                >
                  Discard captured transcript
                </button>
              </div>
            </>
          )}
          <div className="lab-actions">
            <button
              className="button secondary"
              disabled={
                !active ||
                busy ||
                call.uncertain ||
                call.draft !== null ||
                call.conversation.messages.at(-1)?.role !== 'assistant'
              }
              onClick={() => void perform('play', { repeat: false })}
            >
              Play latest reply to caller
            </button>
            <button
              className="text-button"
              disabled={
                !active ||
                busy ||
                call.uncertain ||
                call.draft !== null ||
                call.conversation.messages.at(-1)?.role !== 'assistant'
              }
              onClick={() => void perform('play', { repeat: true })}
            >
              Explicitly replay latest reply
            </button>
          </div>
          <ResidentControls
            conversation={call.conversation}
            pending={busy || call.stopped || call.uncertain}
            onDecision={decide}
          />
          {busy && (
            <p role="status">
              Working:{' '}
              {call.events.at(-1)?.event.replaceAll('_', ' ') ??
                'checking call readiness'}
              . Stay present.
            </p>
          )}
        </>
      )}
    </section>
  )
}
