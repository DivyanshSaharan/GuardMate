"""Repository seed regressions; reference replay is not model evaluation."""

from collections import Counter
from pathlib import Path

from guardmate.evaluation.dataset import dataset_summary, load_dataset
from guardmate.evaluation.runner import GoldPlanProvider, run_evaluation

DATASET = Path(__file__).resolve().parents[2] / "datasets/delivery/scenarios.jsonl"


def test_repository_dataset_provenance_and_splits():
    scenarios = load_dataset(DATASET)
    assert len(scenarios) == 27
    assert Counter(case.split for case in scenarios) == {"train": 15, "validation": 6, "test": 6}
    assert Counter(case.source for case in scenarios) == {
        "synthetic_authored": 24,
        "user_reported_seed_synthetic_expansion": 3,
    }
    assert all(case.review_status == "draft" for case in scenarios)
    assert not dataset_summary(DATASET, scenarios)["cross_split_similarity_warnings"]


def test_reference_replay_including_wording_regressions_passes():
    report = run_evaluation(load_dataset(DATASET), GoldPlanProvider(), "oracle")
    assert not report["skipped_scenario_ids"]
    failures = {case["id"] for case in report["results"] if not case["passed"]}
    assert failures == set()
    assert report["summary"]["reference_replay_success"]["count"] == 27
    assert "model_plan_rubric_match" not in report["summary"]


def test_real_question_seeds_do_not_treat_fragile_as_expensive():
    scenarios = load_dataset(DATASET)
    actual_seeds = [case for case in scenarios if case.source != "synthetic_authored"]
    assert len(actual_seeds) == 3
    assert all(case.split == "train" for case in actual_seeds)
    assert any(
        "Where is the guard room?" in step.text
        for case in actual_seeds
        for step in case.steps
        if step.kind == "courier"
    )
    assert any(
        "Where is your pg located?" in step.text
        for case in actual_seeds
        for step in case.steps
        if step.kind == "courier"
    )
    fragile = next(case for case in actual_seeds if "fragile" in case.id)
    concern = next(
        step for step in fragile.steps if step.kind == "courier" and "fragile" in step.text
    )
    assert concern.gold_plan.action == "request_takeover"
    assert not concern.gold_plan.observation.expensive
    assert concern.expect.forbidden_handoff
