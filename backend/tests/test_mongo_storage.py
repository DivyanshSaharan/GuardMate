from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, time, timedelta, timezone
from unittest.mock import MagicMock

import mongomock
import pytest
from guardmate import mongo
from guardmate.agent.models import Approval, Conversation, Message
from guardmate.agent.store import ConversationStore
from guardmate.models import Availability, DeliveryMode, ResidentProfile, TodayOverride
from guardmate.mongo import MongoPersistence
from guardmate.store import PreferenceStore
from pymongo.errors import InvalidURI, NetworkTimeout


@pytest.fixture(autouse=True)
def isolated_mongo_environment(monkeypatch):
    monkeypatch.delenv("MONGODB_URI", raising=False)
    monkeypatch.delenv("MONGODB_DATABASE", raising=False)


@pytest.fixture
def database():
    return mongomock.MongoClient()["guardmate"]


def test_mongo_preferences_preserve_json_types_across_store_restarts(tmp_path, database):
    store = PreferenceStore(tmp_path, mongo_database=database)
    profile = ResidentProfile(resident_name="Resident", office_start=time(8, 15))
    override = TodayOverride(status=Availability.AT_PG, local_date=date(2026, 10, 13))
    mode = DeliveryMode(enabled=True, expires_at=datetime(2026, 10, 13, 13, tzinfo=UTC))
    for key, value in [("profile", profile), ("today_override", override), ("delivery_mode", mode)]:
        store.write(key, value)

    restarted = PreferenceStore(tmp_path, mongo_database=database)
    assert restarted.read("profile")["office_start"] == "08:15:00"
    assert restarted.read("profile")["outside_office"] == "ask_me"
    assert ResidentProfile.model_validate(restarted.read("profile")) == profile
    assert TodayOverride.model_validate(restarted.read("today_override")) == override
    assert DeliveryMode.model_validate(restarted.read("delivery_mode")) == mode
    store.write("profile", ResidentProfile(resident_name="Updated"))
    assert restarted.read("profile")["resident_name"] == "Updated"
    assert database.preferences.count_documents({"_id": "profile"}) == 1
    restarted.write("today_override", None)
    assert store.read("today_override") is None
    assert store.read("missing") is None
    assert not (tmp_path / "guardmate.sqlite3").exists()


def test_mongo_conversations_keep_microseconds_nested_dates_and_isolated_values(tmp_path, database):
    instant = datetime(2026, 10, 13, 13, 0, 0, 123456, tzinfo=UTC)
    conversation = Conversation(
        id="courier-a",
        created_at=instant,
        messages=[Message(role="courier", content="Parcel", at=instant)],
        approval=Approval(
            id="approval-a",
            location="Reception",
            reason="Resident approved",
            expires_at=instant + timedelta(minutes=5),
            profile_fingerprint="context",
        ),
    )
    store = ConversationStore(tmp_path, mongo_database=database)
    store.write(conversation)
    restarted = ConversationStore(tmp_path, mongo_database=database)
    restored = restarted.read(conversation.id)
    assert restored == conversation
    restored.messages.clear()
    assert len(store.read(conversation.id).messages) == 1
    conversation.revision = 1
    store.write(conversation)
    assert restarted.read(conversation.id).revision == 1
    assert database.conversations.count_documents({"_id": conversation.id}) == 1
    assert restarted.read("missing") is None
    assert not (tmp_path / "guardmate.sqlite3").exists()


def test_mongo_recent_conversations_match_sqlite_order_and_limit(tmp_path, database):
    instant = datetime(2026, 10, 13, 13, tzinfo=UTC)
    stores = [
        ConversationStore(tmp_path / "sqlite"),
        ConversationStore(tmp_path / "mongo", mongo_database=database),
    ]
    conversations = [
        Conversation(id="a", created_at=instant),
        Conversation(id="b", created_at=instant),
        Conversation(id="newest", created_at=instant + timedelta(seconds=1)),
        Conversation(id="older", created_at=instant - timedelta(seconds=1)),
        Conversation(id="offset", created_at=instant.astimezone(timezone(timedelta(hours=5.5)))),
    ]
    for store in stores:
        for conversation in reversed(conversations):
            store.write(conversation)
    for limit in [0, 1, 3, 100, -1]:
        assert stores[1].recent_ids(limit) == stores[0].recent_ids(limit)


def test_mongo_budget_survives_reinitialization_and_never_increases_beyond_cap(tmp_path, database):
    store = ConversationStore(tmp_path, mongo_database=database)
    store.initialize()
    assert store.reserve(60, 100)
    restarted = ConversationStore(tmp_path, mongo_database=database)
    restarted.initialize()
    assert restarted.reserved_microdollars() == 60
    assert not restarted.reserve(41, 100)
    assert restarted.reserve(40, 100)
    assert not store.reserve(1, 100)
    assert store.reserve(0, 100)
    assert store.reserved_microdollars() == 100
    assert database.model_budget.count_documents({}) == 1
    assert not (tmp_path / "guardmate.sqlite3").exists()


