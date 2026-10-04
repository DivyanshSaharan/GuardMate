"""Manual call audio tests: fake native workers only, with no hardware or speech."""

import base64
import io
import json
import struct
import subprocess
import types
import wave
from dataclasses import asdict

import pytest
from guardmate.cellular import AudioDevice, AudioError, validate_audio
from guardmate.cellular import call_audio as module
from guardmate.cellular import call_audio_worker as worker
from guardmate.cellular.call_audio import LocalCallAudio, reply_audio
from guardmate.cellular.call_errors import CallError
from guardmate.cellular.kernel_audio import WindowsKernelAudio

INPUT = AudioDevice(1, "Microphone (vivo T2x 5G Hands-Free)", "input")
OUTPUT = AudioDevice(2, "Speakers (vivo T2x 5G Hands-Free)", "output")


def wav_bytes(rate=16000, frames=1600, *, channels=1, width=2):
    stream = io.BytesIO()
    with wave.open(stream, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(width)
        wav.setframerate(rate)
        wav.writeframes(b"\x12\x00" * (frames * channels * width // 2))
    return stream.getvalue()


def riff(chunks):
    body = b"WAVE" + chunks
    return b"RIFF" + struct.pack("<I", len(body)) + body


class FakeNative:
    def __init__(self):
        self.calls = []
        self.error = None
        self.device_list = [INPUT, OUTPUT, AudioDevice(0, "Local microphone", "input")]

    def devices(self):
        self.calls.append(("list",))
        return self.device_list

    def record(self, device, seconds, rate):
        self.calls.append(("record", device, seconds, rate))
        if self.error:
            raise self.error
        return wav_bytes(rate, rate * seconds)

    def play_reply(self, device, wav):
        self.calls.append(("play", device, wav))
        if self.error:
            raise self.error


class FakeRun:
    def __init__(self):
        self.calls = []
        self.native = FakeNative()
        self.mutate = None
        self.body = None
        self.error = None
        self.returncode = 0

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if self.error:
            raise self.error
        request = module.decode_json(kwargs["input"])
        response = worker.handle(request, audio=self.native)
        if self.mutate:
            self.mutate(response)
        body = self.body if self.body is not None else json.dumps(response).encode()
        return subprocess.CompletedProcess(command, self.returncode, body, b"private driver data")


@pytest.fixture
def ready_audio(tmp_path):
    run = FakeRun()
    return run, LocalCallAudio(tmp_path, consent=True, exclusive_risk=True, run=run)


@pytest.mark.parametrize("rate", [8000, 16000, 22050, 24000, 48000])
def test_reply_conversion_retains_entire_duration_and_pcm(rate):
    source = wav_bytes(rate, rate // 10)
    converted = reply_audio(source)
    parsed_rate, pcm = validate_audio(converted, max_seconds=30)
    assert parsed_rate == 16000
    assert len(pcm) == 1600 * 2
    assert pcm == b"\x12\x00" * 1600


def test_fractional_duration_rounds_up_instead_of_truncating():
    converted = reply_audio(wav_bytes(22050, 100))
    _, pcm = validate_audio(converted, max_seconds=30)
    assert len(pcm) // 2 == (100 * 16000 + 22049) // 22050


def test_exact_thirty_second_reply_is_not_truncated():
    source = wav_bytes(16000, 480000)
    assert reply_audio(source) == source


@pytest.mark.parametrize(
    "source",
    [
        b"",
        b"RIFF" + bytes(60),
        wav_bytes()[:-1],
        wav_bytes() + b"trailing",
        wav_bytes(7999),
        wav_bytes(48001),
        wav_bytes(channels=2),
        wav_bytes(width=1),
        wav_bytes(frames=0),
        wav_bytes(16000, 480001),
        bytes(2_000_001),
        bytearray(wav_bytes()),
    ],
    ids=[
        "empty",
        "bad-riff",
        "truncated",
        "trailing",
        "low-rate",
        "high-rate",
        "stereo",
        "8bit",
        "empty-pcm",
        "over30",
        "huge",
        "mutable",
    ],
)
def test_reply_conversion_refuses_invalid_or_overlong_audio(source):
    with pytest.raises(CallError):
        reply_audio(source)


@pytest.mark.parametrize(
    "offset,fmt,value",
    [
        (20, "<H", 3),
        (28, "<I", 1),
        (32, "<H", 4),
        (34, "<H", 32),
        (40, "<I", 0xFFFFFFFF),
    ],
)
def test_reply_metadata_and_chunk_lengths_must_be_consistent(offset, fmt, value):
    source = bytearray(wav_bytes())
    struct.pack_into(fmt, source, offset, value)
    with pytest.raises(CallError):
        reply_audio(bytes(source))


@pytest.mark.parametrize("chunk", ["fmt", "data"])
def test_duplicate_reply_chunks_refused(chunk):
    source = wav_bytes()
    duplicate = source[12:36] if chunk == "fmt" else source[36:]
    with pytest.raises(CallError):
        reply_audio(riff(source[12:] + duplicate))


def test_valid_odd_metadata_chunk_padding_is_consumed():
    source = wav_bytes()
    assert reply_audio(riff(b"JUNK\x01\x00\x00\x00x\x00" + source[12:])) == source


@pytest.mark.parametrize("pad", [b"", b"\xff"])
def test_missing_or_nonzero_odd_chunk_padding_refused(pad):
    source = wav_bytes()
    with pytest.raises(CallError):
        reply_audio(riff(b"JUNK\x01\x00\x00\x00x" + pad + source[12:]))


@pytest.mark.parametrize("bound", [True, 0, 31, 10.0, 30.1, None])
def test_validator_audio_bound_is_strict_whole_integer(bound):
    with pytest.raises(AudioError, match="whole-second"):
        validate_audio(wav_bytes(), max_seconds=bound)


def test_legacy_ten_second_play_and_new_reply_path_have_separate_bounds(monkeypatch):
    audio = WindowsKernelAudio(sounddevice=object())
    calls = []
    monkeypatch.setattr(audio, "_stream", lambda *args, **kwargs: calls.append((args, kwargs)))
    reply = wav_bytes(16000, 240000)
    with pytest.raises(AudioError, match="10 seconds"):
        audio.play(OUTPUT, reply)
    audio.play_reply(OUTPUT, reply)
    assert len(calls) == 1
    assert calls[0][0][:2] == (OUTPUT, 16000)
    assert len(calls[0][0][2]) == 480000
    assert calls[0][1] == {"recording": False}
    with pytest.raises(AudioError, match="16000 Hz"):
        audio.play_reply(OUTPUT, wav_bytes(8000, 800))
    assert len(calls) == 1


def test_devices_uses_fresh_metadata_worker_without_acknowledgements(tmp_path):
    run = FakeRun()
    audio = LocalCallAudio(tmp_path, run=run)
    assert audio.devices() == [INPUT, OUTPUT]
    assert audio.devices() == [INPUT, OUTPUT]
    assert len(run.calls) == 2
    assert run.native.calls == [("list",), ("list",)]
    assert all(call[1]["timeout"] == 10 for call in run.calls)


@pytest.mark.parametrize(
    "consent,risk", [(False, False), (True, False), (False, True), (1, True), (True, "yes")]
)
def test_live_audio_requires_both_literal_acknowledgements(tmp_path, consent, risk):
    run = FakeRun()
    audio = LocalCallAudio(tmp_path, consent=consent, exclusive_risk=risk, run=run)
    with pytest.raises(CallError, match="consenting call"):
        audio.record(INPUT, 1)
    with pytest.raises(CallError, match="consenting call"):
        audio.play(OUTPUT, wav_bytes())
    assert not run.calls


def test_record_and_reply_play_have_fixed_rate_exact_pins_and_timeouts(ready_audio):
    run, audio = ready_audio
    captured = audio.record(INPUT, 1)
    assert len(validate_audio(captured)[1]) == 32000
    assert run.native.calls[-1] == ("record", INPUT, 1, 16000)
    reply = wav_bytes(16000, 240000)
    assert audio.play(OUTPUT, reply) is None
    assert run.native.calls[-1] == ("play", OUTPUT, reply)
    assert [call[1]["timeout"] for call in run.calls] == [20, 40]


@pytest.mark.parametrize("seconds", [True, 0, -1, 11, 1.0, 0.1])
def test_invalid_capture_duration_never_starts_worker(ready_audio, seconds):
    run, audio = ready_audio
    with pytest.raises(CallError, match="1 through 10"):
        audio.record(INPUT, seconds)
    assert not run.calls


@pytest.mark.parametrize(
    "device",
    [
        None,
        AudioDevice(True, INPUT.name, "input"),
        AudioDevice(-1, INPUT.name, "input"),
        AudioDevice(0, "Local microphone", "input"),
        OUTPUT,
    ],
)
def test_invalid_default_or_other_device_never_starts_worker(ready_audio, device):
    run, audio = ready_audio
    with pytest.raises(CallError, match="explicit"):
        audio.record(device, 1)
    assert not run.calls


def test_secret_environment_and_user_configuration_not_passed(ready_audio, monkeypatch):
    run, audio = ready_audio
    monkeypatch.setenv("OPENAI_API_KEY", "private key")
    monkeypatch.setenv("AZURE_TOKEN", "private token")
    monkeypatch.setenv("PYTHONPATH", "untrusted startup")
    monkeypatch.setenv("USERPROFILE", "private profile")
    monkeypatch.setenv("SYSTEMROOT", "C:\\Windows")
    audio.devices()
    command, kwargs = run.calls[0]
    environment = kwargs["env"]
    assert environment[module.MARKER] == "1"
    assert environment["PYTHONNOUSERSITE"] == environment["PYTHONDONTWRITEBYTECODE"] == "1"
    assert not {"OPENAI_API_KEY", "AZURE_TOKEN", "PYTHONPATH", "USERPROFILE"} & environment.keys()
    assert command[1:3] == ["-I", "-B"]
    assert command[-1].endswith("call_audio_worker.py")
    assert kwargs["stdout"] == subprocess.PIPE and kwargs["stderr"] == subprocess.DEVNULL
    assert kwargs["check"] is False


@pytest.mark.parametrize("action", ["list", "record", "play"])
def test_timeouts_are_uncertain_for_live_audio_and_never_retried(ready_audio, action):
    run, audio = ready_audio
    run.error = subprocess.TimeoutExpired("child", 20, output=b"private diagnostic")
    with pytest.raises(CallError, match="timed out") as error:
        if action == "list":
            audio.devices()
        elif action == "record":
            audio.record(INPUT, 1)
        else:
            audio.play(OUTPUT, wav_bytes())
    assert error.value.uncertain is (action != "list")
    assert "private" not in str(error.value)
    assert len(run.calls) == 1


@pytest.mark.parametrize(
    "mutation",
    [
        {"mode": "another worker"},
        {"version": True},
        {"transport": "winmm"},
        {"action": "play"},
        {"device": asdict(OUTPUT)},
        {"seconds": True},
        {"sample_rate": 8000},
        {"audio_bytes": 1},
        {"pcm_bytes": 1},
        {"extra": "private"},
        {"device": {"id": True, "name": INPUT.name, "direction": "input"}},
    ],
)
def test_wrong_worker_identity_pins_types_or_lengths_fail_closed(ready_audio, mutation):
    run, audio = ready_audio
    run.mutate = lambda response: response.update(mutation)
    with pytest.raises(CallError, match="invalid result") as error:
        audio.record(INPUT, 1)
    assert error.value.uncertain
    assert len(run.calls) == 1


@pytest.mark.parametrize(
    "body",
    [
        b"private driver failure",
        b"{}",
        b'{"x":NaN}',
        b'{"x":1.0}',
        b'{"x":1,"x":2}',
        b"x" * (module.MAX_BODY_BYTES + 1),
    ],
    ids=["diagnostics", "empty-dict", "nan", "float", "duplicate", "huge"],
)
def test_malformed_huge_float_or_duplicate_json_not_forwarded(ready_audio, body):
    run, audio = ready_audio
    run.body = body
    with pytest.raises(CallError, match="invalid result") as error:
        audio.record(INPUT, 1)
    assert error.value.uncertain
    assert "private" not in str(error.value)


def test_short_worker_capture_even_with_consistent_report_is_refused(ready_audio):
    run, audio = ready_audio

    def shorten(response):
        wav = wav_bytes(16000, 15999)
        response.update(
            audio_base64=base64.b64encode(wav).decode(), audio_bytes=len(wav), pcm_bytes=31998
        )

    run.mutate = shorten
    with pytest.raises(CallError, match="invalid result"):
        audio.record(INPUT, 1)


def test_raw_native_failure_is_mapped_to_controlled_message(ready_audio):
    run, audio = ready_audio
    run.native.error = RuntimeError("private driver diagnostic")
    with pytest.raises(CallError, match="do not retry automatically") as error:
        audio.record(INPUT, 1)
    assert error.value.uncertain
    assert "private" not in str(error.value)
    assert len(run.calls) == 1


def test_occupied_endpoint_is_safe_uncertain_and_no_retry(ready_audio):
    run, audio = ready_audio
    run.native.error = AudioError("The WDM-KS endpoint is unavailable or occupied; no retry.")
    with pytest.raises(CallError, match="unavailable or occupied") as error:
        audio.record(INPUT, 1)
    assert error.value.uncertain
    assert len(run.calls) == 1


def request(action="record", **changes):
    value = dict(
        version=1,
        mode=module.MODE,
        transport="wdm-ks",
        action=action,
        consent=True,
        exclusive_risk=True,
    )
    if action == "record":
        value.update(device=asdict(INPUT), seconds=1)
    value.update(changes)
    return value


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        "bad",
        {},
        {"action": []},
        request(consent=False),
        request(exclusive_risk=False),
        request(seconds=1.0),
        request(version=True),
        request(extra="bad"),
    ],
)
def test_worker_invalid_request_is_controlled_before_any_native_access(value):
    native = FakeNative()
    response = worker.handle(value, audio=native)
    assert response["ok"] is False
    assert response["code"] == "invalid_request"
    assert response["uncertain"] is False
    assert not native.calls


def test_worker_stale_pin_does_not_capture():
    native = FakeNative()
    native.device_list = [OUTPUT]
    result = worker.handle(request(), audio=native)
    assert result["code"] == "endpoint_changed" and result["uncertain"] is False
    assert native.calls == [("list",)]


def test_worker_marker_required_without_reading_body(monkeypatch):
    monkeypatch.delenv(module.MARKER, raising=False)
    assert worker.main() == 2


def test_worker_oversize_body_rejected_before_metadata(monkeypatch):
    monkeypatch.setenv(module.MARKER, "1")
    monkeypatch.setattr(
        worker.sys,
        "stdin",
        types.SimpleNamespace(buffer=io.BytesIO(bytes(module.MAX_BODY_BYTES + 1))),
    )
    monkeypatch.setattr(
        worker, "WindowsKernelAudio", lambda: pytest.fail("Native API must remain unused")
    )
    assert worker.main() == 2


class FakeProcess:
    def __init__(self, output=b"{}", error=None):
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO(output)
        self.returncode = 0
        self.error = error
        self.killed = False

    def wait(self, *, timeout):
        if self.error and not self.killed:
            raise self.error
        return 0

    def kill(self):
        self.killed = True


def test_bounded_runner_hides_window_and_caps_stdout(monkeypatch):
    process = FakeProcess(bytes(module.MAX_BODY_BYTES + 1))
    options = []
    monkeypatch.setattr(module, "os", types.SimpleNamespace(name="nt"))
    monkeypatch.setattr(module.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(
        module.subprocess, "Popen", lambda command, **kwargs: options.append(kwargs) or process
    )
    with pytest.raises(module._OutputLimit):
        module._bounded_run(["fake"], input=b"{}", timeout=10, cwd="fake", env={})
    assert process.killed
    assert options[0]["creationflags"] == 0x08000000
    assert options[0]["stderr"] == subprocess.DEVNULL
    assert options[0]["shell"] is False


@pytest.mark.parametrize("error", [subprocess.TimeoutExpired("fake", 10), KeyboardInterrupt()])
def test_bounded_runner_kills_child_on_timeout_or_interrupt(monkeypatch, error):
    process = FakeProcess(error=error)
    monkeypatch.setattr(module.subprocess, "Popen", lambda *args, **kwargs: process)
    with pytest.raises(type(error)):
        module._bounded_run(["fake"], input=b"{}", timeout=10, cwd="fake", env={})
    assert process.killed
    assert process.stdin.closed and process.stdout.closed


def test_thread_start_failure_kills_and_reaps_spawned_worker(monkeypatch):
    process = FakeProcess()

    class FailedThread:
        ident = None

        def __init__(self, **kwargs):
            pass

        def start(self):
            raise RuntimeError("private thread diagnostic")

        def is_alive(self):
            return False

    monkeypatch.setattr(module.threading, "Thread", FailedThread)
    monkeypatch.setattr(module.subprocess, "Popen", lambda *args, **kwargs: process)
    with pytest.raises(RuntimeError):
        module._bounded_run(["fake"], input=b"{}", timeout=10, cwd="fake", env={})
    assert process.killed
    assert process.stdin.closed and process.stdout.closed
