"""Bounded, local replay-corpus inspection and deterministic transcript scoring.

This module never records, synthesizes, transcribes, or contacts a model. WER is
text-edit distance, not a confidence score or a guarantee about delivery safety.
"""

import hashlib
import json
import math
import os
import re
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from guardmate.speech.service import MAX_WAV_BYTES, SpeechError, validate_recording

MAX_MANIFEST_BYTES = 64 * 1024
MAX_CASES = 32
MAX_TEXT_CHARS = 600
SCHEMA_VERSION = 1
NORMALIZATION = "unicode_nfc_casefold_words_preserve_apostrophes_v1"
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\Z")
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_WORDS = re.compile(r"[^\W_]+(?:'[^\W_]+)*", re.UNICODE)
_PROVENANCE = {"synthetic_voice", "fictional_human"}
_RESERVED_WINDOWS = {"con", "prn", "aux", "nul", "clock$"} | {
    f"{prefix}{digit}" for prefix in ("com", "lpt") for digit in "123456789¹²³"
}


class EvaluationError(Exception):
    """Stable public errors deliberately omit filesystem paths and audio content."""

    def __init__(self, code: str, message: str, *, case_id: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.case_id = case_id

    def as_dict(self) -> dict[str, Any]:
        error = {"code": self.code, "message": self.message}
        if self.case_id is not None:
            error["case_id"] = self.case_id
        return {"schema_version": SCHEMA_VERSION, "status": "error", "error": error}


@dataclass(frozen=True)
class SpeechCase:
    case_id: str
    reference: str
    audio: str
    provenance: str
    note: str | None = None


@dataclass(frozen=True)
class SpeechCorpus:
    manifest_path: Path
    corpus_id: str
    cases: tuple[SpeechCase, ...]

    @property
    def provenance(self) -> str:
        return self.cases[0].provenance


@dataclass(frozen=True)
class PreparedRecording:
    case: SpeechCase
    audio: bytes
    duration_ms: int
    audio_sha256: str


def _text(value: Any, maximum: int, *, allow_empty: bool = False) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= maximum
        and (allow_empty or bool(value.strip()))
        and not any(
            unicodedata.category(character).startswith("C") and character not in "\t\r\n"
            for character in value
        )
    )


def normalize_words(text: str) -> tuple[str, ...]:
    if not _text(text, MAX_TEXT_CHARS, allow_empty=True):
        raise EvaluationError("invalid_text", "Use plain text of at most 600 characters.")
    normalized = unicodedata.normalize("NFC", text).casefold()
    normalized = normalized.replace("’", "'").replace("‘", "'")
    return tuple(_WORDS.findall(normalized))


def _audio_parts(value: Any) -> tuple[str, ...]:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 240
        or any(character in value for character in '\\:*?"<>|')
        or any(unicodedata.category(character).startswith("C") for character in value)
        or PurePosixPath(value).is_absolute()
        or PureWindowsPath(value).is_absolute()
        or PureWindowsPath(value).drive
    ):
        raise EvaluationError("unsafe_audio_path", "Audio must be a contained relative WAV path.")
    parts = tuple(value.split("/"))
    if (
        any(
            not part
            or part in {".", ".."}
            or len(part) > 100
            or part[-1] in " ."
            or part.split(".", 1)[0].casefold() in _RESERVED_WINDOWS
            for part in parts
        )
        or PurePosixPath(value).suffix.casefold() != ".wav"
    ):
        raise EvaluationError("unsafe_audio_path", "Audio must be a contained relative WAV path.")
    return parts


