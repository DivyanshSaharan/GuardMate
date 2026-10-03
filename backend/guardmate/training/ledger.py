"""Persistent, conservative admission accounting for bounded training requests.

Reservations represent the admitted logical-work estimate, not provider billing. They
are never refunded, including when a request fails or its result is unknown.
The caller must reserve before submitting a chargeable provider operation.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

TRAINING_BUDGET_MICRODOLLARS = 1_750_000

_STATUSES = frozenset({"planned", "running", "completed", "failed", "unknown"})
_TRANSITIONS = {
    "planned": frozenset({"running", "failed", "unknown"}),
    "running": frozenset({"completed", "failed", "unknown"}),
    "completed": frozenset(),
    "failed": frozenset(),
    "unknown": frozenset(),
}
_SQLITE_MAX_INTEGER = (1 << 63) - 1


class TrainingBudgetError(RuntimeError):
    """A training run or reservation was not admitted by the local ledger."""


def _positive_integer(value: int, name: str, maximum: int) -> None:
    if type(value) is not int or not 0 < value <= maximum:
        raise TrainingBudgetError(f"{name} must be a positive integer at most {maximum}.")


def _identifier(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise TrainingBudgetError(f"{name} must be a nonempty string.")


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


class TrainingLedger:
    """A separate SQLite ledger; ``directory`` must be explicitly supplied.

    Each method uses a fresh connection so admission remains atomic across
    threads, independently created instances, and processes sharing this file.
    This ledger does not inspect or modify the inference budget database.
    """

    def __init__(self, directory: Path):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "guardmate-training.sqlite3"
        with self._transaction(immediate=True) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS training_runs ("
                "run_id TEXT PRIMARY KEY, "
                "fingerprint TEXT NOT NULL, "
                "max_microdollars INTEGER NOT NULL CHECK(max_microdollars > 0), "
                "expected_updates INTEGER NOT NULL CHECK(expected_updates > 0), "
                "status TEXT NOT NULL CHECK(status IN "
                "('planned', 'running', 'completed', 'failed', 'unknown')), "
                "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS training_reservations ("
                "run_id TEXT NOT NULL REFERENCES training_runs(run_id), "
                "operation_id TEXT NOT NULL, "
                "microdollars INTEGER NOT NULL CHECK(microdollars > 0), "
                "created_at TEXT NOT NULL, PRIMARY KEY(run_id, operation_id))"
            )

    @contextmanager
    def _transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _run(connection: sqlite3.Connection, run_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM training_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise TrainingBudgetError("Training run does not exist.")
        return row

    def create_run(
        self, run_id: str, fingerprint: str, max_microdollars: int, expected_updates: int
    ) -> None:
        _identifier(run_id, "run_id")
        _identifier(fingerprint, "fingerprint")
        _positive_integer(max_microdollars, "max_microdollars", TRAINING_BUDGET_MICRODOLLARS)
        _positive_integer(expected_updates, "expected_updates", _SQLITE_MAX_INTEGER)
        now = _timestamp()
        with self._transaction(immediate=True) as connection:
            if connection.execute(
                "SELECT 1 FROM training_runs WHERE run_id = ?", (run_id,)
            ).fetchone():
                raise TrainingBudgetError("Training run already exists; it cannot be recreated.")
            if connection.execute(
                "SELECT 1 FROM training_runs WHERE fingerprint = ?", (fingerprint,)
            ).fetchone():
                raise TrainingBudgetError(
                    "This training fingerprint was already admitted; do not replay it."
                )
            connection.execute(
                "INSERT INTO training_runs "
                "(run_id, fingerprint, max_microdollars, expected_updates, status, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, 'planned', ?, ?)",
                (run_id, fingerprint, max_microdollars, expected_updates, now, now),
            )

    def reserve(self, run_id: str, operation_id: str, microdollars: int) -> None:
        _identifier(run_id, "run_id")
        _identifier(operation_id, "operation_id")
        _positive_integer(microdollars, "microdollars", TRAINING_BUDGET_MICRODOLLARS)
        with self._transaction(immediate=True) as connection:
            run = self._run(connection, run_id)
            if run["status"] != "running":
                raise TrainingBudgetError("Only running training runs can reserve a request.")
            if connection.execute(
                "SELECT 1 FROM training_reservations WHERE run_id = ? AND operation_id = ?",
                (run_id, operation_id),
            ).fetchone():
                raise TrainingBudgetError("Operation is already reserved; it must not be retried.")
            reserved, updates = connection.execute(
                "SELECT COALESCE(SUM(microdollars), 0), COUNT(*) "
                "FROM training_reservations WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            aggregate = connection.execute(
                "SELECT COALESCE(SUM(microdollars), 0) FROM training_reservations"
            ).fetchone()[0]
            if updates >= run["expected_updates"]:
                raise TrainingBudgetError("The training run's update limit is exhausted.")
            if reserved + microdollars > run["max_microdollars"]:
                raise TrainingBudgetError("The training run's reservation budget is exhausted.")
            if aggregate + microdollars > TRAINING_BUDGET_MICRODOLLARS:
                raise TrainingBudgetError("The aggregate training reservation budget is exhausted.")
            connection.execute(
                "INSERT INTO training_reservations "
                "(run_id, operation_id, microdollars, created_at) VALUES (?, ?, ?, ?)",
                (run_id, operation_id, microdollars, _timestamp()),
            )

    def status(self, run_id: str) -> dict:
        _identifier(run_id, "run_id")
        with self._transaction() as connection:
            run = dict(self._run(connection, run_id))
            reserved, updates = connection.execute(
                "SELECT COALESCE(SUM(microdollars), 0), COUNT(*) "
                "FROM training_reservations WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            aggregate = connection.execute(
                "SELECT COALESCE(SUM(microdollars), 0) FROM training_reservations"
            ).fetchone()[0]
        return {
            **run,
            "reserved_microdollars": reserved,
            "aggregate_reserved_microdollars": aggregate,
            "reserved_updates": updates,
        }

    def set_status(self, run_id: str, status: str) -> None:
        _identifier(run_id, "run_id")
        if not isinstance(status, str) or status not in _STATUSES:
            raise TrainingBudgetError("Invalid training status.")
        with self._transaction(immediate=True) as connection:
            current = self._run(connection, run_id)["status"]
            if status == current:
                return
            if status not in _TRANSITIONS[current]:
                raise TrainingBudgetError(
                    f"Cannot change training status from {current} to {status}."
                )
            connection.execute(
                "UPDATE training_runs SET status = ?, updated_at = ? WHERE run_id = ?",
                (status, _timestamp(), run_id),
            )
