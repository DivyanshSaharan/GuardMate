import copy
import hashlib
import importlib
import json
import re
import sys
from types import SimpleNamespace

import pytest
from guardmate.training import preparation, worker
from guardmate.training.ledger import TrainingLedger
from guardmate.training.preparation import fingerprint

FINGERPRINT = "a" * 64
SENSITIVE = "SENSITIVE_PROVIDER_EXCEPTION_MUST_NOT_ESCAPE"


class FakeTensor:
    def __init__(self, *, data, dtype, shape):
        self.data = data
        self.dtype = dtype
        self.shape = shape


class FakeInput:
    @classmethod
    def from_ints(cls, tokens):
        return SimpleNamespace(tokens=tokens)


class FakeDatum:
    def __init__(self, *, model_input, loss_fn_inputs):
        self.model_input = model_input
        self.loss_fn_inputs = loss_fn_inputs


class FakeAdam:
    def __init__(self, *, learning_rate):
        self.learning_rate = learning_rate


FAKE_TYPES = SimpleNamespace(
    Datum=FakeDatum, ModelInput=FakeInput, TensorData=FakeTensor, AdamParams=FakeAdam
)


class FakeFuture:
    def __init__(self, calls, phase, *, fail=False, path=None):
        self.calls = calls
        self.phase = phase
        self.fail = fail
        self.path = path

    def result(self, *, timeout=None):
        self.calls.append(("result", self.phase, timeout))
        if self.fail:
            raise TimeoutError(SENSITIVE)
        return SimpleNamespace(path=self.path)


class FakeTrainer:
    def __init__(self, *, fail=None, fail_update=1):
        self.calls = []
        self.fail = fail
        self.fail_update = fail_update
        self.forward_count = 0
        self.optim_count = 0

    def forward_backward(self, data, *, loss_fn):
        self.forward_count += 1
        self.calls.append(("forward_backward", data, loss_fn))
        if self.fail == "submit_forward" and self.forward_count == self.fail_update:
            raise RuntimeError(SENSITIVE)
        return FakeFuture(
            self.calls,
            "forward_backward",
            fail=self.fail == "forward_backward" and self.forward_count == self.fail_update,
        )

    def optim_step(self, params):
        self.optim_count += 1
        self.calls.append(("optim_step", params))
        if self.fail == "submit_optim" and self.optim_count == self.fail_update:
            raise RuntimeError(SENSITIVE)
        return FakeFuture(
            self.calls,
            "optim_step",
            fail=self.fail == "optim_step" and self.optim_count == self.fail_update,
        )

    def save_state(self, **kwargs):
        self.calls.append(("save_state", kwargs))
        if self.fail == "submit_state":
            raise RuntimeError(SENSITIVE)
        return FakeFuture(
            self.calls,
            "save_state",
            fail=self.fail == "save_state",
            path="not-confirmed" if self.fail == "state_path" else "tinker://test/state",
        )

    def save_weights_for_sampler(self, **kwargs):
        self.calls.append(("save_weights_for_sampler", kwargs))
        if self.fail == "submit_sampler":
            raise RuntimeError(SENSITIVE)
        return FakeFuture(
            self.calls,
            "save_sampler",
            fail=self.fail == "save_sampler",
            path="not-confirmed" if self.fail == "sampler_path" else "tinker://test/sampler",
        )


