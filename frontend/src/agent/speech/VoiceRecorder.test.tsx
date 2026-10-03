import {
  act,
  fireEvent,
  render,
  renderHook,
  screen,
} from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ConversationComposer } from '../ConversationComposer'
import { useVoiceRecorder } from './useVoiceRecorder'

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((yes, no) => {
    resolve = yes
    reject = no
  })
  return { promise, resolve, reject }
}

const contexts: FakeContext[] = []
const trackStop = vi.fn()
const stream = {
  getTracks: () => [{ stop: trackStop }],
} as unknown as MediaStream
const getUserMedia = vi.fn()
const node = () => ({ connect: vi.fn(), disconnect: vi.fn() })
class FakeContext {
  sampleRate = 48_000
  destination = {}
  source = node()
  processor = {
    ...node(),
    onaudioprocess: null as ((event: AudioProcessingEvent) => void) | null,
  }
  gain = { ...node(), gain: { value: 1 } }
  resume = vi.fn(async () => {})
  close = vi.fn(async () => {})
  constructor() {
    contexts.push(this)
  }
  createMediaStreamSource() {
    return this.source
  }
  createScriptProcessor() {
    return this.processor
  }
  createGain() {
    return this.gain
  }
  emit() {
    this.processor.onaudioprocess?.({
      inputBuffer: { getChannelData: () => new Float32Array(16_000).fill(0.2) },
    } as unknown as AudioProcessingEvent)
  }
}
let mediaDescriptor: PropertyDescriptor | undefined

beforeEach(() => {
  contexts.length = 0
  trackStop.mockReset()
  getUserMedia.mockReset().mockResolvedValue(stream)
  mediaDescriptor = Object.getOwnPropertyDescriptor(navigator, 'mediaDevices')
  Object.defineProperty(navigator, 'mediaDevices', {
    configurable: true,
    value: { getUserMedia },
  })
  vi.stubGlobal('AudioContext', FakeContext)
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => ({
      ok: true,
      json: async () => ({
        text: 'Where is the guard room?',
        duration_ms: 333,
        processing_ms: 100,
      }),
    })),
  )
})
afterEach(() => {
  if (mediaDescriptor)
    Object.defineProperty(navigator, 'mediaDevices', mediaDescriptor)
  else Reflect.deleteProperty(navigator, 'mediaDevices')
  vi.unstubAllGlobals()
})

