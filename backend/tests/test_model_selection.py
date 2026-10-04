"""Checkpoint role-play selection and status contracts; no hosted requests or .env reads."""

import importlib
import sys
from datetime import UTC, datetime
from types import ModuleType, SimpleNamespace

import dotenv
import pytest
from fastapi.testclient import TestClient
from guardmate.agent.models import AgentEvent, AgentPlan, ModelIdentity, ModelStatus
from guardmate.agent.provider import MODEL, ModelUnavailable, TinkerProvider
from guardmate.agent.store import ConversationStore
from pydantic import ValidationError

CHECKPOINT = "tinker://session-123:train:0/sampler_weights/guardmate-final"
SECRET = "PRIVATE_CHECKPOINT_OR_SDK_VALUE_MUST_NOT_ESCAPE"


@pytest.fixture
def app_module(monkeypatch, tmp_path):
    # Importing main initializes its default application. Keep even that initialization
    # in a temporary directory and suppress dotenv before the first import.
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: False)
    monkeypatch.delenv("GUARDMATE_SAMPLER_CHECKPOINT", raising=False)
    monkeypatch.delenv("TINKER_API_KEY", raising=False)
    monkeypatch.setenv("GUARDMATE_DATA_DIR", str(tmp_path / "import-only"))
    module = importlib.import_module("guardmate.main")
    monkeypatch.setattr(module, "load_dotenv", lambda *args, **kwargs: False)
    return module


def test_identity_defaults_preserve_unspecified_test_or_legacy_provider():
    identity = ModelIdentity(model="legacy", provider="legacy")
    assert identity.model_dump() == {
        "model": "legacy",
        "provider": "legacy",
        "target_kind": "unspecified",
        "sampler_checkpoint": None,
        "checkpoint_verified": False,
    }


@pytest.mark.parametrize("missing", ["model", "provider"])
def test_identity_requires_model_and_provider(missing):
    fields = {"model": MODEL, "provider": "test"}
    fields.pop(missing)
    with pytest.raises(ValidationError):
        ModelIdentity.model_validate(fields)


def test_model_status_inherits_identity_without_breaking_legacy_constructor():
    status = ModelStatus(
        configured=True,
        model="scripted-test",
        provider="offline",
        message="No hosted request.",
        reserved_usd=0,
        budget_usd=0,
    )
    assert isinstance(status, ModelIdentity)
    assert status.target_kind == "unspecified"
    assert status.sampler_checkpoint is None
    assert status.checkpoint_verified is False
    assert status.voice_connected is False


def test_legacy_event_is_not_relabelled_as_current_model():
    event = AgentEvent.model_validate(
        {
            "action": "clarify",
            "detail": "Historical event without model provenance.",
            "at": "2026-10-03T06:00:00+00:00",
            "model_action": "clarify",
        }
    )
    assert event.model_identity is None
    assert event.model_result is None
    restored = AgentEvent.model_validate_json(event.model_dump_json())
    assert restored.model_identity is None
    assert restored.model_result is None


@pytest.mark.parametrize("result", ["plan_returned", "unavailable"])
def test_event_serializes_explicit_model_identity_and_attempt_result(result):
    identity = ModelIdentity(
        model=MODEL,
        provider="Tinker (hosted open-weight model)",
        target_kind="tuned",
        sampler_checkpoint=CHECKPOINT,
        checkpoint_verified=True,
    )
    event = AgentEvent(
        action="clarify",
        detail="Checked application action.",
        at=datetime(2026, 10, 4, 6, tzinfo=UTC),
        model_identity=identity,
        model_result=result,
    )
    restored = AgentEvent.model_validate_json(event.model_dump_json())
    assert restored.model_identity == identity
    assert restored.model_result == result


@pytest.mark.parametrize("target", ["base", "tuned", "unspecified"])
def test_identity_accepts_only_declared_target_kinds(target):
    assert ModelIdentity(model=MODEL, provider="test", target_kind=target).target_kind == target


def test_unknown_target_and_result_values_are_rejected():
    with pytest.raises(ValidationError):
        ModelIdentity(model=MODEL, provider="test", target_kind="approved")
    with pytest.raises(ValidationError):
        AgentEvent(
            action="clarify",
            detail="test",
            at=datetime(2026, 10, 4, tzinfo=UTC),
            model_result="improved",
        )