class FakeService:
    def __init__(self, trainer, *, fail=False):
        self.trainer = trainer
        self.fail = fail
        self.calls = []

    def create_lora_training_client(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError(SENSITIVE)
        return self.trainer


@pytest.fixture
def job():
    return {
        "model": "Qwen/Qwen3.5-4B",
        "config": {"rank": 16, "learning_rate": 0.0001, "epochs": 2, "batch_size": 2, "seed": 42},
        "fingerprint": FINGERPRINT,
        "datums": [
            {
                "input_tokens": [100, 101, 102],
                "target_tokens": [101, 102, 103],
                "weights": [0.0, 1.0, 1.0],
                "scenario_id": "fictional-scenario",
                "step_index": 0,
            },
            {
                "input_tokens": [200, 201],
                "target_tokens": [201, 202],
                "weights": [0, 1],
                "scenario_id": "fictional-scenario",
                "step_index": 1,
            },
        ],
        "schedule": [
            {
                "epoch": 0,
                "batch_index": 0,
                "datum_indices": [1, 0],
                "processed_tokens": 5,
                "reserved_microdollars": 100,
            },
            {
                "epoch": 1,
                "batch_index": 0,
                "datum_indices": [0],
                "processed_tokens": 3,
                "reserved_microdollars": 60,
            },
        ],
    }


def run_fake(job, tmp_path, *, fail=None, fail_update=1, service_fail=False):
    trainer = FakeTrainer(fail=fail, fail_update=fail_update)
    service = FakeService(trainer, fail=service_fail)
    path = tmp_path / "result.json"
    result = worker.run_job(job, path, service_factory=lambda: (service, FAKE_TYPES))
    return result, trainer, service, path


def records(path):
    return [
        json.loads(line)
        for line in path.with_name(path.name + ".progress").read_text().splitlines()
    ]


def test_native_sequential_update_and_checkpoint_contract(job, tmp_path):
    result, trainer, service, path = run_fake(job, tmp_path)
    assert result == {
        "status": "completed",
        "confirmed_updates": 2,
        "state_path": "tinker://test/state",
        "sampler_path": "tinker://test/sampler",
        "fingerprint": FINGERPRINT,
    }
    assert json.loads(path.read_text()) == result
    assert service.calls == [
        {
            "base_model": "Qwen/Qwen3.5-4B",
            "rank": 16,
            "seed": 42,
            "train_mlp": True,
            "train_attn": True,
            "train_unembed": True,
            "user_metadata": {"fingerprint": FINGERPRINT},
        }
    ]
    assert [call[0] for call in trainer.calls] == [
        "forward_backward",
        "result",
        "optim_step",
        "result",
        "forward_backward",
        "result",
        "optim_step",
        "result",
        "save_state",
        "result",
        "save_weights_for_sampler",
        "result",
    ]
    assert all(call[2] is None for call in trainer.calls if call[0] == "result")
    assert all(
        call[2] == "cross_entropy" for call in trainer.calls if call[0] == "forward_backward"
    )
    assert all(call[1].learning_rate == 0.0001 for call in trainer.calls if call[0] == "optim_step")
    state = next(call[1] for call in trainer.calls if call[0] == "save_state")
    sampler = next(call[1] for call in trainer.calls if call[0] == "save_weights_for_sampler")
    assert re.fullmatch(r"guardmate-a{12}-[0-9a-f]{32}-state", state["name"])
    assert sampler["name"] == state["name"].removesuffix("state") + "sampler"
    assert state["overwrite"] is False
    assert "overwrite" not in sampler
    for options in (state, sampler):
        assert options["ttl_seconds"] == 86_400
        assert options["user_metadata"] == {"confirmed_step": "2", "fingerprint": FINGERPRINT}


def test_token_targets_masks_and_scheduled_order_are_preserved(job, tmp_path):
    _, trainer, _, _ = run_fake(job, tmp_path)
    batches = [call[1] for call in trainer.calls if call[0] == "forward_backward"]
    assert [datum.model_input.tokens for datum in batches[0]] == [[200, 201], [100, 101, 102]]
    assert [datum.model_input.tokens for datum in batches[1]] == [[100, 101, 102]]
    datum = batches[0][1]
    assert datum.loss_fn_inputs["target_tokens"].data == [101, 102, 103]
    assert datum.loss_fn_inputs["target_tokens"].dtype == "int64"
    assert datum.loss_fn_inputs["target_tokens"].shape == [3]
    assert datum.loss_fn_inputs["weights"].data == [0.0, 1.0, 1.0]
    assert datum.loss_fn_inputs["weights"].dtype == "float32"
    assert datum.loss_fn_inputs["weights"].shape == [3]


def test_installed_native_sdk_types_accept_prepared_arrays_without_a_client(job, monkeypatch):
    sdk = pytest.importorskip("tinker")

    def deny(*args, **kwargs):
        raise AssertionError("A client must not be initialized for type construction")

    monkeypatch.setattr(sdk, "ServiceClient", deny)
    datums = worker._datums(job, sdk.types)
    assert datums[0].model_input.to_ints() == [100, 101, 102]
    assert datums[0].model_input.length == 3
    assert datums[0].loss_fn_inputs["target_tokens"].data == [101, 102, 103]
    assert datums[0].loss_fn_inputs["target_tokens"].shape == [3]
    assert datums[0].loss_fn_inputs["target_tokens"].dtype == "int64"
    assert datums[0].loss_fn_inputs["weights"].data == [0.0, 1.0, 1.0]
    assert datums[0].loss_fn_inputs["weights"].shape == [3]
    assert datums[0].loss_fn_inputs["weights"].dtype == "float32"


def test_progress_is_safe_monotonic_and_preserves_confirmed_checkpoint_paths(job, tmp_path):
    _, _, _, path = run_fake(job, tmp_path)
    progress = records(path)
    assert [record["confirmed_updates"] for record in progress] == [1, 2, 2, 2]
    assert progress[:2] == [
        {
            "phase": "confirmed_update",
            "confirmed_updates": 1,
            "epoch": 0,
            "batch_index": 0,
            "fingerprint": FINGERPRINT,
        },
        {
            "phase": "confirmed_update",
            "confirmed_updates": 2,
            "epoch": 1,
            "batch_index": 0,
            "fingerprint": FINGERPRINT,
        },
    ]
    text = path.with_name(path.name + ".progress").read_text()
    assert "scenario_id" not in text
    assert "target_tokens" not in text
    assert "weights" not in text
    assert progress[2]["state_path"] == "tinker://test/state"
    assert progress[3]["sampler_path"] == "tinker://test/sampler"


@pytest.mark.parametrize(
    ("failure", "phase", "confirmed", "forward_count", "optim_count"),
    [
        ("submit_forward", "forward_backward", 0, 1, 0),
        ("forward_backward", "forward_backward", 0, 1, 0),
        ("submit_optim", "optim_step", 0, 1, 1),
        ("optim_step", "optim_step", 0, 1, 1),
        ("submit_state", "save_state", 2, 2, 2),
        ("save_state", "save_state", 2, 2, 2),
        ("state_path", "save_state", 2, 2, 2),
        ("submit_sampler", "save_sampler", 2, 2, 2),
        ("save_sampler", "save_sampler", 2, 2, 2),
        ("sampler_path", "save_sampler", 2, 2, 2),
    ],
)
def test_failures_stop_without_retry_or_unconfirmed_checkpoint_claims(
    job, tmp_path, failure, phase, confirmed, forward_count, optim_count
):
    result, trainer, _, path = run_fake(job, tmp_path, fail=failure)
    assert result["status"] == "unknown"
    assert result["phase"] == phase
    assert result["confirmed_updates"] == confirmed
    assert set(result) == {"status", "phase", "confirmed_updates", "error_type", "fingerprint"}
    assert trainer.forward_count == forward_count
    assert trainer.optim_count == optim_count
    assert SENSITIVE not in path.read_text()
    assert SENSITIVE not in path.with_name(path.name + ".progress").read_text()
    if phase in ("forward_backward", "optim_step"):
        assert not any(call[0].startswith("save") for call in trainer.calls)
        assert records(path) == []
    if phase == "save_state":
        assert not any(call[0] == "save_weights_for_sampler" for call in trainer.calls)
    if phase == "save_sampler":
        assert records(path)[-1]["state_path"] == "tinker://test/state"


def test_later_optimizer_timeout_preserves_only_earlier_confirmed_update(job, tmp_path):
    result, trainer, _, path = run_fake(job, tmp_path, fail="optim_step", fail_update=2)
    assert result["status"] == "unknown"
    assert result["confirmed_updates"] == 1
    assert trainer.optim_count == 2
    assert len(records(path)) == 1
    assert records(path)[0]["confirmed_updates"] == 1
    assert not any(call[0].startswith("save") for call in trainer.calls)


def test_service_or_model_initialization_failure_is_safe_and_stops(job, tmp_path):
    result, trainer, service, path = run_fake(job, tmp_path, service_fail=True)
    assert result["status"] == "unknown"
    assert result["phase"] == "create_training_client"
    assert result["confirmed_updates"] == 0
    assert len(service.calls) == 1
    assert trainer.calls == []
    assert SENSITIVE not in path.read_text()


def test_factory_failure_never_constructs_a_training_client(job, tmp_path):
    def factory():
        raise RuntimeError(SENSITIVE)

    path = tmp_path / "result.json"
    result = worker.run_job(job, path, service_factory=factory)
    assert result["status"] == "unknown"
    assert result["phase"] == "create_service"
    assert result["error_type"] == "RuntimeError"
    assert SENSITIVE not in path.read_text()


@pytest.mark.parametrize("which", ["result", "progress"])
def test_existing_outputs_prevent_provider_initialization_and_are_preserved(job, tmp_path, which):
    path = tmp_path / "result.json"
    existing = path if which == "result" else path.with_name(path.name + ".progress")
    existing.write_text("existing artifact")
    calls = []
    with pytest.raises(FileExistsError):
        worker.run_job(job, path, service_factory=lambda: calls.append(True))
    assert calls == []
    assert existing.read_text() == "existing artifact"


def test_checkpoint_names_are_unique_for_separate_jobs(job, tmp_path):
    first_dir = tmp_path / "one"
    second_dir = tmp_path / "two"
    first_dir.mkdir()
    second_dir.mkdir()
    _, first, _, _ = run_fake(job, first_dir)
    _, second, _, _ = run_fake(job, second_dir)
    first_name = next(call[1]["name"] for call in first.calls if call[0] == "save_state")
    second_name = next(call[1]["name"] for call in second.calls if call[0] == "save_state")
    assert first_name != second_name


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("rank", 32),
        ("rank", True),
        ("seed", 41),
        ("learning_rate", 0.01),
        ("epochs", 0),
        ("epochs", True),
        ("batch_size", 0),
    ],
)
def test_invalid_configuration_is_rejected_before_any_provider_initialization(
    job, tmp_path, field, value
):
    job["config"][field] = value
    calls = []
    result = worker.run_job(
        job, tmp_path / "result.json", service_factory=lambda: calls.append(True)
    )
    assert result["status"] == "unknown"
    assert result["phase"] == "validate_job"
    assert calls == []


