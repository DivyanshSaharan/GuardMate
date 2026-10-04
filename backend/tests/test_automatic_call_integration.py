"""Automatic wireflow with TestClient and fake model/speech/phone dependencies."""

import json

import pytest
from guardmate.agent.models import AgentPlan
from guardmate.cellular.automatic_call import AutomaticCallCoordinator
from guardmate.cellular.call_errors import CallError
from test_manual_call_integration import PHONE_INPUT, PROFILE, synthetic_wav
from test_manual_call_integration import harness as harness


class Listener:
    def __init__(self):
        self.calls = 0

    def listen(self, device, *, on_armed=None):
        assert device == PHONE_INPUT
        self.calls += 1
        if on_armed:
            on_armed()
        return synthetic_wav()


def automatic(harness, *, clock=lambda: 0):
    listener = Listener()
    coordinator = AutomaticCallCoordinator(
        harness.runner,
        audio=listener,
        automatic_submission_consent=True,
        emit=harness.events.append,
        clock=clock,
    )
    return coordinator, listener


def test_automatic_prepaid_yes_to_guard_and_delivered_full_wireflow(harness):
    harness.speech.transcripts = ["Yes, it is prepaid.", "Yes", "Security accepted the parcel."]

    def choose_plan():
        text = harness.provider.prompts[-1][-1]["content"]
        harness.provider.plan = (
            AgentPlan(action="record_outcome", outcome="delivered")
            if text == "Security accepted the parcel."
            else AgentPlan(action="handoff")
        )

    harness.provider.on_generate = choose_plan
    coordinator, listener = automatic(harness)
    result = coordinator.run("Fictional automatic courier")
    assert result.status == "ended" and result.model_attempts == 3
    assert harness.runner.session.facts.prepaid is True
    assert harness.runner.session.facts.guard_available is True
    assert harness.runner.session.courier_reported_outcome == "delivered"
    assert len(harness.audio.plays) == 4 and listener.calls == 3
    assert len(harness.provider.prompts) == len(harness.wire.turns()) == 3
    assert [json.loads(item[2])["text"] for item in harness.wire.turns()] == [
        "Yes, it is prepaid.",
        "Yes",
        "Security accepted the parcel.",
    ]
    assert all("expected_context" in json.loads(item[2]) for item in harness.wire.turns())
    assert not any(item[0] == "PUT" for item in harness.wire.requests)
    assert not harness.runner.stopped and harness.runner.admission_check is None


def test_automatic_approval_pause_keeps_pending_approval_in_real_saved_session(harness):
    text = "It's prepaid. The guard isn't here. Can I leave it at reception?"
    harness.speech.transcripts = [text]
    harness.provider.plan = AgentPlan(
        action="request_approval",
        proposed_location="reception",
        reason="Security unavailable.",
        observation={
            "prepaid": True,
            "guard_available": False,
            "evidence": "It's prepaid. The guard isn't here.",
        },
    )
    coordinator, listener = automatic(harness)
    result = coordinator.run("Fictional automatic courier")
    saved = harness.backend.read(result.session_id)
    assert result.status == "paused" and result.reason == "awaiting_approval"
    assert saved.status == "awaiting_approval" and saved.approval.status == "pending"
    assert saved.authorized_location is None and saved.approval.location == "reception"
    assert listener.calls == 1 and len(harness.audio.plays) == 2
    assert not any(item[0] == "PUT" for item in harness.wire.requests)


def test_automatic_stale_context_at_wire_admission_has_no_model_or_extra_play(harness):
    harness.speech.transcripts = ["It is prepaid. Security is here."]

    def change_before_turn(method, path):
        if method == "POST" and path.endswith("/turns"):
            harness.wire.before_dispatch = None
            harness.change_location()

    harness.wire.before_dispatch = change_before_turn
    coordinator, listener = automatic(harness)
    with pytest.raises(CallError, match="Backend state changed"):
        coordinator.run("Fictional automatic courier")
    saved = harness.backend.read(harness.runner.session.id)
    assert saved.revision == saved.turn_count == 0 and saved.status == "active"
    assert listener.calls == 1 and not harness.provider.prompts
    assert len(harness.audio.plays) == 1 and not harness.runner.stopped


def test_automatic_deadline_during_fake_model_preserves_turn_and_no_new_speech(harness):
    state = {"now": 0}
    harness.speech.transcripts = ["It is prepaid. Security is here."]
    harness.provider.on_generate = lambda: state.update(now=180)
    coordinator, _ = automatic(harness, clock=lambda: state["now"])
    result = coordinator.run("Fictional automatic courier")
    saved = harness.backend.read(result.session_id)
    assert result.status == "deadline" and result.model_attempts == 1
    assert saved.revision == 1 and saved.authorized_location == PROFILE["guard_location"]
    assert len(harness.speech.syntheses) == len(harness.audio.plays) == 1
    assert not harness.runner.stopped


def test_automatic_lost_committed_response_never_resends_or_ends_session(harness):
    harness.speech.transcripts = ["It is prepaid. Security is here."]
    harness.wire.lose_turn_response = True
    coordinator, listener = automatic(harness)
    with pytest.raises(CallError) as caught:
        coordinator.run("Fictional automatic courier")
    assert caught.value.uncertain and harness.runner.uncertain
    saved = harness.backend.read(harness.runner.session.id)
    assert saved.revision == 1 and saved.status == "active"
    assert listener.calls == len(harness.wire.turns()) == len(harness.provider.prompts) == 1
    assert len(harness.audio.plays) == 1
    assert not any(item[0] == "PUT" for item in harness.wire.requests)
