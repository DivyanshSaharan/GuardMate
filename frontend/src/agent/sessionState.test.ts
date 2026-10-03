import { describe, expect, it } from 'vitest'
import { mergeSummaries, sessionName } from './sessionState'
import type { ConversationSummary } from './types'

const a: ConversationSummary = {
  id: 'session-a',
  courier_label: 'Courier A',
  status: 'active',
  created_at: '2026-10-03T06:00:00Z',
  turn_count: 0,
  revision: 0,
  has_pending_approval: false,
}

describe('session summary reconciliation', () => {
  it('preserves references for unchanged list polls', () => {
    const previous = [a]
    expect(mergeSummaries(previous, [{ ...a }])).toBe(previous)
  })
  it('cannot overwrite newer revisions with a stale list response', () => {
    const newer = { ...a, revision: 2, status: 'ended' as const }
    const previous = [newer]
    expect(mergeSummaries(previous, [a])).toBe(previous)
  })
  it('keeps a newly created session when an older list request finishes', () => {
    const b = {
      ...a,
      id: 'session-b',
      courier_label: 'Courier B',
      created_at: '2026-10-03T06:01:00Z',
    }
    expect(mergeSummaries([b, a], [a])).toEqual([b, a])
  })
  it('updates only the changed summary and keeps same labels separate', () => {
    const b = { ...a, id: 'session-b' }
    const next = mergeSummaries(
      [b, a],
      [{ ...a, revision: 1, has_pending_approval: true }],
    )
    expect(next.length).toBe(2)
    expect(next[0]).toBe(b)
    expect(next[1].has_pending_approval).toBe(true)
  })
  it('gives old unlabeled role-plays a stable display name without inventing a caller', () => {
    expect(sessionName({ id: '12345678-abcd', courier_label: '' })).toBe(
      'Earlier role-play · 12345678',
    )
  })
})