def _linked(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def _audio_path(corpus: SpeechCorpus, case: SpeechCase) -> Path:
    root = corpus.manifest_path.parent
    current = root
    try:
        for part in _audio_parts(case.audio):
            current = current / part
            if _linked(current):
                raise EvaluationError(
                    "unsafe_audio_path",
                    "Audio paths must not contain links or junctions.",
                    case_id=case.case_id,
                )
        if not current.resolve().is_relative_to(root):
            raise EvaluationError(
                "unsafe_audio_path",
                "Audio must remain inside its corpus directory.",
                case_id=case.case_id,
            )
    except (OSError, RuntimeError) as error:
        raise EvaluationError(
            "unsafe_audio_path", "The audio path cannot be safely resolved.", case_id=case.case_id
        ) from error
    return current


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("non-finite JSON value")


def load_manifest(path: Path | str) -> SpeechCorpus:
    """Load only the named manifest; never enumerate directories or follow audio links."""
    manifest = Path(path)
    try:
        if _linked(manifest) or not stat.S_ISREG(manifest.stat().st_mode):
            raise EvaluationError("invalid_manifest", "Use a regular, non-linked JSON manifest.")
        if manifest.stat().st_size > MAX_MANIFEST_BYTES:
            raise EvaluationError("manifest_too_large", "The manifest must be at most 64 KiB.")
        with manifest.open("rb") as source:
            raw = source.read(MAX_MANIFEST_BYTES + 1)
        if len(raw) > MAX_MANIFEST_BYTES:
            raise EvaluationError("manifest_too_large", "The manifest must be at most 64 KiB.")
        data = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant
        )
    except (OSError, UnicodeError) as error:
        raise EvaluationError(
            "manifest_not_readable", "The JSON manifest cannot be read."
        ) from error
    except (ValueError, RecursionError) as error:
        raise EvaluationError("invalid_manifest", "Use valid, unambiguous UTF-8 JSON.") from error
    if (
        not isinstance(data, dict)
        or set(data) != {"schema_version", "corpus_id", "cases"}
        or type(data.get("schema_version")) is not int
        or data["schema_version"] != SCHEMA_VERSION
        or not isinstance(data.get("corpus_id"), str)
        or not _TOKEN.fullmatch(data["corpus_id"])
        or not isinstance(data.get("cases"), list)
        or not 1 <= len(data["cases"]) <= MAX_CASES
    ):
        raise EvaluationError("invalid_manifest", "Use a version-1 corpus with 1 to 32 cases.")
    cases: list[SpeechCase] = []
    identifiers: set[str] = set()
    for item in data["cases"]:
        required = {"case_id", "reference", "audio", "provenance"}
        if (
            not isinstance(item, dict)
            or not required <= set(item) <= required | {"note"}
            or not isinstance(item.get("case_id"), str)
            or not _TOKEN.fullmatch(item["case_id"])
            or item["case_id"] in identifiers
            or not _text(item.get("reference"), MAX_TEXT_CHARS)
            or not normalize_words(item["reference"])
            or not isinstance(item.get("provenance"), str)
            or item["provenance"] not in _PROVENANCE
            or ("note" in item and not _text(item["note"], 200))
        ):
            raise EvaluationError(
                "invalid_manifest", "Each case needs unique, bounded valid fields."
            )
        _audio_parts(item["audio"])
        identifiers.add(item["case_id"])
        cases.append(SpeechCase(**item))
    if len({case.provenance for case in cases}) != 1:
        raise EvaluationError(
            "mixed_provenance", "Use separate corpora for synthetic and fictional human speech."
        )
    corpus = SpeechCorpus(manifest.resolve(), data["corpus_id"], tuple(cases))
    # Present symlinks/junctions are refused even when another case is still missing.
    for case in corpus.cases:
        _audio_path(corpus, case)
    return corpus


def _recording(corpus: SpeechCorpus, case: SpeechCase) -> PreparedRecording:
    path = _audio_path(corpus, case)
    try:
        before = path.stat()
        if not stat.S_ISREG(before.st_mode):
            raise EvaluationError(
                "audio_invalid", "Use a regular PCM16 mono 16 kHz WAV file.", case_id=case.case_id
            )
        if before.st_size > MAX_WAV_BYTES:
            raise EvaluationError(
                "audio_invalid",
                "Audio must not exceed the 30-second WAV limit.",
                case_id=case.case_id,
            )
        with path.open("rb") as source:
            opened = os.fstat(source.fileno())
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise EvaluationError(
                    "unsafe_audio_path", "Audio changed during validation.", case_id=case.case_id
                )
            audio = source.read(MAX_WAV_BYTES + 1)
        after = _audio_path(corpus, case).stat()
        if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ):
            raise EvaluationError(
                "unsafe_audio_path", "Audio changed during validation.", case_id=case.case_id
            )
    except FileNotFoundError as error:
        raise EvaluationError(
            "audio_missing", "A named replay recording has not been provided.", case_id=case.case_id
        ) from error
    except OSError as error:
        raise EvaluationError(
            "audio_not_readable", "A named replay recording cannot be read.", case_id=case.case_id
        ) from error
    try:
        duration_ms = validate_recording(audio)
    except SpeechError as error:
        raise EvaluationError("audio_invalid", error.message, case_id=case.case_id) from error
    return PreparedRecording(case, audio, duration_ms, hashlib.sha256(audio).hexdigest())


