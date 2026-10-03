"""Offline admission/process doubles; never initialize the hosted SDK."""

import copy
import json
import subprocess
from importlib.metadata import PackageNotFoundError
from pathlib import Path
from types import SimpleNamespace

import pytest
from guardmate.training import supervisor
from guardmate.training.ledger import TrainingBudgetError, TrainingLedger
from guardmate.training.preparation import fingerprint


def example_job(*, reviewed=True, marker="original"):
    job = {
        "review_ready": reviewed,
        "source_hashes": {"dataset_sha256": marker},
        "config": {"epochs": 1, "batch_size": 4},
        "schedule": [
            {"reserved_microdollars": 100},
            {"reserved_microdollars": 200},
        ],
        "totals": {"reserved_microdollars": 300, "logical_batches": 2},
    }
    job["fingerprint"] = fingerprint(job)
    return job


def options(**changes):
    return {
        "max_microdollars": 1_750_000,
        "max_updates": 2,
        "acknowledge_retries": True,
        "price_checked_on": supervisor.PRICE_CHECKED_ON,
        "duration_seconds": 60,
        **changes,
    }


def test_validate_accepts_source_bound_reviewed_complete_schedule():
    job = example_job()
    supervisor.validate_admission(job, copy.deepcopy(job), **options())


@pytest.mark.parametrize(
    "change",
    [
        {"acknowledge_retries": False},
        {"acknowledge_retries": 1},
        {"price_checked_on": "1900-01-01"},
        {"max_microdollars": 0},
        {"max_microdollars": True},
        {"max_microdollars": 1_750_001},
        {"max_microdollars": 299},
        {"max_microdollars": 300.0},
        {"max_updates": 0},
        {"max_updates": True},
        {"max_updates": 31},
        {"max_updates": 1},
        {"duration_seconds": 59},
        {"duration_seconds": 1801},
        {"duration_seconds": True},
    ],
)
def test_validate_rejects_missing_ack_stale_rate_or_limits(change):
    job = example_job()
    with pytest.raises(supervisor.TrainingAdmissionError):
        supervisor.validate_admission(job, job, **options(**change))


def test_validate_rejects_draft_even_with_valid_fingerprint():
    job = example_job(reviewed=False)
    with pytest.raises(supervisor.TrainingAdmissionError, match="Draft"):
        supervisor.validate_admission(job, copy.deepcopy(job), **options())


def test_validate_rejects_tamper_before_source_comparison():
    preview = example_job()
    job = copy.deepcopy(preview)
    job["schedule"][0]["reserved_microdollars"] = 1
    with pytest.raises(supervisor.TrainingAdmissionError, match="fingerprint"):
        supervisor.validate_admission(job, preview, **options())


@pytest.mark.parametrize("field", ["source_hashes", "config", "totals", "schedule"])
def test_validate_rejects_rehashed_job_differing_from_current_preview(field):
    preview = example_job()
    job = copy.deepcopy(preview)
    job[field] = {"edited": True}
    job["fingerprint"] = fingerprint(job)
    with pytest.raises(supervisor.TrainingAdmissionError, match="changed"):
        supervisor.validate_admission(job, preview, **options())


@pytest.mark.parametrize("installed", [None, "0.31.0", "0.32.1"])
def test_sdk_gate_requires_exact_optional_dependency_version(monkeypatch, installed):
    def fake_version(name):
        assert name == "tinker"
        if installed is None:
            raise PackageNotFoundError(name)
        return installed

    monkeypatch.setattr(supervisor, "version", fake_version)
    with pytest.raises(supervisor.TrainingAdmissionError):
        supervisor.require_sdk_version()


def test_sdk_gate_accepts_pinned_version_without_importing_sdk(monkeypatch):
    monkeypatch.setattr(supervisor, "version", lambda name: supervisor.SDK_VERSION)
    supervisor.require_sdk_version()


