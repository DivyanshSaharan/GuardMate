import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { DesktopSetup } from './DesktopSetup'
import { deferred } from '../test/fixtures'
import {
  DesktopError,
  desktopRequest,
  type CallState,
  type DesktopStatus,
} from './client'

vi.mock('./client', async (original) => ({
  ...(await original<typeof import('./client')>()),
  desktopRequest: vi.fn(),
}))
const api = vi.mocked(desktopRequest)
const status: DesktopStatus = {
  desktop: true,
  windows: true,
  phone_link_installed: true,
  phone_link_message: '',
  setup_complete: true,
  delivery_mode_active: true,
  model_configured: true,
  stt_ready: true,
  tts_ready: true,
  physical_call_state_verified: false,
}
const input = {
  id: 10,
  name: 'Input (Pixel 9)',
  direction: 'input',
  host_api: 'Windows WDM-KS',
}
const output = {
  id: 11,
  name: 'Output (Pixel 9)',
  direction: 'output',
  host_api: 'Windows WDM-KS',
}
const empty: CallState = {
  busy: false,
  stopping: false,
  stopped: true,
  uncertain: false,
  draft: null,
  conversation: null,
  model_attempts: 0,
  max_turns: 5,
  events: [],
  error: '',
  physical_call_ended: false,
}

beforeEach(() => {
  api.mockReset()
  api.mockImplementation(async (path) => {
    if (path === '/status') return status
    if (path === '/devices/scan')
      return {
        devices: [
          input,
          output,
          {
            id: 0,
            name: 'Laptop microphone',
            direction: 'input',
            host_api: 'Windows WDM-KS',
          },
        ],
      }
    return empty
  })
})

async function open() {
  render(<DesktopSetup />)
  await screen.findByRole('heading', { name: 'Windows phone setup' })
  await waitFor(() => expect(api).toHaveBeenCalledWith('/call'))
}

