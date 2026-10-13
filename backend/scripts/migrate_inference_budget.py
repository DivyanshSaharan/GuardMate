"""Carry the inference reservation total to Atlas without uploading resident data."""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from dotenv import load_dotenv  # noqa: E402
from guardmate.mongo import MongoPersistence  # noqa: E402
from pymongo import ReturnDocument  # noqa: E402
from pymongo.errors import DuplicateKeyError, PyMongoError  # noqa: E402


def read_local_budget(source: Path) -> int:
    """Open the explicitly supplied database read-only and read its budget row only."""
    if not source.exists():
        return 0
    connection = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
    try:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'model_budget'"
        ).fetchone()
        if not exists:
            return 0
        row = connection.execute("SELECT reserved FROM model_budget WHERE id = 1").fetchone()
        reserved = row[0] if row is not None else 0
        if type(reserved) is not int or not 0 <= reserved < 2**63:
            raise ValueError("The local inference reservation total is invalid.")
        return reserved
    finally:
        connection.close()


def migrate_budget(source: Path, persistence: MongoPersistence) -> dict[str, int]:
    reserved = read_local_budget(source)
    collection = persistence.database["model_budget"]
    # $max is idempotent and cannot reduce a total already increased by cloud requests.
    try:
        document = collection.find_one_and_update(
            {"_id": 1},
            {"$max": {"reserved": reserved}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
    except DuplicateKeyError:
        # A concurrent initial reservation/migration may create this fixed-id record first.
        document = collection.find_one_and_update(
            {"_id": 1},
            {"$max": {"reserved": reserved}},
            return_document=ReturnDocument.AFTER,
        )
    if document is None:
        raise RuntimeError("The MongoDB inference reservation record is missing.")
    return {
        "source_reserved_microdollars": reserved,
        "target_reserved_microdollars": document["reserved"],
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        required=True,
        help="Explicit path to the existing local guardmate.sqlite3 file (opened read-only).",
    )
    arguments = parser.parse_args(argv)
    load_dotenv(ROOT / ".env", override=False)
    persistence = None
    try:
        persistence = MongoPersistence.from_environment()
        if persistence is None:
            parser.error("Set MONGODB_URI before migrating the inference reservation total.")
        report = migrate_budget(arguments.source, persistence)
    except (OSError, sqlite3.Error, PyMongoError, ValueError, RuntimeError):
        # Never print driver messages: connection errors can contain private connection details.
        parser.exit(
            1, "Inference budget migration failed; verify the local budget and Atlas access.\n"
        )
    finally:
        if persistence is not None:
            persistence.close()
    print(json.dumps(report))


if __name__ == "__main__":
    main()
