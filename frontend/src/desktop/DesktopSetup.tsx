import { useEffect, useState } from 'react'
import { CallPanel } from './CallPanel'
import { DesktopError, desktopRequest, type DesktopStatus } from './client'
import './desktop.css'

export function DesktopSetup({
  setupComplete,
  deliveryModeActive,
}: {
  setupComplete?: boolean
  deliveryModeActive?: boolean
}) {
  const [status, setStatus] = useState<DesktopStatus | null>(null)
  const [error, setError] = useState('')
  const [pending, setPending] = useState(false)
  const [notice, setNotice] = useState('')
  const [closed, setClosed] = useState(false)

  async function quit() {
    setPending(true)
    try {
      await desktopRequest('/quit', {})
      setClosed(true)
    } catch {
      setError(
        'GuardMate could not confirm shutdown. Stop the call assistant and try again.',
      )
    } finally {
      setPending(false)
    }
  }

  async function refresh() {
    setPending(true)
    try {
      setStatus(await desktopRequest<DesktopStatus>('/status'))
      setError('')
    } catch (failure) {
      if (failure instanceof DesktopError && failure.status === 404) return
      setError(
        failure instanceof Error
          ? failure.message
          : 'Desktop readiness is unavailable.',
      )
    } finally {
      setPending(false)
    }
  }
  useEffect(() => {
    void refresh()
  }, [setupComplete, deliveryModeActive])

  async function open(action: 'open' | 'install') {
    setPending(true)
    setError('')
    try {
      await desktopRequest(`/phone-link/${action}`, {})
      setNotice(
        action === 'install'
          ? 'Complete installation in Microsoft Store, then return and refresh.'
          : 'Finish Microsoft sign-in, phone pairing, Bluetooth calls and permissions in Phone Link. Then refresh.',
      )
    } catch (failure) {
      setError(
        failure instanceof Error
          ? failure.message
          : 'Phone Link could not open.',
      )
    } finally {
      setPending(false)
    }
  }

  if (closed)
    return (
      <p role="status">GuardMate has closed. You can close this browser tab.</p>
    )
  if (!status?.desktop) return error ? <p role="status">{error}</p> : null
  return (
    <section
      id="desktop"
      className="card desktop-setup"
      aria-label="Windows desktop setup"
    >
      <div className="desktop-section-heading">
        <div>
          <h2>Windows phone setup</h2>
          <p className="muted">Check your setup before a supervised call.</p>
        </div>
        <button
          className="text-button"
          disabled={pending}
          onClick={() => void quit()}
        >
          Quit GuardMate
        </button>
      </div>
      <ul className="desktop-readiness" aria-label="Setup readiness">
        <li data-ready={status.setup_complete}>
          <span>{status.setup_complete ? 'Ready' : 'Needed'}</span>
          {status.setup_complete ? (
            'Resident preferences saved'
          ) : (
            <a href="#preferences">Save your resident preferences</a>
          )}
        </li>
        <li data-ready={status.delivery_mode_active}>
          <span>{status.delivery_mode_active ? 'Ready' : 'Needed'}</span>
          {status.delivery_mode_active ? (
            'Delivery window active'
          ) : (
            <a href="#today">Enable a delivery window</a>
          )}
        </li>
        <li data-ready={status.model_configured}>
          <span>{status.model_configured ? 'Ready' : 'Unavailable'}</span>
          Cloud connection
        </li>
        <li data-ready={status.stt_ready}>
          <span>{status.stt_ready ? 'Ready' : 'Missing'}</span>
          Local speech recognition
        </li>
        <li data-ready={status.tts_ready}>
          <span>{status.tts_ready ? 'Ready' : 'Missing'}</span>
          Local voice playback
        </li>
      </ul>
      <p>
        Pair your phone in Microsoft Phone Link, turn on Bluetooth, and enable
        Calls. Keep both devices nearby.
      </p>
      <p role="status" className="desktop-status">
        Phone Link:{' '}
        {status.phone_link_installed
          ? 'installed; pairing and call state are not verified'
          : 'not detected'}
        .
      </p>
      <div className="lab-actions">
        {status.phone_link_installed ? (
          <button
            className="button secondary"
            disabled={pending || !status.windows}
            onClick={() => void open('open')}
          >
            Open Phone Link and pair phone
          </button>
        ) : (
          <button
            className="button secondary"
            disabled={pending || !status.windows}
            onClick={() => void open('install')}
          >
            Install or enable Phone Link
          </button>
        )}
        <button
          className="text-button"
          disabled={pending}
          onClick={() => void refresh()}
        >
          Refresh desktop readiness
        </button>
      </div>
      {!status.phone_link_installed && (
        <p className="muted">
          Installation opens the official Microsoft Store. Complete its install
          steps, then refresh readiness here.
        </p>
      )}
      {notice && <p role="status">{notice}</p>}
      {error && <p role="alert">{error}</p>}
      <CallPanel status={status} />
    </section>
  )
}
