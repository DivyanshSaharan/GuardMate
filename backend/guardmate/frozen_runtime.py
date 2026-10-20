"""Fixed worker dispatch for a source checkout and the Windows desktop executable."""

import importlib
import sys
from pathlib import Path

_WORKERS = {
    "speech": "guardmate.speech.worker",
    "call-audio": "guardmate.cellular.call_audio_worker",
    "utterance-audio": "guardmate.cellular.utterance_worker",
    "desktop-audio": "guardmate.desktop.audio_worker",
}


def worker_command(name: str, *arguments: str, root: Path | None = None) -> list[str]:
    """Do not interpret arbitrary modules, script names, or Python switches."""
    if name not in _WORKERS:
        raise ValueError("Unknown GuardMate worker.")
    values = [str(value) for value in arguments]
    if getattr(sys, "frozen", False):
        return [sys.executable, "--worker", name, *values]
    source_root = root or Path(__file__).resolve().parents[2]
    relative = _WORKERS[name].replace(".", "/") + ".py"
    script = source_root / "backend" / relative
    if name == "speech":
        return [sys.executable, str(script), *values]
    executable = (
        source_root / ".venv" / "Scripts" / "python.exe"
        if name in ("call-audio", "utterance-audio")
        else Path(sys.executable)
    )
    return [str(executable), "-I", "-B", str(script), *values]


def dispatch_worker(argv: list[str] | None = None) -> int | None:
    """Run before importing the GUI, application state, dotenv, or hosted SDKs."""
    values = list(sys.argv[1:] if argv is None else argv)
    if not values or values[0] != "--worker":
        return None
    if len(values) < 2 or values[1] not in _WORKERS:
        return 2
    name, arguments = values[1], values[2:]
    if len(arguments) != (2 if name == "speech" else 0):
        return 2
    previous = sys.argv
    try:
        sys.argv = [f"guardmate-{name}", *arguments]
        module = importlib.import_module(_WORKERS[name])
        return module.main()
    finally:
        sys.argv = previous
