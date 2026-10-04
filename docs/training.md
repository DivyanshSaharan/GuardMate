# Bounded LoRA pilot

GuardMate now has an offline-first, train-only preparation command and an explicitly
opt-in hosted Tinker training runner. **No fine-tuning has been executed by this
increment.** The application still uses untuned Qwen3.5-4B. A checkpoint is not
automatically selected for production or treated as an improvement.

## Preview without spending

Install the pinned optional dependencies from `backend/requirements-ai.txt`. The
Qwen tokenizer must already be cached locally; this command does not download model
weights, read `.env`, initialize Tinker, or send an inference/training request.

```powershell
.venv\Scripts\python backend\scripts\train_delivery.py
.venv\Scripts\python backend\scripts\train_delivery.py --epochs 3 --output .data/training/pilot-preview.json
```

Use a new output filename: artifacts cannot be overwritten and must stay under
ignored `.data/training/`. Previewing draft seeds is allowed, but the artifact
explicitly records `review_ready=false`; it cannot be admitted for hosted training.
No live resident settings, saved conversations, or audio recordings enter this export.

The initial cached-tokenizer preview on October 4, 2026 produced 40 targets from 15
train scenarios. Three epochs at batch size 4 mean 30 logical optimizer updates,
251,868 conservatively counted full-sequence tokens and **$0.185640** of estimated
training-token reservations. This is an estimate for this tiny seed set, not an
account balance, a provider-enforced ceiling, or evidence of model quality.

The [official rate card](https://tinker-docs.thinkingmachines.ai/tinker/models/models_and_pricing/)
listed Qwen3.5-4B training at $0.737 per million tokens and checkpoint storage at
$0.10 per GB per month when checked October 4. Storage is excluded from token
reservations; the worker gives its two final checkpoints a finite 24-hour TTL.
Verify rates and the actual remaining promo balance before enabling paid work.

## What is trained

Only train-split courier turns from a fully passing authored-reference replay enter
the recipe. Validation/test targets are never tokenized into training datums. An
oracle replay is not a model baseline. The corpus is still draft: see the
[AI-assisted label review](training-label-review-2026-10-03.md) and
[dataset card](../datasets/delivery/DATASET_CARD.md).

The prompt is the production non-thinking Qwen chat template, including checked
facts, approval state, saved fictional context and actual reference dialogue history.
The completion is the final typed planner JSON plus EOS. All system/user/history
tokens have zero loss weight; earlier application-composed assistant replies are
context, not training targets. Tokens are shifted by one for next-token prediction,
as in the [Tinker datum contract](https://tinker-docs.thinkingmachines.ai/tinker/sdk-cheatsheet/).
Loss masking does not make context computation free: the estimate prices the whole
sequence, including a conservative extra EOS token per example.

Reference approval IDs are random application nonces, not semantic labels. Preparation
replaces only those fictional context IDs with deterministic UUIDs per scenario/request;
otherwise identical oracle replays would produce different fingerprints. It does not
alter courier text, saved policy fields, approval decisions or production approval IDs.

No target is truncated. Prompts over 12,000 tokens or completions over 512 tokens
are refused. This pilot fixes rank 16, learning rate 0.0001 and seed 42; supports
1–3 epochs, batch size 1–16 and at most 30 logical updates. Each epoch shuffles
deterministically. Dataset, prompt/schema, policy/training source, tokenizer
vocabulary/template, rendered tokens and schedule are bound into the job fingerprint.
The runner rebuilds the preview before admission; edited or stale jobs are refused.

## Review gate

All source seeds remain `draft`. AI-assisted consistency review is not human approval
or friend validation. Review the train labels and provisional fragile-item policy,
then explicitly mark only accepted train records `reviewed`. Do not promote held-out
labels to training or claim fictional expansions are observed courier transcripts.

Once review is complete, export a new job:

```powershell
.venv\Scripts\python backend\scripts\train_delivery.py --prepare --epochs 3 --output .data/training/reviewed-pilot.json
```

This refuses draft labels, failed reference replay, stale sources, and excessive
token/update schedules. It still sends no model requests. Forty targets constitute
a small pilot, not broad safety/generalization coverage. Permission checks remain
mandatory regardless of what a future model learns.

## Explicit hosted execution

**This command consumes Tinker credit. Do not run it until the labels and estimated
allowance have been approved.** Put the key only in ignored root `.env`, not in a
command argument, frontend variable or committed artifact.

```powershell
.venv\Scripts\python backend\scripts\train_delivery.py --live --job .data/training/reviewed-pilot.json --max-cost-usd 0.25 --max-updates 30 --ack-sdk-retries --price-checked-on 2026-10-04 --max-duration-seconds 900
```

The complete schedule must fit both explicit limits before allocation. All planned
batch reservations are persisted before starting the SDK subprocess. The separate
`.data/guardmate-training.sqlite3` ledger admits at most **$1.75** in cumulative
training-token estimates. Combined with the existing $0.25 inference ledger, the
planned adapter allowances stay within $2 of the quoted $10 promo, leaving $8 for
later challenges **in estimates only**. Storage, actual provider billing, other
clients and any changed prices are not covered. Do not delete ledgers to regain credit.

The worker sends each `forward_backward` and confirmed `optim_step` sequentially.
It performs no application-level retry or automatic paid validation. Tinker 0.32.0
has internal retries which cannot be disabled through supported training options;
`ServiceClient(max_retries=0)` is not a valid fix. The SDK/rate acknowledgement is
required because this is a **logical-work estimate limit, not a guaranteed invoice
ceiling**. The official [retry guidance](https://tinker-docs.thinkingmachines.ai/tinker/under-the-hood/)
also warns against stacking new request retries/timeouts on SDK retry behavior.

The isolated worker has an explicit overall local duration bound. If it times out,
fails or is interrupted, the run is `unknown`: killing the local worker does not
prove remote queued work was cancelled. Confirmed updates are recorded separately;
an uncertain last optimizer step is never automatically replayed or resumed.
Reservations are retained on failure. The same fingerprint cannot be resubmitted
with another run ID, even after completion; use a genuinely reviewed new experiment
configuration rather than rewriting files to evade an unknown run.

## Checkpoints and the next comparison

On complete success, ignored run artifacts retain the confirmed step count,
fingerprint, full optimizer-state path and distinct sampler-weight path. Full state
is for a future explicit resume; only `/sampler_weights/` is suitable for sampling.
Checkpoint saving does not establish local adapter export, local inference, or
deployment. Expired checkpoints cannot be assumed available later.

Explicit [tuned-checkpoint evaluation and matched comparison](evaluation.md) are now
implemented. Production selection is still separate. Compare base and tuned plans
under the same corpus, prompt, policy, dependency, decoding and scoring fingerprints;
reserve sampling separately. Report model-plan quality as well as checked behavior,
latency, failures and small sample counts. Do not describe the existing historical
base baseline as a matched experiment after policy/source changes, and do not claim
training benefit until a comparable measurement exists.
