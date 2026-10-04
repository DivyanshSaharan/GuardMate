"""Offline report binding checks; test doubles are not evidence of hosted inference."""

import hashlib
import importlib.metadata
import json

import pytest
from guardmate.agent.provider import MODEL
from guardmate.evaluation import runner
from guardmate.evaluation.runner import (
    BoundedProvider,
    EvaluationBindingError,
    GoldPlanProvider,
    RecordingProvider,
    run_evaluation,
)
from guardmate.evaluation.schema import Scenario
from test_evaluation import FakeProvider, plans_for, seed

CORPUS_SHA256 = "a" * 64
CHECKPOINT = "tinker://fake:train:0/sampler_weights/final"


def scenario(identifier="sample-a", split="validation"):
    return Scenario.model_validate(seed(scenario_id=identifier, split=split))


def evaluate(selected, provider=None, mode="live-base-model", **kwargs):
    if provider is None:
        provider = FakeProvider([plan for item in selected for plan in plans_for(item)])
    return run_evaluation(
        selected, provider, mode, corpus_sha256=CORPUS_SHA256, split="validation", **kwargs
    )


@pytest.fixture
def stable_sources(monkeypatch):
    # Isolate these tests from other agents writing unrelated source in the shared tree.
    sources = dict.fromkeys(runner.EVALUATION_SOURCE_PATHS, "b" * 64)
    monkeypatch.setattr(runner, "_source_sha256", lambda: dict(sources))
    return sources


def test_explicit_binding_is_complete_and_preserves_legacy_fingerprints(stable_sources):
    selected = [scenario("sample-b"), scenario("sample-a")]
    report = evaluate(selected)
    binding = report["evaluation_binding"]
    assert binding["version"] == 1
    assert binding["corpus_sha256"] == CORPUS_SHA256
    assert binding["split"] == "validation"
    assert binding["selected_ids"] == ["sample-b", "sample-a"]
    canonical = json.dumps(
        [item.model_dump(mode="json") for item in selected],
        sort_keys=True,
        separators=(",", ":"),
    )
    assert binding["selected_scenarios_sha256"] == hashlib.sha256(canonical.encode()).hexdigest()
    assert binding["source_sha256"] == stable_sources
    assert binding["base_model"] == MODEL
    assert binding["temperature"] == 0.2
    assert binding["max_input_tokens"] == 12_000
    assert binding["max_output_tokens"] == 512
    assert binding["thinking"] is False
    assert binding["decoding"] == {
        "num_samples": 1,
        "stop": ["<|im_end|>"],
        "sampling_seed": None,
    }
    assert set(binding["dependency_versions"]) == {"tinker", "transformers", "jinja2", "pydantic"}
    assert binding["system_prompt_sha256"] == report["fingerprints"]["system_prompt_sha256"]
    assert binding["plan_schema_sha256"] == report["fingerprints"]["plan_schema_sha256"]
    assert set(report["fingerprints"]["source_sha256"]) == {
        "engine.py",
        "dialogue.py",
        "provider.py",
        "prompts.py",
    }
    assert report["target"] == {"kind": "base", "base_model": MODEL, "sampler_checkpoint": None}


def test_legacy_report_is_diagnostic_and_does_not_invent_corpus_hash():
    selected = [scenario()]
    report = run_evaluation(selected, FakeProvider(plans_for(selected[0])), "live-base-model")
    assert "evaluation_binding" not in report
    assert any("diagnostic" in line and "comparison" in line for line in report["limitations"])


@pytest.mark.parametrize(
    ("corpus", "split"),
    [
        (None, "validation"),
        (CORPUS_SHA256, None),
        ("", "validation"),
        ("a" * 63, "validation"),
        ("g" * 64, "validation"),
        (" " + CORPUS_SHA256, "validation"),
        (True, "validation"),
        (CORPUS_SHA256, "all"),
        (CORPUS_SHA256, "train"),
    ],
)
def test_partial_invalid_or_mismatched_binding_stops_before_generation(corpus, split):
    provider = FakeProvider([])
    with pytest.raises(EvaluationBindingError):
        run_evaluation([scenario()], provider, "live-base-model", corpus_sha256=corpus, split=split)
    assert provider.calls == []


def test_bound_replay_rejects_empty_or_duplicate_selection():
    for selected in ([], [scenario(), scenario()]):
        provider = FakeProvider([])
        with pytest.raises(EvaluationBindingError):
            evaluate(selected, provider)
        assert provider.calls == []


def test_corpus_digest_case_is_normalized(stable_sources):
    selected = [scenario()]
    report = run_evaluation(
        selected, GoldPlanProvider(), "oracle", corpus_sha256="A" * 64, split="validation"
    )
    assert report["evaluation_binding"]["corpus_sha256"] == CORPUS_SHA256
    assert report["target"]["kind"] == "oracle"
    assert "model_plan_rubric_match" not in report["summary"]


@pytest.mark.parametrize(
    "wrapper",
    [
        lambda item: item,
        RecordingProvider,
        lambda item: BoundedProvider(RecordingProvider(item), 2, 8950),
    ],
)
def test_tuned_target_is_explicit_even_through_wrappers(stable_sources, wrapper):
    selected = [scenario()]
    provider = FakeProvider(plans_for(selected[0]))
    provider.sampler_checkpoint = CHECKPOINT
    report = evaluate(selected, wrapper(provider), "live-tuned-model")
    assert report["mode"] == "live-tuned-model"
    assert report["target"] == {
        "kind": "tuned",
        "base_model": MODEL,
        "sampler_checkpoint": CHECKPOINT,
    }
    assert report["fingerprints"]["model"] == MODEL


