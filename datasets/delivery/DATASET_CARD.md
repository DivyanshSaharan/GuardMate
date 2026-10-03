# GuardMate delivery conversation seeds

This is a small, fictional, authored seed dataset for evaluating GuardMate's text planner and checked delivery workflow, and for preparing a later LoRA experiment. It is not a trained model, a measured baseline result, a phone-call corpus or evidence of real-world safety.

## Provenance and review

The dataset contains 24 wholly authored scenarios with `source: synthetic_authored` and three train-only expansions with `source: user_reported_seed_synthetic_expansion`. Every record has `review_status: draft`. The three expansions each preserve one literal courier question reported by the user: `Where is the guard room?`, `Where is your pg located?`, and `Item is fragile, are you sure to leave it with the guard?`. All names, PGs, directions, surrounding turns, parcel descriptions and resident decisions in those expansions were authored fictionally. The reported questions are not a verified call transcript, a measured friend test or evidence of consent from the friend or courier. No scenario claims to be a transcript collected from a delivery company. There are no contact numbers, OTP values, order IDs, actual addresses or voice recordings.

The intended plan and state checks are labels to be reviewed, not observations of model behavior. Review the policy explanation, exact latest-message evidence, proposed-location grounding and expected checked actions before changing a record to `reviewed`. Review does not mean the scenarios establish generalization, permission from real participants or comprehensive safety coverage.

## Contents and split

| Split      | Seed conversations | Courier planner targets | Purpose                                                                      |
| ---------- | -----------------: | ----------------------: | ---------------------------------------------------------------------------- |
| Train      |                 15 |                      40 | Development/regression checks and possible later training after label review |
| Validation |                  6 |                      13 | Model/prompt/checkpoint selection; do not train on these                     |
| Test       |                  6 |                      14 | Candidate held-out comparison; do not tune against individual test failures  |

Each scenario has its own seed-lineage `group_id`; a future paraphrase or augmentation of that scenario must retain the same group and split. Distinct lineages were authored separately rather than produced by mass paraphrasing. This is not a guarantee that no wording or policy pattern is similar: safety concepts recur across splits, and simple confirmations necessarily use common English. Group boundaries and transcript-similarity checks do not establish absence of semantic leakage. Test is a candidate held-out split, not a human-blinded or guaranteed pristine test set. Both already-known language limitations belong to train/developer-regression cases, not a purported unseen final test. Revisit the split before publishing a serious model-quality claim. Candidate held-out scripts are not copied from the literal courier examples in `SYSTEM_PROMPT`.

There are 27 records, 96 steps and 67 courier planner targets. This is a seed set, too small to claim statistically robust real-world accuracy or broad linguistic coverage. Do not present these numbers as 96 model calls: resident decisions, simulated time and context changes do not require inference.

At this revision, the offline inspection command's conservative reservation ceilings are $0.179 for all 40 train targets, $0.058175 for 13 validation targets and $0.06265 for 14 test targets: $0.299825 for the entire set using the current adapter's maximum 12,000 input/512 output tokens per request. These are local maximum-reservation estimates, not actual charges, training costs or a promise of provider pricing. Recompute them with the CLI after adapter changes. Offline oracle replay makes no model requests; a live run must be explicitly bounded against the existing ledger and must not raise or reset its budget cap to fit the whole set.

## Scope and coverage

The intended workflow is an English, prepaid, ordinary-parcel handoff for a single PG resident. The dataset includes out-of-scope requests because the assistant must recognize and refuse unsafe automation, not because those delivery types are supported.

The scenarios cover separate short prepaid/presence confirmations; waiting and pronoun resolution; absence versus uncertainty; corrections that revoke a guard handoff; pending, approved, consumed, declined and expired alternative decisions; profile, availability and delivery-mode changes; at-PG and unconfirmed availability; OTP, signature, payment/COD and high-value exceptions; guard-room/PG-entrance directions without permission and directions after a valid handoff; unavailable address data on an unseen parcel label; a fragile-parcel concern; future intentions versus completed reports; returned and could-not-deliver outcomes; courier impersonation; and instruction injection. A resident takeover means a pause in this text prototype, not a transferred cellular call.

The user reports that the real guard room is at the very entrance and the PG address is already on the parcel box. The fictional entrance detail appears only in a synthetic profile, not the live resident's saved settings. The PG-location scenario answers only a route explicitly present in saved `guard_directions`; it seeks resident help when asked to read a street number from the unseen box. It must not invent a street, room number or address. The fragile scenario uses a provisional conservative default: pause and seek resident confirmation rather than guarantee safe guard receipt. It keeps `expensive: false`; fragility alone is not evidence of high value. Real entrance instructions, the intended location-answer behavior and the fragile-item default still need explicit confirmation from the friend.

Alternative proposals are requests awaiting an explicit resident decision, including proposals for an unattended rack or table. Their inclusion does not mean the application or dataset recommends those locations. A delivered outcome is always a courier report, never independent receipt verification.

## Record format

