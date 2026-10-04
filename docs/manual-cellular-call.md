# Manually answered cellular conversation runner

This is an **operator-controlled, half-duplex test**, not unattended call answering.
The existing [audio probe](cellular-audio-probe.md) demonstrated separate capture
and synthetic transmission on the vivo T2x 5G by user confirmation. This increment
connects that route to GuardMate's existing local speech and checked conversation
interfaces. One owner-approved fictional live call now exercised three actual
Qwen turns; the caller confirmed hearing the greeting and all checked replies.
Transcription errors prevented a completed-outcome report. See the
[first integrated test report](live-cellular-test-2026-10-04.md). This is not proof
of unattended calling; further hosted/live tests require fresh agreement.

## What is connected

The operator answers a consenting fictional test call using Phone Link. Every
runner invocation creates a **new UUID conversation** rather than looking up a
telephone number or reusing another courier's parcel facts.

1. `listen` captures a bounded turn from the explicitly selected vivo input.
2. The loopback backend transcribes it locally using whisper.cpp. The draft is
   shown for review; recording alone does not send anything to Qwen.
3. `edit` corrects the draft, `discard` drops it, or `send` explicitly submits it.
4. Hosted Qwen proposes a structured plan. The existing application checks its
   facts and permissions; raw model output is never spoken.
5. The latest saved checked reply is synthesized by local Piper, converted to
   mono PCM16/16 kHz, and played through the pinned vivo output in one stream.

The fresh greeting is spoken when the confirmed runner starts. Starting a session
or speaking the greeting does not sample Qwen. Each explicit `send` attempts one
existing planner turn, without automatic retries or a scripted model replacement.

No new phone number, paid telephony service, virtual-audio driver, call forwarding,
model training or Windows default-device change is required by this increment.
Ordinary SIM/carrier charges are separate from GuardMate; hosted planning uses
existing Tinker credits and the existing conversation reservation cap.

## Setup and inspection

Use the repository's Windows venv and the already installed speech assets:

```powershell
.venv\Scripts\python -m pip install -r backend\requirements-cellular.txt -r backend\requirements-voice.txt
```

See [local speech setup](browser-voice.md) if Whisper/Piper assets are missing.
Ordinary runner use does not download models. Keep `.env`, native runtimes and
model files out of Git.

Start the existing backend in a separate terminal:

```powershell
.venv\Scripts\python -m uvicorn guardmate.main:app --app-dir backend --host 127.0.0.1 --port 8765
```

Read-only inspection is the default:

```powershell
.venv\Scripts\python backend\scripts\run_cellular_call.py
```

This reads local readiness and phone endpoint metadata, not call state. It starts
no recording/playback stream and samples no model. It does not create a session
or alter the resident's settings. An enabled delivery window is needed only for
live mode; enable it yourself in the existing app when you intend to test.

Use the freshly printed phone input/output IDs. The earlier machine snapshot used
25/24, but **those are not permanent IDs**. The child rechecks each selected ID,
full name, direction and WDM-KS host interface; laptop endpoints and fallback
devices are refused. The runner has no ability to verify a call is active.

The backend URL accepts only literal HTTP `127.0.0.1` or `[::1]`, an optional
port, and no path. DNS host aliases, proxy environment settings and HTTP redirects
are not used. Do not expose this unauthenticated single-resident prototype over
the network or use another person's local service.

## Live test: only after separate agreement

Both people must agree to the recording and hosted text processing. Do not leave
a real courier waiting during setup. Answer the call on the PC, keep the phone
off speakerphone, and mute only the laptop microphone, **not the call**. WDM-KS
is exclusive and can briefly interrupt Phone Link's human-audio path.

Replace `INPUT_ID` and `OUTPUT_ID` with fresh metadata values:

```powershell
.venv\Scripts\python backend\scripts\run_cellular_call.py --run --input-id INPUT_ID --output-id OUTPUT_ID --seconds 5 --max-turns 5 --label "Fictional cellular courier" --ack-consenting-test-call --ack-exclusive-audio-risk --ack-hosted-transcripts
```

The three acknowledgments are all required. Live mode also requires an interactive
terminal and typing `CALL READY` for the active, agreed call before creating a
session or playing its greeting. Do not pipe a script into live mode.

Commands are deliberately manual:

- `listen`: start a fixed 1–10 second capture (five seconds by default), then
  local transcription. The caller should speak only during that turn.
- `edit`: enter corrected courier text. Edits never grant resident approval.
- `send`: submit the reviewed text once to the configured hosted planner and
  speak the checked reply. The default runner limit is five planner attempts;
  the allowed CLI limit is 1–10, independently of the existing backend budget.
- `discard`: drop the local draft without a model request.
- `speak`: try an unplayed latest saved reply, without another model turn.
- `repeat`: explicitly repeat a previously played reply while the session is
  active. Native/model failures are never retried automatically.
