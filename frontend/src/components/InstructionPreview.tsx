import { memo, useEffect, useRef, useState } from 'react'
import { Icon } from './Icon'
import { useNotifications } from './Notifications'

interface Props {
  ready: boolean
  pgName?: string
  instruction?: string
  restrictions?: string[]
}

export const InstructionPreview = memo(function InstructionPreview({
  ready,
  pgName,
  instruction,
  restrictions,
}: Props) {
  const [copiedText, setCopiedText] = useState<string | null>(null)
  const timer = useRef<number | undefined>(undefined)
  const { notify } = useNotifications()
  useEffect(() => () => window.clearTimeout(timer.current), [])
  async function copyInstruction() {
    if (!ready || !instruction) return
    try {
      await navigator.clipboard.writeText(instruction)
      window.clearTimeout(timer.current)
      setCopiedText(instruction)
      timer.current = window.setTimeout(() => setCopiedText(null), 2000)
    } catch {
      notify(
        'Copy was unavailable. You can select the instruction text instead.',
        'error',
      )
    }
  }
  return (
    <section
      className="instruction-card"
      aria-label="Current courier instruction"
    >
      <div className="instruction-title">
        <span className="instruction-icon">
          <Icon name="box" size={25} />
        </span>
        <div>
          <span className="eyebrow">THE COURIER INSTRUCTION</span>
          <p>
            {ready
              ? `${pgName} · prepaid parcels`
              : 'Your handoff instructions will appear here'}
          </p>
        </div>
        <button
          className="copy-button"
          type="button"
          disabled={!ready}
          onClick={() => void copyInstruction()}
        >
          {copiedText !== null && copiedText === instruction ? (
            <>
              <Icon name="check" size={15} />
              Copied
            </>
          ) : (
            'Copy instruction'
          )}
        </button>
      </div>
      <blockquote>{instruction ?? 'Loading your delivery plan…'}</blockquote>
      <div className="restriction-list">
        {restrictions?.map((restriction) => (
          <span key={restriction}>
            <Icon name="shield" size={14} />
            {restriction}
          </span>
        ))}
      </div>
    </section>
  )
})
