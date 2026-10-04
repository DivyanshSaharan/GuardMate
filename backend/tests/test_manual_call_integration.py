"""In-process runner/client/API/engine wiring; no real calls or inference.

The HTTPConnection adapter dispatches exclusively through TestClient. Synthetic
WAVs stay in memory, the planner and speech dependencies are fakes, and importing
the application is guarded against dotenv loading and default persistent storage.
"""

import copy
import importlib
import io
import json
import wave
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import dotenv
import pytest
from fastapi.testclient import TestClient
from guardmate.agent.models import AgentPlan, ModelStatus
from guardmate.cellular.backend_client import BackendClient
from guardmate.cellular.call_audio import reply_audio
from guardmate.cellular.call_errors import CallError
from guardmate.cellular.call_runner import ManualCallRunner
from guardmate.cellular.windows_audio import AudioDevice, validate_audio

NOW = datetime(2026, 10, 5, 6, tzinfo=UTC)
PROFILE = {
    "resident_name": "Fictional resident",
    "pg_name": "Fictional PG",
    "guard_location": "the entrance guard room",
    "guard_directions": "Use the pedestrian gate.",
}
PHONE_INPUT = AudioDevice(25, "Input (vivo T2x 5G)", "input")
PHONE_OUTPUT = AudioDevice(24, "Output (vivo T2x 5G)", "output")


