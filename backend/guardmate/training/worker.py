"""Isolated training executor, invoked only after the supervisor's admission.

This module does not load environment files, account for budgets, or add
application retries. The SDK handles its own internal retries. An unconfirmed
provider result ends the job as unknown; the supervisor bounds the process's
local lifetime without claiming to cancel remote work.
"""

import argparse
import hashlib
import json
import math
import os
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

MODEL = "Qwen/Qwen3.5-4B"
RANK = 16
SEED = 42
LEARNING_RATE = 0.0001
CHECKPOINT_TTL_SECONDS = 86_400
ROOT = Path(__file__).resolve().parents[3]


def _default_service_factory() -> tuple[Any, Any]:
    import tinker

    return tinker.ServiceClient(), tinker.types


def _integer(value: Any, *, minimum: int = 0) -> bool:
    return type(value) is int and value >= minimum


def _validate_job(job: dict) -> None:
    if not isinstance(job, dict) or job.get("model") != MODEL:
        raise ValueError("Unsupported training model.")
    config = job.get("config")
    if (
        not isinstance(config, dict)
        or type(config.get("rank")) is not int
        or config["rank"] != RANK
        or type(config.get("seed")) is not int
        or config["seed"] != SEED
        or type(config.get("learning_rate")) not in (int, float)
        or config["learning_rate"] != LEARNING_RATE
        or not _integer(config.get("epochs"), minimum=1)
        or not _integer(config.get("batch_size"), minimum=1)
    ):
        raise ValueError("Unsupported training configuration.")
    if not isinstance(job.get("fingerprint"), str) or not re.fullmatch(
        r"[0-9a-f]{64}", job["fingerprint"]
    ):
        raise ValueError("Invalid job fingerprint.")
    datums = job.get("datums")
    if not isinstance(datums, list) or not datums:
        raise ValueError("Training datums are required.")
    for datum in datums:
        if not isinstance(datum, dict):
            raise ValueError("Invalid training datum.")
        inputs = datum.get("input_tokens")
        targets = datum.get("target_tokens")
        weights = datum.get("weights")
        if (
            not isinstance(inputs, list)
            or not inputs
            or not isinstance(targets, list)
            or not isinstance(weights, list)
            or len(inputs) != len(targets)
            or len(inputs) != len(weights)
            or not all(_integer(token) for token in inputs + targets)
            or not all(
                type(weight) in (int, float) and math.isfinite(weight) and weight in (0, 1)
                for weight in weights
            )
            or not any(weights)
        ):
            raise ValueError("Invalid training token arrays.")
    schedule = job.get("schedule")
    if not isinstance(schedule, list) or not schedule:
        raise ValueError("A nonempty training schedule is required.")
    for step in schedule:
        if not isinstance(step, dict):
            raise ValueError("Invalid schedule entry.")
        indices = step.get("datum_indices")
        if (
            not _integer(step.get("epoch"))
            or not _integer(step.get("batch_index"))
            or not isinstance(indices, list)
            or not indices
            or len(indices) > config["batch_size"]
            or not all(_integer(index) and index < len(datums) for index in indices)
        ):
            raise ValueError("Invalid schedule indices.")


def _datums(job: dict, types: Any) -> list[Any]:
    return [
        types.Datum(
            model_input=types.ModelInput.from_ints(datum["input_tokens"]),
            loss_fn_inputs={
                "target_tokens": types.TensorData(
                    data=datum["target_tokens"], dtype="int64", shape=[len(datum["target_tokens"])]
                ),
                "weights": types.TensorData(
                    data=datum["weights"], dtype="float32", shape=[len(datum["weights"])]
                ),
            },
        )
        for datum in job["datums"]
    ]


def _checkpoint_path(response: Any) -> str:
    path = getattr(response, "path", None)
    if (
        not isinstance(path, str)
        or not path.startswith("tinker://")
        or len(path) <= len("tinker://")
        or len(path) > 2_048
        or any(character.isspace() for character in path)
    ):
        raise ValueError("Provider checkpoint path was not confirmed.")
    return path


def _error_type(error: BaseException) -> str:
    name = type(error).__name__
    return name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", name) else "ProviderError"


def _append_progress(stream: Any, record: dict) -> None:
    stream.write(json.dumps(record, allow_nan=False, sort_keys=True) + "\n")
    stream.flush()
    os.fsync(stream.fileno())