def inspect_audio(corpus: SpeechCorpus) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for case in corpus.cases:
        item: dict[str, Any] = {"case_id": case.case_id, "provenance": case.provenance}
        try:
            recording = _recording(corpus, case)
        except EvaluationError as error:
            item.update(
                status="missing" if error.code == "audio_missing" else "invalid",
                error=error.as_dict()["error"],
            )
        else:
            item.update(
                status="valid",
                duration_ms=recording.duration_ms,
                audio_sha256=recording.audio_sha256,
            )
        results.append(item)
    valid = sum(item["status"] == "valid" for item in results)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "ready" if valid == len(results) else "not_evaluated",
        "corpus_id": corpus.corpus_id,
        "provenance": corpus.provenance,
        "case_count": len(results),
        "valid_audio_count": valid,
        "cases": results,
        "evaluation_performed": False,
    }


def prepare_recordings(corpus: SpeechCorpus) -> list[PreparedRecording]:
    """Validate every named recording before callers admit the first inference job."""
    return [_recording(corpus, case) for case in corpus.cases]


def _number(value: Any, maximum: int | None = None) -> bool:
    return (
        type(value) in {int, float}
        and value >= 0
        and (maximum is None or value <= maximum)
        and math.isfinite(value)
    )


def score_case(
    case_id: str,
    reference: str,
    hypothesis: str,
    duration_ms: int | float,
    processing_ms: int | float,
    audio_sha256: str,
) -> dict[str, Any]:
    if (
        not isinstance(case_id, str)
        or not _TOKEN.fullmatch(case_id)
        or not _text(reference, MAX_TEXT_CHARS)
        or not _text(hypothesis, MAX_TEXT_CHARS, allow_empty=True)
        or not _number(duration_ms, 30_000)
        or not _number(processing_ms, 3_600_000)
        or not isinstance(audio_sha256, str)
        or not _HASH.fullmatch(audio_sha256)
    ):
        raise EvaluationError(
            "invalid_score", "Use bounded text, finite timings and an audio hash."
        )
    ref, hyp = normalize_words(reference), normalize_words(hypothesis)
    if not ref:
        raise EvaluationError("invalid_score", "The reference must contain at least one word.")
    matrix = [list(range(len(hyp) + 1))]
    for i, expected in enumerate(ref, 1):
        row = [i]
        for j, actual in enumerate(hyp, 1):
            row.append(
                min(
                    matrix[i - 1][j - 1] + (expected != actual),
                    matrix[i - 1][j] + 1,
                    row[j - 1] + 1,
                )
            )
        matrix.append(row)
    substitutions = deletions = insertions = 0
    i, j = len(ref), len(hyp)
    # Deterministic optimal-path tie order: diagonal, deletion, then insertion.
    while i or j:
        if i and j and matrix[i][j] == matrix[i - 1][j - 1] + (ref[i - 1] != hyp[j - 1]):
            substitutions += ref[i - 1] != hyp[j - 1]
            i, j = i - 1, j - 1
        elif i and matrix[i][j] == matrix[i - 1][j] + 1:
            deletions += 1
            i -= 1
        else:
            insertions += 1
            j -= 1
    errors = substitutions + deletions + insertions
    return {
        "schema_version": SCHEMA_VERSION,
        "case_id": case_id,
        "reference": reference,
        "hypothesis": hypothesis,
        "reference_words": len(ref),
        "hypothesis_words": len(hyp),
        "substitutions": substitutions,
        "deletions": deletions,
        "insertions": insertions,
        "word_errors": errors,
        "wer": errors / len(ref),
        "exact_match": ref == hyp,
        "duration_ms": duration_ms,
        "processing_ms": processing_ms,
        "audio_sha256": audio_sha256,
        "normalization": NORMALIZATION,
    }


