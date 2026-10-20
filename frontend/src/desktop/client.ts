import type { Conversation } from '../agent/types'

export interface DesktopStatus {
  desktop: boolean
  windows: boolean
  phone_link_installed: boolean
  phone_link_message: string
  setup_complete: boolean
  delivery_mode_active: boolean
  model_configured: boolean
  stt_ready: boolean
  tts_ready: boolean
  physical_call_state_verified: false
}

export interface Endpoint {
  id: number
  name: string
  direction: 'input' | 'output'
  host_api: string
}

export interface CallState {
  busy: boolean
  stopping: boolean
  stopped: boolean
  uncertain: boolean
  draft: string | null
  conversation: Conversation | null
  model_attempts: number
  max_turns: number
  events: Array<{ event: string }>
  error: string
  physical_call_ended: false
}

export class DesktopError extends Error {
  constructor(
    message: string,
    public readonly status: number,
  ) {
    super(message)
  }
}

export async function desktopRequest<T>(
  path: string,
  body?: unknown,
): Promise<T> {
  const response = await fetch(`/api/desktop${path}`, {
    method: body === undefined ? 'GET' : 'POST',
    credentials: 'same-origin',
    cache: 'no-store',
    ...(body === undefined
      ? {}
      : {
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        }),
  })
  const value = await response.json().catch(() => ({}))
  if (!response.ok) {
    throw new DesktopError(
      typeof value.detail === 'string'
        ? value.detail
        : 'The desktop action could not be completed. Check the local app.',
      response.status,
    )
  }
  return value as T
}
