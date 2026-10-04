"""Validate delivery seeds offline; oracle replay is NOT a model baseline. Live calls are opt-in."""

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from guardmate.agent.provider import (  # noqa: E402
    BUDGET_MICRODOLLARS,
    TinkerProvider,
    validate_sampler_checkpoint,
)
from guardmate.agent.store import ConversationStore  # noqa: E402
from guardmate.evaluation.comparison import ComparisonError, compare_reports  # noqa: E402
from guardmate.evaluation.dataset import (  # noqa: E402
    DatasetError,
    dataset_summary,
    load_dataset,
    select_scenarios,
)
from guardmate.evaluation.runner import (  # noqa: E402
    WORST_CALL_MICRODOLLARS,
    BoundedProvider,
    EvaluationBindingError,
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


def checkpoint_path(value: str) -> str:
    try:
        return validate_sampler_checkpoint(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def report_path(value: str) -> Path:
    path = artifact_path(value)
    if path.suffix != ".json":
        raise argparse.ArgumentTypeError("Saved comparison inputs must be .json reports.")
    return path


def assert_corpus(path: Path, expected: str) -> None:
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise DatasetError("Dataset changed during evaluation; no matched comparison is available.")


def read_report(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ComparisonError("A report must contain one JSON object.")
    return value


def write_report(path: Path | None, report: dict) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, ensure_ascii=True, allow_nan=False)
        stream.write("\n")


def printable(report: dict) -> dict:
    return {key: value for key, value in report.items() if key not in ("results", "paired_cases")}


def attach_dataset(report: dict, summary: dict) -> list[dict]:
    report["dataset"] = summary
    report["generated_at"] = datetime.now(UTC).isoformat()
    return [example for case in report["results"] for example in case.pop("training_examples")]


def attach_budget(report: dict, provider: BoundedProvider, ledger: ConversationStore, before: dict):
    report["budget"] = {
        "max_calls": provider.max_calls,
        "provider_attempts": provider.attempts - before["attempts"],
        "shared_run_provider_attempts": provider.attempts,
        "run_maximum_reserved_usd": provider.max_microdollars / 1_000_000,
        "admitted_worst_case_usd": (provider.admitted_microdollars - before["admitted"])
        / 1_000_000,
        "shared_run_admitted_worst_case_usd": provider.admitted_microdollars / 1_000_000,
        "shared_ledger_change_usd": (ledger.reserved_microdollars() - before["ledger"]) / 1_000_000,
        "shared_ledger_reserved_usd": ledger.reserved_microdollars() / 1_000_000,
        "note": "All arms share one allowance and existing ledger; estimates, not actual billing.",
    }


def complete_model_run(report: dict) -> bool:
    """Do not spend on another arm if this one cannot support a complete comparison."""
    return (
        report["requested_scenarios"] == report["evaluated_scenarios"]
        and not report["skipped_scenario_ids"]
        and all(
            not case["stopped"]
            and not case["budget_stop"]
            and case["steps_completed"] == case["steps_expected"]
            and all(
                step["model_attempted"] and step["model_plan"] is not None
                for step in case["steps"]
                if step["kind"] == "courier"
            )
            for case in report["results"]
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=ROOT / "datasets/delivery/scenarios.jsonl")
    parser.add_argument("--split", choices=("train", "validation", "test"), default="validation")
    parser.add_argument("--limit", type=positive)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--oracle", action="store_true", help="Replay authored plans, not an AI model."
    )
    modes.add_argument("--live", action="store_true", help="Explicitly enable paid model requests.")
    modes.add_argument(
        "--compare-reports",
        nargs=2,
        type=report_path,
        metavar=("BASE", "TUNED"),
        help="Compare saved reports offline; never initialize a model client.",
    )
    parser.add_argument(
        "--checkpoint", type=checkpoint_path, help="Evaluate one sampler checkpoint."
    )
    parser.add_argument(
        "--compare-checkpoint",
        type=checkpoint_path,
        help="Run matched base/tuned arms with ONE shared call/cost allowance.",
    )
    parser.add_argument("--base-output", type=artifact_path)
    parser.add_argument("--tuned-output", type=artifact_path)
    parser.add_argument("--max-calls", type=positive)
    parser.add_argument("--max-cost-usd", type=money)
    parser.add_argument("--output", type=artifact_path)
    parser.add_argument("--export-training", type=artifact_path)
    arguments = parser.parse_args()
    if arguments.checkpoint and arguments.compare_checkpoint:
        parser.error("Choose either --checkpoint or --compare-checkpoint, not both.")
    if (arguments.checkpoint or arguments.compare_checkpoint) and not arguments.live:
        parser.error("Checkpoint sampling requires --live; no model request was sent.")
    if arguments.compare_checkpoint:
        if arguments.split == "train":
            parser.error("Matched comparisons use validation/development-test splits, never train.")
        if not arguments.base_output or not arguments.tuned_output:
            parser.error("--compare-checkpoint requires --base-output and --tuned-output.")
    elif arguments.base_output or arguments.tuned_output:
        parser.error("--base-output and --tuned-output require --compare-checkpoint.")
    if arguments.compare_reports and (
        arguments.export_training
        or arguments.max_calls is not None
        or arguments.max_cost_usd is not None
    ):
        parser.error("Offline comparison cannot export training or accept hosted budget flags.")
    if arguments.live and (arguments.max_calls is None or arguments.max_cost_usd is None):
        parser.error("--live requires explicit --max-calls and --max-cost-usd.")
    if arguments.live and arguments.max_cost_usd < WORST_CALL_MICRODOLLARS:
        parser.error("Cost limit cannot admit one worst-case request ($0.004475). No call sent.")
    if arguments.export_training and (not arguments.oracle or arguments.split != "train"):
        parser.error(
            "Training export requires --oracle --split train; never export held-out splits."
        )
    outputs = [
        path
        for path in (
            arguments.output,
            arguments.export_training,
            arguments.base_output,
            arguments.tuned_output,
        )
        if path
    ]
    for path in outputs:
        if path and path.exists():
            parser.error(f"Refusing to overwrite an existing artifact: {path.name}")
    if len(set(outputs)) != len(outputs):
        parser.error("Every report/export needs a different artifact path.")
    if (arguments.compare_reports or arguments.compare_checkpoint) and any(
        path.suffix != ".json" for path in outputs
    ):
        parser.error("Comparison artifacts must be .json.")
    try:
        if arguments.compare_reports:
            comparison = compare_reports(*(read_report(path) for path in arguments.compare_reports))
            write_report(arguments.output, comparison)
            print(json.dumps(printable(comparison), indent=2))
            return 0
        corpus_before = hashlib.sha256(arguments.dataset.read_bytes()).hexdigest()
        scenarios = load_dataset(arguments.dataset)
        selected = select_scenarios(scenarios, arguments.split, arguments.limit)
        summary = dataset_summary(arguments.dataset, scenarios)
        if summary["dataset_sha256"] != corpus_before:
            raise DatasetError("Dataset changed while loading; no model request was sent.")
        calls = sum(isinstance(step, CourierStep) for case in selected for step in case.steps)
        summary["selected"] = {
            "split": arguments.split,
            "scenarios": len(selected),
            "courier_turns": calls,
            "maximum_reserved_usd": calls * WORST_CALL_MICRODOLLARS / 1_000_000,
            "estimate_basis": "Current adapter's 12k input/512 output maximum per call.",
        }
        if arguments.compare_checkpoint:
            required_calls = 2 * calls
            required_cost = required_calls * WORST_CALL_MICRODOLLARS
            if arguments.max_calls < required_calls or arguments.max_cost_usd < required_cost:
                parser.error(
                    "Shared allowance cannot admit both complete arms. "
                    f"Need at least {required_calls} calls and ${required_cost / 1_000_000:.6f}."
                )
        if not arguments.oracle and not arguments.live:
            print(json.dumps({"mode": "validation-only-NO-INFERENCE", **summary}, indent=2))
            return 0
        if arguments.export_training and any(case.review_status != "reviewed" for case in selected):
            raise DatasetError("Training export refused: review every selected seed first.")
        if arguments.live:
            assert_corpus(arguments.dataset, summary["dataset_sha256"])
            from dotenv import load_dotenv

            load_dotenv(ROOT / ".env", override=False)
            # The existing ledger is deliberately shared. Temporary ledgers would bypass the cap.
            ledger = ConversationStore(ROOT / ".data")
            if arguments.compare_checkpoint and (
                ledger.reserved_microdollars() + required_cost > BUDGET_MICRODOLLARS
            ):
                raise DatasetError("Shared inference ledger cannot admit both arms' worst case.")
            provider = BoundedProvider(
                TinkerProvider(ledger, sampler_checkpoint=arguments.checkpoint),
                arguments.max_calls,
                arguments.max_cost_usd,
            )
            before = {"ledger": ledger.reserved_microdollars(), "attempts": 0, "admitted": 0}
            report = run_evaluation(
                selected,
                provider,
                "live-tuned-model" if arguments.checkpoint else "live-base-model",
                corpus_sha256=summary["dataset_sha256"],
                split=arguments.split,
            )
            attach_budget(report, provider, ledger, before)
            corpus_changed = (
                hashlib.sha256(arguments.dataset.read_bytes()).hexdigest()
                != summary["dataset_sha256"]
            )
            if arguments.compare_checkpoint:
                attach_dataset(report, summary)
                write_report(arguments.base_output, report)
                if corpus_changed or not complete_model_run(report):
                    diagnostic = {
                        "mode": "not-run-NO-INFERENCE",
                        "reason": "Dataset changed or base arm was incomplete.",
                        "comparable": False,
                    }
                    write_report(arguments.tuned_output, diagnostic)
                    write_report(arguments.output, diagnostic)
                    print(json.dumps(diagnostic, indent=2))
                    return 1
                # Reuse counters, not a new allowance for the second arm.
                provider.provider = TinkerProvider(
                    ledger, sampler_checkpoint=arguments.compare_checkpoint
                )
                before = {
                    "ledger": ledger.reserved_microdollars(),
                    "attempts": provider.attempts,
                    "admitted": provider.admitted_microdollars,
                }
                tuned = run_evaluation(
                    selected,
                    provider,
                    "live-tuned-model",
                    corpus_sha256=summary["dataset_sha256"],
                    split=arguments.split,
                )
                attach_budget(tuned, provider, ledger, before)
                attach_dataset(tuned, summary)
                write_report(arguments.tuned_output, tuned)
                try:
                    assert_corpus(arguments.dataset, summary["dataset_sha256"])
                    comparison = compare_reports(report, tuned)
                except (ComparisonError, DatasetError) as error:
                    diagnostic = {
                        "mode": "unmatched-diagnostics-NOT-A-COMPARISON",
                        "comparable": False,
                        "reason": str(error),
                    }
                    write_report(arguments.output, diagnostic)
                    print(json.dumps(diagnostic, indent=2))
                    return 1
                write_report(arguments.output, comparison)
                print(json.dumps(printable(comparison), indent=2))
                return 0
            if corpus_changed:
                raise DatasetError(
                    "Dataset changed during model replay; no stable report available."
                )
        else:
            report = run_evaluation(
                selected,
                GoldPlanProvider(),
                "oracle",
                corpus_sha256=summary["dataset_sha256"],
                split=arguments.split,
            )
            assert_corpus(arguments.dataset, summary["dataset_sha256"])
        examples = attach_dataset(report, summary)
        if arguments.export_training:
            if not all(case["passed"] for case in report["results"]):
                raise DatasetError(
                    "Training export refused: reference replay has unresolved failures."
                )
            arguments.export_training.parent.mkdir(parents=True, exist_ok=True)
            with arguments.export_training.open("x", encoding="utf-8") as stream:
                for example in examples:
                    stream.write(json.dumps(example, ensure_ascii=True) + "\n")
        write_report(arguments.output, report)
        print(json.dumps(printable(report), indent=2))
        if report["skipped_scenario_ids"] or any(not case["passed"] for case in report["results"]):
            return 1
        return 0
    except (DatasetError, EvaluationBindingError, ComparisonError) as error:
        print(f"Evaluation stopped: {error}", file=sys.stderr)
        return 2
    except (OSError, ValueError, TypeError, KeyError):
        print(
            "Evaluation stopped: invalid/missing report or artifact. No automatic retry.",
            file=sys.stderr,
        )
        return 2
    except Exception:
        # SDK and arbitrary callback errors can contain credentials or private prompts.
        print(
            "Unexpected evaluation failure; inspect saved reports before any new attempt. "
            "No automatic retry.",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
