"""All artifacts below are offline test doubles, not evidence of hosted model benefit."""

from copy import deepcopy

import pytest
from guardmate.agent.provider import MAX_INPUT_TOKENS, MAX_OUTPUT_TOKENS, MODEL
from guardmate.evaluation.comparison import (
    REQUIRED_SOURCE_PATHS,
    ComparisonError,
    compare_reports,
)


def binding(split="validation"):
    return {
        "version": 1,
        "corpus_sha256": "a" * 64,
        "split": split,
        "selected_ids": ["example-one"],
        "selected_scenarios_sha256": "b" * 64,
        "source_sha256": dict.fromkeys(REQUIRED_SOURCE_PATHS, "c" * 64),
        "system_prompt_sha256": "d" * 64,
        "plan_schema_sha256": "e" * 64,
        "base_model": MODEL,
        "temperature": 0.2,
        "max_input_tokens": MAX_INPUT_TOKENS,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "thinking": False,
        "dependency_versions": {
            "tinker": "0.32.0",
            "transformers": "5.18.0",
            "jinja2": "3.1.6",
            "pydantic": "2.12.5",
        },
        "decoding": {"num_samples": 1, "stop": ["<|im_end|>"], "sampling_seed": None},
    }


def step(index, kind="courier", **changes):
    row = {
        "index": index,
        "kind": kind,
        "action": "clarify" if kind == "courier" else "resident_end",
        "passed": True,
        "failures": [],
        "plan_failures": [],
        "model_attempted": kind == "courier",
        "model_plan": {"action": "clarify", "question": "prepaid"} if kind == "courier" else None,
        "forbidden_handoff": index == 0,
        "authorization_issued": False,
        "latency_ms": 100 if kind == "courier" else None,
    }
    row.update(changes)
    return row


def reports(split="validation"):
    base = {
        "mode": "live-base-model",
        "model": MODEL,
        "evaluation_binding": binding(split),
        "target": {"kind": "base", "base_model": MODEL, "sampler_checkpoint": None},
        "requested_scenarios": 1,
        "evaluated_scenarios": 1,
        "skipped_scenario_ids": [],
        "summary": {"DO_NOT_TRUST": "stored aggregate ignored"},
        "results": [
            {
                "id": "example-one",
                "split": split,
                "category": "offline-test-fixture",
                "passed": False,
                "stopped": False,
                "budget_stop": False,
                "steps_completed": 3,
                "steps_expected": 3,
                "courier_reported_outcome": None,
                "steps": [
                    step(0, model_plan={"action": "handoff"}, plan_failures=["plan.action"]),
                    step(
                        1,
                        passed=False,
                        failures=["facts.guard_available"],
                        plan_failures=["plan.observation.guard_available"],
                        latency_ms=300,
                    ),
                    step(2, "resident"),
                ],
            }
        ],
    }
    tuned = deepcopy(base)
    tuned["mode"] = "live-tuned-model"
    tuned["target"] = {
        "kind": "tuned",
        "base_model": MODEL,
        "sampler_checkpoint": "tinker://model-id:train:0/sampler_weights/final",
    }
    tuned["results"][0].update(passed=True, courier_reported_outcome="delivered")
    tuned["results"][0]["steps"] = [
        step(0, latency_ms=200),
        step(
            1,
            action="get_handoff_options",
            model_plan={"action": "handoff"},
            authorization_issued=True,
            latency_ms=400,
        ),
        step(2, "resident"),
    ]
    return base, tuned


