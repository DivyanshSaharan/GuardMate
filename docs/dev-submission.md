---
title: "GuardMate: 'Leave it with the guard' shouldn't need another phone call"
published: false
tags: devchallenge, weekendchallenge, hf26challenge
---

Built for the [Hacktoberfest Weekend Challenge: Build for a Friend](https://dev.to/challenges/hacktoberfest-weekend-2026-10-01).

## What I Built

My friend orders quite a few things online and lives in a PG—a paying guest
accommodation. During the week, we go to the office. His room is locked, a courier
arrives, and his phone rings with another version of the same question:

**Where should I leave your parcel?**

Usually, the answer is the guard room at the entrance. But the call can arrive
while he is busy, and sometimes a missed conversation becomes a returned parcel.
On weekends he might be home, so blindly sending the same instruction is not
quite right either.

I built **GuardMate**, a personal AI delivery assistant for that specific problem.
It knows his saved PG directions, office routine and today's availability. It
remembers what a courier has already confirmed and asks the resident before
changing an approved handoff location.

The interesting part isn't making an AI say “guard room.” It's deciding when it
must **not** say that: nobody is there, payment is needed, the delivery requires
an OTP, or the courier proposes somewhere else.

Three questions my friend has faced shaped the project: where the guard room is,
where the PG is located, and whether a fragile parcel should really be left with
the guard. They're small questions with consequences for somebody's belongings.

## Demo

- [Product walkthrough — video (MP4)](https://github.com/DivyanshSaharan/GuardMate/blob/main/docs/media/guardmate-demo.mp4)
- [Call recording — audio (MP3)](https://github.com/DivyanshSaharan/GuardMate/blob/main/docs/media/guardmate-call-recording.mp3)

The recording is shared with all speakers' permission. Use the file download
control if your browser does not offer playback on the GitHub page.

The prototype includes a resident dashboard and a courier role-play panel. I can
set office days, override today's availability, and enable an expiring delivery
window. Different couriers have separate conversations and approvals.

We also tested one real audio connection: a manually answered Phone Link call
using a vivo T2x 5G running Android 15, with a consenting caller playing a fictional
courier. Local Whisper captured their speech, real hosted Qwen proposed the next
action, GuardMate checked it, and local Piper spoke the reply back through the call.

The caller confirmed hearing the full greeting and all three replies. When the
last transcription was ambiguous, the model proposed recording an outcome—but
the application refused to mark a delivery complete and asked for clarification.
No parcel actually changed hands.

That's the moment I want the demo to show: not just a successful reply, but a
boundary holding when the inputs are wrong.

[The test report](https://github.com/DivyanshSaharan/GuardMate/blob/main/docs/live-cellular-test-2026-10-04.md)
includes the actual transcripts, model proposals and checked actions. Automatic
turn-taking after manual answering is implemented and tested offline; it has not
yet been demonstrated on a live call. Answering and hanging up remain manual.

## Code

[GuardMate on GitHub](https://github.com/DivyanshSaharan/GuardMate)

The README contains setup instructions. The repository also includes the fictional
delivery dataset, regression tests, training recipe and evaluation reports.

## How I Built It

The dashboard uses React and TypeScript. FastAPI handles the conversation engine,
resident settings and local speech; SQLite stores history and approvals.

At the core is the open-weight **Qwen3.5-4B**, sampled through **Tinker**. It sees
conversation history, saved instructions, availability and confirmed parcel facts,
then returns a structured plan rather than unrestricted courier-facing prose.

GuardMate checks that plan before executing it. Positive facts need supporting
courier evidence. A courier cannot approve an alternative by claiming to be the
resident. Approvals expire and become stale when resident instructions change.
Reported delivery remains labelled courier-reported, not independently verified.

For speech, **whisper.cpp** transcribes locally and **Piper** synthesizes locally.
The call runner captures and plays in separate phases. The automatic mode adds
bounded speech endpointing, turn/time limits and pauses on uncertainty or resident
decisions; it does not silently retry an uncertain operation.

### Fine-tuning with Tinker

I used Tinker for a real LoRA pilot—not only hosted inference. The recipe used
rank 16, 40 planner targets from 15 owner-reviewed fictional scenarios, batch size
4 and three epochs: 30 optimizer updates. The targets are structured planner
outputs, not a memorized list of assistant replies.

Then I ran fresh base and tuned evaluations on the same six development scenarios:

| Metric                           | Base          | Tuned         |
| -------------------------------- | ------------- | ------------- |
| Strict planner agreement         | 7/13 (53.85%) | 8/13 (61.54%) |
| Checked scenario success         | 5/6           | 5/6           |
| Median text-model/policy latency | 2.133 s       | 3.113 s       |

The gain is one additional correctly matched planner turn: **7.69 percentage points
on this tiny rubric**, not proof of generalization. Checked delivery behavior did
not improve, and median latency got worse. I kept base Qwen in the recorded live
test rather than treating a completed training job as an automatic promotion.

[The pilot report](https://github.com/DivyanshSaharan/GuardMate/blob/main/docs/pilot-2026-10-04.md)
documents the matched settings, the changed case, remaining mistakes and estimated
credit reservations. This makes the sponsor integration reproducible without
turning a small result into a larger claim.

## Why Does Open Innovation Matter?

For this project, openness gives me control over the pieces that fail.

When Whisper recognized “parcel” as “passion” or “option,” I could inspect the
local pipeline and build a fixed-audio replay evaluator. I don't have to change
the whole application to try another installed speech model. Raw call audio
doesn't need to be sent to a proprietary speech service.

With open-weight Qwen and Tinker, I could train the planner on a narrow task,
export a sampler and compare it against the base model using the same application
checks. Access to that experiment mattered more than simply adding an AI API call.
Closed services can offer fine-tuning too; the useful distinction here is the
replaceable open components and inspectable behavior, not a claim that every
closed model would perform worse.

There is an important limit: **GuardMate is not fully offline**. Recognized text,
previous messages and saved resident context go to Tinker for planning. Local
speech avoids cloud audio processing, not all hosted data sharing. Training and
sampling use credits; local speech does not need a metered speech API.

The project is still a prototype. English grounding checks can fail, and our first
call exposed substantial ASR problems. I haven't claimed a production rollout or
invented feedback from handing it over to my friend. The next meaningful test is
whether it helps him reliably—not whether it can make one polished conversation.

## Prize Categories

**Best Use of Tinker** — delivery-specific Qwen LoRA training with a fresh matched
baseline and a measured strict-planner improvement. The small sample and unchanged
checked behavior are reported explicitly. I am not claiming use of other sponsors
whose technology is not part of this build.
