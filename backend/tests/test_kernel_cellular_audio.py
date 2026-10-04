"""Injected sounddevice callbacks only: these tests never touch audio hardware."""

import io
import types
import wave

import pytest
from guardmate.cellular import AudioDevice, AudioError, validate_audio
from guardmate.cellular import kernel_audio as module
from guardmate.cellular.kernel_audio import HOST_API, WindowsKernelAudio

INPUT = AudioDevice(2, "Microphone (vivo T2x 5G Hands-Free)", "input")
OUTPUT = AudioDevice(1, "Speakers (vivo T2x 5G Hands-Free)", "output")


def wav_bytes(*, rate=16000, frames=1600):
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x12\x00" * frames)
    return output.getvalue()


class FakeSoundDevice:
    class PortAudioError(Exception):
        pass

    class CallbackStop(Exception):
        pass

    class CallbackAbort(Exception):
        pass

    def __init__(self):
        self.calls = []
        self.device_list = [
            dict(name="Default laptop", hostapi=0, max_input_channels=1, max_output_channels=1),
            dict(name=OUTPUT.name, hostapi=1, max_input_channels=0, max_output_channels=1),
            dict(name=INPUT.name, hostapi=1, max_input_channels=1, max_output_channels=0),
            dict(name="Duplex endpoint", hostapi=1, max_input_channels=1, max_output_channels=1),
            dict(name="No channels", hostapi=1, max_input_channels=0, max_output_channels=0),
        ]
        self.errors = {}
        self.override = {}
        self.status = False
        self.short = False
        self.no_finish = False
        self.malformed = False
        self.after_open = None
        self.callback_frames = None
        self.stream = None
        self.played = bytearray()

    def fail(self, stage):
        if stage in self.errors:
            raise self.errors[stage]

    def query_hostapis(self):
        self.calls.append(("hostapis",))
        self.fail("enumerate")
        return [{"name": "MME"}, {"name": HOST_API}, {"name": "Windows WASAPI"}]

    def query_devices(self):
        self.calls.append(("devices",))
        return self.device_list

    def check_input_settings(self, **kwargs):
        self.calls.append(("check", "input", kwargs))
        self.fail("check")

    def check_output_settings(self, **kwargs):
        self.calls.append(("check", "output", kwargs))
        self.fail("check")

    def RawInputStream(self, **kwargs):
        return self._open("input", kwargs)

    def RawOutputStream(self, **kwargs):
        return self._open("output", kwargs)

    def _open(self, direction, kwargs):
        self.calls.append(("open", direction, kwargs))
        self.fail("open")
        self.stream = FakeStream(self, direction, kwargs)
        if self.after_open:
            self.after_open()
        return self.stream


class FakeStream:
    def __init__(self, api, direction, kwargs):
        self.api = api
        self.direction = direction
        self.kwargs = kwargs
        self.device = kwargs["device"]
        self.channels = kwargs["channels"]
        self.dtype = kwargs["dtype"]
        self.samplerate = kwargs["samplerate"]
        self.__dict__.update(api.override)

    def start(self):
        self.api.calls.append(("start",))
        self.api.fail("start")
        if self.api.no_finish:
            return
        if self.api.short:
            self.kwargs["finished_callback"]()
            return
        for _ in range(600):
            frames = self.api.callback_frames or self.kwargs["blocksize"]
            buffer = (
                b"\x12\x00" * frames
                if self.direction == "input"
                else bytearray(b"\xff" * frames * 2)
            )
            if self.api.malformed:
                buffer = buffer[:-1]
            try:
                self.kwargs["callback"](buffer, frames, None, self.api.status)
            except (self.api.CallbackStop, self.api.CallbackAbort):
                if self.direction == "output":
                    self.api.played.extend(buffer)
                self.kwargs["finished_callback"]()
                return
            if self.direction == "output":
                self.api.played.extend(buffer)
        raise AssertionError("Fake callback failed to stop within ten seconds")

    def abort(self, *, ignore_errors):
        assert ignore_errors is False
        self.api.calls.append(("abort",))
        self.api.fail("abort")

    def close(self, *, ignore_errors):
        assert ignore_errors is False
        self.api.calls.append(("close",))
        self.api.fail("close")


