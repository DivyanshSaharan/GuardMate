# Local speech replay evaluation

Replay a fixed set of verified fictional recordings against an installed Whisper
model, without Phone Link, a running backend, hosted Qwen or inference credits.
The shipped [corpus](../datasets/speech/DATASET_CARD.md) has 16 planned cases but
**no recordings**, so no current speech-accuracy percentage is claimed.

## Inspect without inference

From the repository root:

```powershell
.venv\Scripts\python backend\scripts\evaluate_speech.py
```

This validates the manifest, checks any named WAV files and reports installed
speech-asset presence. It never opens a microphone or runs speech inference.
Missing/invalid clips are listed without exposing transcript text or audio paths.

## Prepare recordings

Get separate consent to retaining fictional fixture audio. Record the cases as
PCM16 mono 16 kHz WAV, at most 30 seconds each, using the manifest's named paths.
Verify every reference against the actual spoken words, not just the intended
script. Never reuse the previous unretained live call as if it had aligned labels.

Use a separate corpus for synthetic voices; human and synthetic provenance cannot
be mixed. The evaluator validates **every** clip before the first native job. A
missing or invalid clip stops evaluation rather than silently shrinking the corpus.

## Opt in to replay

```powershell
.venv\Scripts\python backend\scripts\evaluate_speech.py --run --ack-local-fixture-transcription --output .data/speech-evaluations/base-en-001.json
```

Use a new report filename; overwrites, links and junctions are refused. Reports
live under ignored `.data/speech-evaluations/`. They include reference/hypothesis
text, per-clip audio hashes, word-edit counts, timings, and model/runtime hashes.
Terminal output also includes the completed report. Keep fictional data only.

To evaluate another **already installed** model, pass `--whisper-model` and, if
needed, `--whisper-cli`. Use `--manifest` for another corpus. These options change
only this evaluator, never the running application's speech configuration. The
CLI does not load `.env`; installed defaults or process environment settings apply.
It does not download models or synthesize sample recordings.

## Reading results

Word error rate is `(substitutions + deletions + insertions) / reference words`.
Aggregate WER uses total word errors divided by total reference words, not the
mean of per-case percentages, and can exceed 100%. Normalization ignores case and
punctuation while preserving contractions; it does not repair words or negation.

Exact match refers to normalized transcript equality. Neither score is an ASR
confidence score or parcel-safety guarantee: losing one “not” may change the
meaning despite a low WER. Inspect individual reference/hypothesis pairs.
Timings include per-job native startup; they are not speech-to-speech latency.

Failures/cancellation stop without automatic retries or a complete evaluation
report. Model/runtime hashes are checked again after inference. For a fair future
comparison, use the same verified audio/reference corpus and decoder profile;
separate generated speech, natural human speech and actual cellular recordings.

Offline tests use synthetic PCM and mocked inference. No natural-speech or live
automatic-call evaluation has been performed by implementing this tool.
