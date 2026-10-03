"""CLI opt-in gates with isolated artifacts and offline preview/process doubles."""

import argparse
import copy
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from guardmate.training import supervisor
from guardmate.training.preparation import PreparationError, fingerprint


def preview(*, reviewed=False):
    value = {
        "model": "offline-preview-model",
        "review_ready": reviewed,
        "source_train_ids": ["fictional-train-a"],
        "source_hashes": {"dataset_sha256": "offline-current-dataset"},
        "config": {"epochs": 1, "batch_size": 4},
        "datums": [{"input_tokens": [1, 2], "target_tokens": [2, 3], "weights": [0, 1]}],
        "schedule": [{"reserved_microdollars": 100}],
        "totals": {"reserved_microdollars": 100, "logical_batches": 1},
    }
    value["fingerprint"] = fingerprint(value)
    return value


@pytest.fixture
def cli(tmp_path, monkeypatch):
    script = Path(__file__).resolve().parents[1] / "scripts" / "train_delivery.py"
    specification = importlib.util.spec_from_file_location("offline_training_cli", script)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    calls = []
    current = preview()

    def fake_tokenizer():
        calls.append(("cached-tokenizer",))
        return "offline-cached-tokenizer"

    def fake_preview(path, tokenizer, *, epochs, batch_size):
        calls.append(("preview", path, tokenizer, epochs, batch_size))
        return copy.deepcopy(current)

    def forbidden_launch(*args, **kwargs):
        raise AssertionError("Hosted launch must be explicitly replaced by an offline test double")

    def forbidden_env(*args, **kwargs):
        raise AssertionError("This path must not load .env")

    monkeypatch.setattr(module, "cached_tokenizer", fake_tokenizer)
    monkeypatch.setattr(module, "build_preview", fake_preview)
    monkeypatch.setattr(module, "launch", forbidden_launch)
    monkeypatch.setitem(sys.modules, "dotenv", SimpleNamespace(load_dotenv=forbidden_env))
    return module, tmp_path, calls, current


def invoke(cli, monkeypatch, *arguments):
    module = cli[0]
    monkeypatch.setattr(sys, "argv", ["train_delivery.py", *map(str, arguments)])
    return module.main()


def write_job(root, value):
    path = root / ".data" / "training" / "job.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def live_arguments(path, *extra):
    return [
        "--live",
        "--job",
        path,
        "--max-cost-usd",
        "0.01",
        "--max-updates",
        "1",
        "--ack-sdk-retries",
        "--price-checked-on",
        supervisor.PRICE_CHECKED_ON,
        *extra,
    ]


def test_default_preview_is_offline_no_env_provider_or_ledger(cli, monkeypatch, capsys):
    _, root, calls, _ = cli
    monkeypatch.setenv("TINKER_API_KEY", "do-not-use-or-print-this-test-value")
    (root / ".env").write_text("unrelated-private-fixture", encoding="utf-8")
    assert invoke(cli, monkeypatch) == 0
    output = capsys.readouterr().out
    data = json.loads(output)
    assert data["mode"] == "preview-NO-INFERENCE"
    assert data["review_ready"] is False
    assert data["examples"] == 1
    assert "do-not-use-or-print-this-test-value" not in output
    assert calls[0] == ("cached-tokenizer",)
    assert calls[1][1] == root / "datasets" / "delivery" / "scenarios.jsonl"
    assert not (root / ".data").exists()


def test_prepare_draft_is_refused_without_creating_artifact(cli, monkeypatch, capsys):
    _, root, _, _ = cli
    output = root / ".data" / "training" / "prepared.json"
    assert invoke(cli, monkeypatch, "--prepare", "--output", output) == 1
    assert "draft" in capsys.readouterr().err
    assert not output.exists()
    assert not (root / ".data").exists()


def test_prepare_reviewed_exports_bound_artifact_without_env_or_training(cli, monkeypatch, capsys):
    _, root, _, current = cli
    current["review_ready"] = True
    current["fingerprint"] = fingerprint(current)
    output = root / ".data" / "training" / "prepared.json"
    assert invoke(cli, monkeypatch, "--prepare", "--output", output) == 0
    assert json.loads(output.read_text(encoding="utf-8")) == current
    assert json.loads(capsys.readouterr().out)["mode"] == "prepared-NO-INFERENCE"
    assert not (root / ".data" / "guardmate-training.sqlite3").exists()


