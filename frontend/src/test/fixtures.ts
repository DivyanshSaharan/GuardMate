import type { Dashboard } from '../types'

export function dashboardFixture(): Dashboard {
  return {
    profile: {
      resident_name: 'Demo resident',
      pg_name: 'Demo PG',
      guard_location: 'the guard room beside the entrance',
      guard_directions: 'Use the pedestrian gate.',
      office_days: [0, 1, 2, 3, 4],
      office_start: '09:00:00',
      office_end: '19:00:00',
      outside_office: 'ask_me',
      weekend: 'ask_me',
      timezone: 'Asia/Kolkata',
    },
    delivery_mode: { enabled: false, expires_at: null },
    context: {
      availability: 'at_office',
      availability_source: 'today',
      availability_explanation:
        'Your override for today. Tomorrow, your saved routine takes over.',
      today_override: 'at_office',
      setup_complete: true,
      delivery_mode_active: false,
      delivery_mode_expired: false,
      instruction:
        'Please hand the prepaid parcel to security at the guard room beside the entrance.',
      restrictions: [
        'Prepaid parcels only',
        'OTP, signature or payment requests need your help',
      ],
      local_date: '2026-10-03',
    },
    server_time: '2026-10-03T09:00:00Z',
    voice_connected: false,
  }
}

export function atPg() {
  const data = dashboardFixture()
  data.context.availability = 'at_pg'
  data.context.today_override = 'at_pg'
  data.context.instruction =
    "I'm at the PG. Please call me so I can receive the parcel personally."
  return data
}

export function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: Error) => void
  const promise = new Promise<T>((accept, decline) => {
    resolve = accept
    reject = decline
  })
  return { promise, resolve, reject }
}
