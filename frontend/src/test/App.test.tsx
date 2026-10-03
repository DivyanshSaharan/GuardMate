import type { ComponentProps } from 'react'
import { act, fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import App from '../App'
import { request } from '../api'
import { atPg, dashboardFixture, deferred } from './fixtures'
import type { Dashboard } from '../types'

vi.mock('../api', () => ({ request: vi.fn() }))
const renders = vi.hoisted(() => ({
  mode: vi.fn(),
  preferences: vi.fn(),
  instruction: vi.fn(),
}))

// Instrument the same memoized section boundaries used by the real dashboard.
vi.mock('../components/DeliveryModeCard', async (importOriginal) => {
  const original =
    await importOriginal<typeof import('../components/DeliveryModeCard')>()
  const { memo } = await import('react')
  return {
    DeliveryModeCard: memo(
      (props: ComponentProps<typeof original.DeliveryModeCard>) => {
        renders.mode()
        return <original.DeliveryModeCard {...props} />
      },
    ),
  }
})
vi.mock('../components/PreferencesForm', async (importOriginal) => {
  const original =
    await importOriginal<typeof import('../components/PreferencesForm')>()
  const { memo } = await import('react')
  return {
    PreferencesForm: memo(
      (props: ComponentProps<typeof original.PreferencesForm>) => {
        renders.preferences()
        return <original.PreferencesForm {...props} />
      },
    ),
  }
})
vi.mock('../components/InstructionPreview', async (importOriginal) => {
  const original =
    await importOriginal<typeof import('../components/InstructionPreview')>()
  const { memo } = await import('react')
  return {
    InstructionPreview: memo(
      (props: ComponentProps<typeof original.InstructionPreview>) => {
        renders.instruction()
        return <original.InstructionPreview {...props} />
      },
    ),
  }
})

const api = vi.mocked(request)
async function mountDashboard() {
  await act(async () => {
    render(<App />)
  })
  expect((screen.getByLabelText('Your name') as HTMLInputElement).value).toBe(
    'Demo resident',
  )
  Object.values(renders).forEach((spy) => spy.mockClear())
}

beforeEach(() => {
  api.mockReset()
  api.mockResolvedValue(dashboardFixture())
})

describe('dashboard interaction isolation', () => {
  it('only blocks availability while saving; other sections do not re-render', async () => {
    const save = deferred<Dashboard>()
    api
      .mockResolvedValueOnce(dashboardFixture())
      .mockReturnValueOnce(save.promise)
    await mountDashboard()
    fireEvent.click(screen.getByRole('button', { name: 'At PG' }))
    expect(
      (
        screen.getByRole('button', {
          name: 'At PG',
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(true)
    expect(
      (
        screen.getByRole('button', {
          name: 'Enable delivery mode',
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(false)
    expect(
      (screen.getByLabelText('Your name') as HTMLInputElement).disabled,
    ).toBe(false)
    expect(renders.mode).not.toHaveBeenCalled()
    expect(renders.preferences).not.toHaveBeenCalled()
    await act(async () => {
      save.resolve(atPg())
      await save.promise
    })
    expect(screen.getByRole('heading', { name: 'At PG' })).toBeTruthy()
    expect(renders.mode).not.toHaveBeenCalled()
    expect(renders.preferences).not.toHaveBeenCalled()
    expect(renders.instruction).toHaveBeenCalledTimes(1)
  })

  it('keeps typing inside the preferences form and preserves the draft through a save elsewhere', async () => {
    await mountDashboard()
    const user = userEvent.setup()
    await user.clear(screen.getByLabelText('Your name'))
    await user.type(screen.getByLabelText('Your name'), 'Unsaved draft')
    expect(renders.mode).not.toHaveBeenCalled()
    expect(renders.instruction).not.toHaveBeenCalled()
    api.mockResolvedValueOnce(atPg())
    fireEvent.click(screen.getByRole('button', { name: 'At PG' }))
    await screen.findByRole('heading', { name: 'At PG' })
    expect((screen.getByLabelText('Your name') as HTMLInputElement).value).toBe(
      'Unsaved draft',
    )
  })

  it('renders notifications outside the page flow; dismissing them does not re-render cards', async () => {
    await mountDashboard()
    api.mockResolvedValueOnce(atPg())
    fireEvent.click(screen.getByRole('button', { name: 'At PG' }))
    const message = await screen.findByText(
      'Today’s availability is updated. Your routine resumes tomorrow.',
    )
    expect(message.closest('.app-shell')).toBeNull()
    expect(message.closest('.notification-region')).not.toBeNull()
    Object.values(renders).forEach((spy) => spy.mockClear())
    fireEvent.click(
      screen.getByRole('button', { name: 'Dismiss notification' }),
    )
    expect(
      screen.queryByText(
        'Today’s availability is updated. Your routine resumes tomorrow.',
      ),
    ).toBeNull()
    Object.values(renders).forEach((spy) => expect(spy).not.toHaveBeenCalled())
  })

  it('auto-dismisses a success toast without re-rendering the dashboard sections', async () => {
    vi.useFakeTimers()
    await mountDashboard()
    api.mockResolvedValueOnce(atPg())
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'At PG' }))
    })
    expect(
      screen.getByText(
        'Today’s availability is updated. Your routine resumes tomorrow.',
      ),
    ).toBeTruthy()
    Object.values(renders).forEach((spy) => spy.mockClear())
    await act(async () => {
      await vi.advanceTimersByTimeAsync(6000)
    })
    expect(
      screen.queryByText(
        'Today’s availability is updated. Your routine resumes tomorrow.',
      ),
    ).toBeNull()
    Object.values(renders).forEach((spy) => expect(spy).not.toHaveBeenCalled())
  })

  it('skips unchanged polling snapshots and retains edited form values', async () => {
    vi.useFakeTimers()
    await mountDashboard()
    fireEvent.change(screen.getByLabelText('Your name'), {
      target: { value: 'Still editing' },
    })
    api.mockResolvedValueOnce({
      ...dashboardFixture(),
      server_time: '2026-10-03T09:00:30Z',
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000)
    })
    expect(api).toHaveBeenCalledTimes(2)
    Object.values(renders).forEach((spy) => expect(spy).not.toHaveBeenCalled())
    expect((screen.getByLabelText('Your name') as HTMLInputElement).value).toBe(
      'Still editing',
    )
  })

  it('cannot roll back a new availability save with an older polling response', async () => {
    const poll = deferred<Dashboard>()
    vi.useFakeTimers()
    await mountDashboard()
    api.mockReturnValueOnce(poll.promise).mockResolvedValueOnce(atPg())
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000)
    })
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'At PG' }))
    })
    expect(screen.getByRole('heading', { name: 'At PG' })).toBeTruthy()
    await act(async () => {
      poll.resolve(dashboardFixture())
      await poll.promise
    })
    expect(screen.getByRole('heading', { name: 'At PG' })).toBeTruthy()
  })

  it('retains the saved availability after a failed update and unlocks its controls', async () => {
    await mountDashboard()
    api.mockRejectedValueOnce(new Error('Could not save the update.'))
    fireEvent.click(screen.getByRole('button', { name: 'At PG' }))
    await screen.findByRole('alert')
    expect(screen.getByRole('heading', { name: 'At office' })).toBeTruthy()
    expect(
      (
        screen.getByRole('button', {
          name: 'At PG',
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(false)
    expect(renders.mode).not.toHaveBeenCalled()
    expect(renders.preferences).not.toHaveBeenCalled()
  })

  it('isolates clipboard feedback to the instruction preview', async () => {
    await mountDashboard()
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: vi.fn().mockResolvedValue(undefined) },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Copy instruction' }))
    await screen.findByRole('button', { name: 'Copied' })
    expect(renders.mode).not.toHaveBeenCalled()
    expect(renders.preferences).not.toHaveBeenCalled()
  })

  it('queues writes from different cards rather than applying responses out of order', async () => {
    const availability = deferred<Dashboard>()
    const mode = atPg()
    mode.delivery_mode = { enabled: true, expires_at: '2026-10-03T12:00:00Z' }
    mode.context.delivery_mode_active = true
    api
      .mockResolvedValueOnce(dashboardFixture())
      .mockReturnValueOnce(availability.promise)
      .mockResolvedValueOnce(mode)
    await mountDashboard()
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'At PG' }))
    })
    fireEvent.click(
      screen.getByRole('button', { name: 'Enable delivery mode' }),
    )
    expect(api).toHaveBeenCalledTimes(2)
    await act(async () => {
      availability.resolve(atPg())
      await availability.promise
    })
    await screen.findByRole('heading', { name: 'Instructions enabled' })
    expect(api).toHaveBeenCalledTimes(3)
    expect(screen.getByRole('heading', { name: 'At PG' })).toBeTruthy()
  })
})