def test_optional_output_can_save_draft_preview_but_does_not_mark_it_reviewed(cli, monkeypatch):
    _, root, _, _ = cli
    output = root / ".data" / "training" / "draft-preview.json"
    assert invoke(cli, monkeypatch, "--output", output) == 0
    assert json.loads(output.read_text(encoding="utf-8"))["review_ready"] is False
    assert not (root / ".data" / "guardmate-training.sqlite3").exists()


@pytest.mark.parametrize(
    "arguments", [["--split", "validation"], ["--split", "test"], ["--include-heldout"]]
)
def test_cli_has_no_implicit_heldout_training_switch(cli, monkeypatch, arguments):
    _, root, calls, _ = cli
    with pytest.raises(SystemExit) as error:
        invoke(cli, monkeypatch, *arguments)
    assert error.value.code == 2
    assert not calls
    assert not (root / ".data").exists()


@pytest.mark.parametrize(
    "name",
    [
        "elsewhere.json",
        ".data/evaluation/job.json",
        ".data/training/../escape.json",
        ".data/training/job.jsonl",
    ],
)
def test_output_path_must_stay_in_training_artifact_directory(cli, monkeypatch, name):
    _, root, calls, _ = cli
    with pytest.raises(SystemExit) as error:
        invoke(cli, monkeypatch, "--output", root / name)
    assert error.value.code == 2
    assert not calls


def test_output_existing_file_is_never_overwritten_or_previewed(cli, monkeypatch):
    _, root, calls, _ = cli
    path = root / ".data" / "training" / "existing.json"
    path.parent.mkdir(parents=True)
    path.write_text("preserve-fixture", encoding="utf-8")
    with pytest.raises(SystemExit) as error:
        invoke(cli, monkeypatch, "--output", path)
    assert error.value.code == 2
    assert path.read_text(encoding="utf-8") == "preserve-fixture"
    assert not calls


def test_prepare_requires_explicit_output(cli, monkeypatch):
    with pytest.raises(SystemExit) as error:
        invoke(cli, monkeypatch, "--prepare")
    assert error.value.code == 2
    assert not cli[2]


@pytest.mark.parametrize(
    "arguments",
    [
        ["--live"],
        ["--live", "--job", "JOB"],
        ["--live", "--job", "JOB", "--max-cost-usd", "0.01"],
        ["--live", "--job", "JOB", "--max-cost-usd", "0.01", "--max-updates", "1"],
        [
            "--live",
            "--job",
            "JOB",
            "--max-cost-usd",
            "0.01",
            "--max-updates",
            "1",
            "--ack-sdk-retries",
        ],
    ],
)
def test_live_requires_every_opt_in_before_tokenizer_env_or_launch(cli, monkeypatch, arguments):
    _, root, calls, _ = cli
    replaced = [
        root / ".data" / "training" / "job.json" if item == "JOB" else item for item in arguments
    ]
    with pytest.raises(SystemExit) as error:
        invoke(cli, monkeypatch, *replaced)
    assert error.value.code == 2
    assert not calls
    assert not (root / ".data").exists()


@pytest.mark.parametrize(
    "arguments",
    [
        ["--max-cost-usd", "0.01"],
        ["--max-updates", "1"],
        ["--ack-sdk-retries"],
        ["--price-checked-on", "2026-10-04"],
        ["--max-duration-seconds", "60"],
    ],
)
def test_hosted_admission_options_are_not_allowed_without_live(cli, monkeypatch, arguments):
    with pytest.raises(SystemExit) as error:
        invoke(cli, monkeypatch, *arguments)
    assert error.value.code == 2
    assert not cli[2]


def test_live_cannot_mix_output_export_with_hosted_launch(cli, monkeypatch):
    _, root, calls, _ = cli
    job = root / ".data" / "training" / "job.json"
    output = root / ".data" / "training" / "other.json"
    with pytest.raises(SystemExit) as error:
        invoke(cli, monkeypatch, *live_arguments(job), "--output", output)
    assert error.value.code == 2
    assert not calls


@pytest.mark.parametrize(
    "edit", ["unhashed", "rehashed-source", "rehashed-datums", "rehashed-config"]
)
def test_live_edited_job_is_blocked_before_loading_env_or_launch(cli, monkeypatch, capsys, edit):
    _, root, _, current = cli
    current["review_ready"] = True
    current["fingerprint"] = fingerprint(current)
    job = copy.deepcopy(current)
    if edit == "rehashed-source":
        job["source_hashes"]["dataset_sha256"] = "stale-dataset"
    elif edit == "rehashed-config":
        job["config"]["epochs"] = 2
    else:
        job["datums"][0]["input_tokens"][0] = 999
    if edit != "unhashed":
        job["fingerprint"] = fingerprint(job)
    assert invoke(cli, monkeypatch, *live_arguments(write_job(root, job))) == 1
    assert "Training refused" in capsys.readouterr().err
    assert not (root / ".data" / "guardmate-training.sqlite3").exists()


