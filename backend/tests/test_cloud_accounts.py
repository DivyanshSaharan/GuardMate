import copy
import hashlib
import io
import json
import struct
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import ModuleType, SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import mongomock
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from guardmate.agent.models import AgentPlan
from guardmate.cloud.auth import GoogleOAuth, GoogleSettings
from guardmate.cloud.downloads import ENVELOPE_MAGIC, R2Downloads, Release, personalize_installer
from guardmate.cloud.gateway import AccountBudgetStore, CloudService, InferenceRequest
from guardmate.cloud.migrate_legacy import migrate
from guardmate.cloud.routes import SESSION_COOKIE, build_cloud_router
from guardmate.cloud.store import ACCOUNT_ALLOWANCE, CloudError, MongoCloudStore, digest_token
from pymongo.errors import AutoReconnect


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    import os

    for name in list(os.environ):
        if name.startswith(("MONGODB_", "TINKER_", "GOOGLE_", "R2_", "GUARDMATE_CLOUD_")):
            monkeypatch.delenv(name, raising=False)


class Transactions:
    """Deterministic transaction fake: atomic lock plus rollback, never a fake Atlas session."""

    def __init__(self, database):
        self.database = database
        self.lock = threading.RLock()
        self.active = False

    def __call__(self, callback):
        with self.lock:
            snapshot = {
                name: list(self.database[name].find({}))
                for name in self.database.list_collection_names()
            }
            self.active = True
            try:
                return callback(None)
            except Exception:
                for name in self.database.list_collection_names():
                    self.database[name].delete_many({})
                for name, documents in snapshot.items():
                    if documents:
                        self.database[name].insert_many(copy.deepcopy(documents))
                raise
            finally:
                self.active = False


@pytest.fixture
def state():
    database = mongomock.MongoClient(tz_aware=True).guardmate
    clock = {"now": datetime.now(UTC)}
    transactions = Transactions(database)
    store = MongoCloudStore(database, clock=lambda: clock["now"], transaction_runner=transactions)
    store.initialize()
    account = store.ensure_account("verified-google-subject")["_id"]
    return SimpleNamespace(
        database=database, store=store, account=account, clock=clock, transactions=transactions
    )


def request(request_id=None, content="Fictional delivery context"):
    return InferenceRequest.model_validate(
        {
            "request_id": request_id or str(uuid4()),
            "messages": [
                {"role": "system", "content": "GuardMate planner"},
                {"role": "user", "content": content},
            ],
        }
    )


def test_relogin_redownload_activation_and_copies_share_permanent_lifetime_budget(state):
    store, account = state.store, state.account
    assert store.reserve_and_claim(account, str(uuid4()), "digest", 900_000)
    for _ in range(3):
        assert store.ensure_account("verified-google-subject")["_id"] == account
        credential = store.create_credential(account)
        first, copied = store.activate(credential), store.activate(credential)
        assert first["account_id"] == copied["account_id"] == account
        assert first["access_token"] != copied["access_token"]
        assert store.authenticate(copied["access_token"], kind="api") == account
    assert store.account_status(account)["remaining_microdollars"] == 100_000
    assert store.accounts.count_documents({}) == 1
    assert store.account(account)["allowance"] == 1_000_000


def test_opaque_credentials_are_hashed_revocable_and_sessions_expire(state):
    store = state.store
    credential = store.create_credential(state.account)
    session = store.activate(credential)
    stored = json.dumps(list(store.credentials.find({})), default=str)
    stored_sessions = json.dumps(list(store.sessions.find({})), default=str)
    assert credential not in stored
    assert session["access_token"] not in stored_sessions
    assert store.revoke_credential(state.account, digest_token(credential))
    with pytest.raises(CloudError, match="invalid_credential"):
        store.authenticate(session["access_token"], kind="api")
    with pytest.raises(CloudError):
        store.activate(credential)
    other = store.create_session(state.account)
    state.clock["now"] += timedelta(seconds=901)
    with pytest.raises(CloudError, match="sign_in_required"):
        store.authenticate(other["access_token"], kind="web")


