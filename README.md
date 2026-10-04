# GuardMate

A personal AI delivery assistant built for a friend who lives in a PG. The intended voice agent talks to couriers, follows approved guard-room instructions and asks for help when a handoff needs the resident's decision.

## Current functionality

GuardMate saves the resident's PG instructions, weekly office routine, daily availability override and expiring delivery-mode window. Preferences are stored in a local SQLite database and survive application restarts. Schedule calculations use Asia/Kolkata; daily overrides expire at local midnight.

The text role-play panel now runs Qwen3.5-4B through Tinker. The model plans each next conversational action using the full dialogue, parcel facts, fresh availability and resident approval state. The application validates the plan, checks permission and returns grounded instructions. Conversation history and action traces persist locally. There is no scripted model fallback in production.

Short answers are grounded against the outstanding question before the model sees the updated conversation. “Don't know” and “let me check” preserve that question and get a waiting acknowledgment, not another copy of the question. Known facts cannot be erased or re-asked by an omitted model observation or stale clarification. Old role-play sessions recover clear answers from their saved transcript on the next turn. The trace distinguishes Qwen's proposed action from the application action actually executed.

The English grounding checks recognize `pre paid` and `pre-paid`, including tested negative and uncertain forms. Positive model observations must also be supported by the surrounding assertion, not a quoted positive word stripped of its negation or uncertainty. After an authorized handoff, `I gave it to the guard.` can record a courier-reported delivery; tested future plans, questions, negated reports and unrelated guard activities cannot. These are limited application-checker regressions, not model training or general language-understanding guarantees.

Sending immediately clears the composer, displays the courier's outgoing message and shows an in-dialog waiting indicator. Sending is locked until the request completes; failure restores the draft and flags uncertain delivery rather than automatically retrying. Typing stays local to the composer and does not rerender the transcript.

The high-contrast **End role-play** stop button sits beside **Start role-play** above the chat, including on narrow screens. Ending keeps saved history and ends only the selected role-play.

Multiple couriers now have separate named role-plays. Enter a fictional **New courier label** and start another without ending the first. Use **Saved role-play** to return to either conversation. UUIDs, not labels or phone numbers, isolate each transcript, parcel facts, approvals, handoff permission, turn limit and outcome. Duplicate labels still create separate IDs. Labels are local test metadata, never caller verification, resident authority or model instructions.

The selector loads the latest 50 saved session summaries (API limit 1–100), without transcripts or parcel facts. Previously selected or newly opened sessions remain available in the current panel. Old unlabeled sessions use their short ID as a display name and remain readable without rewriting the database. The browser remembers the selected ID across reloads; each courier's unsent draft stays separate in memory while the panel remains open. Switching always reads that session's fresh saved state. Sends, starts and switching are serialized in this single-panel prototype; switching is disabled while a response is pending. Late responses and stale polls cannot replace another courier's conversation.

Pending approvals in unselected sessions appear in the selector. Session-list reads and approval polls expire stale approvals without model requests. Creating, listing, switching and ending sessions do not invoke Qwen or consume inference credit. The existing delivery-mode requirement still applies when starting a new role-play.

Automatic caller identification, call-to-session routing, multi-device resident authentication and parallel live call processing remain future increments. A real call must get a new session per call/delivery; a masked or repeated telephone number must not be used to reuse parcel facts or approval.

Alternative locations require an explicit, single-use resident approval. Approvals expire after 90 seconds and are invalidated when resident settings change. Courier claims such as “I am the owner” cannot grant approval. OTP, signature, payment and high-value exceptions pause the agent. Delivery outcomes are labelled **courier-reported**, never verified receipt.

The role-play now has opt-in browser speech: record up to 30 seconds, review local Whisper transcription before sending, and explicitly play the latest checked reply with local Piper. Recording/playback are mutually exclusive and stay isolated across sessions. Speech workers use temporary audio, bounded CPU jobs and no model API credentials. Qwen planning remains hosted and untuned. See [browser voice setup and measured limits](docs/browser-voice.md).

Automatic call answering and transfer are not implemented. The browser remains a role-play interface; an optional manually answered Windows call runner is described below.

A separate [Windows cellular-audio probe](docs/cellular-audio-probe.md) now inspects
explicit phone audio endpoints and provides opt-in, bounded recording/playback for
a consenting test call. On a vivo T2x 5G, the user confirmed caller audio in a
five-second WDM-KS recording and reported the identifiable phrase and final number
from synthetic transmission. Directions were tested separately; the standalone
probe did not establish simultaneous duplex, full-phrase intelligibility or
autonomous call handling.
The probe never falls back to the laptop microphone/speaker or invokes Qwen.