@pytest.fixture
def fake_audio():
    api = FakeSoundDevice()
    return api, WindowsKernelAudio(sounddevice=api)


def test_lazy_optional_import_and_non_windows_refusal(monkeypatch):
    calls = []
    monkeypatch.setattr(module, "os", types.SimpleNamespace(name="posix"))
    monkeypatch.setattr(module.importlib, "import_module", lambda name: calls.append(name))
    audio = WindowsKernelAudio()
    assert not calls
    with pytest.raises(AudioError, match="only on Windows"):
        audio.devices()
    assert not calls


def test_missing_optional_dependency_is_safe(monkeypatch):
    monkeypatch.setattr(module, "os", types.SimpleNamespace(name="nt"))

    def missing(name):
        raise ImportError("private library details")

    monkeypatch.setattr(module.importlib, "import_module", missing)
    with pytest.raises(AudioError, match="optional sounddevice") as error:
        WindowsKernelAudio().devices()
    assert "private library details" not in str(error.value)


def test_only_exact_ks_host_api_and_supported_directions_enumerated(fake_audio):
    api, audio = fake_audio
    assert audio.devices() == [
        OUTPUT,
        INPUT,
        AudioDevice(3, "Duplex endpoint", "input"),
        AudioDevice(3, "Duplex endpoint", "output"),
    ]
    assert not any(call[0] in ("open", "check") for call in api.calls)


def test_enumeration_errors_do_not_leak_driver_details(fake_audio):
    api, audio = fake_audio
    api.errors["enumerate"] = RuntimeError("private driver data")
    with pytest.raises(AudioError, match="enumeration failed") as error:
        audio.devices()
    assert "private driver data" not in str(error.value)


@pytest.mark.parametrize("device", [INPUT, OUTPUT])
@pytest.mark.parametrize("rate", [8000, 16000])
def test_queries_one_explicit_application_format_without_stream_open(fake_audio, device, rate):
    api, audio = fake_audio
    assert audio.query_format(device, rate)
    assert api.calls[-1] == (
        "check",
        device.direction,
        dict(device=device.id, channels=1, dtype="int16", samplerate=rate),
    )
    assert not any(call[0] == "open" for call in api.calls)


@pytest.mark.parametrize("code", [-9998, -9997, -9994, -9993])
def test_unsupported_format_is_false_without_fallback(fake_audio, code):
    api, audio = fake_audio
    api.errors["check"] = api.PortAudioError("private format", code)
    assert audio.query_format(INPUT, 16000) is False
    assert len([call for call in api.calls if call[0] == "check"]) == 1


def test_other_query_error_is_safe_audioerror(fake_audio):
    api, audio = fake_audio
    api.errors["check"] = api.PortAudioError("private host error", -9999)
    with pytest.raises(AudioError, match="format query failed") as error:
        audio.query_format(INPUT, 16000)
    assert "private" not in str(error.value)


@pytest.mark.parametrize(
    "device",
    [
        None,
        AudioDevice(-1, INPUT.name, "input"),
        AudioDevice(0xFFFFFFFF, INPUT.name, "input"),
        AudioDevice(True, INPUT.name, "input"),
        AudioDevice(INPUT.id, "", "input"),
        AudioDevice(INPUT.id, "Stale name", "input"),
        AudioDevice(0, "Default laptop", "input"),
        AudioDevice(INPUT.id, INPUT.name, "invalid"),
    ],
)
def test_default_other_host_invalid_or_stale_selection_rejected(fake_audio, device):
    api, audio = fake_audio
    with pytest.raises(AudioError):
        audio.query_format(device, 16000)
    assert not any(call[0] in ("open", "check") for call in api.calls)


