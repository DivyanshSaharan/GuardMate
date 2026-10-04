import hashlib
import io
import json
import math
import struct
import wave
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from guardmate.speech.evaluation import (
    MAX_MANIFEST_BYTES,
    NORMALIZATION,
    EvaluationError,
    aggregate_report,
    inspect_audio,
    load_manifest,
    normalize_words,
    prepare_recordings,
    score_case,
)
from guardmate.speech.service import MAX_WAV_BYTES

HASH = "a" * 64
METADATA = {"implementation": "whisper.cpp", "model_sha256": "b" * 64}


def wav(seconds=0.1, *, silent=False, rate=16000):
    """An artificial non-speech waveform; no microphone, synthesis or inference."""
    count = round(seconds * rate)
    pcm = b"".join(
        struct.pack("<h", 0 if silent else round(1000 * math.sin(index / 10)))
        for index in range(count)
    )
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(pcm)
    return buffer.getvalue()


def case(case_id="prepaid", reference="This parcel is prepaid", audio="audio/prepaid.wav"):
    return {
        "case_id": case_id,
        "reference": reference,
        "audio": audio,
        "provenance": "synthetic_voice",
    }


def manifest(tmp_path, cases=None, **changes):
    data = {
        "schema_version": 1,
        "corpus_id": "delivery-replay",
        "cases": [case()] if cases is None else cases,
    }
    data.update(changes)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def add_audio(tmp_path, name="audio/prepaid.wav", data=None):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(wav() if data is None else data)
    return path


def score(reference="one two", hypothesis="one two", **changes):
    parameters = {
        "case_id": "prepaid",
        "reference": reference,
        "hypothesis": hypothesis,
        "duration_ms": 100,
        "processing_ms": 50,
        "audio_sha256": HASH,
    }
    parameters.update(changes)
    return score_case(**parameters)


def test_normalization_preserves_contractions_without_semantic_expansion():
    assert normalize_words("DON’T leave it! It's pre-paid.") == (
        "don't",
        "leave",
        "it",
        "it's",
        "pre",
        "paid",
    )
    assert normalize_words("ROOM_42, security’s entrance.") == (
        "room",
        "42",
        "security's",
        "entrance",
    )
    assert normalize_words("Café CAFE\u0301") == ("café", "café")
    assert score("don't", "do not")["exact_match"] is False


def test_punctuation_and_case_exact_match_is_normalized():
    result = score("Prepaid, PARCEL!", "prepaid parcel")
    assert result["exact_match"] is True
    assert result["reference"] == "Prepaid, PARCEL!"
    assert result["hypothesis"] == "prepaid parcel"
    assert result["word_errors"] == result["wer"] == 0
    assert result["normalization"] == NORMALIZATION


@pytest.mark.parametrize(
    ("reference", "hypothesis", "counts"),
    [
        ("one two three", "one four three", (1, 0, 0)),
        ("one two three", "one three", (0, 1, 0)),
        ("one two", "one two three", (0, 0, 1)),
        ("one", "two three four", (1, 0, 2)),
        ("one two", "", (0, 2, 0)),
        ("one two", "...", (0, 2, 0)),
        ("one two", "two one", (2, 0, 0)),
        ("one two three four", "zero one three four five", (2, 0, 1)),
    ],
)
def test_edit_counts(reference, hypothesis, counts):
    result = score(reference, hypothesis)
    assert (result["substitutions"], result["deletions"], result["insertions"]) == counts
    assert result["word_errors"] == sum(counts)
    assert result["wer"] == sum(counts) / len(normalize_words(reference))


def test_wer_can_exceed_one():
    result = score("one", "two three four")
    assert result["wer"] == 3


def test_maximum_word_input_remains_bounded():
    result = score(" ".join(["a"] * 300), " ".join(["b"] * 300))
    assert result["substitutions"] == 300
    assert result["wer"] == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"reference": ""},
        {"reference": "!?"},
        {"reference": "a" * 601},
        {"reference": None},
        {"hypothesis": "a" * 601},
        {"hypothesis": []},
        {"hypothesis": "x\x00y"},
        {"reference": "x\x1by"},
        {"case_id": "../private"},
        {"case_id": ""},
        {"case_id": True},
        {"duration_ms": -1},
        {"duration_ms": 30001},
        {"duration_ms": True},
        {"duration_ms": float("nan")},
        {"processing_ms": float("inf")},
        {"processing_ms": -1},
        {"processing_ms": False},
        {"processing_ms": 10**1000},
        {"audio_sha256": "not a hash"},
        {"audio_sha256": "A" * 64},
    ],
)
def test_score_rejects_unbounded_or_ambiguous_inputs(changes):
    with pytest.raises(EvaluationError) as caught:
        score(**changes)
    assert caught.value.code == "invalid_score"