An [operator-controlled cellular conversation runner](docs/manual-cellular-call.md)
now connects the selected phone audio route to local Whisper transcription, the
existing checked Qwen conversation and local Piper replies. Each run creates a
fresh session. Transcripts need review and an explicit send; call answer/hangup
remain manual. Hosted transcript/context consent, an active delivery window and
fresh reply checks are required. Offline tests cover the integration. In the
[first consenting live call](docs/live-cellular-test-2026-10-04.md), the caller
confirmed the full greeting and three checked replies from actual base-Qwen turns.
Speech recognition remained unreliable, and an ambiguous outcome was rejected
rather than recorded as delivery. Unattended operation is not implemented; further
hosted/live tests require fresh agreement.

An offline-first LoRA workflow now previews completion-only, train-split planner targets
and estimates the full schedule before any hosted work. Reviewed labels and explicit
cost/update/retry acknowledgements are required for training. Persistent reservations
and isolated execution preserve uncertain runs without replaying them. No fine-tuning
has run yet; see the [training guide](docs/training.md).

Evaluation can now load an explicit sampler checkpoint, verify its base-model identity
and compare freshly matched base/tuned development replays using one shared inference
allowance. Saved reports can also be compared offline; incomplete, historical, oracle
or mismatched reports cannot become improvement claims. Evaluation does not switch production,
and no tuned-model results exist yet. See the [evaluation guide](docs/evaluation.md).

Role-play model selection is now an explicit backend-only configuration. Base Qwen remains
the default; an optional sampler checkpoint has separate selection/identity-verification
status, with no silent fallback. New model turns save their own provider/checkpoint identity
in the action trace, so changing configuration cannot relabel historical turns. See the
[model-selection guide](docs/model-selection.md). No checkpoint has been selected here.

## Run locally

Use Node.js 22.12+ (or a newer supported Node release) and Python 3.13+. The current setup was tested with Node.js 26 and Python 3.13 on Windows.

