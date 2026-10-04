"""Automatic coordination tests with no audio, subprocesses or HTTP requests."""

import pytest
from guardmate.cellular.automatic_call import AutomaticCallCoordinator, transcript_problem
from guardmate.cellular.call_errors import CallError
from guardmate.cellular.call_runner import ManualCallRunner
from guardmate.cellular.utterance import NoSpeech, UtteranceTooLong
from test_manual_call_runner import INPUT, OUTPUT, FakeAudio, FakeBackend


class Clock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now


class Listener:
    def __init__(self):
        self.calls = []
        self.on_listen = None
        self.error = None

    def listen(self, device, *, on_armed=None):
        self.calls.append(device)
        if on_armed:
            on_armed()
        if self.on_listen:
            self.on_listen()
        if self.error:
            raise self.error
        return b"mock-bounded-utterance"


@pytest.fixture
def rig():
    backend, audio, listener, clock, events = FakeBackend(), FakeAudio(), Listener(), Clock(), []
    runner = ManualCallRunner(
        backend,
        audio,
        INPUT,
        OUTPUT,
        convert_reply=lambda value: b"converted:" + value,
        hosted_consent=True,
        max_turns=2,
        emit=events.append,
    )
    coordinator = AutomaticCallCoordinator(
        runner,
        audio=listener,
        automatic_submission_consent=True,
        emit=events.append,
        clock=clock,
    )
    return coordinator, runner, backend, audio, listener, clock, events


def sends(backend):
    return [item for item in backend.calls if isinstance(item, tuple) and item[0] == "send"]


def test_opt_in_required_before_backend_or_audio(rig):
    _, runner, backend, audio, listener, _, _ = rig
    for consent in (False, None, 1, "yes"):
        with pytest.raises(CallError, match="consent"):
            AutomaticCallCoordinator(runner, audio=listener, automatic_submission_consent=consent)
    assert backend.calls == audio.calls == listener.calls == []


@pytest.mark.parametrize("seconds", [True, 0, 601, "180"])
def test_admission_window_strict_and_bounded(rig, seconds):
    _, runner, _, _, listener, _, _ = rig
    with pytest.raises(CallError):
        AutomaticCallCoordinator(
            runner, audio=listener, automatic_submission_consent=True, max_seconds=seconds
        )


def test_automatic_turn_limit_and_half_duplex_order_without_manual_capture(rig):
    coordinator, runner, backend, audio, listener, _, events = rig
    result = coordinator.run("Fictional courier")
    assert result.status == "limit" and result.reason == "model_turn_limit"
    assert result.session_id == runner.session.id and result.model_attempts == 2
    assert len(sends(backend)) == 2 and len(listener.calls) == 2
    assert [item[0] for item in audio.calls] == ["play", "play", "play"]
    assert "end" not in [item[0] for item in backend.calls if isinstance(item, tuple)]
    stages = [
        item["event"]
        for item in events
        if item["event"]
        in {
            "playback_completed",
            "utterance_armed",
            "automatic_submission",
        }
    ]
    assert stages == [
        "playback_completed",
        "utterance_armed",
        "automatic_submission",
        "playback_completed",
        "utterance_armed",
        "automatic_submission",
        "playback_completed",
    ]
    disclosure = next(item for item in events if item["event"] == "automatic_submission_disclosure")
    assert disclosure["unreviewed_transcripts_uploaded_automatically"] is True
    assert disclosure["operator_review_each_turn"] is False
    assert disclosure["asr_confidence_available"] is False
    assert runner.admission_check is None and not runner.stopped
    with pytest.raises(CallError, match="fresh runner"):
        coordinator.run("Other courier")
    assert len(sends(backend)) == 2


@pytest.mark.parametrize("status", ["awaiting_approval", "needs_resident", "ended"])
def test_paused_or_ended_result_preserves_saved_session_and_never_autoends(rig, status):
    coordinator, runner, backend, _, listener, _, _ = rig
    backend.reply_status = status
    result = coordinator.run("Fictional courier")
    assert result.status == ("ended" if status == "ended" else "paused")
    assert result.reason == status and runner.session.status == status
    assert len(listener.calls) == len(sends(backend)) == 1
    assert not any(isinstance(item, tuple) and item[0] == "end" for item in backend.calls)


@pytest.mark.parametrize(
    "error,reason",
    [
        (NoSpeech("No speech"), "no_speech"),
        (UtteranceTooLong("Utterance too long"), "utterance_too_long"),
    ],
)
def test_typed_capture_outcomes_pause_once_without_asr_or_retry(rig, error, reason):
    coordinator, runner, backend, audio, listener, _, _ = rig
    listener.error = error
    result = coordinator.run("Fictional courier")
    assert result.status == "paused" and result.reason == reason
    assert len(listener.calls) == 1 and len(audio.calls) == 1
    assert not sends(backend)
    assert not any(isinstance(item, tuple) and item[0] == "transcribe" for item in backend.calls)
    assert not runner.uncertain and not runner.stopped


@pytest.mark.parametrize(
    "text",
    [
        "yes",
        "No",
        "okay",
        "[inaudible] prepaid",
        "yes yes yes",
        "thank you for watching",
        "!?!",
        "123456",
        "silence",
        "noise noise noise",
    ],
)
def test_detectable_problem_pauses_original_draft_without_model(rig, text):
    coordinator, runner, backend, _, listener, _, _ = rig
    backend.text = text
    result = coordinator.run("Fictional courier")
    assert result.status == "paused"
    assert runner.draft == text and not sends(backend)
    assert len(listener.calls) == 1