@pytest.mark.parametrize(
    "checkpoint",
    [
        None,
        "",
        "not-a-checkpoint",
        "tinker://fake/weights/final",
        "tinker://fake/sampler_weights/../final",
        7,
    ],
)
def test_tuned_mode_requires_canonical_sampler_export(checkpoint):
    provider = FakeProvider([])
    provider.sampler_checkpoint = checkpoint
    with pytest.raises(ValueError):
        run_evaluation([scenario()], provider, "live-tuned-model")
    assert provider.calls == []


def test_sampler_export_cannot_masquerade_as_base():
    provider = FakeProvider([])
    provider.sampler_checkpoint = CHECKPOINT
    with pytest.raises(ValueError, match="labelled base-model"):
        run_evaluation([scenario()], provider, "live-base-model")
    assert provider.calls == []


@pytest.mark.parametrize("mode", ["live-base-model", "live-tuned-model"])
def test_wrapped_oracle_cannot_masquerade_as_live(mode):
    with pytest.raises(ValueError, match="Oracle"):
        run_evaluation([scenario()], RecordingProvider(GoldPlanProvider()), mode)


def test_source_hashes_cover_complete_agreed_environment(tmp_path, monkeypatch):
    expected = {
        "backend/guardmate/models.py",
        "backend/guardmate/context.py",
        "backend/guardmate/agent/models.py",
        "backend/guardmate/agent/prompts.py",
        "backend/guardmate/agent/engine.py",
        "backend/guardmate/agent/dialogue.py",
        "backend/guardmate/agent/provider.py",
        "backend/guardmate/agent/store.py",
        "backend/guardmate/evaluation/dataset.py",
        "backend/guardmate/evaluation/schema.py",
        "backend/guardmate/evaluation/runner.py",
        "backend/guardmate/evaluation/metrics.py",
        "backend/guardmate/evaluation/comparison.py",
        "backend/scripts/evaluate_delivery.py",
    }
    assert set(runner.EVALUATION_SOURCE_PATHS) == expected
    for name in expected:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(name.encode())
    monkeypatch.setattr(
        runner, "__file__", str(tmp_path / "backend/guardmate/evaluation/runner.py")
    )
    assert runner._source_sha256() == {
        name: hashlib.sha256(name.encode()).hexdigest() for name in expected
    }
    (tmp_path / "backend/guardmate/agent/store.py").unlink()
    with pytest.raises(EvaluationBindingError) as captured:
        runner._source_sha256()
    assert str(tmp_path) not in str(captured.value)


def test_source_fingerprint_failure_stops_before_generation(monkeypatch):
    def unavailable():
        raise EvaluationBindingError("Unavailable source snapshot.")

    monkeypatch.setattr(runner, "_source_sha256", unavailable)
    provider = FakeProvider([])
    with pytest.raises(EvaluationBindingError):
        evaluate([scenario()], provider)
    assert provider.calls == []


def test_dependency_versions_report_actual_metadata_and_absence(monkeypatch):
    def version(package):
        if package == "tinker":
            raise importlib.metadata.PackageNotFoundError(package)
        return f"offline-{package}"

    monkeypatch.setattr(runner.importlib.metadata, "version", version)
    assert runner._dependency_versions() == {
        "tinker": None,
        "transformers": "offline-transformers",
        "jinja2": "offline-jinja2",
        "pydantic": "offline-pydantic",
    }


@pytest.mark.parametrize("drift", ["source", "prompt", "annotations", "versions", "checkpoint"])
def test_drift_during_generation_never_emits_bound_report(stable_sources, monkeypatch, drift):
    selected = [scenario()]
    provider = FakeProvider(plans_for(selected[0]))
    mode = "live-base-model"
    if drift == "source":
        provider.callback = lambda: stable_sources.update(
            {"backend/guardmate/agent/engine.py": "c" * 64}
        )
    elif drift == "prompt":
        provider.callback = lambda: monkeypatch.setattr(runner, "SYSTEM_PROMPT", "changed prompt")
    elif drift == "annotations":
        provider.callback = lambda: setattr(selected[0], "rationale", "Changed scoring annotation.")
    elif drift == "versions":
        provider.callback = lambda: monkeypatch.setattr(
            runner,
            "_dependency_versions",
            lambda: dict.fromkeys(
                ("tinker", "transformers", "jinja2", "pydantic"), "changed-version"
            ),
        )
    else:
        mode = "live-tuned-model"
        provider.sampler_checkpoint = CHECKPOINT
        provider.callback = lambda: setattr(
            provider, "sampler_checkpoint", "tinker://fake/sampler_weights/other"
        )
    with pytest.raises(EvaluationBindingError, match="changed during replay"):
        evaluate(selected, provider, mode)
    assert provider.calls


def test_checkpoint_becoming_invalid_is_binding_error(stable_sources):
    selected = [scenario()]
    provider = FakeProvider(plans_for(selected[0]))
    provider.sampler_checkpoint = CHECKPOINT
    provider.callback = lambda: setattr(provider, "sampler_checkpoint", None)
    with pytest.raises(EvaluationBindingError, match="target changed"):
        evaluate(selected, provider, "live-tuned-model")


def test_legacy_replay_also_rejects_prompt_drift(monkeypatch):
    selected = [scenario()]
    provider = FakeProvider(plans_for(selected[0]))
    provider.callback = lambda: monkeypatch.setattr(runner, "SYSTEM_PROMPT", "changed prompt")
    with pytest.raises(EvaluationBindingError):
        run_evaluation(selected, provider, "live-base-model")