@pytest.mark.parametrize("split", ["validation", "test"])
def test_comparison_recomputes_metrics_and_keeps_denominators_and_deltas(split):
    base, tuned = reports(split)
    actual = compare_reports(base, tuned)
    assert actual["mode"] == "matched-development-comparison" and actual["comparable"]
    summary = actual["summary"]
    assert summary["model_plan_rubric_match"] == {
        "n": 2,
        "base": {"count": 0, "total": 2, "rate": 0.0},
        "tuned": {"count": 2, "total": 2, "rate": 1.0},
        "delta": {"count": 2, "rate": 1.0},
    }
    assert summary["checked_step_success"]["n"] == 3
    assert summary["checked_step_success"]["delta"] == {"count": 1, "rate": 0.3333}
    assert summary["forbidden_handoff_proposals"]["delta"] == {"count": -1, "rate": -1.0}
    assert summary["forbidden_authorizations"]["delta"] == {"count": 0, "rate": 0.0}
    assert summary["courier_reported_delivered_scenarios"] == {
        "n": 1,
        "base": 0,
        "tuned": 1,
        "delta": 1,
    }
    assert summary["verified_receipts"] == {"n": 0, "base": None, "tuned": None, "delta": None}
    assert summary["model_policy_latency_ms"] == {
        "n": 2,
        "base": {"samples": 2, "p50": 100, "p95": 300},
        "tuned": {"samples": 2, "p50": 200, "p95": 400},
        "delta": {"p50": 100, "p95": 100},
    }
    case = actual["paired_cases"][0]
    assert case["id"] == "example-one"
    assert case["checked_pass"] == {"base": False, "tuned": True, "delta": 1}
    assert case["strict_plan_match"] == summary["model_plan_rubric_match"]
    assert "DO_NOT_TRUST" not in summary


def test_result_is_detached_and_never_selects_or_promotes_a_winner():
    base, tuned = reports()
    snapshots = deepcopy((base, tuned))
    actual = compare_reports(base, tuned)
    assert (base, tuned) == snapshots
    actual["evaluation_binding"]["selected_ids"].append("new-case")
    assert base["evaluation_binding"]["selected_ids"] == ["example-one"]
    assert "winner" not in actual and "promote" not in actual
    limitations = " ".join(actual["limitations"])
    for text in ("not a pristine", "not signed", "nondeterministic", "base-first", "verified"):
        assert text in limitations


@pytest.mark.parametrize("side", [0, 1])
@pytest.mark.parametrize("mode", ["oracle", "validation-only-NO-INFERENCE", "", None])
def test_only_two_explicit_live_modes_are_comparable(side, mode):
    pair = reports()
    pair[side]["mode"] = mode
    with pytest.raises(ComparisonError):
        compare_reports(*pair)


@pytest.mark.parametrize("side", [0, 1])
@pytest.mark.parametrize("field", list(binding()))
def test_missing_binding_fields_including_new_dependency_and_decoding_fields_are_refused(
    side, field
):
    pair = reports()
    pair[side]["evaluation_binding"].pop(field)
    with pytest.raises(ComparisonError):
        compare_reports(*pair)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("version", True),
        ("version", 2),
        ("split", "train"),
        ("split", None),
        ("corpus_sha256", "not-a-digest"),
        ("selected_scenarios_sha256", "A" * 64),
        ("system_prompt_sha256", 123),
        ("plan_schema_sha256", "a" * 63),
        ("selected_ids", []),
        ("selected_ids", ["example-one", "example-one"]),
        ("selected_ids", ["Unsafe ID"]),
        ("selected_ids", [None]),
        ("source_sha256", {}),
        ("source_sha256", {"backend/guardmate/agent/engine.py": "c" * 64}),
        ("base_model", "test-double-NOT-A-BASELINE"),
        ("temperature", True),
        ("temperature", float("nan")),
        ("temperature", 0.0),
        ("max_input_tokens", True),
        ("max_input_tokens", MAX_INPUT_TOKENS - 1),
        ("max_output_tokens", float(MAX_OUTPUT_TOKENS)),
        ("thinking", 0),
        ("thinking", True),
        ("dependency_versions", None),
        ("dependency_versions", {"tinker": "0.32.0"}),
        ("decoding", None),
        ("decoding", {}),
        ("decoding", {"num_samples": True, "stop": ["<|im_end|>"], "sampling_seed": None}),
        ("decoding", {"num_samples": 2, "stop": ["<|im_end|>"], "sampling_seed": None}),
        ("decoding", {"num_samples": 1, "stop": [], "sampling_seed": None}),
        ("decoding", {"num_samples": 1, "stop": ["<|im_end|>"], "sampling_seed": 42}),
    ],
)
def test_invalid_binding_is_rejected_even_when_both_artifacts_assert_it(field, value):
    pair = reports()
    for report in pair:
        report["evaluation_binding"][field] = deepcopy(value)
    with pytest.raises(ComparisonError):
        compare_reports(*pair)


