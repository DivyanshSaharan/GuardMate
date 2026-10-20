import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { ModelStatusCard } from './ModelStatusCard'
import type { ModelStatus } from './types'

const status: ModelStatus = {
  configured: true,
  model: 'Qwen/Qwen3.5-4B',
  provider: 'GuardMate gateway · Tinker',
  message: 'Account connected.',
  reserved_usd: 0.012345,
  budget_usd: 1,
  voice_connected: false,
}

describe('AI connection status', () => {
  it('shows a checking state until the connection has been loaded', () => {
    render(<ModelStatusCard status={null} />)
    expect(screen.getByText('Checking AI connection…')).toBeTruthy()
    expect(screen.getByRole('status').textContent).toBe(
      'Checking your AI connection.',
    )
    expect(screen.queryByText('AI connected')).toBeNull()
    expect(screen.queryByText(status.model)).toBeNull()
  })

  it('uses the actual model, provider and connection message', () => {
    render(
      <ModelStatusCard status={{ ...status, model: 'Configured model' }} />,
    )
    expect(screen.getByLabelText('AI connection')).toBeTruthy()
    expect(screen.getByText('AI connected')).toBeTruthy()
    expect(screen.getByText('Configured model')).toBeTruthy()
    expect(screen.getByRole('status').textContent).toBe('Account connected.')
    const provider = screen.getByText(`Provider: ${status.provider}`)
    expect(provider.closest('details')?.open).toBe(false)
    expect(screen.queryByText(/Fine-Tuned|GuardMate-v1|Demo label/)).toBeNull()
  })

  it('shows the server reason when the model is unavailable', () => {
    render(
      <ModelStatusCard
        status={{
          ...status,
          configured: false,
          message: 'Run your downloaded installer to connect your account.',
        }}
      />,
    )
    expect(screen.getByText('AI unavailable')).toBeTruthy()
    expect(screen.getByRole('status').textContent).toBe(
      'Run your downloaded installer to connect your account.',
    )
    expect(screen.queryByText('AI connected')).toBeNull()
  })

  it('updates status without claiming a configured checkpoint is fine-tuned', () => {
    const { rerender } = render(<ModelStatusCard status={status} />)
    rerender(
      <ModelStatusCard
        status={{
          ...status,
          target_kind: 'tuned',
          sampler_checkpoint: 'tinker://private-run/sampler_weights/private',
          checkpoint_verified: false,
          configured: false,
          message: 'Checkpoint unavailable.',
        }}
      />,
    )
    expect(screen.getByText('AI unavailable')).toBeTruthy()
    expect(screen.getByRole('status').textContent).toBe(
      'Checkpoint unavailable.',
    )
    expect(screen.queryByText(/Fine-Tuned|private-run/)).toBeNull()
  })
})
