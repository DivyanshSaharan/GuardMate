import { useCallback, useEffect, useRef, useState } from 'react'
import { request } from '../api'
import { useNotifications } from '../components/Notifications'
import { reconcileDashboard } from '../lib/dashboard'
import type { Availability, Dashboard, ResidentProfile } from '../types'

type Section = 'availability' | 'mode' | 'profile'

export function useDashboard() {
  const [dashboard, setDashboard] = useState<Dashboard | null>(null)
  const [connected, setConnected] = useState(false)
  const [pending, setPending] = useState({
    availability: false,
    mode: false,
    profile: false,
  })
  const { notify, dismiss } = useNotifications()
  const mounted = useRef(false)
  const revision = useRef(0)
  const pendingWrites = useRef(0)
  const lockedSections = useRef(new Set<Section>())
  const writeQueue = useRef<Promise<void>>(Promise.resolve())
  const connectionNotice = useRef<number | null>(null)

  useEffect(() => {
    mounted.current = true
    let cancelled = false
    let polling = false
    const controller = new AbortController()
    async function refresh() {
      if (polling || pendingWrites.current > 0) return
      polling = true
      const startedRevision = revision.current
      try {
        const data = await request<Dashboard>(
          '/dashboard',
          undefined,
          controller.signal,
        )
        if (
          cancelled ||
          pendingWrites.current > 0 ||
          startedRevision !== revision.current
        )
          return
        setDashboard((previous) => reconcileDashboard(previous, data))
        setConnected(true)
        if (connectionNotice.current !== null) {
          dismiss(connectionNotice.current)
          connectionNotice.current = null
        }
      } catch (problem) {
        if (
          cancelled ||
          pendingWrites.current > 0 ||
          startedRevision !== revision.current
        )
          return
        setConnected(false)
        if (connectionNotice.current === null) {
          connectionNotice.current = notify(
            problem instanceof Error
              ? problem.message
              : 'Unable to reach GuardMate.',
            'error',
          )
        }
      } finally {
        polling = false
      }
    }
    void refresh()
    const interval = window.setInterval(() => void refresh(), 30_000)
    return () => {
      cancelled = true
      mounted.current = false
      controller.abort()
      window.clearInterval(interval)
    }
  }, [notify, dismiss])

  const mutate = useCallback(
    async (
      section: Section,
      path: string,
      payload: unknown,
      message: string,
    ) => {
      if (lockedSections.current.has(section)) return null
      lockedSections.current.add(section)
      pendingWrites.current++
      revision.current++
      setPending((current) => ({ ...current, [section]: true }))
      // Serialize writes while keeping other sections interactive. A poll started
      // before a write cannot overwrite its newer response.
      const operation = writeQueue.current.then(async () => {
        const data = await request<Dashboard>(path, payload)
        if (mounted.current) {
          setDashboard((previous) => reconcileDashboard(previous, data))
          setConnected(true)
          notify(message)
        }
        return data
      })
      writeQueue.current = operation.then(
        () => undefined,
        () => undefined,
      )
      try {
        return await operation
      } catch (problem) {
        if (mounted.current)
          notify(
            problem instanceof Error
              ? problem.message
              : 'Unable to save your changes.',
            'error',
          )
        return null
      } finally {
        pendingWrites.current--
        lockedSections.current.delete(section)
        if (mounted.current)
          setPending((current) => ({ ...current, [section]: false }))
      }
    },
    [notify],
  )

  const setAvailability = useCallback(
    (status: Availability | null) =>
      mutate(
        'availability',
        '/availability',
        { status },
        status === null
          ? 'Your saved routine is back in effect.'
          : 'Today’s availability is updated. Your routine resumes tomorrow.',
      ),
    [mutate],
  )
  const setDeliveryMode = useCallback(
    (enabled: boolean, expiresAt?: string) =>
      mutate(
        'mode',
        '/delivery-mode',
        enabled ? { enabled, expires_at: expiresAt } : { enabled },
        enabled
          ? 'Delivery window saved. Voice handling is not connected yet.'
          : 'Delivery mode is off.',
      ),
    [mutate],
  )
  const saveProfile = useCallback(
    async (profile: ResidentProfile) => {
      const result = await mutate(
        'profile',
        '/profile',
        profile,
        'Your instructions and routine are saved.',
      )
      return result?.profile ?? null
    },
    [mutate],
  )

  return {
    dashboard,
    connected,
    pending,
    setAvailability,
    setDeliveryMode,
    saveProfile,
  }
}
