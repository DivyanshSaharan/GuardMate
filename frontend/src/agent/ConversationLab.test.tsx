import { act, fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { request } from '../api'
import { deferred } from '../test/fixtures'
import { ConversationLab } from './ConversationLab'
import type { Conversation, ModelStatus } from './types'

vi.mock('../api', () => ({ request: vi.fn() }))
const api = vi.mocked(request)
const model: ModelStatus = {
  configured: true,
  model: 'Qwen/Qwen3.5-4B',
  provider: 'Tinker',
  message: 'Key configured.',
  reserved_usd: 0.001,
  budget_usd: 0.25,
  voice_connected: false,
}
function session(overrides: Partial<Conversation> = {}): Conversation {
  return {
    id: 'demo-session',
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
        content: 'Hello, I’m the AI delivery assistant.',
        at: '2026-10-03T06:00:00Z',
      },
    ],
    events: [],
    approval: null,
    authorized_location: null,
    courier_reported_outcome: null,
    created_at: '2026-10-03T06:00:00Z',
    ...overrides,
  }
}
async function open() {
  render(<ConversationLab />)
  await act(async () =>
    fireEvent.click(
      screen.getByRole('button', { name: 'Open text role-play' }),
    ),
  )
}
async function start() {
  await open()
  await act(async () =>
    fireEvent.click(screen.getByRole('button', { name: 'Start role-play' })),
  )
}
beforeEach(() => {
  localStorage.clear()
  api.mockReset()
  api.mockImplementation(async (path) =>
    path === '/agent/status' ? model : session(),
  )
})

