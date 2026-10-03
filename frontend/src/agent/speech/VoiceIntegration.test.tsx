import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ConversationTranscript } from '../ConversationTranscript'
import type { Conversation } from '../types'

const conversation: Conversation = {
  id: 'a',
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
      content: 'How can I help?',
      at: '2026-10-03T06:00:00Z',
    },
  ],
  events: [],
  approval: null,
  authorized_location: null,
  courier_reported_outcome: null,
  created_at: '2026-10-03T06:00:00Z',
}
let getUserMedia: ReturnType<typeof vi.fn>
let mediaDescriptor: PropertyDescriptor | undefined
let processor: {
  onaudioprocess: ((event: AudioProcessingEvent) => void) | null
}
const trackStop = vi.fn()
const audioPause = vi.fn()
const node = () => ({ connect: vi.fn(), disconnect: vi.fn() })
class FakeContext {
  sampleRate = 16_000
  destination = {}
  resume = vi.fn(async () => {})
  close = vi.fn(async () => {})
  createMediaStreamSource() {
    return node()
  }
  createScriptProcessor() {
    processor = { ...node(), onaudioprocess: null }
    return processor
  }
  createGain() {
    return { ...node(), gain: { value: 1 } }
  }
}

beforeEach(() => {
  trackStop.mockClear()
  audioPause.mockClear()
  getUserMedia = vi.fn(async () => ({ getTracks: () => [{ stop: trackStop }] }))
  mediaDescriptor = Object.getOwnPropertyDescriptor(navigator, 'mediaDevices')
  Object.defineProperty(navigator, 'mediaDevices', {
    configurable: true,
    value: { getUserMedia },
  })
  vi.stubGlobal('AudioContext', FakeContext)
  vi.stubGlobal('URL', {
    createObjectURL: vi.fn(() => 'blob:reply'),
    revokeObjectURL: vi.fn(),
  })
  vi.stubGlobal(
    'Audio',
    class {
      onended = null
      onerror = null
      src = ''
      pause = audioPause
      play = vi.fn(async () => {})
    },
  )
  vi.stubGlobal(
    'fetch',
    vi.fn(async (path: string) => {
      if (path.endsWith('/status'))
        return {
          ok: true,
          json: async () => ({
            stt_ready: true,
            tts_ready: true,
            max_seconds: 30,
            message: 'Local speech ready.',
          }),
        }
      if (path.endsWith('/transcribe'))
        return {
          ok: true,
          json: async () => ({
            text: 'Prepaid parcel',
            duration_ms: 100,
            processing_ms: 10,
          }),
        }
      return {
        ok: true,
        blob: async () => new Blob(['audio'], { type: 'audio/wav' }),
      }
    }),
  )
})
afterEach(() => {
  if (mediaDescriptor)
    Object.defineProperty(navigator, 'mediaDevices', mediaDescriptor)
  else Reflect.deleteProperty(navigator, 'mediaDevices')
  vi.unstubAllGlobals()
})

