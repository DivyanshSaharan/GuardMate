"""Desktop phone tests use fake devices, speech and planning only."""

import base64
import io
import threading
import wave
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from guardmate.agent.engine import ConversationEngine
from guardmate.agent.models import AgentPlan, ModelStatus
from guardmate.agent.store import ConversationStore
from guardmate.cellular.kernel_audio import WindowsKernelAudio
from guardmate.cellular.windows_audio import AudioError, PairedAudioDevice
from guardmate.context import build_context
from guardmate.desktop.audio import BoundPhoneAudio
from guardmate.desktop.audio_worker import handle
from guardmate.desktop.routes import DesktopCalls, build_desktop_router
from guardmate.models import Dashboard, DeliveryMode, ResidentProfile

TOKEN = "d" * 64
SOURCE = PairedAudioDevice(10, "Input (Pixel 9)", "input", "Windows WDM-KS")
TARGET = PairedAudioDevice(11, "Output (Pixel 9)", "output", "Windows WDM-KS")


def wav(seconds=5):
    output = io.BytesIO()
    with wave.open(output, "wb") as recording:
        recording.setnchannels(1)
        recording.setsampwidth(2)
        recording.setframerate(16000)
        recording.writeframes(b"\x00\x01\x00\xff" * (8000 * seconds))
    return output.getvalue()


class FakePlanner:
    def __init__(self):
        self.calls = 0
        self.on_generate = None

    def status(self):
        return ModelStatus(
            configured=True,
            model="fake",
            provider="fake",
            message="fake",
            reserved_usd=0,
            budget_usd=0.25,
        )

    def generate(self, messages):
        self.calls += 1
        if self.on_generate:
            self.on_generate()
        return AgentPlan(action="clarify", question="prepaid")


class FakeSpeech:
    def __init__(self):
        self.calls = []

    def status(self):
        return {"stt_ready": True, "tts_ready": True}

    def transcribe(self, audio):
        self.calls.append("transcribe")
        return {"text": "Where should I leave this parcel?"}

    def synthesize(self, text):
        self.calls.append("synthesize")
        return wav(1)


