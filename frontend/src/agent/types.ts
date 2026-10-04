export type ModelTargetKind = 'base' | 'tuned' | 'unspecified'

export interface ModelIdentity {
  model: string
  provider: string
  target_kind?: ModelTargetKind
  sampler_checkpoint?: string | null
  checkpoint_verified?: boolean
}

export interface ModelStatus extends ModelIdentity {
  configured: boolean
  message: string
  reserved_usd: number
  budget_usd: number
  voice_connected: false
}

export interface AgentEvent {
  action: string
  detail: string
  at: string
  latency_ms: number | null
  model_action?: string | null
  model_question?: string | null
  model_observation?: Record<string, boolean | null> | null
  model_identity?: ModelIdentity | null
  model_result?: 'plan_returned' | 'unavailable' | null
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
  events: AgentEvent[]
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
