import { useCallback, useEffect, useRef, useState } from 'react'
import { replyAudio } from './api'

export function ReplyPlayback({
  sessionId,
  revision,
  messageIndex,
  blocked,
  ready,
  onBusyChange,
  sessionStatus,
}: {
  sessionId: string
  revision: number
  messageIndex: number | null
  blocked: boolean
  ready: boolean
  onBusyChange?: (busy: boolean) => void
  sessionStatus?: string
}) {
  const [phase, setPhase] = useState<'idle' | 'loading' | 'playing'>('idle')
  const [error, setError] = useState('')
  const generation = useRef(0)
  const controller = useRef<AbortController | null>(null)
  const audio = useRef<HTMLAudioElement | null>(null)
  const objectUrl = useRef<string | null>(null)
  const busy = useRef(false)
  const release = useCallback(() => {
    controller.current?.abort()
    controller.current = null
    if (audio.current) {
      audio.current.onended = null
      audio.current.onerror = null
      audio.current.pause()
      audio.current.src = ''
      audio.current = null
    }
    if (objectUrl.current) URL.revokeObjectURL(objectUrl.current)
    objectUrl.current = null
    busy.current = false
  }, [])
  const stop = useCallback(() => {
    generation.current++
    release()
    setPhase('idle')
    setError('')
  }, [release])
  useEffect(() => {
    stop()
    return () => {
      generation.current++
      release()
    }
  }, [
    sessionId,
    sessionStatus,
    revision,
    messageIndex,
    blocked,
    ready,
    stop,
    release,
  ])
  useEffect(() => {
    onBusyChange?.(phase !== 'idle')
  }, [phase, onBusyChange])

  async function play() {
    if (blocked || !ready || messageIndex === null || busy.current) return
    stop()
    busy.current = true
    const version = generation.current
    const request = new AbortController()
    controller.current = request
    setPhase('loading')
    try {
      const wav = await replyAudio(
        sessionId,
        revision,
        messageIndex,
        request.signal,
      )
      if (version !== generation.current || request.signal.aborted) return
      controller.current = null
      const url = URL.createObjectURL(wav)
      objectUrl.current = url
      const player = new Audio(url)
      audio.current = player
      player.onended = () => {
        if (version === generation.current) stop()
      }
      player.onerror = () => {
        if (version !== generation.current) return
        generation.current++
        release()
        setPhase('idle')
        setError(
          'Local reply audio could not be played. You can still read the reply.',
        )
      }
      await player.play()
      if (version !== generation.current || audio.current !== player) return
      setPhase('playing')
    } catch (failure) {
      if (version !== generation.current || request.signal.aborted) return
      release()
      setPhase('idle')
      setError(
        failure instanceof Error
          ? failure.message
          : 'Local reply audio is unavailable. You can still read the reply.',
      )
    }
  }
  return (
    <div className="reply-playback">
      <div className="voice-actions">
        <button
          type="button"
          className="button secondary"
          disabled={
            blocked || !ready || messageIndex === null || phase !== 'idle'
          }
          onClick={() => void play()}
        >
          Listen to latest reply
        </button>
        {phase !== 'idle' && (
          <button type="button" className="button secondary" onClick={stop}>
            Stop audio
          </button>
        )}
        {phase !== 'idle' && (
          <span role="status">
            {phase === 'loading'
              ? 'Preparing local reply audio…'
              : 'Playing local reply audio…'}
          </span>
        )}
      </div>
      {error && (
        <p className="voice-error" role="alert">
          {error}
        </p>
      )}
    </div>
  )
}
