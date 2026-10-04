# Delivery dataset and baseline evaluation

This increment supplies a reproducible text-conversation benchmark, **not a trained model**.
The default command makes no model requests, loads no API key and does not touch the live
resident's preferences or conversations.

## Seed data

`datasets/delivery/scenarios.jsonl` contains independently authored fictional seed situations.
Each line records provenance, review status, split, seed lineage, a resident profile, courier
turns, resident decisions, expected checked actions/state and reference planner targets.
The dataset card explains coverage and deliberate language probes. These are not actual courier
recordings, friend-collected evidence or a statistically representative evaluation.

Splits are assigned to seed groups, not random paraphrase rows. The loader rejects IDs repeated
anywhere, a seed group appearing in multiple splits, exact normalized courier traces shared
between splits, incomplete profiles, unknown fields and ungrounded reference observations.
Cross-split transcript similarity above 0.85 is flagged for review, not claimed as proof that
semantic/template leakage is absent. Avoid generating training variants from validation/test seeds.

The `test` split is reserved for a frozen final comparison. Use `validation` for development and
checkpoint selection. Synthetic cases must later be complemented by independently authored,
unscripted human role-plays. Ask the friend for actual recurring questions without collecting
private addresses, order IDs, OTPs or recordings without consent.

## Validate and estimate, free

From the repository root:

```powershell
.venv\Scripts\python backend\scripts\evaluate_delivery.py
```

The default split is validation. The command prints counts, split/provenance checks, similarity
warnings and a conservative maximum reservation estimate using the existing adapter's limits.
It is not a sampling run and does not report baseline success.

## Check reference plans, free

```powershell
.venv\Scripts\python backend\scripts\evaluate_delivery.py --oracle --split validation --output .data/evaluation/validation-reference.json
```

`--oracle` feeds the authored reference plans through the real conversation engine in temporary
databases. It checks rubric/engine behavior and exposes policy/checker limitations. It is explicitly
labelled **not a model**, and oracle percentages/latencies must not be reported as Qwen performance.
The original two language-probe failures were fixed independently in the application checker;
the unchanged reference suite now passes all 27 scenarios. Do not weaken rubrics to make a report
green or report this checker regression result as a model improvement. The original real-model
baseline remains historical; rerun base and tuned models under the same checker for a comparison.

## Run an explicitly bounded real baseline

The optional AI requirements and backend-only `TINKER_API_KEY` are the same as the text role-play.
This command consumes hosted inference credit:

```powershell
.venv\Scripts\python backend\scripts\evaluate_delivery.py --live --split validation --limit 2 --max-calls 6 --max-cost-usd 0.03 --output .data/evaluation/base-validation.json
```

Both `--max-calls` and `--max-cost-usd` are required. Each attempt is admitted only if its worst-case
reservation ($0.004475 at the current adapter limits) fits the run allowance. Failed attempts remain
admitted, and there are no retries. The underlying Tinker adapter still uses the existing persistent
`.data/guardmate.sqlite3` budget ledger and its cumulative $0.25 cap. Evaluation conversations use
separate temporary stores: the user's session history/preferences are not changed. Never use a new
temporary budget ledger or delete the existing ledger to regain allowance.

The run stops spending after a provider failure or budget stop. Skipped scenarios, rejected steps,
model-unavailable responses and incomplete scenarios remain visible. Shared-ledger change may include
simultaneous UI activity and is an estimate, not provider billing. Current fixed rates are implementation
estimates; confirm provider pricing again before training or materially increasing the allowance.

Reports and exports must remain under ignored `.data/evaluation/`; existing artifact files are never
overwritten. Use a new descriptive filename for each run. Record the dataset, prompt, plan-schema and
production-source hashes before comparing results. Do not compare different seed sets as if they were
the same benchmark. Keep the model, prompt, policy, decoding settings and replay procedure fixed when
comparing base and tuned checkpoints. Explicit sampler-checkpoint evaluation and matched
development comparisons are now implemented below; [role-play selection](model-selection.md)
is a separate, explicit backend configuration.

