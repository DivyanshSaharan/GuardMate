import { memo } from 'react'

export const OutgoingMessage = memo(function OutgoingMessage({
  text,
  failed,
}: {
  text: string
  failed: boolean
}) {
  return (
    <li className="message-courier message-outgoing">
      <span>Courier · {failed ? 'Response not confirmed' : 'Sending'}</span>
      <p>{text}</p>
      {failed && (
        <small>
          Refresh status before retrying. Your message may have reached the
          agent.
        </small>
      )}
    </li>
  )
})

export const ThinkingIndicator = memo(function ThinkingIndicator() {
  return (
    <li
      className="message-assistant message-thinking"
      role="status"
      aria-label="GuardMate is preparing a reply"
    >
      <span>GuardMate</span>
      <div className="thinking-line">
        <span className="thinking-dots" aria-hidden="true">
          <i />
          <i />
          <i />
        </span>
        <small>Preparing a reply…</small>
      </div>
    </li>
  )
})
