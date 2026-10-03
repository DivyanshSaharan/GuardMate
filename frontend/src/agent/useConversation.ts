import { useCallback, useEffect, useRef, useState } from 'react'
import { request } from '../api'
import type { Conversation, ModelStatus, ResidentDecision } from './types'

const sessionKey = 'guardmate-role-play'

export function useConversation() {
  const [model, setModel] = useState<ModelStatus | null>(null)
  const [conversation, setConversation] = useState<Conversation | null>(null)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const locked = useRef(false)
  const revision = useRef(0)

  const apply = useCallback((next: Conversation) => {
    setConversation((previous) => {
      if (previous?.id === next.id && previous.revision >= next.revision) {
        return previous
      }
      return next
    })
    try {
      localStorage.setItem(sessionKey, next.id)
    } catch {
      // Browser storage is optional; the session remains on the local backend.
    }
  }, [])

  const checkModel = useCallback(async () => {
    try {
      setModel(await request<ModelStatus>('/agent/status'))
    } catch (cause) {
      setError(
        cause instanceof Error ? cause.message : 'Could not read model status.',
      )
    }
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    let disposed = false
    void request<ModelStatus>('/agent/status', undefined, controller.signal)
      .then((status) => {
        if (!disposed) setModel(status)
      })
      .catch((cause) => {
        if (!disposed) setError(cause.message)
      })
    try {
      const id = localStorage.getItem(sessionKey)
      if (id) {
        void request<Conversation>(
          `/conversations/${id}`,
          undefined,
          controller.signal,
        )
          .then((next) => {
            if (!disposed) apply(next)
          })
          .catch(() => {
            if (!disposed) localStorage.removeItem(sessionKey)
          })
      }
    } catch {
      // Private browsing may disable storage.
    }
    return () => {
      disposed = true
      controller.abort()
    }
  }, [apply])

  useEffect(() => {
    if (
      !conversation?.approval ||
      !['pending', 'approved'].includes(conversation.approval.status)
    ) {
      return
    }
    const controller = new AbortController()
    const id = conversation.id
    let disposed = false
    const timer = window.setInterval(() => {
      if (locked.current) return
      const operation = revision.current
      void request<Conversation>(
        `/conversations/${id}`,
        undefined,
        controller.signal,
      )
        .then((next) => {
          if (!disposed && operation === revision.current) apply(next)
        })
        .catch((cause) => {
          if (!disposed) setError(cause.message)
        })
    }, 5000)
    return () => {
      disposed = true
      controller.abort()
      window.clearInterval(timer)
    }
  }, [conversation?.id, conversation?.approval?.status, apply])

  const run = useCallback(
    async (operation: () => Promise<Conversation>): Promise<boolean> => {
      if (locked.current) return false
      locked.current = true
      revision.current += 1
      setPending(true)
      setError('')
      try {
        apply(await operation())
        void checkModel()
        return true
      } catch (cause) {
        setError(
          cause instanceof Error
            ? cause.message
            : 'Conversation could not be updated.',
        )
        return false
      } finally {
        locked.current = false
        setPending(false)
      }
    },
    [apply, checkModel],
  )

  const start = useCallback(
    () =>
      run(() => request<Conversation>('/conversations', {}, undefined, 'POST')),
    [run],
  )
  const send = useCallback(
    (text: string) => {
      if (!conversation) return Promise.resolve(false)
      return run(() =>
        request<Conversation>(
          `/conversations/${conversation.id}/turns`,
          {
            text,
            revision: conversation.revision,
          },
          undefined,
          'POST',
        ),
      )
    },
    [conversation, run],
  )
  const decide = useCallback(
    (decision: ResidentDecision) => {
      if (!conversation) return Promise.resolve(false)
      return run(() =>
        request<Conversation>(`/conversations/${conversation.id}/resident`, {
          decision,
          approval_id: conversation.approval?.id ?? null,
          revision: conversation.revision,
        }),
      )
    },
    [conversation, run],
  )
  const refresh = useCallback(() => {
    if (!conversation) return Promise.resolve(false)
    return run(() => request<Conversation>(`/conversations/${conversation.id}`))
  }, [conversation, run])

  return {
    model,
    conversation,
    pending,
    error,
    start,
    send,
    decide,
    refresh,
    checkModel,
  }
}