## Evaluate a sampler checkpoint

Use the `/sampler_weights/` path from a successfully completed training run. A full
optimizer-state `/weights/` path is deliberately refused, even though other SDK workflows
may support it. Native IDs such as `session-id:train:0` are supported. Queries, fragments,
spaces, empty segments and traversal are not accepted. No missing/expired/incompatible
checkpoint silently falls back to the base model.

```powershell
.venv\Scripts\python backend\scripts\evaluate_delivery.py --live --checkpoint "tinker://YOUR-RUN-ID:train:0/sampler_weights/YOUR-CHECKPOINT" --split validation --max-calls 13 --max-cost-usd 0.06 --output .data/evaluation/tuned-validation.json
```

This is an **explicitly paid sampling command**, not training. The placeholder is not a
real checkpoint. The sampler's public metadata must identify `Qwen/Qwen3.5-4B` before
tokenization, sample-cost reservation or generation. The integration follows the official
[sampling metadata](https://tinker-docs.thinkingmachines.ai/tinker/api-reference/samplingclient/)
and [checkpoint client](https://tinker-docs.thinkingmachines.ai/tinker/api-reference/serviceclient/)
interfaces. Metadata lookup/client setup may wait on the SDK; the call/cost allowance is
not an overall wall-clock deadline or guaranteed provider invoice ceiling.

The application still defaults to untuned Qwen. This command neither changes the resident's
preferences nor switches production to the checkpoint. A user-supplied path and base-model
name are not proof of the training corpus, provenance, local export, or fine-tuning benefit.
There is no completed trained checkpoint or new paid evaluation result from this increment.

## One shared allowance for a matched pair

Run both targets freshly against the same selected validation or exposed development-test
scenarios. Training-split improvement comparisons are refused. On the current six-scenario
validation split, each arm has 13 courier turns; both require 26 admitted worst-case calls,
or $0.116350 under the current limits. Actual token reservations may be lower. Check the
remaining shared $0.25 inference allowance before running; do not delete or replace its ledger.

```powershell
.venv\Scripts\python backend\scripts\evaluate_delivery.py --live --compare-checkpoint "tinker://YOUR-RUN-ID:train:0/sampler_weights/YOUR-CHECKPOINT" --split validation --max-calls 26 --max-cost-usd 0.12 --base-output .data/evaluation/matched-base.json --tuned-output .data/evaluation/matched-tuned.json --output .data/evaluation/matched-comparison.json
```

Both explicit limits cover **the entire pair**, not one allowance per model. Preflight
requires enough allowance for both complete arms and checks the persistent inference cap.
Each actual sample still uses the existing ledger. Concurrent UI sampling can consume
remaining credit and interrupt an arm; it does not increase the cap. Failures stay reserved.
No training ledger is reset or consumed by this command.

The base arm runs first. If it lacks complete attempted model-plan evidence, the tuned arm
is not started: its artifact explicitly says `not-run-NO-INFERENCE`. If the tuned arm fails
or the reports cannot be matched, its individual report remains diagnostic and the comparison
artifact says `comparable=false`. Provider/corpus/source failures are not successes, and
missing turns are not dropped to improve a percentage. There is no automatic paid retry,
resume, winner declaration or model promotion.

## Compare saved reports for free

```powershell
.venv\Scripts\python backend\scripts\evaluate_delivery.py --compare-reports .data/evaluation/matched-base.json .data/evaluation/matched-tuned.json --output .data/evaluation/saved-comparison.json
```

Offline comparison does not load `.env`, open a budget ledger, read the dataset again,
download model files or initialize Tinker. Input/output reports must be `.json` under
ignored `.data/evaluation/`; use new output filenames. Historical reports without the new
binding, oracle outputs, train reports, partial runs and mismatched reports are refused.
Keep the historical untuned baseline unchanged; it cannot be relabelled a matched pair.

Eligibility binds the corpus hash, selected scenario content/order, split, system prompt,
plan schema, production/evaluation source hashes, actual installed dependency versions and
decoding settings. Explicit base/tuned identities are separate from their shared binding.
Sources/environment are checked before and after replay, and the command refuses corpus
drift. Serialized metadata is an assertion, not signed proof that a provider was called.

Comparison recomputes metrics from complete step rows rather than trusting saved aggregate
percentages. It reports sample counts, base/tuned values and deltas for checked success,
strict plan agreement, annotated unsafe proposals/authorizations and text-model latency,
plus paired per-scenario outcomes. Negative deltas are retained; verified receipts remain
unavailable. No arbitrary pass percentage automatically qualifies a checkpoint for deployment.

These are small synthetic development probes. Candidate test examples are already exposed,
not a pristine final holdout. Model-conditioned histories can diverge; scripted short answers
may become unnatural when an earlier response differs. Sampling at temperature 0.2 without
a fixed sampling seed is nondeterministic. Serial base-first order, warm-up, caching and hosted
load confound latency; a single pair does not establish causal speedup or statistical benefit.
Review failures and test finalists with adaptive human role-play before any real unattended use.

## What the metrics mean

- **Model-plan rubric match:** action, relevant parameters and all observation facts agree with
  the authored next plan; evidence for updates must quote the current courier message. A correct
  checked reply can still conceal a wrong model plan. This strict reference match is a diagnostic,
  not a claim that only one natural conversation strategy is acceptable.
- **Checked-step/scenario success:** executed actions and annotated facts, permission, approval,
  pending question, status and outcome match the rubric. An unavailable model is never counted
  as task success just because the agent paused safely.
- **Forbidden handoff proposals/authorizations:** only explicitly annotated forbidden-handoff
  steps are eligible. A new authorization event is counted at that step, even if later revoked;
  retaining an old location is not a new permission. Off-label but safe choices are quality errors,
  not automatically unsafe actions. These limited synthetic counts do not establish real-world safety.
- **Latency:** model/policy text processing, with sample count and nearest-rank p50/p95. This is
  not end-of-speech-to-audio latency. Approval time is simulated through explicit advance/context
  steps; actual provider latency does not consume a simulated approval window.
- **Outcome:** courier-reported delivered is separate from rubric success. Verified receipt is
  unavailable, not zero or implied by a model reply.

Some script turns such as “yes” depend on the actual preceding response. Replay never forces the
gold state/reply into a real model conversation. If a model asks a different question, later scripted
answers may be unnatural; include these failures in review and validate finalists with adaptive humans.

## Prepare supervised examples (not training)

Only reviewed `train` seeds with a completely successful reference replay may be exported:

```powershell
.venv\Scripts\python backend\scripts\evaluate_delivery.py --oracle --split train --export-training .data/evaluation/train-targets.jsonl
```

Draft seeds and any unresolved reference/checker failures deliberately block this command. Review the
policy labels, fix application defects separately, then explicitly update review status. Exporting
validation/test cases as training is refused. The target is typed planner JSON, always including an
observation object, paired with the production prompt and actual reference replay history. Scenario
metadata/rationale stay outside the messages and are not model instructions. Supervised export
itself does not tokenize, train LoRA or save checkpoints. Matched model comparison uses the
separate command modes described above.

The separate [training workflow](training.md) now implements completion-only tokenization,
offline cost previews and explicitly admitted LoRA execution. It does not run automatically
from evaluation, and draft labels still block prepared/live jobs. Tuned-checkpoint comparison
is implemented above; role-play selection is explicitly configured separately and never
automatically changed by evaluation.

The dataset's current sources and review state must be reported honestly in the submission. Do not
claim a fine-tuning benefit until comparable base/tuned measurements exist.
