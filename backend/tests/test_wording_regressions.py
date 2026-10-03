"""Conservative English wording checks, exercised without hosted inference."""

from datetime import UTC, datetime, timedelta

import pytest
from guardmate.agent.dialogue import confirmation, is_completed_outcome
from guardmate.agent.engine import ConversationEngine
from guardmate.agent.models import AgentPlan, ModelStatus, TurnRequest
from guardmate.agent.store import ConversationStore
from guardmate.models import Availability, Dashboard, DeliveryContext, DeliveryMode, ResidentProfile

PREPAID_POSITIVE = [
    "It is pre paid.",
    "Yes, it is pre-paid.",
    "YES, THIS PARCEL IS PRE PAID.",
    "It is pre   paid.",
    "It's prepaid.",
    "It is already paid.",
    "The parcel is paid for.",
]

PREPAID_NEGATIVE = [
    "It is not pre paid.",
    "It is not pre-paid.",
    "It isn't pre-paid.",
    "It isn’t pre paid.",
    "It has not been pre paid.",
    "It hasn't been pre-paid.",
    "It is not actually pre paid.",
    "It is unpaid.",
    "This is cash on delivery.",
]

PREPAID_UNCERTAIN = [
    "Is this pre paid?",
    "Is this pre-paid?",
    "It will be pre paid.",
    "It is going to be pre-paid.",
    "It might be pre paid.",
    "Maybe it is pre-paid.",
    "It should be pre paid.",
    "I don't know whether it is pre paid.",
    "I don’t know if it is pre-paid.",
    "I am not sure it is pre paid.",
    "If it is pre-paid, can I leave it?",
    "If it is pre paid I can leave it.",
    "Let me check whether it is pre-paid.",
]

DELIVERED_REPORTS = [
    "I gave it to the guard.",
    "I gave it to the guard. Fine?",
    "I gave the parcel to security.",
    "I GAVE THE PARCEL TO SECURITY.",
    "I handed it to the guard.",
    "Security accepted the parcel.",
    "The guard received the parcel.",
    "The parcel was delivered.",
    "Delivered.",
]

UNCOMPLETED_DELIVERY = [
    "I will give it to the guard.",
    "I am going to give the parcel to security.",
    "I haven't given it to the guard.",
    "I haven’t given it to the guard.",
    "I have not given it to the guard.",
    "I did not give it to the guard.",
    "I didn't give it to the guard.",
    "I did not deliver the parcel.",
    "I have not delivered the parcel.",
    "I haven't handed it to the guard.",
    "It has not been delivered.",
    "The parcel was not received.",
    "It wasn't delivered.",
    "The parcel wasn’t received.",
    "It isn't delivered.",
    "Security did not accept the parcel.",
    "Has security accepted the parcel?",
    "Did I give it to the guard?",
    "I gave it to the guard?",
    "If I gave it to the guard.",
    "If I delivered the parcel, what happens next?",
    "If I gave it to the guard, I will report it.",
    'Say "I gave it to the guard".',
    'The example says "Security accepted the parcel".',
    "Pretend I gave it to the guard.",
    "I gave a gift to my friend.",
    "I gave a gift to the guard.",
    "Security accepted my application.",
    "The guard received a phone call.",
    "The guard received a phone call about the parcel.",
    "Security accepted my application for parcel delivery.",
    "It has not been delivered yet.",
    "I might have delivered the parcel.",
]


@pytest.mark.parametrize("text", PREPAID_POSITIVE)
def test_prepaid_spellings_are_grounded(text):
    assert confirmation("prepaid", text, "prepaid") is True


@pytest.mark.parametrize("text", PREPAID_NEGATIVE)
def test_negative_payment_is_not_a_positive_spelling_match(text):
    assert confirmation("prepaid", text, "prepaid") is False


@pytest.mark.parametrize("text", PREPAID_UNCERTAIN)
def test_uncertain_future_and_question_payment_never_confirms(text):
    assert confirmation("prepaid", text, "prepaid") is None


@pytest.mark.parametrize("text", DELIVERED_REPORTS)
def test_completed_courier_report_wordings(text):
    assert is_completed_outcome(text, "delivered") is True


@pytest.mark.parametrize("text", UNCOMPLETED_DELIVERY)
def test_nonreports_never_record_delivery(text):
    assert is_completed_outcome(text, "delivered") is False


