"""Automatic CLI consent, cancellation and lifecycle checks with fake modules only."""

import json
import sys
from types import SimpleNamespace

import pytest
from guardmate.cellular import backend_client, call_audio
from guardmate.cellular.call_errors import CallError
from guardmate.cellular.windows_audio import AudioDevice
from scripts import run_automatic_call as cli

INPUT = AudioDevice(25, "Input (vivo T2x 5G)", "input")
OUTPUT = AudioDevice(24, "Output (vivo T2x 5G)", "output")
READY = dict(
    setup_complete=True,
    delivery_mode_active=True,
    model_configured=True,
    stt_ready=True,
    tts_ready=True,
)
LIVE = [
    "--run",
    "--input-id",
    "25",
    "--output-id",
    "24",
    "--ack-consenting-test-call",
    "--ack-exclusive-audio-risk",
    "--ack-hosted-transcripts",
    "--ack-automatic-transcripts",
]


@pytest.fixture
def context(monkeypatch):
    calls = []
    controls = {"ready": READY.copy(), "failure": None, "stop_failure": None}

    class Backend:
        def __init__(self, url):
            calls.append(("backend", url))

        def inspect(self):
            calls.append("inspect")
            return controls["ready"]

    class Audio:
        def __init__(self, root, **kwargs):
            calls.append(("audio", kwargs))

        def devices(self):
            calls.append("devices")
            return [INPUT, OUTPUT]

    class Runner:
        def __init__(self, *args, **kwargs):
            calls.append(("runner", kwargs))
            self.session = SimpleNamespace(id="owned-id")

        def stop(self):
            calls.append("stop")
            if controls["stop_failure"]:
                raise controls["stop_failure"]

    class Coordinator:
        def __init__(self, runner, **kwargs):
            calls.append(("coordinator", kwargs))

        def run(self, label):
            calls.append(("run", label))
            if controls["failure"]:
                raise controls["failure"]
            return SimpleNamespace(
                status="paused", reason="awaiting_approval", session_id="owned-id", model_attempts=1
            )

    monkeypatch.setattr(backend_client, "BackendClient", Backend)
    monkeypatch.setattr(call_audio, "reply_audio", lambda audio: audio)
    monkeypatch.setitem(
        sys.modules, "guardmate.cellular.utterance_audio", SimpleNamespace(UtteranceCallAudio=Audio)
    )
    monkeypatch.setitem(
        sys.modules,
        "guardmate.cellular.automatic_call",
        SimpleNamespace(AutomaticCallCoordinator=Coordinator),
    )
    monkeypatch.setattr(cli, "ManualCallRunner", Runner)
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    return calls, controls


def respond(monkeypatch, value):
    monkeypatch.setattr("builtins.input", lambda prompt: value)


def test_default_inspection_never_starts_a_session_or_coordinator(context, capsys):
    calls, _ = context
    assert cli.main([]) == 0
    assert calls[-2:] == ["inspect", "devices"]
    assert not any(isinstance(item, tuple) and item[0] == "runner" for item in calls)
    data = json.loads(capsys.readouterr().out)
    assert data["model_requests_sent"] == 0 and data["audio_stream_started"] is False


@pytest.mark.parametrize(
    "flag",
    [
        "--ack-consenting-test-call",
        "--ack-exclusive-audio-risk",
        "--ack-hosted-transcripts",
        "--ack-automatic-transcripts",
    ],
)
def test_every_missing_ack_is_rejected_before_discovery_or_http(context, flag):
    calls, _ = context
    with pytest.raises(SystemExit):
        cli.main([item for item in LIVE if item != flag])
    assert calls == []


def test_piped_live_commands_are_refused_before_any_discovery(context, monkeypatch):
    calls, _ = context
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: False))
    with pytest.raises(SystemExit):
        cli.main(LIVE)
    assert calls == []


@pytest.mark.parametrize(
    "option,value",
    [
        ("--max-turns", "0"),
        ("--max-turns", "6"),
        ("--time-limit", "29"),
        ("--time-limit", "601"),
        ("--label", " "),
        ("--label", "x" * 81),
    ],
)
def test_invalid_bounds_or_labels_never_contact_backend(context, option, value):
    calls, _ = context
    with pytest.raises(SystemExit):
        cli.main([*LIVE, option, value])
    assert calls == []


@pytest.mark.parametrize("answer", ["", "CALL READY", "auto call ready", "cancel"])
def test_wrong_confirmation_never_starts_conversation(context, monkeypatch, answer):
    calls, _ = context
    respond(monkeypatch, answer)
    assert cli.main(LIVE) == 0
    assert not any(isinstance(item, tuple) and item[0] in ("runner", "run") for item in calls)


def test_run_is_explicit_and_normal_pause_does_not_end_pending_approval(context, monkeypatch):
    calls, _ = context
    respond(monkeypatch, "AUTO CALL READY")
    assert cli.main([*LIVE, "--max-turns", "3", "--time-limit", "90", "--label", "Courier A"]) == 0
    assert ("run", "Courier A") in calls and "stop" not in calls
    options = next(
        item[1] for item in calls if isinstance(item, tuple) and item[0] == "coordinator"
    )
    assert options["automatic_submission_consent"] is True and options["max_seconds"] == 90


def test_unreviewed_transcript_warning_is_visible_before_confirmation(context, monkeypatch, capsys):
    respond(monkeypatch, "cancel")
    assert cli.main(LIVE) == 0
    output = capsys.readouterr().out
    assert "AUTOMATIC upload of unreviewed ASR text" in output
    assert "not ASR confidence" in output and "FICTIONAL calls only" in output


def test_readiness_failure_cannot_start_session(context, monkeypatch):
    calls, controls = context
    controls["ready"]["delivery_mode_active"] = False
    respond(monkeypatch, "AUTO CALL READY")
    assert cli.main(LIVE) == 1
    assert not any(isinstance(item, tuple) and item[0] in ("runner", "run") for item in calls)


@pytest.mark.parametrize("uncertain", [True, False])
def test_model_or_native_error_never_retries_or_ends_saved_session(context, monkeypatch, uncertain):
    calls, controls = context
    controls["failure"] = CallError("safe controlled error", uncertain=uncertain)
    respond(monkeypatch, "AUTO CALL READY")
    assert cli.main(LIVE) == 1
    assert calls.count(("run", "Automatic fictional cellular test")) == 1
    assert "stop" not in calls


def test_interrupt_explicitly_attempts_only_known_session_stop(context, monkeypatch):
    calls, controls = context
    controls["failure"] = KeyboardInterrupt()
    respond(monkeypatch, "AUTO CALL READY")
    assert cli.main(LIVE) == 130 and calls.count("stop") == 1


def test_unconfirmed_stop_reports_known_id_without_retry(context, monkeypatch, capsys):
    calls, controls = context
    controls["failure"] = KeyboardInterrupt()
    controls["stop_failure"] = CallError("unknown", uncertain=True)
    respond(monkeypatch, "AUTO CALL READY")
    assert cli.main(LIVE) == 130 and calls.count("stop") == 1
    assert '"session_end_unconfirmed"' in capsys.readouterr().out


def test_event_escapes_untrusted_terminal_controls(capsys):
    cli.event({"text": "\x1b[2J\rsecret"})
    output = capsys.readouterr().out
    assert "\x1b" not in output and "\r" not in output
    assert json.loads(output)["text"] == "\x1b[2J\rsecret"
