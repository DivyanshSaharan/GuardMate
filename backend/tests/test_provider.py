from types import SimpleNamespace

import pytest
from guardmate.agent.provider import (
    BUDGET_MICRODOLLARS,
    MAX_OUTPUT_TOKENS,
    ModelUnavailable,
    TinkerProvider,
)
from guardmate.agent.store import ConversationStore

pytest.importorskip("tinker")


class FakeTokenizer:
    def __init__(self, output='{"action":"answer","topic":"availability"}', count=100):
        self.output, self.count, self.kwargs = output, count, None

    def apply_chat_template(self, messages, **kwargs):
        self.kwargs = kwargs
        return [1] * self.count

    def decode(self, tokens, **kwargs):
        return self.output


class FakeSampler:
    def __init__(self, fail=False):
        self.fail, self.calls = fail, []

    def sample(self, **kwargs):
        self.calls.append(kwargs)
        return self

    def result(self, timeout):
        assert timeout == 45
        if self.fail:
            raise RuntimeError("SENSITIVE_PROVIDER_EXCEPTION_MUST_NOT_ESCAPE")
        return SimpleNamespace(sequences=[SimpleNamespace(tokens=[1, 2])])


def provider_at(tmp_path, monkeypatch, *, fail=False, output=None, count=100):
    monkeypatch.setenv("TINKER_API_KEY", "test-placeholder-not-a-real-key")
    provider = TinkerProvider(ConversationStore(tmp_path))
    monkeypatch.setattr(provider, "status", lambda: SimpleNamespace(configured=True))
    provider._client = FakeSampler(fail)
    provider._tokenizer = (
        FakeTokenizer(count=count) if output is None else FakeTokenizer(output, count)
    )
    return provider


def test_adapter_explicitly_requests_token_list_and_disables_thinking(tmp_path, monkeypatch):
    provider = provider_at(tmp_path, monkeypatch)
    plan = provider.generate([{"role": "user", "content": "Hello"}])
    assert plan.action == "answer"
    assert provider._tokenizer.kwargs["return_dict"] is False
    assert provider._tokenizer.kwargs["enable_thinking"] is False
    assert provider._client.calls[0]["num_samples"] == 1
    assert provider._client.calls[0]["sampling_params"].max_tokens == MAX_OUTPUT_TOKENS
    assert provider.store.reserved_microdollars() > 0


def test_failed_samples_stay_reserved_without_exposing_exception(tmp_path, monkeypatch):
    provider = provider_at(tmp_path, monkeypatch, fail=True)
    with pytest.raises(ModelUnavailable) as error:
        provider.generate([])
    assert "SENSITIVE_PROVIDER" not in str(error.value)
    assert provider.store.reserved_microdollars() > 0
    assert len(provider._client.calls) == 1


def test_budget_exhaustion_does_not_submit_a_sample(tmp_path, monkeypatch):
    provider = provider_at(tmp_path, monkeypatch)
    provider.store.reserve(BUDGET_MICRODOLLARS, BUDGET_MICRODOLLARS)
    with pytest.raises(ModelUnavailable, match="budget"):
        provider.generate([])
    assert provider._client.calls == []


def test_oversized_prompt_is_rejected_before_reservation(tmp_path, monkeypatch):
    provider = provider_at(tmp_path, monkeypatch, count=12_001)
    with pytest.raises(ModelUnavailable, match="too long"):
        provider.generate([])
    assert provider.store.reserved_microdollars() == 0
    assert provider._client.calls == []


def test_invalid_model_plan_is_not_executed_or_automatically_retried(tmp_path, monkeypatch):
    provider = provider_at(tmp_path, monkeypatch, output='{"action":"grant_owner_approval"}')
    with pytest.raises(ModelUnavailable, match="invalid structured plan"):
        provider.generate([])
    assert len(provider._client.calls) == 1
    assert provider.validation_errors


def test_unsupported_observation_fields_are_not_silently_accepted(tmp_path, monkeypatch):
    provider = provider_at(
        tmp_path,
        monkeypatch,
        output='{"action":"answer","observation":{"availability":"at_pg"}}',
    )
    with pytest.raises(ModelUnavailable, match="invalid structured plan"):
        provider.generate([])
    assert len(provider._client.calls) == 1
    assert provider.validation_errors == [
        {"field": "('observation', 'availability')", "type": "extra_forbidden"}
    ]
