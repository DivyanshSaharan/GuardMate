import { memo, useState } from 'react'
import { defaultEndTime, formatWindow } from '../lib/presentation'
import { Icon } from './Icon'

interface Props {
  ready: boolean
  active: boolean
  expired: boolean
  expiresAt?: string | null
  disabled: boolean
  pending: boolean
  onChange: (enabled: boolean, expiresAt?: string) => Promise<unknown>
}

export const DeliveryModeCard = memo(function DeliveryModeCard({
  ready,
  active,
  expired,
  expiresAt,
  disabled,
  pending,
  onChange,
}: Props) {
  const [endTime, setEndTime] = useState(defaultEndTime)
  return (
    <article className="card mode-card" aria-busy={pending}>
      <div className="card-heading">
        <span className="card-icon">
          <Icon name="shield" />
        </span>
        <span className="eyebrow">DELIVERY MODE</span>
        <span className={`status-pill ${active ? 'on' : ''}`}>
          <span />
          {active ? 'Enabled' : expired ? 'Window ended' : 'Off'}
        </span>
      </div>
      <h3>
        {active
          ? 'Instructions enabled'
          : ready
            ? 'Ready when you are'
            : 'Let’s get you set up'}
      </h3>
      <p className="muted mode-explanation">
        {active
          ? 'Your saved instructions are enabled for this delivery window.'
          : ready
            ? 'Choose a delivery window for your saved instructions.'
            : 'Save your PG details below to prepare your delivery instructions.'}
      </p>
      <div className="mode-controls">
        <label htmlFor="mode-end">
          End this window at <span>IST</span>
        </label>
        <input
          id="mode-end"
          type="datetime-local"
          value={endTime}
          disabled={disabled || pending || active}
          onChange={(event) => setEndTime(event.target.value)}
          required
        />
        <button
          className={`button mode-action ${active ? 'secondary' : 'primary'}`}
          type="button"
          disabled={disabled || pending || !ready || (!active && !endTime)}
          onClick={() =>
            void onChange(!active, active ? undefined : `${endTime}:00+05:30`)
          }
        >
          {pending
            ? 'Saving…'
            : active
              ? 'Turn off delivery mode'
              : 'Enable delivery mode'}
          <Icon name={active ? 'shield' : 'arrow'} size={17} />
        </button>
      </div>
      <span className="window-end">
        {active && expiresAt ? `Ends ${formatWindow(expiresAt)} IST` : ''}
      </span>
    </article>
  )
})
