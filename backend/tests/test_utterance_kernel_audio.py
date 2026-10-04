"""One fake RawInputStream per utterance; never open a native audio endpoint."""

import types

import pytest
from guardmate.cellular import AudioError, validate_audio
from guardmate.cellular import kernel_audio as module
from guardmate.cellular.kernel_audio import WindowsKernelAudio
from guardmate.cellular.utterance import NoSpeech, UtteranceConfig, UtteranceTooLong
from test_kernel_cellular_audio import INPUT, OUTPUT, FakeSoundDevice, FakeStream
from test_utterance import QUIET, VOICE


class UtteranceStream(FakeStream):
    def start(self):
        self.api.calls.append(("start",))
        self.api.fail("start")
        for frame in self.api.frames:
            self.api.in_callback = True
            self.api.valid_frame_seen = not self.api.status
            try:
                self.kwargs["callback"](frame, len(frame) // 2, None, self.api.status)
            except (self.api.CallbackStop, self.api.CallbackAbort):
                self.kwargs["finished_callback"]()
                return
            finally:
                self.api.in_callback = False
        if self.api.finish_early:
            self.kwargs["finished_callback"]()


class FakeUtteranceSoundDevice(FakeSoundDevice):
    def __init__(self):
        super().__init__()
        self.frames = [QUIET] * 10 + [VOICE] * 12 + [QUIET] * 35
        self.in_callback = False
        self.valid_frame_seen = False
        self.finish_early = False

    def _open(self, direction, kwargs):
        self.calls.append(("open", direction, kwargs))
        self.fail("open")
        self.stream = UtteranceStream(self, direction, kwargs)
        if self.after_open:
            self.after_open()
        return self.stream


@pytest.fixture
def fake_audio():
    api = FakeUtteranceSoundDevice()
    return api, WindowsKernelAudio(sounddevice=api)


@pytest.fixture
def immediate_events(monkeypatch):
    waits = []

    class FakeEvent:
        def __init__(self):
            self.flag = False

        def set(self):
            self.flag = True

        def wait(self, timeout):
            waits.append(timeout)
            return self.flag

    monkeypatch.setattr(module, "threading", types.SimpleNamespace(Event=FakeEvent))
    return waits


def test_one_continuous_capture_armed_after_valid_frame_and_closed_before_return(fake_audio):
    api, audio = fake_audio
    armed = []

    def ready():
        assert api.valid_frame_seen and not api.in_callback
        assert not any(call[0] == "close" for call in api.calls)
        armed.append(True)

    captured = audio.listen_for_utterance(INPUT, on_armed=ready)
    rate, pcm = validate_audio(captured)
    assert rate == 16000 and VOICE * 12 in pcm and pcm.endswith(QUIET * 35)
    assert armed == [True]
    assert len([call for call in api.calls if call[0] == "open"]) == 1
    assert api.calls[-1] == ("close",)
    assert not any(call[0] == "abort" for call in api.calls)


@pytest.mark.parametrize("outcome", ["no_speech", "too_long"])
def test_known_outcome_closes_one_stream_and_returns_no_partial_wav(fake_audio, outcome):
    api, audio = fake_audio
    api.frames = [QUIET] * 50 if outcome == "no_speech" else [VOICE] * 500
    error = NoSpeech if outcome == "no_speech" else UtteranceTooLong
    with pytest.raises(error) as result:
        audio.listen_for_utterance(INPUT, config=UtteranceConfig(idle_timeout_seconds=1))
    assert result.value.uncertain is False
    assert api.calls[-1] == ("close",)
    assert len([call for call in api.calls if call[0] == "open"]) == 1


def test_no_callback_does_not_emit_armed_and_has_bounded_wait(fake_audio, immediate_events):
    api, audio = fake_audio
    api.frames = []
    armed = []
    with pytest.raises(AudioError, match="valid input frames"):
        audio.listen_for_utterance(INPUT, on_armed=lambda: armed.append(True))
    assert not armed
    assert immediate_events == [3.0]
    assert api.calls[-2:] == [("abort",), ("close",)]


def test_callback_status_failure_never_claims_armed(fake_audio, immediate_events):
    api, audio = fake_audio
    api.status = True
    armed = []
    with pytest.raises(AudioError):
        audio.listen_for_utterance(INPUT, on_armed=lambda: armed.append(True))
    assert not armed
    assert api.calls[-2:] == [("abort",), ("close",)]


def test_valid_frames_but_hung_capture_has_finite_wait(fake_audio, immediate_events):
    api, audio = fake_audio
    api.frames = [QUIET]
    with pytest.raises(AudioError, match="time limit"):
        audio.listen_for_utterance(INPUT)
    assert immediate_events == [3.0, 27.0]
    assert api.calls[-2:] == [("abort",), ("close",)]


def test_premature_finished_callback_is_not_a_valid_utterance(fake_audio, immediate_events):
    api, audio = fake_audio
    api.frames = [VOICE]
    api.finish_early = True
    with pytest.raises(AudioError, match="incomplete audio"):
        audio.listen_for_utterance(INPUT)
    assert api.calls[-2:] == [("abort",), ("close",)]


def test_stale_identity_during_constructor_never_starts(fake_audio):
    api, audio = fake_audio
    api.after_open = lambda: api.device_list[INPUT.id].update(name="Unrelated microphone")
    with pytest.raises(AudioError, match="changed or disconnected"):
        audio.listen_for_utterance(INPUT)
    assert not any(call[0] == "start" for call in api.calls)
    assert api.calls[-2:] == [("abort",), ("close",)]


def test_wrong_direction_does_not_open_capture(fake_audio):
    api, audio = fake_audio
    with pytest.raises(AudioError, match="required direction"):
        audio.listen_for_utterance(OUTPUT)
    assert not any(call[0] == "open" for call in api.calls)


@pytest.mark.parametrize("stage", ["open", "start"])
def test_native_failure_does_not_repeat_or_select_other_endpoint(fake_audio, stage):
    api, audio = fake_audio
    api.errors[stage] = api.PortAudioError("private native details", -9985)
    with pytest.raises(AudioError, match="unavailable or occupied") as error:
        audio.listen_for_utterance(INPUT)
    assert "private" not in str(error.value)
    assert len([call for call in api.calls if call[0] == "open"]) == 1
    if stage == "start":
        assert api.calls[-2:] == [("abort",), ("close",)]


def test_interrupt_aborts_and_closes(fake_audio):
    api, audio = fake_audio
    api.errors["start"] = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        audio.listen_for_utterance(INPUT)
    assert api.calls[-2:] == [("abort",), ("close",)]


def test_failed_close_does_not_return_a_wav_and_retains_callback_memory(fake_audio):
    api, audio = fake_audio
    previous = len(module._UNRELEASED_STREAMS)
    api.errors["close"] = api.PortAudioError("private close", -9999)
    try:
        with pytest.raises(AudioError, match="safely release"):
            audio.listen_for_utterance(INPUT)
        assert len(module._UNRELEASED_STREAMS) == previous + 1
        assert module._UNRELEASED_STREAMS[-1][0] is api.stream
    finally:
        del module._UNRELEASED_STREAMS[previous:]
