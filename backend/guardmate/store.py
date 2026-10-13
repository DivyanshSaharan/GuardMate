import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from pydantic import BaseModel
from pymongo.database import Database

from .mongo import MongoPersistence


class PreferenceStore:
    def __init__(self, data_dir: Path, *, mongo_database: Database | None = None):
        self._mongo = MongoPersistence.from_environment() if mongo_database is None else None
        database = self._mongo.database if self._mongo is not None else mongo_database
        self._collection = database["preferences"] if database is not None else None
        if self._collection is not None:
            self.path = None
            return
        data_dir.mkdir(parents=True, exist_ok=True)
        self.path = data_dir / "guardmate.sqlite3"
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS preferences (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
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

    def read(self, key: str) -> dict | None:
        if self._collection is not None:
            document = self._collection.find_one({"_id": key})
            return document["value"] if document is not None else None
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM preferences WHERE key = ?", (key,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def write(self, key: str, value: BaseModel | None) -> None:
        if self._collection is not None:
            if value is None:
                self._collection.delete_one({"_id": key})
            else:
                self._collection.replace_one(
                    {"_id": key}, {"_id": key, "value": value.model_dump(mode="json")}, upsert=True
                )
            return
        with self._connect() as connection:
            if value is None:
                connection.execute("DELETE FROM preferences WHERE key = ?", (key,))
            else:
                connection.execute(
                    "INSERT INTO preferences (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value.model_dump_json()),
                )

    def close(self) -> None:
        if self._mongo is not None:
            self._mongo.close()
