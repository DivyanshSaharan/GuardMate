"""CLI consent/interactive gates, using no sockets, hardware or native inference."""

import json
from types import SimpleNamespace

import pytest
from guardmate.cellular import backend_client, call_audio
from guardmate.cellular.call_errors import CallError
from guardmate.cellular.windows_audio import AudioDevice
from scripts import run_cellular_call as cli

INPUT = AudioDevice(25, "Input (vivo T2x 5G)", "input")
OUTPUT = AudioDevice(24, "Output (vivo T2x 5G)", "output")
READY = {
    "setup_complete": True,
    "delivery_mode_active": True,
    "model_configured": True,
    "stt_ready": True,
    "tts_ready": True,
}
LIVE = [
    "--run",
    "--input-id",
    "25",
    "--output-id",
    "24",
    "--ack-consenting-test-call",
    "--ack-exclusive-audio-risk",
    "--ack-hosted-transcripts",
]


@pytest.fixture
def context(monkeypatch):
    calls = []
    controls = {"ready": READY.copy(), "error": None}

    class FakeBackend:
        def __init__(self, url):
            calls.append(("backend", url))

        def inspect(self):
            calls.append("inspect")
            return controls["ready"]

    class FakeAudio:
        def __init__(self, root, **kwargs):
            calls.append(("audio", kwargs))

        def devices(self):
            calls.append("devices")
            return [INPUT, OUTPUT, AudioDevice(0, "Laptop microphone", "input")]

    class FakeRunner:
        def __init__(self, *args, **kwargs):
            calls.append(("runner", kwargs))
            self.stopped = False
            self.uncertain = False
            self.session = SimpleNamespace(id="known-session")

        def start(self, label):
            calls.append(("start", label))

        def listen(self):
            calls.append("listen")

        def edit(self, text):
            calls.append(("edit", text))

        def send(self):
            calls.append("send")
            if controls["error"]:
                self.uncertain = controls["error"].uncertain
                raise controls["error"]

        def discard(self):
            calls.append("discard")

        def speak(self, *, repeat=False):
            calls.append(("speak", repeat))

        def refresh(self):
            calls.append("refresh")

        def stop(self):
            calls.append("stop")
            self.stopped = True

    monkeypatch.setattr(backend_client, "BackendClient", FakeBackend)
    monkeypatch.setattr(call_audio, "LocalCallAudio", FakeAudio)
    monkeypatch.setattr(call_audio, "reply_audio", lambda audio: audio)
    monkeypatch.setattr(cli, "ManualCallRunner", FakeRunner)
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    return calls, controls


def answers(monkeypatch, values):
    sequence = iter(values)

    def reply(_prompt):
        try:
            value = next(sequence)
        except StopIteration:
            raise AssertionError("Unexpected additional terminal prompt") from None
        if isinstance(value, BaseException):
            raise value
        return value

    monkeypatch.setattr("builtins.input", reply)


def test_default_inspects_only_and_filters_laptop_devices(context, capsys):
    assert cli.main([]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["audio_stream_started"] is False
    assert result["model_requests_sent"] == 0
    assert result["phone_devices"] == [
        {"id": 25, "name": INPUT.name, "direction": "input"},
        {"id": 24, "name": OUTPUT.name, "direction": "output"},
    ]
    assert not any(
        isinstance(item, tuple) and item[0] in ("runner", "start") for item in context[0]
    )


@pytest.mark.parametrize(
    "missing",
    ["--ack-consenting-test-call", "--ack-exclusive-audio-risk", "--ack-hosted-transcripts"],
)
def test_each_ack_required_before_backend_or_audio_access(context, missing):
    with pytest.raises(SystemExit) as error:
        cli.main([item for item in LIVE if item != missing])
    assert error.value.code == 2 and context[0] == []


def test_noninteractive_mode_refused_before_any_io(context, monkeypatch):
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: False))
    with pytest.raises(SystemExit):
        cli.main(LIVE)
    assert context[0] == []


