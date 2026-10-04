"""No hardware capture/playback: explicit gates, worker and file-boundary tests."""

import base64
import io
import json
import os
import subprocess
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest
from guardmate.cellular.windows_audio import AudioDevice, AudioError
from scripts import probe_cellular_audio as probe

INPUT = AudioDevice(2, "Microphone (vivo T2x 5G Hands-Fr", "input")
OUTPUT = AudioDevice(3, "Speakers (vivo T2x 5G Hands-Free", "output")


def wav(seconds=1, rate=16000):
    result = io.BytesIO()
    with wave.open(result, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(rate)
        target.writeframes(b"\x10\x00\xf0\xff" * (seconds * rate // 2))
    return result.getvalue()


@pytest.fixture
def context(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "ROOT", tmp_path)
    calls = []

    class FakeAudio:
        def devices(self):
            calls.append("devices")
            return [INPUT, OUTPUT, AudioDevice(0, "Laptop microphone", "input")]

        def query_format(self, device, rate):
            calls.append(("query", device, rate))
            return rate == 16000

        def record(self, device, seconds, rate):
            calls.append(("record", device, seconds, rate))
            return wav(seconds, rate)

        def play(self, device, audio):
            calls.append(("play", device, audio))

    monkeypatch.setattr(probe, "WindowsAudio", FakeAudio)

    def no_process(*args, **kwargs):
        raise AssertionError("A process must be explicitly mocked; no hardware or native speech.")

    monkeypatch.setattr(probe.subprocess, "run", no_process)
    return tmp_path, calls


def capture_args(root, *extra):
    return [
        "--record",
        "--ack-consenting-test-call",
        "--device-id",
        str(INPUT.id),
        "--expected-name",
        INPUT.name,
        "--wav",
        str(root / ".data/cellular-audio/capture.wav"),
        *extra,
    ]


def worker_summary(audio=None):
    return {
        "mode": "manual-cellular-audio-probe",
        "transport": "winmm",
        "audio_operation_completed": True,
        "direction": "input",
        "device_id": INPUT.id,
        "device_name": INPUT.name,
        "call_audio_verified": False,
        "model_api_requests": 0,
        "audio_base64": base64.b64encode(wav() if audio is None else audio).decode(),
    }


def test_default_never_opens_audio_or_creates_artifacts(context, capsys):
    root, calls = context
    assert probe.main([]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["audio_opened"] is False
    assert result["model_api_requests"] == 0
    assert calls == ["devices"]
    assert not (root / ".data").exists()


def test_query_uses_only_phone_formats_no_audio(context, capsys):
    assert probe.main(["--check-formats"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["devices"][0]["supported_sample_rates"] == [16000]
    assert "supported_sample_rates" not in result["devices"][2]
    assert not any(isinstance(call, tuple) and call[0] in ("record", "play") for call in context[1])


@pytest.mark.parametrize("args", [["--record"], ["--play", "missing.wav"]])
def test_consent_required_before_any_device_access(context, args):
    with pytest.raises(SystemExit) as error:
        probe.main(args)
    assert error.value.code == 2
    assert context[1] == []


@pytest.mark.parametrize("seconds", ["0", "11", "-1"])
def test_duration_bound(context, seconds):
    with pytest.raises(SystemExit):
        probe.main(capture_args(context[0], "--seconds", seconds))
    assert context[1] == []


@pytest.mark.parametrize(
    "device_id,name",
    [(None, None), (-1, INPUT.name), (0, "Laptop microphone"), (2, "vivo T2x 5G renamed")],
)
def test_missing_default_local_or_changed_endpoint_refused(context, device_id, name):
    with pytest.raises(ValueError):
        probe.selected_device(probe.WindowsAudio(), device_id, name, "input")


def test_direction_must_match(context):
    with pytest.raises(ValueError):
        probe.selected_device(probe.WindowsAudio(), OUTPUT.id, OUTPUT.name, "input")


def test_existing_or_outside_artifacts_are_refused(context):
    root, calls = context
    with pytest.raises(ValueError):
        probe.artifact(str(root / "docs/recording.wav"), ".wav")
    existing = root / ".data/cellular-audio/capture.wav"
    probe.write_new(existing, wav())
    assert probe.main(capture_args(root)) == 1
    assert existing.read_bytes() == wav()
    assert calls == []


def test_atomic_publication_refuses_overwrite_and_cleans_temp(context):
    target = context[0] / ".data/cellular-audio/capture.wav"
    probe.write_new(target, wav())
    with pytest.raises(ValueError):
        probe.write_new(target, b"replacement")
    assert target.read_bytes() == wav()
    assert not list(target.parent.glob(".probe-*"))


@pytest.mark.parametrize("outside", [False, True])
def test_internal_or_external_redirected_base_is_refused(context, monkeypatch, outside):
    root, _ = context
    original = Path.resolve
    nominal = root / ".data/cellular-audio"
    redirect = root.parent / "external" if outside else root / "docs"

    def redirected(path, *args, **kwargs):
        actual = original(path, *args, **kwargs)
        return redirect if actual == nominal else actual

    monkeypatch.setattr(Path, "resolve", redirected)
    with pytest.raises(ValueError):
        probe.artifact(str(nominal / "capture.wav"), ".wav")


def test_worker_marker_cannot_be_used_accidentally(context, monkeypatch):
    monkeypatch.delenv("GUARDMATE_CELLULAR_PROBE_WORKER", raising=False)
    with pytest.raises(SystemExit):
        probe.main(capture_args(context[0], "--_worker"))
    assert context[1] == []


def test_native_child_retains_audio_only_in_memory(context, monkeypatch, capsys):
    root, calls = context
    monkeypatch.setenv("GUARDMATE_CELLULAR_PROBE_WORKER", "1")
    assert probe.main(capture_args(root, "--_worker")) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["call_audio_verified"] is False
    assert base64.b64decode(result["audio_base64"]) == wav(5)
    assert any(isinstance(call, tuple) and call[0] == "record" for call in calls)
    assert not (root / ".data").exists()


def test_parent_uses_bounded_secret_free_worker_then_publishes(context, monkeypatch, capsys):
    root, _ = context
    monkeypatch.setenv("TINKER_API_KEY", "secret-test-value-never-sent")
    monkeypatch.setenv("OTHER_API_KEY", "other-secret")
    launches = []

    def completed(command, **kwargs):
        launches.append((command, kwargs))
        assert not (root / ".data").exists()
        return SimpleNamespace(returncode=0, stdout=json.dumps(worker_summary()).encode())

    monkeypatch.setattr(probe.subprocess, "run", completed)
    assert probe.main(capture_args(root)) == 0
    command, settings = launches[0]
    assert command[-1] == "--_worker"
    assert settings["timeout"] == 20
    assert settings["stdin"] == subprocess.DEVNULL
    assert settings["env"]["GUARDMATE_CELLULAR_PROBE_WORKER"] == "1"
    assert "TINKER_API_KEY" not in settings["env"]
    assert "OTHER_API_KEY" not in settings["env"]
    assert (root / ".data/cellular-audio/capture.wav").read_bytes() == wav()
    public = json.loads(capsys.readouterr().out)
    assert "audio_base64" not in public
    assert public["call_audio_verified"] is False


@pytest.mark.parametrize(
    "failure", ["timeout", "failed", "invalid", "identity", "malformed", "overlong"]
)
def test_failed_or_uncertain_worker_does_not_retain_partial_audio(context, monkeypatch, failure):
    root, _ = context
    launches = []

    def failed(command, **kwargs):
        launches.append(command)
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 20)
        if failure == "failed":
            return SimpleNamespace(returncode=1, stdout=b"")
        if failure == "invalid":
            return SimpleNamespace(returncode=0, stdout=b"not-json")
        value = worker_summary()
        if failure == "identity":
            value["device_name"] = "Laptop microphone"
        if failure == "malformed":
            value["audio_base64"] = base64.b64encode(wav()[:-1]).decode()
        if failure == "overlong":
            value["audio_base64"] = base64.b64encode(wav(6)).decode()
        return SimpleNamespace(returncode=0, stdout=json.dumps(value).encode())

    monkeypatch.setattr(probe.subprocess, "run", failed)
    assert probe.main(capture_args(root)) == 1
    assert len(launches) == 1
    assert not (root / ".data").exists()


def test_playback_worker_never_selects_a_default_device(context, monkeypatch, capsys):
    root, calls = context
    source = root / ".data/cellular-audio/clip.wav"
    probe.write_new(source, wav())
    monkeypatch.setenv("GUARDMATE_CELLULAR_PROBE_WORKER", "1")
    assert (
        probe.main(
            [
                "--play",
                str(source),
                "--ack-consenting-test-call",
                "--device-id",
                str(OUTPUT.id),
                "--expected-name",
                OUTPUT.name,
                "--_worker",
            ]
        )
        == 0
    )
    assert ("play", OUTPUT, wav()) in calls
    assert json.loads(capsys.readouterr().out)["call_audio_verified"] is False


def test_explicit_synthetic_clip_does_not_enumerate_or_open_audio(context, monkeypatch, capsys):
    root, calls = context
    target = root / ".data/cellular-audio/synthetic.wav"
    monkeypatch.setattr(probe, "prepare_test_clip", lambda: wav())
    assert probe.main(["--prepare-test-clip", "--wav", str(target)]) == 0
    assert calls == []
    assert target.read_bytes() == wav()
    assert json.loads(capsys.readouterr().out)["audio_opened"] is False


def test_no_credentials_or_backend_application_imports():
    source = Path(probe.__file__).read_text(encoding="utf-8")
    assert "load_dotenv" not in source
    assert "guardmate.main" not in source
    assert "guardmate.agent" not in source
    assert "TinkerProvider" not in source


def test_atomic_write_failure_cleans_unpublished_temp(context, monkeypatch):
    target = context[0] / ".data/cellular-audio/capture.wav"

    def denied(*args):
        raise OSError("offline simulated hardlink failure")

    monkeypatch.setattr(os, "link", denied)
    with pytest.raises(OSError):
        probe.write_new(target, wav())
    assert not target.exists()
    assert not list(target.parent.glob(".probe-*"))


def test_malformed_audio_summary_is_refused():
    with pytest.raises(AudioError):
        probe.recording_summary(wav()[:-1])


def test_kernel_audio_requires_risk_ack_before_device_access(context):
    with pytest.raises(SystemExit) as error:
        probe.main(capture_args(context[0], "--transport", "wdm-ks"))
    assert error.value.code == 2
    assert context[1] == []


def test_kernel_transport_requires_explicit_selection(context, monkeypatch, capsys):
    transports = []
    fake = probe.WindowsAudio()
    monkeypatch.setattr(probe, "audio_transport", lambda name: transports.append(name) or fake)
    assert probe.main(["--transport", "wdm-ks"]) == 0
    assert transports == ["wdm-ks"]
    result = json.loads(capsys.readouterr().out)
    assert result["transport"] == "wdm-ks"
    assert result["audio_opened"] is False


def test_wrong_worker_transport_is_refused(context, monkeypatch):
    value = worker_summary()
    value["transport"] = "wdm-ks"
    monkeypatch.setattr(
        probe.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(value).encode()),
    )
    assert probe.main(capture_args(context[0])) == 1
    assert not (context[0] / ".data").exists()


def test_known_worker_error_is_reported_without_native_error(context, monkeypatch, capsys):
    report = {
        "mode": "manual-cellular-audio-probe-error",
        "transport": "winmm",
        "reason_code": "endpoint_unavailable",
        "native_detail": "do-not-print-driver-data",
    }
    monkeypatch.setattr(
        probe.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout=json.dumps(report).encode()),
    )
    assert probe.main(capture_args(context[0])) == 1
    public = capsys.readouterr().out
    assert "unavailable or occupied" in public
    assert "do-not-print-driver-data" not in public
    assert not (context[0] / ".data").exists()


def test_arbitrary_worker_failure_is_not_forwarded(context, monkeypatch, capsys):
    monkeypatch.setattr(
        probe.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout=b"private-data-do-not-print"),
    )
    assert probe.main(capture_args(context[0])) == 1
    assert "private-data-do-not-print" not in capsys.readouterr().out


def test_kernel_parent_pins_transport_and_keeps_both_acknowledgments(context, monkeypatch, capsys):
    fake = probe.WindowsAudio()
    monkeypatch.setattr(probe, "audio_transport", lambda name: fake)
    launches = []
    value = worker_summary()
    value["transport"] = "wdm-ks"

    def completed(command, **kwargs):
        launches.append(command)
        return SimpleNamespace(returncode=0, stdout=json.dumps(value).encode())

    monkeypatch.setattr(probe.subprocess, "run", completed)
    assert (
        probe.main(
            capture_args(
                context[0],
                "--transport",
                "wdm-ks",
                "--ack-exclusive-audio-risk",
            )
        )
        == 0
    )
    assert "--ack-exclusive-audio-risk" in launches[0]
    assert "--ack-consenting-test-call" in launches[0]
    assert launches[0][launches[0].index("--transport") + 1] == "wdm-ks"
    result = json.loads(capsys.readouterr().out)
    assert result["transport"] == "wdm-ks"
    assert result["call_audio_verified"] is False