def test_concurrent_requests_cannot_exceed_exact_account_cap(state):
    def admit(_):
        try:
            return state.store.reserve_and_claim(state.account, str(uuid4()), "digest", 10_000)
        except CloudError as error:
            assert error.code == "account_budget_exhausted"
            return False

    with ThreadPoolExecutor(max_workers=16) as executor:
        outcomes = list(executor.map(admit, range(130)))
    assert sum(outcomes) == 100
    assert state.store.account(state.account)["reserved"] == ACCOUNT_ALLOWANCE
    assert state.store.requests.count_documents({}) == 100


def test_duplicate_race_claims_one_dispatch_and_changed_payload_cannot_reuse_id(state):
    identifier = str(uuid4())

    def admit(_):
        try:
            return state.store.reserve_and_claim(state.account, identifier, "same", 10)
        except CloudError as error:
            assert error.code == "request_outcome_uncertain"
            return False

    with ThreadPoolExecutor(max_workers=8) as executor:
        outcomes = list(executor.map(admit, range(30)))
    assert sum(outcomes) == 1
    assert state.store.account(state.account)["reserved"] == 10
    with pytest.raises(CloudError, match="request_payload_changed"):
        state.store.reserve_and_claim(state.account, identifier, "changed", 10)


def test_reservation_transaction_rolls_back_counter_when_request_insert_fails(state, monkeypatch):
    def fail(*args, **kwargs):
        raise AutoReconnect("fictional private connection details")

    monkeypatch.setattr(state.store.requests, "insert_one", fail)
    with pytest.raises(AutoReconnect):
        state.store.reserve_and_claim(state.account, str(uuid4()), "digest", 50)
    assert state.store.account(state.account)["reserved"] == 0
    assert state.store.requests.count_documents({}) == 0


def test_paid_provider_runs_after_transaction_commit_and_private_context_is_not_saved(state):
    calls = []

    class Provider:
        def __init__(self, adapter):
            self.adapter = adapter

        def generate(self, messages):
            assert self.adapter.reserve(1174, ACCOUNT_ALLOWANCE)
            assert not state.transactions.active
            assert state.store.account(state.account)["reserved"] == 1174
            calls.append(messages)
            return AgentPlan(action="answer")

    service = CloudService(state.store, Provider)
    body = request(content="PRIVATE fictional resident data")
    result = service.infer(state.account, body)
    assert result["plan"]["action"] == "answer"
    assert result["reserved_microdollars"] == 1174
    with pytest.raises(CloudError, match="request_already_completed"):
        service.infer(state.account, body)
    assert len(calls) == 1
    ledger = json.dumps(list(state.store.requests.find({})), default=str)
    assert "PRIVATE" not in ledger and "messages" not in ledger and '"plan"' not in ledger
    assert state.database.model_budget.count_documents({}) == 0
    assert state.database.conversations.count_documents({}) == 0
    assert state.database.preferences.count_documents({}) == 0


def test_timeout_retains_charge_and_duplicate_is_never_sent_again(state):
    calls = []

    class Provider:
        def __init__(self, adapter):
            self.adapter = adapter

        def generate(self, messages):
            self.adapter.reserve(2000, ACCOUNT_ALLOWANCE)
            calls.append(True)
            raise TimeoutError("secret credential and prompt must not escape")

    body = request()
    service = CloudService(state.store, Provider)
    with pytest.raises(CloudError, match="inference_unavailable") as caught:
        service.infer(state.account, body)
    assert "secret" not in str(caught.value)
    assert state.store.account(state.account)["reserved"] == 2000
    assert state.store.request_record(state.account, str(body.request_id))["state"] == "uncertain"
    with pytest.raises(CloudError, match="request_outcome_uncertain"):
        service.infer(state.account, body)
    assert len(calls) == 1


def test_dispatch_crash_record_and_dedup_are_permanent(state):
    identifier = str(uuid4())
    state.store.reserve_and_claim(state.account, identifier, "digest", 50)
    state.clock["now"] += timedelta(days=365)
    with pytest.raises(CloudError, match="request_outcome_uncertain"):
        state.store.reserve_and_claim(state.account, identifier, "digest", 50)
    assert all(
        "expireAfterSeconds" not in index
        for index in state.store.requests.index_information().values()
    )
    assert state.store.account(state.account)["reserved"] == 50


