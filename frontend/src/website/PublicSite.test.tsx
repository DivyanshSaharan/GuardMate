import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { PublicSite } from './PublicSite'

afterEach(() => vi.unstubAllGlobals())

describe('public website', () => {
  it('shows a public example without accessing resident APIs or paid inference', async () => {
    const fetcher = vi.fn(async (path: string) => {
      if (path === '/api/cloud/config')
        return {
          ok: true,
          json: async () => ({
            oauth_ready: false,
            download_ready: false,
            budget_microdollars: 1_000_000,
            signin_url: '/api/cloud/auth/google/start',
          }),
        }
      return { ok: false, status: 401 }
    })
    vi.stubGlobal('fetch', fetcher)
    render(<PublicSite />)
    await screen.findByText(/Sign-in is being configured/)
    fireEvent.click(
      screen.getByRole('button', { name: 'Open conversation example' }),
    )
    expect(screen.getByText(/Resident approval required/)).toBeTruthy()
    expect(fetcher.mock.calls.map(([path]) => path).sort()).toEqual([
      '/api/cloud/config',
      '/api/cloud/me',
    ])
    expect(
      screen.queryByRole('link', { name: 'Download for Windows' }),
    ).toBeNull()
  })

  it('offers the authenticated download and shows the existing account balance', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (path: string) => ({
        ok: true,
        json: async () =>
          path === '/api/cloud/config'
            ? {
                oauth_ready: true,
                download_ready: true,
                budget_microdollars: 1_000_000,
              }
            : {
                account_id: 'account-a',
                budget_microdollars: 1_000_000,
                reserved_microdollars: 300_000,
                remaining_microdollars: 700_000,
              },
      })),
    )
    render(<PublicSite />)
    await waitFor(() => expect(screen.getByText('$0.70')).toBeTruthy())
    const link = screen.getByRole('link', { name: 'Download for Windows' })
    expect(link.getAttribute('href')).toBe('/api/cloud/download/windows')
    expect(
      screen.queryByRole('link', { name: 'Continue with Google' }),
    ).toBeNull()
  })
})
