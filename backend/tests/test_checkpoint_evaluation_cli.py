"""Offline CLI boundary tests; scripted providers are not hosted-model evidence."""

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import dotenv
import pytest
from guardmate.agent.models import ModelStatus
from guardmate.agent.provider import BUDGET_MICRODOLLARS, MODEL, ModelUnavailable
from guardmate.agent.store import ConversationStore
from guardmate.evaluation.dataset import load_dataset
from guardmate.evaluation.runner import WORST_CALL_MICRODOLLARS, BoundedProvider, run_evaluation

REPOSITORY = Path(__file__).resolve().parents[2]
CHECKPOINT = "tinker://session-123:train:0/sampler_weights/guardmate-final"
SECRET = "PRIVATE_EXCEPTION_VALUE_MUST_NOT_ESCAPE"


def forbidden(*args, **kwargs):
    raise AssertionError(
        "Offline/early-rejected command must not load secrets or allocate a client."
    )


def record():
    return {
        "id": "cli-validation",
        "group_id": "cli-validation",
        "split": "validation",
        "category": "dialogue-memory",
        "source": "synthetic_authored",
        "review_status": "draft",
        "rationale": "Fictional scripted reference for offline CLI boundary tests.",
        "profile": {
            "resident_name": "Fictional resident",
            "pg_name": "Fictional PG",
            "guard_location": "the guard room",
        },
        "availability": "at_office",
        "steps": [
            {
                "kind": "courier",
                "text": "I have an order to deliver",
                "gold_plan": {"action": "clarify", "question": "prepaid"},
                "expect": {
                    "actions": ["clarify"],
                    "status": "active",
                    "authorized_location": "none",
                    "pending_question": "prepaid",
                    "forbidden_handoff": True,
                },
            },
            {
                "kind": "courier",
                "text": "yes",
                "gold_plan": {
                    "action": "clarify",
                    "question": "guard_available",
                    "observation": {"prepaid": True, "evidence": "yes"},
                },
                "expect": {
                    "actions": ["clarify"],
                    "status": "active",
                    "authorized_location": "none",
                    "pending_question": "guard_available",
                    "forbidden_handoff": True,
                },
            },
        ],
    }


def write_dataset(cli, records):
    path = cli.ROOT / "datasets/delivery/scenarios.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in records), encoding="utf-8")
    return path


@pytest.fixture
def cli(tmp_path, monkeypatch):
    script = REPOSITORY / "backend/scripts/evaluate_delivery.py"
    spec = importlib.util.spec_from_file_location("checkpoint_evaluation_cli_test", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "TinkerProvider", forbidden)
    monkeypatch.setattr(module, "ConversationStore", forbidden)
    monkeypatch.setattr(dotenv, "load_dotenv", forbidden)
    write_dataset(module, [record()])
    return module


def invoke(cli, monkeypatch, *arguments):
    monkeypatch.setattr(sys, "argv", ["evaluate_delivery.py", *map(str, arguments)])
    return cli.main()


def artifact(cli, name):
    return cli.ROOT / ".data/evaluation" / name


def pair_args(cli, *, calls=4, cost="0.017900"):
    return [
        "--live",
        "--compare-checkpoint",
        CHECKPOINT,
        "--max-calls",
        str(calls),
        "--max-cost-usd",
        str(cost),
        "--base-output",
        artifact(cli, "base.json"),
        "--tuned-output",
        artifact(cli, "tuned.json"),
        "--output",
        artifact(cli, "comparison.json"),
    ]


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


class ScriptedProvider:
    """Explicit test double with reference plans and optional simulated provider failures."""

    def __init__(self, plans, *, sampler_checkpoint=None, ledger=None):
        self.plans = list(plans)
        self.sampler_checkpoint = sampler_checkpoint
        self.ledger = ledger
        self.calls = []
        self.fail = False

    def status(self):
        return ModelStatus(
            configured=True,
            model=MODEL,
            provider="scripted-unit-test-NOT-HOSTED-INFERENCE",
            message="Test double only; no model request.",
            reserved_usd=0,
            budget_usd=0.25,
        )

    def generate(self, messages):
        self.calls.append(messages)
        if self.ledger is not None:
            assert self.ledger.reserve(1000, BUDGET_MICRODOLLARS)
        if self.fail:
            # Production TinkerProvider already sanitizes SDK exceptions to this shape.
            raise ModelUnavailable("Qwen failed (RuntimeError). No handoff was authorized.")
        return self.plans.pop(0).model_copy(deep=True)


