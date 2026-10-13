"""Shared, bounded MongoDB connection for durable application storage."""

import os

from pymongo import MongoClient
from pymongo.database import Database
from pymongo.errors import PyMongoError
from pymongo.read_concern import ReadConcern
from pymongo.write_concern import WriteConcern


class MongoPersistence:
    def __init__(self, uri: str, database_name: str = "guardmate"):
        if not uri.strip():
            raise RuntimeError("MONGODB_URI must contain a MongoDB connection string.")
        if not database_name.strip() or any(
            character in database_name for character in '/\\. "$*<>:|?\x00'
        ):
            raise RuntimeError("MONGODB_DATABASE must be a valid MongoDB database name.")
        try:
            self.client = MongoClient(
                uri,
                appname="GuardMate",
                connect=False,
                serverSelectionTimeoutMS=5_000,
                connectTimeoutMS=5_000,
                socketTimeoutMS=5_000,
                waitQueueTimeoutMS=5_000,
                timeoutMS=10_000,
                maxPoolSize=10,
                retryWrites=True,
                retryReads=True,
            )
            self.database: Database = self.client.get_database(
                database_name,
                write_concern=WriteConcern(w="majority", wtimeout=5_000),
                read_concern=ReadConcern("majority"),
            )
        except (PyMongoError, ValueError):
            # Driver exceptions can include URI details; configuration logs must not expose them.
            raise RuntimeError("Invalid MongoDB connection configuration.") from None

    @classmethod
    def from_environment(cls) -> "MongoPersistence | None":
        uri = os.environ.get("MONGODB_URI", "")
        if not uri.strip():
            return None
        return cls(uri, os.environ.get("MONGODB_DATABASE", "guardmate"))

    def ping(self) -> None:
        self.database.command("ping")

    def close(self) -> None:
        self.client.close()
