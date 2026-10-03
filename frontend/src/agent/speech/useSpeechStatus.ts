import { useEffect, useState } from 'react'
import { speechStatus, type SpeechStatus } from './api'

export function useSpeechStatus() {
  const [status, setStatus] = useState<SpeechStatus | null>(null)
  const [error, setError] = useState('')
  useEffect(() => {
    const controller = new AbortController()
    void speechStatus(controller.signal)
      .then((value) => {
        if (!controller.signal.aborted) setStatus(value)
      })
      .catch(() => {
        if (!controller.signal.aborted) {
          setError('Local voice is unavailable. Text conversation still works.')
        }
      })
    return () => controller.abort()
  }, [])
  return { status, error }
}
