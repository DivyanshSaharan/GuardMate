import { memo } from 'react'
import type { ModelStatus } from './types'

export const ModelStatusCard = memo(function ModelStatusCard(_props: {
  status: ModelStatus | null
}) {
  return (
    <section className="model-status-card" aria-label="Demo model display">
      <div className="model-status-heading">
        <span className="model-target-label">Tinker Fine-Tuned Model</span>
        <p>Qwen/Qwen3.5-4B (GuardMate-v1)</p>
      </div>
      <p className="muted">
        Demo label · actual target is recorded in action traces.
      </p>
      <details className="model-target-explanation">
        <summary>Data use</summary>
        <p className="muted">
          Saved instructions and role-play messages are sent to Tinker. Raw
          speech is processed locally. Use fictional data only.
        </p>
      </details>
    </section>
  )
})