@pytest.mark.parametrize("value", [None, [], "x\0y", "x\u200by", "x" * 601])
def test_normalization_rejects_invalid_inputs(value):
    with pytest.raises(EvaluationError):
        normalize_words(value)


def test_manifest_is_frozen_and_does_not_require_audio(tmp_path):
    item = case()
    item["note"] = "Fictional, prepaid delivery only."
    corpus = load_manifest(manifest(tmp_path, [item]))
    assert corpus.corpus_id == "delivery-replay"
    assert corpus.provenance == "synthetic_voice"
    assert corpus.manifest_path == (tmp_path / "manifest.json").resolve()
    assert corpus.cases[0].note == item["note"]
    with pytest.raises(FrozenInstanceError):
        corpus.cases[0].reference = "changed"


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": True},
        {"schema_version": 2},
        {"schema_version": "1"},
        {"corpus_id": ""},
        {"corpus_id": "private/path"},
        {"corpus_id": "x" * 81},
        {"corpus_id": None},
        {"cases": []},
        {"cases": {}},
        {"cases": [case(str(number)) for number in range(33)]},
        {"unexpected": "secret"},
    ],
)
def test_manifest_rejects_schema_and_bounds(tmp_path, changes):
    with pytest.raises(EvaluationError) as caught:
        load_manifest(manifest(tmp_path, **changes))
    assert caught.value.code == "invalid_manifest"


@pytest.mark.parametrize(
    "changes",
    [
        {"case_id": ""},
        {"case_id": "one two"},
        {"case_id": "../private"},
        {"case_id": "x" * 81},
        {"case_id": None},
        {"reference": ""},
        {"reference": " "},
        {"reference": "..."},
        {"reference": "x" * 601},
        {"reference": "hello\0world"},
        {"reference": []},
        {"provenance": "real_courier"},
        {"provenance": []},
        {"note": "x" * 201},
        {"note": ""},
        {"note": None},
        {"unexpected": "secret"},
    ],
)
def test_manifest_rejects_invalid_case_fields(tmp_path, changes):
    item = case()
    item.update(changes)
    with pytest.raises(EvaluationError) as caught:
        load_manifest(manifest(tmp_path, [item]))
    assert caught.value.code == "invalid_manifest"


def test_manifest_rejects_duplicate_ids_and_mixed_provenance(tmp_path):
    with pytest.raises(EvaluationError) as caught:
        load_manifest(manifest(tmp_path, [case(), case()]))
    assert caught.value.code == "invalid_manifest"
    second = case("guard", "Security is here", "audio/guard.wav")
    second["provenance"] = "fictional_human"
    with pytest.raises(EvaluationError) as caught:
        load_manifest(manifest(tmp_path, [case(), second]))
    assert caught.value.code == "mixed_provenance"


@pytest.mark.parametrize(
    "audio",
    [
        "../private.wav",
        "audio/../../private.wav",
        "/private.wav",
        "C:/private.wav",
        "C:private.wav",
        "\\\\server\\private.wav",
        "audio\\private.wav",
        "audio//private.wav",
        "audio/./private.wav",
        "audio/private.mp3",
        "audio/.wav.",
        "audio/private.wav:secret",
        "audio/nul.wav",
        "audio/COM1.wav",
        "audio/lpt².wav",
        "audio/private.wav ",
        "audio/hidden. /private.wav",
        "audio/pri\0vate.wav",
        "audio/*.wav",
        "audio/private?.wav",
        "",
        None,
        [],
        "a" * 241 + ".wav",
    ],
)
def test_manifest_refuses_unsafe_audio_paths(tmp_path, audio):
    with pytest.raises(EvaluationError) as caught:
        load_manifest(manifest(tmp_path, [case(audio=audio)]))
    assert caught.value.code == "unsafe_audio_path"


@pytest.mark.parametrize(
    "raw",
    [
        b'{"schema_version":1,"schema_version":1}',
        b'{"cases":NaN}',
        b'{"cases":Infinity}',
        b"[]",
        b"null",
        b"not json",
        b"\xff",
    ],
)
def test_manifest_rejects_bad_json_without_leaking_content(tmp_path, raw):
    path = tmp_path / "private-manifest.json"
    path.write_bytes(raw)
    with pytest.raises(EvaluationError) as caught:
        load_manifest(path)
    assert str(path) not in str(caught.value.as_dict())
    assert caught.value.as_dict()["status"] == "error"


