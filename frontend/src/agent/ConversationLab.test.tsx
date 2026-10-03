import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { request } from '../api'
import { deferred } from '../test/fixtures'
import { ConversationLab } from './ConversationLab'
import { summarize } from './sessionState'
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
    courier_label: 'Courier A',
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
  fireEvent.change(screen.getByLabelText('New courier label'), {
    target: { value: 'Courier A' },
  })
  await act(async () =>
    fireEvent.click(screen.getByRole('button', { name: 'Start role-play' })),
  )
}
beforeEach(() => {
  localStorage.clear()
  api.mockReset()
  api.mockImplementation(async (path, _payload, _signal, method) =>
    path === '/agent/status'
      ? model
      : path === '/conversations' && method !== 'POST'
        ? []
        : session(),
  )
})
afterEach(() => vi.useRealTimers())

function courierApi(initial: Conversation[] = []) {
  const saved = new Map(
    initial.map((conversation) => [conversation.id, conversation]),
  )
  const handler = async (
    path: string,
    payload?: unknown,
    _signal?: AbortSignal,
    method?: string,
  ) => {
    if (path === '/agent/status') return model
    if (path === '/conversations' && method !== 'POST') {
      return Array.from(saved.values()).map(summarize)
    }
    if (path === '/conversations' && method === 'POST') {
      const next = session({
        id: `courier-${saved.size + 1}`,
        courier_label: (payload as { courier_label: string }).courier_label,
      })
      saved.set(next.id, next)
      return next
    }
    const id = path.split('/')[2]
    const current = saved.get(id)
    if (!current) throw new Error('Conversation not found.')
    if (path.endsWith('/resident')) {
      const decision = (payload as { decision: string }).decision
      const next = {
        ...current,
        revision: current.revision + 1,
        status: decision === 'end' ? ('ended' as const) : ('active' as const),
        approval:
          current.approval && decision === 'approve'
            ? { ...current.approval, status: 'approved' as const }
            : current.approval,
      }
      saved.set(id, next)
      return next
    }
    return current
  }
  api.mockImplementation(handler)
  return { saved, handler }
}

async function startCourier(label: string) {
  fireEvent.change(screen.getByLabelText('New courier label'), {
    target: { value: label },
  })
  await act(async () =>
    fireEvent.click(screen.getByRole('button', { name: 'Start role-play' })),
  )
}

async function selectCourier(id: string) {
  await act(async () =>
    fireEvent.change(screen.getByLabelText('Saved role-play'), {
      target: { value: id },
    }),
  )
}

