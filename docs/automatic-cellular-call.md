# Automatic conversation after manual answering

This experimental mode handles speech turns without an operator typing `listen`
and `send` for each turn. **Answering and hanging up the physical call remain
manual.** Keep the operator and terminal present; use consenting fictional test
calls only. This automatic mode has not been validated on a live call.

The [previous live test](live-cellular-test-2026-10-04.md) demonstrated the manually
controlled audio/model/reply path, but also revealed substantial Whisper errors.
Automatic endpointing is not a fix for ASR accuracy, and neither energy detection
nor transcript filtering supplies a confidence score.

## What happens

After an interactive `AUTO CALL READY` confirmation, the runner creates one fresh
conversation and speaks its saved greeting. It then repeats these phases:

1. Open one pinned vivo input stream and report `utterance_armed` locally after
   its first valid input callback. This is an on-screen cue, not a beep the caller
   can hear and not proof of caller identity or an active call.
2. Keep a short pre-roll, detect sustained audio energy and finish after about
   700 ms of quiet. Defaults allow 15 seconds waiting for onset and at most ten
   seconds of complete utterance audio, including pre-roll and trailing silence.
   An overlong utterance is discarded, not silently truncated.
3. Close input, transcribe locally, inspect the text for explicit warning signs,
   and submit it automatically to hosted Qwen. **No per-turn human review.**
4. Reuse the existing parcel/approval checker, synthesize the latest saved checked
   reply locally with Piper, and play that whole reply through the pinned output.
5. Only after playback finishes, start another input stream if the owned session
   is still active and the limits permit another turn.

Capture and playback never deliberately overlap. There is no continuous duplex,
barge-in, voice identity verification, automatic answer, automatic hangup or
automatic reconnection. An answer spoken during playback or before the next input
stream starts can be lost; pre-roll cannot recover audio from before stream start.

## Consent and launch

The former manual-mode agreement to **reviewed** text is not consent to this mode.
Both people must separately agree, before launch, to local capture and automatic
upload of **unreviewed recognized text**, previous conversation and saved resident
delivery instructions to Qwen/Tinker. Whisper can mishear or invent words. The
resident must agree to sharing their saved instructions too.

Raw call WAV is not uploaded to Tinker or retained by the runner. The local speech
service uses temporary files. Submitted text and checked conversation history
persist in the existing local SQLite store; terminal output may also be retained.
Credentials stay in the backend and are stripped from native worker environments.

Keep the backend running, enable a bounded delivery window yourself, and inspect
readiness and fresh phone endpoint IDs:

```powershell
.venv\Scripts\python backend\scripts\run_automatic_call.py
```

The default command opens no capture/playback stream, creates no session, changes
no resident preference and sends no model request. Endpoint IDs are not stable.

Manually answer the agreed fictional call on the PC. Keep the phone off
speakerphone and mute only the laptop microphone, not the call. WDM-KS access is
exclusive and can interrupt the human Phone Link audio path. Use freshly printed
IDs in place of `INPUT_ID` and `OUTPUT_ID`:

```powershell
.venv\Scripts\python backend\scripts\run_automatic_call.py --run --input-id INPUT_ID --output-id OUTPUT_ID --max-turns 5 --time-limit 180 --label "Fictional automatic courier" --ack-consenting-test-call --ack-exclusive-audio-risk --ack-hosted-transcripts --ack-automatic-transcripts
```

All four acknowledgments, an interactive terminal and exact `AUTO CALL READY`
confirmation are mandatory. Missing consent/TTY is refused before discovery or
HTTP. The operator must coordinate the first speech with the local armed cue; do
not pipe scripted answers or commands into the test.

## Limits and stops

The CLI allows one to five attempted model turns and an admission deadline of
30–600 seconds (180 by default). The existing backend $0.25 reservation cap is
unchanged. No model, transcription or audio failure is automatically retried; no
new session/call is started when the current run stops.

The deadline is checked between phases, including before model submission and
native playback. It is **not an in-flight interruption**: an already admitted
capture, backend job or playing clip can finish after the deadline, within its
existing bounded timeout. A hangup/resident change during playback is likewise
not detected automatically. Ctrl+C is the operator's emergency stop.

The loop stops on silence timeout, overlong speech, malformed/empty/explicitly
inaudible text, detected exact reply echo/repetition, ambiguous confirmation,
resident approval/takeover, completed session, expired/stale context, limits, or
any uncertain operation. These text rules catch only specific warning signs;
plausible recognition errors can still pass. They are not a safety or recognition
accuracy guarantee.

Captured text stays bound to its original question revision and resident-context
hash, checked before and after capture/transcription and again before the POST.
Saved replies are checked before/after synthesis and immediately before playback.
Settings changes never silently move a short answer to another question.

Stopping automation does **not** end the saved session or decline a pending
approval. Use the existing resident UI to inspect, decide or end it; handle the
physical caller yourself. Approving an alternative does not restart the loop.
Rejected unsent drafts exist only in process memory/terminal output, not the saved
conversation, and are lost from memory when the CLI exits.

Ctrl+C attempts to end only the known saved session after stopping native work.
An uncertain POST may still finish on the backend: inspect the known ID and never
resend or replay based on an older reply. Physical hangup is always manual.

## Verification boundary

Offline tests use synthetic PCM, fake native streams, fake HTTP/model/speech and
temporary stores. They exercise endpoint timing, clipping refusal, armed event
ordering, consent/protocol validation, secret-free child cleanup, half-duplex
ordering, deadlines, stale context, uncertainty, caps and approval preservation.
They do not demonstrate natural caller speech, live automatic ASR quality,
end-to-end latency, Phone Link recovery or reliable real courier behavior.

No model download, paid service, number purchase, budget increase, inference,
recording or playback is required merely to build/test this mode. A future live
automatic test requires fresh both-party agreement to its different data flow.
