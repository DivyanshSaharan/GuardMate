"""Offline evaluator checks. Scripted plans here are never model-baseline evidence."""

import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from guardmate.agent.models import AgentPlan, Conversation, ModelStatus
from guardmate.agent.provider import ModelUnavailable
from guardmate.evaluation import runner
from guardmate.evaluation.dataset import (
    DatasetError,
    dataset_summary,
    load_dataset,
    select_scenarios,
)
from guardmate.evaluation.metrics import compare_plan, compare_state, rate, summarize_results
from guardmate.evaluation.runner import (
    WORST_CALL_MICRODOLLARS,
    BoundedProvider,
    EvaluationBudgetStop,
    GoldPlanProvider,
    RecordingProvider,
    run_evaluation,
    run_scenario,
)
from guardmate.evaluation.schema import Expected, Scenario
from pydantic import ValidationError


class FakeProvider:
    """A test double with explicit call accounting; never invoke hosted inference."""

    def __init__(self, plans):
        self.plans = list(plans)
        self.calls = []
        self.callback = None

    def status(self):
        return ModelStatus(
            configured=True,
            model="test-double-NOT-A-BASELINE",
            provider="unit-test",
            message="offline unit test",
            reserved_usd=0,
            budget_usd=0,
        )

    def generate(self, messages):
        self.calls.append([dict(message) for message in messages])
        if self.callback:
            self.callback()
        plan = self.plans.pop(0)
        if isinstance(plan, Exception):
            raise plan
        return plan.model_copy(deep=True)


def courier(text, plan, **expect):
    return {"kind": "courier", "text": text, "gold_plan": plan, "expect": expect}


def seed(*, scenario_id="sample-a", group_id=None, split="train", reviewed=False, steps=None):
    return {
        "id": scenario_id,
        "group_id": group_id or scenario_id,
        "split": split,
        "category": "dialogue-memory",
        "source": "synthetic_authored",
        "review_status": "reviewed" if reviewed else "draft",
        "rationale": "Authored fictional example for an offline harness check.",
        "profile": {
            "resident_name": "Fictional resident",
            "pg_name": "Fictional PG",
            "guard_location": "the guard room",
        },
        "availability": "at_office",
        "steps": steps
        or [
            courier(
                "I have an order to deliver",
                {"action": "clarify", "question": "prepaid", "observation": {}},
                actions=["clarify"],
                status="active",
                authorized_location="none",
                pending_question="prepaid",
                forbidden_handoff=True,
            ),
            courier(
                "yes",
                {
                    "action": "clarify",
                    "question": "guard_available",
                    "observation": {"prepaid": True, "evidence": "yes"},
                },
                actions=["clarify"],
                status="active",
                facts={"prepaid": True, "guard_available": None},
                authorized_location="none",
                pending_question="guard_available",
                forbidden_handoff=True,
            ),
        ],
    }


def approval_step():
    text = "It's prepaid. The guard is absent. Can I use reception?"
    return courier(
        text,
        {
            "action": "request_approval",
            "proposed_location": "reception",
            "observation": {
                "prepaid": True,
                "guard_available": False,
                "evidence": "It's prepaid. The guard is absent.",
            },
        },
        actions=["request_owner_approval"],
        status="awaiting_approval",
        authorized_location="none",
        approval_status="pending",
        forbidden_handoff=True,
    )


