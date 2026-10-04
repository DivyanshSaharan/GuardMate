# GuardMate

An AI delivery assistant for a friend who lives in a paying guest accommodation
(PG) and keeps getting courier calls while at the office.

His usual answer: **hand the prepaid parcel to security at the guard room near
the entrance**. Missed calls sometimes mean returned parcels. GuardMate turns that
repeated instruction into a conversation that remembers the delivery, knows when
he is home, and asks him before changing the handoff plan.

Built for **Hacktoberfest 2026 · Build for a Friend**.

[DEV submission draft](docs/dev-submission.md) · [Submission checklist](docs/submission-checklist.md)

## Demo

- [Product walkthrough — video (MP4)](docs/media/guardmate-demo.mp4)
- [Call recording — audio (MP3)](docs/media/guardmate-call-recording.mp3)

Open either file on GitHub, or use its download/raw-file control if playback is
not available. The call recording is shared with all speakers' permission.

## What it does

- Saves PG directions, guard-room instructions, office days and today's availability.
- Uses Qwen to plan multi-turn conversations, not just classify a message.
- Isolates each courier's history, parcel facts and resident decisions.
- Checks model proposals against saved instructions before speaking a reply.
- Requires expiring resident approval for an alternative location; pauses on
  payment, OTP, signature and high-value exceptions.
- Supports text and local browser voice role-play, plus a Windows Phone Link call path.
- Provides experimental automatic turn-taking **after you manually answer**.

Courier-reported delivery is not independently verified receipt. GuardMate does
not answer or hang up calls automatically.

## Open AI at the core

The planner uses open-weight **Qwen3.5-4B** through Tinker. **whisper.cpp** transcribes
speech locally; **Piper** generates replies locally. The model proposes structured
actions, and GuardMate's application checks decide what can execute.

```text
Courier audio → local Whisper → Qwen planner → permission checks → local Piper → caller
                                   ↑
                  saved instructions, routine and conversation
```

Open components let us inspect the voice pipeline, replay audio against different
local models, and fine-tune the planner on delivery-specific examples. Raw audio
need not go to a cloud speech service. The planner is still hosted: recognized
text, conversation history and saved resident context go to Tinker.
**The complete application is not offline.**

| Layer                      | Stack                                             |
| -------------------------- | ------------------------------------------------- |
| Dashboard                  | React, TypeScript, Vite                           |
| Backend and persistence    | FastAPI, Pydantic, SQLite                         |
| Planning and LoRA training | Qwen3.5-4B, Tinker                                |
| Local speech               | whisper.cpp `base.en`, Piper                      |
| Tested cellular route      | Windows Phone Link, pinned WDM-KS Bluetooth audio |

## Demonstrated results

**One integrated, consenting fictional phone call** worked on a vivo T2x 5G
(Android 15). The caller heard the full greeting and three checked replies from
actual Qwen turns. The operator answered and reviewed transcripts. An ambiguous
final transcript was rejected instead of recording a completed delivery. Human
audio worked in both directions afterward.
[Live-call report](docs/live-cellular-test-2026-10-04.md).

**One LoRA pilot** completed 30 updates on 40 fictional planner targets. A fresh,
matched six-scenario development comparison produced:

| Metric                           | Base    | Tuned   |
| -------------------------------- | ------- | ------- |
| Strict planner agreement         | 7/13    | 8/13    |
| Checked scenario success         | 5/6     | 5/6     |
| Median text-model/policy latency | 2.133 s | 3.113 s |

This is a small planner-agreement gain, **not a demonstrated functional or speed
improvement**. These are exposed fictional development cases, not field accuracy.
The recorded live call used base Qwen; the pilot did not automatically promote a
checkpoint. [Pilot report](docs/pilot-2026-10-04.md).

The demo's fixed “Tinker Fine-Tuned Model” heading is a presentation label, not
runtime evidence. Saved action traces record the actual model/checkpoint used.

Automatic turn-taking has offline regression coverage but has **not** been tested
on a live call. Transcription errors remain the biggest reliability issue.

## Run locally

Use Python 3.13+ and Node.js 22.12+; development has been tested on Windows.
From the repository root:

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r backend\requirements-dev.txt
.venv\Scripts\python -m pip install -r backend\requirements-ai.txt
npm ci
Copy-Item .env.example .env
```

Add your `TINKER_API_KEY` to the ignored `.env` file. Keep it out of frontend code.
Text conversations need hosted inference credit; offline tests do not.

Start the backend:

```powershell
.venv\Scripts\python -m uvicorn guardmate.main:app --app-dir backend --host 127.0.0.1 --port 8765
```

In another terminal:

```powershell
npm run dev
```

Open [GuardMate](http://127.0.0.1:5173). Save your instructions, enable a bounded
delivery window, and start a fictional courier role-play. Resident decisions use
separate controls, not the courier chat.
[API docs](http://127.0.0.1:8765/docs) are available locally.

On Ubuntu/WSL, create a separate environment with `python3 -m venv .venv` and use
`.venv/bin/python`. The tested cellular route runs on Windows, not WSL.

### Optional voice and calls

Local speech needs an explicit asset download:

```powershell
.venv\Scripts\python -m pip install -r backend\requirements-voice.txt
.venv\Scripts\python backend\scripts\setup_speech.py --download
```

Restart the backend afterward. Follow the [browser voice guide](docs/browser-voice.md),
[manual call guide](docs/manual-cellular-call.md), or
[automatic turn-taking guide](docs/automatic-cellular-call.md).

Call tests need both participants' consent. Automatic mode additionally needs
consent to sending **unreviewed** recognized text to Tinker. Use supervised
fictional calls only.

## Evaluate and verify

```powershell
.venv\Scripts\python -m pytest
.venv\Scripts\python -m ruff check backend
.venv\Scripts\python -m ruff format --check backend
npm run build
npm test
npm run format:check
```

Tests use fake providers and audio devices; they need no sponsor credit. Coverage
includes memory, grounded facts, approval expiry, stale replies, isolated sessions,
budget reservations, voice cleanup and automatic-loop stop conditions.

- [Delivery dataset](datasets/delivery/DATASET_CARD.md) and
  [evaluation guide](docs/evaluation.md): fictional multi-turn planner evaluation.
- [Training guide](docs/training.md): reviewed targets, bounded LoRA runs and exports.
- [Model selection](docs/model-selection.md): explicit backend configuration with no silent fallback.
- [Speech replay guide](docs/speech-replay.md): fixed audio-corpus word-error scoring.
  Shipped speech cases are recording plans; missing clips produce no accuracy score.

## Privacy, cost and limits

History lives in ignored `.data/guardmate.sqlite3`. Raw audio uses local temporary
files; submitted text and history persist locally and go to the hosted planner.
Secrets, weights, runtime recordings and checkpoints are excluded from Git. The
explicitly approved demo media above is intentionally included for public sharing.

The inference adapter has a persistent **$0.25 estimated reservation cap**, not a
provider billing limit. Training has separate accounting. Uncertain requests are
not automatically retried; do not reset the ledger to bypass limits.

This is a single-resident, loopback-only prototype without remote authentication.
Run one backend worker and do not expose it publicly. There is no automatic caller
verification, answering, hangup or barge-in. English checks and ASR can fail; it is
not ready for unattended real deliveries.

Third-party component and voice terms are in the
[speech guide](docs/browser-voice.md#sources-and-licences). Model details are in the
[Qwen model card](https://huggingface.co/Qwen/Qwen3.5-4B); hosted integration uses the
[Tinker sampling API](https://tinker-docs.thinkingmachines.ai/tinker/api-reference/samplingclient/).