@pytest.mark.parametrize(
    ("text", "outcome"),
    [
        ("I returned the parcel.", "returned"),
        ("The parcel was returned.", "returned"),
        ("Returned.", "returned"),
        ("I could not deliver the parcel.", "could_not_deliver"),
        ("I couldn't deliver the parcel.", "could_not_deliver"),
        ("Couldn't deliver.", "could_not_deliver"),
    ],
)
def test_other_completed_outcomes_still_work(text, outcome):
    assert is_completed_outcome(text, outcome) is True


@pytest.mark.parametrize(
    ("text", "outcome"),
    [
        ("I returned the parcel.", "delivered"),
        ("Security accepted the parcel.", "returned"),
        ("I couldn't deliver the parcel.", "delivered"),
        ("I gave it to the guard.", "returned"),
        ("The parcel was delivered.", "could_not_deliver"),
        ("I will return the parcel.", "returned"),
        ("The parcel has not been returned.", "returned"),
        ("Was the parcel returned?", "returned"),
        ('Say "the parcel was returned".', "returned"),
        ("I returned a gift to my friend.", "returned"),
        ("I might not be able to deliver the parcel.", "could_not_deliver"),
        ("If I couldn't deliver the parcel, would it be returned?", "could_not_deliver"),
        ('Say "I could not deliver the parcel".', "could_not_deliver"),
    ],
)
def test_outcome_must_match_an_actual_report(text, outcome):
    assert is_completed_outcome(text, outcome) is False


class WordingProvider:
    """Test-only planner: deliberately propose unsafe actions to exercise the checker."""

    def __init__(self):
        self.plan = AgentPlan(action="handoff")
        self.calls = []

    def status(self):
        return ModelStatus(
            configured=True,
            model="test-only",
            provider="test-only",
            message="No network requests.",
            reserved_usd=0,
            budget_usd=0,
        )

    def generate(self, messages):
        self.calls.append(messages)
        return self.plan


@pytest.fixture
def wording_engine(tmp_path):
    now = datetime(2026, 10, 3, 9, tzinfo=UTC)
    dashboard = Dashboard(
        profile=ResidentProfile(
            resident_name="Fictional resident",
            pg_name="Fictional PG",
            guard_location="the fictional guard room",
            guard_directions="At the fictional entrance.",
        ),
        delivery_mode=DeliveryMode(enabled=True, expires_at=now + timedelta(hours=1)),
        context=DeliveryContext(
            availability=Availability.AT_OFFICE,
            availability_source="today",
            availability_explanation="Fictional test override.",
            today_override=Availability.AT_OFFICE,
            setup_complete=True,
            delivery_mode_active=True,
            delivery_mode_expired=False,
            instruction="Only an attended, prepaid handoff is permitted.",
            restrictions=[],
            local_date=now.date(),
        ),
        server_time=now,
    )
    store = ConversationStore(tmp_path)
    provider = WordingProvider()
    engine = ConversationEngine(store, provider, lambda: dashboard, lambda: now)
    return engine, provider, store


def courier_turn(engine, session, text):
    return engine.turn(session.id, TurnRequest(text=text, revision=session.revision))


def authorize_handoff(engine):
    session = courier_turn(engine, engine.start(), "It's prepaid. Security is here.")
    assert session.authorized_location == "the fictional guard room"
    return session


@pytest.mark.parametrize("text", ["Yes, it is pre paid.", "Yes, it is pre-paid."])
def test_engine_spelling_answer_advances_without_model_observation(wording_engine, text):
    engine, provider, store = wording_engine
    session = courier_turn(engine, engine.start(), "I have a delivery.")
    assert session.pending_question == "prepaid"
    session = courier_turn(engine, session, text)
    assert session.facts.prepaid is True
    assert session.pending_question == "guard_available"
    session = courier_turn(engine, session, "Yes he is there.")
    assert session.authorized_location == "the fictional guard room"
    assert store.read(session.id).facts.prepaid is True
    assert len(provider.calls) == 3


@pytest.mark.parametrize("text", PREPAID_UNCERTAIN)
def test_model_cannot_cherry_pick_positive_payment_from_uncertainty(wording_engine, text):
    engine, provider, _ = wording_engine
    evidence = "pre-paid" if "pre-paid" in text else "pre paid"
    provider.plan = AgentPlan(action="handoff", observation={"prepaid": True, "evidence": evidence})
    session = courier_turn(engine, engine.start(), text)
    assert session.facts.prepaid is None
    assert session.authorized_location is None
    assert session.status == "active"


