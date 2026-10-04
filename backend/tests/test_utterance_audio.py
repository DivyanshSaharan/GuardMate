"""Fake JSONL native workers only; no devices, models, speech, or HTTP."""

import io
import json
import subprocess
import types
from dataclasses import asdict

import pytest
from guardmate.cellular import AudioError, validate_audio
from guardmate.cellular import utterance_audio as module
from guardmate.cellular import utterance_worker as worker
from guardmate.cellular.call_errors import CallError
from guardmate.cellular.utterance import NoSpeech, UtteranceConfig, UtteranceTooLong
from guardmate.cellular.utterance_audio import UtteranceCallAudio
from test_call_audio import INPUT, OUTPUT, FakeProcess, wav_bytes


class FakeNative:
    def __init__(self):
        self.calls = []
        self.devices_list = [INPUT, OUTPUT]
        self.error = None
        self.armed = True
        self.closed = False
        self.wav = wav_bytes(16000, 16000)

    def devices(self):
        self.calls.append(("list",))
        return self.devices_list

    def listen_for_utterance(self, device, *, config, on_armed):
        self.calls.append(("listen", device, config))
        try:
            if self.armed:
                on_armed()
            if self.error:
                raise self.error
            return self.wav
        finally:
            self.closed = True


class FakeListenerRun:
    def __init__(self):
        self.calls = []
        self.native = FakeNative()
        self.error = None
        self.mutate = None
        self.mutate_ready = None
        self.body = None
        self.returncode = 0

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if self.error:
            raise self.error
        request = module.decode_json(kwargs["input"])

        def emit(value):
            assert not self.native.closed
            if self.mutate_ready:
                self.mutate_ready(value)
            kwargs["on_event"](value)

        response = worker.handle(request, emit=emit, audio=self.native)
        if self.mutate:
            self.mutate(response)
        body = self.body if self.body is not None else json.dumps(response).encode()
        return subprocess.CompletedProcess(
            command, self.returncode, body, b"private native details"
        )


@pytest.fixture
def ready_audio(tmp_path):
    run = FakeListenerRun()
    return run, UtteranceCallAudio(tmp_path, consent=True, exclusive_risk=True, listen_run=run)


def test_one_child_armed_callback_precedes_closed_complete_return(ready_audio):
    run, audio = ready_audio
    callbacks = []

    def ready():
        assert not run.native.closed
        callbacks.append(True)

    wav = audio.listen(INPUT, on_armed=ready)
    assert callbacks == [True] and run.native.closed
    assert validate_audio(wav)[0] == 16000
    assert len(run.calls) == 1
    assert run.native.calls == [("list",), ("listen", INPUT, UtteranceConfig())]
    assert run.calls[0][1]["timeout"] == 30


@pytest.mark.parametrize("consent,risk", [(False, False), (True, False), (False, True), (1, True)])
def test_acknowledgements_required_before_worker(tmp_path, consent, risk):
    run = FakeListenerRun()
    audio = UtteranceCallAudio(tmp_path, consent=consent, exclusive_risk=risk, listen_run=run)
    with pytest.raises(CallError, match="consenting call"):
        audio.listen(INPUT)
    assert not run.calls


@pytest.mark.parametrize("device", [None, OUTPUT])
def test_explicit_phone_input_required_before_worker(ready_audio, device):
    run, audio = ready_audio
    with pytest.raises(CallError, match="capture endpoint"):
        audio.listen(device)
    assert not run.calls


@pytest.mark.parametrize("error", [NoSpeech("quiet"), UtteranceTooLong("overlong")])
def test_known_outcomes_are_typed_non_uncertain_without_retry(ready_audio, error):
    run, audio = ready_audio
    run.native.error = error
    with pytest.raises(type(error)) as result:
        audio.listen(INPUT)
    assert result.value.uncertain is False
    assert run.native.closed
    assert len(run.calls) == 1


def test_known_outcome_without_actual_armed_signal_is_uncertain(ready_audio):
    run, audio = ready_audio
    run.native.armed = False
    run.native.error = NoSpeech("quiet")
    with pytest.raises(CallError) as error:
        audio.listen(INPUT)
    assert type(error.value) is CallError and error.value.uncertain


