import multiprocessing
import sqlite3
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor

import pytest
from guardmate.agent.store import ConversationStore
from guardmate.training.ledger import (
    TRAINING_BUDGET_MICRODOLLARS,
    TrainingBudgetError,
    TrainingLedger,
)


def running_ledger(tmp_path, *, maximum=100, updates=10, run_id="run"):
    ledger = TrainingLedger(tmp_path)
    fingerprint = "dataset-and-plan-fingerprint" if run_id == "run" else f"fingerprint-{run_id}"
    ledger.create_run(run_id, fingerprint, maximum, updates)
    ledger.set_status(run_id, "running")
    return ledger


def admit(directory, run_id, operation_id, amount):
    try:
        TrainingLedger(directory).reserve(run_id, operation_id, amount)
    except TrainingBudgetError:
        return False
    return True


def test_run_metadata_and_explicit_database_path(tmp_path):
    ledger = TrainingLedger(tmp_path / ".data")
    ledger.create_run("run", "fingerprint", 120, 2)
    assert ledger.path == tmp_path / ".data" / "guardmate-training.sqlite3"
    result = ledger.status("run")
    assert result["run_id"] == "run"
    assert result["fingerprint"] == "fingerprint"
    assert result["max_microdollars"] == 120
    assert result["expected_updates"] == 2
    assert result["status"] == "planned"
    assert result["reserved_microdollars"] == 0
    assert result["aggregate_reserved_microdollars"] == 0
    assert result["reserved_updates"] == 0
    assert result["created_at"] == result["updated_at"]


def test_exact_run_budget_boundary_is_admitted_and_excess_is_not(tmp_path):
    ledger = running_ledger(tmp_path)
    ledger.reserve("run", "update-1", 40)
    ledger.reserve("run", "update-2", 60)
    with pytest.raises(TrainingBudgetError, match="run's reservation budget"):
        ledger.reserve("run", "update-3", 1)
    assert ledger.status("run")["reserved_microdollars"] == 100
    assert ledger.status("run")["reserved_updates"] == 2


def test_exact_aggregate_boundary_persists_across_run_states_and_instances(tmp_path):
    ledger = running_ledger(tmp_path, maximum=TRAINING_BUDGET_MICRODOLLARS)
    ledger.reserve("run", "update-1", TRAINING_BUDGET_MICRODOLLARS - 1)
    ledger.set_status("run", "failed")
    other = running_ledger(tmp_path, maximum=10, run_id="other")
    other.reserve("other", "update-1", 1)
    with pytest.raises(TrainingBudgetError, match="aggregate"):
        other.reserve("other", "update-2", 1)
    assert TrainingLedger(tmp_path).status("run")["aggregate_reserved_microdollars"] == (
        TRAINING_BUDGET_MICRODOLLARS
    )


def test_update_limit_is_enforced_even_with_unspent_money(tmp_path):
    ledger = running_ledger(tmp_path, updates=1)
    ledger.reserve("run", "update-1", 1)
    with pytest.raises(TrainingBudgetError, match="update limit"):
        ledger.reserve("run", "update-2", 1)
    assert ledger.status("run")["reserved_microdollars"] == 1


def test_duplicate_operations_are_not_retryable_and_do_not_double_reserve(tmp_path):
    ledger = running_ledger(tmp_path)
    ledger.reserve("run", "update-1", 25)
    for amount in (25, 20):
        with pytest.raises(TrainingBudgetError, match="already reserved"):
            TrainingLedger(tmp_path).reserve("run", "update-1", amount)
    assert ledger.status("run")["reserved_microdollars"] == 25
    assert ledger.status("run")["reserved_updates"] == 1


def test_operation_ids_are_scoped_to_each_run(tmp_path):
    ledger = running_ledger(tmp_path)
    ledger.reserve("run", "update-1", 5)
    other = running_ledger(tmp_path, run_id="other")
    other.reserve("other", "update-1", 7)
    assert ledger.status("run")["aggregate_reserved_microdollars"] == 12


