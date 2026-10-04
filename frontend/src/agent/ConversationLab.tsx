import { memo, useRef, useState } from 'react'
import { AgentTrace } from './AgentTrace'
import { ConversationTranscript } from './ConversationTranscript'
import { ModelStatusCard } from './ModelStatusCard'
import { ResidentControls } from './ResidentControls'
import { SessionControls } from './SessionControls'
import { sessionName } from './sessionState'
import { useConversation } from './useConversation'
import './conversation.css'

function RolePlay() {
  const {
    model,
    conversation,
    sessions,
    initializing,
    pending,
    error,
    start,
    selectSession,
    send,
    decide,
    refresh,
  } = useConversation()
  const drafts = useRef(new Map<string, string>())
  return (
    <div className="conversation-lab-body">
      <ModelStatusCard status={model} />
      <SessionControls
        sessions={sessions}
        conversation={conversation}
        pending={pending}
        initializing={initializing}
        configured={model?.configured ?? false}
        onStart={start}
        onSelect={selectSession}
        onEnd={() => decide('end')}
        onRefresh={refresh}
      />
      <div className="lab-error" role={error ? 'alert' : undefined}>
        {error}
      </div>
      {conversation && (
        <>
          <div className="selected-session" aria-label="Selected courier">
            <h3>{sessionName(conversation)}</h3>
            <span className="muted">
              Role-play {conversation.id.slice(0, 8)}
            </span>
          </div>
          <div className="conversation-grid">
            <ConversationTranscript
              key={conversation.id}
              conversation={conversation}
              pending={pending}
              onSend={send}
              draftStore={drafts.current}
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
