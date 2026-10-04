import { memo, useState } from 'react'
import type { FormEvent } from 'react'
import { sameProfile } from '../lib/dashboard'
import { availabilityChoices, officeDays } from '../lib/presentation'
import { emptyProfile } from '../types'
import type { Availability, ResidentProfile } from '../types'
import { Icon } from './Icon'

interface Props {
  profile?: ResidentProfile
  ready: boolean
  disabled: boolean
  pending: boolean
  onSave: (profile: ResidentProfile) => Promise<ResidentProfile | null>
}

export const PreferencesForm = memo(function PreferencesForm({
  profile,
  ready,
  disabled,
  pending,
  onSave,
}: Props) {
  const [editedProfile, setEditedProfile] = useState<ResidentProfile | null>(
    null,
  )
  const saved = profile ?? emptyProfile
  const draft = editedProfile ?? saved
  const dirty = !sameProfile(draft, saved)
  const blocked = disabled || pending
  function change<K extends keyof ResidentProfile>(
    key: K,
    value: ResidentProfile[K],
  ) {
    setEditedProfile((current) => ({ ...(current ?? saved), [key]: value }))
  }
  async function save(event: FormEvent) {
    event.preventDefault()
    if (blocked || !dirty) return
    const accepted = await onSave(draft)
    if (accepted) setEditedProfile(null)
  }
  const savedDays = saved.office_days.map((day) => officeDays[day]).join(', ')
  return (
    <section id="preferences" className="preferences-section">
      <div className="section-heading">
        <h2>Make it yours</h2>
        <span>
          <Icon name="settings" size={16} /> Set once, adjust anytime
        </span>
      </div>
      <form
        className="preferences-card"
        onSubmit={(event) => void save(event)}
        aria-busy={pending}
      >
        <div className="preferences-main">
          <div className="form-section-title">
            <Icon name="pin" />
            <div>
              <h3>Where parcels should land</h3>
              <p>Your assistant follows these saved directions.</p>
            </div>
          </div>
          <div className="form-grid">
            <label>
              Your name
              <input
                name="resident_name"
                placeholder="e.g. Arjun"
                value={draft.resident_name}
                maxLength={80}
                required
                disabled={blocked}
                onChange={(event) =>
                  change('resident_name', event.target.value)
                }
              />
            </label>
            <label>
              PG name
              <input
                name="pg_name"
                placeholder="e.g. Maple House PG"
                value={draft.pg_name}
                maxLength={120}
                required
                disabled={blocked}
                onChange={(event) => change('pg_name', event.target.value)}
              />
            </label>
            <label className="full-width">
              Guard-room location
              <input
                name="guard_location"
                placeholder="e.g. the guard room beside the main entrance"
                value={draft.guard_location}
                maxLength={180}
                required
                disabled={blocked}
                onChange={(event) =>
                  change('guard_location', event.target.value)
                }
              />
            </label>
            <label className="full-width">
              How to find it <span className="optional">optional</span>
              <textarea
                name="guard_directions"
                placeholder="A landmark or entrance instruction that helps the courier."
                value={draft.guard_directions}
                maxLength={800}
                rows={3}
                disabled={blocked}
                onChange={(event) =>
                  change('guard_directions', event.target.value)
                }
              />
            </label>
          </div>
        </div>
        <div className="preferences-routine">
          <div className="form-section-title">
            <Icon name="clock" />
            <div>
              <h3>Your usual routine</h3>
              <p>Today’s override always takes priority.</p>
            </div>
          </div>
          <fieldset className="days-fieldset">
            <legend>Office days</legend>
            <div className="day-buttons">
              {officeDays.map((day, index) => (
                <button
                  type="button"
                  key={day}
                  disabled={blocked}
                  aria-label={day}
                  aria-pressed={draft.office_days.includes(index)}
                  className={
                    draft.office_days.includes(index) ? 'day selected' : 'day'
                  }
                  onClick={() =>
                    change(
                      'office_days',
                      draft.office_days.includes(index)
                        ? draft.office_days.filter((value) => value !== index)
                        : [...draft.office_days, index].sort(),
                    )
                  }
                >
                  {day}
                </button>
              ))}
            </div>
          </fieldset>
          <div className="form-grid times">
            <label>
              Office starts
              <input
                type="time"
                value={draft.office_start.slice(0, 5)}
                required
                disabled={blocked}
                onChange={(event) =>
                  change(
                    'office_start',
                    event.target.value ? `${event.target.value}:00` : '',
                  )
                }
              />
            </label>
            <label>
              Office ends
              <input
                type="time"
                value={draft.office_end.slice(0, 5)}
                required
                disabled={blocked}
                onChange={(event) =>
                  change(
                    'office_end',
                    event.target.value ? `${event.target.value}:00` : '',
                  )
                }
              />
            </label>
          </div>
          <label>
            Outside office hours
            <select
              value={draft.outside_office}
              disabled={blocked}
              onChange={(event) =>
                change('outside_office', event.target.value as Availability)
              }
            >
              {availabilityChoices.map((choice) => (
                <option key={choice.value} value={choice.value}>
                  {choice.label}
                </option>
              ))}
            </select>
          </label>
          <label>
            On non-office days
            <select
              value={draft.weekend}
              disabled={blocked}
              onChange={(event) =>
                change('weekend', event.target.value as Availability)
              }
            >
              {availabilityChoices.map((choice) => (
                <option key={choice.value} value={choice.value}>
                  {choice.label}
                </option>
              ))}
            </select>
          </label>
          <div className="routine-note">
            <Icon name="info" size={16} />
            <span>
              All times are IST. We’ll ask you first by default outside your
              office routine.
            </span>
          </div>
        </div>
        <div className="form-footer">
          <span>
            {dirty
              ? 'You have unsaved changes.'
              : ready
                ? `Saved routine: ${savedDays || 'no office days'}.`
                : 'Your preferences stay on this device.'}
          </span>
          <button
            className="button primary"
            type="submit"
            disabled={blocked || !dirty}
          >
            {pending ? 'Saving…' : 'Save preferences'}
            <Icon name="check" size={17} />
          </button>
        </div>
      </form>
    </section>
  )
})
