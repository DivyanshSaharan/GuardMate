"""Packaging protocol tests: fake worker entries only; no devices or hosted APIs."""

import sys
import types
from pathlib import Path

import pytest
from guardmate import frozen_runtime as runtime


@pytest.fixture(autouse=True)
def private_environment(monkeypatch):
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    for name in (
        "MONGODB_URI",
        "TINKER_API_KEY",
        "GUARDMATE_ACCESS_USERNAME",
        "GUARDMATE_ACCESS_PASSWORD",
        "RENDER",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("name", ["speech", "call-audio", "utterance-audio", "desktop-audio"])
def test_frozen_workers_reuse_executable_without_python_switches(monkeypatch, name):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\Program Files\GuardMate\GuardMate.exe")
    assert runtime.worker_command(name, "argument with spaces") == [
        sys.executable,
        "--worker",
        name,
        "argument with spaces",
    ]


def test_source_workers_keep_isolated_cellular_command(tmp_path, monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    command = runtime.worker_command("call-audio", root=tmp_path)
    assert command == [
        str(tmp_path / ".venv/Scripts/python.exe"),
        "-I",
        "-B",
        str(tmp_path / "backend/guardmate/cellular/call_audio_worker.py"),
    ]
    speech = runtime.worker_command("speech", "model", "output")
    assert speech[0] == sys.executable
    assert Path(speech[1]).name == "worker.py" and speech[2:] == ["model", "output"]


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["--worker"],
        ["--worker", "arbitrary.module"],
        ["--worker", "speech"],
        ["--worker", "call-audio", "extra"],
    ],
)
def test_nonworker_or_invalid_arguments_never_import_modules(monkeypatch, arguments):
    monkeypatch.setattr(
        runtime.importlib, "import_module", lambda _: pytest.fail("Unexpected import")
    )
    assert runtime.dispatch_worker(arguments) == (None if not arguments else 2)


def test_dispatch_calls_only_selected_main_and_restores_arguments(monkeypatch):
    previous = sys.argv
    seen = []

    def main():
        seen.append(list(sys.argv))
        return 7

    def load(module):
        assert module == "guardmate.speech.worker"
        return types.SimpleNamespace(main=main)

    monkeypatch.setattr(runtime.importlib, "import_module", load)
    assert runtime.dispatch_worker(["--worker", "speech", "model", "output"]) == 7
    assert seen == [["guardmate-speech", "model", "output"]]
    assert sys.argv is previous
