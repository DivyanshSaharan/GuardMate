import type { Availability } from '../types'

export const availabilityChoices: {
  value: Availability
  label: string
  icon: string
}[] = [
  { value: 'at_office', label: 'At office', icon: 'office' },
  { value: 'at_pg', label: 'At PG', icon: 'home' },
  { value: 'ask_me', label: 'Ask me first', icon: 'phone' },
]
export const officeDays = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

const dateFormatter = new Intl.DateTimeFormat('en-IN', {
  weekday: 'long',
  month: 'short',
  day: 'numeric',
  timeZone: 'Asia/Kolkata',
})
const windowFormatter = new Intl.DateTimeFormat('en-IN', {
  dateStyle: 'medium',
  timeStyle: 'short',
  timeZone: 'Asia/Kolkata',
})

export function formatDate(localDate?: string) {
  return dateFormatter.format(
    localDate ? new Date(`${localDate}T12:00:00+05:30`) : new Date(),
  )
}

export function formatWindow(instant: string) {
  return windowFormatter.format(new Date(instant))
}

export function defaultEndTime() {
  const instant = new Date(Date.now() + 2 * 60 * 60 * 1000)
  return new Date(instant.getTime() + 330 * 60 * 1000)
    .toISOString()
    .slice(0, 16)
}