def test_live_review_gate_runs_before_env_and_launch(cli, monkeypatch, capsys):
    _, root, _, current = cli
    assert invoke(cli, monkeypatch, *live_arguments(write_job(root, current))) == 1
    assert "Draft" in capsys.readouterr().err
    assert not (root / ".data" / "guardmate-training.sqlite3").exists()


@pytest.mark.parametrize(
    "extra",
    [
        ["--price-checked-on", "1900-01-01"],
        ["--max-duration-seconds", "59"],
        ["--max-duration-seconds", "1801"],
    ],
)
def test_live_stale_rate_and_bad_timeout_are_blocked_before_env(cli, monkeypatch, extra):
    _, root, _, current = cli
    current["review_ready"] = True
    current["fingerprint"] = fingerprint(current)
    assert invoke(cli, monkeypatch, *live_arguments(write_job(root, current), *extra)) == 1
    assert not (root / ".data" / "guardmate-training.sqlite3").exists()


@pytest.mark.parametrize("status,exit_code", [("completed", 0), ("unknown", 1)])
def test_live_launches_only_after_validated_admission_and_env_loading(
    cli, monkeypatch, capsys, status, exit_code
):
    module, root, calls, current = cli
    current["review_ready"] = True
    current["fingerprint"] = fingerprint(current)

    def fake_env(path, *, override):
        assert path == root / ".env"
        assert override is False
        calls.append(("load-env",))

    def fake_launch(passed_root, job, actual_preview, **admission):
        assert calls[-1] == ("load-env",)
        assert passed_root == root
        assert job == actual_preview == current
        assert admission["max_microdollars"] == 10_000
        assert admission["max_updates"] == 1
        calls.append(("offline-launch-double",))
        return {"status": status, "run_id": "offline-fixture"}

    monkeypatch.setitem(sys.modules, "dotenv", SimpleNamespace(load_dotenv=fake_env))
    monkeypatch.setattr(module, "launch", fake_launch)
    assert invoke(cli, monkeypatch, *live_arguments(write_job(root, current))) == exit_code
    assert calls[-1] == ("offline-launch-double",)
    assert json.loads(capsys.readouterr().out)["status"] == status
    assert not (root / ".data" / "guardmate-training.sqlite3").exists()


def test_invalid_json_live_job_does_not_even_request_tokenizer(cli, monkeypatch, capsys):
    _, root, calls, _ = cli
    path = write_job(root, {})
    path.write_text("{", encoding="utf-8")
    assert invoke(cli, monkeypatch, *live_arguments(path)) == 1
    assert not calls
    assert "No automatic download or retry" in capsys.readouterr().err


def test_missing_cached_tokenizer_is_controlled_and_never_downloads(cli, monkeypatch, capsys):
    module, root, _, _ = cli

    def missing():
        raise OSError("synthetic cache miss with private fixture text")

    monkeypatch.setattr(module, "cached_tokenizer", missing)
    assert invoke(cli, monkeypatch) == 1
    error = capsys.readouterr().err
    assert "No automatic download or retry" in error
    assert "private fixture" not in error
    assert not (root / ".data").exists()


def test_preparation_error_is_controlled_and_does_not_load_env(cli, monkeypatch):
    module = cli[0]

    def refuse(*args, **kwargs):
        raise PreparationError("Every train reference must pass")

    monkeypatch.setattr(module, "build_preview", refuse)
    assert invoke(cli, monkeypatch) == 1


@pytest.mark.parametrize("value", ["0", "-1", "1.750001", "NaN", "Infinity", "0.0000001", "bad"])
def test_money_limit_rejects_invalid_or_unbounded_precision(cli, value):
    with pytest.raises(argparse.ArgumentTypeError):
        cli[0].money(value)


@pytest.mark.parametrize("value,expected", [("0.000001", 1), ("1.75", 1_750_000), ("0.01", 10_000)])
def test_money_limit_preserves_exact_microdollars(cli, value, expected):
    assert cli[0].money(value) == expected
