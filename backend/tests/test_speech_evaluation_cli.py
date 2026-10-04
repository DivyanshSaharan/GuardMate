import hashlib
import io
import json
import math
import struct
import wave
from pathlib import Path

import pytest
from guardmate.speech.service import SpeechError, validate_recording
from scripts import evaluate_speech as cli

RUN = ["--run", "--ack-local-fixture-transcription"]


def wav():
    output = io.BytesIO()
    with wave.open(output, "wb") as recording:
        recording.setnchannels(1)
        recording.setsampwidth(2)
        recording.setframerate(16000)
        recording.writeframes(
            b"".join(struct.pack("<h", round(4000 * math.sin(i / 10))) for i in range(3200))
        )
    return output.getvalue()


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    folder = tmp_path / "datasets" / "speech"
    folder.mkdir(parents=True)
    manifest = folder / "manifest.json"
    cases = [
        {
            "case_id": name,
            "reference": text,
            "audio": f"audio/{name}.wav",
            "provenance": "fictional_human",
        }
        for name, text in (("prepaid", "This is prepaid."), ("guard", "Security is here."))
    ]
    manifest.write_text(json.dumps({"schema_version": 1, "corpus_id": "test-v1", "cases": cases}))
    binary = tmp_path / "whisper.exe"
    model = tmp_path / "model.bin"
    binary.write_bytes(b"inert fake runtime")
    model.write_bytes(b"inert fake model")
    calls = []
    control = {"ready": True, "error": None, "change_model": False, "result": None}

    class FakeSpeech:
        def __init__(self, root, *, whisper_cli=None, whisper_model=None):
            calls.append("service")
            self.whisper_cli = whisper_cli or binary
            self.whisper_model = whisper_model or model

        def status(self):
            calls.append("status")
            return {"stt_ready": control["ready"], "tts_ready": False}

        def transcribe(self, audio):
            calls.append("transcribe")
            if control["error"]:
                raise control["error"]
            if control["change_model"]:
                self.whisper_model.write_bytes(b"changed inert model")
            if control["result"] is not None:
                return control["result"]
            index = calls.count("transcribe") - 1
            return {
                "text": cases[index]["reference"],
                "duration_ms": validate_recording(audio),
                "processing_ms": 20,
            }

    monkeypatch.setattr(cli, "SpeechService", FakeSpeech)

    def populate():
        (folder / "audio").mkdir(exist_ok=True)
        for case in cases:
            (folder / case["audio"]).write_bytes(wav())

    return manifest, cases, calls, control, populate, model, binary


def output(capsys):
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


@pytest.mark.parametrize("extra", [[], ["--output", "private.json"]])
def test_missing_consent_precedes_any_access(setup, monkeypatch, extra):
    manifest, _, calls, *_ = setup
    for name in ("load_manifest", "report_path", "file_digest", "prepare_recordings"):
        monkeypatch.setattr(cli, name, lambda *_: pytest.fail("Access before consent"))
    with pytest.raises(SystemExit) as error:
        cli.main(["--manifest", str(manifest), "--run", *extra])
    assert error.value.code == 2
    assert calls == []


def test_default_inspects_without_native_work_or_scores(setup, capsys):
    manifest, _, calls, *_ = setup
    assert cli.main(["--manifest", str(manifest)]) == 0
    report = output(capsys)[0]
    assert report["fixtures"]["status"] == "not_evaluated"
    assert report["fixtures"]["valid_audio_count"] == 0
    assert report["speech_jobs"] == report["hosted_model_requests"] == 0
    assert report["evaluation_performed"] is False
    assert "transcribe" not in calls
    assert not (cli.ROOT / ".data").exists()


def test_missing_fixture_refuses_the_whole_corpus(setup, capsys):
    manifest, cases, calls, _, populate, *_ = setup
    populate()
    (manifest.parent / cases[-1]["audio"]).unlink()
    assert cli.main(["--manifest", str(manifest), *RUN]) == 1
    assert "transcribe" not in calls
    assert output(capsys)[-1]["complete_report"] is False


def test_invalid_last_wav_does_not_transcribe_first(setup, capsys):
    manifest, cases, calls, _, populate, *_ = setup
    populate()
    (manifest.parent / cases[-1]["audio"]).write_bytes(b"not a wav")
    assert cli.main(["--manifest", str(manifest), *RUN]) == 1
    assert "transcribe" not in calls
    assert output(capsys)[-1]["code"] == "audio_invalid"