@pytest.mark.parametrize("fingerprint", ["", "../unsafe", "z" * 64, None])
def test_invalid_fingerprint_is_not_exposed_in_output_or_checkpoint_names(
    job, tmp_path, fingerprint
):
    job["fingerprint"] = fingerprint
    calls = []
    result = worker.run_job(
        job, tmp_path / "result.json", service_factory=lambda: calls.append(True)
    )
    assert result["status"] == "unknown"
    assert result["fingerprint"] == ""
    assert calls == []


@pytest.mark.parametrize("indices", [[], [True], [-1], [2], [0, 1, 0], None])
def test_invalid_schedule_never_initializes_provider(job, tmp_path, indices):
    job["schedule"][0]["datum_indices"] = indices
    calls = []
    result = worker.run_job(
        job, tmp_path / "result.json", service_factory=lambda: calls.append(True)
    )
    assert result["status"] == "unknown"
    assert result["phase"] == "validate_job"
    assert calls == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("input_tokens", []),
        ("input_tokens", [True, 1, 2]),
        ("target_tokens", [1]),
        ("weights", [0, 0, 0]),
        ("weights", [0, float("nan"), 1]),
        ("weights", [0, -1, 1]),
        ("weights", [0, True, 1]),
    ],
)
def test_invalid_token_arrays_never_initialize_provider(job, tmp_path, field, value):
    job["datums"][0][field] = value
    calls = []
    result = worker.run_job(
        job, tmp_path / "result.json", service_factory=lambda: calls.append(True)
    )
    assert result["status"] == "unknown"
    assert result["phase"] == "validate_job"
    assert calls == []