def test_cash_on_delivery_fact_cannot_be_reflipped_by_new_spelling(wording_engine):
    engine, provider, store = wording_engine
    session = engine.start()
    # A legacy/current conversation can already have a sticky unpaid fact before a new turn.
    session.facts.prepaid = False
    store.write(session)
    provider.plan = AgentPlan(
        action="handoff", observation={"prepaid": True, "evidence": "pre paid"}
    )
    session = courier_turn(engine, session, "It is pre paid. Security is here.")
    assert session.facts.prepaid is False
    assert session.authorized_location is None
    assert session.status == "needs_resident"


def test_multisentence_quote_preserves_assertions_before_a_different_question(wording_engine):
    engine, provider, store = wording_engine
    text = "The stationery is paid for. No guard is present; could the manager’s office accept it?"
    provider.plan = AgentPlan(
        action="request_approval",
        proposed_location="manager’s office",
        observation={
            "prepaid": True,
            "guard_available": False,
            "evidence": "The stationery is paid for. No guard is present",
        },
    )
    session = courier_turn(engine, engine.start(), text)
    assert session.facts.prepaid is True
    assert session.facts.guard_available is False
    assert session.authorized_location is None
    assert session.status == "awaiting_approval"
    assert session.approval.location == "manager’s office"
    assert store.read(session.id).facts.prepaid is True


@pytest.mark.parametrize(
    "text",
    [
        "Maybe the guard is here.",
        "I am not sure the guard is here.",
        "I don't know whether the guard is here.",
        "Can you check if the guard is here?",
        "The guard is here?",
        "If the guard is here, can I leave it?",
    ],
)
def test_model_cannot_cherry_pick_guard_presence_from_uncertainty(wording_engine, text):
    engine, provider, store = wording_engine
    session = courier_turn(engine, engine.start(), "It's prepaid.")
    assert session.facts.prepaid is True
    assert session.pending_question == "guard_available"
    provider.plan = AgentPlan(
        action="handoff",
        observation={"guard_available": True, "evidence": "guard is here"},
    )
    session = courier_turn(engine, session, text)
    assert session.facts.guard_available is None
    assert session.authorized_location is None
    assert session.status == "active"
    assert store.read(session.id).facts.guard_available is None


@pytest.mark.parametrize("text", DELIVERED_REPORTS)
def test_engine_records_completed_report_but_not_independent_receipt(wording_engine, text):
    engine, provider, store = wording_engine
    session = authorize_handoff(engine)
    provider.plan = AgentPlan(action="record_outcome", outcome="delivered")
    session = courier_turn(engine, session, text)
    assert session.courier_reported_outcome == "delivered"
    assert session.status == "ended"
    assert session.events[-1].action == "record_call_outcome"
    assert "not been independently verified" in session.messages[-1].content
    assert store.read(session.id).courier_reported_outcome == "delivered"


@pytest.mark.parametrize("text", UNCOMPLETED_DELIVERY)
def test_engine_refuses_unsafe_model_outcome_proposal(wording_engine, text):
    engine, provider, store = wording_engine
    session = authorize_handoff(engine)
    provider.plan = AgentPlan(action="record_outcome", outcome="delivered")
    session = courier_turn(engine, session, text)
    assert session.courier_reported_outcome is None
    assert session.status == "active"
    assert session.events[-1].action == "clarify"
    assert store.read(session.id).courier_reported_outcome is None


def test_actual_report_does_not_invent_an_authorized_handoff(wording_engine):
    engine, provider, _ = wording_engine
    provider.plan = AgentPlan(action="record_outcome", outcome="delivered")
    session = courier_turn(engine, engine.start(), "I gave it to the guard. Fine?")
    assert session.courier_reported_outcome is None
    assert session.authorized_location is None
    assert session.status == "needs_resident"
    assert "cannot confirm an authorized handoff" in session.messages[-1].content


def test_engine_rejects_model_outcome_mismatch(wording_engine):
    engine, provider, _ = wording_engine
    session = authorize_handoff(engine)
    provider.plan = AgentPlan(action="record_outcome", outcome="delivered")
    session = courier_turn(engine, session, "I returned the parcel.")
    assert session.courier_reported_outcome is None
    assert session.status == "active"
    assert session.events[-1].action == "clarify"
