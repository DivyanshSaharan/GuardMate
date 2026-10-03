import { useCallback, useEffect, useRef, useState } from 'react'
import { request } from '../api'
import { mergeSummaries, summarize } from './sessionState'
import type {
  Conversation,
  ConversationSummary,
  ModelStatus,
  ResidentDecision,
} from './types'

const sessionKey = 'guardmate-role-play'

export function useConversation() {
  const [model, setModel] = useState<ModelStatus | null>(null)
  const [conversation, setConversation] = useState<Conversation | null>(null)
  const [sessions, setSessions] = useState<ConversationSummary[]>([])
  const [initializing, setInitializing] = useState(true)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const locked = useRef(false)
  const revision = useRef(0)
  const mounted = useRef(false)
  const selectedId = useRef<string | null>(null)
  const cache = useRef(new Map<string, Conversation>())
  const modelRevision = useRef(0)

  const apply = useCallback((next: Conversation) => {
    const cached = cache.current.get(next.id)
    const current = cached && cached.revision >= next.revision ? cached : next
    cache.current.set(next.id, current)
    setSessions((previous) => mergeSummaries(previous, [summarize(current)]))
    if (selectedId.current !== next.id) return current
    setConversation((previous) => {
      if (
        previous?.id === current.id &&
        previous.revision >= current.revision
      ) {
        return previous
      }
      return current
    })
    return current
  }, [])

  const choose = useCallback((session: Conversation) => {
    selectedId.current = session.id
    setConversation(session)
    try {
      localStorage.setItem(sessionKey, session.id)
    } catch {
      // Browser storage is optional; the session remains on the local backend.
    }
  }, [])

  const checkModel = useCallback(async () => {
    const operation = ++modelRevision.current
    try {
      const status = await request<ModelStatus>('/agent/status')
      if (mounted.current && operation === modelRevision.current)
        setModel(status)
    } catch (cause) {
      if (mounted.current && operation === modelRevision.current) {
        setError(
          cause instanceof Error
            ? cause.message
            : 'Could not read model status.',
        )
      }
    }
  }, [])

  const loadSessions = useCallback(async (signal?: AbortSignal) => {
    const next = await request<ConversationSummary[]>(
      '/conversations',
      undefined,
      signal,
    )
    if (mounted.current && !signal?.aborted) {
      setSessions((previous) => mergeSummaries(previous, next))
    }
  }, [])

  useEffect(() => {
    mounted.current = true
    const controller = new AbortController()
    let disposed = false
    const operation = revision.current
    const tasks: Promise<unknown>[] = [
      checkModel(),
      loadSessions(controller.signal),
    ]
    try {
      const id = localStorage.getItem(sessionKey)
      if (id) {
        tasks.push(
          request<Conversation>(
            `/conversations/${id}`,
            undefined,
            controller.signal,
          ).then((next) => {
            if (!disposed && operation === revision.current) choose(apply(next))
          }),
        )
      }
    } catch {
      // Private browsing may disable storage.
    }
    void Promise.allSettled(tasks).then((results) => {
      if (disposed) return
      const failed = results.find((result) => result.status === 'rejected')
      if (failed?.status === 'rejected') {
        setError(
          failed.reason instanceof Error
            ? failed.reason.message
            : 'Could not load saved role-plays.',
        )
      }
      setInitializing(false)
    })
    return () => {
      disposed = true
      mounted.current = false
      revision.current += 1
      controller.abort()
    }
  }, [apply, choose, checkModel, loadSessions])

  const backgroundApproval = sessions.some(
    (session) => session.has_pending_approval,
  )
  const selectedApproval =
    !!conversation?.approval &&
    ['pending', 'approved'].includes(conversation.approval.status)
  useEffect(() => {
    if (!backgroundApproval && !selectedApproval) return
    const controller = new AbortController()
    const id = conversation?.id
    let disposed = false
    const timer = window.setInterval(() => {
      if (locked.current) return
      const operation = revision.current
      void loadSessions(controller.signal).catch((cause) => {
        if (!disposed) setError(cause.message)
      })
      if (id && selectedApproval) {
        void request<Conversation>(
          `/conversations/${id}`,
          undefined,
          controller.signal,
        )
          .then((next) => {
            if (
              !disposed &&
              operation === revision.current &&
              selectedId.current === id
            )
              apply(next)
          })
          .catch((cause) => {
            if (!disposed) setError(cause.message)
          })
      }
    }, 5000)
    return () => {
      disposed = true
      controller.abort()
      window.clearInterval(timer)
    }
  }, [
    conversation?.id,
    backgroundApproval,
    selectedApproval,
    apply,
    loadSessions,
  ])

  const run = useCallback(
    async (
      operation: () => Promise<Conversation>,
      targetId?: string,
      select = false,
    ): Promise<boolean> => {
      if (locked.current || initializing) return false
      locked.current = true
      const command = ++revision.current
      setPending(true)
      setError('')
      try {
        const next = await operation()
        if (!mounted.current || command !== revision.current) return false
        if (targetId && next.id !== targetId)
          throw new Error(
            'The reply belongs to a different role-play. Refresh status.',
          )
        const current = apply(next)
        if (select) choose(current)
        void checkModel()
        void loadSessions().catch((cause) => {
          if (mounted.current) setError(cause.message)
        })
        return true
      } catch (cause) {
        if (mounted.current)
          setError(
            cause instanceof Error
              ? cause.message
              : 'Conversation could not be updated.',
          )
        return false
      } finally {
        locked.current = false
        if (mounted.current) setPending(false)
      }
    },
    [apply, choose, checkModel, loadSessions, initializing],
  )

  const start = useCallback(
    (courierLabel: string) =>
      run(
        () =>
          request<Conversation>(
            '/conversations',
            { courier_label: courierLabel },
            undefined,
            'POST',
          ),
        undefined,
        true,
      ),
    [run],
  )
  const selectSession = useCallback(
    (id: string) => {
      if (!id || id === selectedId.current) return Promise.resolve(false)
      return run(() => request<Conversation>(`/conversations/${id}`), id, true)
    },
    [run],
  )
  const send = useCallback(
    (text: string) => {
      if (!conversation) return Promise.resolve(false)
      return run(
        () =>
          request<Conversation>(
            `/conversations/${conversation.id}/turns`,
            {
              text,
              revision: conversation.revision,
            },
            undefined,
            'POST',
          ),
        conversation.id,
      )
    },
    [conversation, run],
  )
  const decide = useCallback(
    (decision: ResidentDecision) => {
      if (!conversation) return Promise.resolve(false)
      return run(
        () =>
          request<Conversation>(`/conversations/${conversation.id}/resident`, {
            decision,
            approval_id: conversation.approval?.id ?? null,
            revision: conversation.revision,
          }),
        conversation.id,
      )
    },
    [conversation, run],
  )
  const refresh = useCallback(() => {
    if (!conversation)
      return Promise.all([checkModel(), loadSessions()]).catch((cause) => {
        if (mounted.current) setError(cause.message)
      })
    return run(
      () => request<Conversation>(`/conversations/${conversation.id}`),
      conversation.id,
    )
  }, [conversation, run, checkModel, loadSessions])

  return {
    model,
    conversation,
    sessions,
    initializing,
    pending,
    error,
    start,
    selectSession,
    send,
    decide,
    refresh,
    checkModel,
  }
}