def test_worker_does_not_mutate_prepared_artifact(job, tmp_path):
    original = copy.deepcopy(job)
    run_fake(job, tmp_path)
    assert job == original


def test_default_factory_has_no_unsupported_service_constructor_kwargs(job, tmp_path, monkeypatch):
    trainer = FakeTrainer()
    service = FakeService(trainer)
    constructor_calls = []

    def constructor(*args, **kwargs):
        constructor_calls.append((args, kwargs))
        return service

    monkeypatch.setitem(
        sys.modules, "tinker", SimpleNamespace(ServiceClient=constructor, types=FAKE_TYPES)
    )
    result = worker.run_job(job, tmp_path / "result.json")
    assert result["status"] == "completed"
    assert constructor_calls == [((), {})]


def test_module_import_does_not_import_sdk_or_initialize_a_provider(monkeypatch):
    import builtins

    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name == "tinker" or name.startswith("tinker."):
            raise AssertionError("SDK import before worker execution")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    importlib.reload(worker)


def test_progress_io_failure_stops_without_checkpoint_or_additional_update(
    job, tmp_path, monkeypatch
):
    def fail(stream, record):
        raise OSError(SENSITIVE)

    monkeypatch.setattr(worker, "_append_progress", fail)
    result, trainer, _, path = run_fake(job, tmp_path)
    assert result["status"] == "unknown"
    assert result["phase"] == "record_progress"
    assert result["confirmed_updates"] == 1
    assert trainer.forward_count == 1
    assert trainer.optim_count == 1
    assert not any(call[0].startswith("save") for call in trainer.calls)
    assert SENSITIVE not in path.read_text()


