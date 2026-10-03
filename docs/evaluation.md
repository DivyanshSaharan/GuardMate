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
Expected language-probe failures remain failures; do not weaken their rubrics to make the report green.

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
comparing base and tuned checkpoints; tuned-checkpoint selection is a later increment.

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

Draft seeds and unresolved reference/checker failures deliberately block this command. Review the
policy labels, fix application defects separately, then explicitly update review status. Exporting
validation/test cases as training is refused. The target is typed planner JSON, always including an
observation object, paired with the production prompt and actual reference replay history. Scenario
metadata/rationale stay outside the messages and are not model instructions. Masking/tokenization,
LoRA training, checkpoint export and a tuned-model comparison are not implemented by this command.

The dataset's current sources and review state must be reported honestly in the submission. Do not
claim a fine-tuning benefit until comparable base/tuned measurements exist.
