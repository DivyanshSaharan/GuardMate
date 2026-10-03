import { memo, useState } from 'react'
import { AgentTrace } from './AgentTrace'
import { ConversationTranscript } from './ConversationTranscript'
import { ResidentControls } from './ResidentControls'
import { useConversation } from './useConversation'
import './conversation.css'

function RolePlay() {
  const {
    model,
    conversation,
    pending,
    error,
    start,
    send,
    decide,
    refresh,
    checkModel,
  } = useConversation()
  const active = conversation && conversation.status !== 'ended'
  return (
    <div className="conversation-lab-body">
      <p className="muted">
        {model?.message ?? 'Checking the model configuration…'}
      </p>
      <p className="model-budget">
        {model?.model ?? 'Qwen3.5-4B'} · hosted inference · estimated usage
        reserved: ${(model?.reserved_usd ?? 0).toFixed(4)} / $
        {(model?.budget_usd ?? 0.25).toFixed(2)}
      </p>
      <p className="muted">
        Not offline yet. Saved instructions and role-play messages are sent to
        Tinker. Never enter real OTPs or private customer data.
      </p>
      <p className="muted">
        Qwen plans the next action; GuardMate renders the checked reply. This is
        the base model, not a fine-tuned one yet.
      </p>
      <div className="lab-actions" role="group" aria-label="Role-play controls">
        <button
          className="button secondary"
          disabled={pending || !model?.configured || !!active}
          onClick={() => void start()}
        >
          Start role-play
        </button>
        <button
          className="button role-play-end"
          disabled={pending || !active}
          title="End this test conversation. Saved history is kept."
          onClick={() => void decide('end')}
        >
          <span className="stop-mark" aria-hidden="true" />
          End role-play
        </button>
        <button
          className="text-button"
          disabled={pending}
          onClick={() => void (conversation ? refresh() : checkModel())}
        >
          Refresh status
        </button>
        {conversation && (
          <span className="muted">
            {conversation.status.replaceAll('_', ' ')} ·{' '}
            {conversation.turn_count}/20 turns
          </span>
        )}
      </div>
      <div className="lab-error" role={error ? 'alert' : undefined}>
        {error}
      </div>
      {conversation && (
        <>
          <div className="conversation-grid">
            <ConversationTranscript
              conversation={conversation}
              pending={pending}
              onSend={send}
            />
            <ResidentControls
              conversation={conversation}
              pending={pending}
              onDecision={decide}
            />
          </div>
          {conversation.courier_reported_outcome && (
            <p className="reported-outcome">
              Courier reported:{' '}
              {conversation.courier_reported_outcome.replaceAll('_', ' ')}.
              Receipt is not independently verified.
            </p>
          )}
          <AgentTrace events={conversation.events} />
        </>
      )}
    </div>
  )
}

export const ConversationLab = memo(function ConversationLab() {
  const [open, setOpen] = useState(false)
  return (
    <section
      className="conversation-lab"
      id="conversation"
      aria-label="Conversation testing"
    >
      <div className="section-heading">
        <h2>Try a delivery conversation</h2>
        <button
          className="text-button"
          onClick={() => setOpen((value) => !value)}
          aria-expanded={open}
        >
          {open ? 'Close panel' : 'Open text role-play'}
        </button>
      </div>
      {open ? (
        <RolePlay />
      ) : (
        <p className="muted">
          Test the real Qwen agent with a fictional courier. No phone call is
          answered.
        </p>
      )}
    </section>
  )
})