def test_concurrent_mongo_instances_initialize_once_and_atomically_share_cap(tmp_path, database):
    def reserve(index):
        store = ConversationStore(tmp_path / str(index), mongo_database=database)
        return store.reserve(1, 57)

    with ThreadPoolExecutor(max_workers=16) as executor:
        outcomes = list(executor.map(reserve, range(400)))
    assert sum(outcomes) == 57
    assert ConversationStore(tmp_path, mongo_database=database).reserved_microdollars() == 57
    assert database.model_budget.count_documents({}) == 1


@pytest.mark.parametrize(("amount", "limit"), [(-1, 100), (1, -1), (True, 100), (1.5, 100)])
def test_invalid_budget_reservation_cannot_reduce_or_corrupt_shared_budget(
    tmp_path, database, amount, limit
):
    store = ConversationStore(tmp_path, mongo_database=database)
    assert store.reserve(50, 100)
    with pytest.raises(ValueError, match="nonnegative integers"):
        store.reserve(amount, limit)
    assert store.reserved_microdollars() == 50


def test_failed_mongo_budget_operation_raises_without_local_fallback(
    tmp_path, database, monkeypatch
):
    store = ConversationStore(tmp_path, mongo_database=database)
    store.initialize()

    def unavailable(*args, **kwargs):
        raise NetworkTimeout("test database unavailable")

    monkeypatch.setattr(database.model_budget, "update_one", unavailable)
    with pytest.raises(NetworkTimeout):
        store.reserve(1, 100)
    assert not (tmp_path / "guardmate.sqlite3").exists()
    with pytest.raises(RuntimeError, match="SQLite connections are unavailable"):
        with store.connect():
            pytest.fail("MongoDB must never fall back to a SQLite connection")


def test_mongo_environment_selection_shares_database_without_creating_sqlite(tmp_path, monkeypatch):
    client = mongomock.MongoClient()
    monkeypatch.setenv("MONGODB_URI", "mongodb://test.invalid")
    monkeypatch.setenv("MONGODB_DATABASE", "guardmate_custom")
    monkeypatch.setattr(mongo, "MongoClient", lambda *args, **kwargs: client)
    PreferenceStore(tmp_path).write("profile", ResidentProfile(resident_name="Mongo resident"))
    assert PreferenceStore(tmp_path).read("profile")["resident_name"] == "Mongo resident"
    assert ConversationStore(tmp_path).reserve(10, 20)
    assert ConversationStore(tmp_path).reserved_microdollars() == 10
    assert client.guardmate_custom.preferences.count_documents({}) == 1
    assert not (tmp_path / "guardmate.sqlite3").exists()


def test_mongo_connection_has_bounded_timeouts_and_majority_durability(monkeypatch):
    options = {}
    client = MagicMock()

    def construct(uri, **kwargs):
        options.update(kwargs)
        return client

    monkeypatch.setattr(mongo, "MongoClient", construct)
    persistence = MongoPersistence("mongodb://test.invalid")
    assert options["connect"] is False
    for name in ["serverSelectionTimeoutMS", "connectTimeoutMS", "socketTimeoutMS", "timeoutMS"]:
        assert 0 < options[name] <= 10_000
    assert options["retryWrites"] is True
    assert options["maxPoolSize"] == 10
    database_options = client.get_database.call_args.kwargs
    assert database_options["write_concern"].document == {"w": "majority", "wtimeout": 5_000}
    assert database_options["read_concern"].document == {"level": "majority"}
    persistence.ping()
    client.get_database.return_value.command.assert_called_once_with("ping")
    persistence.close()
    client.close.assert_called_once_with()


def test_invalid_mongo_uri_does_not_expose_secret_or_use_sqlite(tmp_path, monkeypatch):
    uri = "mongodb://secret-user:secret-password@test.invalid"
    monkeypatch.setenv("MONGODB_URI", uri)

    def invalid(*args, **kwargs):
        raise InvalidURI(f"bad URI: {uri}")

    monkeypatch.setattr(mongo, "MongoClient", invalid)
    with pytest.raises(RuntimeError, match="Invalid MongoDB connection configuration") as caught:
        ConversationStore(tmp_path)
    assert uri not in str(caught.value)
    assert caught.value.__suppress_context__
    assert not (tmp_path / "guardmate.sqlite3").exists()


@pytest.mark.parametrize("name", ["", "has.dot", "has space", "has/slash", "\x00"])
def test_invalid_mongo_database_name_is_rejected_without_exposing_uri(name):
    with pytest.raises(RuntimeError, match="MONGODB_DATABASE") as caught:
        MongoPersistence("mongodb://secret-user:secret-password@test.invalid", name)
    assert "secret-password" not in str(caught.value)
