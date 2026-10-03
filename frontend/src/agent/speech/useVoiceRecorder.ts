import { useCallback, useEffect, useRef, useState } from 'react'
import { transcribe } from './api'
import { pcmToWav } from './pcm'

export type RecordingPhase =
  'idle' | 'requesting' | 'recording' | 'transcribing' | 'review'

interface Capture {
  stream: MediaStream
  context: AudioContext
  source: MediaStreamAudioSourceNode
  processor: ScriptProcessorNode
  mute: GainNode
  chunks: Float32Array[]
  samples: number
}

export function useVoiceRecorder(sessionId: string, blocked: boolean) {
  const [phase, setPhase] = useState<RecordingPhase>('idle')
  const [transcript, setTranscript] = useState('')
  const [error, setError] = useState('')
  const generation = useRef(0)
  const capture = useRef<Capture | null>(null)
  const controller = useRef<AbortController | null>(null)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const active = useRef(false)
  const blockedRef = useRef(blocked)
  blockedRef.current = blocked

  const release = useCallback(() => {
    if (timer.current) clearTimeout(timer.current)
    timer.current = null
    const current = capture.current
    capture.current = null
    if (!current) return
    current.processor.onaudioprocess = null
    current.stream.getTracks().forEach((track) => track.stop())
    current.source.disconnect()
    current.processor.disconnect()
    current.mute.disconnect()
    void current.context.close().catch(() => {})
  }, [])

  const cancel = useCallback(() => {
    generation.current++
    active.current = false
    controller.current?.abort()
    controller.current = null
    release()
    setPhase('idle')
    setTranscript('')
    setError('')
  }, [release])

  useEffect(() => {
    cancel()
    return () => {
      generation.current++
      active.current = false
      controller.current?.abort()
      release()
    }
  }, [sessionId, blocked, cancel, release])

  const stop = useCallback(async () => {
    const current = capture.current
    if (!current || !active.current || blockedRef.current) return
    const version = generation.current
    active.current = false
    release()
    setPhase('transcribing')
    const request = new AbortController()
    controller.current = request
    try {
      const wav = pcmToWav(current.chunks, current.context.sampleRate)
      const result = await transcribe(wav, request.signal)
      if (version !== generation.current || blockedRef.current) return
      controller.current = null
      if (!result.text.trim())
        throw new Error(
          'No speech was detected. Please try again or type your message.',
        )
      setTranscript(result.text.trim())
      setPhase('review')
    } catch (failure) {
      if (version !== generation.current || request.signal.aborted) return
      controller.current = null
      setError(
        failure instanceof Error
          ? failure.message
          : 'The recording could not be transcribed. Please try again.',
      )
      setPhase('idle')
    }
  }, [release])

  const start = useCallback(async () => {
    if (active.current || blockedRef.current) return
    cancel()
    const version = generation.current
    active.current = true
    setPhase('requesting')
    let stream: MediaStream | null = null
    let context: AudioContext | null = null
    try {
      if (!navigator.mediaDevices?.getUserMedia || !window.AudioContext) {
        throw new Error(
          'Microphone recording is not supported here. Open GuardMate on localhost in a modern browser, or type your message.',
        )
      }
      stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
        },
      })
      if (version !== generation.current || blockedRef.current) {
        stream.getTracks().forEach((track) => track.stop())
        return
      }
      context = new AudioContext()
      const source = context.createMediaStreamSource(stream)
      const processor = context.createScriptProcessor(4096, 1, 1)
      const mute = context.createGain()
      mute.gain.value = 0
      const current: Capture = {
        stream,
        context,
        source,
        processor,
        mute,
        chunks: [],
        samples: 0,
      }
      capture.current = current
      processor.onaudioprocess = (event) => {
        if (version !== generation.current || !active.current) return
        const remaining =
          Math.floor(current.context.sampleRate * 30) - current.samples
        const input = event.inputBuffer.getChannelData(0)
        const chunk = input.slice(0, Math.max(0, remaining))
        if (chunk.length) {
          current.chunks.push(chunk)
          current.samples += chunk.length
        }
        if (chunk.length >= remaining) void stop()
      }
      source.connect(processor)
      processor.connect(mute)
      mute.connect(context.destination)
      await context.resume()
      if (
        version !== generation.current ||
        blockedRef.current ||
        capture.current !== current ||
        !active.current
      )
        return
      setPhase('recording')
      timer.current = setTimeout(() => void stop(), 30_000)
    } catch (failure) {
      // The stream can have arrived before AudioContext setup fails.
      stream?.getTracks().forEach((track) => track.stop())
      if (capture.current?.context === context) release()
      else if (context) void context.close().catch(() => {})
      if (version !== generation.current) return
      active.current = false
      const denied =
        failure instanceof DOMException && failure.name === 'NotAllowedError'
      setError(
        denied
          ? 'Microphone permission was denied. Allow it in your browser to record, or continue typing.'
          : failure instanceof Error
            ? failure.message
            : 'The microphone could not be opened. You can still type your message.',
      )
      setPhase('idle')
    }
  }, [cancel, release, stop])

  return { phase, transcript, setTranscript, error, start, stop, cancel }
}
