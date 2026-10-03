"""Isolate opt-in SDK work; uncertain requests are never replayed or refunded.

The persistent ledger bounds estimated logical work, NOT provider billing. SDK
internal retries and remote work surviving a timeout cannot be cancelled here.
"""

import json
import os
import subprocess
import sys
import uuid
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from .ledger import TRAINING_BUDGET_MICRODOLLARS, TrainingBudgetError, TrainingLedger
from .preparation import fingerprint

SDK_VERSION = "0.32.0"
PRICE_CHECKED_ON = "2026-10-04"


class TrainingAdmissionError(RuntimeError):
    """No hosted work was admitted, or an admitted run became uncertain."""


def validate_admission(
    job: dict,
    current_preview: dict,
    *,
    max_microdollars: int,
    max_updates: int,
    acknowledge_retries: bool,
    price_checked_on: str,
    duration_seconds: int,
) -> None:
    if acknowledge_retries is not True:
        raise TrainingAdmissionError(
            "Acknowledge SDK retries: the estimate is not a guaranteed invoice ceiling."
        )
    if price_checked_on != PRICE_CHECKED_ON:
        raise TrainingAdmissionError("Verify the documented rate card date before live training.")
    if (
        type(max_microdollars) is not int
        or not 0 < max_microdollars <= TRAINING_BUDGET_MICRODOLLARS
    ):
        raise TrainingAdmissionError("Training estimate limit must be positive and at most $1.75.")
    if type(max_updates) is not int or not 1 <= max_updates <= 30:
        raise TrainingAdmissionError("Logical optimizer-step limit must be from 1 to 30.")
    if type(duration_seconds) is not int or not 60 <= duration_seconds <= 1800:
        raise TrainingAdmissionError("Worker duration must be from 60 to 1800 seconds.")
    if not isinstance(job, dict) or job.get("fingerprint") != fingerprint(job):
        raise TrainingAdmissionError("Job fingerprint is invalid.")
    if job != current_preview:
        raise TrainingAdmissionError(
            "Dataset, policy, training source, tokenizer or configuration changed. Prepare again."
        )
    if job.get("review_ready") is not True:
        raise TrainingAdmissionError("Draft training labels cannot enable hosted training.")
    totals = job["totals"]
    if totals["reserved_microdollars"] > max_microdollars:
        raise TrainingAdmissionError("The complete planned schedule exceeds this estimate limit.")
    if totals["logical_batches"] > max_updates:
        raise TrainingAdmissionError("The complete planned schedule exceeds this step limit.")


def require_sdk_version() -> None:
    try:
        installed = version("tinker")
    except PackageNotFoundError:
        raise TrainingAdmissionError("Install the pinned optional AI dependencies first.") from None
    if installed != SDK_VERSION:
        raise TrainingAdmissionError("SDK version changed; re-audit retry and checkpoint behavior.")


def worker_environment(root: Path, api_key: str) -> dict[str, str]:
    # Do not expose unrelated .env or cloud credentials to the SDK subprocess.
    permitted = (
        "SystemRoot",
        "WINDIR",
        "PATH",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "LANG",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    )
    return {
        **{name: os.environ[name] for name in permitted if name in os.environ},
        "TINKER_API_KEY": api_key,
        "PYTHONPATH": str(root / "backend"),
        "PYTHONUNBUFFERED": "1",
    }


def _save(path: Path, value: dict) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True, indent=2, ensure_ascii=True)
        stream.write("\n")


def _read_progress(
    path: Path, maximum: int, expected_fingerprint: str
) -> tuple[int, str | None, str | None]:
    """Read only complete progress records; partial writes cannot prove a step."""
    confirmed = 0
    state_path = sampler_path = None
    try:
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                    if (
                        not isinstance(record, dict)
                        or record.get("fingerprint") != expected_fingerprint
                    ):
                        break
                    value = record.get("confirmed_updates")
                    phase = record.get("phase")
                    if type(value) is not int:
                        break
                    if phase == "confirmed_update" and value == confirmed + 1 and value <= maximum:
                        confirmed = value
                    elif phase == "confirmed_state" and value == confirmed == maximum:
                        if state_path is not None or not isinstance(record.get("state_path"), str):
                            break
                        state_path = record["state_path"]
                    elif phase == "confirmed_sampler" and value == confirmed == maximum:
                        if (
                            state_path is None
                            or sampler_path is not None
                            or not isinstance(record.get("sampler_path"), str)
                        ):
                            break
                        sampler_path = record["sampler_path"]
                    else:
                        break
                except (json.JSONDecodeError, AttributeError):
                    break
    except OSError:
        pass
    return confirmed, state_path, sampler_path


