import io
import math
import struct
import subprocess
import sys
import types
import wave
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from guardmate.agent.engine import ConversationEngine, fingerprint
from guardmate.agent.models import AgentPlan, Conversation, Message, ModelStatus, TurnRequest
from guardmate.agent.provider import ModelUnavailable
from guardmate.agent.store import ConversationStore
from guardmate.context import build_context
from guardmate.models import Dashboard, DeliveryMode, ResidentProfile, TodayOverride
from guardmate.speech.routes import build_speech_router
from guardmate.speech.service import (
    MAX_TTS_BYTES,
    MAX_WAV_BYTES,
    SPEECH_TIMEOUT,
    SpeechError,
    SpeechService,
    _subprocess_environment,
    validate_recording,
)
from guardmate.speech.worker import BoundedAudioFile, synthesize


def recording(seconds=0.2, *, sample_rate=16000, channels=1, width=2, silent=False):
    frames = round(seconds * sample_rate)
    data = (
        b"\x00" * frames * channels * width
        if silent
        else b"".join(
            struct.pack("<h", round(4000 * math.sin(index / 10)))
            for index in range(frames * channels)
        )
    )
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(width)
        wav.setframerate(sample_rate)
        wav.writeframes(data)
    return output.getvalue()


@pytest.fixture
def service(tmp_path):
    whisper_cli = tmp_path / "fake whisper cli.exe"
    whisper_model = tmp_path / "fake whisper.bin"
    piper_model = tmp_path / "fake voice.onnx"
    # These fake assets never run or load: tests replace the process/Piper API boundary.
    for path in (whisper_cli, whisper_model, piper_model):
        path.touch()
    Path(str(piper_model) + ".json").write_text(
        '{"phoneme_type":"espeak", "espeak":{"voice":"en-us"}}', encoding="utf-8"
    )
    return SpeechService(
        tmp_path,
        whisper_cli=whisper_cli,
        whisper_model=whisper_model,
        piper_model=piper_model,
        piper_available=lambda: True,
    )


def test_recording_duration_and_exact_maximum():
    assert validate_recording(recording()) == 200
    assert validate_recording(recording(30)) == 30000


@pytest.mark.parametrize(
    "audio",
    [
        b"",
        b"not wav",
        recording(silent=True),
        recording(0),
        recording(channels=2),
        recording(sample_rate=44100),
        recording(width=1),
        recording()[:-2],
        b"RIFF" + b"\0" * 100,
    ],
    ids=[
        "empty",
        "non-wav",
        "silent",
        "no-frames",
        "stereo",
        "44k",
        "8bit",
        "truncated",
        "bad-riff",
    ],
)
def test_rejects_invalid_empty_and_silent_audio(audio):
    with pytest.raises(SpeechError) as caught:
        validate_recording(audio)
    assert caught.value.status_code == 422