class FakePhoneAudio:
    def __init__(self):
        self.inventory = [SOURCE, TARGET]
        self.operations = []
        self.cancelled = False

    def reset(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True

    def devices(self):
        return self.inventory.copy()

    def bind(self, name, source, target):
        return BoundPhoneAudio(self, name, source, target)

    def _invoke(self, request, timeout):
        assert request["endpoint"] in [asdict(device) for device in self.inventory]
        self.operations.append(request["action"])
        if request["action"] == "record":
            return {
                "ok": True,
                "endpoint": request["endpoint"],
                "audio": base64.b64encode(wav(request["seconds"])).decode(),
            }
        return {"ok": True, "endpoint": request["endpoint"]}


def start_request(**changes):
    return {
        "phone_name": "Pixel 9",
        "input": asdict(SOURCE),
        "output": asdict(TARGET),
        "label": "Fictional courier",
        "seconds": 5,
        "max_turns": 5,
        "call_answered": True,
        "consenting_call": True,
        "hosted_text_consent": True,
        "exclusive_audio_risk": True,
        "phone_endpoints_confirmed": True,
        **changes,
    }


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    for name in (
        "MONGODB_URI",
        "RENDER",
        "TINKER_API_KEY",
        "GUARDMATE_ACCESS_USERNAME",
        "GUARDMATE_ACCESS_PASSWORD",
        "GUARDMATE_SAMPLER_CHECKPOINT",
    ):
        monkeypatch.setenv(name, "")
    now = datetime(2026, 10, 5, 6, tzinfo=UTC)
    profile = ResidentProfile(resident_name="Demo", pg_name="Demo PG", guard_location="Guard room")
    mode = DeliveryMode(enabled=True, expires_at=now + timedelta(hours=1))

    def dashboard():
        return Dashboard(
            profile=profile,
            delivery_mode=mode,
            context=build_context(profile, mode, None, now),
            server_time=now,
        )

    planner, speech, audio = FakePlanner(), FakeSpeech(), FakePhoneAudio()
    engine = ConversationEngine(ConversationStore(tmp_path), planner, dashboard, lambda: now)
    calls = DesktopCalls(engine, speech, tmp_path, audio=audio, phone_detector=lambda: True)
    app = FastAPI()
    app.include_router(
        build_desktop_router(engine, speech, tmp_path, token=TOKEN, controller=calls)
    )
    with TestClient(
        app,
        base_url="http://127.0.0.1:8765",
        client=("127.0.0.1", 5100),
        headers={"Authorization": f"Bearer {TOKEN}"},
    ) as client:
        yield client, calls, planner, speech, audio, profile


def test_inspection_never_opens_audio_or_requests_paid_planning(rig):
    client, _, planner, speech, audio, _ = rig
    status = client.get("/api/desktop/status")
    assert status.status_code == 200
    assert status.json()["physical_call_state_verified"] is False
    inventory = client.post("/api/desktop/devices/scan")
    assert inventory.status_code == 200 and inventory.json()["audio_stream_started"] is False
    assert inventory.json()["devices"] == [asdict(SOURCE), asdict(TARGET)]
    assert planner.calls == 0 and speech.calls == [] and audio.operations == []


@pytest.mark.parametrize(
    "confirmation",
    [
        "call_answered",
        "consenting_call",
        "hosted_text_consent",
        "exclusive_audio_risk",
        "phone_endpoints_confirmed",
    ],
)
def test_every_informed_confirmation_is_required_before_a_session_or_audio(rig, confirmation):
    client, calls, planner, _, audio, _ = rig
    assert (
        client.post(
            "/api/desktop/call/start", json=start_request(**{confirmation: False})
        ).status_code
        == 422
    )
    assert calls.runner is None and planner.calls == 0 and audio.operations == []


def test_real_manual_flow_uses_fresh_session_and_separate_review_send_and_play(rig):
    client, _, planner, speech, audio, _ = rig
    first = client.post("/api/desktop/call/start", json=start_request())
    assert first.status_code == 200
    first_id = first.json()["conversation"]["id"]
    assert planner.calls == 0 and audio.operations == [] and speech.calls == []
    assert client.post("/api/desktop/call/start", json=start_request()).status_code == 409
    captured = client.post("/api/desktop/call/capture")
    assert captured.status_code == 200
    assert captured.json()["draft"] == "Where should I leave this parcel?"
    assert planner.calls == 0 and speech.calls == ["transcribe"]
    sent = client.post("/api/desktop/call/send", json={"text": "Where is the guard room?"})
    assert sent.status_code == 200 and sent.json()["draft"] is None
    assert any(
        message["content"] == "Where is the guard room?"
        for message in sent.json()["conversation"]["messages"]
    )
    assert planner.calls == 1 and audio.operations == ["record"]
    assert client.post("/api/desktop/call/play", json={"repeat": False}).status_code == 200
    assert audio.operations == ["record", "play"]
    assert client.post("/api/desktop/call/play", json={"repeat": False}).status_code == 409
    assert client.post("/api/desktop/call/play", json={"repeat": True}).status_code == 200
    stopped = client.post("/api/desktop/call/stop")
    assert stopped.status_code == 200 and stopped.json()["stopped"]
    assert not stopped.json()["physical_call_ended"]
    assert audio.cancelled
    assert client.post("/api/desktop/call/capture").status_code == 409
    next_call = client.post("/api/desktop/call/start", json=start_request())
    assert next_call.status_code == 200
    assert next_call.json()["conversation"]["id"] != first_id


def test_changed_endpoint_never_falls_back_to_laptop_audio(rig):
    client, calls, planner, _, audio, _ = rig
    audio.inventory = [
        PairedAudioDevice(10, "Laptop microphone", "input", "Windows WDM-KS"),
        TARGET,
    ]
    response = client.post("/api/desktop/call/start", json=start_request())
    assert response.status_code == 409
    assert calls.runner is None and planner.calls == 0 and audio.operations == []


def test_wrong_phone_name_and_host_api_are_refused(rig):
    client, _, _, _, audio, _ = rig
    assert (
        client.post("/api/desktop/call/start", json=start_request(phone_name="Laptop")).status_code
        == 409
    )
    wrong = asdict(SOURCE) | {"host_api": "Windows WASAPI"}
    assert (
        client.post("/api/desktop/call/start", json=start_request(input=wrong)).status_code == 422
    )
    assert audio.operations == []


def test_changed_policy_rejects_captured_draft_before_any_model_request(rig):
    client, _, planner, _, _, profile = rig
    assert client.post("/api/desktop/call/start", json=start_request()).status_code == 200
    assert client.post("/api/desktop/call/capture").status_code == 200
    profile.guard_location = "Changed guard room"
    response = client.post("/api/desktop/call/send", json={"text": "Where is security?"})
    assert response.status_code == 409 and planner.calls == 0


def test_stop_during_hosted_turn_prevents_playback_without_resending(rig):
    client, _, planner, speech, audio, _ = rig
    entered, release = threading.Event(), threading.Event()

    def block():
        entered.set()
        assert release.wait(5)

    planner.on_generate = block
    client.post("/api/desktop/call/start", json=start_request())
    client.post("/api/desktop/call/capture")
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(
            client.post, "/api/desktop/call/send", json={"text": "Where is security?"}
        )
        assert entered.wait(5)
        stopping = client.post("/api/desktop/call/stop")
        assert stopping.status_code == 200 and stopping.json()["stopping"]
        assert audio.cancelled
        release.set()
        assert pending.result(timeout=5).json()["stopped"]
    assert planner.calls == 1 and speech.calls == ["transcribe"]
    assert audio.operations == ["record"]


def test_local_token_cookie_and_origin_guards(rig):
    client, _, _, _, _, _ = rig
    assert client.get("/api/desktop/status", headers={"Authorization": ""}).status_code == 401
    client.cookies.set("guardmate_local", TOKEN)
    assert client.get("/api/desktop/status", headers={"Authorization": ""}).status_code == 200
    assert (
        client.post(
            "/api/desktop/devices/scan", headers={"Origin": "https://attacker.test"}
        ).status_code
        == 403
    )
    assert client.get("/api/desktop/status", headers={"Host": "attacker.test"}).status_code == 403


def test_audio_worker_revalidates_non_vivo_identity_before_opening_any_stream():
    class MetadataOnlyAudio:
        def paired_devices(self):
            return [SOURCE, TARGET]

        def validate_paired(self, endpoint):
            raise AudioError("Endpoint disconnected")

        def record(self, *args):
            pytest.fail("A disconnected endpoint must never be recorded")

    audio = MetadataOnlyAudio()
    assert handle({"version": 1, "action": "list"}, audio=audio)["ok"]
    result = handle(
        {
            "version": 1,
            "action": "record",
            "phone_name": "Pixel 9",
            "endpoint": asdict(SOURCE),
            "seconds": 5,
        },
        audio=audio,
    )
    assert not result["ok"] and "disconnected" not in result["message"]


def test_kernel_metadata_has_full_identity_and_never_constructs_streams():
    class FakePortAudio:
        def query_hostapis(self):
            return [{"name": "Windows WDM-KS"}]

        def query_devices(self):
            return [
                {
                    "name": "Headset (Pixel 9)",
                    "hostapi": 0,
                    "max_input_channels": 1,
                    "max_output_channels": 1,
                }
            ]

    audio = WindowsKernelAudio(sounddevice=FakePortAudio())
    pins = audio.paired_devices()
    assert pins == [
        PairedAudioDevice(0, "Headset (Pixel 9)", "input", "Windows WDM-KS"),
        PairedAudioDevice(0, "Headset (Pixel 9)", "output", "Windows WDM-KS"),
    ]
    with pytest.raises(AudioError):
        audio.validate_paired(PairedAudioDevice(0, "Headset (Pixel 9)", "input", "Windows WASAPI"))
