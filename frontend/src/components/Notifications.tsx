import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import type { ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { Icon } from './Icon'

type Notification = { id: number; message: string; kind: 'success' | 'error' }
type NotificationActions = {
  notify: (message: string, kind?: Notification['kind']) => number
  dismiss: (id: number) => void
}
const NotificationContext = createContext<NotificationActions | null>(null)

export function NotificationProvider({ children }: { children: ReactNode }) {
  const [notification, setNotification] = useState<Notification | null>(null)
  const nextId = useRef(0)
  const notify = useCallback(
    (message: string, kind: Notification['kind'] = 'success') => {
      const id = ++nextId.current
      setNotification({ id, message, kind })
      return id
    },
    [],
  )
  const dismiss = useCallback((id: number) => {
    setNotification((current) => (current?.id === id ? null : current))
  }, [])
  const actions = useMemo(() => ({ notify, dismiss }), [notify, dismiss])

  useEffect(() => {
    if (!notification || notification.kind === 'error') return
    const timer = window.setTimeout(() => dismiss(notification.id), 6000)
    return () => window.clearTimeout(timer)
  }, [notification, dismiss])

  return (
    <NotificationContext value={actions}>
      {children}
      {createPortal(
        <div
          className="notification-region"
          aria-live="polite"
          aria-atomic="true"
        >
          {notification && (
            <div
              key={notification.id}
              className={`feedback toast ${notification.kind}`}
              role={notification.kind === 'error' ? 'alert' : 'status'}
            >
              <Icon
                name={notification.kind === 'error' ? 'info' : 'check'}
                size={18}
              />
              <span>{notification.message}</span>
              <button
                type="button"
                onClick={() => dismiss(notification.id)}
                aria-label="Dismiss notification"
              >
                ×
              </button>
            </div>
          )}
        </div>,
        document.body,
      )}
    </NotificationContext>
  )
}

export function useNotifications() {
  const actions = useContext(NotificationContext)
  if (!actions)
    throw new Error('Notifications must be used within NotificationProvider.')
  return actions
}
