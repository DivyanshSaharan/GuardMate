import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { ModelStatusCard } from './ModelStatusCard'
import type { ModelStatus } from './types'

const checkpoint = 'tinker://model-id:train:0/sampler_weights/final'
const status: ModelStatus = {
  configured: true,
  model: 'Qwen/Qwen3.5-4B',
  provider: 'Tinker',
  message: 'Key configured.',
  reserved_usd: 0.012345,
  budget_usd: 0.25,
  voice_connected: false,
}

describe('current model target', () => {
  it('does not assume a base or tuned model while status is loading', () => {
    render(<ModelStatusCard status={null} />)
    expect(screen.getByText('Checking model target…')).toBeTruthy()
    expect(screen.queryByText('Base model')).toBeNull()
    expect(screen.queryByText('Sampler checkpoint')).toBeNull()
    expect(screen.getByText(/no model request is sent/)).toBeTruthy()
  })

  it.each([undefined, 'unspecified'] as const)(
    'does not fabricate a selection for an older or %s backend',
    (kind) => {
      render(<ModelStatusCard status={{ ...status, target_kind: kind }} />)
      expect(screen.getByText('Target not specified')).toBeTruthy()
      expect(
        screen.getByText(/No base model or checkpoint selection is assumed/),
      ).toBeTruthy()
      expect(screen.queryByText('Base model')).toBeNull()
      expect(screen.queryByText('Sampler checkpoint')).toBeNull()
    },
  )

  it('shows an explicit untuned base selection and keeps budget/disclosures', () => {
    render(
      <ModelStatusCard
        status={{
          ...status,
          target_kind: 'base',
          sampler_checkpoint: null,
          checkpoint_verified: false,
        }}
      />,
    )
    expect(screen.getByText('Base model')).toBeTruthy()
    expect(screen.getByText(/Untuned base model/)).toBeTruthy()
    expect(screen.getByText(/\$0.0123 \/ \$0.25/)).toBeTruthy()
    expect(screen.getByText(/messages are sent to Tinker/)).toBeTruthy()
    expect(screen.queryByText('Checkpoint details')).toBeNull()
    expect(screen.queryByRole('button')).toBeNull()
  })

  it('shows an unverified sampler, expandable URI and no fallback without offering a selector', () => {
    render(
      <ModelStatusCard
        status={{
          ...status,
          message: `Sampler checkpoint configured: ${checkpoint} (not yet verified).`,
          target_kind: 'tuned',
          sampler_checkpoint: checkpoint,
          checkpoint_verified: false,
        }}
      />,
    )
    expect(screen.getByText('Sampler checkpoint')).toBeTruthy()
    expect(screen.getByText(/Checkpoint not verified yet/)).toBeTruthy()
    expect(
      screen.getByText(/There is no fallback to the base model/),
    ).toBeTruthy()
    expect(
      screen.getByText(/Target selection is a backend setting/),
    ).toBeTruthy()
    const uri = screen.getByText(checkpoint)
    const details = uri.closest('details') as HTMLDetailsElement
    expect(details.open).toBe(false)
    expect(screen.getAllByText(checkpoint)).toHaveLength(1)
    expect(screen.getByText(/configured: the selected checkpoint/)).toBeTruthy()
    fireEvent.click(screen.getByText('Checkpoint details'))
    expect(details.open).toBe(true)
    expect(screen.queryByRole('button')).toBeNull()
    expect(screen.queryByText(/Untuned base model/)).toBeNull()
  })

  it('calls verification model identity only, never quality or safety approval', () => {
    const { rerender } = render(
      <ModelStatusCard
        status={{
          ...status,
          target_kind: 'tuned',
          sampler_checkpoint: checkpoint,
          checkpoint_verified: false,
        }}
      />,
    )
    rerender(
      <ModelStatusCard
        status={{
          ...status,
          target_kind: 'tuned',
          sampler_checkpoint: checkpoint,
          checkpoint_verified: true,
        }}
      />,
    )
    expect(screen.getByText(/Base-model identity verified/)).toBeTruthy()
    expect(
      screen.getByText(/does not establish improved quality or safety/),
    ).toBeTruthy()
    expect(screen.queryByText(/Checkpoint not verified yet/)).toBeNull()
  })

  it('leaves an unconfigured backend message visible without inventing successful verification', () => {
    render(
      <ModelStatusCard
        status={{
          ...status,
          configured: false,
          message: 'Install requirements-ai.txt and set the local key.',
          target_kind: 'tuned',
          sampler_checkpoint: checkpoint,
          checkpoint_verified: false,
        }}
      />,
    )
    expect(screen.getByText(/Install requirements-ai/)).toBeTruthy()
    expect(screen.getByText(/Checkpoint not verified yet/)).toBeTruthy()
    expect(screen.queryByText(/Base-model identity verified/)).toBeNull()
  })
})
