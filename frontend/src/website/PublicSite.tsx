import { useEffect, useState } from 'react'
import { Footer, Hero } from '../components/PageChrome'
import { Icon } from '../components/Icon'
import './website.css'

type Configuration = {
  oauth_ready: boolean
  download_ready: boolean
  signin_url: string
  budget_microdollars: number
}
type Account = {
  account_id: string
  budget_microdollars: number
  reserved_microdollars: number
  remaining_microdollars: number
}
type Credential = { credential_id: string; revoked: boolean }
const money = (value: number) => '$' + (value / 1_000_000).toFixed(2)

export function PublicSite() {
  const [configuration, setConfiguration] = useState<Configuration | null>(null)
  const [account, setAccount] = useState<Account | null>(null)
  const [error, setError] = useState('')
  const [demo, setDemo] = useState(false)
  const [credentials, setCredentials] = useState<Credential[] | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    const controller = new AbortController()
    fetch('/api/cloud/config', { signal: controller.signal })
      .then((response) => {
        if (!response.ok) throw new Error()
        return response.json() as Promise<Configuration>
      })
      .then(setConfiguration)
      .catch(() => {
        if (!controller.signal.aborted)
          setError('Unable to reach GuardMate. Please try again.')
      })
    fetch('/api/cloud/me', { signal: controller.signal })
      .then((response) => (response.ok ? response.json() : null))
      .then(setAccount)
      .catch(() => {})
    return () => controller.abort()
  }, [])

  async function logout() {
    setBusy(true)
    try {
      const response = await fetch('/api/cloud/logout', { method: 'POST' })
      if (!response.ok) throw new Error()
      setAccount(null)
      setCredentials(null)
    } catch {
      setError('Sign-out could not be confirmed. Please try again.')
    } finally {
      setBusy(false)
    }
  }

  async function manageCopies() {
    setBusy(true)
    try {
      const response = await fetch('/api/cloud/credentials')
      if (!response.ok) throw new Error()
      const result = (await response.json()) as { credentials: Credential[] }
      setCredentials(result.credentials)
    } catch {
      setError(
        'Your downloaded copies could not be loaded. Please sign in again.',
      )
    } finally {
      setBusy(false)
    }
  }

  async function revoke(credentialId: string) {
    setBusy(true)
    try {
      const response = await fetch(
        '/api/cloud/credentials/' +
          encodeURIComponent(credentialId) +
          '/revoke',
        { method: 'POST' },
      )
      if (!response.ok) throw new Error()
      setCredentials(
        (current) =>
          current?.map((item) =>
            item.credential_id === credentialId
              ? { ...item, revoked: true }
              : item,
          ) ?? null,
      )
    } catch {
      setError('That copy could not be disabled. Please try again.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="app-shell website">
      <header className="header">
        <a className="brand" href="/" aria-label="GuardMate home">
          <span className="brand-symbol">
            <Icon name="shield" size={25} />
          </span>
          <span>
            GuardMate<span className="brand-dot">.</span>
          </span>
        </a>
        <nav aria-label="Main navigation">
          <a href="#how-it-works">How it works</a>
          <a href="#demo">Demo</a>
          <a href="#download">Download</a>
        </nav>
        <a
          className="private-label"
          href="https://github.com/DivyanshSaharan/GuardMate"
        >
          View source
        </a>
      </header>
      <main>
        <Hero />
        <div className="website-intro">
          <a className="website-primary" href="#download">
            Get GuardMate for Windows
          </a>
          <a className="website-secondary" href="#demo">
            See a delivery conversation
          </a>
          <p>
            Built for a friend. Made for the courier calls that arrive during
            your day.
          </p>
        </div>
        {error && (
          <p className="website-error" role="alert">
            {error}
          </p>
        )}

        <section id="how-it-works" className="website-section">
          <div className="section-heading">
            <h2>Your phone. Your instructions. A little backup.</h2>
          </div>
          <div className="website-steps">
            <article>
              <span>01</span>
              <h3>Set your delivery plan</h3>
              <p>
                Save where the guard room is, your routine, and when to ask you
                first.
              </p>
            </article>
            <article>
              <span>02</span>
              <h3>Connect your phone</h3>
              <p>
                The Windows app guides you through Phone Link pairing and checks
                your call audio.
              </p>
            </article>
            <article>
              <span>03</span>
              <h3>Stay in control</h3>
              <p>
                Answer the call, review what the courier said, and choose when
                GuardMate speaks.
              </p>
            </article>
          </div>
        </section>

        <section id="demo" className="website-section website-demo">
          <div>
            <div className="eyebrow">A FICTIONAL DELIVERY</div>
            <h2>A safe handoff starts with the right questions.</h2>
            <p>
              This prepared example shows the delivery flow. Real calls run
              through the Windows app.
            </p>
            <button
              className="website-secondary"
              onClick={() => setDemo(!demo)}
              aria-expanded={demo}
            >
              {demo ? 'Close example' : 'Open conversation example'}
            </button>
          </div>
          <div
            className="website-conversation"
            aria-label="Prepared conversation example"
          >
            <p className="website-resident">
              Resident’s plan: at the office · prepaid parcels with security
            </p>
            <p className="website-courier">
              <strong>Courier</strong> I have a parcel for you.
            </p>
            <p className="website-assistant">
              <strong>GuardMate</strong> Is the parcel prepaid?
            </p>
            {demo && (
              <>
                <p className="website-courier">
                  <strong>Courier</strong> Yes, it’s prepaid.
                </p>
                <p className="website-assistant">
                  <strong>GuardMate</strong> Is the security guard there?
                </p>
                <p className="website-courier">
                  <strong>Courier</strong> The guard isn’t here. Can I leave it
                  at reception?
                </p>
                <p className="website-assistant">
                  <strong>GuardMate</strong> I’ll ask the resident before
                  changing the location.
                </p>
                <div className="website-decision">
                  Resident approval required · alternative location
                </div>
              </>
            )}
          </div>
        </section>

        <section id="download" className="website-section website-download">
          <div>
            <div className="eyebrow">GUARDMATE FOR WINDOWS</div>
            <h2>Take your delivery plan with you.</h2>
            <p>
              Sign in to download your account’s installer. Setup installs the
              app and speech models, then helps you connect your phone.
            </p>
            <p>
              Windows x64, an Android phone, Bluetooth, and internet access are
              required. Phone pairing and permissions need your confirmation.
            </p>
            <p>
              The initial installer is unsigned and Windows may show a publisher
              warning. Calling is supervised: you answer and hang up.
            </p>
          </div>
          <div className="website-account">
            {account ? (
              <>
                <h3>Your GuardMate allowance</h3>
                <div className="website-balance">
                  {money(account.remaining_microdollars)}
                </div>
                <p>
                  remaining of {money(account.budget_microdollars)} · estimated
                  reservations
                </p>
                <p>
                  Reinstalling or using copied installers keeps this same
                  account allowance.
                </p>
                {configuration?.download_ready ? (
                  <a
                    className="website-primary"
                    href="/api/cloud/download/windows"
                  >
                    Download for Windows
                  </a>
                ) : (
                  <p role="status">The Windows download is being prepared.</p>
                )}
                <div className="website-account-actions">
                  <button
                    className="website-secondary"
                    onClick={manageCopies}
                    disabled={busy}
                  >
                    Manage downloaded copies
                  </button>
                  <button
                    className="website-secondary"
                    onClick={logout}
                    disabled={busy}
                  >
                    Sign out
                  </button>
                </div>
                {credentials && (
                  <div>
                    <p>
                      Disabling a download also disables installations activated
                      from its copies.
                    </p>
                    {credentials.length === 0 && (
                      <p>No installers downloaded yet.</p>
                    )}
                    {credentials.map((item, index) => (
                      <div className="website-copy" key={item.credential_id}>
                        <span>
                          Download {index + 1} ·{' '}
                          {item.revoked ? 'Disabled' : 'Active'}
                        </span>
                        {!item.revoked && (
                          <button
                            onClick={() => revoke(item.credential_id)}
                            disabled={busy}
                          >
                            Disable
                          </button>
                        )}
                      </div>
                    ))}
                  </div>
                )}
              </>
            ) : (
              <>
                <h3>One account. Your own allowance.</h3>
                <p>
                  New accounts receive{' '}
                  {money(configuration?.budget_microdollars ?? 1_000_000)} of
                  estimated AI allowance, provided by the project owner.
                </p>
                {configuration?.oauth_ready ? (
                  <a
                    className="website-primary"
                    href="/api/cloud/auth/google/start"
                  >
                    Continue with Google
                  </a>
                ) : (
                  <p role="status">
                    {configuration
                      ? 'Sign-in is being configured. You can explore the example above.'
                      : 'Checking sign-in availability…'}
                  </p>
                )}
                <p>
                  No payment is collected from you. Speech runs locally;
                  reviewed text and delivery context go to GuardMate and Tinker
                  for AI replies.
                </p>
              </>
            )}
          </div>
        </section>
        <section className="website-section website-privacy" id="privacy">
          <h2>Your instructions stay close.</h2>
          <p>
            The desktop app saves resident preferences and conversations on your
            computer. The server stores your account identity and estimated AI
            usage. Call audio is processed locally. Submitted text and context
            are sent to Tinker; GuardMate is not fully offline.
          </p>
          <p>
            GuardMate checks proposed actions before speaking. Payments, OTPs,
            signatures, sensitive requests, and unapproved locations need
            resident involvement. Caller labels do not verify identity, and
            reported delivery is not proof of receipt.
          </p>
          <p>
            Sign-in uses Google’s account identity. Download credentials can be
            disabled through your account above. For privacy questions, contact
            the maintainer through the
            <a href="https://github.com/DivyanshSaharan/GuardMate">
              {' '}
              project repository
            </a>
            .
          </p>
        </section>
      </main>
      <Footer />
    </div>
  )
}
