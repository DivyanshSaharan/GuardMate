# Explicit role-play model selection

The role-play backend defaults to untuned `Qwen/Qwen3.5-4B`. Optional checkpoint
selection now uses the same hosted Tinker provider, permission checks, conversations
and persistent $0.25 estimated-inference ledger. This does not implement local Qwen
inference, automatic checkpoint promotion or connected phone calls.

The first approved training/evaluation run is documented in the
[October 4 pilot report](pilot-2026-10-04.md). No tuned checkpoint is automatically
selected, and configuration alone cannot establish a fine-tuning benefit.
Label/policy approval and bounded paid experiments remain separate steps.

## Choose a checkpoint explicitly

First complete a reviewed training run and inspect its fresh
[matched evaluation](evaluation.md), including failures and small sample counts.
Then configure the backend-only variable in ignored root `.env`:

```dotenv
GUARDMATE_SAMPLER_CHECKPOINT=tinker://YOUR-RUN-ID:train:0/sampler_weights/YOUR-CHECKPOINT
```

This placeholder is not a real checkpoint. Use only the canonical `/sampler_weights/`
export, not the full optimizer-state `/weights/` path. Restart the backend after
changing configuration; there is no browser model-selection control or remote API.
Do not put the API key or this configuration in frontend/Vite variables.

An unset or empty variable selects base Qwen. An invalid nonempty path refuses
application setup rather than silently selecting base. Checkpoint loading and base-model
metadata verification happen on the first model turn, not on a status read, greeting,
session switch or resident decision. The provider must identify `Qwen/Qwen3.5-4B`
before tokenization, sample-cost reservation or generation. Missing, expired or
incompatible checkpoints pause the conversation for resident help; they never trigger
an automatic base-model retry. Failed sample reservations remain in the shared ledger.

The environment selection applies to the default role-play provider only. Evaluation
CLI targets remain explicit through `--checkpoint` or `--compare-checkpoint`; an
environment value cannot silently turn a base evaluation into a tuned one.

## Understand the status and action trace

The model card distinguishes **Base model**, **Sampler checkpoint**, and older/custom
providers with unspecified target identity. Checkpoint configuration is initially
**not yet verified**. A later verified flag means only that public provider metadata
matched the expected base-model identity. It is not checkpoint availability certification,
proof of training provenance, safety approval, or evidence of better performance.

Each new model turn records an immutable snapshot of provider, model, target kind,
checkpoint path and identity-verification state on its saved action event. A returned
plan is separate from the application action actually executed. **Plan unavailable**
does not mean that no paid request was sent: timeout and invalid-plan failures can occur
after submission. Identity records are local assertions, not signed provider receipts.

Application greetings, resident decisions and expiry notices do not get model provenance.
Legacy events remain readable without inventing their model identity. Changing the
current selection cannot relabel old events. A session continued after a configuration
change can contain different model identities; use a new fictional session for controlled
testing rather than treating mixed history as a matched comparison.

## Roll back without losing history

Remove or empty `GUARDMATE_SAMPLER_CHECKPOINT` and restart the backend. Base Qwen becomes
the explicit selection for subsequent turns; saved events keep their prior identity.
Do not delete conversations or budget ledgers to change models or regain allowance.
The trained checkpoints currently have a 24-hour TTL, so do not assume an old path
will stay available. No configuration change alters delivery permission checks or
the absence of remote resident authentication; keep this prototype on loopback.
