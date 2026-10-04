import { memo } from 'react'
import type { AgentEvent } from './types'

export const TurnModelProvenance = memo(function TurnModelProvenance({
  event,
}: {
  event: AgentEvent
}) {
  const identity = event.model_identity
  const attempted =
    !!identity ||
    !!event.model_result ||
    !!event.model_action ||
    event.latency_ms != null
  if (!attempted) return null
  const kind = identity?.target_kind ?? 'unspecified'
  const label =
    kind === 'base'
      ? 'Base model'
      : kind === 'tuned'
        ? 'Sampler checkpoint'
        : 'Target not specified'
  return (
    <div className="turn-model-provenance">
      <p className="model-result">
        {event.model_result === 'plan_returned'
          ? 'Plan returned'
          : event.model_result === 'unavailable'
            ? 'Plan unavailable'
            : 'Model result not recorded'}
      </p>
      {identity ? (
        <>
          <p>
            Model target for this turn: {label} · {identity.model} ·{' '}
            {identity.provider}
          </p>
          {kind === 'tuned' && (
            <p>
              {identity.checkpoint_verified
                ? 'Base-model identity verified at this turn; not evidence of improved quality or safety.'
                : 'Checkpoint not verified at this turn.'}
            </p>
          )}
          {kind === 'tuned' && identity.sampler_checkpoint && (
            <details className="model-checkpoint-details">
              <summary>Saved checkpoint</summary>
              <code>{identity.sampler_checkpoint}</code>
            </details>
          )}
        </>
      ) : (
        <p>Model identity not recorded.</p>
      )}
    </div>
  )
})