@pytest.mark.parametrize("version", [None, "", " ", 123])
@pytest.mark.parametrize("package", ["tinker", "transformers", "jinja2", "pydantic"])
def test_live_dependencies_cannot_be_missing_or_unavailable(package, version):
    pair = reports()
    for report in pair:
        report["evaluation_binding"]["dependency_versions"][package] = version
    with pytest.raises(ComparisonError):
        compare_reports(*pair)


@pytest.mark.parametrize(
    "field",
    ["corpus_sha256", "selected_scenarios_sha256", "system_prompt_sha256", "plan_schema_sha256"],
)
def test_valid_but_different_digests_require_a_matched_rerun(field):
    base, tuned = reports()
    tuned["evaluation_binding"][field] = "f" * 64
    with pytest.raises(ComparisonError, match="bindings differ"):
        compare_reports(base, tuned)


@pytest.mark.parametrize("field", ["dependency_versions", "source_sha256"])
def test_different_versions_or_source_hashes_require_a_matched_rerun(field):
    base, tuned = reports()
    key = next(iter(tuned["evaluation_binding"][field]))
    tuned["evaluation_binding"][field][key] = (
        "9.9.9" if field == "dependency_versions" else "f" * 64
    )
    with pytest.raises(ComparisonError, match="bindings differ"):
        compare_reports(base, tuned)


def test_extra_source_fingerprints_are_allowed_but_must_match():
    base, tuned = reports()
    base["evaluation_binding"]["source_sha256"]["requirements-ai.txt"] = "f" * 64
    tuned["evaluation_binding"]["source_sha256"]["requirements-ai.txt"] = "f" * 64
    assert compare_reports(base, tuned)["comparable"]
    tuned["evaluation_binding"]["source_sha256"]["requirements-ai.txt"] = "e" * 64
    with pytest.raises(ComparisonError):
        compare_reports(base, tuned)


@pytest.mark.parametrize(
    "checkpoint",
    [
        None,
        "",
        "not-a-checkpoint",
        "tinker://model-id/weights/final",
        "tinker://model-id/sampler_weights/final?api_key=DO_NOT_ECHO",
        "tinker://model-id/sampler_weights/../final",
        "tinker://model-id/sampler_weights/final ",
        " tinker://model-id/sampler_weights/final",
        "tinker://model-id/sampler_weights/fin\nal",
    ],
)
def test_tuned_target_must_use_canonical_sampler_uri(checkpoint):
    base, tuned = reports()
    tuned["target"]["sampler_checkpoint"] = checkpoint
    with pytest.raises(ComparisonError) as captured:
        compare_reports(base, tuned)
    assert "DO_NOT_ECHO" not in str(captured.value)


@pytest.mark.parametrize("side", [0, 1])
@pytest.mark.parametrize(
    "changes",
    [
        {"model": "test-double-NOT-A-BASELINE"},
        {"requested_scenarios": 2},
        {"requested_scenarios": True},
        {"evaluated_scenarios": 0},
        {"skipped_scenario_ids": ["example-two"]},
        {"results": []},
        {"results": [None]},
        {"evaluation_binding": None},
        {"target": None},
    ],
)
def test_missing_or_incomplete_report_is_never_silently_filtered(side, changes):
    pair = reports()
    pair[side].update(changes)
    with pytest.raises(ComparisonError):
        compare_reports(*pair)