describe('opt-in local recording', () => {
  it('reviews speech, preserves a typed draft, and never automatically sends to the model', async () => {
    const onSend = vi.fn(async () => true)
    render(
      <ConversationComposer
        sessionId="a"
        pending={false}
        disabled={false}
        speechReady
        onSend={onSend}
      />,
    )
    fireEvent.change(screen.getByLabelText('Courier message'), {
      target: { value: 'I have a parcel.' },
    })
    fireEvent.click(
      screen.getByRole('button', { name: 'Record courier message' }),
    )
    await screen.findByRole('button', { name: 'Stop recording' })
    contexts[0].emit()
    fireEvent.click(screen.getByRole('button', { name: 'Stop recording' }))
    const review = await screen.findByLabelText('Review the local transcript')
    expect(trackStop).toHaveBeenCalled()
    expect(contexts[0].close).toHaveBeenCalled()
    expect((review as HTMLTextAreaElement).value).toBe(
      'Where is the guard room?',
    )
    expect(onSend).not.toHaveBeenCalled()
    fireEvent.change(review, { target: { value: 'Where should I leave it?' } })
    fireEvent.click(screen.getByRole('button', { name: 'Use transcript' }))
    expect(
      (screen.getByLabelText('Courier message') as HTMLInputElement).value,
    ).toBe('I have a parcel. Where should I leave it?')
    expect(onSend).not.toHaveBeenCalled()
    expect(fetch).toHaveBeenCalledWith(
      '/api/speech/transcribe',
      expect.objectContaining({
        method: 'POST',
        headers: { 'Content-Type': 'audio/wav' },
        body: expect.any(Blob),
      }),
    )
  })

  it('disables recording while waiting for a response or resident action', () => {
    const { rerender } = render(
      <ConversationComposer
        sessionId="a"
        pending
        disabled={false}
        speechReady
        onSend={vi.fn()}
      />,
    )
    expect(
      screen
        .getByRole('button', { name: 'Record courier message' })
        .hasAttribute('disabled'),
    ).toBe(true)
    fireEvent.click(
      screen.getByRole('button', { name: 'Record courier message' }),
    )
    expect(getUserMedia).not.toHaveBeenCalled()
    rerender(
      <ConversationComposer
        sessionId="a"
        pending={false}
        disabled
        speechReady
        onSend={vi.fn()}
      />,
    )
    expect(
      screen
        .getByRole('button', { name: 'Record courier message' })
        .hasAttribute('disabled'),
    ).toBe(true)
  })

  it('shows a useful permission denial without breaking the text draft', async () => {
    getUserMedia.mockRejectedValue(
      new DOMException('denied', 'NotAllowedError'),
    )
    render(
      <ConversationComposer
        sessionId="a"
        pending={false}
        disabled={false}
        speechReady
        onSend={vi.fn()}
      />,
    )
    fireEvent.change(screen.getByLabelText('Courier message'), {
      target: { value: 'Prepaid parcel' },
    })
    fireEvent.click(
      screen.getByRole('button', { name: 'Record courier message' }),
    )
    expect((await screen.findByRole('alert')).textContent).toContain(
      'permission was denied',
    )
    expect(
      (screen.getByLabelText('Courier message') as HTMLInputElement).value,
    ).toBe('Prepaid parcel')
    expect(fetch).not.toHaveBeenCalled()
  })

  it('stops a stream arriving after cancellation, without creating an audio context', async () => {
    const permission = deferred<MediaStream>()
    getUserMedia.mockReturnValue(permission.promise)
    const { result } = renderHook(() => useVoiceRecorder('a', false))
    let opening!: Promise<void>
    act(() => {
      opening = result.current.start()
    })
    expect(result.current.phase).toBe('requesting')
    act(() => result.current.cancel())
    await act(async () => {
      permission.resolve(stream)
      await opening
    })
    expect(trackStop).toHaveBeenCalledOnce()
    expect(contexts).toHaveLength(0)
    expect(result.current.phase).toBe('idle')
  })

  it('ignores microphone permission granted after a different session was selected', async () => {
    const permission = deferred<MediaStream>()
    getUserMedia.mockReturnValue(permission.promise)
    const { result, rerender } = renderHook(
      ({ id }) => useVoiceRecorder(id, false),
      { initialProps: { id: 'a' } },
    )
    let opening!: Promise<void>
    act(() => {
      opening = result.current.start()
    })
    rerender({ id: 'b' })
    await act(async () => {
      permission.resolve(stream)
      await opening
    })
    expect(trackStop).toHaveBeenCalledOnce()
    expect(contexts).toHaveLength(0)
    expect(result.current.phase).toBe('idle')
  })

  it('stops an acquired microphone when audio context construction fails', async () => {
    vi.stubGlobal(
      'AudioContext',
      class {
        constructor() {
          throw new Error('Audio unavailable')
        }
      },
    )
    const { result } = renderHook(() => useVoiceRecorder('a', false))
    await act(async () => result.current.start())
    expect(trackStop).toHaveBeenCalledOnce()
    expect(result.current.error).toBe('Audio unavailable')
    expect(result.current.phase).toBe('idle')
  })

  it('stops microphone tracks and context on unmount', async () => {
    const { result, unmount } = renderHook(() => useVoiceRecorder('a', false))
    await act(async () => result.current.start())
    expect(result.current.phase).toBe('recording')
    unmount()
    expect(trackStop).toHaveBeenCalled()
    expect(contexts[0].close).toHaveBeenCalled()
    expect(contexts[0].processor.onaudioprocess).toBeNull()
    expect(fetch).not.toHaveBeenCalled()
  })

  it.each(['session', 'pending'] as const)(
    'ignores late transcription after %s changes',
    async (change) => {
      const response = deferred<unknown>()
      vi.mocked(fetch).mockReturnValue(response.promise as Promise<Response>)
      const { result, rerender } = renderHook(
        ({ id, blocked }) => useVoiceRecorder(id, blocked),
        { initialProps: { id: 'a', blocked: false } },
      )
      await act(async () => result.current.start())
      contexts[0].emit()
      let stopping!: Promise<void>
      act(() => {
        stopping = result.current.stop()
      })
      const signal = vi.mocked(fetch).mock.calls[0][1]?.signal
      rerender(
        change === 'session'
          ? { id: 'b', blocked: false }
          : { id: 'a', blocked: true },
      )
      expect(signal?.aborted).toBe(true)
      await act(async () => {
        response.resolve({
          ok: true,
          json: async () => ({
            text: 'A different courier',
            duration_ms: 333,
            processing_ms: 100,
          }),
        })
        await stopping
      })
      expect(result.current.transcript).toBe('')
      expect(result.current.phase).toBe('idle')
    },
  )

  it('caps recording at 30 seconds and transcribes only after stopping the microphone', async () => {
    vi.useFakeTimers()
    const { result } = renderHook(() => useVoiceRecorder('a', false))
    await act(async () => result.current.start())
    contexts[0].emit()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000)
    })
    expect(trackStop).toHaveBeenCalled()
    expect(contexts[0].close).toHaveBeenCalled()
    expect(result.current.phase).toBe('review')
    expect(fetch).toHaveBeenCalledOnce()
  })

  it('stops the microphone when a response becomes pending', async () => {
    const { result, rerender } = renderHook(
      ({ blocked }) => useVoiceRecorder('a', blocked),
      { initialProps: { blocked: false } },
    )
    await act(async () => result.current.start())
    rerender({ blocked: true })
    expect(trackStop).toHaveBeenCalled()
    expect(contexts[0].close).toHaveBeenCalled()
    expect(result.current.phase).toBe('idle')
    expect(fetch).not.toHaveBeenCalled()
  })

  it('retains text usability after the optional transcription service fails', async () => {
    vi.mocked(fetch).mockResolvedValue({
      ok: false,
      json: async () => ({ detail: 'Install the local speech model first.' }),
    } as Response)
    const { result } = renderHook(() => useVoiceRecorder('a', false))
    await act(async () => result.current.start())
    contexts[0].emit()
    await act(async () => result.current.stop())
    expect(result.current.error).toBe('Install the local speech model first.')
    expect(result.current.phase).toBe('idle')
    expect(trackStop).toHaveBeenCalled()
  })
})