def test_legacy_migration_is_once_only_retains_new_spend_and_never_modifies_old_ledger(state):
    state.database.model_budget.insert_one({"_id": 1, "reserved": 155565})
    state.store.reserve_and_claim(state.account, str(uuid4()), "digest", 100)
    for _ in range(3):
        state.store.preserve_legacy_spend(state.account, "legacy-original-inference-v1", 155565)
    assert state.store.account(state.account)["reserved"] == 155665
    assert state.database.model_budget.find_one({"_id": 1})["reserved"] == 155565
    other = state.store.ensure_account("other-person")["_id"]
    with pytest.raises(CloudError, match="legacy_migration_conflict"):
        state.store.preserve_legacy_spend(other, "legacy-original-inference-v1", 155565)
    assert state.store.account(other)["reserved"] == 0


def oauth(state, *, bad_claim=None):
    claims = {
        "iss": "https://accounts.google.com",
        "aud": "test-client",
        "sub": "google-person",
        "email_verified": True,
    }

    def verify(token, audience):
        assert token == "verified-by-injected-test-verifier" and audience == "test-client"
        result = {**claims}
        if bad_claim:
            result.update(bad_claim)
        return result

    client = GoogleOAuth(
        state.store,
        GoogleSettings("test-client", "private-secret", "https://app.test"),
        exchange=lambda code, verifier: "verified-by-injected-test-verifier",
        verify=verify,
    )
    url, browser = client.start()
    query = parse_qs(urlsplit(url).query)
    claims["nonce"] = query["nonce"][0]
    assert query["code_challenge_method"] == ["S256"]
    return client, query["state"][0], browser


def test_google_login_checks_claims_browser_state_nonce_and_replay(state):
    client, token, browser = oauth(state)
    with pytest.raises(CloudError, match="invalid_oauth_state"):
        client.complete("code", token, "different-browser")
    session = client.complete("code", token, browser)
    assert state.store.authenticate(session["access_token"], kind="web") == session["account_id"]
    with pytest.raises(CloudError, match="invalid_oauth_state"):
        client.complete("code", token, browser)


@pytest.mark.parametrize(
    "bad",
    [
        {"iss": "https://attacker.test"},
        {"aud": "other"},
        {"email_verified": False},
        {"nonce": "wrong"},
        {"sub": ""},
    ],
)
def test_invalid_google_identity_does_not_allocate_account(state, bad):
    client, token, browser = oauth(state, bad_claim=bad)
    with pytest.raises(CloudError, match="google_sign_in_failed"):
        client.complete("code", token, browser)
    assert state.store.accounts.count_documents({}) == 1


def test_personalized_installer_matches_exact_unsigned_runtime_frame():
    token = "gm_activation_" + "test-only-random" * 4
    framed = personalize_installer(b"MZfake-executable", "https://app.test", token)
    assert framed[-16:] == ENVELOPE_MAGIC
    length = struct.unpack("<I", framed[-20:-16])[0]
    envelope = framed[-52 - length : -52]
    assert hashlib.sha256(envelope).digest() == framed[-52:-20]
    assert json.loads(envelope) == {
        "version": 1,
        "server_url": "https://app.test",
        "bootstrap_token": token,
    }


def test_private_r2_checks_installer_integrity_and_presigns_short_release_url():
    content = b"MZfake-executable"

    class Client:
        def generate_presigned_url(self, operation, **kwargs):
            assert operation == "get_object" and kwargs["ExpiresIn"] == 300
            assert kwargs["Params"] == {"Bucket": "private", "Key": "runtime.zip"}
            return "https://private.test/signed"

        def get_object(self, **kwargs):
            return {"Body": io.BytesIO(content)}

    downloads = R2Downloads(
        Client(),
        "private",
        Release("1", "runtime.zip", "a" * 64, 100),
        Release("1", "setup.exe", hashlib.sha256(content).hexdigest(), len(content)),
    )
    assert downloads.manifest()["url"] == "https://private.test/signed"
    assert downloads.personalized("https://app.test", "gm_activation_" + "x" * 48).startswith(
        content
    )


def test_unconfigured_public_router_is_available_and_paid_routes_fail_closed():
    app = FastAPI()
    app.include_router(build_cloud_router())
    with TestClient(app, base_url="https://app.test") as client:
        assert client.get("/api/cloud/config").json()["oauth_ready"] is False
        assert client.get("/api/cloud/auth/google/start").status_code == 503
        assert client.post("/api/cloud/activate", json={"credential": "x" * 40}).status_code == 503