def test_manifest_size_is_checked_before_json(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_bytes(b" " * (MAX_MANIFEST_BYTES + 1))
    with pytest.raises(EvaluationError) as caught:
        load_manifest(path)
    assert caught.value.code == "manifest_too_large"


def test_missing_manifest_has_path_free_error(tmp_path):
    with pytest.raises(EvaluationError) as caught:
        load_manifest(tmp_path / "private-name.json")
    assert caught.value.code == "manifest_not_readable"
    assert "private-name" not in str(caught.value.as_dict())


@pytest.mark.parametrize("linked_component", ["audio", "prepaid.wav"])
@pytest.mark.parametrize("kind", ["is_symlink", "is_junction"])
def test_audio_parent_and_leaf_links_are_refused_before_audio_read(
    tmp_path, monkeypatch, linked_component, kind
):
    path = manifest(tmp_path)
    original = getattr(Path, kind)
    monkeypatch.setattr(
        Path,
        kind,
        lambda candidate: candidate.name == linked_component or original(candidate),
    )
    with pytest.raises(EvaluationError) as caught:
        load_manifest(path)
    assert caught.value.code == "unsafe_audio_path"


def test_manifest_link_itself_is_refused(tmp_path, monkeypatch):
    path = manifest(tmp_path)
    monkeypatch.setattr(Path, "is_symlink", lambda candidate: candidate == path)
    with pytest.raises(EvaluationError) as caught:
        load_manifest(path)
    assert caught.value.code == "invalid_manifest"


def test_inspection_is_not_evaluation_and_omits_text_audio_and_paths(tmp_path):
    corpus = load_manifest(manifest(tmp_path))
    report = inspect_audio(corpus)
    assert report["status"] == "not_evaluated"
    assert report["evaluation_performed"] is False
    assert report["valid_audio_count"] == 0
    assert report["cases"][0]["status"] == "missing"
    assert report["cases"][0]["error"]["code"] == "audio_missing"
    serialized = json.dumps(report)
    assert str(tmp_path) not in serialized
    assert corpus.cases[0].reference not in serialized
    assert "wer" not in serialized


def test_inspection_reports_every_valid_missing_and_invalid_case(tmp_path):
    cases = [case(), case("bad", audio="bad.wav"), case("missing", audio="missing.wav")]
    valid_audio = wav()
    add_audio(tmp_path, data=valid_audio)
    add_audio(tmp_path, "bad.wav", b"private invalid bytes")
    report = inspect_audio(load_manifest(manifest(tmp_path, cases)))
    assert [item["status"] for item in report["cases"]] == ["valid", "invalid", "missing"]
    assert report["valid_audio_count"] == 1
    assert report["cases"][0]["audio_sha256"] == hashlib.sha256(valid_audio).hexdigest()
    assert report["cases"][0]["duration_ms"] == 100
    assert "private invalid bytes" not in json.dumps(report)


def test_inspection_ready_still_does_not_claim_evaluation(tmp_path):
    add_audio(tmp_path)
    report = inspect_audio(load_manifest(manifest(tmp_path)))
    assert report["status"] == "ready"
    assert report["evaluation_performed"] is False


@pytest.mark.parametrize("data", [b"", b"not a wav", wav(silent=True), wav(rate=8000)])
def test_preparation_reuses_speech_recording_validation(tmp_path, data):
    add_audio(tmp_path, data=data)
    corpus = load_manifest(manifest(tmp_path))
    with pytest.raises(EvaluationError) as caught:
        prepare_recordings(corpus)
    assert caught.value.code == "audio_invalid"
    assert caught.value.case_id == "prepaid"


def test_audio_size_check_is_bounded(tmp_path):
    add_audio(tmp_path, data=b"x" * (MAX_WAV_BYTES + 1))
    with pytest.raises(EvaluationError) as caught:
        prepare_recordings(load_manifest(manifest(tmp_path)))
    assert caught.value.code == "audio_invalid"


def test_directory_is_not_audio(tmp_path):
    (tmp_path / "audio" / "prepaid.wav").mkdir(parents=True)
    report = inspect_audio(load_manifest(manifest(tmp_path)))
    assert report["cases"][0]["status"] == "invalid"


def test_prepare_all_before_return_and_preserve_immutable_audio(tmp_path):
    add_audio(tmp_path)
    cases = [case(), case("second", audio="second.wav")]
    corpus = load_manifest(manifest(tmp_path, cases))
    with pytest.raises(EvaluationError) as caught:
        prepare_recordings(corpus)
    assert caught.value.case_id == "second"
    add_audio(tmp_path, "second.wav")
    recordings = prepare_recordings(corpus)
    assert len(recordings) == 2
    assert recordings[0].case is corpus.cases[0]
    assert isinstance(recordings[0].audio, bytes)
    with pytest.raises(FrozenInstanceError):
        recordings[0].audio = b"changed"


def test_link_introduced_after_load_is_refused_on_inspect_and_prepare(tmp_path, monkeypatch):
    add_audio(tmp_path)
    corpus = load_manifest(manifest(tmp_path))
    monkeypatch.setattr(Path, "is_symlink", lambda candidate: candidate.name == "audio")
    report = inspect_audio(corpus)
    assert report["cases"][0]["error"]["code"] == "unsafe_audio_path"
    with pytest.raises(EvaluationError):
        prepare_recordings(corpus)


def test_micro_wer_not_mean_case_wer_and_manifest_order(tmp_path):
    corpus = load_manifest(
        manifest(
            tmp_path, [case("short", "one", "short.wav"), case("long", "one two three", "long.wav")]
        )
    )
    first = score("one", "two", case_id="short")
    second = score("one two three", "one two three", case_id="long")
    report = aggregate_report(corpus, [second, first], METADATA)
    assert report["status"] == "evaluated"
    assert report["totals"]["micro_wer"] == 0.25
    assert report["totals"]["exact_match_rate"] == 0.5
    assert report["totals"]["processing_ms"] == 100
    assert [item["case_id"] for item in report["cases"]] == ["short", "long"]
    assert report["provenance"] == "synthetic_voice"
    assert report["model"] == METADATA
    assert str(tmp_path) not in json.dumps(report)


def test_fictional_human_corpus_is_labeled_separately(tmp_path):
    item = case(reference="one two")
    item["provenance"] = "fictional_human"
    corpus = load_manifest(manifest(tmp_path, [item]))
    report = aggregate_report(corpus, [score()], METADATA)
    assert report["provenance"] == "fictional_human"


@pytest.mark.parametrize("results", [[], None, {}, [score(), score()], [score(case_id="unknown")]])
def test_aggregate_refuses_partial_duplicate_and_unknown_cases(tmp_path, results):
    corpus = load_manifest(manifest(tmp_path, [case(reference="one two")]))
    with pytest.raises(EvaluationError) as caught:
        aggregate_report(corpus, results, METADATA)
    assert caught.value.code == "incomplete_results"


@pytest.mark.parametrize(
    "changes",
    [
        {"reference": "different"},
        {"word_errors": 3},
        {"wer": 0.5},
        {"reference_words": 50},
        {"audio_sha256": "wrong"},
        {"extra": "secret"},
        {"duration_ms": float("nan")},
        {"processing_ms": -1},
        {"schema_version": True},
        {"exact_match": 1},
        {"word_errors": False},
        {"wer": 0},
    ],
)
def test_aggregate_recomputes_and_validates_score_fields(tmp_path, changes):
    corpus = load_manifest(manifest(tmp_path, [case(reference="one two")]))
    result = score()
    result.update(changes)
    with pytest.raises(EvaluationError) as caught:
        aggregate_report(corpus, [result], METADATA)
    assert caught.value.code == "invalid_score"


@pytest.mark.parametrize(
    "metadata",
    [
        {},
        None,
        [],
        {"model_sha256": HASH},
        {"implementation": "whisper.cpp"},
        {"implementation": "", "model_sha256": HASH},
        {"implementation": "x" * 129, "model_sha256": HASH},
        {"implementation": "C:\\private\\whisper.exe", "model_sha256": HASH},
        {"implementation": "/private/whisper", "model_sha256": HASH},
        {"implementation": "whisper.cpp", "model_sha256": "wrong"},
        {"implementation": "whisper.cpp", "model_sha256": HASH, "api_key": "secret"},
    ],
)
def test_metadata_bounds_and_no_path_or_unknown_secret_fields(tmp_path, metadata):
    corpus = load_manifest(manifest(tmp_path, [case(reference="one two")]))
    with pytest.raises(EvaluationError) as caught:
        aggregate_report(corpus, [score()], metadata)
    assert caught.value.code == "invalid_metadata"


def test_runtime_binary_identity_can_replace_implementation_label(tmp_path):
    corpus = load_manifest(manifest(tmp_path, [case(reference="one two")]))
    metadata = {"model_sha256": HASH, "cli_sha256": "b" * 64, "language": "en"}
    assert aggregate_report(corpus, [score()], metadata)["model"] == metadata


def test_decoder_profile_is_recorded_with_asset_identity(tmp_path):
    corpus = load_manifest(manifest(tmp_path, [case(reference="one two")]))
    metadata = {**METADATA, "decoder_profile": "guardmate-en-cpu-t4-p1-no-fallback-v1"}
    assert aggregate_report(corpus, [score()], metadata)["model"] == metadata


def test_error_schema_is_stable_and_path_free():
    assert EvaluationError(
        "audio_missing", "Missing fictional clip.", case_id="prepaid"
    ).as_dict() == {
        "schema_version": 1,
        "status": "error",
        "error": {
            "code": "audio_missing",
            "message": "Missing fictional clip.",
            "case_id": "prepaid",
        },
    }
