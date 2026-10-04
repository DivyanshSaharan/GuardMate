import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { AgentTrace } from './AgentTrace'
import { ModelStatusCard } from './ModelStatusCard'
import type { AgentEvent, ModelIdentity, ModelStatus } from './types'

const checkpoint = 'tinker://saved-model:train:0/sampler_weights/saved-turn'
const identity: ModelIdentity = {
  model: 'Qwen/Qwen3.5-4B',
  provider: 'Tinker',
  target_kind: 'base',
  sampler_checkpoint: null,
  checkpoint_verified: false,
}
function event(overrides: Partial<AgentEvent> = {}): AgentEvent {
  return {
    action: 'clarify',
    detail: 'The application asked the missing question.',
    at: '2026-10-04T05:00:00Z',
    latency_ms: 1200,
    model_action: 'clarify',
    model_question: 'prepaid',
    ...overrides,
  }
}

describe('saved per-turn model provenance', () => {
  it('renders a returned base plan independently of its checked application action', () => {
    render(
      <AgentTrace
        events={[
          event({
            action: 'request_takeover',
            model_action: 'handoff',
            model_identity: identity,
            model_result: 'plan_returned',
          }),
        ]}
      />,
    )
    expect(screen.getByText('Plan returned')).toBeTruthy()
    expect(screen.getByText(/this turn: Base model · Qwen/)).toBeTruthy()
    expect(screen.getByText('request_takeover')).toBeTruthy()
    expect(screen.getByText(/Model proposed: handoff/)).toBeTruthy()
    expect(screen.queryByText('Plan unavailable')).toBeNull()
  })

  it.each([false, true])(
    'renders the saved sampler verification state (%s) with its own URI',
    (verified) => {
      render(
        <AgentTrace
          events={[
            event({
              model_identity: {
                ...identity,
                target_kind: 'tuned',
                sampler_checkpoint: checkpoint,
                checkpoint_verified: verified,
              },
              model_result: 'plan_returned',
            }),
          ]}
        />,
      )
      expect(screen.getByText(/this turn: Sampler checkpoint/)).toBeTruthy()
      expect(screen.getByText(checkpoint).closest('details')?.open).toBe(false)
      expect(
        screen.getByText(
          verified
            ? /Base-model identity verified at this turn/
            : /Checkpoint not verified at this turn/,
        ),
      ).toBeTruthy()
      if (verified)
        expect(
          screen.getByText(/not evidence of improved quality or safety/),
        ).toBeTruthy()
    },
  )

  it('shows unavailable rather than returned even when the configured sampler is recorded', () => {
    render(
      <AgentTrace
        events={[
          event({
            action: 'model_unavailable',
            model_action: null,
            model_question: null,
            model_identity: {
              ...identity,
              target_kind: 'tuned',
              sampler_checkpoint: checkpoint,
            },
            model_result: 'unavailable',
          }),
        ]}
      />,
    )
    expect(screen.getByText('Plan unavailable')).toBeTruthy()
    expect(screen.getByText(/this turn: Sampler checkpoint/)).toBeTruthy()
    expect(screen.queryByText('Plan returned')).toBeNull()
    expect(screen.queryByText(/Model proposed/)).toBeNull()
  })

  it('does not assume target kind when an identity record omitted newer fields', () => {
    render(
      <AgentTrace
        events={[
          event({
            model_identity: {
              model: identity.model,
              provider: identity.provider,
            },
            model_result: 'plan_returned',
          }),
        ]}
      />,
    )
    expect(screen.getByText(/this turn: Target not specified/)).toBeTruthy()
    expect(screen.queryByText(/this turn: Base model/)).toBeNull()
  })

  it.each([
    { model_action: 'wait', latency_ms: null },
    { model_action: null, latency_ms: 0 },
  ])(
    'marks legacy attempted events without relabelling them as current models',
    (legacy) => {
      render(<AgentTrace events={[event(legacy)]} />)
      expect(screen.getByText('Model identity not recorded.')).toBeTruthy()
      expect(screen.getByText('Model result not recorded')).toBeTruthy()
      expect(screen.queryByText(/Model target for this turn/)).toBeNull()
    },
  )

  it('does not fabricate model evidence for resident or ordinary events', () => {
    render(
      <AgentTrace
        events={[
          event({
            action: 'resident_end',
            model_action: null,
            model_question: null,
            latency_ms: null,
            model_identity: null,
            model_result: null,
          }),
        ]}
      />,
    )
    expect(screen.getByText('resident_end')).toBeTruthy()
    expect(screen.queryByText('Model identity not recorded.')).toBeNull()
    expect(screen.queryByText('Model result not recorded')).toBeNull()
    expect(screen.queryByText(/Model target for this turn/)).toBeNull()
  })

  it('preserves each saved identity when the current backend target changes', () => {
    const savedEvents = [
      event({ model_identity: identity, model_result: 'plan_returned' }),
      event({
        model_identity: {
          ...identity,
          target_kind: 'tuned',
          sampler_checkpoint: checkpoint,
          checkpoint_verified: true,
        },
        model_result: 'plan_returned',
      }),
    ]
    const current: ModelStatus = {
      ...identity,
      configured: true,
      message: 'Current backend configuration.',
      reserved_usd: 0,
      budget_usd: 0.25,
      voice_connected: false,
    }
    const { rerender } = render(
      <>
        <ModelStatusCard status={current} />
        <AgentTrace events={savedEvents} />
      </>,
    )
    const traceBefore = screen
      .getByText('See the agent’s actions (2)')
      .closest('details')!
    const savedText = traceBefore.textContent
    rerender(
      <>
        <ModelStatusCard
          status={{
            ...current,
            target_kind: 'tuned',
            sampler_checkpoint:
              'tinker://new-model:train:1/sampler_weights/new-selection',
            checkpoint_verified: false,
          }}
        />
        <AgentTrace events={savedEvents} />
      </>,
    )
    const traceAfter = screen
      .getByText('See the agent’s actions (2)')
      .closest('details')!
    expect(traceAfter.textContent).toBe(savedText)
    expect(within(traceAfter).getByText(checkpoint)).toBeTruthy()
    expect(within(traceAfter).queryByText(/new-selection/)).toBeNull()
    expect(within(traceAfter).getByText(/this turn: Base model/)).toBeTruthy()
    expect(
      within(traceAfter).getByText(/Base-model identity verified at this turn/),
    ).toBeTruthy()
  })
})