@pytest.mark.parametrize("value", [None, ""])
def test_unset_or_empty_checkpoint_environment_keeps_explicit_base(
    app_module, monkeypatch, tmp_path, value
):
    if value is not None:
        monkeypatch.setenv("GUARDMATE_SAMPLER_CHECKPOINT", value)
    instances = []

    class TrackedProvider(TinkerProvider):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            instances.append(self)

    monkeypatch.setattr(app_module, "TinkerProvider", TrackedProvider)
    app = app_module.create_app(tmp_path / "data")
    assert len(instances) == 1
    assert instances[0].sampler_checkpoint is None
    assert instances[0]._client is instances[0]._tokenizer is None
    with TestClient(app) as client:
        status = client.get("/api/agent/status").json()
    assert status["target_kind"] == "base"
    assert status["model"] == MODEL
    assert status["sampler_checkpoint"] is None
    assert status["checkpoint_verified"] is False
    assert status["configured"] is False


def test_canonical_environment_selects_checkpoint_but_does_not_verify_eagerly(
    app_module, monkeypatch, tmp_path
):
    monkeypatch.setenv("GUARDMATE_SAMPLER_CHECKPOINT", CHECKPOINT)
    instances = []

    class TrackedProvider(TinkerProvider):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            instances.append(self)

    monkeypatch.setattr(app_module, "TinkerProvider", TrackedProvider)
    app = app_module.create_app(tmp_path / "data")
    assert instances[0].sampler_checkpoint == CHECKPOINT
    assert instances[0]._client is instances[0]._tokenizer is None
    with TestClient(app) as client:
        status = client.get("/api/agent/status").json()
    assert status["target_kind"] == "tuned"
    assert status["sampler_checkpoint"] == CHECKPOINT
    assert status["checkpoint_verified"] is False
    assert "not yet verified" in status["message"]
    assert "identity only, not improvement or safety" in status["message"]
    assert instances[0]._client is None


@pytest.mark.parametrize(
    "value",
    [
        " ",
        "\n",
        " " + CHECKPOINT,
        CHECKPOINT + " ",
        "tinker://session-123:train:0/weights/full-state",
        "tinker://session-123:train:0/sampler_weights/../final",
        CHECKPOINT + "?secret=" + SECRET,
        CHECKPOINT + "#" + SECRET,
    ],
)
def test_invalid_nonempty_environment_fails_closed_without_base_fallback_or_sdk(
    app_module, monkeypatch, tmp_path, value
):
    monkeypatch.setenv("GUARDMATE_SAMPLER_CHECKPOINT", value)
    attempts = []

    class TrackedProvider(TinkerProvider):
        def __init__(self, *args, **kwargs):
            attempts.append(kwargs)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(app_module, "TinkerProvider", TrackedProvider)
    with pytest.raises(ValueError, match="canonical") as error:
        app_module.create_app(tmp_path / "data")
    assert attempts == [{"sampler_checkpoint": value}]
    assert SECRET not in str(error.value)


@pytest.mark.parametrize("checkpoint", [None, CHECKPOINT])
def test_create_app_and_health_do_not_call_model_status_or_initialize_sdk(
    app_module, monkeypatch, tmp_path, checkpoint
):
    if checkpoint:
        monkeypatch.setenv("GUARDMATE_SAMPLER_CHECKPOINT", checkpoint)

    def forbidden(*args, **kwargs):
        raise AssertionError("Application creation must not initialize or sample a hosted model.")

    monkeypatch.setattr(TinkerProvider, "status", forbidden)
    sdk = ModuleType("tinker")
    sdk.ServiceClient = forbidden
    monkeypatch.setitem(sys.modules, "tinker", sdk)
    app = app_module.create_app(tmp_path / "data")
    with TestClient(app) as client:
        assert client.get("/api/health").json()["status"] == "ok"
    assert ConversationStore(tmp_path / "data").reserved_microdollars() == 0


class InjectedProvider:
    def __init__(self):
        self.status_calls = 0
        self.generate_calls = 0

    def __bool__(self):
        return False  # Dependency injection must not use truthiness to pick a production provider.

    def status(self):
        self.status_calls += 1
        return ModelStatus(
            configured=True,
            model="injected-offline-provider",
            provider="test-double",
            message="No hosted model.",
            reserved_usd=0,
            budget_usd=0,
        )

    def generate(self, messages):
        self.generate_calls += 1
        return AgentPlan(action="clarify", question="prepaid")


