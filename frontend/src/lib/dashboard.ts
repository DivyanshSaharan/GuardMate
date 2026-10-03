import type { Dashboard, ResidentProfile } from '../types'

function sameArray(left: readonly unknown[], right: readonly unknown[]) {
  return (
    left.length === right.length &&
    left.every((value, index) => value === right[index])
  )
}

function sameFields<T extends object>(left: T, right: T) {
  return (Object.keys(right) as (keyof T)[]).every((key) => {
    const before = left[key]
    const after = right[key]
    return Array.isArray(before) && Array.isArray(after)
      ? sameArray(before, after)
      : Object.is(before, after)
  })
}

export function sameProfile(left: ResidentProfile, right: ResidentProfile) {
  return sameFields(left, right)
}

// The heartbeat timestamp is not display state. Preserve unchanged references so
// polling and unrelated mutations do not invalidate section memoization.
export function reconcileDashboard(
  previous: Dashboard | null,
  incoming: Dashboard,
): Dashboard {
  if (!previous) return incoming
  const profile = sameProfile(previous.profile, incoming.profile)
    ? previous.profile
    : incoming.profile
  const deliveryMode = sameFields(
    previous.delivery_mode,
    incoming.delivery_mode,
  )
    ? previous.delivery_mode
    : incoming.delivery_mode
  const restrictions = sameArray(
    previous.context.restrictions,
    incoming.context.restrictions,
  )
    ? previous.context.restrictions
    : incoming.context.restrictions
  const context = sameFields(previous.context, incoming.context)
    ? previous.context
    : { ...incoming.context, restrictions }
  if (
    profile === previous.profile &&
    deliveryMode === previous.delivery_mode &&
    context === previous.context &&
    incoming.voice_connected === previous.voice_connected
  ) {
    return previous
  }
  return { ...incoming, profile, delivery_mode: deliveryMode, context }
}
