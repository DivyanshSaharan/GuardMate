import { describe, expect, it } from 'vitest'
import { atPg, dashboardFixture } from '../test/fixtures'
import { reconcileDashboard } from './dashboard'

describe('dashboard reconciliation', () => {
  it('accepts the initial snapshot', () => {
    const incoming = dashboardFixture()
    expect(reconcileDashboard(null, incoming)).toBe(incoming)
  })
  it('ignores heartbeat-only changes without invalidating references', () => {
    const previous = dashboardFixture()
    const incoming = {
      ...dashboardFixture(),
      server_time: '2026-10-03T09:00:30Z',
    }
    expect(reconcileDashboard(previous, incoming)).toBe(previous)
  })
  it('retains unrelated sections when availability changes', () => {
    const previous = dashboardFixture()
    const next = reconcileDashboard(previous, atPg())
    expect(next).not.toBe(previous)
    expect(next.profile).toBe(previous.profile)
    expect(next.delivery_mode).toBe(previous.delivery_mode)
    expect(next.context.restrictions).toBe(previous.context.restrictions)
    expect(next.context.availability).toBe('at_pg')
  })
  it('does not swallow expiry or local-midnight changes', () => {
    const previous = dashboardFixture()
    const incoming = dashboardFixture()
    incoming.context.delivery_mode_expired = true
    incoming.context.local_date = '2026-10-04'
    const next = reconcileDashboard(previous, incoming)
    expect(next.context).not.toBe(previous.context)
    expect(next.context.local_date).toBe('2026-10-04')
  })
  it('updates the profile when an office day changes', () => {
    const previous = dashboardFixture()
    const incoming = dashboardFixture()
    incoming.profile.office_days = [0, 1, 2, 3]
    expect(reconcileDashboard(previous, incoming).profile).toBe(
      incoming.profile,
    )
  })
})