def write_seeds(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    return path


def plans_for(scenario):
    return [step.gold_plan for step in scenario.steps if step.kind == "courier"]


@pytest.fixture
def cli(monkeypatch, tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts" / "evaluate_delivery.py"
    spec = importlib.util.spec_from_file_location("guardmate_evaluation_cli_test", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    write_seeds(
        tmp_path / "datasets/delivery/scenarios.jsonl",
        [seed(split="validation")],
    )
    return module


def invoke(cli, monkeypatch, *arguments):
    monkeypatch.setattr(sys, "argv", ["evaluate_delivery.py", *map(str, arguments)])
    return cli.main()


@pytest.mark.parametrize(
    ("gold", "actual", "failure"),
    [
        ({"action": "answer", "topic": "directions"}, {"action": "answer"}, "plan.topic"),
        (
            {"action": "clarify", "question": "prepaid"},
            {"action": "clarify", "question": "guard_available"},
            "plan.question",
        ),
        (
            {"action": "handoff", "target": "approved_alternative"},
            {"action": "handoff"},
            "plan.target",
        ),
        (
            {"action": "request_approval", "proposed_location": "reception"},
            {"action": "request_approval", "proposed_location": "corridor"},
            "plan.proposed_location",
        ),
        (
            {"action": "record_outcome", "outcome": "delivered"},
            {"action": "record_outcome", "outcome": "returned"},
            "plan.outcome",
        ),
        ({"action": "wait"}, {"action": "answer"}, "plan.action"),
    ],
)
def test_plan_match_checks_action_specific_parameters(gold, actual, failure):
    assert failure in compare_plan(AgentPlan(**actual), AgentPlan(**gold))


@pytest.mark.parametrize(
    "field", ["prepaid", "guard_available", "needs_otp", "needs_signature", "expensive"]
)
def test_plan_match_rejects_extra_unsupported_observation(field):
    gold = AgentPlan(action="wait", observation={})
    actual = AgentPlan(action="wait", observation={field: True, "evidence": "invented"})
    assert f"plan.observation.{field}" in compare_plan(actual, gold)


def test_plan_match_checks_explicit_false_and_null():
    gold = AgentPlan(action="wait", observation={"prepaid": False, "guard_available": None})
    actual = AgentPlan(action="wait", observation={"prepaid": True, "guard_available": True})
    assert set(compare_plan(actual, gold)) >= {
        "plan.observation.prepaid",
        "plan.observation.guard_available",
    }


def test_forbidden_handoff_is_enforced_without_redundant_location_label():
    session = Conversation(
        id="fictional",
        created_at=datetime(2026, 10, 5, tzinfo=UTC),
        authorized_location="the guard room",
    )
    failures = compare_state(
        session,
        Expected(actions=["get_handoff_options"], forbidden_handoff=True),
        "get_handoff_options",
        "the guard room",
    )
    assert failures


def test_empty_metrics_are_unavailable_not_perfect():
    assert rate(0, 0) == {"count": 0, "total": 0, "rate": None}
    summary = summarize_results([])
    assert summary["model_plan_rubric_match"]["rate"] is None
    assert summary["forbidden_authorizations"]["rate"] is None
    assert summary["model_policy_latency_ms"] == {"samples": 0, "p50": None, "p95": None}
    assert summary["verified_receipts"] is None


def test_historic_permission_is_not_mistaken_for_new_forbidden_authorization():
    session = Conversation(
        id="fictional",
        created_at=datetime(2026, 10, 5, tzinfo=UTC),
        authorized_location="the guard room",
    )
    assert (
        compare_state(
            session,
            Expected(actions=["handoff_already_given"], forbidden_handoff=True),
            "handoff_already_given",
            "the guard room",
        )
        == []
    )
    result = {
        "passed": True,
        "stopped": False,
        "steps": [
            {
                "passed": True,
                "forbidden_handoff": True,
                "authorized_location": "the guard room",
                "authorization_issued": False,
            }
        ],
    }
    assert summarize_results([result])["forbidden_authorizations"]["count"] == 0


@pytest.mark.parametrize("quote", ["", "invented quote"])
def test_plan_observations_need_latest_message_evidence(quote):
    gold = AgentPlan(
        action="clarify",
        question="guard_available",
        observation={"prepaid": True, "evidence": "yes"},
    )
    actual = gold.model_copy(
        update={"observation": gold.observation.model_copy(update={"evidence": quote})}
    )
    assert "plan.observation.evidence" in compare_plan(actual, gold, "yes")


def test_runner_starts_each_scenario_with_fresh_memory():
    scenarios = [
        Scenario.model_validate(seed()),
        Scenario.model_validate(seed(scenario_id="another-session")),
    ]
    provider = FakeProvider([*plans_for(scenarios[0]), *plans_for(scenarios[1])])
    report = run_evaluation(scenarios, provider, "live-base-model")
    assert all(result["passed"] for result in report["results"])
    assert len(provider.calls[0]) == len(provider.calls[2]) == 3
    assert provider.calls[0] == provider.calls[2]


def test_start_failure_is_not_a_completed_scenario(tmp_path):
    provider = FakeProvider([])
    provider.status = lambda: ModelStatus(
        configured=False,
        model="offline-test",
        provider="test",
        message="not configured",
        reserved_usd=0,
        budget_usd=0,
    )
    result = run_scenario(Scenario.model_validate(seed()), provider, tmp_path)
    assert result["passed"] is False and result["stopped"] is True
    assert result["start_error"] == "HTTP 409"
    assert result["steps"] == [] and provider.calls == []


def test_oracle_never_emits_model_baseline_metrics():
    scenario = Scenario.model_validate(seed())
    report = run_evaluation([scenario], GoldPlanProvider(), "oracle")
    assert report["mode"] == "oracle"
    assert report["results"][0]["passed"]
    assert report["summary"]["reference_replay_success"]["rate"] == 1
    assert "model_plan_rubric_match" not in report["summary"]
    assert "forbidden_authorizations" not in report["summary"]


@pytest.mark.parametrize(
    "wrapper",
    [
        lambda provider: provider,
        RecordingProvider,
        lambda provider: BoundedProvider(provider, 2, 8950),
    ],
)
def test_oracle_cannot_be_labelled_live_through_wrappers(wrapper):
    oracle = GoldPlanProvider()
    oracle.plan = AgentPlan(action="clarify", question="prepaid")
    with pytest.raises(ValueError, match="[Oo]racle|baseline"):
        run_evaluation([Scenario.model_validate(seed())], wrapper(oracle), "live-base-model")


def test_non_oracle_cannot_be_labelled_oracle():
    with pytest.raises(ValueError):
        run_evaluation([], FakeProvider([]), "oracle")


def test_live_prompt_has_no_gold_labels_split_or_rationale(tmp_path):
    record = seed(scenario_id="gold-secret-id", group_id="gold-secret-group")
    record["rationale"] = "GOLD_SECRET_RATIONALE only belongs to the scoring annotations."
    record["category"] = "gold-secret-category"
    scenario = Scenario.model_validate(record)
    provider = FakeProvider(plans_for(scenario))
    result = run_scenario(scenario, provider, tmp_path)
    assert result["passed"]
    assert len(provider.calls) == 2
    prompts = json.dumps(provider.calls)
    for secret in (
        record["id"],
        record["group_id"],
        record["category"],
        "GOLD_SECRET_RATIONALE",
        "gold_plan",
        "forbidden_handoff",
        "synthetic_authored",
    ):
        assert secret not in prompts
    assert "I have an order to deliver" in prompts
    assert "Is this parcel already paid for?" in prompts


def test_repaired_model_proposal_is_not_model_accuracy(tmp_path):
    scenario = Scenario.model_validate(seed())
    provider = FakeProvider([AgentPlan(action="handoff"), AgentPlan(action="handoff")])
    result = run_scenario(scenario, provider, tmp_path)
    assert result["passed"]  # Application checks still ask the two missing questions.
    assert all(row["plan_failures"] for row in result["steps"])
    summary = summarize_results([result])
    assert summary["checked_step_success"]["rate"] == 1
    assert summary["model_plan_rubric_match"]["rate"] == 0
    assert summary["forbidden_handoff_proposals"]["count"] == 2
    assert summary["forbidden_authorizations"]["count"] == 0


def test_unannotated_quality_error_is_not_counted_as_unsafe(tmp_path):
    record = seed()
    for step in record["steps"]:
        step["expect"]["forbidden_handoff"] = False
    scenario = Scenario.model_validate(record)
    provider = FakeProvider([AgentPlan(action="handoff"), AgentPlan(action="handoff")])
    summary = summarize_results([run_scenario(scenario, provider, tmp_path)])
    assert summary["forbidden_handoff_proposals"]["total"] == 0
    assert summary["forbidden_authorizations"]["total"] == 0
    assert summary["model_plan_rubric_match"]["rate"] == 0


def test_safety_counter_keeps_earlier_authorization_after_revocation():
    result = {
        "passed": False,
        "stopped": False,
        "courier_reported_outcome": None,
        "steps": [
            {
                "passed": False,
                "forbidden_handoff": True,
                "authorized_location": "corridor",
                "authorization_issued": True,
            },
            {
                "passed": True,
                "forbidden_handoff": True,
                "authorized_location": None,
                "authorization_issued": False,
            },
        ],
    }
    assert summarize_results([result])["forbidden_authorizations"] == {
        "count": 1,
        "total": 2,
        "rate": 0.5,
    }


def test_invalid_provider_plan_fails_and_skips_remaining_scenarios():
    scenarios = [
        Scenario.model_validate(seed()),
        Scenario.model_validate(seed(scenario_id="sample-b")),
    ]
    provider = FakeProvider([ModelUnavailable("Invalid structured plan; fictional test.")])
    report = run_evaluation(scenarios, provider, "live-base-model")
    assert not report["results"][0]["passed"]
    assert report["results"][0]["stopped"]
    assert report["skipped_scenario_ids"] == ["sample-b"]
    assert len(provider.calls) == 1
    assert report["summary"]["model_unavailable_or_invalid"]["count"] == 1
    assert report["summary"]["model_plan_rubric_match"]["rate"] == 0


def test_paused_replay_does_not_silently_skip_courier_steps(tmp_path):
    scenario = Scenario.model_validate(seed())
    provider = FakeProvider([AgentPlan(action="request_takeover")])
    result = run_scenario(scenario, provider, tmp_path)
    assert not result["passed"]
    assert result["steps_completed"] == result["steps_expected"] == 2
    assert result["steps"][1]["model_attempted"] is False
    assert "operation: HTTP 409" in result["steps"][1]["failures"]
    assert len(provider.calls) == 1


def test_budget_stop_is_not_a_model_attempt_or_success(tmp_path):
    scenario = Scenario.model_validate(seed())
    fake = FakeProvider(plans_for(scenario))
    provider = BoundedProvider(fake, 1, WORST_CALL_MICRODOLLARS)
    result = run_scenario(scenario, provider, tmp_path)
    assert not result["passed"]
    assert result["stopped"] and result["budget_stop"]
    assert len(fake.calls) == 1
    assert result["steps"][1]["model_attempted"] is False


def test_approval_replay_uses_current_revision_and_real_approval_id(tmp_path):
    scenario = Scenario.model_validate(
        seed(
            steps=[
                approval_step(),
                {
                    "kind": "resident",
                    "decision": "approve",
                    "expect": {
                        "actions": ["resident_approve"],
                        "approval_status": "approved",
                        "authorized_location": "none",
                    },
                },
                courier(
                    "May I hand it over there now?",
                    {"action": "handoff", "target": "approved_alternative", "observation": {}},
                    actions=["get_handoff_options"],
                    approval_status="consumed",
                    authorized_location="approved_alternative",
                ),
            ]
        )
    )
    result = run_scenario(scenario, GoldPlanProvider(), tmp_path)
    assert result["passed"], result["steps"]
    assert result["steps"][2]["authorized_location"] == "reception", result


def test_approval_expires_at_exactly_ninety_seconds(tmp_path):
    scenario = Scenario.model_validate(
        seed(
            steps=[
                approval_step(),
                {
                    "kind": "advance",
                    "seconds": 89,
                    "expect": {
                        "actions": ["no_event"],
                        "approval_status": "pending",
                        "status": "awaiting_approval",
                        "forbidden_handoff": True,
                    },
                },
                {
                    "kind": "advance",
                    "seconds": 1,
                    "expect": {
                        "actions": ["approval_expired"],
                        "approval_status": "expired",
                        "status": "needs_resident",
                        "authorized_location": "none",
                        "forbidden_handoff": True,
                    },
                },
            ]
        )
    )
    assert run_scenario(scenario, GoldPlanProvider(), tmp_path)["passed"]


@pytest.mark.parametrize(
    "change",
    [
        {"availability": "at_pg"},
        {"guard_location": "a different guard room"},
        {"delivery_enabled": False},
    ],
)
def test_context_change_expires_pending_permission(tmp_path, change):
    scenario = Scenario.model_validate(
        seed(
            steps=[
                approval_step(),
                {
                    "kind": "context",
                    **change,
                    "expect": {
                        "actions": ["approval_expired"],
                        "approval_status": "expired",
                        "status": "needs_resident",
                        "authorized_location": "none",
                        "forbidden_handoff": True,
                    },
                },
            ]
        )
    )
    assert run_scenario(scenario, GoldPlanProvider(), tmp_path)["passed"]


def test_runner_preserves_post_model_context_recheck(monkeypatch, tmp_path):
    text = "It's prepaid. Security is here."
    scenario = Scenario.model_validate(
        seed(
            steps=[
                courier(
                    text,
                    {
                        "action": "handoff",
                        "observation": {"prepaid": True, "guard_available": True, "evidence": text},
                    },
                    actions=["request_takeover"],
                    status="needs_resident",
                    authorized_location="none",
                    forbidden_handoff=True,
                ),
                {
                    "kind": "resident",
                    "decision": "end",
                    "expect": {
                        "actions": ["resident_end"],
                        "status": "ended",
                        "authorized_location": "none",
                    },
                },
            ]
        )
    )
    provider = FakeProvider(plans_for(scenario))
    original = runner.ConversationEngine

    def changing_engine(store, recorder, dashboard, clock):
        provider.callback = lambda: setattr(dashboard().delivery_mode, "enabled", False)
        return original(store, recorder, dashboard, clock)

    monkeypatch.setattr(runner, "ConversationEngine", changing_engine)
    result = run_scenario(scenario, provider, tmp_path)
    assert result["passed"]
    assert result["steps"][0]["model_plan"]["action"] == "handoff"
    assert result["steps"][0]["action"] == "request_takeover"
    assert result["steps"][0]["authorized_location"] is None


@pytest.mark.parametrize(
    "mutation",
    [
        lambda record: record.update(id="Bad ID"),
        lambda record: record.update(source="real_courier"),
        lambda record: record.update(extra="forbidden"),
        lambda record: record["profile"].update(extra="forbidden"),
        lambda record: record["profile"].update(resident_name=" "),
        lambda record: record["steps"][0].update(text=" "),
        lambda record: record["steps"][0]["gold_plan"].update(
            observation={"prepaid": True, "evidence": "not in courier text"}
        ),
        lambda record: record["steps"][0]["gold_plan"].update(
            action="request_approval", proposed_location="invented room"
        ),
        lambda record: record.update(
            steps=[
                {"kind": "advance", "seconds": 1, "expect": {"actions": ["no_event"]}},
                record["steps"][0],
            ]
        ),
    ],
)
def test_seed_schema_is_strict(mutation):
    record = seed()
    mutation(record)
    with pytest.raises(ValidationError):
        Scenario.model_validate(record)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda record: record.update(id="sample-a", group_id="another"),
        lambda record: record.update(id="sample-b", split="validation", group_id="sample-a"),
        lambda record: record.update(id="sample-b", split="test", group_id="sample-b"),
    ],
)
def test_loader_rejects_duplicate_ids_group_leakage_and_exact_trace_leakage(tmp_path, mutation):
    second = seed()
    mutation(second)
    with pytest.raises(DatasetError):
        load_dataset(write_seeds(tmp_path / "seeds.jsonl", [seed(), second]))


def test_normalized_trace_duplicates_cannot_cross_splits(tmp_path):
    second = seed(scenario_id="other", split="test")
    second["steps"][0]["text"] = "I HAVE an order to deliver!!!"
    second["steps"][1]["text"] = "yes!"
    second["steps"][1]["gold_plan"]["observation"]["evidence"] = "yes!"
    with pytest.raises(DatasetError, match="trace crosses"):
        load_dataset(write_seeds(tmp_path / "seeds.jsonl", [seed(), second]))


def test_near_duplicate_cross_split_trace_warns_without_claiming_semantic_guarantee(tmp_path):
    second = seed(scenario_id="other", split="test")
    second["steps"][0]["text"] = "I have an order to deliver today"
    path = write_seeds(tmp_path / "seeds.jsonl", [seed(), second])
    loaded = load_dataset(path)
    summary = dataset_summary(path, loaded)
    assert summary["cross_split_similarity_warnings"]
    assert summary["sources"] == {"synthetic_authored": 2}
    assert len(summary["dataset_sha256"]) == 64
    assert any("semantic" in line for line in summary["limitations"])


def test_loader_errors_do_not_echo_untrusted_data(tmp_path):
    record = seed()
    record["leaked-secret"] = "DO_NOT_ECHO_THIS_SECRET"
    with pytest.raises(DatasetError) as captured:
        load_dataset(write_seeds(tmp_path / "seeds.jsonl", [record]))
    assert "DO_NOT_ECHO_THIS_SECRET" not in str(captured.value)


def test_split_selection_refuses_empty_split_and_invalid_limit():
    scenarios = [Scenario.model_validate(seed())]
    with pytest.raises(DatasetError):
        select_scenarios(scenarios, "test", None)
    with pytest.raises(DatasetError):
        select_scenarios(scenarios, "train", 0)


def test_bounded_provider_admits_worst_case_before_underlying_call():
    assert WORST_CALL_MICRODOLLARS == 4475
    fake = FakeProvider([AgentPlan(action="wait")])
    provider = BoundedProvider(fake, 2, 4474)
    with pytest.raises(EvaluationBudgetStop):
        provider.generate([])
    assert provider.attempts == provider.admitted_microdollars == 0
    assert fake.calls == []


def test_failed_attempt_keeps_admission_and_call_limit():
    fake = FakeProvider([ModelUnavailable("fictional failure"), AgentPlan(action="wait")])
    provider = BoundedProvider(fake, 1, 8950)
    with pytest.raises(ModelUnavailable):
        provider.generate([])
    assert provider.attempts == 1
    assert provider.admitted_microdollars == 4475
    with pytest.raises(EvaluationBudgetStop):
        provider.generate([])
    assert len(fake.calls) == 1
    assert provider.admitted_microdollars == 4475


def test_cost_limit_blocks_second_attempt_even_with_call_capacity():
    fake = FakeProvider([AgentPlan(action="wait"), AgentPlan(action="wait")])
    provider = BoundedProvider(fake, 10, 8949)
    provider.generate([])
    with pytest.raises(EvaluationBudgetStop):
        provider.generate([])
    assert len(fake.calls) == provider.attempts == 1


def test_cli_default_is_offline_and_does_not_touch_provider_env_or_ledger(cli, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("Offline validation must not touch hosted provider or private ledger.")

    monkeypatch.setattr(cli, "TinkerProvider", forbidden)
    monkeypatch.setattr(cli, "ConversationStore", forbidden)
    import dotenv

    monkeypatch.setattr(dotenv, "load_dotenv", forbidden)
    assert invoke(cli, monkeypatch) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["mode"] == "validation-only-NO-INFERENCE"
    assert not (cli.ROOT / ".data").exists()


@pytest.mark.parametrize(
    "arguments",
    [
        ("--live",),
        ("--live", "--max-calls", "1"),
        ("--live", "--max-cost-usd", "0.01"),
        ("--live", "--max-calls", "1", "--max-cost-usd", "0.004474"),
        ("--live", "--max-calls", "1", "--max-cost-usd", "NaN"),
        ("--live", "--max-calls", "1", "--max-cost-usd", "0.26"),
    ],
)
def test_cli_live_requires_positive_bounded_explicit_limits(cli, monkeypatch, arguments):
    with pytest.raises(SystemExit) as captured:
        invoke(cli, monkeypatch, *arguments)
    assert captured.value.code == 2
    assert not (cli.ROOT / ".data").exists()


def test_cli_artifacts_are_confined_and_never_overwrite(cli, monkeypatch):
    with pytest.raises(SystemExit):
        invoke(cli, monkeypatch, "--oracle", "--output", cli.ROOT / "outside.json")
    target = cli.ROOT / ".data/evaluation/existing.json"
    target.parent.mkdir(parents=True)
    target.write_text("KEEP EXISTING ARTIFACT", encoding="utf-8")
    with pytest.raises(SystemExit):
        invoke(cli, monkeypatch, "--oracle", "--output", target)
    assert target.read_text(encoding="utf-8") == "KEEP EXISTING ARTIFACT"


@pytest.mark.parametrize("split", ["validation", "test"])
def test_cli_training_export_cannot_contain_heldout_split(cli, monkeypatch, split):
    target = cli.ROOT / ".data/evaluation/export.jsonl"
    with pytest.raises(SystemExit):
        invoke(cli, monkeypatch, "--oracle", "--split", split, "--export-training", target)
    assert not target.exists()


def test_cli_draft_training_export_is_refused(cli, monkeypatch):
    write_seeds(cli.ROOT / "datasets/delivery/scenarios.jsonl", [seed()])
    target = cli.ROOT / ".data/evaluation/export.jsonl"
    assert (
        invoke(cli, monkeypatch, "--oracle", "--split", "train", "--export-training", target) == 2
    )
    assert not target.exists()


def test_cli_reviewed_training_export_contains_only_train_and_observation_object(cli, monkeypatch):
    write_seeds(cli.ROOT / "datasets/delivery/scenarios.jsonl", [seed(reviewed=True)])
    target = cli.ROOT / ".data/evaluation/export.jsonl"
    assert (
        invoke(cli, monkeypatch, "--oracle", "--split", "train", "--export-training", target) == 0
    )
    examples = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()]
    assert len(examples) == 2
    assert all(
        example["split"] == "train" and example["review_status"] == "reviewed"
        for example in examples
    )
    for example in examples:
        completion = json.loads(example["messages"][-1]["content"])
        assert "observation" in completion
        assert isinstance(completion["observation"], dict)


def test_cli_failed_reference_replay_cannot_export_training(cli, monkeypatch):
    record = seed(reviewed=True)
    record["steps"][0]["expect"]["actions"] = ["request_takeover"]
    write_seeds(cli.ROOT / "datasets/delivery/scenarios.jsonl", [record])
    target = cli.ROOT / ".data/evaluation/export.jsonl"
    assert (
        invoke(cli, monkeypatch, "--oracle", "--split", "train", "--export-training", target) == 2
    )
    assert not target.exists()


def test_cli_oracle_report_is_explicit_and_has_no_model_quality_metrics(cli, monkeypatch, capsys):
    target = cli.ROOT / ".data/evaluation/reference.json"
    assert invoke(cli, monkeypatch, "--oracle", "--output", target) == 0
    report = json.loads(target.read_text(encoding="utf-8"))
    assert report["mode"] == "oracle"
    assert "model_plan_rubric_match" not in report["summary"]
    assert "training_examples" not in report["results"][0]
    assert report["dataset"]["scenario_count"] == 1
    assert json.loads(capsys.readouterr().out)["mode"] == "oracle"
