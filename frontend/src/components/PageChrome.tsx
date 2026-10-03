import { memo } from 'react'
import { formatDate } from '../lib/presentation'
import { Icon } from './Icon'

export const Header = memo(function Header() {
  return (
    <header className="header">
      <a className="brand" href="#today" aria-label="GuardMate home">
        <span className="brand-symbol">
          <Icon name="shield" size={25} />
        </span>
        <span>
          GuardMate<span className="brand-dot">.</span>
        </span>
      </a>
      <nav aria-label="Main navigation">
        <a className="nav-active" href="#today">
          Today
        </a>
        <a href="#preferences">Your preferences</a>
      </nav>
      <span className="private-label">
        <Icon name="shield" size={15} /> Saved on your device
      </span>
    </header>
  )
})

export const Hero = memo(function Hero() {
  return (
    <>
      <section className="hero">
        <div>
          <div className="eyebrow">
            <span className="small-line" /> A LITTLE BACKUP FOR PG LIFE
          </div>
          <h1>
            Your day goes on.
            <br />
            <span>Your parcels have a plan.</span>
          </h1>
          <p>
            One place for the instructions your delivery assistant will follow.
            <br className="desktop-break" /> Set your routine, then get back to
            your day.
          </p>
        </div>
        <div className="parcel-illustration" aria-hidden="true">
          <div className="illustration-orbit" />
          <div className="parcel">
            <div className="parcel-tape" />
            <span className="parcel-label">
              <Icon name="box" size={27} />
              <span>
                HANDLE
                <br />
                WITH CARE
              </span>
            </span>
            <span className="parcel-number">GM / 001</span>
          </div>
          <div className="illustration-check">
            <Icon name="check" size={29} />
          </div>
          <span className="illustration-caption">A safe place to land.</span>
        </div>
      </section>
      <div className="prototype-note">
        <Icon name="info" size={18} />
        <p>
          GuardMate is being built in stages.{' '}
          <strong>
            Voice handling is not connected yet; this version does not answer
            calls.
          </strong>
        </p>
      </div>
    </>
  )
})

export const TodayHeading = memo(function TodayHeading({
  localDate,
}: {
  localDate?: string
}) {
  return (
    <div className="section-heading">
      <h2>Today’s delivery plan</h2>
      <span>{formatDate(localDate)} · IST</span>
    </div>
  )
})

export const Footer = memo(function Footer() {
  return (
    <footer className="footer">
      <span>
        <Icon name="shield" size={16} /> Your instructions. Your call.
      </span>
      <span>Built for a friend, and the parcels that matter.</span>
    </footer>
  )
})
