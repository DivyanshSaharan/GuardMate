"""Preview train-only SFT offline; hosted LoRA training requires separate explicit admission."""

import argparse
import json
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from guardmate.agent.provider import MODEL  # noqa: E402
from guardmate.evaluation.dataset import DatasetError  # noqa: E402
from guardmate.training.preparation import PreparationError, build_preview  # noqa: E402
from guardmate.training.supervisor import (  # noqa: E402
    TrainingAdmissionError,
    launch,
    validate_admission,
)


def money(value: str) -> int:
    try:
        amount = Decimal(value)
        micros = amount * 1_000_000
        if not amount.is_finite() or not 0 < amount <= Decimal("1.75") or micros != int(micros):
            raise ValueError
        return int(micros)
    except (InvalidOperation, ValueError, OverflowError):
        raise argparse.ArgumentTypeError(
            "Use a positive estimate limit <= $1.75, up to 6 decimals."
        ) from None


def artifact_path(value: str) -> Path:
    path = Path(value).resolve()
    if not path.is_relative_to((ROOT / ".data" / "training").resolve()) or path.suffix != ".json":
        raise argparse.ArgumentTypeError("Training artifacts must be .json in .data/training/.")
    return path


def cached_tokenizer():
    from transformers import AutoTokenizer

    # Never make an implicit model download or initialize a hosted client.
    return AutoTokenizer.from_pretrained(MODEL, local_files_only=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=ROOT / "datasets/delivery/scenarios.jsonl")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--prepare", action="store_true", help="Export a reviewed, train-only job.")
    modes.add_argument(
        "--live", action="store_true", help="Explicitly enable credit-consuming LoRA."
    )
    parser.add_argument("--epochs", type=int, default=1, choices=range(1, 4))
    parser.add_argument("--batch-size", type=int, default=4, choices=range(1, 17))
    parser.add_argument("--output", type=artifact_path)
    parser.add_argument("--job", type=artifact_path)
    parser.add_argument("--max-cost-usd", type=money)
    parser.add_argument("--max-updates", type=int, choices=range(1, 31))
    parser.add_argument("--ack-sdk-retries", action="store_true")
    parser.add_argument(
        "--price-checked-on", help="Date of verified official rate card (YYYY-MM-DD)."
    )
    parser.add_argument("--max-duration-seconds", type=int, default=900)
    arguments = parser.parse_args()
    if arguments.live:
        if not arguments.job or arguments.output or arguments.max_cost_usd is None:
            parser.error("--live requires --job and --max-cost-usd, and cannot export --output.")
        if arguments.max_updates is None or not arguments.ack_sdk_retries:
            parser.error("--live requires --max-updates and --ack-sdk-retries.")
        if not arguments.price_checked_on:
            parser.error(
                "--live requires --price-checked-on after checking the official rate card."
            )
    elif arguments.job or any(
        (
            arguments.max_cost_usd is not None,
            arguments.max_updates is not None,
            arguments.ack_sdk_retries,
            arguments.price_checked_on is not None,
            arguments.max_duration_seconds != 900,
        )
    ):
        parser.error("Hosted admission options require --live.")
    if arguments.prepare and not arguments.output:
        parser.error("--prepare requires a new --output artifact path.")
    if arguments.output and arguments.output.exists():
        parser.error("Refusing to overwrite an existing training artifact.")
    try:
        job = None
        epochs, batch_size = arguments.epochs, arguments.batch_size
        if arguments.live:
            job = json.loads(arguments.job.read_text(encoding="utf-8"))
            if not isinstance(job, dict) or not isinstance(job.get("config"), dict):
                raise TrainingAdmissionError("Invalid job configuration.")
            epochs, batch_size = job["config"].get("epochs"), job["config"].get("batch_size")
        preview = build_preview(
            arguments.dataset, cached_tokenizer(), epochs=epochs, batch_size=batch_size
        )
        if arguments.prepare and not preview["review_ready"]:
            raise PreparationError("Labels are still draft; review and mark train seeds first.")
        if arguments.live:
            admission = {
                "max_microdollars": arguments.max_cost_usd,
                "max_updates": arguments.max_updates,
                "acknowledge_retries": arguments.ack_sdk_retries,
                "price_checked_on": arguments.price_checked_on,
                "duration_seconds": arguments.max_duration_seconds,
            }
            validate_admission(job, preview, **admission)
            from dotenv import load_dotenv

            load_dotenv(ROOT / ".env", override=False)
            result = launch(ROOT, job, preview, **admission)
            print(json.dumps(result, indent=2))
            return 0 if result["status"] == "completed" else 1
        if arguments.output:
            arguments.output.parent.mkdir(parents=True, exist_ok=True)
            with arguments.output.open("x", encoding="utf-8") as stream:
                json.dump(preview, stream, indent=2, sort_keys=True, ensure_ascii=True)
                stream.write("\n")
        print(
            json.dumps(
                {
                    "mode": "prepared-NO-INFERENCE"
                    if arguments.prepare
                    else "preview-NO-INFERENCE",
                    "model": preview["model"],
                    "review_ready": preview["review_ready"],
                    "train_scenarios": len(preview["source_train_ids"]),
                    "examples": len(preview["datums"]),
                    "config": preview["config"],
                    "totals": preview["totals"],
                    "fingerprint": preview["fingerprint"],
                    "note": "Full context priced; final planner JSON + EOS supervised. Estimate.",
                },
                indent=2,
            )
        )
        return 0
    except (PreparationError, TrainingAdmissionError, DatasetError) as error:
        # These are our controlled errors, never SDK exception messages.
        print(f"Training refused: {error}", file=sys.stderr)
        return 1
    except (OSError, ValueError, TypeError, KeyError, ImportError):
        print(
            "Training stopped: invalid/missing artifact, dataset or cached tokenizer. "
            "No automatic download or retry; check saved run status before any new attempt.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
