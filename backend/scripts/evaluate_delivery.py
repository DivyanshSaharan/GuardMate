"""Validate delivery seeds offline; oracle replay is NOT a model baseline. Live calls are opt-in."""

import argparse
import json
import sys
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from guardmate.agent.provider import TinkerProvider  # noqa: E402
from guardmate.agent.store import ConversationStore  # noqa: E402
from guardmate.evaluation.dataset import (  # noqa: E402
    DatasetError,
    dataset_summary,
    load_dataset,
    select_scenarios,
)
from guardmate.evaluation.runner import (  # noqa: E402
    WORST_CALL_MICRODOLLARS,
    BoundedProvider,
    GoldPlanProvider,
    run_evaluation,
)
from guardmate.evaluation.schema import CourierStep  # noqa: E402


def money(value: str) -> int:
    try:
        amount = Decimal(value)
        if not amount.is_finite() or not Decimal("0") < amount <= Decimal("0.25"):
            raise ValueError
        return int(amount * 1_000_000)
    except (InvalidOperation, ValueError, OverflowError):
        raise argparse.ArgumentTypeError(
            "Choose a positive amount no greater than $0.25."
        ) from None


def positive(value: str) -> int:
    number = int(value)
    if not 1 <= number <= 100:
        raise argparse.ArgumentTypeError("Choose an integer from 1 to 100.")
    return number


def artifact_path(value: str) -> Path:
    path = Path(value).resolve()
    if not path.is_relative_to((ROOT / ".data" / "evaluation").resolve()):
        raise argparse.ArgumentTypeError("Generated artifacts must stay in .data/evaluation/.")
    if path.suffix not in (".json", ".jsonl"):
        raise argparse.ArgumentTypeError("Artifact must be .json or .jsonl.")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=ROOT / "datasets/delivery/scenarios.jsonl")
    parser.add_argument("--split", choices=("train", "validation", "test"), default="validation")
    parser.add_argument("--limit", type=positive)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--oracle", action="store_true", help="Replay authored plans, not an AI model."
    )
    modes.add_argument(
        "--live", action="store_true", help="Explicitly enable paid base-Qwen requests."
    )
    parser.add_argument("--max-calls", type=positive)
    parser.add_argument("--max-cost-usd", type=money)
    parser.add_argument("--output", type=artifact_path)
    parser.add_argument("--export-training", type=artifact_path)
    arguments = parser.parse_args()
    if arguments.live and (arguments.max_calls is None or arguments.max_cost_usd is None):
        parser.error("--live requires explicit --max-calls and --max-cost-usd.")
    if arguments.live and arguments.max_cost_usd < WORST_CALL_MICRODOLLARS:
        parser.error("Cost limit cannot admit one worst-case request ($0.004475). No call sent.")
    if arguments.export_training and (not arguments.oracle or arguments.split != "train"):
        parser.error(
            "Training export requires --oracle --split train; never export held-out splits."
        )
    for path in (arguments.output, arguments.export_training):
        if path and path.exists():
            parser.error(f"Refusing to overwrite an existing artifact: {path.name}")
    if arguments.output and arguments.output == arguments.export_training:
        parser.error("Report and training export need different paths.")
    try:
        scenarios = load_dataset(arguments.dataset)
        selected = select_scenarios(scenarios, arguments.split, arguments.limit)
        summary = dataset_summary(arguments.dataset, scenarios)
        calls = sum(isinstance(step, CourierStep) for case in selected for step in case.steps)
        summary["selected"] = {
            "split": arguments.split,
            "scenarios": len(selected),
            "courier_turns": calls,
            "maximum_reserved_usd": calls * WORST_CALL_MICRODOLLARS / 1_000_000,
            "estimate_basis": "Current adapter's 12k input/512 output maximum per call.",
        }
        if not arguments.oracle and not arguments.live:
            print(json.dumps({"mode": "validation-only-NO-INFERENCE", **summary}, indent=2))
            return 0
        if arguments.export_training and any(case.review_status != "reviewed" for case in selected):
            raise DatasetError("Training export refused: review every selected seed first.")
        if arguments.live:
            from dotenv import load_dotenv

            load_dotenv(ROOT / ".env", override=False)
            # The existing ledger is deliberately shared. Temporary ledgers would bypass the cap.
            ledger = ConversationStore(ROOT / ".data")
            before = ledger.reserved_microdollars()
            provider = BoundedProvider(
                TinkerProvider(ledger), arguments.max_calls, arguments.max_cost_usd
            )
            report = run_evaluation(selected, provider, "live-base-model")
            report["budget"] = {
                "max_calls": provider.max_calls,
                "provider_attempts": provider.attempts,
                "run_maximum_reserved_usd": provider.max_microdollars / 1_000_000,
                "admitted_worst_case_usd": provider.admitted_microdollars / 1_000_000,
                "shared_ledger_change_usd": (ledger.reserved_microdollars() - before) / 1_000_000,
                "shared_ledger_reserved_usd": ledger.reserved_microdollars() / 1_000_000,
                "note": "Ledger change can include simultaneous UI activity; not actual billing.",
            }
        else:
            report = run_evaluation(selected, GoldPlanProvider(), "oracle")
        report["dataset"] = summary
        report["generated_at"] = datetime.now(UTC).isoformat()
        examples = [
            example for case in report["results"] for example in case.pop("training_examples")
        ]
        if arguments.export_training:
            if not all(case["passed"] for case in report["results"]):
                raise DatasetError(
                    "Training export refused: reference replay has unresolved failures."
                )
            arguments.export_training.parent.mkdir(parents=True, exist_ok=True)
            with arguments.export_training.open("x", encoding="utf-8") as stream:
                for example in examples:
                    stream.write(json.dumps(example, ensure_ascii=True) + "\n")
        if arguments.output:
            arguments.output.parent.mkdir(parents=True, exist_ok=True)
            with arguments.output.open("x", encoding="utf-8") as stream:
                json.dump(report, stream, indent=2, ensure_ascii=True)
        print(
            json.dumps({key: value for key, value in report.items() if key != "results"}, indent=2)
        )
        if report["skipped_scenario_ids"] or any(not case["passed"] for case in report["results"]):
            return 1
        return 0
    except (DatasetError, OSError) as error:
        print(f"Evaluation stopped: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