def synthetic_wav(rate=16000):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(b"\x00\x08\x00\xf8" * (rate // 10))
    return buffer.getvalue()


class FakeProvider:
    def __init__(self):
        self.plan = AgentPlan(action="handoff")
        self.prompts = []
        self.on_generate = None

    def status(self):
        return ModelStatus(
            configured=True,
            model="in-process-fake",
            provider="test",
            message="test only",
            reserved_usd=0,
            budget_usd=0.25,
        )

    def generate(self, messages):
        self.prompts.append(copy.deepcopy(messages))
        if self.on_generate:
            self.on_generate()
        return self.plan


class FakeSpeech:
    def __init__(self):
        self.transcripts = []
        self.transcriptions = []
        self.syntheses = []
        self.on_transcribe = None
        self.on_synthesize = None

    def status(self):
        return {"stt_ready": True, "tts_ready": True, "message": "fake", "max_seconds": 30}

    def transcribe(self, audio):
        rate, pcm = validate_audio(audio)
        assert rate == 16000
        self.transcriptions.append(audio)
        if self.on_transcribe:
            self.on_transcribe()
        assert self.transcripts, "Supply a fake local transcript before listen()"
        return {
            "text": self.transcripts.pop(0),
            "duration_ms": len(pcm) * 1000 // (rate * 2),
            "processing_ms": 0,
        }

    def synthesize(self, text):
        self.syntheses.append(text)
        if self.on_synthesize:
            self.on_synthesize()
        return synthetic_wav(22050)


class FakeAudio:
    def __init__(self):
        self.records = []
        self.plays = []

    def record(self, device, seconds):
        assert device == PHONE_INPUT
        self.records.append((device, seconds))
        return synthetic_wav()

    def play(self, device, audio):
        assert device == PHONE_OUTPUT
        rate, _ = validate_audio(audio)
        assert rate == 16000
        self.plays.append((device, audio))


class WireResponse:
    def __init__(self, response):
        self.status = response.status_code
        self.response = response
        self.data = response.content

    def getheader(self, name, default=None):
        return self.response.headers.get(name, default)

    def read(self, amount):
        return self.data[:amount]


class InProcessConnectionFactory:
    """Faithful byte-body HTTP boundary, without a listening server or sockets."""

    def __init__(self, api):
        self.api = api
        self.requests = []
        self.closed = 0
        self.before_dispatch = None
        self.lose_turn_response = False

    def __call__(self, host, port, *, timeout):
        assert (host, port, timeout) == ("127.0.0.1", 8765, 60)
        factory = self

        class Connection:
            def request(self, method, path, *, body, headers):
                self.arguments = (method, path, body, headers)
                factory.requests.append(self.arguments)

            def getresponse(self):
                method, path, body, headers = self.arguments
                if factory.before_dispatch:
                    factory.before_dispatch(method, path)
                response = factory.api.request(method, path, content=body, headers=headers)
                if factory.lose_turn_response and method == "POST" and path.endswith("/turns"):
                    factory.lose_turn_response = False
                    raise TimeoutError("Synthetic lost response after an in-process commit")
                return WireResponse(response)

            def close(self):
                factory.closed += 1

        return Connection()

    def turns(self):
        return [item for item in self.requests if item[0] == "POST" and item[1].endswith("/turns")]


@dataclass
class Harness:
    api: TestClient
    backend: BackendClient
    wire: InProcessConnectionFactory
    provider: FakeProvider
    speech: FakeSpeech
    audio: FakeAudio
    runner: ManualCallRunner
    events: list

    def new_runner(self):
        return ManualCallRunner(
            self.backend,
            self.audio,
            PHONE_INPUT,
            PHONE_OUTPUT,
            convert_reply=reply_audio,
            hosted_consent=True,
            emit=self.events.append,
        )

    def change_location(self):
        response = self.api.put(
            "/api/profile", json={**PROFILE, "guard_location": "a different guard room"}
        )
        assert response.status_code == 200


@pytest.fixture
def harness(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("An integration test attempted a real HTTP connection")

    monkeypatch.setattr("http.client.HTTPConnection", forbidden)
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: False)
    monkeypatch.setenv("GUARDMATE_DATA_DIR", str(tmp_path / "unused-module-default"))
    main = importlib.import_module("guardmate.main")
    monkeypatch.setattr(main, "load_dotenv", lambda *args, **kwargs: False)
    provider, speech, audio, events = FakeProvider(), FakeSpeech(), FakeAudio(), []
    application = main.create_app(
        tmp_path / "fixture-db", lambda: NOW, provider=provider, speech_service=speech
    )
    with TestClient(application) as api:
        assert api.put("/api/profile", json=PROFILE).status_code == 200
        assert (
            api.put(
                "/api/delivery-mode",
                json={"enabled": True, "expires_at": (NOW + timedelta(hours=8)).isoformat()},
            ).status_code
            == 200
        )
        wire = InProcessConnectionFactory(api)
        backend = BackendClient(connection_factory=wire)
        runner = ManualCallRunner(
            backend,
            audio,
            PHONE_INPUT,
            PHONE_OUTPUT,
            convert_reply=reply_audio,
            hosted_consent=True,
            emit=events.append,
        )
        yield Harness(api, backend, wire, provider, speech, audio, runner, events)
        assert wire.closed == len(wire.requests)


def capture(harness, text):
    harness.speech.transcripts.append(text)
    return harness.runner.listen()


def test_prepaid_guard_confirmed_and_reported_delivery_uses_real_wireflow(harness):
    greeting = harness.runner.start("Fictional courier")
    assert greeting.revision == 0 and len(harness.audio.plays) == 1
    assert not harness.provider.prompts and not harness.wire.turns()

    assert capture(harness, "Yes, it is prepaid.") == "Yes, it is prepaid."
    assert not harness.provider.prompts and not harness.wire.turns()
    prepaid = harness.runner.send()
    assert prepaid.facts.prepaid is True and prepaid.facts.guard_available is None
    assert prepaid.authorized_location is None and prepaid.pending_question == "guard_available"

    assert capture(harness, "Yes, security is here.") == "Yes, security is here."
    confirmed = harness.runner.send()
    assert confirmed.facts.guard_available is True
    assert confirmed.authorized_location == PROFILE["guard_location"]

    harness.provider.plan = AgentPlan(action="record_outcome", outcome="delivered")
    capture(harness, "Security accepted the parcel.")
    delivered = harness.runner.send()
    assert delivered.status == "ended" and delivered.courier_reported_outcome == "delivered"
    assert "not been independently verified" in delivered.messages[-1].content
    assert len(harness.provider.prompts) == harness.runner.model_attempts == 3
    assert len(harness.audio.records) == 3 and len(harness.audio.plays) == 4
    assert harness.speech.syntheses == [
        greeting.messages[-1].content,
        prepaid.messages[-1].content,
        confirmed.messages[-1].content,
        delivered.messages[-1].content,
    ]
    payloads = [json.loads(item[2]) for item in harness.wire.turns()]
    assert [item["revision"] for item in payloads] == [0, 1, 2]
    assert all(len(item["expected_context"]) == 64 for item in payloads)
    assert [item["text"] for item in payloads] == [
        "Yes, it is prepaid.",
        "Yes, security is here.",
        "Security accepted the parcel.",
    ]
    with pytest.raises(CallError, match="paused or ended"):
        harness.runner.listen()
    harness.runner.stop()
    assert harness.events[-1] == {"event": "stopped", "physical_call_ended": False}


def test_changed_settings_after_review_rejects_stale_yes_before_model_or_wire_turn(harness):
    original = harness.runner.start("Fictional courier")
    capture(harness, "yes")
    harness.change_location()
    with pytest.raises(CallError, match="Delivery context changed"):
        harness.runner.send()
    assert harness.runner.draft == "yes"
    assert not harness.provider.prompts and not harness.wire.turns()
    assert len(harness.audio.plays) == 1
    saved = harness.backend.read(original.id)
    assert saved.revision == saved.turn_count == 0
    assert saved.messages == original.messages


def test_changed_settings_during_local_transcription_discards_capture(harness):
    harness.runner.start("Fictional courier")
    harness.speech.on_transcribe = harness.change_location
    with pytest.raises(CallError, match="context changed during capture"):
        capture(harness, "yes")
    assert harness.runner.draft is None
    assert len(harness.audio.records) == 1 and len(harness.audio.plays) == 1
    assert len(harness.speech.transcriptions) == 1
    assert not harness.provider.prompts and not harness.wire.turns()


def test_last_moment_context_change_is_rejected_at_real_api_admission(harness):
    original = harness.runner.start("Fictional courier")
    capture(harness, "yes")
    capture_token = harness.runner._draft_context

    def change_before_turn(method, path):
        if method == "POST" and path.endswith("/turns"):
            harness.wire.before_dispatch = None
            harness.change_location()

    harness.wire.before_dispatch = change_before_turn
    with pytest.raises(CallError, match="Backend state changed") as caught:
        harness.runner.send()
    assert not caught.value.uncertain and not harness.runner.uncertain
    assert len(harness.wire.turns()) == 1 and not harness.provider.prompts
    assert json.loads(harness.wire.turns()[0][2])["expected_context"] == capture_token
    assert len(harness.audio.plays) == 1
    saved = harness.backend.read(original.id)
    assert saved.revision == saved.turn_count == 0 and saved.messages == original.messages


def test_context_change_during_fake_planning_returns_only_checked_takeover(harness):
    harness.runner.start("Fictional courier")
    capture(harness, "It is prepaid. Security is here.")
    harness.provider.on_generate = harness.change_location
    updated = harness.runner.send()
    assert updated.status == "needs_resident" and updated.authorized_location is None
    assert updated.events[-1].action == "request_takeover"
    assert "delivery plan changed" in updated.messages[-1].content
    assert harness.speech.syntheses[-1] == updated.messages[-1].content
    assert len(harness.provider.prompts) == 1 and len(harness.audio.plays) == 2
    with pytest.raises(CallError, match="paused or ended"):
        harness.runner.listen()


def test_real_speech_route_rejects_context_change_during_fake_synthesis(harness):
    harness.runner.start("Fictional courier")
    capture(harness, "It is prepaid. Security is here.")
    harness.speech.on_synthesize = harness.change_location
    with pytest.raises(CallError, match="Backend state changed"):
        harness.runner.send()
    assert len(harness.provider.prompts) == 1 and len(harness.speech.syntheses) == 2
    assert len(harness.audio.plays) == 1
    assert harness.runner.session.revision == 1 and harness.runner.draft is None


def test_final_revalidation_after_conversion_prevents_native_playback(harness):
    harness.runner.start("Fictional courier")
    capture(harness, "It is prepaid. Security is here.")

    def convert_then_change(audio):
        converted = reply_audio(audio)
        harness.change_location()
        return converted

    harness.runner.convert_reply = convert_then_change
    with pytest.raises(CallError, match="saved reply or delivery context changed"):
        harness.runner.send()
    assert len(harness.provider.prompts) == 1 and len(harness.speech.syntheses) == 2
    assert len(harness.audio.plays) == 1


def test_fresh_second_runner_has_no_previous_courier_facts_or_handoff(harness):
    first = harness.runner.start("Courier A")
    capture(harness, "It is prepaid. Security is here.")
    assert harness.runner.send().authorized_location == PROFILE["guard_location"]
    harness.runner.stop()
    second_runner = harness.new_runner()
    second = second_runner.start("Courier B")
    assert second.id != first.id and second.revision == second.turn_count == 0
    assert second.facts.prepaid is None and second.facts.guard_available is None
    assert second.authorized_location is None and second.approval is None
    assert len(second.messages) == 1
    assert "Courier A" not in second.model_dump_json()
    assert harness.backend.read(first.id).status == "ended"
    assert len(harness.provider.prompts) == 1
    second_runner.stop()


def test_committed_turn_with_lost_response_is_latched_without_resend(harness):
    original = harness.runner.start("Fictional courier")
    capture(harness, "It is prepaid. Security is here.")
    harness.wire.lose_turn_response = True
    with pytest.raises(CallError) as caught:
        harness.runner.send()
    assert caught.value.uncertain and harness.runner.uncertain
    assert len(harness.provider.prompts) == len(harness.wire.turns()) == 1
    assert harness.backend.read(original.id).revision == 1
    assert len(harness.audio.plays) == 1
    for action in (harness.runner.send, harness.runner.listen):
        with pytest.raises(CallError, match="uncertain result"):
            action()
    assert len(harness.provider.prompts) == len(harness.wire.turns()) == 1
    harness.runner.stop()
    assert harness.backend.read(original.id).status == "ended"
    assert harness.runner.stopped