def launch(
    root: Path,
    job: dict,
    current_preview: dict,
    *,
    max_microdollars: int,
    max_updates: int,
    acknowledge_retries: bool,
    price_checked_on: str,
    duration_seconds: int = 900,
) -> dict:
    """Reserve every planned batch BEFORE initializing the hosted SDK worker."""
    validate_admission(
        job,
        current_preview,
        max_microdollars=max_microdollars,
        max_updates=max_updates,
        acknowledge_retries=acknowledge_retries,
        price_checked_on=price_checked_on,
        duration_seconds=duration_seconds,
    )
    require_sdk_version()
    api_key = os.environ.get("TINKER_API_KEY", "")
    if not api_key:
        raise TrainingAdmissionError("TINKER_API_KEY is not configured. No job sent.")
    run_id = uuid.uuid4().hex
    data_dir = root / ".data"
    run_dir = data_dir / "training" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    job_path = run_dir / "job.json"
    result_path = run_dir / "worker-result.json"
    _save(job_path, job)
    ledger = TrainingLedger(data_dir)
    try:
        ledger.create_run(run_id, job["fingerprint"], max_microdollars, len(job["schedule"]))
    except TrainingBudgetError:
        raise TrainingAdmissionError(
            "Training job was already admitted or its allowance is invalid. No SDK worker started."
        ) from None
    ledger.set_status(run_id, "running")
    try:
        # Admission failure halfway through keeps prior reservations. No hosted
        # work is submitted until the entire schedule has been reserved.
        for index, batch in enumerate(job["schedule"]):
            ledger.reserve(run_id, f"update-{index + 1}", batch["reserved_microdollars"])
    except TrainingBudgetError:
        ledger.set_status(run_id, "failed")
        raise TrainingAdmissionError(
            "Schedule admission failed; no SDK worker was started."
        ) from None

    process = None
    worker_result = None
    reason = "worker-unknown"
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "guardmate.training.worker",
                str(job_path),
                str(result_path),
            ],
            cwd=root,
            env=worker_environment(root, api_key),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        process.wait(timeout=duration_seconds)
        if process.returncode == 0 and result_path.exists():
            worker_result = json.loads(result_path.read_text(encoding="utf-8"))
            reason = "worker-reported-unknown"
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        reason = "local-timeout-or-interruption-remote-work-unresolved"
    except (OSError, ValueError):
        reason = "worker-start-or-result-failed"
    finally:
        if process is not None and process.poll() is None:
            try:
                process.kill()
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                reason = "worker-termination-unconfirmed-remote-work-unresolved"

    expected = len(job["schedule"])
    confirmed, recorded_state, recorded_sampler = _read_progress(
        Path(str(result_path) + ".progress"), expected, job["fingerprint"]
    )
    completed = (
        isinstance(worker_result, dict)
        and worker_result.get("status") == "completed"
        and worker_result.get("fingerprint") == job["fingerprint"]
        and type(worker_result.get("confirmed_updates")) is int
        and worker_result["confirmed_updates"] == expected == confirmed
        and isinstance(worker_result.get("state_path"), str)
        and worker_result["state_path"].startswith("tinker://")
        and "/weights/" in worker_result["state_path"]
        and isinstance(worker_result.get("sampler_path"), str)
        and worker_result["sampler_path"].startswith("tinker://")
        and "/sampler_weights/" in worker_result["sampler_path"]
        and worker_result["state_path"] == recorded_state
        and worker_result["sampler_path"] == recorded_sampler
    )
    ledger.set_status(run_id, "completed" if completed else "unknown")
    summary = {
        "run_id": run_id,
        "status": "completed" if completed else "unknown",
        "reason": "confirmed-schedule-and-checkpoints" if completed else reason,
        "fingerprint": job["fingerprint"],
        "confirmed_updates": confirmed,
        "expected_updates": expected,
        "ledger": ledger.status(run_id),
        "note": "Reservations are estimates, not invoices. Never replay an unknown run.",
    }
    if completed:
        summary["state_path"] = worker_result["state_path"]
        summary["sampler_path"] = worker_result["sampler_path"]
    _save(run_dir / "summary.json", summary)
    return summary