@pytest.mark.parametrize("rate", [0, 44100, True, 16000.0, None])
def test_invalid_rate_refused_without_api_calls(fake_audio, rate):
    api, audio = fake_audio
    with pytest.raises(AudioError, match="sample rate"):
        audio.record(INPUT, 1, rate)
    assert not api.calls


@pytest.mark.parametrize("seconds", [0, -1, 11, True, 1.0, 0.1])
def test_invalid_duration_refused_without_api_calls(fake_audio, seconds):
    api, audio = fake_audio
    with pytest.raises(AudioError, match="1 through 10"):
        audio.record(INPUT, seconds)
    assert not api.calls


@pytest.mark.parametrize("rate,seconds", [(8000, 1), (16000, 1), (16000, 10)])
def test_callback_capture_returns_exact_valid_pcm16_wav(fake_audio, rate, seconds):
    api, audio = fake_audio
    recording = audio.record(INPUT, seconds, rate)
    parsed_rate, pcm = validate_audio(recording)
    assert parsed_rate == rate
    assert pcm == b"\x12\x00" * rate * seconds
    assert api.calls[-1] == ("close",)
    assert not any(call[0] == "abort" for call in api.calls)
    opened = [call for call in api.calls if call[0] == "open"]
    assert len(opened) == 1
    assert opened[0][2]["device"] == INPUT.id
    assert opened[0][2]["dtype"] == "int16"
    assert opened[0][2]["channels"] == 1
    assert opened[0][2]["samplerate"] == rate


def test_output_last_callback_is_zero_padded_then_drained(fake_audio):
    api, audio = fake_audio
    audio.play(OUTPUT, wav_bytes(rate=8000, frames=161))
    assert api.played == b"\x12\x00" * 161 + bytes(159 * 2)
    assert api.calls[-1] == ("close",)
    assert not any(call[0] == "abort" for call in api.calls)


def test_varying_input_callback_size_trims_exact_requested_recording(fake_audio):
    api, audio = fake_audio
    api.callback_frames = 173
    _, pcm = validate_audio(audio.record(INPUT, 1, 8000))
    assert pcm == b"\x12\x00" * 8000
    assert api.calls[-1] == ("close",)


def test_varying_output_callback_size_pads_only_last_block(fake_audio):
    api, audio = fake_audio
    api.callback_frames = 120
    audio.play(OUTPUT, wav_bytes(rate=8000, frames=161))
    assert api.played == b"\x12\x00" * 161 + bytes(79 * 2)
    assert api.calls[-1] == ("close",)


def test_direction_mismatch_rejected_without_open(fake_audio):
    api, audio = fake_audio
    with pytest.raises(AudioError, match="required direction"):
        audio.record(OUTPUT, 1)
    with pytest.raises(AudioError, match="required direction"):
        audio.play(INPUT, wav_bytes())
    assert not any(call[0] == "open" for call in api.calls)


def test_metadata_changed_during_open_aborts_before_start(fake_audio):
    api, audio = fake_audio
    api.after_open = lambda: api.device_list[2].update(name="Unrelated microphone")
    with pytest.raises(AudioError, match="changed or disconnected"):
        audio.record(INPUT, 1)
    assert not any(call[0] == "start" for call in api.calls)
    assert api.calls[-2:] == [("abort",), ("close",)]


@pytest.mark.parametrize(
    "override",
    [
        {"device": 0},
        {"channels": 2},
        {"dtype": "float32"},
        {"samplerate": 48000},
    ],
)
def test_actual_stream_must_retain_pinned_device_and_application_format(fake_audio, override):
    api, audio = fake_audio
    api.override = override
    with pytest.raises(AudioError, match="requested device and format"):
        audio.record(INPUT, 1)
    assert not any(call[0] == "start" for call in api.calls)
    assert api.calls[-2:] == [("abort",), ("close",)]