describe('text role-play', () => {
  it('does not request model configuration or spend credits until opened', () => {
    render(<ConversationLab />)
    expect(api).not.toHaveBeenCalled()
    expect(screen.getByText(/No phone call is answered/)).toBeTruthy()
  })

  it('shows hosted-data disclosure and blocks starting when not configured', async () => {
    api.mockResolvedValue({ ...model, configured: false })
    await open()
    expect(
      (
        screen.getByRole('button', {
          name: 'Start role-play',
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(true)
    expect(
      screen.getByText(
        /Saved instructions and role-play messages are sent to Tinker/,
      ),
    ).toBeTruthy()
  })

  it('sends a revisioned courier turn and preserves draft on failure', async () => {
    await start()
    const input = screen.getByLabelText('Courier message') as HTMLInputElement
    fireEvent.change(input, { target: { value: 'The guard is not here.' } })
    api.mockRejectedValueOnce(new Error('Conversation changed. Refresh it.'))
    await act(async () =>
      fireEvent.click(screen.getByRole('button', { name: 'Send' })),
    )
    expect(input.value).toBe('The guard is not here.')
    expect(screen.getByRole('alert').textContent).toContain(
      'Conversation changed',
    )
    expect(api).toHaveBeenCalledWith(
      '/conversations/demo-session/turns',
      {
        text: 'The guard is not here.',
        revision: 0,
      },
      undefined,
      'POST',
    )
  })

  it('shows the outgoing bubble and loader immediately, clears input and blocks duplicate sends', async () => {
    await start()
    const response = deferred<Conversation>()
    api.mockReturnValueOnce(response.promise)
    const input = screen.getByLabelText('Courier message') as HTMLInputElement
    fireEvent.change(input, { target: { value: 'Is it the pedestrian gate?' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    expect(input.disabled).toBe(true)
    expect(
      (
        screen.getByRole('button', {
          name: 'End role-play',
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(true)
    expect(input.value).toBe('')
    expect(screen.getByText('Is it the pedestrian gate?')).toBeTruthy()
    expect(
      screen.getByRole('status', { name: 'GuardMate is preparing a reply' }),
    ).toBeTruthy()
    expect(
      (screen.getByRole('button', { name: 'Waiting…' }) as HTMLButtonElement)
        .disabled,
    ).toBe(true)
    fireEvent.submit(input.closest('form')!)
    expect(
      api.mock.calls.filter(([path]) => path.endsWith('/turns')).length,
    ).toBe(1)
    await act(async () =>
      response.resolve(session({ revision: 1, turn_count: 1 })),
    )
    expect(input.value).toBe('')
    expect(input.disabled).toBe(false)
    expect(
      screen.queryByRole('status', { name: 'GuardMate is preparing a reply' }),
    ).toBeNull()
  })

  it('replaces the optimistic bubble with one canonical courier message after acknowledgment', async () => {
    await start()
    const response = deferred<Conversation>()
    api.mockReturnValueOnce(response.promise)
    const input = screen.getByLabelText('Courier message') as HTMLInputElement
    fireEvent.change(input, { target: { value: 'yes' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    expect(screen.getAllByText('yes').length).toBe(1)
    await act(async () =>
      response.resolve(
        session({
          revision: 1,
          turn_count: 1,
          messages: [
            ...session().messages,
            { role: 'courier', content: 'yes', at: '2026-10-03T06:00:01Z' },
            {
              role: 'assistant',
              content: 'Please check for security.',
              at: '2026-10-03T06:00:02Z',
            },
          ],
        }),
      ),
    )
    expect(screen.getAllByText('yes').length).toBe(1)
    expect(screen.queryByText('Courier · Sending')).toBeNull()
  })

  it('marks an uncertain response and restores the draft instead of silently dropping the message', async () => {
    await start()
    api.mockRejectedValueOnce(new Error('Unable to reach GuardMate.'))
    const input = screen.getByLabelText('Courier message') as HTMLInputElement
    fireEvent.change(input, { target: { value: 'Let me check' } })
    await act(async () =>
      fireEvent.click(screen.getByRole('button', { name: 'Send' })),
    )
    expect(input.value).toBe('Let me check')
    expect(screen.getByText('Courier · Response not confirmed')).toBeTruthy()
    expect(screen.getByText(/Refresh status before retrying/)).toBeTruthy()
    expect(
      screen.queryByRole('status', { name: 'GuardMate is preparing a reply' }),
    ).toBeNull()
  })

  it('provides one prominent end control beside start and ends through the resident endpoint', async () => {
    await start()
    const controls = screen.getByRole('group', { name: 'Role-play controls' })
    const end = screen.getByRole('button', { name: 'End role-play' })
    expect(controls.contains(end)).toBe(true)
    expect(
      controls.contains(
        screen.getByRole('button', { name: 'Start role-play' }),
      ),
    ).toBe(true)
    expect(end.classList.contains('role-play-end')).toBe(true)
    expect(
      screen.getAllByRole('button', { name: 'End role-play' }).length,
    ).toBe(1)
    api.mockResolvedValueOnce(session({ status: 'ended', revision: 1 }))
    await act(async () => fireEvent.click(end))
    expect(api).toHaveBeenCalledWith('/conversations/demo-session/resident', {
      decision: 'end',
      approval_id: null,
      revision: 0,
    })
    expect((end as HTMLButtonElement).disabled).toBe(true)
    expect(
      (
        screen.getByRole('button', {
          name: 'Start role-play',
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(false)
  })

  it('resident approval uses its own endpoint and exact pending proposal', async () => {
    const pending = session({
      status: 'awaiting_approval',
      approval: {
        id: 'approval-one',
        location: 'reception',
        reason: 'Security unavailable.',
        status: 'pending',
        expires_at: '2026-10-03T06:01:30Z',
      },
    })
    api.mockImplementation(async (path) =>
      path === '/agent/status' ? model : pending,
    )
    await start()
    expect(screen.getByText('reception')).toBeTruthy()
    await act(async () =>
      fireEvent.click(screen.getByRole('button', { name: 'Approve once' })),
    )
    expect(api).toHaveBeenCalledWith('/conversations/demo-session/resident', {
      decision: 'approve',
      approval_id: 'approval-one',
      revision: 0,
    })
  })

  it('restores a local session and disables courier messages after takeover', async () => {
    localStorage.setItem('guardmate-role-play', 'demo-session')
    api.mockImplementation(async (path) =>
      path === '/agent/status'
        ? model
        : session({ status: 'needs_resident', revision: 1 }),
    )
    await open()
    expect(
      (screen.getByLabelText('Courier message') as HTMLInputElement).disabled,
    ).toBe(true)
    expect(screen.getByText(/Agent paused/)).toBeTruthy()
    expect(api).toHaveBeenCalledWith(
      '/conversations/demo-session',
      undefined,
      expect.any(AbortSignal),
    )
  })
})