- `refresh`: reread the owned saved session and **discard the draft**. After a
  resident decision, capture a fresh answer at the new question/revision.
- `stop`: end only this runner's known saved session. **Hang up the actual call
  yourself in Phone Link.** Ctrl+C stops the native child and attempts to end the
  known session; an uncertain start never causes another session to be guessed.

An awaiting approval, resident takeover or ended state blocks new capture/send
and manual replay. The newly returned checked pause/outcome notification may be
spoken once so the caller knows to keep the parcel or wait for the resident.
Approve/decline/take over only through the existing resident controls. This runner
does not transfer a phone call. Every new call requires stopping the old runner
and starting a new one; masked/repeated numbers do not identify a session.

## Safety, freshness and uncertainty

A captured/editable draft stays bound to its exact session, question revision and
resident-context hash. Changes during recording/transcription or before sending
reject the draft. The optional `expected_context` field is sent unchanged to the
backend: a mismatch is rejected before appending the courier turn or sampling a
model. If the plan changes while Qwen is thinking, the returned plan is refused
and a checked resident-help reply replaces any handoff. Legacy text clients can
omit this field; the manual runner does not omit it.

Speech uses only the latest saved assistant message with matching revision and
resident context. Freshness is checked before/after synthesis and again after
conversion immediately before native playback. The whole reply must fit the
30-second bound; overlong or invalid output is refused, **never truncated**.
The original standalone probe remains limited to ten seconds.

These are admission checks, not in-flight barge-in. A resident change or hangup
during an already playing clip is not automatically detected; use Ctrl+C and
manual Phone Link controls. Continuous listening, voice-activity detection,
automatic answering/hangup, caller identification, simultaneous duplex and
background call monitoring are not implemented. Laptop/driver/phone support
outside the demonstrated hardware is unknown.

An uncertain hosted POST may still be running or already saved on the backend.
The runner latches the uncertainty and stops accepting further audio/model work:
it does not resend, reuse an older reply or consume queued commands. Inspect/end
the known session in the app and handle the physical call manually. Local native
failures likewise do not prove what the caller heard.

## Data and resource boundaries

Raw caller WAV exists in memory and in the existing local speech service's
temporary directory while transcription runs; it is not added to this runner's
SQLite history or uploaded to Tinker. Temporary speech files are removed on
completion/failure by that service. An HTTP cancellation cannot instantly stop an
already admitted backend speech job; its existing 45-second timeout applies.
Terminal output is visible and may be retained by the terminal/app; it is not an
encrypted log. The separate earlier probe recordings remain ignored/local until
the owner deletes them; this runner does not silently remove them.

Explicitly sent reviewed text, prior conversation and saved resident delivery
context **do go to hosted Qwen/Tinker** and confirmed turns persist locally in
the existing conversation store. API credentials stay in the backend `.env`/
environment and are never passed to the native audio workers.

Native workers use a stripped environment, hidden windows, bounded JSON/stdio,
explicit paired phone identity, and one operation at a time. Capture children
have a 20-second deadline; complete-reply playback children have a 40-second
deadline. No raw audio artifact is written by those workers. No auto-redial,
retry, API-budget increase or alternative endpoint is attempted.

## Verification status

Offline tests cover the local client, private worker protocol, whole-reply
conversion, CLI acknowledgments/cancellation, session ownership, context changes,
uncertain outcomes and checked pause handling. These tests do not establish live
Whisper accuracy, Qwen call quality, latency, hangup recovery or caller receipt.

Nine in-process wireflow tests connect the actual runner, HTTP client, FastAPI
routes, conversation engine and temporary SQLite store. They substitute the
planner, speech service and phone audio with fakes and prohibit real HTTP
connections. Cases include prepaid/guard confirmation through reported delivery,
fresh-call isolation, context changes at each processing boundary, and a committed
turn whose response is lost without resending it. Run them without credits:

```powershell
.venv\Scripts\python -m pytest backend\tests\test_manual_call_integration.py
```

Read-only inspection also succeeded on the configured Windows installation:
Whisper/Piper and Qwen configuration were ready, the paired vivo input/output were
listed, and delivery mode was off. Inspection opened no audio stream, created no
conversation and sent no model request. Readiness is not proof of an active call.

The prior separate-direction hardware proof is documented in the
[cellular-audio checkpoint](cellular-audio-probe.md). The
[first integrated live test](live-cellular-test-2026-10-04.md) confirmed the full
greeting and three checked replies on the same consenting call, while revealing
substantial transcription errors. Normal human Phone Link audio worked afterward.
Total speech-to-speech latency, natural-turn recognition, fresh-call recovery and
unattended operation remain unverified. The test used base Qwen, not the tuned
checkpoint; it created no verified delivery or general accuracy claim.
