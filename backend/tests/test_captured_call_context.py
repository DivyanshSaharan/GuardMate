"""Backend admission/post-planner guards for reviewed cellular transcripts."""

from datetime import UTC, datetime

import pytest
from fastapi import HTTPException
from guardmate.agent.engine import ConversationEngine, fingerprint
from guardmate.agent.models import AgentPlan, ModelStatus, TurnRequest
from guardmate.agent.store import ConversationStore
from guardmate.context import build_context
from guardmate.models import Dashboard, DeliveryMode, ResidentProfile, TodayOverride
from pydantic import ValidationError

NOW = datetime(2026, 10, 5, 6, 0, tzinfo=UTC)


class Provider:
    def __init__(self):
        self.calls = 0
        self.callback = None

    def status(self):
        return ModelStatus(
            configured=True,
            model="test",
            provider="test",
            message="test",
            reserved_usd=0,
            budget_usd=1,
        )

    def generate(self, messages):
        self.calls += 1
        if self.callback:
            self.callback()
        return AgentPlan(action="handoff")


@pytest.fixture
def rig(tmp_path):
    state = {"location": "the entrance guard room", "availability": "at_office", "active": True}

    def dashboard():
        profile = ResidentProfile(
            resident_name="Demo", pg_name="Demo PG", guard_location=state["location"]
        )
        mode = (
            DeliveryMode(enabled=True, expires_at=datetime(2026, 10, 5, 14, 0, tzinfo=UTC))
            if state["active"]
            else DeliveryMode()
        )
        override = TodayOverride(status=state["availability"], local_date=NOW.date())
        return Dashboard(
            profile=profile,
            delivery_mode=mode,
            context=build_context(profile, mode, override, NOW),
            server_time=NOW,
        )

    provider = Provider()
    engine = ConversationEngine(ConversationStore(tmp_path), provider, dashboard, lambda: NOW)
    session = engine.start("Fictional caller")
    return engine, provider, state, session


def test_matching_capture_context_can_use_checked_handoff(rig):
    engine, provider, _, session = rig
    updated = engine.turn(
        session.id,
        TurnRequest(
            revision=session.revision,
            text="It's prepaid. Security is here.",
            expected_context=fingerprint(engine.dashboard()),
        ),
    )
    assert provider.calls == 1 and updated.authorized_location == "the entrance guard room"
    assert updated.reply_context_fingerprint == fingerprint(engine.dashboard())


def test_stale_capture_rejected_before_history_counter_or_provider(rig):
    engine, provider, state, session = rig
    captured = fingerprint(engine.dashboard())
    state["location"] = "a different guard room"
    with pytest.raises(HTTPException) as error:
        engine.turn(
            session.id,
            TurnRequest(
                revision=session.revision,
                text="yes",
                expected_context=captured,
            ),
        )
    assert error.value.status_code == 409
    assert provider.calls == 0
    saved = engine.read(session.id)
    assert saved.messages == session.messages and saved.turn_count == 0 and saved.revision == 0


@pytest.mark.parametrize("change", ["location", "availability"])
def test_context_change_while_model_thinks_refuses_returned_handoff(rig, change):
    engine, provider, state, session = rig
    captured = fingerprint(engine.dashboard())
    provider.callback = lambda: state.update(
        {change: "a different room" if change == "location" else "at_pg"}
    )
    updated = engine.turn(
        session.id,
        TurnRequest(
            revision=session.revision,
            text="It's prepaid. Security is here.",
            expected_context=captured,
        ),
    )
    assert provider.calls == 1 and updated.status == "needs_resident"
    assert updated.authorized_location is None
    assert "delivery plan changed" in updated.messages[-1].content
    assert updated.events[-1].action == "request_takeover"
    assert updated.events[-1].model_action == "handoff"
    assert updated.reply_context_fingerprint == fingerprint(engine.dashboard())


def test_delivery_mode_expiry_during_model_work_still_blocks_handoff(rig):
    engine, provider, state, session = rig
    provider.callback = lambda: state.update(active=False)
    updated = engine.turn(
        session.id,
        TurnRequest(
            revision=session.revision,
            text="It's prepaid. Security is here.",
            expected_context=fingerprint(engine.dashboard()),
        ),
    )
    assert updated.status == "needs_resident" and updated.authorized_location is None
    assert "Delivery mode has ended" in updated.messages[-1].content


def test_legacy_text_requests_do_not_require_new_context_field(rig):
    engine, provider, _, session = rig
    request = TurnRequest(revision=session.revision, text="It's prepaid. Security is here.")
    assert request.expected_context is None
    assert engine.turn(session.id, request).authorized_location == "the entrance guard room"
    assert provider.calls == 1


def test_manual_reply_stamp_uses_checked_snapshot_not_later_settings(rig, monkeypatch):
    engine, _, state, session = rig
    original_execute = engine._execute
    captured = fingerprint(engine.dashboard())

    def change_after_check(session, plan, text, dashboard):
        original_execute(session, plan, text, dashboard)
        state["location"] = "a newly changed guard room"

    monkeypatch.setattr(engine, "_execute", change_after_check)
    updated = engine.turn(
        session.id,
        TurnRequest(
            revision=session.revision,
            text="It's prepaid. Security is here.",
            expected_context=captured,
        ),
    )
    assert updated.reply_context_fingerprint == captured
    assert updated.reply_context_fingerprint != fingerprint(engine.dashboard())
    # Existing speech freshness rejects this old-context reply; it cannot be
    # relabelled with the new settings and sent to the phone by the runner.


def test_mode_disabled_before_admission_has_no_model_or_history_side_effect(rig):
    engine, provider, state, session = rig
    captured = fingerprint(engine.dashboard())
    state["active"] = False
    with pytest.raises(HTTPException) as error:
        engine.turn(session.id, TurnRequest(text="yes", revision=0, expected_context=captured))
    assert error.value.status_code == 409 and provider.calls == 0
    saved = engine.read(session.id)
    assert saved.revision == 0 and saved.messages == session.messages and saved.turn_count == 0


@pytest.mark.parametrize("context", ["", "x" * 64, "a" * 63, "a" * 65, "A" * 64, True, 0])
def test_capture_context_schema_requires_exact_lowercase_sha256(context):
    with pytest.raises(ValidationError):
        TurnRequest(text="yes", revision=0, expected_context=context)