def test_cli_reports_only_safe_metadata_for_unreadable_job(tmp_path, capsys):
    source = tmp_path / "missing-sensitive-file.json"
    assert worker.main([str(source), str(tmp_path / "result.json")]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "status": "unknown",
        "phase": "load_job",
        "error_type": "FileNotFoundError",
    }
    assert str(source) not in captured.err


def admitted_paths(
    job,
    tmp_path,
    monkeypatch,
    *,
    run_id="1" * 32,
    terminal=None,
    reserve_count=None,
    reservation_delta=0,
    ledger_fingerprint=None,
):
    monkeypatch.setattr(worker, "ROOT", tmp_path)
    job = copy.deepcopy(job)
    job["review_ready"] = True
    job.setdefault("source_hashes", bound_source_hashes())
    job["fingerprint"] = fingerprint(job)
    run_dir = tmp_path / ".data" / "training" / "runs" / run_id
    run_dir.mkdir(parents=True)
    source = run_dir / "job.json"
    source.write_text(json.dumps(job))
    ledger = TrainingLedger(tmp_path / ".data")
    ledger.create_run(run_id, ledger_fingerprint or job["fingerprint"], 1_000, len(job["schedule"]))
    ledger.set_status(run_id, "running")
    steps = job["schedule"] if reserve_count is None else job["schedule"][:reserve_count]
    for index, step in enumerate(steps):
        ledger.reserve(
            run_id, f"update-{index + 1}", step["reserved_microdollars"] + reservation_delta
        )
    if terminal:
        ledger.set_status(run_id, terminal)
    return job, source, run_dir / "worker-result.json", ledger