def plans_for(scenarios):
    return [
        step.gold_plan
        for scenario in scenarios
        for step in scenario.steps
        if step.kind == "courier"
    ]


def install_live_double(cli, monkeypatch, *, fail_arm=None, initial_reserved=0):
    scenarios = load_dataset(cli.ROOT / "datasets/delivery/scenarios.jsonl")
    plans = plans_for([case for case in scenarios if case.split == "validation"])
    instances, ledgers, paths, bounded, environment_loads = [], [], [], [], []

    def ledger_factory(path):
        paths.append(path)
        store = ConversationStore(path)
        if initial_reserved:
            assert store.reserve(initial_reserved, BUDGET_MICRODOLLARS)
        ledgers.append(store)
        return store

    def provider_factory(store, *, sampler_checkpoint=None):
        provider = ScriptedProvider(plans, sampler_checkpoint=sampler_checkpoint, ledger=store)
        provider.fail = len(instances) == fail_arm
        instances.append(provider)
        return provider

    class TrackedBounded(BoundedProvider):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            bounded.append(self)

    monkeypatch.setattr(cli, "ConversationStore", ledger_factory)
    monkeypatch.setattr(cli, "TinkerProvider", provider_factory)
    monkeypatch.setattr(cli, "BoundedProvider", TrackedBounded)
    monkeypatch.setattr(
        dotenv, "load_dotenv", lambda path, **kwargs: environment_loads.append((path, kwargs))
    )
    return SimpleNamespace(
        instances=instances,
        ledgers=ledgers,
        paths=paths,
        bounded=bounded,
        environment_loads=environment_loads,
    )


def write_saved_pair(cli):
    scenarios = load_dataset(cli.ROOT / "datasets/delivery/scenarios.jsonl")
    paths = [artifact(cli, "saved-base.json"), artifact(cli, "saved-tuned.json")]
    for checkpoint, mode, path in zip(
        (None, CHECKPOINT), ("live-base-model", "live-tuned-model"), paths, strict=True
    ):
        provider = ScriptedProvider(plans_for(scenarios), sampler_checkpoint=checkpoint)
        report = run_evaluation(
            scenarios, provider, mode, corpus_sha256="a" * 64, split="validation"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report), encoding="utf-8")
    return paths


def test_legacy_default_remains_offline_without_environment_or_ledger(cli, monkeypatch, capsys):
    assert invoke(cli, monkeypatch) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "validation-only-NO-INFERENCE"
    assert not (cli.ROOT / ".data").exists()


@pytest.mark.parametrize(
    "arguments",
    [
        ["--checkpoint", CHECKPOINT],
        ["--oracle", "--checkpoint", CHECKPOINT],
        ["--compare-checkpoint", CHECKPOINT],
        ["--oracle", "--compare-checkpoint", CHECKPOINT],
        ["--live", "--checkpoint", CHECKPOINT, "--compare-checkpoint", CHECKPOINT],
        ["--live", "--checkpoint", "tinker://run/weights/full"],
        ["--live", "--checkpoint", CHECKPOINT + "?query=value"],
        ["--live", "--compare-checkpoint", CHECKPOINT + "#fragment"],
        ["--live", "--checkpoint", CHECKPOINT],
        ["--live", "--checkpoint", CHECKPOINT, "--max-calls", "2"],
        ["--live", "--checkpoint", CHECKPOINT, "--max-cost-usd", "0.02"],
        ["--live", "--compare-checkpoint", CHECKPOINT, "--max-calls", "4"],
        ["--live", "--compare-checkpoint", CHECKPOINT, "--max-cost-usd", "0.02"],
        ["--live", "--compare-checkpoint", CHECKPOINT, "--split", "train"],
        [
            "--live",
            "--compare-checkpoint",
            CHECKPOINT,
            "--max-calls",
            "4",
            "--max-cost-usd",
            "0.02",
        ],
    ],
)
def test_invalid_modes_and_missing_limits_reject_before_private_access(cli, monkeypatch, arguments):
    monkeypatch.setattr(cli, "load_dataset", forbidden)
    with pytest.raises(SystemExit) as error:
        invoke(cli, monkeypatch, *arguments)
    assert error.value.code == 2
    assert not (cli.ROOT / ".data").exists()


