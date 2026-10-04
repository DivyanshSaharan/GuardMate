# Train-only policy and label review — October 3, 2026

Historical review state: the findings below describe the original all-draft
corpus. On October 4 the project owner explicitly approved the 15 fictional train
records, including resident confirmation for fragile-parcel concerns. Only their
review flags changed; see the [pilot report](pilot-2026-10-04.md). This approval is
not friend validation, and validation/test records remain draft.

This is an **AI-assisted policy/label review**, not human approval, friend validation,
a live-model evaluation, or evidence from real delivery calls. No source labels or
review-status flags were changed by this review. **All source seeds remain draft.**

The review inspected only the 15 `train` scenarios and their 40 courier planner
targets. Validation/test individual examples were not inspected for this review.
Offline reference replay passed 15/15 scenarios and 55/55 total steps, with no
model requests, API-key access, or inference-credit use. Replay establishes that
authored plans and state expectations agree with the current application; it does
not establish model accuracy or generalization.

Dataset SHA-256:

```text
586c943e45521fc3ecd558679dffa0a0634889e9723d154dc8827e1a74fc7a51
```

## Per-scenario findings

| Train scenario                       | Policy/label result                        | Grounding and caveats                                                                                                                                                                                                                                                        |
| ------------------------------------ | ------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `train-short-confirmations`          | Pass                                       | Short affirmatives resolve against separate pending payment and guard questions. The delivered report follows authorization and remains unverified.                                                                                                                          |
| `train-wait-pronoun`                 | Pass                                       | Unknown/checking statements preserve the guard question. The later pronoun confirmation resolves guard presence without re-asking established payment.                                                                                                                       |
| `train-guard-correction-alternative` | Pass                                       | Explicit guard absence withdraws prior authorization. The quoted alternative needs UI approval, consumed once. The second-parcel approval-status answer refuses approval reuse while preserving the first parcel's completion; it does not provide multi-parcel support.     |
| `train-pending-no-bypass`            | Pass                                       | A later guard-presence statement does not bypass an existing pending resident decision.                                                                                                                                                                                      |
| `train-resident-declines`            | Pass                                       | The quoted shoe-rack proposal requests a resident decision; no unattended handoff is granted. Inclusion does not recommend that location.                                                                                                                                    |
| `train-approved-timeout`             | Pass                                       | The quoted laundry counter needs approval. Simulated expiry pauses the agent without renewal. Only the opening courier plan is an SFT target; approval expiry itself is application behavior.                                                                                |
| `train-weekend-personal-receipt`     | Pass                                       | Saved `at_pg` availability is answered cautiously; the explicit security-receipt request needs personal confirmation. Replay uses an availability override, not an actual weekend clock.                                                                                     |
| `train-unconfirmed-availability`     | Pass                                       | Directions remain navigation only. An explicit receiver-decision request under `ask_me` appropriately invokes resident help.                                                                                                                                                 |
| `train-otp-revealed-late`            | Pass                                       | Explicit OTP disclosure remains sticky and overrides ordinary payment/presence facts; no code or bypass is supplied.                                                                                                                                                         |
| `train-cod-exception`                | Pass                                       | Explicit COD evidence establishes `prepaid=false`; payment takeover is appropriate despite prepaid-only product scope.                                                                                                                                                       |
| `train-language-spaced-prepaid`      | Pass                                       | Spaced payment wording confirms payment but not guard presence. Its retained source rationale describes the historical checker limitation.                                                                                                                                   |
| `train-language-gave-to-guard`       | Pass                                       | Payment and guard presence support prior authorization. The subsequent past-tense report is recorded as courier-reported, not independently verified receipt. Its source rationale is historical.                                                                            |
| `train-user-guard-room-at-entrance`  | Pass; friend configuration unvalidated     | The literal user-reported question receives only fictional saved entrance directions. Actual guard-room instructions and the friend's preferred default still need review.                                                                                                   |
| `train-user-pg-location-unseen-box`  | Pass; friend behavior unvalidated          | A saved fictional route may be provided. The assistant cannot read an unseen box label or invent an unsaved street number; the follow-up appropriately requests resident help. Actual PG-location instructions still need friend review.                                     |
| `train-user-fragile-confirmation`    | Pass under provisional conservative policy | Safety uncertainty triggers takeover and revokes the ordinary authorization. Fragility does not set `expensive=true`. The fragile-item default is still pending explicit user/friend confirmation, not a confirmed preference or independent production fragility guardrail. |

No contradictory gold observations, invented proposed locations, pending-question
overwrite, or unjustified takeovers were identified in these scripts. All observation
evidence quotes occur in the latest courier message; each alternative proposal is
present in that message. These findings are limited to the inspected scripts and
current policy, not a comprehensive semantic or safety guarantee.

The three user-reported question seeds are not verified transcripts. Their fictional
profiles, surrounding conversation, and resident decisions remain authored expansions.
They must not be presented as consented friend tests or observed delivery outcomes.

## Export and training requirements

The existing export path correctly requires oracle mode, train split, reviewed selected
records, and successful reference replay. It restricts generated files to ignored
artifact paths and refuses existing-file overwrite. This audit does **not** itself
satisfy the source records' `reviewed` gate or authorize a draft-to-reviewed status flip.

Production messages use current application-owned facts, pending questions, approvals,
saved context, and reference replay history. Scenario rationale and review metadata
stay outside model messages. Preserve that separation in training preparation.

Supervise **only the final assistant planner-JSON completion**. Earlier assistant
messages are application-composed courier replies, not planner outputs. Applying loss
to them would train the model toward the wrong output interface. Context and prior
conversation tokens need zero loss weight; the completion's planner tokens carry loss.

A `review_status="reviewed"` flag alone is an assertion, not an authenticated audit.
Any future review manifest should identify AI-assisted provenance and bind its evidence
to dataset, prompt, plan-schema, and policy/source hashes. Never describe this review
as human approval or friend validation. Paid training and model-quality claims require
separate explicit execution limits and comparable base/tuned evaluation.

## Distribution and missing coverage

The 40 targets contain: answer 9, handoff 8, clarify 7, request takeover 6,
request approval 4, delivered-report outcome 4, and wait 2. Twenty targets have
empty observations. Explicit observation updates contain payment true 13/false 1,
guard presence true 10/false 4, and one OTP requirement.

The train targets do not include returned/could-not-deliver outcomes, signature or
high-value updates. Coverage is also insufficient for transcription errors, noisy or
mixed-language speech, varied accents, many ambiguous short answers/corrections,
directions interrupting a pending clarification, and multiple parcels in one session.
There are no consented call recordings or unscripted human role-plays.

This small seed set can support a transparently described training pilot after its
review gates are explicitly satisfied. It cannot establish broad model safety,
statistically robust real-world accuracy, or a fine-tuning benefit by itself. Keep
production permission checks mandatory; oracle replay is not a model benchmark.