describe('desktop call controls', () => {
  it('checks metadata without installing, opening audio or creating a call', async () => {
    await open()
    expect(api.mock.calls.map(([path]) => path)).toEqual(['/status', '/call'])
    expect(
      (
        screen.getByRole('button', {
          name: 'Start answered call',
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(true)
    expect(
      screen.queryByRole('button', { name: 'Capture 5-second caller turn' }),
    ).toBeNull()
  })

  it('opens the official install flow only after an explicit click', async () => {
    api.mockImplementation(async (path) =>
      path === '/status' ? { ...status, phone_link_installed: false } : empty,
    )
    await open()
    await act(async () =>
      fireEvent.click(
        screen.getByRole('button', { name: 'Install or enable Phone Link' }),
      ),
    )
    expect(api).toHaveBeenCalledWith('/phone-link/install', {})
    expect(
      await screen.findByText(/Complete installation in Microsoft Store/),
    ).toBeTruthy()
    expect(api.mock.calls.some(([path]) => path === '/call/start')).toBe(false)
  })

  it('requires paired endpoint selection and every consent before starting', async () => {
    await open()
    fireEvent.click(screen.getByText('Configure phone audio and call consent'))
    fireEvent.change(screen.getByLabelText('Paired phone name'), {
      target: { value: 'Pixel 9' },
    })
    await act(async () =>
      fireEvent.click(
        screen.getByRole('button', { name: 'Inspect phone audio endpoints' }),
      ),
    )
    expect(
      screen.queryByRole('option', { name: /Laptop microphone/ }),
    ).toBeNull()
    fireEvent.change(screen.getByLabelText('Phone input (caller audio)'), {
      target: { value: '10' },
    })
    fireEvent.change(screen.getByLabelText('Phone output (reply to caller)'), {
      target: { value: '11' },
    })
    fireEvent.change(screen.getByLabelText('Call label'), {
      target: { value: 'Demo courier' },
    })
    const start = screen.getByRole('button', {
      name: 'Start answered call',
    }) as HTMLButtonElement
    expect(start.disabled).toBe(true)
    for (const checkbox of screen.getAllByRole('checkbox'))
      fireEvent.click(checkbox)
    expect(start.disabled).toBe(false)
    await act(async () => fireEvent.click(start))
    expect(api).toHaveBeenCalledWith(
      '/call/start',
      expect.objectContaining({
        phone_name: 'Pixel 9',
        input,
        output,
        label: 'Demo courier',
        call_answered: true,
        consenting_call: true,
        hosted_text_consent: true,
        exclusive_audio_risk: true,
        phone_endpoints_confirmed: true,
      }),
    )
    expect(
      api.mock.calls.some(
        ([path]) => path === '/call/capture' || path === '/call/play',
      ),
    ).toBe(false)
  })

  it('shows first-run requirements and refreshes them after preferences and the window change', async () => {
    let current = {
      ...status,
      setup_complete: false,
      delivery_mode_active: false,
    }
    api.mockImplementation(async (path) =>
      path === '/status' ? current : empty,
    )
    const view = render(
      <DesktopSetup setupComplete={false} deliveryModeActive={false} />,
    )
    expect(
      await screen.findByRole('link', {
        name: 'Save your resident preferences',
      }),
    ).toBeTruthy()
    expect(
      screen.getByRole('link', { name: 'Enable a delivery window' }),
    ).toBeTruthy()
    expect(
      screen.queryByRole('button', { name: 'Install or enable Phone Link' }),
    ).toBeNull()
    current = status
    view.rerender(<DesktopSetup setupComplete deliveryModeActive />)
    expect(await screen.findByText('Resident preferences saved')).toBeTruthy()
    expect(screen.getByText('Delivery window active')).toBeTruthy()
    expect(api.mock.calls.filter(([path]) => path === '/status')).toHaveLength(
      2,
    )
    expect(api.mock.calls.some(([path]) => path === '/call/start')).toBe(false)
  })

  it('restores an active call after a page reload and keeps Stop outside collapsed setup', async () => {
    const active: CallState = {
      ...empty,
      stopped: false,
      conversation: {
        id: 'existing-call',
        courier_label: 'Existing courier',
        status: 'active',
        facts: {
          prepaid: null,
          guard_available: null,
          needs_otp: false,
          needs_signature: false,
          expensive: false,
        },
        messages: [],
        events: [],
        approval: null,
        authorized_location: null,
        courier_reported_outcome: null,
        created_at: '2026-10-20T10:00:00Z',
        turn_count: 0,
        revision: 1,
      },
    }
    api.mockImplementation(async (path) =>
      path === '/status' ? status : active,
    )
    await open()
    await screen.findByRole('list', { name: 'Phone conversation' })
    const stop = screen.getByRole('button', {
      name: 'Stop call assistant',
    }) as HTMLButtonElement
    expect(stop.disabled).toBe(false)
    expect(stop.closest('details')).toBeNull()
    expect(
      (
        screen
          .getByLabelText('Paired phone name')
          .closest('details') as HTMLDetailsElement
      ).open,
    ).toBe(false)
    await act(async () => fireEvent.click(stop))
    expect(api).toHaveBeenCalledWith('/call/stop', {})
  })

  it('does not replace a confirmed Stop with a late initial call snapshot', async () => {
    const initial = deferred<CallState>()
    api.mockImplementation(async (path) => {
      if (path === '/status') return status
      if (path === '/call') return initial.promise
      return empty
    })
    await open()
    await act(async () =>
      fireEvent.click(
        screen.getByRole('button', { name: 'Stop call assistant' }),
      ),
    )
    expect(api).toHaveBeenCalledWith('/call/stop', {})
    await act(async () =>
      initial.resolve({ ...empty, error: 'Stale call snapshot' }),
    )
    expect(screen.queryByText('Stale call snapshot')).toBeNull()
    expect(
      (
        screen.getByRole('button', {
          name: 'Stop call assistant',
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(true)
  })

  it('hides local controls on the hosted app when the route is unavailable', async () => {
    api.mockRejectedValue(new DesktopError('Not found', 404))
    const view = render(<DesktopSetup />)
    await waitFor(() => expect(api).toHaveBeenCalled())
    expect(view.container.textContent).toBe('')
  })
})