@pytest.mark.parametrize("flag", ["--base-output", "--tuned-output"])
def test_arm_outputs_require_paired_sampling(cli, monkeypatch, flag):
    with pytest.raises(SystemExit) as error:
        invoke(cli, monkeypatch, flag, artifact(cli, "wrong.json"))
    assert error.value.code == 2


@pytest.mark.parametrize(
    "calls,cost",
    [(3, "0.017900"), (4, "0.017899"), (100, "0.004475"), (4, "0.004474")],
)
def test_paired_full_worst_case_admission_precedes_environment_and_provider(
    cli, monkeypatch, calls, cost
):
    with pytest.raises(SystemExit) as error:
        invoke(cli, monkeypatch, *pair_args(cli, calls=calls, cost=cost))
    assert error.value.code == 2
    assert not (cli.ROOT / ".data").exists()


@pytest.mark.parametrize("output_name", ["base.json", "tuned.json", "comparison.json"])
def test_paired_existing_outputs_are_never_overwritten(cli, monkeypatch, output_name):
    path = artifact(cli, output_name)
    path.parent.mkdir(parents=True)
    path.write_text("existing-private-artifact", encoding="utf-8")
    with pytest.raises(SystemExit) as error:
        invoke(cli, monkeypatch, *pair_args(cli))
    assert error.value.code == 2
    assert path.read_text(encoding="utf-8") == "existing-private-artifact"


def test_all_paired_outputs_must_have_distinct_resolved_paths(cli, monkeypatch):
    args = pair_args(cli)
    args[args.index("--tuned-output") + 1] = artifact(cli, "base.json")
    with pytest.raises(SystemExit) as error:
        invoke(cli, monkeypatch, *args)
    assert error.value.code == 2
    assert not (cli.ROOT / ".data").exists()


@pytest.mark.parametrize(
    "path", ["outside.json", ".data/evaluation/../outside.json", ".data/evaluation/result.txt"]
)
def test_generated_artifacts_cannot_escape_the_ignored_evaluation_directory(cli, monkeypatch, path):
    args = pair_args(cli)
    args[args.index("--output") + 1] = cli.ROOT / path
    with pytest.raises(SystemExit) as error:
        invoke(cli, monkeypatch, *args)
    assert error.value.code == 2
    assert not (cli.ROOT / ".data").exists()


@pytest.mark.parametrize("flag", ["--base-output", "--tuned-output", "--output"])
def test_paired_comparison_outputs_require_json_not_jsonl(cli, monkeypatch, flag):
    args = pair_args(cli)
    args[args.index(flag) + 1] = artifact(cli, "not-a-report.jsonl")
    with pytest.raises(SystemExit) as error:
        invoke(cli, monkeypatch, *args)
    assert error.value.code == 2
    assert not (cli.ROOT / ".data").exists()


def test_offline_saved_comparison_does_not_read_dataset_environment_or_ledger(
    cli, monkeypatch, capsys
):
    base, tuned = write_saved_pair(cli)
    monkeypatch.setattr(cli, "load_dataset", forbidden)
    output = artifact(cli, "offline-comparison.json")
    assert invoke(cli, monkeypatch, "--compare-reports", base, tuned, "--output", output) == 0
    result = read_json(output)
    assert result["comparable"] is True
    assert result["mode"] == "matched-development-comparison"
    assert result["targets"]["tuned"]["sampler_checkpoint"] == CHECKPOINT
    assert "winner" not in result
    printed = json.loads(capsys.readouterr().out)
    assert "paired_cases" not in printed
    assert result["summary"]["verified_receipts"]["n"] == 0


@pytest.mark.parametrize(
    "extra", [["--max-calls", "4"], ["--max-cost-usd", "0.02"], ["--oracle"], ["--live"]]
)
def test_offline_comparison_refuses_hosted_budget_flags_or_other_modes(cli, monkeypatch, extra):
    with pytest.raises(SystemExit) as error:
        invoke(
            cli,
            monkeypatch,
            "--compare-reports",
            artifact(cli, "base.json"),
            artifact(cli, "tuned.json"),
            *extra,
        )
    assert error.value.code == 2
    assert not (cli.ROOT / ".data").exists()