def test_assistant_echo_pauses_without_model(rig):
    coordinator, runner, backend, _, _, _, _ = rig
    backend.text = "Hello, I'm GuardMate."
    result = coordinator.run("Fictional courier")
    assert result.reason == "assistant_echo" and runner.draft == backend.text
    assert not sends(backend)


@pytest.mark.parametrize("question", ["prepaid", "guard_available"])
@pytest.mark.parametrize("text", ["yes", "no", "yeah", "nope"])
def test_bare_confirmation_only_for_exact_saved_question(rig, question, text):
    _, _, backend, _, _, _, _ = rig
    item = backend.start("Fictional courier")
    item.pending_question = question
    assert transcript_problem(item, text) is None
    item.pending_question = "alternative_location"
    assert transcript_problem(item, text) == "noncontextual_confirmation"


def test_negative_and_safety_words_are_preserved_not_replaced(rig):
    coordinator, _, backend, _, _, _, _ = rig
    backend.text = "No, this is not prepaid; I need an OTP and a signature."
    backend.reply_status = "needs_resident"
    result = coordinator.run("Fictional courier")
    assert result.status == "paused"
    assert sends(backend)[0][-1] == backend.text


def test_capture_expiry_stops_before_local_asr(rig):
    coordinator, runner, backend, audio, listener, clock, _ = rig
    listener.on_listen = lambda: setattr(clock, "now", 180)
    result = coordinator.run("Fictional courier")
    assert result.status == "deadline" and result.model_attempts == 0
    assert len(listener.calls) == 1 and len(audio.calls) == 1
    assert not sends(backend)
    assert not any(isinstance(item, tuple) and item[0] == "transcribe" for item in backend.calls)
    assert runner.admission_check is None


def test_asr_expiry_stops_before_model_admission(rig):
    coordinator, _, backend, audio, _, clock, _ = rig
    original = backend.transcribe

    def transcribe(value):
        result = original(value)
        clock.now = 180
        return result

    backend.transcribe = transcribe
    result = coordinator.run("Fictional courier")
    assert result.status == "deadline" and result.model_attempts == 0
    assert not sends(backend) and len(audio.calls) == 1


def test_model_expiry_preserves_checked_result_but_admits_no_synthesis_or_play(rig):
    coordinator, runner, backend, audio, _, clock, _ = rig
    original = backend.send

    def send(*args, **kwargs):
        result = original(*args, **kwargs)
        clock.now = 180
        return result

    backend.send = send
    result = coordinator.run("Fictional courier")
    assert result.status == "deadline" and result.model_attempts == 1
    assert runner.session.revision == 1 and len(audio.calls) == 1
    assert backend.calls.count("synthesize") == 1  # greeting only


def test_synthesis_expiry_admits_no_new_playback(rig):
    coordinator, runner, backend, audio, _, clock, _ = rig

    def expire_on_second_synthesis():
        if backend.current.revision:
            clock.now = 180

    backend.on_synthesize = expire_on_second_synthesis
    result = coordinator.run("Fictional courier")
    assert result.status == "deadline" and len(sends(backend)) == 1
    assert len(audio.calls) == 1 and runner.session.revision == 1


def test_uncertain_send_propagates_and_latches_without_auto_end_or_resend(rig):
    coordinator, runner, backend, audio, listener, _, _ = rig
    backend.send_error = CallError("Synthetic lost response", uncertain=True)
    with pytest.raises(CallError) as caught:
        coordinator.run("Fictional courier")
    assert caught.value.uncertain and runner.uncertain
    assert len(sends(backend)) == len(listener.calls) == 1 and len(audio.calls) == 1
    assert runner.admission_check is None
    assert not any(isinstance(item, tuple) and item[0] == "end" for item in backend.calls)


def test_generic_capture_failure_is_not_retried(rig):
    coordinator, runner, backend, _, listener, _, _ = rig
    listener.error = CallError("Endpoint unavailable", uncertain=True)
    with pytest.raises(CallError):
        coordinator.run("Fictional courier")
    assert runner.uncertain and len(listener.calls) == 1 and not sends(backend)


@pytest.mark.parametrize("kind", [NoSpeech, UtteranceTooLong])
def test_typed_outcome_must_not_hide_uncertainty(rig, kind):
    coordinator, runner, backend, _, listener, _, _ = rig
    listener.error = kind("Synthetic uncertain capture", uncertain=True)
    with pytest.raises(CallError) as caught:
        coordinator.run("Fictional courier")
    assert caught.value.uncertain and runner.uncertain
    assert len(listener.calls) == 1 and not sends(backend)


def test_ctrlc_propagates_without_autoending_and_restores_prior_admission_hook(rig):
    coordinator, runner, backend, _, listener, _, _ = rig

    def original_hook():
        pass

    runner.admission_check = original_hook
    listener.error = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        coordinator.run("Fictional courier")
    assert runner.admission_check is original_hook and runner.session is not None
    assert not any(isinstance(item, tuple) and item[0] == "end" for item in backend.calls)


def test_manual_optional_capture_keeps_snapshot_and_skips_default_record(rig):
    _, runner, backend, audio, _, _, _ = rig
    runner.start("Fictional courier")
    assert runner.listen(capture=lambda: b"injected-local-capture") == backend.text
    assert ("transcribe", b"injected-local-capture") in backend.calls
    assert all(item[0] != "record" for item in audio.calls)
    assert runner._draft_context == backend.token