def test_rejects_dc_offset_silence():
    audio = bytearray(recording(silent=True))
    audio[44:] = struct.pack("<h", 5000) * ((len(audio) - 44) // 2)
    with pytest.raises(SpeechError, match="silent"):
        validate_recording(bytes(audio))


def test_recording_duration_and_body_size_are_bounded():
    for audio in (recording(30.01), b"x" * (MAX_WAV_BYTES + 1)):
        with pytest.raises(SpeechError) as caught:
            validate_recording(audio)
        assert caught.value.status_code == 413


@pytest.mark.parametrize(
    "offset, value, packing", [(28, 64000, "<I"), (32, 4, "<H"), (20, 3, "<H")]
)
def test_inconsistent_wav_header_is_rejected(offset, value, packing):
    audio = bytearray(recording())
    struct.pack_into(packing, audio, offset, value)
    with pytest.raises(SpeechError) as caught:
        validate_recording(bytes(audio))
    assert caught.value.status_code == 422


def test_duplicate_data_chunk_is_rejected():
    audio = bytearray(recording())
    audio.extend(b"data\x02\0\0\0\0\0")
    struct.pack_into("<I", audio, 4, len(audio) - 8)
    with pytest.raises(SpeechError) as caught:
        validate_recording(bytes(audio))
    assert caught.value.status_code == 422


def test_bounded_wav_metadata_chunk_is_supported():
    original = recording()
    audio = bytearray(original[:12] + b"JUNK\x02\0\0\0\0\0" + original[12:])
    struct.pack_into("<I", audio, 4, len(audio) - 8)
    assert validate_recording(bytes(audio)) == 200


def test_status_has_fixed_contract_and_hides_paths(service):
    status = service.status()
    assert set(status) == {"stt_ready", "tts_ready", "max_seconds", "message"}
    assert status["stt_ready"] is True and status["tts_ready"] is True
    assert status["max_seconds"] == 30
    assert str(service.whisper_cli) not in status["message"]
    service.whisper_cli.unlink()
    assert service.status()["stt_ready"] is False
    service._piper_available = lambda: False
    assert service.status()["tts_ready"] is False


def test_local_default_paths_and_environment_config(tmp_path, monkeypatch):
    for name in ("GUARDMATE_WHISPER_CLI", "GUARDMATE_WHISPER_MODEL", "GUARDMATE_PIPER_MODEL"):
        monkeypatch.delenv(name, raising=False)
    default = SpeechService(tmp_path, piper_available=lambda: False)
    assert default.whisper_model == tmp_path / "models/speech/ggml-base.en.bin"
    assert default.piper_model == tmp_path / "models/speech/en_US-ljspeech-high.onnx"
    explicit = tmp_path / "configured-cli"
    monkeypatch.setenv("GUARDMATE_WHISPER_CLI", str(explicit))
    assert SpeechService(tmp_path).whisper_cli == explicit


def test_children_do_not_receive_model_credentials(monkeypatch):
    monkeypatch.setenv("TINKER_API_KEY", "fake-private-key")
    monkeypatch.setenv("UNRELATED_SECRET", "fake-secret")
    monkeypatch.setenv("WHISPER_ARG_DEVICE", "999")
    monkeypatch.setenv("PYTHONPATH", "untrusted-module-path")
    env = _subprocess_environment()
    assert "TINKER_API_KEY" not in env
    assert "UNRELATED_SECRET" not in env
    assert "WHISPER_ARG_DEVICE" not in env
    assert "PYTHONPATH" not in env
    assert env["OMP_NUM_THREADS"] == "4"


def whisper_result(monkeypatch, text):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        transcript = Path(command[command.index("-of") + 1]).with_suffix(".txt")
        transcript.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", run)
    return calls


def test_transcription_uses_only_local_cli_and_deletes_all_temporary_files(service, monkeypatch):
    calls = whisper_result(monkeypatch, "  It is prepaid.\nSecurity is here.  ")
    response = service.transcribe(recording())
    assert response["text"] == "It is prepaid. Security is here."
    assert response["duration_ms"] == 200
    assert response["processing_ms"] >= 0
    command, options = calls[0]
    assert command[0] == str(service.whisper_cli.resolve())
    assert "-ng" in command and command[command.index("-l") + 1] == "en"
    assert options["timeout"] == SPEECH_TIMEOUT
    assert options["stdout"] == options["stderr"] == subprocess.DEVNULL
    assert options["stdin"] == subprocess.DEVNULL
    assert not options["cwd"].exists()
    assert "shell" not in options


@pytest.mark.parametrize(
    "text",
    ["", "[BLANK_AUDIO]", "(silence)", "x" * 601, b"x" * 4097],
    ids=["empty", "blank", "silence", "long-text", "long-file"],
)
def test_no_empty_or_unbounded_transcript_is_returned(service, monkeypatch, text):
    calls = whisper_result(monkeypatch, text)
    with pytest.raises(SpeechError) as caught:
        service.transcribe(recording())
    assert caught.value.status_code == 422
    assert not calls[0][1]["cwd"].exists()


@pytest.mark.parametrize("failure", ["timeout", "launch", "failed", "missing", "invalid-utf8"])
def test_cli_failures_are_bounded_private_and_release_resources(service, monkeypatch, failure):
    calls = []

    def run(command, **kwargs):
        calls.append(kwargs)
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 45, stderr=b"private transcript")
        if failure == "launch":
            raise OSError("private path and transcript")
        if failure == "invalid-utf8":
            Path(command[command.index("-of") + 1]).with_suffix(".txt").write_bytes(b"\xff")
        return subprocess.CompletedProcess(command, 1 if failure == "failed" else 0)

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(SpeechError) as caught:
        service.transcribe(recording())
    assert caught.value.status_code == (504 if failure == "timeout" else 503)
    assert "private" not in caught.value.message
    assert not calls[0]["cwd"].exists()
    assert service._job.acquire(blocking=False)
    service._job.release()