def test_complete_replay_has_measured_identity_and_word_counts(setup, capsys):
    manifest, _, calls, _, populate, model, binary = setup
    populate()
    destination = cli.ROOT / ".data" / "speech-evaluations" / "new.json"
    assert cli.main(["--manifest", str(manifest), *RUN, "--output", str(destination)]) == 0
    report = output(capsys)[-1]["report"]
    assert report["status"] == "evaluated"
    assert report["totals"]["micro_wer"] == 0
    assert report["totals"]["exact_match_count"] == 2
    assert calls.count("transcribe") == 2
    assert report["model"]["model_sha256"] == hashlib.sha256(model.read_bytes()).hexdigest()
    assert report["model"]["cli_sha256"] == hashlib.sha256(binary.read_bytes()).hexdigest()
    assert report["model"]["decoder_profile"] == "guardmate-en-cpu-t4-p1-no-fallback-v1"
    assert json.loads(destination.read_text()) == report
    assert "confidence" not in report["totals"]


def test_assets_not_ready_do_not_start_or_write(setup, capsys):
    manifest, _, calls, control, populate, *_ = setup
    populate()
    control["ready"] = False
    assert cli.main(["--manifest", str(manifest), *RUN]) == 1
    assert "transcribe" not in calls
    assert output(capsys)[-1]["code"] == "speech_not_ready"


def test_missing_model_is_a_preflight_failure(setup, capsys):
    manifest, _, calls, _, populate, model, _ = setup
    populate()
    model.unlink()
    assert cli.main(["--manifest", str(manifest), *RUN]) == 1
    assert "transcribe" not in calls
    assert output(capsys)[-1]["code"] == "asset_identity"


def test_native_failure_stops_without_retry_or_complete_report(setup, capsys):
    manifest, _, calls, control, populate, *_ = setup
    populate()
    control["error"] = SpeechError(504, "Native timeout")
    destination = cli.ROOT / ".data" / "speech-evaluations" / "failed.json"
    assert cli.main(["--manifest", str(manifest), *RUN, "--output", str(destination)]) == 1
    assert calls.count("transcribe") == 1
    failure = output(capsys)[-1]
    assert failure["automatic_retry"] is False
    assert failure["complete_report"] is False
    assert not destination.exists()


def test_changed_model_invalidates_the_report(setup, capsys):
    manifest, _, _, control, populate, *_ = setup
    populate()
    control["change_model"] = True
    assert cli.main(["--manifest", str(manifest), *RUN]) == 1
    assert output(capsys)[-1]["code"] == "asset_changed"


@pytest.mark.parametrize(
    "result",
    [
        {"text": "", "duration_ms": 200, "processing_ms": 20},
        {"text": "valid", "duration_ms": True, "processing_ms": 20},
        {"text": "valid", "duration_ms": 201, "processing_ms": 20},
        {"text": "valid", "duration_ms": 200, "processing_ms": -1},
        {"text": "valid", "duration_ms": 200, "processing_ms": True},
    ],
)
def test_invalid_transcription_metadata_stops_without_retry(setup, capsys, result):
    manifest, _, calls, control, populate, *_ = setup
    populate()
    control["result"] = result
    assert cli.main(["--manifest", str(manifest), *RUN]) == 1
    assert calls.count("transcribe") == 1
    assert output(capsys)[-1]["code"] == "invalid_transcription"


@pytest.mark.parametrize("kind", ["outside", "wrong_suffix", "exists", "symlink", "junction"])
def test_report_paths_are_rejected_before_service_access(setup, monkeypatch, kind):
    manifest, _, calls, _, _, _, _ = setup
    path = cli.ROOT / ".data" / "speech-evaluations" / "report.json"
    if kind == "outside":
        path = cli.ROOT / "public.json"
    elif kind == "wrong_suffix":
        path = path.with_suffix(".wav")
    elif kind == "exists":
        path.parent.mkdir(parents=True)
        path.write_text("keep me")
    else:
        name = "is_symlink" if kind == "symlink" else "is_junction"
        monkeypatch.setattr(Path, name, lambda item: item == path.parent)
    with pytest.raises(SystemExit) as error:
        cli.main(["--manifest", str(manifest), *RUN, "--output", str(path)])
    assert error.value.code == 2
    assert calls == []
    if kind == "exists":
        assert path.read_text() == "keep me"


def test_output_without_run_is_rejected_before_access(setup):
    manifest, _, calls, *_ = setup
    with pytest.raises(SystemExit):
        cli.main(["--manifest", str(manifest), "--output", str(cli.ROOT / "private.json")])
    assert calls == []


def test_cancelled_native_job_has_no_complete_report(setup, capsys):
    manifest, _, calls, control, populate, *_ = setup
    populate()
    control["error"] = KeyboardInterrupt()
    assert cli.main(["--manifest", str(manifest), *RUN]) == 130
    assert calls.count("transcribe") == 1
    assert output(capsys)[-1]["complete_report"] is False


def test_terminal_output_escapes_controls(capsys):
    cli.emit({"hypothesis": "hello\u001b[2J"})
    assert "\u001b" not in capsys.readouterr().out
