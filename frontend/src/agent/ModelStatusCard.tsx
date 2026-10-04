import { memo } from 'react'
import type { ModelStatus } from './types'

export const ModelStatusCard = memo(function ModelStatusCard({
  status,
}: {
  status: ModelStatus | null
}) {
  const kind = status?.target_kind ?? 'unspecified'
  const statusMessage = status?.sampler_checkpoint
    ? status.message.replaceAll(
        status.sampler_checkpoint,
        'the selected checkpoint',
      )
    : status?.message
  const label =
    kind === 'base'
      ? 'Base model'
      : kind === 'tuned'
        ? 'Sampler checkpoint'
        : status
          ? 'Target not specified'
          : 'Checking model target…'
  return (
    <section className="model-status-card" aria-label="Current model target">
      <div className="model-status-heading">
        <span className="model-target-label">{label}</span>
        <p>{status?.model ?? 'Waiting for backend status…'}</p>
      </div>
      <p className="model-target-note muted">
        {kind === 'base'
          ? 'Untuned base model. No sampler checkpoint is selected.'
          : kind === 'tuned'
            ? status?.checkpoint_verified
              ? 'Base-model identity verified. Identity only; not quality or safety approval.'
              : 'Checkpoint not verified yet.'
            : status
              ? 'Target identity was not reported. No base model or checkpoint selection is assumed.'
              : 'Reading the backend configuration; no model request is sent by this check.'}
      </p>
      {kind === 'tuned' && status?.sampler_checkpoint && (
        <details className="model-checkpoint-details">
          <summary>Checkpoint details</summary>
          <code>{status.sampler_checkpoint}</code>
        </details>
      )}
      <p className="muted model-status-message">
        {statusMessage ?? 'Checking the model configuration…'}
      </p>
      <p className="model-budget">
        Hosted inference · estimated usage reserved: $
        {(status?.reserved_usd ?? 0).toFixed(4)} / $
        {(status?.budget_usd ?? 0.25).toFixed(2)}
      </p>
      <p className="muted">
        Not offline yet. Saved instructions and role-play messages are sent to
        Tinker. Never enter real OTPs or private customer data.
      </p>
      <details className="model-target-explanation">
        <summary>How this target is used</summary>
        <p className="muted">
          The model proposes the next action; GuardMate renders the checked
          reply. Target selection is a backend setting; restart the backend
          after changing it.
        </p>
        {kind === 'tuned' && (
          <>
            <p className="muted">
              If this checkpoint fails, the agent pauses. There is no fallback
              to the base model.
            </p>
            <p className="muted">
              Verification checks the checkpoint’s base-model identity. It does
              not establish improved quality or safety.
            </p>
          </>
        )}
      </details>
    </section>
  )
})