@pytest.fixture
def launch_fixture(tmp_path, monkeypatch):
    monkeypatch.setenv("TINKER_API_KEY", "offline-dummy-not-a-real-key")
    monkeypatch.setattr(supervisor, "require_sdk_version", lambda: None)
    counter = iter(range(100))
    monkeypatch.setattr(
        supervisor.uuid, "uuid4", lambda: SimpleNamespace(hex=f"offline-run-{next(counter)}")
    )
    calls = []

    def install(*, result=None, timeout=False, progress=None, returncode=0, start_error=False):
        class FakeProcess:
            def __init__(self, args, **kwargs):
                calls.append((args, kwargs, self))
                self.args = args
                self.killed = False
                self.returncode = None
                self.waits = []
                job_path, result_path = Path(args[-2]), Path(args[-1])
                saved = json.loads(job_path.read_text(encoding="utf-8"))
                ledger = TrainingLedger(tmp_path / ".data")
                status = ledger.status(job_path.parent.name)
                # Assert admission order at the exact subprocess boundary.
                assert status["status"] == "running"
                assert status["reserved_updates"] == len(saved["schedule"])
                assert status["reserved_microdollars"] == saved["totals"]["reserved_microdollars"]
                if start_error:
                    raise OSError("synthetic process start failure")
                if progress is not None:
                    text = progress(saved) if callable(progress) else progress
                    Path(str(result_path) + ".progress").write_text(text, encoding="utf-8")
                if result is not None:
                    value = result(saved) if callable(result) else result
                    result_path.write_text(json.dumps(value), encoding="utf-8")

            def wait(self, *, timeout):
                self.waits.append(timeout)
                if timeout and timeout != 5 and len(self.waits) == 1:
                    if timeout_mode:
                        raise subprocess.TimeoutExpired(self.args, timeout)
                if not self.killed:
                    self.returncode = returncode
                return self.returncode

            def poll(self):
                return self.returncode

            def kill(self):
                self.killed = True
                self.returncode = -9

        timeout_mode = timeout
        monkeypatch.setattr(supervisor.subprocess, "Popen", FakeProcess)

    install()
    return tmp_path, calls, install


def completed_result(job):
    return {
        "status": "completed",
        "fingerprint": job["fingerprint"],
        "confirmed_updates": 2,
        "state_path": "tinker://offline/weights/final",
        "sampler_path": "tinker://offline/sampler_weights/final",
    }


def progress_records(job, *, updates=2, checkpoints=True):
    records = [
        {
            "fingerprint": job["fingerprint"],
            "phase": "confirmed_update",
            "confirmed_updates": index,
        }
        for index in range(1, updates + 1)
    ]
    if checkpoints:
        records.extend(
            {
                "fingerprint": job["fingerprint"],
                "phase": phase,
                "confirmed_updates": updates,
                ("state_path" if phase == "confirmed_state" else "sampler_path"): path,
            }
            for phase, path in (
                ("confirmed_state", "tinker://offline/weights/final"),
                ("confirmed_sampler", "tinker://offline/sampler_weights/final"),
            )
        )
    return records


def progress_text(records):
    return "".join(json.dumps(record) + "\n" for record in records)


def completed_progress(job):
    return progress_text(progress_records(job))


def partial_progress(job):
    return progress_text(progress_records(job, updates=1, checkpoints=False))


def test_launch_reserves_full_schedule_before_process_and_confirms_checkpoints(launch_fixture):
    root, calls, install = launch_fixture
    install(result=completed_result, progress=completed_progress)
    job = example_job()
    result = supervisor.launch(root, job, copy.deepcopy(job), **options())
    assert len(calls) == 1
    assert result["status"] == "completed"
    assert result["confirmed_updates"] == 2
    assert result["ledger"]["reserved_microdollars"] == 300
    assert result["ledger"]["reserved_updates"] == 2
    assert result["state_path"].startswith("tinker://")
    assert result["sampler_path"].startswith("tinker://")
    assert not (root / ".data" / "guardmate.sqlite3").exists()


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "running"},
        {"fingerprint": "wrong"},
        {"confirmed_updates": 1},
        {"confirmed_updates": True},
        {"confirmed_updates": 2.0},
        {"state_path": "https://example.test/weights/final"},
        {"state_path": "tinker://offline/sampler_weights/final"},
        {"sampler_path": "tinker://offline/weights/final"},
        {"sampler_path": "file:///sampler_weights/final"},
        {"sampler_path": None},
    ],
)
def test_launch_cannot_confirm_bad_worker_evidence(launch_fixture, changes):
    root, calls, install = launch_fixture
    install(result=lambda job: {**completed_result(job), **changes}, progress=completed_progress)
    job = example_job()
    result = supervisor.launch(root, job, job, **options())
    assert len(calls) == 1
    assert result["status"] == "unknown"
    assert result["ledger"]["reserved_microdollars"] == 300
    assert "state_path" not in result
    assert "sampler_path" not in result


