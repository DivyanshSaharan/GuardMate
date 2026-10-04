import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { ModelStatusCard } from './ModelStatusCard'
import type { ModelStatus } from './types'

const status: ModelStatus = {
  configured: true,
  model: 'Qwen/Qwen3.5-4B',
  provider: 'Tinker',
  message: 'Key configured.',
  reserved_usd: 0.012345,
  budget_usd: 0.25,
  voice_connected: false,
}

describe('demo model display', () => {
  it.each([
    null,
    status,
    { ...status, target_kind: 'base' as const },
    {
      ...status,
      target_kind: 'tuned' as const,
      checkpoint_verified: true,
      sampler_checkpoint: 'tinker://private-run/sampler_weights/private',
    },
    { ...status, configured: false, message: 'Not configured.' },
  ])(
    'keeps the requested presentation label separate from runtime evidence',
    (value) => {
      render(<ModelStatusCard status={value} />)
      expect(screen.getByLabelText('Demo model display')).toBeTruthy()
      expect(screen.getByText('Tinker Fine-Tuned Model')).toBeTruthy()
      expect(screen.getByText('Qwen/Qwen3.5-4B (GuardMate-v1)')).toBeTruthy()
      expect(
        screen.getByText(/actual target is recorded in action traces/),
      ).toBeTruthy()
      expect(screen.queryByText(/Successfully loaded/)).toBeNull()
      expect(screen.queryByText(/Base-model identity verified/)).toBeNull()
      expect(screen.queryByRole('button')).toBeNull()
      expect(screen.queryByText(/private-run/)).toBeNull()
    },
  )

  it('does not change backend configuration when the presentation is rendered', () => {
    const snapshot = structuredClone(status)
    const { rerender } = render(<ModelStatusCard status={status} />)
    rerender(<ModelStatusCard status={{ ...status, target_kind: 'tuned' }} />)
    expect(status).toEqual(snapshot)
    expect(screen.getByText(/Demo label/)).toBeTruthy()
  })
})