def test_silent_audio_never_starts_a_worker(service, monkeypatch):
    monkeypatch.setattr(
        subprocess, "run", lambda *args, **kwargs: pytest.fail("No worker for silence")
    )
    with pytest.raises(SpeechError):
        service.transcribe(recording(silent=True))


def test_busy_speech_refuses_without_queueing(service, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("No queued worker"))
    assert service._job.acquire(blocking=False)
    try:
        with pytest.raises(SpeechError) as caught:
            service.transcribe(recording())
        assert caught.value.status_code == 429
        with pytest.raises(SpeechError) as caught:
            service.synthesize("Saved reply")
        assert caught.value.status_code == 429
    finally:
        service._job.release()


def test_synthesis_passes_text_over_stdin_and_returns_wav(service, monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        Path(command[-1]).write_bytes(recording(sample_rate=22050))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", run)
    assert service.synthesize("Saved private assistant reply").startswith(b"RIFF")
    command, kwargs = calls[0]
    assert command[0] == sys.executable
    assert "Saved private assistant reply" not in " ".join(command)
    assert kwargs["input"] == b"Saved private assistant reply"
    assert kwargs["timeout"] == SPEECH_TIMEOUT
    assert not kwargs["cwd"].exists()


@pytest.mark.parametrize(
    "audio",
    [b"", b"not wav", b"x" * (MAX_TTS_BYTES + 1), recording(61)],
    ids=["empty", "non-wav", "oversized", "too-long"],
)
def test_synthesis_audio_is_bounded_and_validated(service, monkeypatch, audio):
    def run(command, **kwargs):
        Path(command[-1]).write_bytes(audio)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(SpeechError) as caught:
        service.synthesize("Saved reply")
    assert caught.value.status_code == 503


def test_piper_worker_lazily_loads_cpu_voice_and_synthesizes_local_wav(
    service, tmp_path, monkeypatch
):
    calls = []

    class Voice:
        @staticmethod
        def load(model, **kwargs):
            calls.append((model, kwargs))
            return Voice()

        def synthesize_wav(self, text, wav):
            calls.append(text)
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(22050)
            wav.writeframes(struct.pack("<h", 250) * 200)

    monkeypatch.setitem(sys.modules, "piper", types.SimpleNamespace(PiperVoice=Voice))
    output = tmp_path / "worker.wav"
    synthesize(service.piper_model, output, "Checked reply")
    assert calls == [(str(service.piper_model), {"use_cuda": False}), "Checked reply"]
    with wave.open(str(output), "rb") as wav:
        assert wav.getnframes() == 200 and wav.getframerate() == 22050


def test_piper_worker_rejects_voice_that_could_download_external_resources(service, tmp_path):
    Path(str(service.piper_model) + ".json").write_text(
        '{"phoneme_type":"espeak", "espeak":{"voice":"ar"}}', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="English"):
        synthesize(service.piper_model, tmp_path / "unused.wav", "Checked reply")


def test_worker_output_file_cannot_exceed_limit(tmp_path):
    output = BoundedAudioFile(tmp_path / "bounded.wav")
    try:
        with pytest.raises(ValueError, match="limit"):
            output.write(b"x" * (MAX_TTS_BYTES + 1))
        assert output.file.tell() == 0
    finally:
        output.file.close()


class FakeEngine:
    def __init__(self):
        self.read_count = 0
        self.now = datetime(2026, 10, 5, 6, tzinfo=UTC)
        self.profile = ResidentProfile(
            resident_name="Fictional resident", pg_name="Demo PG", guard_location="the guard room"
        )
        self.mode = DeliveryMode(enabled=True, expires_at=self.now + timedelta(hours=4))
        self.override = None
        self.session = Conversation(
            id="5e05dd16-0d7a-4cba-8a96-bceec031e25c",
            created_at=datetime.now(UTC),
            revision=1,
            reply_context_fingerprint=fingerprint(self.dashboard()),
            messages=[
                Message(role="courier", content="caller input", at=datetime.now(UTC)),
                Message(role="assistant", content="Checked saved reply", at=datetime.now(UTC)),
            ],
        )

    def dashboard(self):
        return Dashboard(
            profile=self.profile,
            delivery_mode=self.mode,
            context=build_context(self.profile, self.mode, self.override, self.now),
            server_time=self.now,
        )

    def read(self, session_id):
        self.read_count += 1
        if session_id != self.session.id:
            raise HTTPException(404, "Conversation not found.")
        return self.session.model_copy(deep=True)


@pytest.fixture
def api(service):
    engine = FakeEngine()
    app = FastAPI()
    app.include_router(build_speech_router(engine, service))
    with TestClient(app) as client:
        yield client, engine, service


def speak_url(engine):
    return f"/api/conversations/{engine.session.id}/speech"


def test_speech_status_endpoint(api):
    client, _, _ = api
    assert client.get("/api/speech/status").json()["max_seconds"] == 30


def test_transcription_endpoint_does_not_read_or_change_conversation(api, monkeypatch):
    client, engine, _ = api
    whisper_result(monkeypatch, "It is prepaid")
    response = client.post(
        "/api/speech/transcribe", content=recording(), headers={"content-type": "audio/wav"}
    )
    assert response.status_code == 200 and response.json()["text"] == "It is prepaid"
    assert engine.read_count == 0 and engine.session.revision == 1


@pytest.mark.parametrize("content_type", ["audio/webm", "application/json", "", "audio/x-wav"])
def test_transcription_rejects_other_formats_without_starting_worker(
    api, monkeypatch, content_type
):
    client, _, _ = api
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("Invalid upload"))
    response = client.post(
        "/api/speech/transcribe", content=recording(), headers={"content-type": content_type}
    )
    assert response.status_code == 415