def test_cli_success_and_unknown_exit_codes(job, tmp_path, monkeypatch):
    _, source, result_path, _ = admitted_paths(job, tmp_path / "success", monkeypatch)
    monkeypatch.setattr(
        worker, "_default_service_factory", lambda: (FakeService(FakeTrainer()), FAKE_TYPES)
    )
    assert worker.main([str(source), str(result_path)]) == 0
    _, source, result_path, _ = admitted_paths(job, tmp_path / "unknown", monkeypatch)
    monkeypatch.setattr(
        worker,
        "_default_service_factory",
        lambda: (FakeService(FakeTrainer(fail="optim_step")), FAKE_TYPES),
    )
    assert worker.main([str(source), str(result_path)]) == 2


def denied_factory(monkeypatch):
    calls = []

    def deny():
        calls.append(True)
        raise AssertionError("Provider initialization must not occur")

    monkeypatch.setattr(worker, "_default_service_factory", deny)
    return calls


def bound_source_hashes():
    return {
        name: hashlib.sha256(path.read_bytes()).hexdigest()
        for name, path in preparation.SOURCE_PATHS.items()
    }


def test_cli_rejects_changed_bound_sources_before_sdk_initialization(job, tmp_path, monkeypatch):
    source_path = tmp_path / "bound-policy.py"
    source_path.write_text("bound policy")
    monkeypatch.setattr(preparation, "SOURCE_PATHS", {"agent/policy.py": source_path})
    _, source, result_path, ledger = admitted_paths(job, tmp_path, monkeypatch)
    before = ledger.status("1" * 32)
    source_path.write_text("changed policy")
    calls = denied_factory(monkeypatch)
    assert worker.main([str(source), str(result_path)]) == 2
    assert calls == []
    assert ledger.status("1" * 32) == before
    assert not result_path.exists()


@pytest.mark.parametrize("missing_source", list(preparation.SOURCE_PATHS))
def test_cli_requires_every_bound_source_before_sdk_initialization(
    job, tmp_path, monkeypatch, missing_source
):
    job["source_hashes"] = bound_source_hashes()
    job["source_hashes"].pop(missing_source)
    _, source, result_path, _ = admitted_paths(job, tmp_path, monkeypatch)
    calls = denied_factory(monkeypatch)
    assert worker.main([str(source), str(result_path)]) == 2
    assert calls == []
    assert not result_path.exists()


@pytest.mark.parametrize("source_hashes", [None, [], "unbound"])
def test_cli_requires_a_source_binding_object(job, tmp_path, monkeypatch, source_hashes):
    job["source_hashes"] = source_hashes
    _, source, result_path, _ = admitted_paths(job, tmp_path, monkeypatch)
    calls = denied_factory(monkeypatch)
    assert worker.main([str(source), str(result_path)]) == 2
    assert calls == []
    assert not result_path.exists()


@pytest.mark.parametrize("terminal", ["completed", "failed", "unknown"])
def test_cli_rejects_terminal_runs_before_sdk_initialization(job, tmp_path, monkeypatch, terminal):
    _, source, result_path, _ = admitted_paths(job, tmp_path, monkeypatch, terminal=terminal)
    calls = denied_factory(monkeypatch)
    assert worker.main([str(source), str(result_path)]) == 2
    assert calls == []
    assert not result_path.exists()


@pytest.mark.parametrize("reserved", [0, 1])
def test_cli_rejects_partial_update_admission(job, tmp_path, monkeypatch, reserved):
    _, source, result_path, ledger = admitted_paths(
        job, tmp_path, monkeypatch, reserve_count=reserved
    )
    before = ledger.status("1" * 32)
    calls = denied_factory(monkeypatch)
    assert worker.main([str(source), str(result_path)]) == 2
    assert calls == []
    assert ledger.status("1" * 32) == before
    assert not result_path.exists()


