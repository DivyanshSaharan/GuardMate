import importlib.util
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import mongomock
import pytest
from guardmate.agent.store import ConversationStore

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "migrate_inference_budget.py"
SPEC = importlib.util.spec_from_file_location("migrate_inference_budget", SCRIPT)
migration = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(migration)


@pytest.fixture
def persistence():
    return SimpleNamespace(database=mongomock.MongoClient()["guardmate"])


def local_budget(tmp_path, reserved):
    source = tmp_path / "guardmate.sqlite3"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE model_budget (id INTEGER PRIMARY KEY, reserved INTEGER)")
        connection.execute("INSERT INTO model_budget VALUES (1, ?)", (reserved,))
        connection.execute("CREATE TABLE preferences (key TEXT PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO preferences VALUES ('profile', 'private resident data')")
        connection.execute("CREATE TABLE conversations (id TEXT PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO conversations VALUES ('private', 'private conversation')")
    return source


def test_migration_is_idempotent_read_only_and_uploads_only_budget(tmp_path, persistence):
    source = local_budget(tmp_path, 125_000)
    before = source.read_bytes()
    expected = {"source_reserved_microdollars": 125_000, "target_reserved_microdollars": 125_000}
    assert migration.migrate_budget(source, persistence) == expected
    assert migration.migrate_budget(source, persistence) == expected
    assert source.read_bytes() == before
    assert persistence.database.list_collection_names() == ["model_budget"]
    assert persistence.database.model_budget.find_one({}) == {"_id": 1, "reserved": 125_000}
    store = ConversationStore(tmp_path, mongo_database=persistence.database)
    assert store.reserved_microdollars() == 125_000
    assert store.reserve(125_000, 250_000)
    assert not store.reserve(1, 250_000)


def test_migration_never_lowers_existing_cloud_reservations(tmp_path, persistence):
    source = local_budget(tmp_path, 100)
    persistence.database.model_budget.insert_one({"_id": 1, "reserved": 200})
    assert migration.migrate_budget(source, persistence)["target_reserved_microdollars"] == 200
    assert persistence.database.model_budget.count_documents({}) == 1


def test_missing_local_database_is_zero_and_does_not_create_source(tmp_path, persistence):
    source = tmp_path / "does-not-exist.sqlite3"
    assert migration.migrate_budget(source, persistence) == {
        "source_reserved_microdollars": 0,
        "target_reserved_microdollars": 0,
    }
    assert not source.exists()


@pytest.mark.parametrize("table", [False, True])
def test_missing_local_budget_table_or_row_is_zero(tmp_path, persistence, table):
    source = tmp_path / "guardmate.sqlite3"
    with sqlite3.connect(source) as connection:
        if table:
            connection.execute(
                "CREATE TABLE model_budget (id INTEGER PRIMARY KEY, reserved INTEGER)"
            )
    assert migration.migrate_budget(source, persistence)["source_reserved_microdollars"] == 0


@pytest.mark.parametrize("reserved", [-1, "invalid", None])
def test_invalid_source_budget_fails_before_upload(tmp_path, persistence, reserved):
    source = local_budget(tmp_path, reserved)
    with pytest.raises(ValueError, match="invalid"):
        migration.migrate_budget(source, persistence)
    assert persistence.database.model_budget.count_documents({}) == 0


def test_cli_suppresses_connection_secrets(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(migration, "load_dotenv", lambda *args, **kwargs: None)

    def unavailable():
        raise RuntimeError("mongodb://private-user:private-password@private-host")

    monkeypatch.setattr(migration.MongoPersistence, "from_environment", unavailable)
    with pytest.raises(SystemExit) as caught:
        migration.main(["--source", str(tmp_path / "source.sqlite3")])
    assert caught.value.code == 1
    output = capsys.readouterr()
    assert "private-password" not in output.err
    assert "private-host" not in output.err
    assert output.out == ""