@pytest.mark.parametrize("value", [CHECKPOINT, "invalid-" + SECRET])
def test_injected_provider_bypasses_checkpoint_environment_even_when_falsey(
    app_module, monkeypatch, tmp_path, value
):
    monkeypatch.setenv("GUARDMATE_SAMPLER_CHECKPOINT", value)

    def forbidden(*args, **kwargs):
        raise AssertionError("An injected provider must bypass production checkpoint selection.")

    monkeypatch.setattr(app_module, "TinkerProvider", forbidden)
    injected = InjectedProvider()
    app = app_module.create_app(tmp_path / "data", provider=injected)
    assert injected.status_calls == injected.generate_calls == 0
    with TestClient(app) as client:
        status = client.get("/api/agent/status").json()
    assert injected.status_calls == 1
    assert status["model"] == "injected-offline-provider"
    assert status["target_kind"] == "unspecified"
    assert status["sampler_checkpoint"] is None
    assert status["checkpoint_verified"] is False
    assert injected.generate_calls == 0


@pytest.fixture
def fake_sdk(monkeypatch):
    events = []

    class Tokenizer:
        def apply_chat_template(self, messages, **kwargs):
            events.append("tokenize")
            return [1] * 100

        def decode(self, tokens, **kwargs):
            return '{"action":"clarify","question":"prepaid"}'

    class Sampler:
        base_model = MODEL

        def get_base_model(self):
            events.append("metadata")
            return self.base_model

        def get_tokenizer(self):
            events.append("tokenizer")
            return Tokenizer()

        def sample(self, **kwargs):
            events.append("sample")
            return self

        def result(self, timeout):
            assert timeout == 45
            return SimpleNamespace(sequences=[SimpleNamespace(tokens=[1])])

    sampler = Sampler()
    create_calls = []

    class ServiceClient:
        def __init__(self):
            events.append("service")

        def create_sampling_client(self, **kwargs):
            create_calls.append(kwargs)
            return sampler

    class Options:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    sdk = ModuleType("tinker")
    sdk.ServiceClient = ServiceClient
    sdk.types = SimpleNamespace(
        ModelInput=SimpleNamespace(from_ints=lambda tokens: tokens), SamplingParams=Options
    )
    retry = ModuleType("tinker.lib.retry_handler")
    retry.RetryConfig = Options
    monkeypatch.setitem(sys.modules, "tinker", sdk)
    monkeypatch.setitem(sys.modules, "tinker.lib.retry_handler", retry)
    monkeypatch.setattr("guardmate.agent.provider.importlib.util.find_spec", lambda package: True)
    monkeypatch.setenv("TINKER_API_KEY", "offline-test-placeholder-not-a-key")
    return SimpleNamespace(sampler=sampler, events=events, create_calls=create_calls)


def test_tuned_status_verification_flag_changes_only_after_matching_sdk_metadata(
    tmp_path, fake_sdk
):
    provider = TinkerProvider(ConversationStore(tmp_path), sampler_checkpoint=CHECKPOINT)
    before = provider.status()
    assert before.target_kind == "tuned"
    assert before.sampler_checkpoint == CHECKPOINT
    assert before.checkpoint_verified is False
    assert fake_sdk.events == []
    assert provider.generate([]).action == "clarify"
    after = provider.status()
    assert before.checkpoint_verified is False  # A previous status remains a historical value.
    assert after.checkpoint_verified is True
    assert after.target_kind == "tuned"
    assert after.sampler_checkpoint == CHECKPOINT
    assert "base model verified" in after.message
    assert "not improvement or safety" in after.message
    assert fake_sdk.events == ["service", "metadata", "tokenizer", "tokenize", "sample"]
    assert fake_sdk.create_calls[0]["model_path"] == CHECKPOINT
    assert "base_model" not in fake_sdk.create_calls[0]


@pytest.mark.parametrize("metadata", [None, "other/model", MODEL + ":variant"])
def test_wrong_metadata_keeps_tuned_status_unverified_without_paid_sample(
    tmp_path, fake_sdk, metadata
):
    fake_sdk.sampler.base_model = metadata
    provider = TinkerProvider(ConversationStore(tmp_path), sampler_checkpoint=CHECKPOINT)
    with pytest.raises(ModelUnavailable, match="could not be verified"):
        provider.generate([])
    assert provider.status().checkpoint_verified is False
    assert provider.status().target_kind == "tuned"
    assert provider.status().sampler_checkpoint == CHECKPOINT
    assert fake_sdk.events == ["service", "metadata"]
    assert provider.store.reserved_microdollars() == 0


def test_base_status_remains_base_and_does_not_claim_checkpoint_verification(tmp_path, fake_sdk):
    provider = TinkerProvider(ConversationStore(tmp_path))
    assert provider.status().target_kind == "base"
    provider.generate([])
    status = provider.status()
    assert status.target_kind == "base"
    assert status.sampler_checkpoint is None
    assert status.checkpoint_verified is False
    assert "metadata" not in fake_sdk.events
    assert fake_sdk.create_calls[0]["base_model"] == MODEL
    assert "model_path" not in fake_sdk.create_calls[0]