@pytest.mark.parametrize("failed_stage", ["open", "start"])
def test_native_errors_are_safe_and_close_open_stream(fake_audio, failed_stage):
    api, audio = fake_audio
    api.errors[failed_stage] = api.PortAudioError("private endpoint data", -9999)
    with pytest.raises(AudioError, match="access failed") as error:
        audio.record(INPUT, 1)
    assert "private" not in str(error.value)
    if failed_stage == "start":
        assert api.calls[-2:] == [("abort",), ("close",)]
    else:
        assert not any(call[0] == "close" for call in api.calls)


def test_keyboardinterrupt_aborts_closes_and_propagates(fake_audio):
    api, audio = fake_audio
    api.errors["start"] = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        audio.record(INPUT, 1)
    assert api.calls[-2:] == [("abort",), ("close",)]


@pytest.mark.parametrize("failed_stage", ["check", "open", "start"])
def test_unavailable_endpoint_has_safe_actionable_failure_and_no_retry(fake_audio, failed_stage):
    api, audio = fake_audio
    api.errors[failed_stage] = api.PortAudioError("private occupied endpoint", -9985)
    with pytest.raises(AudioError, match="unavailable or occupied; no retry") as error:
        if failed_stage == "check":
            audio.query_format(INPUT, 16000)
        else:
            audio.record(INPUT, 1)
    assert "private" not in str(error.value)
    opened = [call for call in api.calls if call[0] == "open"]
    assert len(opened) == (0 if failed_stage == "check" else 1)
    assert all(call[2]["device"] == INPUT.id for call in opened)
    if failed_stage == "start":
        assert api.calls[-2:] == [("abort",), ("close",)]


@pytest.mark.parametrize("mode", ["status", "malformed", "short"])
def test_callback_failures_or_incomplete_audio_refused_and_released(fake_audio, mode):
    api, audio = fake_audio
    setattr(api, mode, True)
    with pytest.raises(AudioError, match="incomplete audio"):
        audio.record(INPUT, 1)
    assert api.calls[-2:] == [("abort",), ("close",)]


def test_output_error_still_fills_entire_callback_with_silence(fake_audio):
    api, audio = fake_audio
    api.status = True
    with pytest.raises(AudioError, match="incomplete audio"):
        audio.play(OUTPUT, wav_bytes())
    assert api.played == bytes(320 * 2)
    assert api.calls[-2:] == [("abort",), ("close",)]


def test_timeout_is_bounded_and_aborts_closes(fake_audio, monkeypatch):
    api, audio = fake_audio
    api.no_finish = True
    timeouts = []

    class FakeEvent:
        def set(self):
            pass

        def wait(self, timeout):
            timeouts.append(timeout)
            return False

    monkeypatch.setattr(module.threading, "Event", FakeEvent)
    with pytest.raises(AudioError, match="time limit"):
        audio.record(INPUT, 10)
    assert timeouts == [12.0]
    assert api.calls[-2:] == [("abort",), ("close",)]


def test_abort_error_still_closes_and_preserves_primary_failure(fake_audio):
    api, audio = fake_audio
    api.short = True
    api.errors["abort"] = api.PortAudioError("private abort", -9999)
    with pytest.raises(AudioError, match="incomplete audio"):
        audio.record(INPUT, 1)
    assert api.calls[-2:] == [("abort",), ("close",)]


def test_failed_close_retains_stream_and_callback_resources(fake_audio):
    api, audio = fake_audio
    retained = module._UNRELEASED_STREAMS
    previous = len(retained)
    api.errors["close"] = api.PortAudioError("private close", -9999)
    try:
        with pytest.raises(AudioError, match="safely release"):
            audio.record(INPUT, 1)
        assert len(retained) == previous + 1
        assert retained[-1][0] is api.stream
        assert retained[-1][1][1].tobytes() == b"\x12\x00" * 16000
    finally:
        # Fake streams own no native buffers and can safely be removed here.
        del retained[previous:]


def test_invalid_wav_refused_before_optional_import_or_device_access(fake_audio):
    api, audio = fake_audio
    with pytest.raises(AudioError):
        audio.play(OUTPUT, b"not a complete wav")
    assert not api.calls
