import type { Conversation, ConversationSummary } from './types'

export function summarize(session: Conversation): ConversationSummary {
  return {
    id: session.id,
    courier_label: session.courier_label ?? '',
    status: session.status,
    created_at: session.created_at,
    turn_count: session.turn_count,
    revision: session.revision,
    has_pending_approval: session.approval?.status === 'pending',
  }
}

export function mergeSummaries(
  previous: ConversationSummary[],
  incoming: ConversationSummary[],
): ConversationSummary[] {
  const byId = new Map(previous.map((session) => [session.id, session]))
  for (const next of incoming) {
    const saved = byId.get(next.id)
    if (
      !saved ||
      (next.revision >= saved.revision &&
        (saved.courier_label !== next.courier_label ||
          saved.status !== next.status ||
          saved.created_at !== next.created_at ||
          saved.turn_count !== next.turn_count ||
          saved.revision !== next.revision ||
          saved.has_pending_approval !== next.has_pending_approval))
    ) {
      byId.set(next.id, next)
    }
  }
  const sorted = Array.from(byId.values()).sort(
    (a, b) =>
      b.created_at.localeCompare(a.created_at) || b.id.localeCompare(a.id),
  )
  if (
    previous.length === sorted.length &&
    previous.every((session, index) => session === sorted[index])
  ) {
    return previous
  }
  return sorted
}

export function sessionName(session: { id: string; courier_label?: string }) {
  return (
    session.courier_label?.trim() ||
    `Earlier role-play · ${session.id.slice(0, 8)}`
  )
}
