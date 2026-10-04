# First integrated cellular call test — October 4, 2026

## Result

The manually answered, half-duplex route worked on one consenting fictional
Phone Link call using a vivo T2x 5G (Android 15). The caller confirmed hearing the
complete local greeting and all three checked replies. Both people also confirmed
normal human audio in both directions after the runner exited.

This proves the tested route can capture caller speech, transcribe locally, submit
reviewed text to real hosted Qwen, check its plan, synthesize locally and transmit
the reply back on this hardware. It does **not** establish unattended answering,
continuous listening, simultaneous duplex, another phone/carrier combination or
fresh-call recovery. The carrier/SIM used for this particular call was not logged.

Speech recognition was unreliable. The test reached a checked guard-room handoff,
but **did not record a completed delivery**: the final misrecognized sentence was
too ambiguous for the application checker. No parcel actually changed hands.

## Consent and setup

Both people explicitly agreed to up to five fictional hosted turns, local capture,
and sending reviewed text, conversation history and saved resident instructions to
Tinker. They confirmed the same call was active on the PC before starting audio.
The exclusive WDM-KS interruption risk was acknowledged.

- A user-approved 15-minute delivery window was enabled temporarily. The original
  disabled setting was restored afterward. Saved availability (`at_office`) and
  PG instructions were not changed.
- Fresh metadata first showed input/output 31/30; the live invocation reported
  25/24 instead. The stale selection was refused before session creation, audio or
  inference. The new inspection values were explicitly selected and rechecked.
- One fresh conversation was created:
  `b9fb1a16-4d78-452e-bc90-daaa42b702bb`, labelled
  `Fictional cellular courier Oct4`.
- Capture length was eight seconds. The caller was asked to repeat each short
  phrase with pauses to accommodate manual timing and stream startup. This is
  not a natural-turn or voice-activity-detection benchmark.
- Raw call audio was not retained by the runner or uploaded to Tinker. The local
  speech service uses temporary files; confirmed text/history persists locally.

## Actual transcripts and responses

No submitted transcript was manually corrected. Four captures produced three
explicit model submissions; the first incorrect draft was discarded on request.

The intended first line was “I have a prepaid parcel to deliver.” Its first
transcript was `prepare pass and don't deliver.` That negative draft was **not
sent**. A user-requested second recording returned
`a prepaid passion to deliver. I have`, which was submitted unchanged.

| Turn | Submitted local transcript | Qwen proposal | Checked application action | Model/policy latency |
| --- | --- | --- | --- | --- |
| 1 | `a prepaid passion to deliver. I have` | Clarify guard availability | Ask whether security can accept the parcel in person; prepaid saved as true | 33,512 ms |
| 2 | `is here. Yes, security is here here.` | Handoff | Authorize the saved guard-room location, requiring handover to security rather than unattended placement | 2,228 ms |
| 3 | `security accepted the option security accepted the option security accepted` | Record outcome | Reject an unsupported completed outcome and ask for clarification | 2,354 ms |

Exact checked replies, each confirmed fully heard by the caller:

1. “Is security there to accept the parcel in person?”
2. “Please hand the prepaid parcel to security at the guard room beside the main
   entrance. Do not leave it unattended. Please tell me once security has accepted
   it.”
3. “Has the parcel actually been received, returned, or not delivered yet?”

The caller also confirmed the complete greeting ending “How can I help with this
delivery?” Native playback completion alone was not used as receipt evidence.

The final saved state was `ended`, revision 4, turn count 3, and
`courier_reported_outcome: null`. Ending was an explicit operator command, not
automatic detection or physical hangup. The physical call remained under Phone
Link's manual controls.

## Model and resource evidence

All three saved traces identify `Qwen/Qwen3.5-4B`, hosted through Tinker, with
`target_kind: base`, no sampler checkpoint, and `model_result: plan_returned`.
There was no scripted planner replacement or tuned model in this test.

The first turn included cold provider setup; subsequent model/policy turns took
about 2.2–2.4 seconds. These are saved backend event durations, **not total
speech-to-speech latency**. Capture, transcription, synthesis, native startup and
operator review add time. End-to-end component timing was not measured here.

The existing reservation ledger increased from $0.123602 to $0.127139: an
estimated worst-case reservation of **$0.003537 for this test**, not an actual
invoice or account balance. The existing $0.25 test-stage cap was not raised.
No training, new telephony service, number purchase or model download was done.

## What needs work next

The phone audio connection gap is now demonstrated for this operator-controlled
configuration. The next reliability work is capture timing and transcription:
`parcel` became `passion` and `option`, and repeated speech crossed capture
boundaries. These observations do not distinguish ASR-model, signal-quality and
timing causes; no aligned recording/reference exists to calculate a credible word
error rate or accuracy percentage.

An improved test should expose a real stream-ready cue, evaluate single natural
utterances, and measure capture/STT/planning/TTS/playback durations separately.
Any retained diagnostic audio or additional hosted/live test needs fresh agreement.
Automatic answering, hangup, VAD, interruption, locked-phone recovery, a new call's
reconnection and broader delivery exceptions remain unverified.
