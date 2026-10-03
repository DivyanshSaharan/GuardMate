export interface ModelStatus {
  configured: boolean
  model: string
  provider: string
  message: string
  reserved_usd: number
  budget_usd: number
  voice_connected: false
}

export interface Conversation {
  id: string
  courier_label?: string
  status: 'active' | 'awaiting_approval' | 'needs_resident' | 'ended'
  facts: {
    prepaid: boolean | null
    guard_available: boolean | null
    needs_otp: boolean
    needs_signature: boolean
    expensive: boolean
  }
  messages: Array<{
    role: 'courier' | 'assistant' | 'resident'
    content: string
    at: string
  }>
  events: Array<{
    action: string
    detail: string
    at: string
    latency_ms: number | null
    model_action?: string | null
    model_question?: string | null
    model_observation?: Record<string, boolean | null> | null
  }>
  approval: {
    id: string
    location: string
    reason: string
    status: 'pending' | 'approved' | 'declined' | 'expired' | 'consumed'
    expires_at: string
  } | null
  authorized_location: string | null
  courier_reported_outcome: string | null
  created_at: string
  turn_count: number
  revision: number
}

export type ResidentDecision = 'approve' | 'decline' | 'takeover' | 'end'

export interface ConversationSummary {
  id: string
  courier_label: string
  status: Conversation['status']
  created_at: string
  turn_count: number
  revision: number
  has_pending_approval: boolean
}
