export type Availability = 'at_office' | 'at_pg' | 'ask_me'

export interface ResidentProfile {
  resident_name: string
  pg_name: string
  guard_location: string
  guard_directions: string
  office_days: number[]
  office_start: string
  office_end: string
  outside_office: Availability
  weekend: Availability
  timezone: 'Asia/Kolkata'
}

export interface Dashboard {
  profile: ResidentProfile
  delivery_mode: { enabled: boolean; expires_at: string | null }
  context: {
    availability: Availability
    availability_source: 'today' | 'office_hours' | 'outside_office' | 'weekend'
    availability_explanation: string
    today_override: Availability | null
    setup_complete: boolean
    delivery_mode_active: boolean
    delivery_mode_expired: boolean
    instruction: string
    restrictions: string[]
    local_date: string
  }
  server_time: string
  voice_connected: false
}

export const emptyProfile: ResidentProfile = {
  resident_name: '',
  pg_name: '',
  guard_location: '',
  guard_directions: '',
  office_days: [0, 1, 2, 3, 4],
  office_start: '09:00',
  office_end: '19:00',
  outside_office: 'ask_me',
  weekend: 'ask_me',
  timezone: 'Asia/Kolkata',
}
