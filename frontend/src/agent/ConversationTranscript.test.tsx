import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { ConversationTranscript } from './ConversationTranscript'
import type { Conversation } from './types'

describe('conversation render isolation', () => {
  it('typing in the composer does not rerender the transcript', () => {
    const readContent = vi.fn(() => 'Hello, how can I help?')
    const conversation: Conversation = {
      id: 'render-test',
      status: 'active',
      revision: 0,
      turn_count: 0,
      facts: {
        prepaid: null,
        guard_available: null,
        needs_otp: false,
        needs_signature: false,
        expensive: false,
      },
      messages: [
        {
          role: 'assistant',
          get content() {
            return readContent()
          },
          at: '2026-10-03T06:00:00Z',
        },
      ],
      events: [],
      approval: null,
      authorized_location: null,
      courier_reported_outcome: null,
      created_at: '2026-10-03T06:00:00Z',
    }
    render(
      <ConversationTranscript
        conversation={conversation}
        pending={false}
        onSend={vi.fn(async () => true)}
      />,
    )
    readContent.mockClear()
    fireEvent.change(screen.getByLabelText('Courier message'), {
      target: { value: 'Are you available?' },
    })
    expect(readContent).not.toHaveBeenCalled()
    expect(
      (screen.getByLabelText('Courier message') as HTMLInputElement).value,
    ).toBe('Are you available?')
  })
})