def test_malformed_final_config_bool_integer_identity_is_uncertain(tmp_path):
    run = FakeListenerRun()
    config = UtteranceConfig(pre_roll_ms=0)
    audio = UtteranceCallAudio(
        tmp_path, consent=True, exclusive_risk=True, config=config, listen_run=run
    )
    run.mutate = lambda value: value["config"].update(pre_roll_ms=False)
    with pytest.raises(CallError, match="invalid result") as error:
        audio.listen(INPUT)
    assert error.value.uncertain


@pytest.mark.parametrize(
    "mutation",
    [
        {"mode": "wrong"},
        {"version": True},
        {"transport": "winmm"},
        {"action": "record"},
        {"device": {"id": True, "name": INPUT.name, "direction": "input"}},
        {"sample_rate": 8000},
        {"pcm_bytes": 1},
        {"audio_bytes": True},
        {"extra": "raw details"},
    ],
)
def test_malformed_identity_or_audio_report_fails_uncertain(ready_audio, mutation):
    run, audio = ready_audio
    run.mutate = lambda value: value.update(mutation)
    with pytest.raises(CallError, match="invalid result") as error:
        audio.listen(INPUT)
    assert error.value.uncertain
    assert len(run.calls) == 1


def test_wrong_armed_identity_does_not_invoke_ui_callback(ready_audio):
    run, audio = ready_audio
    run.mutate_ready = lambda value: value.update(mode="wrong worker")
    callbacks = []
    with pytest.raises(CallError) as error:
        audio.listen(INPUT, on_armed=lambda: callbacks.append(True))
    assert error.value.uncertain and not callbacks


def test_duplicate_armed_event_is_refused(ready_audio):
    run, audio = ready_audio

    def duplicate(command, **kwargs):
        request = module.decode_json(kwargs["input"])
        identity = {
            key: request[key]
            for key in ("version", "mode", "transport", "action", "device", "config")
        }
        event = {**identity, "event": "armed"}
        kwargs["on_event"](event)
        kwargs["on_event"](event)

    audio._listen_run = duplicate
    callbacks = []
    with pytest.raises(CallError) as error:
        audio.listen(INPUT, on_armed=lambda: callbacks.append(True))
    assert error.value.uncertain and callbacks == [True]


def test_short_or_overlong_capture_cannot_be_transcribed(ready_audio):
    run, audio = ready_audio
    for wav in (wav_bytes(16000, 100), wav_bytes(16000, 160001)):
        run.native.wav = wav
        with pytest.raises(CallError) as error:
            audio.listen(INPUT)
        assert error.value.uncertain
        run.native.closed = False


@pytest.mark.parametrize(
    "body",
    [
        b"private diagnostic",
        b'{"x":NaN}',
        b'{"x":1.0}',
        b'{"x":1,"x":2}',
        bytes(module.MAX_BODY_BYTES + 1),
    ],
    ids=["diagnostic", "nan", "float", "duplicate", "huge"],
)
def test_invalid_json_or_native_output_is_not_forwarded(ready_audio, body):
    run, audio = ready_audio
    run.body = body
    with pytest.raises(CallError, match="invalid result") as error:
        audio.listen(INPUT)
    assert error.value.uncertain and "private" not in str(error.value)


@pytest.mark.parametrize(
    "error", [subprocess.TimeoutExpired("child", 30), RuntimeError("private details")]
)
def test_worker_failure_is_uncertain_and_never_retried(ready_audio, error):
    run, audio = ready_audio
    run.error = error
    with pytest.raises(CallError, match="do not retry") as result:
        audio.listen(INPUT)
    assert result.value.uncertain and "private" not in str(result.value)
    assert len(run.calls) == 1


def test_native_driver_diagnostic_is_controlled(ready_audio):
    run, audio = ready_audio
    run.native.error = AudioError("private driver details")
    with pytest.raises(CallError, match="failed") as error:
        audio.listen(INPUT)
    assert error.value.uncertain and "private" not in str(error.value)


def test_secret_free_isolated_hidden_worker_configuration(ready_audio, monkeypatch):
    run, audio = ready_audio
    monkeypatch.setenv("OPENAI_API_KEY", "private key")
    monkeypatch.setenv("PYTHONPATH", "untrusted")
    audio.listen(INPUT)
    command, kwargs = run.calls[0]
    assert command[1:3] == ["-I", "-B"] and command[-1].endswith("utterance_worker.py")
    assert kwargs["env"][module.MARKER] == "1"
    assert not {"OPENAI_API_KEY", "PYTHONPATH"} & kwargs["env"].keys()
    assert kwargs["stderr"] == subprocess.DEVNULL


