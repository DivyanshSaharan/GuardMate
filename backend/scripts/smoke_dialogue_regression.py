"""Bounded live replay of the reported clarification loop, using fictional context only."""

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from dotenv import load_dotenv  # noqa: E402
from guardmate.agent.engine import ConversationEngine  # noqa: E402
from guardmate.agent.models import TurnRequest  # noqa: E402
from guardmate.agent.provider import TinkerProvider  # noqa: E402
from guardmate.agent.store import ConversationStore  # noqa: E402
from guardmate.context import build_context  # noqa: E402
from guardmate.models import Dashboard, DeliveryMode, ResidentProfile, TodayOverride  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--availability-only", action="store_true")
    arguments = parser.parse_args()
    if not arguments.live:
        parser.error("Use --live to explicitly allow credit-consuming requests.")
    load_dotenv(ROOT / ".env", override=False)
    now = datetime.now(UTC)
    profile = ResidentProfile(
        resident_name="Demo resident",
        pg_name="Demo PG",
        guard_location="the guard room beside the main entrance",
    )
    mode = DeliveryMode(enabled=True, expires_at=now + timedelta(hours=1))
    override = TodayOverride(
        status="at_office", local_date=now.astimezone(ZoneInfo("Asia/Kolkata")).date()
    )

    def dashboard():
        instant = datetime.now(UTC)
        return Dashboard(
            profile=profile,
            delivery_mode=mode,
            context=build_context(profile, mode, override, instant),
            server_time=instant,
        )

    store = ConversationStore(ROOT / ".data")
    provider = TinkerProvider(store)
    engine = ConversationEngine(store, provider, dashboard, lambda: datetime.now(UTC))
    if arguments.availability_only:
        override.status = "at_pg"
        for _ in range(3):
            available = engine.start()
            for text in ["I have an order to deliver", "Are you available?"]:
                available = engine.turn(
                    available.id, TurnRequest(text=text, revision=available.revision)
                )
                event = available.events[-1]
                print(
                    json.dumps(
                        {
                            "courier": text,
                            "reply": available.messages[-1].content,
                            "model_action": event.model_action,
                            "executed_action": event.action,
                            "validation_errors": provider.validation_errors,
                        }
                    ),
                    flush=True,
                )
                assert event.action != "model_unavailable", event.detail
                assert available.authorized_location is None
        print(json.dumps({"availability_passed": True}), flush=True)
        return
    session = engine.start()
    observations = []
    for index, text in enumerate(
        [
            "I have an order to deliver",
            "yes",
            "don't know",
            "let me check",
            "yes",
            "yes he is there",
            "yes it is prepaid",
            "Security received the parcel. I handed it over.",
        ]
    ):
        session = engine.turn(session.id, TurnRequest(text=text, revision=session.revision))
        event = session.events[-1]
        observation = {
            "courier": text,
            "reply": session.messages[-1].content,
            "model_action": event.model_action,
            "executed_action": event.action,
            "facts": session.facts.model_dump(),
            "latency_ms": event.latency_ms,
        }
        observations.append(observation)
        print(json.dumps(observation), flush=True)
        assert event.action != "model_unavailable", event.detail
        if index == 1:
            assert session.facts.prepaid is True
        if index in (2, 3):
            assert event.action == "wait_for_courier"
            assert session.facts.guard_available is None
        if index == 4:
            assert session.authorized_location == profile.guard_location
        if index >= 4:
            assert "Is this parcel already paid for?" not in session.messages[-1].content
            assert "Is security there to accept" not in session.messages[-1].content
    assert session.courier_reported_outcome == "delivered"

    pronoun = engine.start()
    for text in ["It's prepaid", "let me check", "yes he is there"]:
        pronoun = engine.turn(pronoun.id, TurnRequest(text=text, revision=pronoun.revision))
        assert pronoun.events[-1].action != "model_unavailable"
    assert pronoun.authorized_location == profile.guard_location
    report = {
        "model": provider.status().model,
        "passed": True,
        "reserved_usd_total": provider.status().reserved_usd,
        "replay": observations,
        "pronoun_session": pronoun.model_dump(mode="json"),
    }
    (ROOT / ".data" / "qwen-dialogue-regression.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    print(
        json.dumps({"passed": True, "reserved_usd_total": provider.status().reserved_usd}),
        flush=True,
    )


if __name__ == "__main__":
    try:
        main()
    except AssertionError as error:
        print(f"Dialogue regression failed: {error}", flush=True)
        sys.exit(1)