From the repository root:

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r backend\requirements-dev.txt
npm install
```

Start the backend in one terminal:

```powershell
.venv\Scripts\python -m uvicorn guardmate.main:app --app-dir backend --host 127.0.0.1 --port 8765
```

Start the frontend in a second terminal:

```powershell
npm run dev
```

Open <http://127.0.0.1:5173>. API documentation is available at <http://127.0.0.1:8765/docs>.

On Ubuntu, create the virtual environment with `python3 -m venv .venv` and use `.venv/bin/python` in place of `.venv\Scripts\python`. A venv should be created independently for each operating system rather than shared between Windows and WSL.

## Verify

```powershell
.venv\Scripts\python -m pytest
.venv\Scripts\python -m ruff check backend
.venv\Scripts\python -m ruff format --check backend
npm run build
npm test
npm run format:check
```

Tests cover persisted preferences, office-time boundaries, configurable office days, local-midnight override expiry, delivery-window expiry and invalid settings. Automated tests need no model API key or sponsor credit.

Agent tests additionally cover multi-turn memory, missing/ungrounded facts, resident-only approvals, timeout, stale decisions, profile changes during model inference, unsafe handoffs, future intentions versus reported outcomes, and persistent budget reservations. Provider contract tests run when the optional AI dependencies are installed; they use a fake sampler and do not send API requests.

Frontend regression tests cover section render isolation, scoped pending controls, draft preservation, notification dismissal, unchanged polling snapshots and stale-response protection. The dashboard is composed of separate availability, delivery-mode, instruction-preview and preferences components. Form drafts, delivery-window edits and clipboard feedback stay local to their sections. Notifications render in a fixed-position portal, so appearing or disappearing messages do not move the page content.

## Data and configuration

Runtime data is saved to `.data/guardmate.sqlite3`, which is excluded from Git. Set `GUARDMATE_DATA_DIR` to choose another storage directory. Dependencies, runtime caches, secrets, model weights and checkpoints are also excluded from Git.

The dashboard is intended for the local resident and both development services bind to loopback by default. Resident commands trust the local UI; there is no remote resident authentication. Do not expose either service publicly. The command lock is process-local: run one backend worker for this prototype.

## Test the real model

Install the optional tokenizer and hosted inference dependencies:

```powershell
.venv\Scripts\python -m pip install -r backend\requirements-ai.txt
```

Copy `.env.example` to `.env` and enter your own `TINKER_API_KEY` there. Restart the backend. The key is read only by the backend and `.env` is ignored by Git; never put it in frontend code or a Vite environment variable.

Save your preferences, enable a delivery window and open **Try a delivery conversation → Open text role-play**. Use fictional data. You play the courier; the **Your decision** panel is the resident's separate approval channel. The panel shows the transcript, executed actions, model/policy latency and estimated usage reserved. Reloading restores the role-play ID from browser storage and its conversation from SQLite.

Hosted inference is **not offline or private to your laptop**: saved resident context and role-play messages go to Tinker. Only Qwen tokenizer files are downloaded locally, not the Qwen weights. Optional speech models run locally; local Qwen inference remains a later milestone.

Each model request is limited to 12,000 input tokens and 512 output tokens. A SQLite ledger reserves estimated worst-case cost before submission and refuses requests above a cumulative **$0.25 test-stage cap**. Failed/timed-out requests remain reserved, and invalid plans are not automatically retried. This is a conservative token-price estimate, not your account's actual billing balance or a provider-enforced spending limit. It covers this adapter only; training and other clients are not included. Do not delete the ledger to bypass the cap.

A reproducible live smoke test uses fictional data and the same budget ledger:

```powershell
.venv\Scripts\python backend\scripts\smoke_agent.py --live
.venv\Scripts\python backend\scripts\smoke_dialogue_regression.py --live
```

The `--live` flag explicitly enables credit-consuming requests. The generated report is saved to ignored `.data/qwen-smoke-report.json`. The initial untuned smoke check completed four scenarios: ordinary prepaid handoff, resident-approved alternative with a rejected courier impersonation, OTP escalation and no automatic handoff for unknown availability. The alternative scenario needed one redundant clarification. Warm observed model/policy turns were approximately 2–3.2 seconds; cold startup was approximately 11 seconds. These are small smoke-test observations, not a frozen evaluation or evidence of fine-tuning improvement.

The dialogue regression separately replays the reported repeated-question sequence and a pronoun-based confirmation scenario against real Qwen. Its fictional-data report is saved to ignored `.data/qwen-dialogue-regression.json`. It shares the same cumulative budget ledger and does not modify saved resident preferences.

## Boundaries of this prototype

The model produces a typed dialogue plan, not arbitrary courier-facing text. Permission-sensitive replies are composed from saved facts and checked actions. Positive parcel/guard observations need an exact courier quote plus a conservative English confirmation check. Such a quote is not proof the courier is telling the truth. The English checks can reject valid phrasing or miss unusual phrasing; the model is also fallible. Broad adversarial, speech and real-device tests are still required before automatic live use. Approval timeout is checked by the backend, not by a browser timer.

## Dataset and baseline evaluation

The repository now includes fictional multi-turn delivery seeds and an offline-first evaluation
runner. It validates provenance/split boundaries, replays authored reference plans separately from
real model inference, and scores Qwen's proposed plans separately from application-checked replies.
Reference replay is **not a model baseline**. Draft/unresolved data cannot silently become training
targets. Live sampling requires explicit call/cost limits and shares the existing $0.25 ledger.

Start free with `.venv\Scripts\python backend\scripts\evaluate_delivery.py`.
See [the evaluation guide](docs/evaluation.md) and [dataset card](datasets/delivery/DATASET_CARD.md)
for commands, provenance, wording regression probes, metric definitions and training-export boundaries.

The first real, untuned [validation baseline](docs/baseline-2026-10-03.md) completed six scenarios
and 13 model turns: 4/6 checked scenarios passed and 7/13 plans matched the strict reference rubric.
These are small draft-seed development results, not fine-tuning improvement or field effectiveness.
The [cellular preflight](docs/cellular-feasibility.md) documents why the current WSL setup is not
ready for the two-way phone-audio proof; no driver/pairing setup was performed.

## Planned AI stack

- Qwen3.5-4B with supervised LoRA training through Tinker (currently using the untuned base model).
- whisper.cpp for local speech recognition and Piper for local speech generation.
- Validated tools for handoff instructions, approval requests and caller-reported outcomes.
- A manual browser voice interface for independent agent testing (continuous streaming remains future work).
- Windows Phone Link with pinned WDM-KS audio for the operator-controlled cellular test runner. One integrated call is demonstrated; answering and hangup remain manual, and speech recognition needs improvement.

Ubuntu in WSL supports development. The current cellular experiment runs on Windows, not WSL. A future Linux/BlueZ route would require compatible Bluetooth hardware exposed to Linux and its own two-way audio proof.

The integration follows the [Tinker sampling API](https://tinker-docs.thinkingmachines.ai/tinker/api-reference/samplingclient/) and the [Qwen3.5 model card](https://huggingface.co/Qwen/Qwen3.5-4B). Budget estimates use the published [Tinker model pricing](https://tinker-docs.thinkingmachines.ai/tinker/models/models_and_pricing/).
