import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ReplyPlayback } from './ReplyPlayback'

const players: FakeAudio[] = []
class FakeAudio {
  src: string
  onended: (() => void) | null = null
  onerror: (() => void) | null = null
  play = vi.fn(async () => {})
  pause = vi.fn()
  constructor(src: string) {
    this.src = src
    players.push(this)
  }
}
const createObjectURL = vi.fn(() => 'blob:local-reply')
const revokeObjectURL = vi.fn()
beforeEach(() => {
  players.length = 0
  createObjectURL.mockClear()
  revokeObjectURL.mockClear()
  vi.stubGlobal('Audio', FakeAudio)
  vi.stubGlobal('URL', { createObjectURL, revokeObjectURL })
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => ({
      ok: true,
      blob: async () => new Blob(['audio'], { type: 'audio/wav' }),
    })),
  )
})
afterEach(() => vi.unstubAllGlobals())

describe('local checked-reply playback', () => {
  it('requests only a saved reply by revision/index and plays only after an explicit click', async () => {
    render(
      <ReplyPlayback
        sessionId="a"
        revision={4}
        messageIndex={2}
        blocked={false}
        ready
      />,
    )
    expect(fetch).not.toHaveBeenCalled()
    fireEvent.click(
      screen.getByRole('button', { name: 'Listen to latest reply' }),
    )
    await screen.findByText('Playing local reply audio…')
    expect(fetch).toHaveBeenCalledWith(
      '/api/conversations/a/speech',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ revision: 4, message_index: 2 }),
      }),
    )
    expect(players[0].play).toHaveBeenCalledOnce()
    fireEvent.click(screen.getByRole('button', { name: 'Stop audio' }))
    expect(players[0].pause).toHaveBeenCalled()
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:local-reply')
  })

  it('does not play when blocked, no latest assistant reply, or voice is unavailable', () => {
    const { rerender } = render(
      <ReplyPlayback
        sessionId="a"
        revision={0}
        messageIndex={0}
        blocked
        ready
      />,
    )
    expect(
      screen
        .getByRole('button', { name: 'Listen to latest reply' })
        .hasAttribute('disabled'),
    ).toBe(true)
    rerender(
      <ReplyPlayback
        sessionId="a"
        revision={0}
        messageIndex={null}
        blocked={false}
        ready
      />,
    )
    expect(
      screen
        .getByRole('button', { name: 'Listen to latest reply' })
        .hasAttribute('disabled'),
    ).toBe(true)
    rerender(
      <ReplyPlayback
        sessionId="a"
        revision={0}
        messageIndex={0}
        blocked={false}
        ready={false}
      />,
    )
    expect(
      screen
        .getByRole('button', { name: 'Listen to latest reply' })
        .hasAttribute('disabled'),
    ).toBe(true)
    expect(fetch).not.toHaveBeenCalled()
  })

  it('cleans playback and object URLs when a new revision, pending state, or session arrives', async () => {
    const { rerender } = render(
      <ReplyPlayback
        sessionId="a"
        revision={0}
        messageIndex={0}
        blocked={false}
        ready
      />,
    )
    fireEvent.click(
      screen.getByRole('button', { name: 'Listen to latest reply' }),
    )
    await screen.findByText('Playing local reply audio…')
    rerender(
      <ReplyPlayback
        sessionId="a"
        revision={1}
        messageIndex={2}
        blocked={false}
        ready
      />,
    )
    expect(players[0].pause).toHaveBeenCalled()
    expect(revokeObjectURL).toHaveBeenCalledOnce()
    fireEvent.click(
      screen.getByRole('button', { name: 'Listen to latest reply' }),
    )
    await screen.findByText('Playing local reply audio…')
    rerender(
      <ReplyPlayback
        sessionId="b"
        revision={0}
        messageIndex={0}
        blocked
        ready
      />,
    )
    expect(players[1].pause).toHaveBeenCalled()
    expect(revokeObjectURL).toHaveBeenCalledTimes(2)
  })

  it('discards an audio response arriving after session switch', async () => {
    let resolve!: (response: Response) => void
    vi.mocked(fetch).mockReturnValue(
      new Promise<Response>((yes) => {
        resolve = yes
      }),
    )
    const { rerender } = render(
      <ReplyPlayback
        sessionId="a"
        revision={0}
        messageIndex={0}
        blocked={false}
        ready
      />,
    )
    fireEvent.click(
      screen.getByRole('button', { name: 'Listen to latest reply' }),
    )
    const signal = vi.mocked(fetch).mock.calls[0][1]?.signal
    rerender(
      <ReplyPlayback
        sessionId="b"
        revision={0}
        messageIndex={0}
        blocked={false}
        ready
      />,
    )
    expect(signal?.aborted).toBe(true)
    await act(async () =>
      resolve({ ok: true, blob: async () => new Blob(['late']) } as Response),
    )
    expect(players).toHaveLength(0)
    expect(createObjectURL).not.toHaveBeenCalled()
  })

  it('stops and revokes audio on unmount or natural completion', async () => {
    const { unmount } = render(
      <ReplyPlayback
        sessionId="a"
        revision={0}
        messageIndex={0}
        blocked={false}
        ready
      />,
    )
    fireEvent.click(
      screen.getByRole('button', { name: 'Listen to latest reply' }),
    )
    await screen.findByText('Playing local reply audio…')
    act(() => players[0].onended?.())
    expect(revokeObjectURL).toHaveBeenCalledOnce()
    fireEvent.click(
      screen.getByRole('button', { name: 'Listen to latest reply' }),
    )
    await screen.findByText('Playing local reply audio…')
    unmount()
    expect(players[1].pause).toHaveBeenCalled()
    expect(revokeObjectURL).toHaveBeenCalledTimes(2)
  })

  it('displays optional speech failure without substituting cloud speech', async () => {
    vi.mocked(fetch).mockResolvedValue({
      ok: false,
      json: async () => ({
        detail: 'The conversation changed. Refresh before playing.',
      }),
    } as Response)
    render(
      <ReplyPlayback
        sessionId="a"
        revision={0}
        messageIndex={0}
        blocked={false}
        ready
      />,
    )
    fireEvent.click(
      screen.getByRole('button', { name: 'Listen to latest reply' }),
    )
    expect((await screen.findByRole('alert')).textContent).toContain(
      'conversation changed',
    )
    expect(players).toHaveLength(0)
  })
})
