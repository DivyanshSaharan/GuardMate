"""Call coordination tests using no hardware, speech runtime or model requests."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from guardmate.agent.models import Conversation, Message
from guardmate.cellular.call_errors import CallError
from guardmate.cellular.call_runner import ManualCallRunner, phone_device, require_ready
from guardmate.cellular.windows_audio import AudioDevice

NOW = datetime(2026, 10, 4, 9, 0, tzinfo=UTC)
INPUT = AudioDevice(25, "Input (vivo T2x 5G)", "input")
OUTPUT = AudioDevice(24, "Output (vivo T2x 5G)", "output")
READY = {
    "setup_complete": True,
    "delivery_mode_active": True,
    "model_configured": True,
    "stt_ready": True,
    "tts_ready": True,
}


class FakeBackend:
    def __init__(self):
        self.current = None
        self.calls = []
        self.ready = READY.copy()
        self.token = "a" * 64
        self.text = "I have a prepaid parcel."
        self.reply_status = "active"
        self.send_error = None
        self.on_synthesize = None

    def inspect(self):
        self.calls.append("inspect")
        return self.ready.copy()

    def start(self, label):
        self.calls.append("start")
        self.current = Conversation(
            id=str(uuid4()),
            courier_label=label,
            created_at=NOW,
            messages=[Message(role="assistant", content="Hello, I'm GuardMate.", at=NOW)],
            reply_context_fingerprint=self.token,
        )
        return self.current.model_copy(deep=True)

    def read(self, identifier):
        self.calls.append(("read", identifier))
        assert identifier == self.current.id
        return self.current.model_copy(deep=True)

    def context_token(self, session):
        self.calls.append("context")
        if session.id != self.current.id or session.revision != self.current.revision:
            raise CallError("Conversation changed; discard the draft.")
        if session.status != self.current.status or not self.ready["delivery_mode_active"]:
            raise CallError("Delivery plan changed.")
        return self.token

    def transcribe(self, audio):
        self.calls.append(("transcribe", audio))
        return {"text": self.text, "duration_ms": 5000, "processing_ms": 1}

    def send(self, session, text, *, expected_context=None):
        self.calls.append(("send", session.id, session.revision, text))
        if self.send_error:
            raise self.send_error
        assert expected_context == self.token
        assert session.id == self.current.id and session.revision == self.current.revision
        self.current.messages.extend(
            [
                Message(role="courier", content=text, at=NOW),
                Message(
                    role="assistant", content="Is security there to accept the parcel?", at=NOW
                ),
            ]
        )
        self.current.revision += 1
        self.current.turn_count += 1
        self.current.status = self.reply_status
        self.current.reply_context_fingerprint = self.token
        return self.current.model_copy(deep=True)

    def synthesize(self, session):
        self.calls.append("synthesize")
        if self.on_synthesize:
            self.on_synthesize()
        return b"mock-local-piper-audio"

    def ensure_fresh(self, session):
        self.calls.append("fresh")
        if (
            session.id != self.current.id
            or session.revision != self.current.revision
            or session.status != self.current.status
            or session.messages[-1] != self.current.messages[-1]
            or session.reply_context_fingerprint != self.token
            or not self.ready["delivery_mode_active"]
        ):
            raise CallError("Saved reply or context changed.")

    def end(self, session):
        self.calls.append(("end", session.id, session.revision))
        assert session.id == self.current.id and session.revision == self.current.revision
        self.current.status = "ended"
        self.current.revision += 1
        return self.current.model_copy(deep=True)


class FakeAudio:
    def __init__(self):
        self.calls = []
        self.on_record = None
        self.on_play = None
        self.play_error = None

    def record(self, device, seconds):
        self.calls.append(("record", device, seconds))
        if self.on_record:
            self.on_record()
        return b"mock-phone-capture"

    def play(self, device, audio):
        self.calls.append(("play", device, audio))
        if self.on_play:
            self.on_play()
        if self.play_error:
            raise self.play_error


@pytest.fixture
def rig():
    backend, audio, events = FakeBackend(), FakeAudio(), []
    runner = ManualCallRunner(
        backend,
        audio,
        INPUT,
        OUTPUT,
        convert_reply=lambda audio: b"converted:" + audio,
        hosted_consent=True,
        emit=events.append,
    )
    return runner, backend, audio, events


def test_start_creates_fresh_session_and_speaks_greeting_without_model(rig):
    runner, backend, audio, events = rig
    session = runner.start("Fictional courier")
    assert session.id == runner.session.id
    assert session.courier_label == "Fictional courier"
    assert runner.model_attempts == 0
    assert not any(isinstance(item, tuple) and item[0] == "send" for item in backend.calls)
    assert audio.calls == [("play", OUTPUT, b"converted:mock-local-piper-audio")]
    assert events[-1] == {"event": "playback_completed", "caller_heard_verified": False}
    with pytest.raises(CallError, match="already owns"):
        runner.start("Another call")


def test_missing_hosted_consent_has_no_backend_or_audio_work(rig):
    runner, backend, audio, _ = rig
    runner.hosted_consent = False
    with pytest.raises(CallError, match="consent"):
        runner.start("Demo")
    assert backend.calls == [] and audio.calls == []


@pytest.mark.parametrize("field", list(READY))
def test_readiness_failure_before_session_or_audio(rig, field):
    runner, backend, audio, _ = rig
    backend.ready[field] = False
    with pytest.raises(CallError):
        runner.start("Demo")
    assert backend.calls == ["inspect"] and audio.calls == []


def test_listen_is_local_review_and_send_is_explicit_single_model_turn(rig):
    runner, backend, audio, events = rig
    runner.start("Demo")
    backend.calls.clear()
    audio.calls.clear()
    assert runner.listen() == backend.text
    assert runner.model_attempts == 0
    assert audio.calls == [("record", INPUT, 5)]
    assert not any(isinstance(item, tuple) and item[0] == "send" for item in backend.calls)
    assert events[-1]["event"] == "transcript_review"
    runner.send()
    sends = [item for item in backend.calls if isinstance(item, tuple) and item[0] == "send"]
    assert sends == [("send", runner.session.id, 0, backend.text)]
    assert runner.model_attempts == 1 and runner.draft is None
    assert audio.calls[-1] == ("play", OUTPUT, b"converted:mock-local-piper-audio")
    assert backend.calls[-1] == "fresh"
    with pytest.raises(CallError, match="Capture and review"):
        runner.send()


def test_edit_preserves_ownership_and_context_and_requires_captured_draft(rig):
    runner, backend, _, _ = rig
    runner.start("Demo")
    with pytest.raises(CallError, match="no captured"):
        runner.edit("yes")
    runner.listen()
    original_id, revision, token = runner.session.id, runner.session.revision, runner._draft_context
    runner.edit("  yes, it is prepaid  ")
    assert runner.draft == "yes, it is prepaid"
    assert (runner.session.id, runner.session.revision, runner._draft_context) == (
        original_id,
        revision,
        token,
    )
    runner.send()
    assert backend.current.messages[-2].content == "yes, it is prepaid"


@pytest.mark.parametrize("text", ["", "   ", "x" * 601, None, 12])
def test_invalid_edits_rejected_without_clearing_valid_draft(rig, text):
    runner, _, _, _ = rig
    runner.start("Demo")
    runner.listen()
    previous = runner.draft
    with pytest.raises(CallError):
        runner.edit(text)
    assert runner.draft == previous


def test_pending_draft_blocks_recording_and_playback(rig):
    runner, _, audio, _ = rig
    runner.start("Demo")
    runner.listen()
    count = len(audio.calls)
    for action in (runner.listen, runner.speak, lambda: runner.speak(repeat=True)):
        with pytest.raises(CallError):
            action()
    assert len(audio.calls) == count


@pytest.mark.parametrize("change", ["context", "revision", "status", "window"])
def test_captured_draft_cannot_cross_context_question_or_resident_changes(rig, change):
    runner, backend, audio, _ = rig
    runner.start("Demo")
    runner.listen()
    if change == "context":
        backend.token = "new-resident-context"
    elif change == "revision":
        backend.current.revision += 1
    elif change == "status":
        backend.current.status = "needs_resident"
    else:
        backend.ready["delivery_mode_active"] = False
    count = len(audio.calls)
    with pytest.raises(CallError):
        runner.send()
    assert runner.model_attempts == 0 and len(audio.calls) == count
    assert runner.draft is not None


def test_context_change_during_capture_discards_transcript_without_send(rig):
    runner, backend, audio, _ = rig
    runner.start("Demo")
    audio.on_record = lambda: setattr(backend, "token", "changed")
    with pytest.raises(CallError, match="during capture"):
        runner.listen()
    assert runner.draft is None and runner.model_attempts == 0


@pytest.mark.parametrize("text", [None, "", "  ", "a" * 601, 2])
def test_invalid_local_transcript_cannot_be_sent(rig, text):
    runner, backend, _, _ = rig
    runner.start("Demo")
    backend.text = text
    with pytest.raises(CallError, match="transcript"):
        runner.listen()
    assert runner.draft is None and runner.model_attempts == 0


def test_refresh_discards_draft_and_updates_exact_owned_session(rig):
    runner, backend, _, events = rig
    runner.start("Demo")
    runner.listen()
    identifier = runner.session.id
    backend.current.revision += 1
    backend.current.status = "awaiting_approval"
    updated = runner.refresh()
    assert updated.id == identifier and updated.revision == 1
    assert runner.draft is None and runner._draft_context is None
    assert events[-1]["transcript_discarded"] is True
    with pytest.raises(CallError, match="paused"):
        runner.listen()


def test_uncertain_send_latches_and_never_retries_or_plays(rig):
    runner, backend, audio, _ = rig
    runner.start("Demo")
    runner.listen()
    backend.send_error = CallError("Hosted response timed out.", uncertain=True)
    count = len(audio.calls)
    with pytest.raises(CallError):
        runner.send()
    assert runner.uncertain and runner.model_attempts == 1 and runner.draft is not None
    for action in (runner.send, runner.listen, runner.speak, runner.refresh, runner.discard):
        with pytest.raises(CallError, match="uncertain"):
            action()
    assert len(audio.calls) == count
    assert (
        len([item for item in backend.calls if isinstance(item, tuple) and item[0] == "send"]) == 1
    )
    runner.stop()
    assert runner.stopped and backend.current.status == "ended"


@pytest.mark.parametrize("status", ["awaiting_approval", "needs_resident", "ended"])
def test_new_checked_pause_reply_spoken_once_then_new_jobs_blocked(rig, status):
    runner, backend, audio, events = rig
    runner.start("Demo")
    runner.listen()
    backend.reply_status = status
    runner.send()
    assert runner.session.status == status
    assert events[-1]["event"] == "paused"
    count = len(audio.calls)
    for action in (runner.listen, runner.send, runner.speak, lambda: runner.speak(repeat=True)):
        with pytest.raises(CallError, match="paused"):
            action()
    assert len(audio.calls) == count


@pytest.mark.parametrize("change", ["context", "revision", "resident", "window"])
def test_reply_stale_during_synthesis_is_never_played(rig, change):
    runner, backend, audio, _ = rig
    runner.start("Demo")
    runner.listen()

    def mutate():
        if change == "context":
            backend.token = "changed"
        elif change == "revision":
            backend.current.revision += 1
        elif change == "resident":
            backend.current.messages.append(Message(role="resident", content="Take over", at=NOW))
        else:
            backend.ready["delivery_mode_active"] = False

    backend.on_synthesize = mutate
    count = len(audio.calls)
    with pytest.raises(CallError, match="changed"):
        runner.send()
    assert len(audio.calls) == count and runner.draft is None


def test_post_conversion_freshness_check_rejects_resident_change(rig):
    runner, backend, audio, _ = rig
    runner.start("Demo")
    runner.listen()

    def convert(audio):
        backend.current.revision += 1
        return audio

    runner.convert_reply = convert
    count = len(audio.calls)
    with pytest.raises(CallError, match="changed"):
        runner.send()
    assert len(audio.calls) == count


def test_explicit_repeat_only_no_automatic_playback_retry(rig):
    runner, backend, audio, _ = rig
    runner.start("Demo")
    with pytest.raises(CallError, match="already been played"):
        runner.speak()
    runner.speak(repeat=True)
    assert len(audio.calls) == 2 and runner.model_attempts == 0
    assert not any(isinstance(item, tuple) and item[0] == "send" for item in backend.calls)


def test_native_uncertainty_stops_all_further_audio(rig):
    runner, _, audio, _ = rig
    audio.play_error = CallError("Native playback timed out.", uncertain=True)
    with pytest.raises(CallError):
        runner.start("Demo")
    assert runner.uncertain
    with pytest.raises(CallError, match="uncertain"):
        runner.speak(repeat=True)
    assert len(audio.calls) == 1


def test_turn_limit_preserves_budget_without_extra_capture_or_send(rig):
    runner, _, audio, _ = rig
    runner.max_turns = 1
    runner.start("Demo")
    runner.listen()
    runner.send()
    count = len(audio.calls)
    with pytest.raises(CallError, match="limit"):
        runner.listen()
    assert runner.model_attempts == 1 and len(audio.calls) == count


def test_stop_uses_latest_owned_revision_and_is_idempotent(rig):
    runner, backend, _, events = rig
    runner.start("Demo")
    identifier = runner.session.id
    backend.current.revision = 7
    runner.stop()
    assert ("end", identifier, 7) in backend.calls
    count = len(backend.calls)
    runner.stop()
    assert len(backend.calls) == count
    assert events[-1] == {"event": "stopped", "physical_call_ended": False}


def test_stop_without_known_session_never_searches_or_ends_another(rig):
    runner, backend, audio, _ = rig
    runner.stop()
    assert backend.calls == [] and audio.calls == [] and runner.stopped


def test_busy_runner_rejects_overlapping_commands(rig):
    runner, _, audio, _ = rig
    observed = []

    def concurrent():
        with pytest.raises(CallError, match="pending"):
            runner.listen()
        observed.append(True)

    audio.on_play = concurrent
    runner.start("Demo")
    assert observed == [True]


@pytest.mark.parametrize("value", [False, None, "true", 1])
def test_readiness_requires_real_true(value):
    status = READY.copy()
    status["delivery_mode_active"] = value
    with pytest.raises(CallError):
        require_ready(status)


@pytest.mark.parametrize(
    "identifier,direction",
    [(0, "input"), (True, "input"), (-1, "input"), (24, "input"), (25, "output")],
)
def test_default_other_or_direction_mismatched_devices_are_refused(identifier, direction):
    with pytest.raises(CallError):
        phone_device(
            [INPUT, OUTPUT, AudioDevice(0, "Laptop microphone", "input")], identifier, direction
        )


def test_fresh_explicit_phone_selection_refuses_duplicates():
    assert phone_device([INPUT, OUTPUT], 25, "input") == INPUT
    with pytest.raises(CallError, match="ambiguous"):
        phone_device([INPUT, INPUT], 25, "input")


@pytest.mark.parametrize(
    "kwargs",
    [{"seconds": 0}, {"seconds": 11}, {"seconds": True}, {"max_turns": 0}, {"max_turns": 11}],
)
def test_runner_limits_are_validated_before_work(kwargs):
    with pytest.raises(CallError):
        ManualCallRunner(
            FakeBackend(), FakeAudio(), INPUT, OUTPUT, convert_reply=lambda x: x, **kwargs
        )
