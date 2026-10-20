import { memo } from 'react'
import type { ModelStatus } from './types'

export const ModelStatusCard = memo(function ModelStatusCard({
  status,
}: {
  status: ModelStatus | null
}) {
  return (
    <section className="model-status-card" aria-label="AI connection">
      <div className="model-status-heading">
        <span className="model-target-label">
          {status === null
            ? 'Checking AI connection…'
            : status.configured
              ? 'AI connected'
              : 'AI unavailable'}
        </span>
        {status?.model && <p>{status.model}</p>}
      </div>
      <p className="muted" role="status">
        {status?.message || 'Checking your AI connection.'}
      </p>
      {status?.provider && (
        <details className="model-target-explanation">
          <summary>Connection details</summary>
          <p className="muted">Provider: {status.provider}</p>
        </details>
      )}
      <details className="model-target-explanation">
        <summary>Data use</summary>
        <p className="muted">
          Saved instructions and role-play messages are sent to Tinker. Raw
          speech is processed locally.
        </p>
      </details>
    </section>
  )
})