def valid_request():
    return dict(
        version=1,
        mode=module.MODE,
        transport="wdm-ks",
        action="listen",
        device=asdict(INPUT),
        config=asdict(UtteranceConfig()),
        consent=True,
        exclusive_risk=True,
    )


@pytest.mark.parametrize(
    "mutation",
    [
        {"consent": 1},
        {"exclusive_risk": False},
        {"action": []},
        {"version": True},
        {"extra": "bad"},
    ],
)
def test_worker_invalid_input_refused_before_native_metadata(mutation):
    native = FakeNative()
    request = valid_request()
    request.update(mutation)
    response = worker.handle(request, emit=lambda value: pytest.fail("Must not arm"), audio=native)
    assert response["code"] == "invalid_request" and not native.calls


def test_stale_phone_pin_refused_before_open():
    native = FakeNative()
    native.devices_list = [OUTPUT]
    response = worker.handle(
        valid_request(), emit=lambda value: pytest.fail("Must not arm"), audio=native
    )
    assert response["code"] == "endpoint_changed"
    assert native.calls == [("list",)]


def test_worker_marker_and_input_bound_checked_before_native(monkeypatch):
    monkeypatch.delenv(module.MARKER, raising=False)
    assert worker.main() == 2
    monkeypatch.setenv(module.MARKER, "1")
    monkeypatch.setattr(
        worker.sys,
        "stdin",
        types.SimpleNamespace(buffer=io.BytesIO(bytes(module.MAX_BODY_BYTES + 1))),
    )
    monkeypatch.setattr(
        worker, "WindowsKernelAudio", lambda: pytest.fail("Must not load native API")
    )
    assert worker.main() == 2


def event_stream(*values):
    return b"".join(json.dumps(value).encode() + b"\n" for value in values)


def test_default_supervisor_consumes_ready_incrementally_and_hides_window(monkeypatch):
    events = [{"event": "armed"}, {"event": "result", "ok": True}]
    process = FakeProcess(event_stream(*events))
    options = []
    seen = []
    monkeypatch.setattr(module, "os", types.SimpleNamespace(name="nt"))
    monkeypatch.setattr(module.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(
        module.subprocess, "Popen", lambda command, **kwargs: options.append(kwargs) or process
    )
    result = module._listener_run(
        ["fake"], input=b"{}", timeout=30, cwd="fake", env={}, on_event=seen.append
    )
    assert seen == [events[0]] and module.decode_json(result.stdout) == events[1]
    assert options[0]["creationflags"] == 0x08000000 and options[0]["shell"] is False


@pytest.mark.parametrize(
    "body",
    [
        b"private raw diagnostics\n",
        event_stream({"event": "unknown"}),
        event_stream({"event": "result"}, {"event": "armed"}),
        b'{"event":"result"}',
        bytes(module.MAX_BODY_BYTES + 1),
    ],
    ids=["diagnostics", "unknown", "after-result", "missing-newline", "huge"],
)
def test_default_supervisor_kills_malformed_event_stream(monkeypatch, body):
    process = FakeProcess(body)
    monkeypatch.setattr(module.subprocess, "Popen", lambda *args, **kwargs: process)
    with pytest.raises(subprocess.SubprocessError):
        module._listener_run(
            ["fake"], input=b"{}", timeout=30, cwd="fake", env={}, on_event=lambda value: None
        )
    assert process.killed


@pytest.mark.parametrize("error", [subprocess.TimeoutExpired("fake", 30), KeyboardInterrupt()])
def test_default_supervisor_kills_on_timeout_or_ctrl_c(monkeypatch, error):
    process = FakeProcess(event_stream({"event": "result"}), error=error)
    monkeypatch.setattr(module.subprocess, "Popen", lambda *args, **kwargs: process)
    with pytest.raises(type(error)):
        module._listener_run(
            ["fake"], input=b"{}", timeout=30, cwd="fake", env={}, on_event=lambda value: None
        )
    assert process.killed and process.stdin.closed and process.stdout.closed
