import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Lock

from pymongo.database import Database
from pymongo.errors import DuplicateKeyError

from ..mongo import MongoPersistence
from .models import Conversation


class ConversationStore:
    def __init__(self, data_dir: Path, *, mongo_database: Database | None = None):
        self._mongo = MongoPersistence.from_environment() if mongo_database is None else None
        database = self._mongo.database if self._mongo is not None else mongo_database
        self._collection = database["conversations"] if database is not None else None
        self._budget = database["model_budget"] if database is not None else None
        self._initialized = False
        self._initialization_lock = Lock()
        if self._collection is not None:
            self.path = None
            return
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

    def initialize(self) -> None:
        if self._collection is None or self._initialized:
            return
        with self._initialization_lock:
            if self._initialized:
                return
            # The fixed _id makes initialization safe across processes and service restarts.
            try:
                self._budget.update_one({"_id": 1}, {"$setOnInsert": {"reserved": 0}}, upsert=True)
            except DuplicateKeyError:
                # Another instance may win the first upsert. Never reset its existing budget.
                if self._budget.find_one({"_id": 1}) is None:
                    raise
            self._collection.create_index([("created_at", -1), ("_id", -1)])
            self._initialized = True

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        if self.path is None:
            raise RuntimeError(
                "SQLite connections are unavailable when MongoDB storage is enabled."
            )
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def read(self, session_id: str) -> Conversation | None:
        if self._collection is not None:
            document = self._collection.find_one({"_id": session_id})
            return Conversation.model_validate(document["value"]) if document is not None else None
        with self.connect() as connection:
            row = connection.execute(
                "SELECT value FROM conversations WHERE id = ?", (session_id,)
            ).fetchone()
        return Conversation.model_validate_json(row[0]) if row else None

    def recent_ids(self, limit: int) -> list[str]:
        if self._collection is not None:
            if limit == 0:
                return []
            cursor = self._collection.find({}, {"_id": 1}).sort([("created_at", -1), ("_id", -1)])
            if limit > 0:
                cursor = cursor.limit(limit)
            return [document["_id"] for document in cursor]
        with self.connect() as connection:
            return [
                row[0]
                for row in connection.execute(
                    "SELECT id FROM conversations "
                    "ORDER BY json_extract(value, '$.created_at') DESC, id DESC LIMIT ?",
                    (limit,),
                )
            ]

    def write(self, conversation: Conversation) -> None:
        if self._collection is not None:
            value = conversation.model_dump(mode="json")
            self._collection.replace_one(
                {"_id": conversation.id},
                {"_id": conversation.id, "created_at": value["created_at"], "value": value},
                upsert=True,
            )
            return
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO conversations (id, value) VALUES (?, ?) "
                "ON CONFLICT(id) DO UPDATE SET value = excluded.value",
                (conversation.id, conversation.model_dump_json()),
            )

    def reserved_microdollars(self) -> int:
        if self._budget is not None:
            self.initialize()
            document = self._budget.find_one({"_id": 1})
            if document is None:
                raise RuntimeError("MongoDB model budget record is missing.")
            return document["reserved"]
        with self.connect() as connection:
            return connection.execute("SELECT reserved FROM model_budget WHERE id = 1").fetchone()[
                0
            ]

    def reserve(self, microdollars: int, limit: int) -> bool:
        # Reserve worst-case tokens BEFORE submission, including failed/timed-out samples.
        if type(microdollars) is not int or type(limit) is not int or microdollars < 0 or limit < 0:
            raise ValueError("Budget reservations and limits must be nonnegative integers.")
        if self._budget is not None:
            self.initialize()
            result = self._budget.update_one(
                {"_id": 1, "reserved": {"$lte": limit - microdollars}},
                {"$inc": {"reserved": microdollars}},
            )
            # Matching and incrementing happen atomically on MongoDB, across all workers.
            return result.matched_count == 1
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE model_budget SET reserved = reserved + ? "
                "WHERE id = 1 AND reserved + ? <= ?",
                (microdollars, microdollars, limit),
            )
            return cursor.rowcount == 1

    def close(self) -> None:
        if self._mongo is not None:
            self._mongo.close()