def test_recreating_a_run_cannot_reset_its_reservations_or_metadata(tmp_path):
    ledger = running_ledger(tmp_path)
    ledger.reserve("run", "update-1", 15)
    with pytest.raises(TrainingBudgetError, match="already exists"):
        TrainingLedger(tmp_path).create_run("run", "different", 80, 1)
    status = ledger.status("run")
    assert status["fingerprint"] == "dataset-and-plan-fingerprint"
    assert status["max_microdollars"] == 100
    assert status["expected_updates"] == 10
    assert status["reserved_microdollars"] == 15
    assert status["status"] == "running"


@pytest.mark.parametrize(
    "previous_status", ["planned", "running", "completed", "failed", "unknown"]
)
def test_a_fingerprint_cannot_be_replayed_with_a_new_run_id(tmp_path, previous_status):
    ledger = TrainingLedger(tmp_path)
    ledger.create_run("original", "same-artifact", 100, 1)
    if previous_status != "planned":
        ledger.set_status("original", "running")
        if previous_status != "running":
            ledger.reserve("original", "first", 10)
            ledger.set_status("original", previous_status)
    with pytest.raises(TrainingBudgetError, match="already admitted"):
        TrainingLedger(tmp_path).create_run("new-id", "same-artifact", 100, 1)
    with pytest.raises(TrainingBudgetError, match="does not exist"):
        ledger.status("new-id")


@pytest.mark.parametrize("terminal", ["completed", "failed", "unknown"])
def test_terminal_runs_keep_reservations_and_cannot_resume(tmp_path, terminal):
    ledger = running_ledger(tmp_path)
    ledger.reserve("run", "update-1", 15)
    ledger.set_status("run", terminal)
    reopened = TrainingLedger(tmp_path)
    reopened.set_status("run", terminal)
    for next_status in ("planned", "running", "completed", "failed", "unknown"):
        if next_status == terminal:
            continue
        with pytest.raises(TrainingBudgetError, match="Cannot change"):
            reopened.set_status("run", next_status)
    with pytest.raises(TrainingBudgetError, match="Only running"):
        reopened.reserve("run", "update-2", 10)
    assert reopened.status("run")["reserved_microdollars"] == 15
    assert reopened.status("run")["aggregate_reserved_microdollars"] == 15


def test_planned_run_cannot_spend_or_claim_completed(tmp_path):
    ledger = TrainingLedger(tmp_path)
    ledger.create_run("run", "fingerprint", 100, 1)
    ledger.set_status("run", "planned")
    with pytest.raises(TrainingBudgetError, match="Only running"):
        ledger.reserve("run", "update-1", 10)
    with pytest.raises(TrainingBudgetError, match="Cannot change"):
        ledger.set_status("run", "completed")
    assert ledger.status("run")["reserved_microdollars"] == 0


@pytest.mark.parametrize("terminal", ["failed", "unknown"])
def test_planned_run_may_be_aborted_without_spending(tmp_path, terminal):
    ledger = TrainingLedger(tmp_path)
    ledger.create_run("run", "fingerprint", 100, 1)
    ledger.set_status("run", terminal)
    assert ledger.status("run")["status"] == terminal
    assert ledger.status("run")["reserved_microdollars"] == 0


@pytest.mark.parametrize("amount", [0, -1, True, False, 1.5, "1", None, 1_750_001])
def test_run_money_limit_requires_a_positive_integer_within_cap(tmp_path, amount):
    ledger = TrainingLedger(tmp_path)
    with pytest.raises(TrainingBudgetError, match="max_microdollars"):
        ledger.create_run("run", "fingerprint", amount, 1)
    with pytest.raises(TrainingBudgetError, match="does not exist"):
        ledger.status("run")