def _metadata(metadata: Any) -> dict[str, str]:
    allowed = {
        "implementation",
        "model_id",
        "language",
        "decoder_profile",
        "model_sha256",
        "cli_sha256",
    }
    if (
        not isinstance(metadata, dict)
        or not metadata
        or not set(metadata) <= allowed
        or "model_sha256" not in metadata
        or not ({"implementation", "cli_sha256"} & set(metadata))
        or any(not _text(value, 128) for value in metadata.values())
        or any(any(character in value for character in "\\/:@") for value in metadata.values())
        or any(
            not _HASH.fullmatch(metadata[key])
            for key in ("model_sha256", "cli_sha256")
            if key in metadata
        )
    ):
        raise EvaluationError(
            "invalid_metadata", "Provide bounded model/runtime identities and hashes, not paths."
        )
    return dict(metadata)


def aggregate_report(
    corpus: SpeechCorpus,
    results: list[dict[str, Any]],
    model_metadata: dict[str, str],
) -> dict[str, Any]:
    """Require a complete, once-only corpus; never label a partial replay an evaluation."""
    metadata = _metadata(model_metadata)
    if not isinstance(results, list) or len(results) != len(corpus.cases):
        raise EvaluationError("incomplete_results", "Score every corpus case exactly once.")
    by_id = {case.case_id: case for case in corpus.cases}
    checked: dict[str, dict[str, Any]] = {}
    for result in results:
        if not isinstance(result, dict) or not isinstance(result.get("case_id"), str):
            raise EvaluationError("invalid_score", "Each result must be a case score.")
        case_id = result["case_id"]
        if case_id not in by_id or case_id in checked:
            raise EvaluationError("incomplete_results", "Score every corpus case exactly once.")
        case = by_id[case_id]
        if result.get("reference") != case.reference:
            raise EvaluationError("invalid_score", "A score must use its exact manifest reference.")
        try:
            recomputed = score_case(
                case_id,
                case.reference,
                result["hypothesis"],
                result["duration_ms"],
                result["processing_ms"],
                result["audio_sha256"],
            )
        except KeyError as error:
            raise EvaluationError(
                "invalid_score", "Each result must be a complete case score."
            ) from error
        integer_keys = {
            "schema_version",
            "reference_words",
            "hypothesis_words",
            "substitutions",
            "deletions",
            "insertions",
            "word_errors",
        }
        if (
            result != recomputed
            or any(type(result[key]) is not int for key in integer_keys)
            or type(result["exact_match"]) is not bool
            or type(result["wer"]) is not float
        ):
            raise EvaluationError("invalid_score", "Score fields must match deterministic WER.")
        checked[case_id] = recomputed
    ordered = [checked[case.case_id] for case in corpus.cases]
    reference_words = sum(item["reference_words"] for item in ordered)
    totals = {
        key: sum(item[key] for item in ordered)
        for key in (
            "reference_words",
            "hypothesis_words",
            "substitutions",
            "deletions",
            "insertions",
            "word_errors",
            "duration_ms",
            "processing_ms",
        )
    }
    totals.update(
        micro_wer=totals["word_errors"] / reference_words,
        exact_match_count=sum(item["exact_match"] for item in ordered),
        exact_match_rate=sum(item["exact_match"] for item in ordered) / len(ordered),
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "evaluated",
        "corpus_id": corpus.corpus_id,
        "provenance": corpus.provenance,
        "case_count": len(ordered),
        "normalization": NORMALIZATION,
        "model": metadata,
        "totals": totals,
        "cases": ordered,
        "interpretation": (
            "Text edit distance on this corpus only; not ASR confidence or delivery safety."
        ),
    }