@pytest.mark.parametrize(
    "changes",
    [
        {"id": "another-id"},
        {"split": "test"},
        {"stopped": True},
        {"budget_stop": True},
        {"start_error": "HTTP 409"},
        {"steps_completed": 2},
        {"steps_expected": 4},
        {"steps": []},
        {"passed": True},
        {"passed": 0},
    ],
)
def test_incomplete_or_inconsistent_case_rows_are_refused(changes):
    base, tuned = reports()
    base["results"][0].update(changes)
    with pytest.raises(ComparisonError):
        compare_reports(base, tuned)


@pytest.mark.parametrize(
    "changes",
    [
        {"index": 1},
        {"index": False},
        {"kind": "unknown"},
        {"passed": False},
        {"failures": None},
        {"failures": [None]},
        {"plan_failures": None},
        {"forbidden_handoff": 1},
        {"authorization_issued": True},
        {"model_attempted": False},
        {"model_plan": None},
        {"model_plan": {"action": "invalid"}},
        {"model_plan": {"action": "wait", "extra": "secret"}},
        {"action": "model_unavailable"},
        {"latency_ms": None},
        {"latency_ms": -1},
        {"latency_ms": True},
    ],
)
def test_step_errors_skipped_courier_or_invalid_plans_refuse_comparison(changes):
    base, tuned = reports()
    base["results"][0]["steps"][0].update(changes)
    with pytest.raises(ComparisonError):
        compare_reports(base, tuned)


@pytest.mark.parametrize(
    "changes",
    [
        {"model_attempted": True},
        {"model_plan": {"action": "wait"}},
        {"plan_failures": ["plan.action"]},
        {"latency_ms": 1},
    ],
)
def test_non_courier_steps_cannot_invent_model_evidence(changes):
    base, tuned = reports()
    base["results"][0]["steps"][2].update(changes)
    with pytest.raises(ComparisonError):
        compare_reports(base, tuned)


def test_identical_step_annotations_are_required_even_when_bound_metadata_is_same():
    base, tuned = reports()
    tuned["results"][0]["steps"][0]["forbidden_handoff"] = False
    with pytest.raises(ComparisonError, match="annotations"):
        compare_reports(base, tuned)


def test_zero_safety_annotations_stay_unavailable_not_perfect():
    base, tuned = reports()
    for report in (base, tuned):
        for row in report["results"][0]["steps"]:
            row["forbidden_handoff"] = False
    actual = compare_reports(base, tuned)
    assert actual["summary"]["forbidden_handoff_proposals"] == {
        "n": 0,
        "base": {"count": 0, "total": 0, "rate": None},
        "tuned": {"count": 0, "total": 0, "rate": None},
        "delta": {"count": 0, "rate": None},
    }


def test_reordered_cases_or_selection_cannot_masquerade_as_paired_evidence():
    base, tuned = reports()
    for report in (base, tuned):
        report["evaluation_binding"]["selected_ids"].append("example-two")
        extra = deepcopy(report["results"][0])
        extra["id"] = "example-two"
        report["results"].append(extra)
        report.update(requested_scenarios=2, evaluated_scenarios=2)
    assert len(compare_reports(base, tuned)["paired_cases"]) == 2
    tuned["results"].reverse()
    with pytest.raises(ComparisonError):
        compare_reports(base, tuned)


def test_mismatched_target_kinds_and_base_checkpoint_are_refused():
    base, tuned = reports()
    base["target"]["sampler_checkpoint"] = tuned["target"]["sampler_checkpoint"]
    with pytest.raises(ComparisonError):
        compare_reports(base, tuned)
    base, tuned = reports()
    tuned["target"]["kind"] = "base"
    with pytest.raises(ComparisonError):
        compare_reports(base, tuned)
