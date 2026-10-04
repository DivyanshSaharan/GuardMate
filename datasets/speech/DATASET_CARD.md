# Courier speech replay corpus

`guardmate-courier-en-v1` contains 16 **planned fictional human speech cases**.
There are no bundled recordings or measured ASR results.

The references cover prepaid payment, uncertainty, security presence, fragile
items, completed versus future/negated handoffs, directions, and short yes/no
answers. Three question seeds were provided by the project owner from their
friend's experience; they are not verified recordings or real courier transcripts.
All other wording is authored. Cedar PG is fictional.

## Collecting fixtures

Only collect consenting, fictional role-play speech. Save each clip as the named
`audio/<case_id>.wav` file: complete PCM16, mono, 16 kHz, no longer than 30 seconds.
Verify the reference against what was actually spoken. A planned script is not
ground truth if the speaker changes it. Do not include addresses, phone numbers,
OTPs or genuine customer conversations.

Audio under this corpus's `audio/` directory is ignored by Git. Local evaluation
uses temporary native-inference files; the source fixtures stay on disk until
their owner removes them. Reports contain reference and recognized text.

Use a separate manifest with `provenance: "synthetic_voice"` for generated speech.
The evaluator refuses mixed human/synthetic corpora. Synthetic performance is not
evidence of accent, Bluetooth-call or real courier accuracy.

## Interpretation

The [replay evaluator](../../docs/speech-replay.md) reports word-edit counts and
normalized exact matches only. Missing clips prevent evaluation. These scores do
not measure parcel-policy safety, caller honesty or independent receipt, and are
not training labels for the planner.
