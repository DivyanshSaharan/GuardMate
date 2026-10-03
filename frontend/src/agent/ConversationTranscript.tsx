import { memo, useCallback, useEffect, useRef, useState } from 'react'
import { OutgoingMessage, ThinkingIndicator } from './ConversationActivity'
import { ConversationComposer } from './ConversationComposer'
import type { Conversation } from './types'
import { ReplyPlayback } from './speech/ReplyPlayback'
import { useSpeechStatus } from './speech/useSpeechStatus'

export const ConversationTranscript = memo(function ConversationTranscript({
  conversation,
  pending,
  onSend,
  draftStore,
}: {
  conversation: Conversation
  pending: boolean
  onSend: (text: string) => Promise<boolean>
  draftStore?: Map<string, string>
}) {
  const [outgoing, setOutgoing] = useState<{
    text: string
    baseMessageCount: number
    failed: boolean
  } | null>(null)
  const acknowledged =
    outgoing &&
    conversation.messages
      .slice(outgoing.baseMessageCount)
      .some(
        (message) =>
          message.role === 'courier' && message.content === outgoing.text,
      )
  const showOutgoing = outgoing && !acknowledged
  const waiting = !!showOutgoing && !outgoing.failed && pending
  const messagesRef = useRef<HTMLOListElement>(null)
  useEffect(() => {
    const list = messagesRef.current
    if (list) list.scrollTop = list.scrollHeight
  }, [conversation.messages, outgoing?.text, outgoing?.failed, waiting])
  const paused = ['needs_resident', 'ended'].includes(conversation.status)
  const [voiceOwner, setVoiceOwner] = useState<'record' | 'reply' | null>(null)
  const recordingBusy = useCallback((busy: boolean) => {
    setVoiceOwner((owner) =>
      busy ? 'record' : owner === 'record' ? null : owner,
    )
  }, [])
  const playbackBusy = useCallback((busy: boolean) => {
    setVoiceOwner((owner) =>
      busy ? 'reply' : owner === 'reply' ? null : owner,
    )
  }, [])
  const speech = useSpeechStatus()
  const latestMessageIndex = conversation.messages.length - 1
  const latestAssistantIndex =
    conversation.messages[latestMessageIndex]?.role === 'assistant'
      ? latestMessageIndex
      : null
  const send = useCallback(
    async (text: string) => {
      setOutgoing({
        text,
        baseMessageCount: conversation.messages.length,
        failed: false,
      })
      const success = await onSend(text)
      if (success) setOutgoing(null)
      else
        setOutgoing((current) =>
          current ? { ...current, failed: true } : null,
        )
      return success
    },
    [conversation.messages.length, onSend],
  )
  return (
    <div className="conversation-chat">
      <ol
        ref={messagesRef}
        className="conversation-messages"
        aria-label="Courier conversation"
      >
        {conversation.messages.map((message, index) => (
          <li key={index} className={`message-${message.role}`}>
            <span>
              {message.role === 'assistant'
                ? 'GuardMate'
                : message.role === 'resident'
                  ? 'Resident decision'
                  : 'Courier'}
            </span>
            <p>{message.content}</p>
          </li>
        ))}
        {showOutgoing && (
          <OutgoingMessage text={outgoing.text} failed={outgoing.failed} />
        )}
        {waiting && <ThinkingIndicator />}
      </ol>
      <ReplyPlayback
        sessionId={conversation.id}
        revision={conversation.revision}
        messageIndex={latestAssistantIndex}
        blocked={pending || voiceOwner === 'record'}
        ready={!!speech.status?.tts_ready}
        onBusyChange={playbackBusy}
        sessionStatus={conversation.status}
      />
      <div className="conversation-progress" role="status">
        {pending
          ? 'Waiting for the response. Sending another message is paused.'
          : paused
            ? 'Agent paused. The resident must handle the next step.'
            : 'You’re playing the courier. Use fictional details only.'}
      </div>
      <ConversationComposer
        pending={pending}
        disabled={paused}
        onSend={send}
        sessionId={conversation.id}
        draftStore={draftStore}
        speechReady={!!speech.status?.stt_ready}
        recordingBlocked={voiceOwner === 'reply'}
        onVoiceBusyChange={recordingBusy}
      />
      <p className="local-voice-note">
        Raw audio stays on this computer. Sending your reviewed message sends
        its text, earlier conversation and saved resident instructions to hosted
        Tinker/Qwen. No phone calls are connected.
      </p>
      <p className="local-voice-status" role="status">
        {speech.error ||
          speech.status?.message ||
          'Checking local voice availability…'}
      </p>
    </div>
  )
})