@pytest.mark.parametrize("updates", [0, -1, True, False, 1.5, "1", None, 1 << 63])
def test_update_limit_requires_a_positive_sqlite_integer(tmp_path, updates):
    ledger = TrainingLedger(tmp_path)
    with pytest.raises(TrainingBudgetError, match="expected_updates"):
        ledger.create_run("run", "fingerprint", 100, updates)


@pytest.mark.parametrize("amount", [0, -1, True, False, 1.5, "1", None, 1_750_001])
def test_reservation_requires_a_positive_integer_within_cap(tmp_path, amount):
    ledger = running_ledger(tmp_path)
    with pytest.raises(TrainingBudgetError, match="microdollars"):
        ledger.reserve("run", "update-1", amount)
    assert ledger.status("run")["reserved_microdollars"] == 0


@pytest.mark.parametrize("identifier", ["", "   ", None, 123])
def test_run_identifiers_must_be_nonempty_strings(tmp_path, identifier):
    ledger = TrainingLedger(tmp_path)
    with pytest.raises(TrainingBudgetError, match="run_id"):
        ledger.create_run(identifier, "fingerprint", 100, 1)
    with pytest.raises(TrainingBudgetError, match="run_id"):
        ledger.status(identifier)
    with pytest.raises(TrainingBudgetError, match="run_id"):
        ledger.set_status(identifier, "running")
    with pytest.raises(TrainingBudgetError, match="run_id"):
        ledger.reserve(identifier, "update-1", 10)


@pytest.mark.parametrize("identifier", ["", "   ", None, 123])
def test_fingerprint_and_operation_must_be_nonempty_strings(tmp_path, identifier):
    ledger = TrainingLedger(tmp_path)
    with pytest.raises(TrainingBudgetError, match="fingerprint"):
        ledger.create_run("run", identifier, 100, 1)
    ledger.create_run("run", "fingerprint", 100, 1)
    ledger.set_status("run", "running")
    with pytest.raises(TrainingBudgetError, match="operation_id"):
        ledger.reserve("run", identifier, 1)


@pytest.mark.parametrize("status", ["", "pending", "RUNNING", None, 123, []])
def test_status_values_are_strict(tmp_path, status):
    ledger = running_ledger(tmp_path)
    with pytest.raises(TrainingBudgetError, match="Invalid training status"):
        ledger.set_status("run", status)
    assert ledger.status("run")["status"] == "running"


def test_missing_run_is_never_admitted(tmp_path):
    ledger = TrainingLedger(tmp_path)
    for method, args in (
        (ledger.status, ("missing",)),
        (ledger.reserve, ("missing", "update-1", 10)),
        (ledger.set_status, ("missing", "running")),
    ):
        with pytest.raises(TrainingBudgetError, match="does not exist"):
            method(*args)


def test_untrusted_ids_and_fingerprints_are_sql_parameters(tmp_path):
    run_id = "run'); DROP TABLE training_runs; --"
    fingerprint = "'; DELETE FROM training_reservations; --"
    operation = "update'); UPDATE training_runs SET max_microdollars = 99999999; --"
    ledger = TrainingLedger(tmp_path)
    ledger.create_run(run_id, fingerprint, 100, 1)
    ledger.set_status(run_id, "running")
    ledger.reserve(run_id, operation, 10)
    result = ledger.status(run_id)
    assert result["fingerprint"] == fingerprint
    assert result["max_microdollars"] == 100
    assert result["reserved_microdollars"] == 10
    with sqlite3.connect(ledger.path) as connection:
        assert connection.execute("SELECT operation_id FROM training_reservations").fetchone() == (
            operation,
        )


def test_training_ledger_never_changes_existing_inference_budget(tmp_path):
    inference = ConversationStore(tmp_path)
    assert inference.reserve(70_000, 250_000)
    ledger = running_ledger(tmp_path)
    ledger.reserve("run", "update-1", 90)
    ledger.set_status("run", "unknown")
    TrainingLedger(tmp_path)
    assert inference.reserved_microdollars() == 70_000
    assert ledger.path != inference.path