@pytest.mark.parametrize("contents", ["not-json-" + SECRET, json.dumps([SECRET])])
def test_bad_saved_reports_fail_safely_without_echoing_their_contents(
    cli, monkeypatch, capsys, contents
):
    base, tuned = artifact(cli, "bad-base.json"), artifact(cli, "bad-tuned.json")
    base.parent.mkdir(parents=True)
    base.write_text(contents, encoding="utf-8")
    tuned.write_text("{}", encoding="utf-8")
    assert invoke(cli, monkeypatch, "--compare-reports", base, tuned) == 2
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err


def test_missing_saved_report_is_safe_and_initializes_no_provider(cli, monkeypatch, capsys):
    assert (
        invoke(
            cli,
            monkeypatch,
            "--compare-reports",
            artifact(cli, "missing-base.json"),
            artifact(cli, "missing-tuned.json"),
        )
        == 2
    )
    assert "invalid/missing report or artifact" in capsys.readouterr().err


@pytest.mark.parametrize(
    "reserved", [BUDGET_MICRODOLLARS, BUDGET_MICRODOLLARS - 4 * WORST_CALL_MICRODOLLARS + 1]
)
def test_global_persistent_allowance_exhaustion_precedes_model_allocation(
    cli, monkeypatch, capsys, reserved
):
    state = install_live_double(cli, monkeypatch, initial_reserved=reserved)
    assert invoke(cli, monkeypatch, *pair_args(cli)) == 2
    assert state.instances == state.bounded == []
    assert state.paths == [cli.ROOT / ".data"]
    assert state.ledgers[0].reserved_microdollars() == reserved
    assert "ledger cannot admit both arms" in capsys.readouterr().err
    assert not artifact(cli, "base.json").exists()


def test_paired_sampling_has_one_allowance_and_exact_shared_persistent_ledger(cli, monkeypatch):
    state = install_live_double(cli, monkeypatch, initial_reserved=1234)
    assert invoke(cli, monkeypatch, *pair_args(cli)) == 0
    assert len(state.bounded) == 1
    assert state.paths == [cli.ROOT / ".data"]
    assert state.environment_loads == [(cli.ROOT / ".env", {"override": False})]
    assert [provider.sampler_checkpoint for provider in state.instances] == [None, CHECKPOINT]
    assert all(provider.ledger is state.ledgers[0] for provider in state.instances)
    allowance = state.bounded[0]
    assert allowance.attempts == 4
    assert allowance.admitted_microdollars == 4 * WORST_CALL_MICRODOLLARS
    assert allowance.max_microdollars == 4 * WORST_CALL_MICRODOLLARS
    assert state.ledgers[0].reserved_microdollars() == 1234 + 4000
    base, tuned = read_json(artifact(cli, "base.json")), read_json(artifact(cli, "tuned.json"))
    assert base["mode"] == "live-base-model"
    assert tuned["mode"] == "live-tuned-model"
    assert base["budget"]["provider_attempts"] == tuned["budget"]["provider_attempts"] == 2
    assert base["budget"]["shared_run_provider_attempts"] == 2
    assert tuned["budget"]["shared_run_provider_attempts"] == 4
    assert tuned["budget"]["shared_run_admitted_worst_case_usd"] == 0.0179
    assert base["evaluation_binding"] == tuned["evaluation_binding"]
    assert all(
        "training_examples" not in case for report in (base, tuned) for case in report["results"]
    )
    assert read_json(artifact(cli, "comparison.json"))["comparable"] is True


def test_single_checkpoint_explicitly_selects_tuned_target_without_changing_base(cli, monkeypatch):
    state = install_live_double(cli, monkeypatch)
    output = artifact(cli, "single-tuned.json")
    assert (
        invoke(
            cli,
            monkeypatch,
            "--live",
            "--checkpoint",
            CHECKPOINT,
            "--max-calls",
            "2",
            "--max-cost-usd",
            "0.008950",
            "--output",
            output,
        )
        == 0
    )
    assert len(state.instances) == len(state.bounded) == 1
    assert state.instances[0].sampler_checkpoint == CHECKPOINT
    report = read_json(output)
    assert report["mode"] == "live-tuned-model"
    assert report["model"] == MODEL
    assert report["target"] == {
        "kind": "tuned",
        "base_model": MODEL,
        "sampler_checkpoint": CHECKPOINT,
    }
    assert report["budget"]["provider_attempts"] == 2