def test_upload_size_is_checked_on_stream_without_content_length(api, monkeypatch):
    client, _, _ = api
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("Oversized upload"))
    response = client.post(
        "/api/speech/transcribe",
        content=iter([b"x" * MAX_WAV_BYTES, b"x"]),
        headers={"content-type": "audio/wav"},
    )
    assert response.status_code == 413


@pytest.mark.parametrize(
    "length, expected", [("-1", 400), ("garbage", 400), (str(MAX_WAV_BYTES + 1), 413)]
)
def test_invalid_or_large_content_length_is_rejected(api, length, expected):
    client, _, _ = api
    response = client.post(
        "/api/speech/transcribe",
        content=b"x",
        headers={"content-type": "audio/wav", "content-length": length},
    )
    assert response.status_code == expected


def test_only_latest_saved_checked_assistant_reply_can_be_spoken(api, monkeypatch):
    client, engine, service = api
    texts = []

    def synth(text):
        texts.append(text)
        return recording()

    monkeypatch.setattr(service, "synthesize", synth)
    response = client.post(speak_url(engine), json={"revision": 1, "message_index": 1})
    assert response.status_code == 200 and response.content.startswith(b"RIFF")
    assert response.headers["cache-control"] == "no-store"
    assert texts == ["Checked saved reply"] and engine.read_count == 2
    assert engine.session.revision == 1


@pytest.mark.parametrize(
    "body, expected",
    [
        ({"revision": 0, "message_index": 1}, 409),
        ({"revision": 1, "message_index": 0}, 422),
        ({"revision": 1, "message_index": 20}, 422),
        ({"revision": 1, "message_index": -1}, 422),
        ({"revision": 1, "message_index": 1, "text": "untrusted"}, 422),
        ({"revision": True, "message_index": 1}, 422),
        ({"revision": 1, "message_index": 1.0}, 422),
    ],
)
def test_playback_refuses_stale_untrusted_and_invalid_requests(api, monkeypatch, body, expected):
    client, engine, service = api
    monkeypatch.setattr(service, "synthesize", lambda text: pytest.fail("Unauthorized text"))
    assert client.post(speak_url(engine), json=body).status_code == expected


