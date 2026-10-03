import { memo } from 'react'
import type { Conversation } from './types'

export const AgentTrace = memo(function AgentTrace({
  events,
}: {
  events: Conversation['events']
}) {
  return (
    <details className="agent-trace">
      <summary>See the agent’s actions ({events.length})</summary>
      <ol>
        {events.map((event, index) => (
          <li key={index}>
            <code>{event.action}</code>
            {event.model_action && (
              <p>
                Qwen proposed: {event.model_action}
                {event.model_question ? ` / ${event.model_question}` : ''}. The
                application executed the checked action above.
              </p>
            )}
            <p>{event.detail}</p>
            {event.latency_ms !== null && (
              <span>
                {(event.latency_ms / 1000).toFixed(2)}s · model and policy time
              </span>
            )}
          </li>
        ))}
      </ol>
    </details>
  )
})
