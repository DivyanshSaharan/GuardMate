# Local browser voice role-play

Browser speech is a manual, half-duplex testing interface, not a cellular bridge or autonomous
call-answering service. It reuses the existing conversation and resident-approval workflow.
Whisper/Piper run on the backend computer; **Qwen planning is still hosted through Tinker and untuned**.

## Setup on Windows x64

From the repository root, install the optional Python runtime and explicitly download speech assets:

```powershell
.venv\Scripts\python -m pip install -r backend\requirements-voice.txt
.venv\Scripts\python backend\scripts\setup_speech.py --download
```

The setup downloads about 266 MB of model/binary assets, plus approximately 49 MB of Piper/ONNX
Python wheels and dependencies. No sponsor API, credits, virtual number, FFmpeg or microphone access
is used for setup. It pins immutable model revisions and checks file size/SHA-256 before use. Existing
mismatched files are not overwritten. All runtime/model files stay in ignored `.cache/voice/` and
`models/speech/`; do not commit them or your `.env`.

Without `--download`, the setup command only inspects the pinned asset files. Restart the backend
after installation. `GET /api/speech/status` reports asset/dependency presence, not a completed
runtime or device test. The default paths are:

- Whisper.cpp CPU `v1.8.2`: `.cache/voice/whisper-v1.8.2/Release/whisper-cli.exe`. Keep its sibling DLLs.
- English Whisper `base.en`: `models/speech/ggml-base.en.bin`.
- Piper `1.8.0`, CPU ONNX Runtime `1.30.0`: `models/speech/en_US-ljspeech-high.onnx` plus its `.onnx.json`.

The backend-only overrides `GUARDMATE_WHISPER_CLI`, `GUARDMATE_WHISPER_MODEL`, and
`GUARDMATE_PIPER_MODEL` accept local paths. No runtime URL or cloud speech provider is accepted.
The explicit setup download needs internet; ordinary speech requests do not download models.

On Linux, install the Python voice requirements and run setup with `--download --models-only`.
Build [Whisper.cpp](https://github.com/ggml-org/whisper.cpp/tree/v1.8.2) separately and set
`GUARDMATE_WHISPER_CLI` to its `whisper-cli` executable. Create a separate Linux virtual environment;
the Windows ZIP and venv are not Linux runtimes. Linux speech has not yet been demonstrated here.

## Use

Open the role-play panel, enable a delivery window, and create a **new** fictional courier session.
Microphone access requires a modern browser on localhost (or a secure origin); grant permission
yourself only when you choose **Record courier message**.

1. Record a short English message. **Stop recording** transcribes it; the limit is 30 seconds.
2. Edit the local transcript and choose **Use transcript**. It appends to an existing typed draft,
   rather than replacing it. Nothing has been sent to Qwen yet.
3. Click **Send**. The existing outgoing message and waiting indicator appear immediately, and a
   second send is blocked until the response finishes.
4. Choose **Listen to latest reply** to generate/play local Piper audio. **Stop audio** cancels playback.

Recording, transcription/review and reply playback mutually exclude each other to avoid capturing
GuardMate's own voice. Typing remains available where appropriate; voice failures do not remove
the text workflow. Session changes, pending sends, cancellation, panel closure and component
unmount stop tracks/playback and ignore late results. The browser uses bounded WebAudio PCM capture,
not its potentially cloud-backed speech-recognition or voice-synthesis APIs.

No automatic sending, continuous listening, voice activity detection, barge-in, live streaming,
phone answering or transfer is implemented. Transcription is fallible, including on accents/noise;
review before sending. Real courier collection requires consent and a separate data policy.

## Data, permission and resource boundaries

Raw microphone WAV goes only to the loopback backend and its local Whisper subprocess. It is
temporarily written for native inference and removed when the request completes/fails; the app
does not add it to the SQLite history or upload it to Tinker. Canceling a browser request does not
promise immediate interruption of a backend job already running; it is bounded by a 45-second
timeout and still cleans its temporary files. Backends/logs/proxies outside this local setup are
not part of that retention promise.

Confirmed courier messages, the prior conversation and saved resident context **do** go to hosted
Qwen/Tinker through the existing Send path and persist in the local conversation history. Recording,
transcription, listening and checking speech status make no Qwen requests. Saved replies only are
synthesized; caller-supplied text cannot be passed to the synthesis endpoint.

Speech workers receive a minimal environment without model API credentials, run with no shell,
use four CPU threads where supported, and accept one native job at a time. Input must be complete
PCM16 mono 16 kHz WAV, at most 30 seconds. Silent/DC-only, malformed, oversized or overlong
transcriptions are refused. Silence checking is an amplitude heuristic, not speech detection or
a guarantee against hallucinated transcriptions. Output is capped at 2 MB/60 seconds.

Playback requires the latest saved assistant message, matching conversation revision and the
resident context used to compose it, with an active delivery window. These checks run before and
after synthesis. A settings/availability/window change can reject playback even without a new
model turn. Old sessions without the new source-context stamp remain readable but their audio
is refused; use a fresh session/reply. Audio is never a verified parcel receipt or new approval.

## Verification and measured limits

Free inspection and explicit native synthetic smoke check:

```powershell
.venv\Scripts\python backend\scripts\smoke_speech.py
.venv\Scripts\python backend\scripts\smoke_speech.py --run --output .data/evaluation/local-speech-new-run.json --sample .data/evaluation/local-speech-new-sample.wav
```

Use new artifact names; files are never overwritten. The optional sample is retained **synthetic**
Piper output, not a microphone recording. The report records no model API requests. This check
generates speech, converts it to the expected WAV format, and runs actual local transcription;
it does not claim to be a real courier, microphone/browser playback or friend test.

On October 3, 2026, Windows/Ryzen 7 7840HS/16 GB RAM completed all three clean synthetic roundtrips
with exact normalized transcriptions. Piper jobs took 3,379–3,792 ms; Whisper jobs took
2,061–2,364 ms for 1.5–3.2 seconds of generated audio. These include fresh process/model loading
for every request. They are not full spoken-conversation latency, accent accuracy, noisy-call
performance or a fine-tuning improvement. Faster warm/persistent workers remain future work.

## Sources and licences

- [Whisper.cpp v1.8.2](https://github.com/ggml-org/whisper.cpp/releases/tag/v1.8.2), MIT code;
  [pinned English model](https://huggingface.co/ggerganov/whisper.cpp/tree/5359861c739e955e79d9a303bcbc70fb988958b1).
- [Piper 1.8.0](https://pypi.org/project/piper-tts/1.8.0/) runtime is GPL-3.0-or-later, not MIT.
  [Python synthesis API](https://github.com/OHF-Voice/piper1-gpl/blob/639388b6317fc4731e91d53da42aea68fd4166ff/docs/API_PYTHON.md).
- The [LJ Speech voice card](https://huggingface.co/rhasspy/piper-voices/blob/c10ece1aade47bb51c153c893d14e5bf8e5b7117/en/en_US/ljspeech/high/MODEL_CARD)
  identifies a trained-from-scratch voice and public-domain [training dataset](https://keithito.com/LJ-Speech-Dataset/).
  Do not infer every Piper voice's terms from the repository label.

Setup preserves the Whisper/Piper licence text, voice model card and voice-repository README.
Keep component notices/provenance and review applicable terms before distributing bundled runtimes
or models. Downloading these assets does not make the complete application offline: the planner
still needs Tinker until a separate local-model deployment is demonstrated.