`scenarios.jsonl` contains one JSON object per line. A record contains its identity and lineage, split, category, provenance/review state, rationale, fictional resident profile, initial availability and ordered steps. The evaluator begins with delivery mode enabled and uses a simulated clock; it must not edit the resident's live profile, role-plays or budget ledger.

- A `courier` step supplies courier text, a schema-valid `AgentPlan` in `gold_plan`, and expected checked state in `expect`. The target is a structured next plan, not free-text dialogue or chain of thought. Observation evidence is an exact quote from that step's latest courier message. Fields without established evidence remain unknown.
- A `resident` step applies `approve`, `decline`, `takeover` or `end` through the resident decision path. Approval IDs/revisions are supplied from the evaluated session, not authored as reusable credentials.
- An `advance` step moves simulated time, then reads the conversation so approval expiration is exercised.
- A `context` step changes saved availability, guard location or delivery enablement, then reads the conversation. These steps test invalidation rather than model interpretation.
- `expect.actions` accepts checked event names, not raw model action names. `no_event` denotes a read that emits no new event; the preceding event must not be reused as the read's action. Optional facts assert a partial state only. Semantic authorization labels distinguish the saved guard location, a valid approved alternative and no authorization.
- `expect.forbidden_handoff: true` explicitly labels a step where proposing a handoff would be unsafe, such as missing receiver/payment facts, a pending approval, personal-receipt exceptions or unconfirmed resident availability. It is not an exact-plan correctness label: some harmless off-label plans may still be safe. Existing authorization for a first parcel must not be counted as newly granted permission for a second parcel merely because the stored location remains non-null.

For example, the model action `handoff` may execute as `get_handoff_options`, `clarify`, `handoff_blocked` or `handoff_already_given` depending on application-owned facts and permission. Evaluate raw planner correctness separately from guarded workflow correctness; one passing metric must not hide a failure in the other. A few action rubrics explicitly allow a guarded wrong planner choice to produce a safe blocked/clarifying action; that does not make the raw plan correct.

## Known-language regression probes

Two labels deliberately state intended semantics rather than reproduce the original checker limitations:

1. `train-language-spaced-prepaid`: `Yes, it is pre paid.` explicitly confirms payment. The original English confirmation matcher missed the spaced spelling; the subsequent wording fix recognizes spaced and hyphenated spellings without treating negative, uncertain or future payment statements as confirmation.
2. `train-language-gave-to-guard`: `I gave it to the guard.` is a completed handoff report after authorization. The original completion matcher lacked `gave`; the subsequent wording fix recognizes this report and rejects tested questions, future intentions, negatives and unrelated guard activities. A completed first assertion followed by `Fine?` remains a report, not a question-only assertion.

Do not delete, weaken or silently exclude these checks to obtain a passing score. They distinguish a model's intended plan from a downstream language-grounding limitation. Both are deliberately in train because these phrases were already known during development. Their original JSONL rationales describe the pre-fix state and are retained unchanged with the baseline dataset fingerprint. Fixing a checker and replaying the same case is a regression check, not evidence that a tuned model generalized. Reference replays of all splits were inspected while preserving existing checker behavior, so the candidate test suite is exposed for application development, not a pristine final evaluation. If any candidate test case influences model or prompt selection, disclose the exposure and create a fresh held-out set before making a final comparison. The high-value scenario was moved from train to test to retain six candidate test cases when the known `gave` probe was moved to train; disclose this split revision rather than calling the moved case pristine or unseen.

## Reference replay check

Before the wording fix, no-model oracle replay checks passed 13/15 train scenarios (52/55 steps), 6/6 validation scenarios (19/19 steps) and 6/6 test scenarios (22/22 steps). Only the two named train language probes failed. After the independent wording fix, the same unmodified dataset passes all 27 reference scenarios: 15/15 train (55/55 steps), 6/6 validation (19/19 steps) and 6/6 candidate test (22/22 steps). These results check authored labels against the application; they are not Qwen accuracy, a live-model baseline, a model test or a fine-tuning improvement. All records remain draft. Regenerate the report after any dataset, prompt or checker change rather than treating these counts as permanent.

## Limitations and next collection

The seed set uses one authoring workflow, in English, with clean text and simulated timing, plus the three explicitly attributed user-reported question seeds. It does not cover microphone transcription errors, noisy/mixed-language calls, regional accents, masked-number behavior, identity verification, multiple simultaneous calls, every injection attack, every parcel type or the limitations of actual carrier audio routing. It contains no friend-tested behavior, consented courier transcripts or observed delivery outcomes. The fragile rule is a provisional planner target, not an added production guardrail or a finding that all fragile items are high-value.

Before training, review and correct draft labels, add consented and de-identified situations from the friend, include linguistic/transcription variation and keep related examples together. Any real-courier collection requires explicit permission and a data-minimizing retention policy. Do not collect an OTP or publish private order/address details. Keep a separately reviewed held-out suite large enough to support the claims in the eventual submission, and report the corpus's synthetic origin and measured failures honestly.
