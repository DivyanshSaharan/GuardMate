import sys
from types import ModuleType, SimpleNamespace

import pytest
from guardmate.agent.provider import (
    BUDGET_MICRODOLLARS,
    MAX_OUTPUT_TOKENS,
    MODEL,
    ModelUnavailable,
    TinkerProvider,
    validate_sampler_checkpoint,
)
from guardmate.agent.store import ConversationStore

CHECKPOINT = "tinker://18ed09de-0c90-4a99-b7c7-46b40c507312:train:0/sampler_weights/guardmate-final"
SECRET = "SENSITIVE_METADATA_OR_PROVIDER_EXCEPTION"


@pytest.mark.parametrize(
    "path",
    [
        CHECKPOINT,
        "tinker://18ed09de-0c90-4a99-b7c7-46b40c507312/sampler_weights/final_001",
        "tinker://model_123/sampler_weights/checkpoint-001.v2",
        "tinker://session-123:train:12/sampler_weights/final",
        "tinker://run:443/sampler_weights/final",
    ],
)
def test_canonical_sampler_paths_preserve_exact_identity(path):
    assert validate_sampler_checkpoint(path) == path


@pytest.mark.parametrize(
    "path",
    [
        None,
        False,
        12,
        b"tinker://run/sampler_weights/final",
        "",
        " ",
        " " + CHECKPOINT,
        CHECKPOINT + " ",
        CHECKPOINT + "\n",
        CHECKPOINT + "\x00",
        CHECKPOINT + "\t",
        CHECKPOINT + "?base_model=Qwen/Qwen3.5-4B",
        CHECKPOINT + "#final",
        CHECKPOINT + "/",
        CHECKPOINT + "/nested",
        "TINKER://run/sampler_weights/final",
        "https://run/sampler_weights/final",
        "tinker://run/weights/final",
        "tinker://run/fullstate/final",
        "tinker://run/sampler/final",
        "tinker://run/sampler_weights/",
        "tinker:///sampler_weights/final",
        "tinker://../sampler_weights/final",
        "tinker://run/sampler_weights/..",
        "tinker://run/sampler_weights/../final",
        "tinker://run/sampler_weights/foo..bar",
        "tinker://run/sampler_weights/%2e%2e",
        "tinker://run/sampler_weights/final%2Fother",
        "tinker://user@run/sampler_weights/final",
        "tinker://run:/sampler_weights/final",
        "tinker://:train:0/sampler_weights/final",
        "tinker://run::train:0/sampler_weights/final",
        "tinker://run:train:/sampler_weights/final",
        "tinker://run\\sampler_weights\\final",
        "tinker://rún/sampler_weights/final",
        "tinker://run/sampler_weights/finaⅼ",
        "tinker://" + "x" * 201 + "/sampler_weights/final",
        "tinker://" + "x" * 195 + ":train:0/sampler_weights/final",
        "tinker://run/sampler_weights/" + "x" * 201,
    ],
)
def test_invalid_sampler_paths_are_rejected_without_echoing_input(path):
    with pytest.raises(ValueError, match="canonical") as error:
        validate_sampler_checkpoint(path)
    assert str(error.value) == (
        "Checkpoint must be a canonical tinker://model-id/sampler_weights/checkpoint-name path."
    )


class FakeTokenizer:
    def __init__(self, events):
        self.events = events
        self.template_kwargs = None

    def apply_chat_template(self, messages, **kwargs):
        self.events.append("tokenize")
        self.template_kwargs = kwargs
        return [1] * 100

    def decode(self, tokens, **kwargs):
        return '{"action":"answer","topic":"availability"}'


class FakeSampler:
    def __init__(self, events):
        self.events = events
        self.base_model = MODEL
        self.metadata_error = None
        self.sample_error = None
        self.sample_calls = []
        self.tokenizer = FakeTokenizer(events)

    def get_base_model(self):
        self.events.append("metadata")
        if self.metadata_error:
            raise self.metadata_error
        return self.base_model

    def get_tokenizer(self):
        self.events.append("tokenizer")
        return self.tokenizer

    def sample(self, **kwargs):
        self.events.append("sample")
        self.sample_calls.append(kwargs)
        return self

    def result(self, timeout):
        assert timeout == 45
        if self.sample_error:
            raise self.sample_error
        return SimpleNamespace(sequences=[SimpleNamespace(tokens=[1, 2])])