def run_job(
    job: dict,
    result_path: Path,
    *,
    service_factory: Callable[[], tuple[Any, Any]] | None = None,
) -> dict:
    """Run one admitted job, with exclusive result and ``.progress`` outputs.

    The optional factory returns ``(service_client, SDK_types)`` for offline
    tests. Completed update counts only advance after optimizer confirmation.
    No request is retried or resumed, including checkpoint requests.
    """
    result_path = Path(result_path)
    progress_path = Path(str(result_path) + ".progress")
    # Fail before provider initialization if either output already exists.
    if result_path.exists() or progress_path.exists():
        raise FileExistsError("Training result or progress output already exists.")
    confirmed_updates = 0
    fingerprint = job.get("fingerprint") if isinstance(job, dict) else None
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        fingerprint = ""
    phase = "validate_job"
    with (
        result_path.open("x", encoding="utf-8") as output,
        progress_path.open("x", encoding="utf-8") as progress,
    ):
        try:
            _validate_job(job)
            phase = "create_service"
            service, types = (service_factory or _default_service_factory)()
            phase = "prepare_datums"
            datums = _datums(job, types)
            phase = "create_training_client"
            trainer = service.create_lora_training_client(
                base_model=MODEL,
                rank=RANK,
                seed=SEED,
                train_mlp=True,
                train_attn=True,
                train_unembed=True,
                user_metadata={"fingerprint": fingerprint},
            )
            for step in job["schedule"]:
                batch = [datums[index] for index in step["datum_indices"]]
                phase = "forward_backward"
                trainer.forward_backward(batch, loss_fn="cross_entropy").result()
                phase = "optim_step"
                trainer.optim_step(types.AdamParams(learning_rate=LEARNING_RATE)).result()
                confirmed_updates += 1
                phase = "record_progress"
                _append_progress(
                    progress,
                    {
                        "phase": "confirmed_update",
                        "confirmed_updates": confirmed_updates,
                        "epoch": step["epoch"],
                        "batch_index": step["batch_index"],
                        "fingerprint": fingerprint,
                    },
                )
            name = f"guardmate-{fingerprint[:12]}-{uuid4().hex}"
            metadata = {"confirmed_step": str(confirmed_updates), "fingerprint": fingerprint}
            phase = "save_state"
            state_path = _checkpoint_path(
                trainer.save_state(
                    name=f"{name}-state",
                    ttl_seconds=CHECKPOINT_TTL_SECONDS,
                    overwrite=False,
                    user_metadata=metadata,
                ).result()
            )
            phase = "record_progress"
            _append_progress(
                progress,
                {
                    "phase": "confirmed_state",
                    "confirmed_updates": confirmed_updates,
                    "fingerprint": fingerprint,
                    "state_path": state_path,
                },
            )
            phase = "save_sampler"
            sampler_path = _checkpoint_path(
                trainer.save_weights_for_sampler(
                    name=f"{name}-sampler",
                    ttl_seconds=CHECKPOINT_TTL_SECONDS,
                    user_metadata=metadata,
                ).result()
            )
            phase = "record_progress"
            _append_progress(
                progress,
                {
                    "phase": "confirmed_sampler",
                    "confirmed_updates": confirmed_updates,
                    "fingerprint": fingerprint,
                    "sampler_path": sampler_path,
                },
            )
            result = {
                "status": "completed",
                "confirmed_updates": confirmed_updates,
                "state_path": state_path,
                "sampler_path": sampler_path,
                "fingerprint": fingerprint,
            }
        except BaseException as error:
            result = {
                "status": "unknown",
                "confirmed_updates": confirmed_updates,
                "phase": phase,
                "error_type": _error_type(error),
                "fingerprint": fingerprint,
            }
        json.dump(result, output, allow_nan=False, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    return result


def _validate_admission(job: dict, job_path: Path, result_path: Path) -> None:
    from .ledger import TrainingLedger
    from .preparation import SOURCE_PATHS, fingerprint

    _validate_job(job)
    root = ROOT.resolve()
    resolved_job = job_path.resolve(strict=True)
    run_id = resolved_job.parent.name
    if not re.fullmatch(r"[0-9a-f]{32}", run_id):
        raise ValueError("Invalid training run identifier.")
    run_dir = root / ".data" / "training" / "runs" / run_id
    if resolved_job != run_dir / "job.json" or result_path.resolve() != (
        run_dir / "worker-result.json"
    ):
        raise ValueError("Worker artifacts must use the admitted run directory.")
    if job.get("fingerprint") != fingerprint(job) or job.get("review_ready") is not True:
        raise ValueError("Training job must have a valid, reviewed fingerprint.")
    source_hashes = job.get("source_hashes")
    if not isinstance(source_hashes, dict) or any(
        source_hashes.get(name) != hashlib.sha256(path.read_bytes()).hexdigest()
        for name, path in SOURCE_PATHS.items()
    ):
        raise ValueError("Training sources must match every admitted source binding.")
    reservations = [step.get("reserved_microdollars") for step in job["schedule"]]
    if not all(_integer(reservation, minimum=1) for reservation in reservations):
        raise ValueError("Invalid training reservation schedule.")
    data_dir = root / ".data"
    if not (data_dir / "guardmate-training.sqlite3").is_file():
        raise ValueError("Training ledger must already exist.")
    status = TrainingLedger(data_dir).status(run_id)
    if (
        status["status"] != "running"
        or status["fingerprint"] != job["fingerprint"]
        or status["expected_updates"] != len(job["schedule"])
        or status["reserved_updates"] != len(job["schedule"])
        or status["reserved_microdollars"] != sum(reservations)
    ):
        raise ValueError("Training job has not been fully admitted.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Execute one pre-admitted training job.")
    parser.add_argument("job_path", type=Path)
    parser.add_argument("result_path", type=Path)
    args = parser.parse_args(argv)
    phase = "load_job"
    try:
        with args.job_path.open(encoding="utf-8") as source:
            job = json.load(source)
        phase = "validate_admission"
        _validate_admission(job, args.job_path, args.result_path)
        phase = "run_job"
        result = run_job(job, args.result_path)
    except BaseException as error:
        print(
            json.dumps({"status": "unknown", "phase": phase, "error_type": _error_type(error)}),
            file=sys.stderr,
        )
        return 2
    return 0 if result["status"] == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