def test_first_arm_failure_never_allocates_or_calls_a_second_provider(cli, monkeypatch, capsys):
    state = install_live_double(cli, monkeypatch, fail_arm=0)
    assert invoke(cli, monkeypatch, *pair_args(cli)) == 1
    assert len(state.instances) == 1
    assert len(state.instances[0].calls) == state.bounded[0].attempts == 1
    assert read_json(artifact(cli, "base.json"))["results"][0]["stopped"] is True
    diagnostic = read_json(artifact(cli, "tuned.json"))
    assert diagnostic == read_json(artifact(cli, "comparison.json"))
    assert diagnostic["mode"] == "not-run-NO-INFERENCE"
    assert diagnostic["comparable"] is False
    assert "base arm was incomplete" in diagnostic["reason"].lower()
    assert "winner" not in diagnostic
    assert SECRET not in capsys.readouterr().out


def test_second_arm_failure_writes_diagnostics_not_a_fake_comparison(cli, monkeypatch):
    state = install_live_double(cli, monkeypatch, fail_arm=1)
    assert invoke(cli, monkeypatch, *pair_args(cli)) == 1
    assert len(state.instances) == 2
    assert [len(provider.calls) for provider in state.instances] == [2, 1]
    assert state.bounded[0].attempts == 3
    assert read_json(artifact(cli, "base.json"))["results"][0]["stopped"] is False
    assert read_json(artifact(cli, "tuned.json"))["results"][0]["stopped"] is True
    diagnostic = read_json(artifact(cli, "comparison.json"))
    assert diagnostic["mode"] == "unmatched-diagnostics-NOT-A-COMPARISON"
    assert diagnostic["comparable"] is False
    assert "winner" not in diagnostic


def test_artifact_io_exception_is_safe_and_never_replays_a_completed_arm(cli, monkeypatch, capsys):
    state = install_live_double(cli, monkeypatch)

    def fail_write(*args, **kwargs):
        raise OSError(SECRET)

    monkeypatch.setattr(cli, "write_report", fail_write)
    assert invoke(cli, monkeypatch, *pair_args(cli)) == 2
    assert len(state.instances) == 1
    assert len(state.instances[0].calls) == 2
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err
    assert "No automatic retry" in captured.err


def test_all_current_validation_scripts_replay_with_shared_pair_accounting(cli, monkeypatch):
    scenarios = load_dataset(REPOSITORY / "datasets/delivery/scenarios.jsonl")
    selected = [scenario for scenario in scenarios if scenario.split == "validation"]
    write_dataset(cli, [scenario.model_dump(mode="json") for scenario in selected])
    calls = 2 * len(plans_for(selected))
    state = install_live_double(cli, monkeypatch)
    assert (
        invoke(
            cli,
            monkeypatch,
            *pair_args(cli, calls=calls, cost=f"{calls * WORST_CALL_MICRODOLLARS / 1_000_000:.6f}"),
        )
        == 0
    )
    assert len(state.bounded) == 1
    assert state.bounded[0].attempts == calls
    comparison = read_json(artifact(cli, "comparison.json"))
    assert comparison["comparable"] is True
    assert comparison["evaluation_binding"]["selected_ids"] == [
        scenario.id for scenario in selected
    ]
    assert comparison["summary"]["scenario_rubric_success"]["n"] == len(selected)


@pytest.mark.parametrize("name", ["input.jsonl", "input.txt"])
def test_offline_comparison_inputs_require_json_reports(cli, monkeypatch, name):
    monkeypatch.setattr(cli, "load_dataset", forbidden)
    with pytest.raises(SystemExit) as error:
        invoke(
            cli,
            monkeypatch,
            "--compare-reports",
            artifact(cli, name),
            artifact(cli, "other.json"),
        )
    assert error.value.code == 2
    assert not (cli.ROOT / ".data").exists()