describe('recording and reply audio coordination', () => {
  it.each(['ended', 'needs_resident'] as const)(
    'allows explicit final assistant reply audio when %s, but keeps microphone disabled',
    async (status) => {
      render(
        <ConversationTranscript
          conversation={{ ...conversation, status }}
          pending={false}
          onSend={vi.fn()}
        />,
      )
      await screen.findByText('Local speech ready.')
      expect(
        screen
          .getByRole('button', { name: 'Record courier message' })
          .hasAttribute('disabled'),
      ).toBe(true)
      expect(
        screen
          .getByRole('button', { name: 'Listen to latest reply' })
          .hasAttribute('disabled'),
      ).toBe(false)
      fireEvent.click(
        screen.getByRole('button', { name: 'Listen to latest reply' }),
      )
      await screen.findByText('Playing local reply audio…')
      expect(getUserMedia).not.toHaveBeenCalled()
    },
  )

  it('stops current playback when conversation status changes even without a revision change', async () => {
    const { rerender } = render(
      <ConversationTranscript
        conversation={conversation}
        pending={false}
        onSend={vi.fn()}
      />,
    )
    await screen.findByText('Local speech ready.')
    fireEvent.click(
      screen.getByRole('button', { name: 'Listen to latest reply' }),
    )
    await screen.findByText('Playing local reply audio…')
    rerender(
      <ConversationTranscript
        conversation={{ ...conversation, status: 'ended' }}
        pending={false}
        onSend={vi.fn()}
      />,
    )
    expect(audioPause).toHaveBeenCalled()
    expect(screen.queryByRole('button', { name: 'Stop audio' })).toBeNull()
    expect(
      screen
        .getByRole('button', { name: 'Listen to latest reply' })
        .hasAttribute('disabled'),
    ).toBe(false)
  })

  it('disables recording during reply audio but leaves typed messaging available', async () => {
    render(
      <ConversationTranscript
        conversation={conversation}
        pending={false}
        onSend={vi.fn(async () => true)}
      />,
    )
    await screen.findByText('Local speech ready.')
    fireEvent.click(
      screen.getByRole('button', { name: 'Listen to latest reply' }),
    )
    await screen.findByText('Playing local reply audio…')
    expect(
      screen
        .getByRole('button', { name: 'Record courier message' })
        .hasAttribute('disabled'),
    ).toBe(true)
    expect(
      screen.getByLabelText('Courier message').hasAttribute('disabled'),
    ).toBe(false)
    fireEvent.click(
      screen.getByRole('button', { name: 'Record courier message' }),
    )
    expect(getUserMedia).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Stop audio' }))
    expect(
      screen
        .getByRole('button', { name: 'Record courier message' })
        .hasAttribute('disabled'),
    ).toBe(false)
    expect(audioPause).toHaveBeenCalled()
  })

  it('disables reply audio during microphone capture, transcription and transcript review', async () => {
    const onSend = vi.fn(async () => true)
    render(
      <ConversationTranscript
        conversation={conversation}
        pending={false}
        onSend={onSend}
      />,
    )
    await screen.findByText('Local speech ready.')
    fireEvent.click(
      screen.getByRole('button', { name: 'Record courier message' }),
    )
    await screen.findByRole('button', { name: 'Stop recording' })
    expect(
      screen
        .getByRole('button', { name: 'Listen to latest reply' })
        .hasAttribute('disabled'),
    ).toBe(true)
    act(() =>
      processor.onaudioprocess?.({
        inputBuffer: { getChannelData: () => new Float32Array(1600) },
      } as unknown as AudioProcessingEvent),
    )
    fireEvent.click(screen.getByRole('button', { name: 'Stop recording' }))
    await screen.findByLabelText('Review the local transcript')
    expect(
      screen
        .getByRole('button', { name: 'Listen to latest reply' })
        .hasAttribute('disabled'),
    ).toBe(true)
    expect(onSend).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Discard transcript' }))
    expect(
      screen
        .getByRole('button', { name: 'Listen to latest reply' })
        .hasAttribute('disabled'),
    ).toBe(false)
    expect(trackStop).toHaveBeenCalled()
  })

  it('does not offer old assistant audio after a resident decision becomes the latest saved message', async () => {
    render(
      <ConversationTranscript
        conversation={{
          ...conversation,
          messages: [
            ...conversation.messages,
            {
              role: 'resident',
              content: 'Take over',
              at: '2026-10-03T06:00:01Z',
            },
          ],
        }}
        pending={false}
        onSend={vi.fn()}
      />,
    )
    await screen.findByText('Local speech ready.')
    expect(
      screen
        .getByRole('button', { name: 'Listen to latest reply' })
        .hasAttribute('disabled'),
    ).toBe(true)
  })

  it('optional local speech status failure leaves the existing text path enabled', async () => {
    vi.mocked(fetch).mockRejectedValue(new Error('offline'))
    render(
      <ConversationTranscript
        conversation={conversation}
        pending={false}
        onSend={vi.fn()}
      />,
    )
    await screen.findByText(
      'Local voice is unavailable. Text conversation still works.',
    )
    expect(
      screen.getByLabelText('Courier message').hasAttribute('disabled'),
    ).toBe(false)
    expect(
      screen
        .getByRole('button', { name: 'Record courier message' })
        .hasAttribute('disabled'),
    ).toBe(true)
  })
})