@pytest.mark.parametrize("progress", [None, "", partial_progress, "not-json\n"])
def test_launch_requires_complete_progress_even_if_result_claims_completion(
    launch_fixture, progress
):
    root, _, install = launch_fixture
    install(result=completed_result, progress=progress)
    job = example_job()
    result = supervisor.launch(root, job, job, **options())
    assert result["status"] == "unknown"
    assert result["ledger"]["reserved_microdollars"] == 300


def test_timeout_kills_local_process_keeps_unknown_and_all_reservations(launch_fixture):
    root, calls, install = launch_fixture
    install(timeout=True, progress=partial_progress)
    job = example_job()
    result = supervisor.launch(root, job, job, **options())
    process = calls[0][2]
    assert process.killed
    assert process.waits == [60, 5]
    assert result["status"] == "unknown"
    assert result["confirmed_updates"] == 1
    assert result["ledger"]["reserved_microdollars"] == 300
    ledger = TrainingLedger(root / ".data")
    with pytest.raises(TrainingBudgetError):
        ledger.set_status(result["run_id"], "running")
    with pytest.raises(TrainingBudgetError):
        ledger.reserve(result["run_id"], "retry", 100)


@pytest.mark.parametrize("failure", ["start", "exit"])
def test_process_failure_never_refunds_admitted_schedule(launch_fixture, failure):
    root, _, install = launch_fixture
    install(start_error=failure == "start", returncode=1)
    job = example_job()
    result = supervisor.launch(root, job, job, **options())
    assert result["status"] == "unknown"
    assert result["ledger"]["reserved_microdollars"] == 300


def test_failed_aggregate_admission_preserves_partial_reservations_without_process(launch_fixture):
    root, calls, _ = launch_fixture
    ledger = TrainingLedger(root / ".data")
    ledger.create_run("earlier", "other-fingerprint", 1_750_000, 1)
    ledger.set_status("earlier", "running")
    ledger.reserve("earlier", "first", 1_749_850)
    ledger.set_status("earlier", "unknown")
    job = example_job()
    with pytest.raises(supervisor.TrainingAdmissionError, match="no SDK"):
        supervisor.launch(root, job, job, **options())
    assert not calls
    rejected = ledger.status("offline-run-0")
    assert rejected["status"] == "failed"
    assert rejected["reserved_microdollars"] == 100
    assert rejected["aggregate_reserved_microdollars"] == 1_749_950


def test_same_fingerprint_cannot_be_replayed_after_unknown_worker(launch_fixture):
    root, calls, _ = launch_fixture
    job = example_job()
    first = supervisor.launch(root, job, job, **options())
    assert first["status"] == "unknown"
    with pytest.raises(supervisor.TrainingAdmissionError):
        supervisor.launch(root, job, job, **options())
    assert len(calls) == 1
    assert TrainingLedger(root / ".data").status(first["run_id"])["reserved_microdollars"] == 300


def test_invalid_admission_never_touches_sdk_gate_or_ledger(tmp_path, monkeypatch):
    def forbidden():
        raise AssertionError("SDK gate must not run for inadmissible draft")

    monkeypatch.setattr(supervisor, "require_sdk_version", forbidden)
    job = example_job(reviewed=False)
    with pytest.raises(supervisor.TrainingAdmissionError):
        supervisor.launch(tmp_path, job, job, **options())
    assert not (tmp_path / ".data").exists()


