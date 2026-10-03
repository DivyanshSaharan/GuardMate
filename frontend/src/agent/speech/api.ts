export interface SpeechStatus {
  stt_ready: boolean
  tts_ready: boolean
  message: string
  max_seconds: number
}

export interface Transcription {
  text: string
  duration_ms: number
  processing_ms: number
}

async function checked(response: Response): Promise<Response> {
  if (response.ok) return response
  const body = await response.json().catch(() => ({}))
  throw new Error(
    typeof body.detail === 'string'
      ? body.detail
      : 'Local voice is unavailable. You can still use the text conversation.',
  )
}

export async function speechStatus(signal: AbortSignal): Promise<SpeechStatus> {
  return (await checked(await fetch('/api/speech/status', { signal }))).json()
}

export async function transcribe(
  wav: Blob,
  signal: AbortSignal,
): Promise<Transcription> {
  return (
    await checked(
      await fetch('/api/speech/transcribe', {
        method: 'POST',
        headers: { 'Content-Type': 'audio/wav' },
        body: wav,
        signal,
      }),
    )
  ).json()
}

export async function replyAudio(
  sessionId: string,
  revision: number,
  messageIndex: number,
  signal: AbortSignal,
): Promise<Blob> {
  return (
    await checked(
      await fetch(
        `/api/conversations/${encodeURIComponent(sessionId)}/speech`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ revision, message_index: messageIndex }),
          signal,
        },
      ),
    )
  ).blob()
}