@pytest.mark.parametrize(
    "extra",
    [
        ["--seconds", "0"],
        ["--seconds", "11"],
        ["--max-turns", "0"],
        ["--max-turns", "11"],
        ["--label", ""],
        ["--label", "a" * 81],
        ["--input-id", "-1"],
    ],
)
def test_invalid_bounds_refused_before_any_io(context, extra):
    with pytest.raises(SystemExit):
        cli.main([*LIVE, *extra])
    assert context[0] == []


def test_no_session_or_audio_before_exact_call_ready_confirmation(context, monkeypatch, capsys):
    answers(monkeypatch, ["not ready"])
    assert cli.main(LIVE) == 0
    assert "session_created" in capsys.readouterr().out
    assert not any(
        isinstance(item, tuple) and item[0] in ("runner", "start") for item in context[0]
    )


def test_incomplete_setup_refused_before_call_confirmation(context, capsys):
    context[1]["ready"]["delivery_mode_active"] = False
    assert cli.main(LIVE) == 1
    assert not any(isinstance(item, tuple) and item[0] == "runner" for item in context[0])
    assert "enable a delivery window" in capsys.readouterr().out


def test_commands_stay_explicit_and_stop_does_not_hang_up_phone(context, monkeypatch, capsys):
    answers(
        monkeypatch,
        [
            "CALL READY",
            "listen",
            "edit",
            "yes, prepaid",
            "send",
            "discard",
            "speak",
            "repeat",
            "refresh",
            "stop",
        ],
    )
    assert cli.main(LIVE) == 0
    calls = context[0]
    assert ("start", "Manual cellular test") in calls
    assert "listen" in calls and ("edit", "yes, prepaid") in calls
    assert calls.count("send") == 1 and calls[-1] == "stop"
    assert ("speak", False) in calls and ("speak", True) in calls
    assert "End the physical call yourself" in capsys.readouterr().out


def test_uncertain_send_exits_without_consuming_queued_commands(context, monkeypatch, capsys):
    context[1]["error"] = CallError("Uncertain hosted turn", uncertain=True)
    answers(monkeypatch, ["CALL READY", "send", "listen", "send", "stop"])
    assert cli.main(LIVE) == 1
    assert context[0].count("send") == 1 and "listen" not in context[0]
    assert "stop" not in context[0]
    assert "No resend/retry" in capsys.readouterr().out


def test_known_validation_failure_allows_operator_to_stop(context, monkeypatch):
    context[1]["error"] = CallError("There is no draft")
    answers(monkeypatch, ["CALL READY", "send", "stop"])
    assert cli.main(LIVE) == 0
    assert context[0][-1] == "stop"


@pytest.mark.parametrize("failure", [KeyboardInterrupt(), EOFError()])
def test_interrupt_ends_only_known_session_and_reports_manual_hangup(
    context, monkeypatch, capsys, failure
):
    answers(monkeypatch, ["CALL READY", failure])
    assert cli.main(LIVE) == 130
    assert context[0][-1] == "stop"
    assert "Physical hangup is manual" in capsys.readouterr().out


def test_interrupt_before_call_ready_has_no_session_to_end(context, monkeypatch):
    answers(monkeypatch, [KeyboardInterrupt()])
    assert cli.main(LIVE) == 130
    assert "stop" not in context[0]


def test_terminal_output_escapes_caller_control_sequences(capsys):
    cli.event({"text": "courier\x1b[2J\nprivate"})
    output = capsys.readouterr().out
    assert "\x1b" not in output and "\\u001b" in output
    assert json.loads(output)["text"] == "courier\x1b[2J\nprivate"


def test_cli_does_not_import_settings_provider_or_load_keys():
    source = __import__("pathlib").Path(cli.__file__).read_text(encoding="utf-8")
    for forbidden in ("load_dotenv", "guardmate.main", "TinkerProvider", "TINKER_API_KEY"):
        assert forbidden not in source