def test_routes_auth_separation_and_sanitized_credential_validation(state):
    service = CloudService(state.store, lambda adapter: None)
    app = FastAPI()
    app.include_router(build_cloud_router(service, public_origin="https://app.test"))
    credential = state.store.create_credential(state.account)
    web = state.store.create_session(state.account)
    with TestClient(app, base_url="https://app.test") as client:
        assert client.get("/api/cloud/account").status_code == 401
        result = client.post("/api/cloud/activate", json={"credential": credential})
        assert result.status_code == 200
        api = result.json()["access_token"]
        assert (
            client.get("/api/cloud/account", headers={"Authorization": "Bearer " + api}).status_code
            == 200
        )
        assert (
            client.get("/api/cloud/me", headers={"Authorization": "Bearer " + api}).status_code
            == 401
        )
        client.cookies.set(SESSION_COOKIE, web["access_token"])
        assert client.get("/api/cloud/me").status_code == 200
        response = client.post("/api/cloud/logout", headers={"Origin": "https://evil.test"})
        assert response.status_code == 403
        response = client.post("/api/cloud/activate", json={"credential": "secret"})
        assert response.status_code == 422 and "secret" not in response.text
        huge = {"request_id": str(uuid4()), "messages": [{"role": "user", "content": "x" * 256000}]}
        assert (
            client.post(
                "/api/cloud/inference", json=huge, headers={"Authorization": "Bearer " + api}
            ).status_code
            == 413
        )


def test_production_transaction_uses_snapshot_majority_and_bounded_commit():
    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def with_transaction(self, callback, **kwargs):
            assert kwargs["read_concern"].document == {"level": "snapshot"}
            assert kwargs["write_concern"].document == {"w": "majority", "wtimeout": 5000}
            assert kwargs["max_commit_time_ms"] == 10000
            return callback(self)

    database = mongomock.MongoClient().db
    store = MongoCloudStore(database)
    store.database = SimpleNamespace(client=SimpleNamespace(start_session=Session))
    assert store._transaction(lambda session: session is not None)


def test_adapter_rejects_global_limit_and_double_dispatch(state):
    adapter = AccountBudgetStore(state.store, state.account, str(uuid4()), "digest")
    with pytest.raises(CloudError, match="invalid_dispatch"):
        adapter.reserve(100, 250000)
    assert state.store.account(state.account)["reserved"] == 0
    assert adapter.reserve(100, ACCOUNT_ALLOWANCE)
    with pytest.raises(CloudError, match="invalid_dispatch"):
        adapter.reserve(100, ACCOUNT_ALLOWANCE)
    assert state.store.account(state.account)["reserved"] == 100


def test_owner_adjusted_allowance_is_server_owned_and_checked_atomically(state):
    state.store.accounts.update_one({"_id": state.account}, {"$set": {"allowance": 2_000_000}})
    adapter = AccountBudgetStore(state.store, state.account, str(uuid4()), "digest")
    assert adapter.budget_microdollars == 2_000_000
    assert adapter.reserve(100, 2_000_000)
    assert state.store.ensure_account("verified-google-subject")["allowance"] == 2_000_000
    # A reduction after the adapter's snapshot must still be authoritative at admission.
    lowered = AccountBudgetStore(state.store, state.account, str(uuid4()), "digest-2")
    state.store.accounts.update_one({"_id": state.account}, {"$set": {"allowance": 150}})
    with pytest.raises(CloudError, match="account_budget_exhausted"):
        lowered.reserve(51, 2_000_000)
    assert state.store.account(state.account)["reserved"] == 100