@pytest.fixture
def fake_sdk(monkeypatch):
    """No SDK service, metadata network request, tokenizer download or paid sample runs."""
    events = []
    sampler = FakeSampler(events)
    create_calls = []
    service_calls = []

    class ServiceClient:
        def __init__(self):
            service_calls.append(True)

        def create_sampling_client(self, **kwargs):
            create_calls.append(kwargs)
            events.append("create")
            return sampler

    class RetryConfig:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class SamplingParams:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    sdk = ModuleType("tinker")
    sdk.ServiceClient = ServiceClient
    sdk.types = SimpleNamespace(
        ModelInput=SimpleNamespace(from_ints=lambda tokens: tokens),
        SamplingParams=SamplingParams,
    )
    retry_module = ModuleType("tinker.lib.retry_handler")
    retry_module.RetryConfig = RetryConfig
    monkeypatch.setitem(sys.modules, "tinker", sdk)
    monkeypatch.setitem(sys.modules, "tinker.lib.retry_handler", retry_module)
    monkeypatch.setattr(TinkerProvider, "status", lambda self: SimpleNamespace(configured=True))
    return SimpleNamespace(
        sampler=sampler,
        events=events,
        create_calls=create_calls,
        service_calls=service_calls,
    )


def test_checkpoint_constructor_validates_before_any_sdk_call(tmp_path, fake_sdk):
    with pytest.raises(ValueError):
        TinkerProvider(ConversationStore(tmp_path), sampler_checkpoint="tinker://run/weights/full")
    assert fake_sdk.service_calls == []


def test_checkpoint_is_keyword_only_and_cannot_be_reassigned(tmp_path):
    store = ConversationStore(tmp_path)
    with pytest.raises(TypeError):
        TinkerProvider(store, CHECKPOINT)
    provider = TinkerProvider(store, sampler_checkpoint=CHECKPOINT)
    with pytest.raises(AttributeError):
        provider.sampler_checkpoint = "tinker://different/sampler_weights/final"
    assert provider.sampler_checkpoint == CHECKPOINT


def test_checkpoint_metadata_is_verified_before_tokenization_or_paid_sample(tmp_path, fake_sdk):
    provider = TinkerProvider(ConversationStore(tmp_path), sampler_checkpoint=CHECKPOINT)
    assert provider.generate([{"role": "user", "content": "Hello"}]).action == "answer"
    assert fake_sdk.events == ["create", "metadata", "tokenizer", "tokenize", "sample"]
    assert fake_sdk.create_calls[0]["model_path"] == CHECKPOINT
    assert "base_model" not in fake_sdk.create_calls[0]
    config = fake_sdk.create_calls[0]["retry_config"]
    assert config.enable_retry_logic is False
    assert config.progress_timeout == 45
    assert provider.store.reserved_microdollars() > 0
    params = fake_sdk.sampler.sample_calls[0]["sampling_params"]
    assert params.max_tokens == MAX_OUTPUT_TOKENS
    assert params.temperature == 0.2
    assert params.stop == ["<|im_end|>"]
    assert fake_sdk.sampler.tokenizer.template_kwargs == {
        "tokenize": True,
        "return_dict": False,
        "add_generation_prompt": True,
        "enable_thinking": False,
    }
    provider.generate([])
    assert fake_sdk.events.count("metadata") == 1
    assert len(fake_sdk.create_calls) == 1
    assert len(fake_sdk.sampler.sample_calls) == 2


@pytest.mark.parametrize(
    "base_model", [None, "", "other/model", MODEL + ":variant", {"name": MODEL}]
)
def test_missing_or_wrong_base_model_fails_closed_without_fallback(tmp_path, fake_sdk, base_model):
    fake_sdk.sampler.base_model = base_model
    provider = TinkerProvider(ConversationStore(tmp_path), sampler_checkpoint=CHECKPOINT)
    for _ in range(2):
        with pytest.raises(ModelUnavailable, match="base model could not be verified"):
            provider.generate([])
    assert fake_sdk.events == ["create", "metadata", "metadata"]
    assert len(fake_sdk.create_calls) == 1
    assert "base_model" not in fake_sdk.create_calls[0]
    assert provider.store.reserved_microdollars() == 0
    assert fake_sdk.sampler.sample_calls == []
    assert provider._checkpoint_verified is False


