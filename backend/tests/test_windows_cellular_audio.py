"""WinMM contract tests. Every audio API here is fake; no hardware is opened."""

import ctypes
import io
import struct
import types
import wave

import pytest
from guardmate.cellular import AudioDevice, AudioError, WindowsAudio, validate_audio
from guardmate.cellular import windows_audio as audio_module

INPUT_NAME = "Microphone (vivo T2x 5G Hands-Free HF Audio)"[:31]
OUTPUT_NAME = "Speakers (vivo T2x 5G Hands-Free HF Audio)"[:31]
INPUT = AudioDevice(1, INPUT_NAME, "input")
OUTPUT = AudioDevice(1, OUTPUT_NAME, "output")


def wav_bytes(*, rate=16000, channels=1, width=2, frames=16000):
    stream = io.BytesIO()
    with wave.open(stream, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(width)
        wav.setframerate(rate)
        wav.writeframes(b"\x12" * frames * channels * width)
    return stream.getvalue()


class FakeWinMM:
    def __init__(self):
        self.names = {
            "input": ["Local microphone", INPUT_NAME],
            "output": ["Local speakers", OUTPUT_NAME],
        }
        self.calls = []
        self.errors = {}
        self.handle_names = {}
        self.header = None
        self.complete = True
        self.short = False
        self.opened_name = None
        self.played = None

    def _result(self, name):
        result = self.errors.get(name, 0)
        if isinstance(result, BaseException):
            raise result
        if isinstance(result, list):
            return result.pop(0) if result else 0
        return result

    def waveInGetNumDevs(self):
        self.calls.append(("enumerate", "input"))
        return len(self.names["input"])

    def waveOutGetNumDevs(self):
        self.calls.append(("enumerate", "output"))
        return len(self.names["output"])

    def _caps(self, direction, identifier, pointer, size):
        self.calls.append(("caps", direction, identifier))
        caps = pointer._obj
        assert size == ctypes.sizeof(caps)
        name = (
            self.handle_names[identifier]
            if identifier in self.handle_names
            else self.names[direction][identifier]
        )
        encoded = name.encode("utf-16-le")[:62]
        for index in range(len(encoded) // 2):
            caps.szPname[index] = struct.unpack_from("<H", encoded, index * 2)[0]
        return self._result("caps")

    def waveInGetDevCapsW(self, *args):
        return self._caps("input", *args)

    def waveOutGetDevCapsW(self, *args):
        return self._caps("output", *args)

    def _open(self, direction, handle, identifier, fmt, callback, instance, flags):
        self.calls.append(("open", direction, identifier, flags))
        assert identifier != 0xFFFFFFFF
        assert callback == instance == 0
        fmt = fmt._obj
        assert (
            fmt.wFormatTag,
            fmt.nChannels,
            fmt.nAvgBytesPerSec,
            fmt.nBlockAlign,
            fmt.wBitsPerSample,
            fmt.cbSize,
        ) == (1, 1, fmt.nSamplesPerSec * 2, 2, 16, 0)
        if flags == audio_module._WAVE_FORMAT_QUERY:
            assert handle is None
            return self._result("query")
        assert flags == 0
        result = self._result("open")
        if result == 0:
            handle._obj.value = 101 if direction == "input" else 102
            self.handle_names[handle._obj.value] = (
                self.opened_name or self.names[direction][identifier]
            )
        return result

    def waveInOpen(self, *args):
        return self._open("input", *args)

    def waveOutOpen(self, *args):
        return self._open("output", *args)

    def _prepare(self, handle, header, size):
        self.calls.append(("prepare",))
        assert size == ctypes.sizeof(header._obj)
        self.header = header._obj
        result = self._result("prepare")
        if result == 0:
            self.header.dwFlags |= audio_module._WHDR_PREPARED
        return result

    waveInPrepareHeader = _prepare
    waveOutPrepareHeader = _prepare

    def waveInAddBuffer(self, handle, header, size):
        self.calls.append(("queue",))
        assert header._obj is self.header
        return self._result("queue")

    def waveInStart(self, handle):
        self.calls.append(("start",))
        result = self._result("start")
        if result == 0 and self.complete:
            size = self.header.dwBufferLength
            ctypes.memmove(self.header.lpData, b"\x12\x00" * (size // 2), size)
            self.header.dwBytesRecorded = size - (2 if self.short else 0)
            self.header.dwFlags |= audio_module._WHDR_DONE
        return result

    def waveOutWrite(self, handle, header, size):
        self.calls.append(("write",))
        result = self._result("write")
        self.played = ctypes.string_at(self.header.lpData, self.header.dwBufferLength)
        if result == 0 and self.complete:
            self.header.dwFlags |= audio_module._WHDR_DONE
        return result

    def _reset(self, handle):
        self.calls.append(("reset",))
        if self.header is not None:
            # Inspecting the backing memory here proves that cleanup still owns it.
            assert len(ctypes.string_at(self.header.lpData, self.header.dwBufferLength))
            self.header.dwFlags |= audio_module._WHDR_DONE
        return self._result("reset")

    waveInReset = _reset
    waveOutReset = _reset

    def _unprepare(self, handle, header, size):
        self.calls.append(("unprepare",))
        assert header._obj is self.header
        assert len(ctypes.string_at(header._obj.lpData, header._obj.dwBufferLength))
        result = self._result("unprepare")
        if result == 0:
            self.header.dwFlags &= ~audio_module._WHDR_PREPARED
        return result

    waveInUnprepareHeader = _unprepare
    waveOutUnprepareHeader = _unprepare

    def _close(self, handle):
        self.calls.append(("close",))
        assert handle.value in self.handle_names
        return self._result("close")

    waveInClose = _close
    waveOutClose = _close


@pytest.fixture
def fake_audio():
    fake = FakeWinMM()
    return fake, WindowsAudio(winmm=fake)


@pytest.fixture
def fast_clock(monkeypatch):
    state = {"now": 0.0}

    def monotonic():
        return state["now"]

    def sleep(seconds):
        state["now"] += seconds

    monkeypatch.setattr(
        audio_module, "time", types.SimpleNamespace(monotonic=monotonic, sleep=sleep)
    )
    return state


def test_ctypes_layout_uses_windows_widths_and_pointer_sizes():
    assert ctypes.sizeof(audio_module._WaveFormat) == 18
    assert ctypes.sizeof(audio_module._WaveInCaps) == 80
    assert ctypes.sizeof(audio_module._WaveOutCaps) == 84
    assert ctypes.sizeof(audio_module._WaveHeader) == (
        48 if ctypes.sizeof(ctypes.c_void_p) == 8 else 32
    )
    assert audio_module._WaveHeader.dwBufferLength.offset == ctypes.sizeof(ctypes.c_void_p)


def test_all_native_functions_have_full_prototypes():
    native = types.SimpleNamespace()
    names = {
        "waveIn": [
            "GetNumDevs",
            "GetDevCapsW",
            "Open",
            "PrepareHeader",
            "UnprepareHeader",
            "Reset",
            "Close",
            "AddBuffer",
            "Start",
        ],
        "waveOut": [
            "GetNumDevs",
            "GetDevCapsW",
            "Open",
            "PrepareHeader",
            "UnprepareHeader",
            "Reset",
            "Close",
            "Write",
        ],
    }
    for prefix, suffixes in names.items():
        for suffix in suffixes:
            setattr(native, prefix + suffix, types.SimpleNamespace())
    audio_module._configure(native)
    for prefix, suffixes in names.items():
        for suffix in suffixes:
            function = getattr(native, prefix + suffix)
            assert isinstance(function.argtypes, list)
            assert function.restype is ctypes.c_uint32
    assert native.waveInGetDevCapsW.argtypes[0] is ctypes.c_size_t
    assert native.waveOutOpen.argtypes[-2] is ctypes.c_size_t


def test_native_dll_is_lazy_and_non_windows_is_refused(monkeypatch):
    called = []
    monkeypatch.setattr(audio_module, "os", types.SimpleNamespace(name="posix"))
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args: called.append(args), raising=False)
    audio = WindowsAudio()
    assert not called
    with pytest.raises(AudioError, match="only on Windows"):
        audio.devices()
    assert not called


def test_enumerates_names_and_directions_without_open(fake_audio):
    fake, audio = fake_audio
    assert audio.devices() == [
        AudioDevice(0, "Local microphone", "input"),
        INPUT,
        AudioDevice(0, "Local speakers", "output"),
        OUTPUT,
    ]
    assert not any(call[0] == "open" for call in fake.calls)


def test_enumeration_failure_fails_closed(fake_audio):
    fake, audio = fake_audio
    fake.errors["caps"] = 2
    with pytest.raises(AudioError, match="enumeration failed"):
        audio.devices()
    assert not any(call[0] == "open" for call in fake.calls)


@pytest.mark.parametrize("device", [INPUT, OUTPUT])
@pytest.mark.parametrize("rate", [8000, 16000])
def test_query_only_never_opens_or_cleans_up_a_handle(fake_audio, device, rate):
    fake, audio = fake_audio
    assert audio.query_format(device, rate) is True
    assert fake.calls[-1] == ("open", device.direction, device.id, 1)
    assert not fake.handle_names
    assert not any(call[0] in ("reset", "prepare", "close") for call in fake.calls)


def test_query_unsupported_is_false_but_other_failure_is_error(fake_audio):
    fake, audio = fake_audio
    fake.errors["query"] = 32
    assert audio.query_format(INPUT, 16000) is False
    fake.errors["query"] = 5
    with pytest.raises(AudioError, match="query failed"):
        audio.query_format(INPUT, 16000)


@pytest.mark.parametrize(
    "device",
    [
        AudioDevice(-1, INPUT_NAME, "input"),
        AudioDevice(0xFFFFFFFF, INPUT_NAME, "input"),
        AudioDevice(True, INPUT_NAME, "input"),
        AudioDevice(1, "", "input"),
        AudioDevice(1, INPUT_NAME, "invalid"),
        AudioDevice(0, INPUT_NAME, "input"),
        AudioDevice(1, "Stale endpoint", "input"),
        None,
    ],
)
def test_default_invalid_and_stale_selection_never_opens(fake_audio, device):
    fake, audio = fake_audio
    with pytest.raises(AudioError):
        audio.query_format(device, 16000)
    assert not any(call[0] == "open" for call in fake.calls)


def test_each_use_reenumerates_and_detects_disconnect(fake_audio):
    fake, audio = fake_audio
    assert audio.query_format(INPUT, 8000)
    fake.names["input"][1] = "Another microphone"
    with pytest.raises(AudioError, match="changed or disconnected"):
        audio.record(INPUT, 1)
    assert len([call for call in fake.calls if call[0] == "open"]) == 1


@pytest.mark.parametrize("rate", [0, 44100, True, 16000.0, None])
def test_invalid_rates_fail_before_device_open(fake_audio, rate):
    fake, audio = fake_audio
    with pytest.raises(AudioError, match="sample rate"):
        audio.query_format(INPUT, rate)
    assert not fake.calls


@pytest.mark.parametrize("seconds", [0, -1, 11, True, 0.5, 1.0])
def test_record_duration_must_be_integer_1_to_10(fake_audio, seconds):
    fake, audio = fake_audio
    with pytest.raises(AudioError, match="1 through 10"):
        audio.record(INPUT, seconds)
    assert not fake.calls


@pytest.mark.parametrize("rate,seconds", [(8000, 1), (16000, 1), (16000, 10)])
def test_record_returns_complete_mono_pcm16_wav_and_releases(fake_audio, rate, seconds):
    fake, audio = fake_audio
    recording = audio.record(INPUT, seconds, rate)
    parsed_rate, pcm = validate_audio(recording)
    assert parsed_rate == rate
    assert pcm == b"\x12\x00" * rate * seconds
    assert fake.calls[-3:] == [("reset",), ("unprepare",), ("close",)]
    assert any(call == ("caps", "input", 101) for call in fake.calls)


def test_play_queues_only_pcm_then_releases(fake_audio):
    fake, audio = fake_audio
    recording = wav_bytes(rate=8000, frames=800)
    assert audio.play(OUTPUT, recording) is None
    assert fake.played == b"\x12" * 1600
    assert fake.calls[-3:] == [("reset",), ("unprepare",), ("close",)]
    assert any(call == ("caps", "output", 102) for call in fake.calls)


@pytest.mark.parametrize("operation", ["record", "play"])
def test_direction_mismatch_never_opens(fake_audio, operation):
    fake, audio = fake_audio
    with pytest.raises(AudioError, match="required direction"):
        if operation == "record":
            audio.record(OUTPUT, 1)
        else:
            audio.play(INPUT, wav_bytes())
    assert not any(call[0] == "open" for call in fake.calls)


def test_hotplug_between_enumeration_and_open_closes_before_any_buffer(fake_audio):
    fake, audio = fake_audio
    fake.opened_name = "Unrelated microphone"
    with pytest.raises(AudioError, match="changed while opening"):
        audio.record(INPUT, 1)
    assert not any(call[0] in ("prepare", "queue", "start") for call in fake.calls)
    assert fake.calls[-2:] == [("reset",), ("close",)]


@pytest.mark.parametrize("failed_stage", ["open", "prepare", "queue", "start", "write"])
def test_native_stage_error_releases_open_device(fake_audio, failed_stage):
    fake, audio = fake_audio
    fake.errors[failed_stage] = 5
    with pytest.raises(AudioError, match="could not"):
        if failed_stage == "write":
            audio.play(OUTPUT, wav_bytes())
        else:
            audio.record(INPUT, 1)
    if failed_stage == "open":
        assert not any(call[0] == "close" for call in fake.calls)
    elif failed_stage == "prepare":
        assert fake.calls[-2:] == [("reset",), ("close",)]
    else:
        assert fake.calls[-3:] == [("reset",), ("unprepare",), ("close",)]


def test_short_recording_rejected_and_released(fake_audio):
    fake, audio = fake_audio
    fake.short = True
    with pytest.raises(AudioError, match="incomplete recording"):
        audio.record(INPUT, 1)
    assert fake.calls[-3:] == [("reset",), ("unprepare",), ("close",)]


@pytest.mark.parametrize("operation", ["record", "play"])
def test_no_completion_times_out_and_releases(fake_audio, fast_clock, operation):
    fake, audio = fake_audio
    fake.complete = False
    with pytest.raises(AudioError, match="time limit"):
        if operation == "record":
            audio.record(INPUT, 1)
        else:
            audio.play(OUTPUT, wav_bytes())
    assert 3 <= fast_clock["now"] <= 3.02
    assert fake.calls[-3:] == [("reset",), ("unprepare",), ("close",)]


def test_keyboard_interrupt_releases_then_propagates(fake_audio):
    fake, audio = fake_audio
    fake.errors["start"] = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        audio.record(INPUT, 1)
    assert fake.calls[-3:] == [("reset",), ("unprepare",), ("close",)]


def test_unprepare_retries_stillplaying_without_freeing_buffer(fake_audio, fast_clock):
    fake, audio = fake_audio
    fake.errors["unprepare"] = [33, 33, 0]
    assert audio.record(INPUT, 1)
    assert fake.calls[-5:] == [
        ("reset",),
        ("unprepare",),
        ("unprepare",),
        ("unprepare",),
        ("close",),
    ]
    assert fast_clock["now"] == 0.02


@pytest.mark.parametrize("failed_stage", ["unprepare", "close"])
def test_failed_release_retains_native_memory_and_fails(fake_audio, fast_clock, failed_stage):
    fake, audio = fake_audio
    retained = audio_module._UNRELEASED_BUFFERS
    previous = len(retained)
    fake.errors[failed_stage] = 33 if failed_stage == "unprepare" else 5
    try:
        with pytest.raises(AudioError, match="safely release"):
            audio.record(INPUT, 1)
        assert len(retained) == previous + 1
        api, handle, header, buffer = retained[-1]
        assert api is fake
        assert handle.value == 101
        assert ctypes.string_at(header.lpData, header.dwBufferLength) == buffer.raw
        assert fake.calls[-1] == ("close",)
        assert fast_clock["now"] <= 0.501
    finally:
        # Only fake drivers are retained by this test, so no native owner exists.
        del retained[previous:]


def test_reset_exception_still_attempts_unprepare_and_close(fake_audio):
    fake, audio = fake_audio
    fake.errors["reset"] = OSError("private driver details")
    with pytest.raises(AudioError, match="safely release") as error:
        audio.record(INPUT, 1)
    assert "private driver details" not in str(error.value)
    assert fake.calls[-3:] == [("reset",), ("unprepare",), ("close",)]


@pytest.mark.parametrize(
    "recording",
    [
        b"",
        b"not a wav",
        wav_bytes()[:-2],
        wav_bytes() + b"extra",
        wav_bytes(rate=44100),
        wav_bytes(channels=2),
        wav_bytes(width=1),
        wav_bytes(frames=0),
        wav_bytes(rate=8000, frames=80001),
        bytearray(wav_bytes()),
    ],
    ids=[
        "empty",
        "not-wav",
        "truncated",
        "trailing",
        "44k",
        "stereo",
        "8bit",
        "zero",
        "long",
        "mutable",
    ],
)
def test_invalid_wav_is_rejected_before_any_device_access(fake_audio, recording):
    fake, audio = fake_audio
    with pytest.raises(AudioError):
        audio.play(OUTPUT, recording)
    assert not fake.calls


@pytest.mark.parametrize(
    "offset,fmt,value",
    [
        (20, "<H", 3),
        (28, "<I", 1),
        (32, "<H", 4),
        (34, "<H", 8),
        (40, "<I", 0xFFFFFFFF),
    ],
)
def test_inconsistent_pcm_metadata_and_chunk_lengths_rejected(offset, fmt, value):
    recording = bytearray(wav_bytes())
    struct.pack_into(fmt, recording, offset, value)
    with pytest.raises(AudioError):
        validate_audio(bytes(recording))


def test_duplicate_data_chunks_rejected():
    recording = wav_bytes(frames=1)
    duplicate = recording + recording[36:]
    duplicate = duplicate[:4] + struct.pack("<I", len(duplicate) - 8) + duplicate[8:]
    with pytest.raises(AudioError, match="one PCM data chunk"):
        validate_audio(duplicate)