@pytest.mark.parametrize("delta", [-1, 1])
def test_cli_requires_exact_full_monetary_reservations(job, tmp_path, monkeypatch, delta):
    _, source, result_path, _ = admitted_paths(job, tmp_path, monkeypatch, reservation_delta=delta)
    calls = denied_factory(monkeypatch)
    assert worker.main([str(source), str(result_path)]) == 2
    assert calls == []
    assert not result_path.exists()


def test_cli_requires_job_and_ledger_fingerprints_to_match(job, tmp_path, monkeypatch):
    _, source, result_path, _ = admitted_paths(
        job, tmp_path, monkeypatch, ledger_fingerprint="b" * 64
    )
    calls = denied_factory(monkeypatch)
    assert worker.main([str(source), str(result_path)]) == 2
    assert calls == []


def test_cli_rejects_fingerprint_tampering_before_sdk_initialization(job, tmp_path, monkeypatch):
    admitted, source, result_path, _ = admitted_paths(job, tmp_path, monkeypatch)
    admitted["datums"][0]["input_tokens"][0] = 999
    source.write_text(json.dumps(admitted))
    calls = denied_factory(monkeypatch)
    assert worker.main([str(source), str(result_path)]) == 2
    assert calls == []
    assert not result_path.exists()


@pytest.mark.parametrize("review", [False, None, 1, "true"])
def test_cli_never_accepts_draft_or_nonboolean_review_metadata(job, tmp_path, monkeypatch, review):
    admitted, source, result_path, _ = admitted_paths(job, tmp_path, monkeypatch)
    admitted["review_ready"] = review
    admitted["fingerprint"] = fingerprint(admitted)
    source.write_text(json.dumps(admitted))
    calls = denied_factory(monkeypatch)
    assert worker.main([str(source), str(result_path)]) == 2
    assert calls == []


def test_cli_requires_existing_ledger_without_creating_it(job, tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "ROOT", tmp_path)
    job["review_ready"] = True
    job["source_hashes"] = bound_source_hashes()
    job["fingerprint"] = fingerprint(job)
    run_dir = tmp_path / ".data" / "training" / "runs" / ("1" * 32)
    run_dir.mkdir(parents=True)
    source = run_dir / "job.json"
    source.write_text(json.dumps(job))
    calls = denied_factory(monkeypatch)
    assert worker.main([str(source), str(run_dir / "worker-result.json")]) == 2
    assert calls == []
    assert not (tmp_path / ".data" / "guardmate-training.sqlite3").exists()


@pytest.mark.parametrize("which", ["job", "result", "job-name", "result-name"])
def test_cli_requires_exact_admitted_artifact_paths(job, tmp_path, monkeypatch, which):
    admitted, source, result_path, _ = admitted_paths(job, tmp_path, monkeypatch)
    if which == "job":
        source = tmp_path / "job.json"
        source.write_text(json.dumps(admitted))
    elif which == "result":
        result_path = tmp_path / "worker-result.json"
    elif which == "job-name":
        source = source.with_name("another-job.json")
        source.write_text(json.dumps(admitted))
    else:
        result_path = result_path.with_name("another-result.json")
    calls = denied_factory(monkeypatch)
    assert worker.main([str(source), str(result_path)]) == 2
    assert calls == []
    assert not result_path.exists()


def test_cli_cannot_replay_same_admitted_result(job, tmp_path, monkeypatch):
    _, source, result_path, _ = admitted_paths(job, tmp_path, monkeypatch)
    trainer = FakeTrainer()
    service = FakeService(trainer)
    monkeypatch.setattr(worker, "_default_service_factory", lambda: (service, FAKE_TYPES))
    assert worker.main([str(source), str(result_path)]) == 0
    first = result_path.read_bytes()
    assert worker.main([str(source), str(result_path)]) == 2
    assert len(service.calls) == 1
    assert result_path.read_bytes() == first