def test_unexpected_constructor_error_is_safe_without_retry_or_second_arm(cli, monkeypatch, capsys):
    state = install_live_double(cli, monkeypatch)
    calls = []

    def fail_constructor(*args, **kwargs):
        calls.append(True)
        raise RuntimeError(SECRET)

    monkeypatch.setattr(cli, "TinkerProvider", fail_constructor)
    assert invoke(cli, monkeypatch, *pair_args(cli)) == 2
    assert calls == [True]
    assert state.bounded == state.instances == []
    assert state.ledgers[0].reserved_microdollars() == 0
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err
    assert "Unexpected evaluation failure" in captured.err
    assert not artifact(cli, "comparison.json").exists()


def test_unexpected_sampling_error_is_safe_and_preserves_existing_reservation(
    cli, monkeypatch, capsys
):
    state = install_live_double(cli, monkeypatch)

    def fail_sample(self, messages):
        self.calls.append(messages)
        assert self.ledger.reserve(1000, BUDGET_MICRODOLLARS)
        raise RuntimeError(SECRET)

    monkeypatch.setattr(ScriptedProvider, "generate", fail_sample)
    assert invoke(cli, monkeypatch, *pair_args(cli)) == 2
    assert len(state.instances) == state.bounded[0].attempts == 1
    assert len(state.instances[0].calls) == 1
    assert state.ledgers[0].reserved_microdollars() == 1000
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err
    assert "No automatic retry" in captured.err
    assert not artifact(cli, "comparison.json").exists()


def test_corpus_change_after_selection_rejects_before_environment_and_model(
    cli, monkeypatch, capsys
):
    summarize = cli.dataset_summary

    def changed_summary(path, scenarios):
        summary = summarize(path, scenarios)
        path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        return summary

    monkeypatch.setattr(cli, "dataset_summary", changed_summary)
    assert invoke(cli, monkeypatch, *pair_args(cli)) == 2
    assert "Dataset changed" in capsys.readouterr().err
    assert not (cli.ROOT / ".data").exists()


@pytest.mark.parametrize("changed_arm", ["live-base-model", "live-tuned-model"])
def test_corpus_changes_during_an_arm_preserve_diagnostics_without_comparison(
    cli, monkeypatch, changed_arm
):
    state = install_live_double(cli, monkeypatch)
    replay = cli.run_evaluation

    def changed_replay(scenarios, provider, mode, **kwargs):
        report = replay(scenarios, provider, mode, **kwargs)
        if mode == changed_arm:
            path = cli.ROOT / "datasets/delivery/scenarios.jsonl"
            path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        return report

    monkeypatch.setattr(cli, "run_evaluation", changed_replay)
    assert invoke(cli, monkeypatch, *pair_args(cli)) == 1
    diagnostic = read_json(artifact(cli, "comparison.json"))
    assert diagnostic["comparable"] is False
    assert "Dataset changed" in diagnostic["reason"]
    assert "winner" not in diagnostic
    if changed_arm == "live-base-model":
        assert len(state.instances) == 1
        assert state.bounded[0].attempts == 2
        assert read_json(artifact(cli, "tuned.json"))["mode"] == "not-run-NO-INFERENCE"
    else:
        assert len(state.instances) == 2
        assert state.bounded[0].attempts == 4
        assert read_json(artifact(cli, "tuned.json"))["mode"] == "live-tuned-model"


def test_single_checkpoint_corpus_change_does_not_write_a_stable_report(cli, monkeypatch, capsys):
    state = install_live_double(cli, monkeypatch)
    replay = cli.run_evaluation

    def changed_replay(*args, **kwargs):
        report = replay(*args, **kwargs)
        path = cli.ROOT / "datasets/delivery/scenarios.jsonl"
        path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        return report

    monkeypatch.setattr(cli, "run_evaluation", changed_replay)
    output = artifact(cli, "single-changed.json")
    assert (
        invoke(
            cli,
            monkeypatch,
            "--live",
            "--checkpoint",
            CHECKPOINT,
            "--max-calls",
            "2",
            "--max-cost-usd",
            "0.008950",
            "--output",
            output,
        )
        == 2
    )
    assert len(state.instances[0].calls) == 2
    assert not output.exists()
    assert "Dataset changed" in capsys.readouterr().err