def test_even_old_assistant_reply_is_not_playable(api, monkeypatch):
    client, engine, service = api
    engine.session.messages[0].role = "assistant"
    monkeypatch.setattr(service, "synthesize", lambda text: pytest.fail("Old reply"))
    assert (
        client.post(speak_url(engine), json={"revision": 1, "message_index": 0}).status_code == 422
    )


def test_resident_message_is_not_synthesized(api, monkeypatch):
    client, engine, service = api
    engine.session.messages[-1].role = "resident"
    monkeypatch.setattr(service, "synthesize", lambda text: pytest.fail("Resident message"))
    assert (
        client.post(speak_url(engine), json={"revision": 1, "message_index": 1}).status_code == 422
    )


def test_reply_changed_during_synthesis_is_discarded(api, monkeypatch):
    client, engine, service = api

    def synth(text):
        engine.session.revision += 1
        return recording()

    monkeypatch.setattr(service, "synthesize", synth)
    response = client.post(speak_url(engine), json={"revision": 1, "message_index": 1})
    assert response.status_code == 409 and not response.content.startswith(b"RIFF")


def test_same_revision_but_changed_saved_text_is_discarded(api, monkeypatch):
    client, engine, service = api

    def synth(text):
        engine.session.messages[-1].content = "Changed reply"
        return recording()

    monkeypatch.setattr(service, "synthesize", synth)
    assert (
        client.post(speak_url(engine), json={"revision": 1, "message_index": 1}).status_code == 409
    )


def change_context(engine, change):
    if change == "location":
        engine.profile.guard_location = "the new guard room"
    elif change == "directions":
        engine.profile.guard_directions = "Use the other gate."
    elif change == "availability":
        engine.override = TodayOverride(status="at_pg", local_date=engine.now.date())
    elif change == "expiry":
        engine.now = engine.mode.expires_at + timedelta(seconds=1)
    else:
        engine.mode = DeliveryMode(enabled=False)


@pytest.mark.parametrize("change", ["location", "directions", "availability", "expiry", "disabled"])
def test_context_changed_before_synthesis_without_revision_change_refuses_audio(
    api, monkeypatch, change
):
    client, engine, service = api
    change_context(engine, change)
    monkeypatch.setattr(service, "synthesize", lambda text: pytest.fail("Stale source context"))
    response = client.post(speak_url(engine), json={"revision": 1, "message_index": 1})
    assert response.status_code == 409 and engine.session.revision == 1


@pytest.mark.parametrize("change", ["location", "directions", "availability", "expiry", "disabled"])
def test_context_changed_during_synthesis_without_revision_change_discards_audio(
    api, monkeypatch, change
):
    client, engine, service = api

    def synth(text):
        change_context(engine, change)
        return recording()

    monkeypatch.setattr(service, "synthesize", synth)
    response = client.post(speak_url(engine), json={"revision": 1, "message_index": 1})
    assert response.status_code == 409 and engine.session.revision == 1
    assert not response.content.startswith(b"RIFF")


def test_legacy_reply_without_source_stamp_fails_closed_but_stays_readable(api, monkeypatch):
    client, engine, service = api
    engine.session.reply_context_fingerprint = None
    monkeypatch.setattr(service, "synthesize", lambda text: pytest.fail("Unstamped legacy reply"))
    response = client.post(speak_url(engine), json={"revision": 1, "message_index": 1})
    assert response.status_code == 409
    assert "fresh text turn" in response.json()["detail"]
    assert engine.read(engine.session.id).messages[-1].content == "Checked saved reply"


@pytest.mark.parametrize("status", ["ended", "needs_resident"])
def test_saved_final_safe_assistant_reply_can_be_spoken_with_current_context(
    api, monkeypatch, status
):
    client, engine, service = api
    engine.session.status = status
    monkeypatch.setattr(service, "synthesize", lambda text: recording())
    assert (
        client.post(speak_url(engine), json={"revision": 1, "message_index": 1}).status_code == 200
    )