describe('text role-play', () => {
  it('does not request model configuration or spend credits until opened', () => {
    render(<ConversationLab />)
    expect(api).not.toHaveBeenCalled()
    expect(screen.getByText(/No phone call is answered/)).toBeTruthy()
  })

  it('shows hosted-data disclosure and blocks starting when not configured', async () => {
    api.mockImplementation(async (path) =>
      path === '/agent/status' ? { ...model, configured: false } : [],
    )
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
    fireEvent.change(screen.getByLabelText('New courier label'), {
      target: { value: 'Courier B' },
    })
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
    api.mockImplementation(async (path, _payload, _signal, method) =>
      path === '/agent/status'
        ? model
        : path === '/conversations' && method !== 'POST'
          ? []
          : pending,
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
    api.mockImplementation(async (path, _payload, _signal, method) =>
      path === '/agent/status'
        ? model
        : path === '/conversations' && method !== 'POST'
          ? []
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

  it('starts another courier without ending the first, preserves separate drafts and ends only the selection', async () => {
    const { saved } = courierApi()
    await open()
    await startCourier('Courier A')
    fireEvent.change(screen.getByLabelText('Courier message'), {
      target: { value: 'Draft for A' },
    })
    await startCourier('Courier B')
    expect(screen.getByRole('heading', { name: 'Courier B' })).toBeTruthy()
    expect(
      (screen.getByLabelText('Courier message') as HTMLInputElement).value,
    ).toBe('')
    expect(saved.get('courier-1')?.status).toBe('active')
    expect(api).toHaveBeenCalledWith(
      '/conversations',
      { courier_label: 'Courier B' },
      undefined,
      'POST',
    )
    fireEvent.change(screen.getByLabelText('Courier message'), {
      target: { value: 'Draft for B' },
    })
    await selectCourier('courier-1')
    expect(
      (screen.getByLabelText('Courier message') as HTMLInputElement).value,
    ).toBe('Draft for A')
    await act(async () =>
      fireEvent.click(screen.getByRole('button', { name: 'End role-play' })),
    )
    expect(saved.get('courier-1')?.status).toBe('ended')
    expect(saved.get('courier-2')?.status).toBe('active')
    await selectCourier('courier-2')
    expect(
      (screen.getByLabelText('Courier message') as HTMLInputElement).value,
    ).toBe('Draft for B')
    expect(
      (screen.getByLabelText('Courier message') as HTMLInputElement).disabled,
    ).toBe(false)
  })

  it("switches history and resident approvals together and never uses another courier's approval ID", async () => {
    const a = session({
      id: 'courier-a',
      courier_label: 'Courier A',
      status: 'awaiting_approval',
      approval: {
        id: 'approval-a',
        location: 'A reception',
        reason: 'Guard absent.',
        status: 'pending',
        expires_at: '2026-10-03T10:01:30Z',
      },
      messages: [
        {
          role: 'courier',
          content: 'History for A only',
          at: '2026-10-03T10:00:00Z',
        },
      ],
    })
    const b = session({
      id: 'courier-b',
      courier_label: 'Courier B',
      messages: [
        {
          role: 'courier',
          content: 'History for B only',
          at: '2026-10-03T10:00:00Z',
        },
      ],
    })
    courierApi([a, b])
    localStorage.setItem('guardmate-role-play', a.id)
    await open()
    expect(screen.getByText('A reception')).toBeTruthy()
    await selectCourier(b.id)
    expect(screen.queryByText('A reception')).toBeNull()
    expect(screen.queryByText('History for A only')).toBeNull()
    expect(screen.getByText('History for B only')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Approve once' })).toBeNull()
    await selectCourier(a.id)
    expect(screen.queryByText('History for B only')).toBeNull()
    await act(async () =>
      fireEvent.click(screen.getByRole('button', { name: 'Approve once' })),
    )
    expect(api).toHaveBeenCalledWith('/conversations/courier-a/resident', {
      decision: 'approve',
      approval_id: 'approval-a',
      revision: 0,
    })
  })

  it('locks switching and new sessions until the selected courier response completes', async () => {
    const a = session({ id: 'courier-a', courier_label: 'Courier A' })
    const b = session({ id: 'courier-b', courier_label: 'Courier B' })
    const { handler } = courierApi([a, b])
    const response = deferred<Conversation>()
    api.mockImplementation((path, ...args) =>
      path.endsWith('/turns') ? response.promise : handler(path, ...args),
    )
    localStorage.setItem('guardmate-role-play', a.id)
    await open()
    fireEvent.change(screen.getByLabelText('Courier message'), {
      target: { value: 'Where do I go?' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    expect(
      (screen.getByLabelText('Saved role-play') as HTMLSelectElement).disabled,
    ).toBe(true)
    expect(
      (screen.getByLabelText('New courier label') as HTMLInputElement).disabled,
    ).toBe(true)
    fireEvent.change(screen.getByLabelText('Saved role-play'), {
      target: { value: b.id },
    })
    await act(async () =>
      response.resolve({ ...a, revision: 1, turn_count: 1 }),
    )
    expect(screen.getByRole('heading', { name: 'Courier A' })).toBeTruthy()
    expect(
      api.mock.calls.filter(([path]) => path === '/conversations/courier-b')
        .length,
    ).toBe(0)
    expect(
      (screen.getByLabelText('Saved role-play') as HTMLSelectElement).disabled,
    ).toBe(false)
  })

  it('rejects a mismatched response instead of replacing the selected courier', async () => {
    const a = session({ id: 'courier-a', courier_label: 'Courier A' })
    const b = session({ id: 'courier-b', courier_label: 'Courier B' })
    const { handler } = courierApi([a, b])
    api.mockImplementation((path, ...args) =>
      path.endsWith('/turns') ? Promise.resolve(b) : handler(path, ...args),
    )
    localStorage.setItem('guardmate-role-play', a.id)
    await open()
    fireEvent.change(screen.getByLabelText('Courier message'), {
      target: { value: 'Only for A' },
    })
    await act(async () =>
      fireEvent.click(screen.getByRole('button', { name: 'Send' })),
    )
    expect(screen.getByRole('alert').textContent).toContain(
      'different role-play',
    )
    expect(screen.getByRole('heading', { name: 'Courier A' })).toBeTruthy()
    expect(
      (screen.getByLabelText('Courier message') as HTMLInputElement).value,
    ).toBe('Only for A')
    expect(localStorage.getItem('guardmate-role-play')).toBe(a.id)
  })

  it('restores the selected courier after reopening and keeps identical labels as distinct IDs', async () => {
    const a = session({ id: 'courier-a', courier_label: 'Same courier' })
    const b = session({ id: 'courier-b', courier_label: 'Same courier' })
    courierApi([a, b])
    await open()
    await selectCourier(b.id)
    await act(async () =>
      fireEvent.click(screen.getByRole('button', { name: 'Close panel' })),
    )
    await act(async () =>
      fireEvent.click(
        screen.getByRole('button', { name: 'Open text role-play' }),
      ),
    )
    expect(
      (screen.getByLabelText('Saved role-play') as HTMLSelectElement).value,
    ).toBe(b.id)
    expect(screen.getByText('Role-play courier-')).toBeTruthy()
    expect(
      screen
        .getAllByRole('option')
        .filter((option) =>
          (option as HTMLOptionElement).value.startsWith('courier-'),
        ).length,
    ).toBe(2)
    expect(localStorage.getItem('guardmate-role-play')).toBe(b.id)
  })

  it('ignores a late poll from the previous courier after switching', async () => {
    vi.useFakeTimers()
    const a = session({
      id: 'courier-a',
      courier_label: 'Courier A',
      approval: {
        id: 'approval-a',
        location: 'reception',
        reason: 'Guard absent.',
        status: 'pending',
        expires_at: '2026-10-03T10:01:30Z',
      },
    })
    const b = session({ id: 'courier-b', courier_label: 'Courier B' })
    const { handler } = courierApi([a, b])
    const oldPoll = deferred<Conversation>()
    let delayPoll = false
    api.mockImplementation((path, ...args) =>
      path === '/conversations/courier-a' && delayPoll
        ? oldPoll.promise
        : handler(path, ...args),
    )
    localStorage.setItem('guardmate-role-play', a.id)
    await open()
    delayPoll = true
    await act(async () => vi.advanceTimersByTime(5000))
    await selectCourier(b.id)
    await act(async () =>
      oldPoll.resolve({
        ...a,
        revision: 99,
        messages: [
          {
            role: 'assistant',
            content: 'Late reply for A',
            at: '2026-10-03T10:01:00Z',
          },
        ],
      }),
    )
    expect(screen.queryByText('Late reply for A')).toBeNull()
    expect(screen.getByRole('heading', { name: 'Courier B' })).toBeTruthy()
    expect(localStorage.getItem('guardmate-role-play')).toBe(b.id)
  })
})