def test_missing_key_never_creates_ledger_or_process(launch_fixture, monkeypatch):
    root, calls, _ = launch_fixture
    monkeypatch.delenv("TINKER_API_KEY", raising=False)
    job = example_job()
    with pytest.raises(supervisor.TrainingAdmissionError, match="not configured"):
        supervisor.launch(root, job, job, **options())
    assert not calls
    assert not (root / ".data").exists()


def test_worker_environment_filters_unrelated_secrets_and_only_injects_explicit_key(
    tmp_path, monkeypatch
):
    for name in ("AWS_SECRET_ACCESS_KEY", "OPENAI_API_KEY", "OTHER_PRIVATE_VALUE", "PYTHONPATH"):
        monkeypatch.setenv(name, "should-not-leak")
    monkeypatch.setenv("TINKER_API_KEY", "different-parent-key")
    monkeypatch.setenv("PATH", "offline-path")
    environment = supervisor.worker_environment(tmp_path, "offline-worker-key")
    assert environment["TINKER_API_KEY"] == "offline-worker-key"
    assert environment["PYTHONPATH"] == str(tmp_path / "backend")
    assert environment["PATH"] == "offline-path"
    assert environment["PYTHONUNBUFFERED"] == "1"
    assert not any(
        name in environment
        for name in ("AWS_SECRET_ACCESS_KEY", "OPENAI_API_KEY", "OTHER_PRIVATE_VALUE")
    )


def test_progress_partial_write_is_not_a_confirmed_update(tmp_path):
    path = tmp_path / "result.progress"
    job = example_job()
    path.write_text(partial_progress(job) + '{"confirmed_updates":', encoding="utf-8")
    assert supervisor._read_progress(path, 2, job["fingerprint"]) == (1, None, None)


@pytest.mark.parametrize("value", [True, 3, 1.0, -1])
def test_progress_wrong_type_or_over_limit_cannot_confirm_updates(tmp_path, value):
    path = tmp_path / "result.progress"
    job = example_job()
    record = progress_records(job, updates=1, checkpoints=False)[0]
    record["confirmed_updates"] = value
    path.write_text(progress_text([record]), encoding="utf-8")
    assert supervisor._read_progress(path, 2, job["fingerprint"]) == (0, None, None)


@pytest.mark.parametrize(
    "index,changes",
    [
        (0, {"phase": "started_update"}),
        (0, {"fingerprint": "wrong-job"}),
        (1, {"confirmed_updates": 1}),
        (2, {"fingerprint": "wrong-job"}),
        (2, {"phase": "started_state"}),
        (2, {"confirmed_updates": 1}),
        (2, {"state_path": "tinker://offline/weights/different"}),
        (3, {"phase": "started_sampler"}),
        (3, {"sampler_path": "tinker://offline/sampler_weights/different"}),
    ],
)
def test_launch_requires_matching_phase_fingerprint_counts_and_checkpoint_progress(
    launch_fixture, index, changes
):
    root, _, install = launch_fixture

    def changed_progress(job):
        records = progress_records(job)
        records[index].update(changes)
        return progress_text(records)

    install(result=completed_result, progress=changed_progress)
    job = example_job()
    result = supervisor.launch(root, job, job, **options())
    assert result["status"] == "unknown"
    assert result["ledger"]["reserved_microdollars"] == 300
    assert "state_path" not in result
    assert "sampler_path" not in result


def test_final_result_alone_cannot_prove_checkpoint_saves(launch_fixture):
    root, _, install = launch_fixture
    install(
        result=completed_result,
        progress=lambda job: progress_text(progress_records(job, checkpoints=False)),
    )
    job = example_job()
    result = supervisor.launch(root, job, job, **options())
    assert result["confirmed_updates"] == 2
    assert result["status"] == "unknown"


def test_progress_wrong_job_fingerprint_cannot_prove_updates(tmp_path):
    path = tmp_path / "result.progress"
    path.write_text(completed_progress(example_job()), encoding="utf-8")
    assert supervisor._read_progress(path, 2, "another-job") == (0, None, None)


def test_progress_non_object_record_cannot_prove_any_updates(tmp_path):
    path = tmp_path / "result.progress"
    path.write_text("[]\n", encoding="utf-8")
    assert supervisor._read_progress(path, 2, example_job()["fingerprint"]) == (0, None, None)