class StampProvider:
    def __init__(self, context):
        self.context = context
        self.failure = False

    def status(self):
        return ModelStatus(
            configured=True,
            model="fake",
            provider="fake",
            message="fake",
            reserved_usd=0,
            budget_usd=0,
        )

    def generate(self, messages):
        self.context.profile.guard_directions = "Updated while provider was running."
        if self.failure:
            raise ModelUnavailable("Fake test failure")
        return AgentPlan(action="answer", topic="directions")


def test_engine_stamps_start_reply_and_actual_checked_post_model_context(tmp_path):
    context = FakeEngine()
    provider = StampProvider(context)
    engine = ConversationEngine(
        ConversationStore(tmp_path), provider, context.dashboard, lambda: context.now
    )
    session = engine.start()
    first_stamp = session.reply_context_fingerprint
    assert first_stamp == fingerprint(context.dashboard())
    session = engine.turn(session.id, TurnRequest(text="Where is the guard room?", revision=0))
    assert session.reply_context_fingerprint == fingerprint(context.dashboard())
    assert session.reply_context_fingerprint != first_stamp
    assert "Updated while provider" in session.messages[-1].content


def test_model_unavailable_reply_stamps_the_fresh_safety_context(tmp_path):
    context = FakeEngine()
    provider = StampProvider(context)
    provider.failure = True
    engine = ConversationEngine(
        ConversationStore(tmp_path), provider, context.dashboard, lambda: context.now
    )
    session = engine.start()
    session = engine.turn(session.id, TurnRequest(text="Delivery question", revision=0))
    assert session.status == "needs_resident"
    assert session.reply_context_fingerprint == fingerprint(context.dashboard())


def test_execute_stamp_uses_passed_dashboard_not_later_dashboard_read(tmp_path):
    context = FakeEngine()
    checked = context.dashboard()
    engine = ConversationEngine(
        ConversationStore(tmp_path), StampProvider(context), context.dashboard, lambda: context.now
    )
    session = engine.start()
    context.profile = context.profile.model_copy(update={"guard_location": "changed afterward"})
    engine._execute(session, AgentPlan(action="answer", topic="directions"), "Where?", checked)
    assert session.reply_context_fingerprint == fingerprint(checked)
    assert session.reply_context_fingerprint != fingerprint(context.dashboard())
    assert "changed afterward" not in session.messages[-1].content


def test_expired_approval_safe_reply_stamps_current_context(tmp_path):
    context = FakeEngine()
    engine = ConversationEngine(
        ConversationStore(tmp_path), StampProvider(context), context.dashboard, lambda: context.now
    )
    session = engine.start()
    engine._execute(
        session,
        AgentPlan(action="request_approval", proposed_location="manager office"),
        "Can I leave it in the manager office?",
        context.dashboard(),
    )
    engine.store.write(session)
    context.profile.guard_directions = "Changed directions."
    refreshed = engine.read(session.id)
    assert refreshed.events[-1].action == "approval_expired"
    assert refreshed.reply_context_fingerprint == fingerprint(context.dashboard())


def test_unknown_conversation_and_malformed_uuid_cannot_synthesize(api, monkeypatch):
    client, _, service = api
    monkeypatch.setattr(service, "synthesize", lambda text: pytest.fail("Unknown session"))
    for session_id, expected in (("bad", 422), ("1792bb4c-9615-45dd-a972-96dce69f57d9", 404)):
        response = client.post(
            f"/api/conversations/{session_id}/speech", json={"revision": 1, "message_index": 1}
        )
        assert response.status_code == expected


@pytest.mark.parametrize("code", [429, 503, 504])
def test_speech_endpoint_preserves_safe_error_status(api, monkeypatch, code):
    client, engine, service = api

    def synth(text):
        raise SpeechError(code, "Safe public message.")

    monkeypatch.setattr(service, "synthesize", synth)
    response = client.post(speak_url(engine), json={"revision": 1, "message_index": 1})
    assert response.status_code == code and response.json()["detail"] == "Safe public message."
