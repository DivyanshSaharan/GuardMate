import { useEffect, useState } from 'react'
import { CallPanel } from './CallPanel'
import { DesktopError, desktopRequest, type DesktopStatus } from './client'
import './desktop.css'

export function DesktopSetup() {
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
  }, [])

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
    <section className="card desktop-setup" aria-label="Windows desktop setup">
      <h2>Windows phone setup</h2>
      <p>
        Use your paired phone through Microsoft Phone Link. You complete
        Microsoft sign-in, QR/Bluetooth pairing and call permissions yourself.
      </p>
      <p role="status">
        Phone Link:{' '}
        {status.phone_link_installed
          ? 'installed; pairing and call state are not verified'
          : 'not detected'}
        .
      </p>
      <div className="lab-actions">
        <button
          className="button secondary"
          disabled={pending || !status.windows}
          onClick={() => void open('install')}
        >
          Install or enable Phone Link
        </button>
        <button
          className="button secondary"
          disabled={pending || !status.windows}
          onClick={() => void open('open')}
        >
          Open Phone Link and pair phone
        </button>
        <button
          className="text-button"
          disabled={pending}
          onClick={() => void refresh()}
        >
          Refresh desktop readiness
        </button>
        <button
          className="text-button"
          disabled={pending}
          onClick={() => void quit()}
        >
          Quit GuardMate
        </button>
      </div>
      <p className="muted">
        Installation opens the official Microsoft Store; you approve its terms
        and final install action there. Pair the phone in Phone Link, turn on
        Bluetooth, and enable Calls. Keep both devices nearby.
      </p>
      {notice && <p role="status">{notice}</p>}
      {error && <p role="alert">{error}</p>}
      <CallPanel status={status} />
    </section>
  )
}
