"""Offline event attribution; injected providers never invoke hosted inference."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from guardmate.agent.engine import ConversationEngine
from guardmate.agent.models import AgentPlan, ModelStatus, ResidentDecision, TurnRequest
from guardmate.agent.provider import ModelUnavailable
from guardmate.agent.store import ConversationStore
from guardmate.context import build_context
from guardmate.models import Dashboard, DeliveryMode, ResidentProfile, TodayOverride

CHECKPOINT = "tinker://fictional:train:0/sampler_weights/final"


class InjectedProvider:
    def __init__(self, *, target_kind="base", sampler_checkpoint=None):
        self.target_kind = target_kind
        self.sampler_checkpoint = sampler_checkpoint
        self.checkpoint_verified = False
        self.plan = AgentPlan(action="answer", topic="directions")
        self.callback = None
        self.calls = 0
        self.messages = []
        self.status_failure = False

    def status(self):
        if self.status_failure:
            raise RuntimeError("PRIVATE_STATUS_ERROR")
        return ModelStatus(
            configured=True,
            model="fictional-open-model",
            provider="offline injected provider",
            target_kind=self.target_kind,
            sampler_checkpoint=self.sampler_checkpoint,
            checkpoint_verified=self.checkpoint_verified,
            message="PRIVATE_STATUS_MESSAGE",
            reserved_usd=12.345,
            budget_usd=20,
        )

    def generate(self, messages):
        self.calls += 1
        self.messages.append([dict(message) for message in messages])
        if self.callback:
            self.callback()
        if isinstance(self.plan, Exception):
            raise self.plan
        return self.plan.model_copy(deep=True)


@pytest.fixture
def setup(tmp_path):
    clock = {"now": datetime(2026, 10, 5, 6, tzinfo=UTC)}
    profile = ResidentProfile(
        resident_name="Fictional resident",
        pg_name="Fictional PG",
        guard_location="the fictional guard room",
    )
    mode = DeliveryMode(enabled=True, expires_at=clock["now"] + timedelta(hours=2))
    override = TodayOverride(status="at_office", local_date=clock["now"].date())

    def dashboard():
        return Dashboard(
            profile=profile,
            delivery_mode=mode,
            context=build_context(profile, mode, override, clock["now"]),
            server_time=clock["now"],
        )

    provider = InjectedProvider()
    store = ConversationStore(tmp_path)
    engine = ConversationEngine(store, provider, dashboard, lambda: clock["now"])
    return engine, provider, store, clock, dashboard


def turn(engine, session, text="Where is the guard room?"):
    return engine.turn(session.id, TurnRequest(text=text, revision=session.revision))


def test_base_turn_saves_only_identity_allowlist(setup):
    engine, provider, store, *_ = setup
    session = turn(engine, engine.start())
    event = session.events[-1]
    assert event.model_identity.model_dump() == {
        "model": "fictional-open-model",
        "provider": "offline injected provider",
        "target_kind": "base",
        "sampler_checkpoint": None,
        "checkpoint_verified": False,
    }
    assert event.model_result == "plan_returned"
    assert event.model_action == "answer"
    assert store.read(session.id).events[-1].model_identity == event.model_identity
    saved = session.model_dump_json()
    assert "PRIVATE_STATUS_MESSAGE" not in saved
    assert "reserved_usd" not in saved and "budget_usd" not in saved
    assert provider.calls == 1


def test_first_tuned_generation_captures_post_call_verification(setup):
    engine, provider, *_ = setup
    provider.target_kind = "tuned"
    provider.sampler_checkpoint = CHECKPOINT
    provider.callback = lambda: setattr(provider, "checkpoint_verified", True)
    session = turn(engine, engine.start())
    identity = session.events[-1].model_identity
    assert identity.target_kind == "tuned"
    assert identity.sampler_checkpoint == CHECKPOINT
    assert identity.checkpoint_verified is True
    assert provider.calls == 1


@pytest.mark.parametrize("verified", [False, True])
def test_unavailable_retains_attempted_target_without_claiming_no_api_sample(setup, verified):
    engine, provider, *_ = setup
    provider.target_kind = "tuned"
    provider.sampler_checkpoint = CHECKPOINT
    provider.checkpoint_verified = verified
    provider.plan = ModelUnavailable(
        "The provider became unavailable after an attempted operation."
    )
    session = turn(engine, engine.start())
    event = session.events[-1]
    assert session.status == "needs_resident"
    assert session.authorized_location is None
    assert event.action == "model_unavailable"
    assert event.model_result == "unavailable"
    assert event.model_identity.target_kind == "tuned"
    assert event.model_identity.checkpoint_verified is verified
    assert event.model_action is None
    assert provider.calls == 1


def test_rejected_model_plan_remains_attributed_to_attempted_model(setup):
    engine, provider, *_ = setup
    provider.plan = AgentPlan(action="handoff")
    session = turn(engine, engine.start(), "This parcel needs an OTP.")
    event = session.events[-1]
    assert event.action == "request_takeover"
    assert event.model_action == "handoff"
    assert event.model_result == "plan_returned"
    assert event.model_identity.target_kind == "base"
    assert session.authorized_location is None


def test_provenance_does_not_enter_prompts_or_grant_handoff_permission(setup):
    engine, provider, *_ = setup
    provider.target_kind = "tuned"
    provider.sampler_checkpoint = CHECKPOINT
    provider.checkpoint_verified = True
    provider.plan = AgentPlan(action="handoff")
    session = turn(engine, engine.start(), "Where should I leave the parcel?")
    session = turn(engine, session, "Is it okay to leave it now?")
    assert session.authorized_location is None
    assert session.facts.prepaid is None and session.facts.guard_available is None
    assert session.events[-1].action == "clarify"
    assert session.events[-1].model_identity.target_kind == "tuned"
    prompts = json.dumps(provider.messages)
    for excluded in (
        CHECKPOINT,
        "model_identity",
        "model_result",
        "target_kind",
        "checkpoint_verified",
        "sampler_checkpoint",
        "fictional-open-model",
        "offline injected provider",
    ):
        assert excluded not in prompts
    assert "action" in AgentPlan.model_json_schema()["properties"]
    assert "model_identity" not in AgentPlan.model_json_schema()["properties"]


def test_existing_unspecified_status_does_not_guess_base_or_tuned(setup):
    engine, provider, *_ = setup
    provider.status = lambda: ModelStatus(
        configured=True,
        model="old-fake",
        provider="legacy fake",
        message="legacy local status",
        reserved_usd=0,
        budget_usd=0,
    )
    session = turn(engine, engine.start())
    identity = session.events[-1].model_identity
    assert identity.model == "old-fake"
    assert identity.target_kind == "unspecified"
    assert identity.sampler_checkpoint is None
    assert identity.checkpoint_verified is False


def test_provider_switching_and_restart_do_not_relabel_saved_events(setup):
    engine, provider, store, clock, dashboard = setup
    session = turn(engine, engine.start())
    first_identity = session.events[-1].model_identity.model_copy(deep=True)
    tuned = InjectedProvider(target_kind="tuned", sampler_checkpoint=CHECKPOINT)
    tuned.checkpoint_verified = True
    restarted = ConversationEngine(
        ConversationStore(store.path.parent), tuned, dashboard, lambda: clock["now"]
    )
    session = turn(restarted, restarted.read(session.id))
    persisted = store.read(session.id)
    assert persisted.events[0].model_identity == first_identity
    assert persisted.events[0].model_identity.target_kind == "base"
    assert persisted.events[1].model_identity.target_kind == "tuned"
    assert persisted.events[1].model_identity.sampler_checkpoint == CHECKPOINT
    assert provider.calls == tuned.calls == 1


def test_mid_call_engine_selection_change_does_not_change_attribution(setup):
    engine, provider, *_ = setup
    other = InjectedProvider(target_kind="tuned", sampler_checkpoint=CHECKPOINT)
    provider.callback = lambda: setattr(engine, "provider", other)
    session = turn(engine, engine.start())
    assert session.events[-1].model_identity.target_kind == "base"
    assert provider.calls == 1 and other.calls == 0


def test_mutable_adapter_cannot_transfer_verification_to_different_target(setup):
    engine, provider, *_ = setup

    def change_status():
        provider.target_kind = "tuned"
        provider.sampler_checkpoint = CHECKPOINT
        provider.checkpoint_verified = True

    provider.callback = change_status
    session = turn(engine, engine.start())
    identity = session.events[-1].model_identity
    assert identity.target_kind == "base"
    assert identity.sampler_checkpoint is None
    assert identity.checkpoint_verified is False


def test_post_call_status_failure_falls_back_without_losing_checked_reply(setup):
    engine, provider, *_ = setup
    provider.callback = lambda: setattr(provider, "status_failure", True)
    session = turn(engine, engine.start())
    assert session.events[-1].model_identity.target_kind == "base"
    assert session.events[-1].model_result == "plan_returned"
    assert session.events[-1].action == "get_delivery_context"
    assert "PRIVATE_STATUS_ERROR" not in session.model_dump_json()


def test_unreadable_identity_does_not_discard_success_or_guess_target(setup):
    engine, provider, *_ = setup
    session = engine.start()
    provider.status_failure = True
    session = turn(engine, session)
    assert session.events[-1].model_identity is None
    assert session.events[-1].model_result == "plan_returned"
    assert session.events[-1].action == "get_delivery_context"


@pytest.mark.parametrize("decision", ["end", "takeover"])
def test_greeting_and_resident_events_have_no_model_identity(setup, decision):
    engine, provider, *_ = setup
    session = engine.start()
    assert session.events == []
    session = turn(engine, session)
    session = engine.decide(
        session.id, ResidentDecision(decision=decision, revision=session.revision)
    )
    assert session.events[-1].model_identity is None
    assert session.events[-1].model_result is None
    assert provider.calls == 1


def test_approval_expiry_is_not_relabelled_as_model_generated(setup):
    engine, provider, _, clock, _ = setup
    provider.plan = AgentPlan(action="request_approval", proposed_location="reception")
    session = turn(engine, engine.start(), "May I leave the parcel at reception?")
    assert session.events[-1].model_identity.target_kind == "base"
    clock["now"] += timedelta(seconds=90)
    session = engine.read(session.id)
    assert session.events[-1].action == "approval_expired"
    assert session.events[-1].model_identity is None
    assert session.events[-1].model_result is None
    with pytest.raises(HTTPException):
        turn(engine, session)
    assert provider.calls == 1


@pytest.mark.parametrize("decision", ["approve", "decline"])
def test_resident_approval_decision_does_not_inherit_prior_model_identity(setup, decision):
    engine, provider, *_ = setup
    provider.plan = AgentPlan(action="request_approval", proposed_location="reception")
    session = turn(engine, engine.start(), "May I leave the parcel at reception?")
    session = engine.decide(
        session.id,
        ResidentDecision(
            decision=decision,
            approval_id=session.approval.id,
            revision=session.revision,
        ),
    )
    assert session.events[-2].model_identity.target_kind == "base"
    assert session.events[-1].model_identity is None
    assert session.events[-1].model_result is None
    assert provider.calls == 1


def test_legacy_saved_events_remain_unattributed_after_read_and_new_turn(setup):
    engine, _, store, *_ = setup
    session = turn(engine, engine.start())
    legacy = session.model_dump(mode="json")
    for event in legacy["events"]:
        event.pop("model_identity")
        event.pop("model_result")
    with store.connect() as connection:
        connection.execute(
            "UPDATE conversations SET value = ? WHERE id = ?", (json.dumps(legacy), session.id)
        )
    session = engine.read(session.id)
    assert session.events[0].model_identity is None
    assert session.events[0].model_result is None
    session = turn(engine, session)
    assert session.events[0].model_identity is None
    assert session.events[1].model_identity.target_kind == "base"
    assert store.read(session.id).events[0].model_identity is None
