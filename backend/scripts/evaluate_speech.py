"""Inspect fictional speech fixtures; opt in to local-only Whisper replay scoring.

No microphone, Phone Link, hosted model, downloads, resident settings or sessions.
Actual replay needs every verified reference WAV and explicit local-data consent.
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from guardmate.speech.evaluation import (  # noqa: E402
    EvaluationError,
    aggregate_report,
    inspect_audio,
    load_manifest,
    prepare_recordings,
    score_case,
)
from guardmate.speech.service import SpeechError, SpeechService  # noqa: E402


def emit(value: dict) -> None:
    # References/hypotheses can contain untrusted text; never emit terminal controls.
    print(json.dumps(value, ensure_ascii=True, allow_nan=False), flush=True)


def report_path(value: str | Path) -> Path:
    path = Path(value).absolute()
    allowed = ROOT.absolute() / ".data" / "speech-evaluations"
    if not path.is_relative_to(allowed) or ".." in path.parts or path.suffix.lower() != ".json":
        raise argparse.ArgumentTypeError("Use a new JSON file under .data/speech-evaluations/.")
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink() or ancestor.is_junction():
            raise argparse.ArgumentTypeError("Report paths must not traverse links or junctions.")
        if ancestor != path and ancestor.exists() and not ancestor.is_dir():
            raise argparse.ArgumentTypeError("The report parent is not a directory.")
        if ancestor == ROOT.absolute():
            break
    if path.exists():
        raise argparse.ArgumentTypeError("Refusing to overwrite an existing report.")
    return path


def file_digest(path: Path) -> str:
    """Bounded content identity for the chosen installed runtime/model, not a download."""
    try:
        before = path.stat()
        if not path.is_file() or not 0 < before.st_size <= 2_000_000_000:
            raise ValueError
        digest = hashlib.sha256()
        with path.open("rb") as source:
            count = 0
            while block := source.read(1024 * 1024):
                count += len(block)
                if count > before.st_size:
                    raise ValueError
                digest.update(block)
        after = path.stat()
        if count != before.st_size or (before.st_size, before.st_mtime_ns) != (
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ValueError
        return digest.hexdigest()
    except (OSError, ValueError):
        raise EvaluationError(
            "asset_identity", "The installed speech asset is missing or changed."
        ) from None


def model_identity(service: SpeechService) -> dict:
    return {
        "implementation": "whisper.cpp",
        "language": "en",
        "decoder_profile": "guardmate-en-cpu-t4-p1-no-fallback-v1",
        "model_sha256": file_digest(service.whisper_model),
        "cli_sha256": file_digest(service.whisper_cli),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=ROOT / "datasets/speech/manifest.json")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--ack-local-fixture-transcription", action="store_true")
    parser.add_argument(
        "--whisper-cli", type=Path, help="Installed evaluator-only runtime override."
    )
    parser.add_argument(
        "--whisper-model", type=Path, help="Installed evaluator-only model override."
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.run and not args.ack_local_fixture_transcription:
        parser.error(
            "--run requires --ack-local-fixture-transcription: use only consenting fictional "
            "fixtures with references verified against the actual speech. Raw WAV goes to "
            "local Whisper only; reference/recognized text appears in the report/terminal."
        )
    if args.output and not args.run:
        parser.error("Saving a measured report requires --run and local-fixture consent.")
    if args.output:
        try:
            args.output = report_path(args.output)
        except argparse.ArgumentTypeError as error:
            parser.error(str(error))

    completed = 0
    current_case = None
    try:
        corpus = load_manifest(args.manifest)
        service = SpeechService(
            ROOT, whisper_cli=args.whisper_cli, whisper_model=args.whisper_model
        )
        if not args.run:
            emit(
                {
                    "event": "speech_corpus_inspection",
                    "fixtures": inspect_audio(corpus),
                    "speech": service.status(),
                    "evaluation_performed": False,
                    "speech_jobs": 0,
                    "hosted_model_requests": 0,
                    "audio_stream_started": False,
                }
            )
            return 0

        recordings = prepare_recordings(corpus)
        if not service.status()["stt_ready"]:
            raise EvaluationError("speech_not_ready", "Installed local transcription is not ready.")
        identity = model_identity(service)
        if args.output:
            report_path(args.output)
        emit(
            {
                "event": "local_speech_replay_started",
                "cases": len(recordings),
                "raw_audio_destination": "Local installed Whisper only.",
                "transcripts_retained": "Report and terminal; no conversation session.",
                "hosted_model_requests": 0,
                "automatic_retry": False,
                "production_model_changed": False,
            }
        )
        results = []
        for recording in recordings:
            current_case = recording.case.case_id
            result = service.transcribe(recording.audio)
            if (
                not isinstance(result, dict)
                or type(result.get("text")) is not str
                or not result["text"].strip()
                or len(result["text"]) > 600
                or type(result.get("duration_ms")) is not int
                or result["duration_ms"] != recording.duration_ms
                or type(result.get("processing_ms")) is not int
                or result["processing_ms"] < 0
            ):
                raise EvaluationError(
                    "invalid_transcription", "Local speech returned invalid metadata."
                )
            results.append(
                score_case(
                    current_case,
                    recording.case.reference,
                    result["text"],
                    recording.duration_ms,
                    result["processing_ms"],
                    recording.audio_sha256,
                )
            )
            completed += 1
            emit({"event": "speech_case_scored", "case_id": current_case, "completed": completed})
        if model_identity(service) != identity:
            raise EvaluationError(
                "asset_changed", "Speech assets changed; no complete report is valid."
            )
        report = aggregate_report(corpus, results, identity)
        if args.output:
            path = report_path(args.output)
            path.parent.mkdir(parents=True, exist_ok=True)
            report_path(path)
            with path.open("x", encoding="utf-8") as destination:
                json.dump(report, destination, ensure_ascii=True, allow_nan=False, indent=2)
                destination.write("\n")
        emit({"event": "speech_evaluation_completed", "report": report})
        return 0
    except KeyboardInterrupt:
        emit(
            {
                "event": "speech_evaluation_cancelled",
                "completed_cases": completed,
                "complete_report": False,
                "hosted_model_requests": 0,
                "automatic_retry": False,
            }
        )
        return 130
    except (EvaluationError, SpeechError, OSError, argparse.ArgumentTypeError) as error:
        emit(
            {
                "event": "speech_evaluation_failed",
                "code": getattr(error, "code", "local_speech_failure"),
                "case_id": current_case,
                "completed_cases": completed,
                "complete_report": False,
                "hosted_model_requests": 0,
                "automatic_retry": False,
            }
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
