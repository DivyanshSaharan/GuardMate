import { memo } from 'react'
import { availabilityChoices } from '../lib/presentation'
import type { Availability } from '../types'
import { Icon } from './Icon'

interface Props {
  availability?: Availability
  explanation?: string
  override?: Availability | null
  disabled: boolean
  pending: boolean
  onChange: (status: Availability | null) => Promise<unknown>
}

export const AvailabilityCard = memo(function AvailabilityCard({
  availability,
  explanation,
  override,
  disabled,
  pending,
  onChange,
}: Props) {
  const choice = availabilityChoices.find(
    (option) => option.value === availability,
  )
  return (
    <article className="card availability-card" aria-busy={pending}>
      <div className="card-heading">
        <span className="card-icon">
          <Icon name={choice?.icon ?? 'office'} />
        </span>
        <span className="eyebrow">YOUR AVAILABILITY</span>
      </div>
      <h3>{choice?.label ?? 'Loading'}</h3>
      <p className="muted context-explanation">
        {explanation ?? 'Connecting to your saved preferences…'}
      </p>
      <div
        className="choice-group"
        aria-label="Override availability for today"
      >
        {availabilityChoices.map((option) => (
          <button
            key={option.value}
            type="button"
            disabled={disabled || pending}
            aria-pressed={availability === option.value}
            className={
              availability === option.value ? 'choice selected' : 'choice'
            }
            onClick={() => {
              if (override !== option.value) void onChange(option.value)
            }}
          >
            <Icon name={option.icon} size={16} />
            {option.label}
          </button>
        ))}
      </div>
      <div className="availability-footer">
        {override ? (
          <button
            type="button"
            className="text-button"
            disabled={disabled || pending}
            onClick={() => void onChange(null)}
          >
            Use my saved routine <Icon name="arrow" size={14} />
          </button>
        ) : (
          <span>
            <Icon name="clock" size={14} /> Following your saved routine
          </span>
        )}
        <span className="save-indicator" role="status">
          {pending ? 'Saving…' : ''}
        </span>
      </div>
    </article>
  )
})