def fake_tinker_provider_factory(state, monkeypatch, *, count=100, barrier=None):
    from guardmate.agent.provider import TinkerProvider
    from guardmate.cloud.gateway import default_provider_factory

    sdk = ModuleType("tinker")
    sdk.types = SimpleNamespace(
        ModelInput=SimpleNamespace(from_ints=lambda tokens: tokens),
        SamplingParams=lambda **kwargs: SimpleNamespace(**kwargs),
    )
    retry = ModuleType("tinker.lib.retry_handler")
    retry.RetryConfig = lambda **kwargs: SimpleNamespace(**kwargs)
    monkeypatch.setitem(sys.modules, "tinker", sdk)
    monkeypatch.setitem(sys.modules, "tinker.lib.retry_handler", retry)
    samples = []

    class Tokenizer:
        def apply_chat_template(self, messages, **kwargs):
            assert kwargs["enable_thinking"] is False and kwargs["return_dict"] is False
            if barrier is not None:
                barrier.wait(timeout=5)
            return [1] * count

        def decode(self, tokens, **kwargs):
            return '{"action":"answer"}'

    class Sampler:
        def sample(self, **kwargs):
            assert kwargs["num_samples"] == 1
            assert kwargs["sampling_params"].max_tokens == 512
            samples.append(kwargs)
            return SimpleNamespace(
                result=lambda timeout: SimpleNamespace(sequences=[SimpleNamespace(tokens=[1])])
            )

    def factory(adapter):
        provider = default_provider_factory(adapter)
        assert isinstance(provider, TinkerProvider)
        assert provider.budget_microdollars == adapter.budget_microdollars
        monkeypatch.setattr(provider, "status", lambda: SimpleNamespace(configured=True))
        provider._client, provider._tokenizer = Sampler(), Tokenizer()
        return provider

    return factory, samples


def test_real_tinker_entry_uses_account_adapter_and_never_charges_legacy_twice(state, monkeypatch):
    state.database.model_budget.insert_one({"_id": 1, "reserved": 155565})
    factory, samples = fake_tinker_provider_factory(state, monkeypatch)
    service = CloudService(state.store, factory)
    body = request()
    result = service.infer(state.account, body)
    assert result["plan"]["action"] == "answer"
    assert result["reserved_microdollars"] == 548
    assert state.database.model_budget.find_one({"_id": 1})["reserved"] == 155565
    with pytest.raises(CloudError, match="request_already_completed"):
        service.infer(state.account, body)
    assert len(samples) == 1


def test_racing_real_provider_tokenization_produces_one_sample(state, monkeypatch):
    factory, samples = fake_tinker_provider_factory(
        state, monkeypatch, barrier=threading.Barrier(2)
    )
    service, body = CloudService(state.store, factory, max_concurrent=2), request()

    def invoke(_):
        try:
            service.infer(state.account, body)
            return True
        except CloudError as error:
            assert error.status_code == 409
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        result = list(executor.map(invoke, range(2)))
    assert sum(result) == 1 and len(samples) == 1
    assert state.store.account(state.account)["reserved"] == 548


def test_real_token_limit_prevents_reservation_and_model_sample(state, monkeypatch):
    factory, samples = fake_tinker_provider_factory(state, monkeypatch, count=12001)
    with pytest.raises(CloudError, match="inference_unavailable"):
        CloudService(state.store, factory).infer(state.account, request())
    assert samples == []
    assert state.store.account(state.account)["reserved"] == 0
    assert state.store.requests.count_documents({}) == 0


def test_unknown_commit_outcome_retains_claim_and_cannot_dispatch_twice(state):
    transactions = state.transactions

    def uncertain(callback):
        transactions(callback)
        raise AutoReconnect("private connection metadata")

    state.store._transaction_runner = uncertain
    calls = []

    class Provider:
        def __init__(self, adapter):
            self.adapter = adapter

        def generate(self, messages):
            self.adapter.reserve(100, ACCOUNT_ALLOWANCE)
            calls.append(True)
            return AgentPlan(action="answer")

    body = request()
    with pytest.raises(CloudError, match="inference_unavailable"):
        CloudService(state.store, Provider).infer(state.account, body)
    assert state.store.account(state.account)["reserved"] == 100
    assert calls == []
    with pytest.raises(CloudError, match="request_outcome_uncertain"):
        CloudService(state.store, Provider).infer(state.account, body)


def test_explicit_legacy_migration_dry_run_and_repeated_apply_preserve_source(state):
    state.database.model_budget.insert_one({"_id": 1, "reserved": 155565})
    preview = migrate(state.store, state.account)
    assert preview["applied"] is False
    assert preview["additional_opening_microdollars"] == 155565
    assert state.store.account(state.account)["reserved"] == 0
    for _ in range(2):
        migrate(state.store, state.account, apply=True)
    assert state.store.account(state.account)["reserved"] == 155565
    assert state.database.model_budget.find_one({"_id": 1})["reserved"] == 155565
    state.database.model_budget.update_one({"_id": 1}, {"$inc": {"reserved": 100}})
    with pytest.raises(ValueError, match="manual reconciliation"):
        migrate(state.store, state.account, apply=True)
