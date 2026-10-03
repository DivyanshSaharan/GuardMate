import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .models import Conversation


class ConversationStore:
    def __init__(self, data_dir: Path):
        data_dir.mkdir(parents=True, exist_ok=True)
        self.path = data_dir / "guardmate.sqlite3"
        with self.connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS conversations "
                "(id TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS model_budget (id INTEGER PRIMARY KEY, reserved INTEGER)"
            )
            connection.execute("INSERT OR IGNORE INTO model_budget VALUES (1, 0)")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def read(self, session_id: str) -> Conversation | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT value FROM conversations WHERE id = ?", (session_id,)
            ).fetchone()
        return Conversation.model_validate_json(row[0]) if row else None

    def write(self, conversation: Conversation) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO conversations (id, value) VALUES (?, ?) "
                "ON CONFLICT(id) DO UPDATE SET value = excluded.value",
                (conversation.id, conversation.model_dump_json()),
            )

    def reserved_microdollars(self) -> int:
        with self.connect() as connection:
            return connection.execute("SELECT reserved FROM model_budget WHERE id = 1").fetchone()[
                0
            ]

    def reserve(self, microdollars: int, limit: int) -> bool:
        # Reserve worst-case tokens BEFORE submission, including failed/timed-out samples.
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE model_budget SET reserved = reserved + ? "
                "WHERE id = 1 AND reserved + ? <= ?",
                (microdollars, microdollars, limit),
            )
            return cursor.rowcount == 1