def test_missing_metadata_sdk_method_fails_closed(tmp_path, fake_sdk, monkeypatch):
    monkeypatch.delattr(FakeSampler, "get_base_model")
    provider = TinkerProvider(ConversationStore(tmp_path), sampler_checkpoint=CHECKPOINT)
    with pytest.raises(ModelUnavailable, match="AttributeError"):
        provider.generate([])
    assert fake_sdk.events == ["create"]
    assert provider.store.reserved_microdollars() == 0
    assert fake_sdk.sampler.sample_calls == []


def test_metadata_exception_is_sanitized_and_never_falls_back(tmp_path, fake_sdk):
    fake_sdk.sampler.metadata_error = RuntimeError(SECRET)
    provider = TinkerProvider(ConversationStore(tmp_path), sampler_checkpoint=CHECKPOINT)
    with pytest.raises(ModelUnavailable) as error:
        provider.generate([])
    assert SECRET not in str(error.value)
    assert fake_sdk.events == ["create", "metadata"]
    assert len(fake_sdk.create_calls) == 1
    assert provider.store.reserved_microdollars() == 0
    assert fake_sdk.sampler.sample_calls == []


def test_checkpoint_sample_failure_stays_reserved_without_app_retry(tmp_path, fake_sdk):
    fake_sdk.sampler.sample_error = RuntimeError(SECRET)
    provider = TinkerProvider(ConversationStore(tmp_path), sampler_checkpoint=CHECKPOINT)
    with pytest.raises(ModelUnavailable) as error:
        provider.generate([])
    assert SECRET not in str(error.value)
    assert len(fake_sdk.sampler.sample_calls) == 1
    assert provider.store.reserved_microdollars() > 0
    assert "base_model" not in fake_sdk.create_calls[0]


def test_checkpoint_uses_existing_persistent_inference_budget(tmp_path, fake_sdk):
    store = ConversationStore(tmp_path)
    assert store.reserve(BUDGET_MICRODOLLARS, BUDGET_MICRODOLLARS)
    provider = TinkerProvider(ConversationStore(tmp_path), sampler_checkpoint=CHECKPOINT)
    with pytest.raises(ModelUnavailable, match="budget"):
        provider.generate([])
    assert fake_sdk.sampler.sample_calls == []
    assert provider.store.reserved_microdollars() == BUDGET_MICRODOLLARS


def test_base_provider_keeps_base_model_constructor_without_metadata_lookup(tmp_path, fake_sdk):
    provider = TinkerProvider(ConversationStore(tmp_path))
    provider.generate([])
    assert provider.sampler_checkpoint is None
    assert fake_sdk.create_calls[0]["base_model"] == MODEL
    assert "model_path" not in fake_sdk.create_calls[0]
    assert fake_sdk.events == ["create", "tokenizer", "tokenize", "sample"]


def test_checkpoint_status_keeps_model_identity_and_truthful_verification(tmp_path, monkeypatch):
    monkeypatch.setattr("guardmate.agent.provider.importlib.util.find_spec", lambda package: True)
    monkeypatch.setenv("TINKER_API_KEY", "test-placeholder-not-a-real-key")
    provider = TinkerProvider(ConversationStore(tmp_path), sampler_checkpoint=CHECKPOINT)
    status = provider.status()
    assert status.model == MODEL
    assert CHECKPOINT in status.message
    assert "not yet verified" in status.message
    assert status.configured is True
    provider._checkpoint_verified = True
    assert "base model verified" in provider.status().message
    monkeypatch.delenv("TINKER_API_KEY")
    assert provider.status().configured is False
    assert CHECKPOINT in provider.status().message


def test_base_status_has_no_checkpoint_claim(tmp_path, monkeypatch):
    monkeypatch.setattr("guardmate.agent.provider.importlib.util.find_spec", lambda package: True)
    monkeypatch.setenv("TINKER_API_KEY", "test-placeholder-not-a-real-key")
    status = TinkerProvider(ConversationStore(tmp_path)).status()
    assert status.model == MODEL
    assert "checkpoint" not in status.message.lower()
