import { memo } from 'react'
import type { Conversation, ResidentDecision } from './types'

export const ResidentControls = memo(function ResidentControls({
  conversation,
  pending,
  onDecision,
}: {
  conversation: Conversation
  pending: boolean
  onDecision: (decision: ResidentDecision) => Promise<boolean>
}) {
  const approval = conversation.approval
  const ended = conversation.status === 'ended'
  return (
    <section className="resident-controls" aria-label="Resident controls">
      <h3>Your decision</h3>
      {approval ? (
        <>
          <span className="eyebrow">{approval.status.replace('_', ' ')}</span>
          <p className="approval-location">{approval.location}</p>
          <p className="muted">{approval.reason}</p>
          <p className="muted">
            Approval expires at{' '}
            {new Date(approval.expires_at).toLocaleTimeString('en-IN', {
              timeZone: 'Asia/Kolkata',
              hour: '2-digit',
              minute: '2-digit',
              second: '2-digit',
            })}{' '}
            IST. Silence never approves.
          </p>
          {approval.status === 'pending' && (
            <div className="approval-actions">
              <button
                className="button primary"
                disabled={pending}
                onClick={() => void onDecision('approve')}
              >
                Approve once
              </button>
              <button
                className="button secondary"
                disabled={pending}
                onClick={() => void onDecision('decline')}
              >
                Decline
              </button>
            </div>
          )}
        </>
      ) : (
        <p className="muted">
          Alternative handoffs appear here for your approval. Courier messages
          cannot grant it.
        </p>
      )}
      <div className="resident-actions">
        <button
          className="text-button"
          disabled={pending || ended}
          onClick={() => void onDecision('takeover')}
        >
          Handle personally
        </button>
      </div>
      <p className="muted">
        This panel pauses the text agent. It does not connect or transfer a
        cellular call.
      </p>
    </section>
  )
})