def test_same_run_admission_is_atomic_across_threads_and_instances(tmp_path):
    ledger = running_ledger(tmp_path, maximum=100, updates=30)
    with ThreadPoolExecutor(max_workers=12) as executor:
        results = list(
            executor.map(lambda number: admit(tmp_path, "run", f"update-{number}", 10), range(30))
        )
    assert sum(results) == 10
    result = ledger.status("run")
    assert result["reserved_microdollars"] == 100
    assert result["reserved_updates"] == 10


def test_duplicate_admission_is_atomic_across_threads_and_instances(tmp_path):
    ledger = running_ledger(tmp_path, maximum=1_000, updates=30)
    with ThreadPoolExecutor(max_workers=12) as executor:
        results = list(executor.map(lambda _: admit(tmp_path, "run", "update-1", 10), range(30)))
    assert sum(results) == 1
    result = ledger.status("run")
    assert result["reserved_microdollars"] == 10
    assert result["reserved_updates"] == 1


def test_aggregate_admission_is_atomic_across_different_runs(tmp_path):
    ledger = TrainingLedger(tmp_path)
    for number in range(12):
        ledger.create_run(f"run-{number}", f"fingerprint-{number}", 250_000, 1)
        ledger.set_status(f"run-{number}", "running")
    with ThreadPoolExecutor(max_workers=12) as executor:
        results = list(
            executor.map(
                lambda number: admit(tmp_path, f"run-{number}", "update-1", 250_000), range(12)
            )
        )
    assert sum(results) == 7
    assert ledger.status("run-0")["aggregate_reserved_microdollars"] == (
        TRAINING_BUDGET_MICRODOLLARS
    )


def test_aggregate_admission_is_atomic_across_spawned_processes(tmp_path):
    ledger = TrainingLedger(tmp_path)
    for number in range(12):
        ledger.create_run(f"run-{number}", f"fingerprint-{number}", 250_000, 1)
        ledger.set_status(f"run-{number}", "running")
    with ProcessPoolExecutor(
        max_workers=4, mp_context=multiprocessing.get_context("spawn")
    ) as executor:
        futures = [
            executor.submit(admit, tmp_path, f"run-{number}", "update-1", 250_000)
            for number in range(12)
        ]
        results = [future.result(timeout=30) for future in futures]
    assert sum(results) == 7
    assert TrainingLedger(tmp_path).status("run-0")["aggregate_reserved_microdollars"] == (
        TRAINING_BUDGET_MICRODOLLARS
    )


def test_simultaneous_new_instances_preserve_existing_reservations(tmp_path):
    ledger = running_ledger(tmp_path)
    ledger.reserve("run", "update-1", 25)
    with ThreadPoolExecutor(max_workers=12) as executor:
        results = list(
            executor.map(
                lambda _: TrainingLedger(tmp_path).status("run")["reserved_microdollars"],
                range(30),
            )
        )
    assert results == [25] * 30


def test_simultaneous_run_creation_never_reinitializes_existing_run(tmp_path):
    def create(_):
        try:
            TrainingLedger(tmp_path).create_run("run", "fingerprint", 100, 1)
        except TrainingBudgetError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=12) as executor:
        results = list(executor.map(create, range(30)))
    assert sum(results) == 1
    assert TrainingLedger(tmp_path).status("run")["status"] == "planned"


def test_ledger_exposes_no_reset_refund_or_purge_api(tmp_path):
    ledger = TrainingLedger(tmp_path)
    assert not any(
        hasattr(ledger, name) for name in ("reset", "refund", "release", "purge", "delete")
    )


def test_training_and_existing_inference_caps_fit_planned_two_dollars():
    assert TRAINING_BUDGET_MICRODOLLARS + 250_000 == 2_000_000
