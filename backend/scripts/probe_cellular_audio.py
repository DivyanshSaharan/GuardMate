"""Explicit Windows cellular-audio experiment, NOT an automatic phone/AI adapter.

Default: list endpoints only. Recording/playback require an agreed test call, a
specific vivo endpoint ID/name and a killable child. No model API or .env access.
"""

import argparse
import base64
import io
import json
import math
import os
import subprocess
import sys
import tempfile
import wave
from array import array
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from guardmate.cellular.windows_audio import (  # noqa: E402
    AudioDevice,
    AudioError,
    WindowsAudio,
    validate_audio,
)

PHONE_LABEL = "vivo T2x 5G"
MAX_SECONDS = 10
TEST_PHRASE = "GuardMate audio check. Purple parcel forty seven."
SAFE_FAILURES = {
    "endpoint_unavailable": "The WDM-KS endpoint is unavailable or occupied; no retry.",
    "native_access": "Windows WDM-KS audio access failed.",
    "deadline": "The WDM-KS device did not finish within its time limit.",
    "dependency_missing": "The optional sounddevice audio dependency is unavailable.",
    "incomplete_audio": "The WDM-KS callback reported incomplete audio.",
    "format_changed": "The WDM-KS stream did not retain the requested device and format.",
}


def audio_transport(transport: str):
    if transport == "winmm":
        return WindowsAudio()
    if transport == "wdm-ks":
        from guardmate.cellular.kernel_audio import WindowsKernelAudio

        return WindowsKernelAudio()
    raise ValueError("Select an explicit supported audio transport.")


def artifact(value: str, suffix: str, *, existing: bool = False) -> Path:
    nominal_base = ROOT.resolve() / ".data/cellular-audio"
    base = nominal_base.resolve()
    path = Path(value).resolve()
    nominal_path = Path(os.path.abspath(value))
    if base != nominal_base or path != nominal_path or not path.is_relative_to(base):
        raise ValueError(
            "Use an ignored .data/cellular-audio/ artifact path inside this repository."
        )
    if path.suffix.casefold() != suffix:
        raise ValueError(f"Use a {suffix} artifact.")
    if existing:
        if not path.is_file():
            raise ValueError("The audio artifact does not exist.")
    elif path.exists():
        raise ValueError("Refusing to overwrite an existing artifact.")
    return path


def selected_device(
    audio: WindowsAudio, device_id: int | None, name: str | None, direction: str
) -> AudioDevice:
    if device_id is None or not 0 <= device_id < 65535 or not name or len(name) > 256:
        raise ValueError("Select an explicit device ID and its exact printed name.")
    if PHONE_LABEL.casefold() not in name.casefold():
        raise ValueError("This experiment is limited to the explicitly paired vivo T2x 5G.")
    matches = [
        device
        for device in audio.devices()
        if device.direction == direction and device.id == device_id and device.name == name
    ]
    if len(matches) != 1:
        raise ValueError("The selected endpoint changed or disappeared. Inspect devices again.")
    return matches[0]


def worker_environment() -> dict[str, str]:
    allowed = {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH", "USERPROFILE", "LOCALAPPDATA"}
    return {key: value for key, value in os.environ.items() if key.upper() in allowed}


def write_new(path: Path, data: bytes) -> None:
    # Complete data first, then atomically link it into place without replacing anything.
    path = artifact(str(path), path.suffix)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temp = tempfile.mkstemp(prefix=".probe-", dir=path.parent)
    temporary = Path(raw_temp)
    try:
        with os.fdopen(descriptor, "wb") as target:
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
        artifact(str(path), path.suffix)
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def recording_summary(audio: bytes) -> dict[str, float | int]:
    validate_audio(audio)
    with wave.open(io.BytesIO(audio), "rb") as recording:
        frames = recording.getnframes()
        sample_rate = recording.getframerate()
        samples = array("h", recording.readframes(frames))
    if sys.byteorder != "little":
        samples.byteswap()
    mean = sum(samples) / len(samples) if samples else 0
    rms = (
        math.sqrt(sum((sample - mean) ** 2 for sample in samples) / len(samples)) if samples else 0
    )
    return {
        "duration_seconds": round(frames / sample_rate, 3),
        "sample_rate": sample_rate,
        "non_dc_rms": round(rms, 2),
    }


def prepare_test_clip() -> bytes:
    # Explicit synthetic local speech only; no caller capture and no model API.
    from guardmate.speech import SpeechError, SpeechService

    try:
        spoken = SpeechService(ROOT).synthesize(TEST_PHRASE)
    except SpeechError:
        raise AudioError(
            "The installed local speech runtime could not prepare a test clip."
        ) from None
    with wave.open(io.BytesIO(spoken), "rb") as source:
        rate = source.getframerate()
        samples = array("h", source.readframes(source.getnframes()))
    if sys.byteorder != "little":
        samples.byteswap()
    output = array("h")
    for index in range(len(samples) * 16000 // rate):
        position = index * rate / 16000
        left = int(position)
        fraction = position - left
        output.append(
            round(
                samples[left] * (1 - fraction) + samples[min(left + 1, len(samples) - 1)] * fraction
            )
        )
    if sys.byteorder != "little":
        output.byteswap()
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(16000)
        target.writeframes(output.tobytes())
    prepared = buffer.getvalue()
    validate_audio(prepared)
    return prepared


def run_worker(args: argparse.Namespace, audio: WindowsAudio) -> dict:
    direction = "input" if args.record else "output"
    device = selected_device(audio, args.device_id, args.expected_name, direction)
    if args.record:
        output = artifact(args.wav, ".wav")
        recorded = audio.record(device, args.seconds, args.sample_rate)
        # The parent retains audio only after the bounded native worker has completed.
        detail = {"wav": str(output.relative_to(ROOT)), **recording_summary(recorded)}
        detail["audio_base64"] = base64.b64encode(recorded).decode("ascii")
    else:
        source = artifact(args.play, ".wav", existing=True)
        if source.stat().st_size > MAX_SECONDS * 16000 * 2 + 4096:
            raise ValueError("The playback WAV exceeds the 10-second audio bound.")
        with source.open("rb") as file:
            spoken = file.read(MAX_SECONDS * 16000 * 2 + 4097)
        audio.play(device, spoken)
        detail = {"wav": str(source.relative_to(ROOT))}
    return {
        "mode": "manual-cellular-audio-probe",
        "transport": args.transport,
        "audio_operation_completed": True,
        "direction": direction,
        "device_id": device.id,
        "device_name": device.name,
        **detail,
        "call_audio_verified": False,
        "model_api_requests": 0,
        "note": (
            "Native completion is NOT proof of caller audio. "
            "Independent caller confirmation required."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--record", action="store_true", help="Record an agreed test call to a new WAV."
    )
    action.add_argument(
        "--play", help="Send a short, existing local test WAV to the selected endpoint."
    )
    action.add_argument(
        "--prepare-test-clip",
        action="store_true",
        help="Generate a synthetic local Piper WAV, no playback.",
    )
    parser.add_argument("--device-id", type=int)
    parser.add_argument(
        "--transport",
        choices=("winmm", "wdm-ks"),
        default="winmm",
        help="Explicit audio interface; WDM-KS can interrupt Phone Link's audio.",
    )
    parser.add_argument(
        "--expected-name", help="Exact endpoint name from a fresh default inspection."
    )
    parser.add_argument("--seconds", type=int, default=5)
    parser.add_argument("--sample-rate", type=int, choices=(8000, 16000), default=16000)
    parser.add_argument("--wav", help="New ignored .data/cellular-audio/*.wav recording artifact.")
    parser.add_argument("--report", help="Optional new ignored .data/cellular-audio/*.json report.")
    parser.add_argument(
        "--check-formats", action="store_true", help="Query 8/16 kHz formats; no audio open."
    )
    parser.add_argument("--ack-consenting-test-call", action="store_true")
    parser.add_argument(
        "--ack-exclusive-audio-risk",
        action="store_true",
        help="Acknowledge WDM-KS can temporarily take the phone audio away from Phone Link.",
    )
    parser.add_argument("--_worker", action="store_true", help=argparse.SUPPRESS)
    raw_args = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(raw_args)
    live_audio = args.record or args.play is not None
    if not 1 <= args.seconds <= MAX_SECONDS:
        parser.error("Choose a recording duration from 1 to 10 seconds.")
    if live_audio and not args.ack_consenting_test_call:
        parser.error(
            "Recording/playback requires --ack-consenting-test-call and an active agreed test call."
        )
    if live_audio and args.transport == "wdm-ks" and not args.ack_exclusive_audio_risk:
        parser.error("WDM-KS audio requires --ack-exclusive-audio-risk; Phone Link may lose audio.")
    if args.record and not args.wav:
        parser.error("Recording requires a new --wav artifact.")
    if args.prepare_test_clip and (
        not args.wav
        or args._worker
        or args.report
        or args.check_formats
        or args.device_id is not None
        or args.expected_name is not None
    ):
        parser.error("Preparing a synthetic clip requires only a new --wav artifact.")
    if not live_audio and not args.prepare_test_clip and (args.wav or args.report or args._worker):
        parser.error("Artifact/worker options require an explicit recording or playback action.")
    if args.play and args.wav:
        parser.error("--wav is only for recording; --play selects its input WAV.")
    if live_audio and args.check_formats:
        parser.error("Inspect formats separately before an audio action.")
    if args._worker and os.environ.get("GUARDMATE_CELLULAR_PROBE_WORKER") != "1":
        parser.error("The worker flag is reserved for the bounded probe subprocess.")
    try:
        report = artifact(args.report, ".json") if args.report else None
        if args.prepare_test_clip:
            output = artifact(args.wav, ".wav")
            write_new(output, prepare_test_clip())
            print(
                json.dumps(
                    {
                        "mode": "synthetic-local-test-clip",
                        "wav": str(output.relative_to(ROOT)),
                        "intended_phrase": TEST_PHRASE,
                        "audio_opened": False,
                        "model_api_requests": 0,
                    },
                    indent=2,
                )
            )
            return 0
        if args.record:
            artifact(args.wav, ".wav")
        if args.play:
            artifact(args.play, ".wav", existing=True)
        audio = audio_transport(args.transport)
        if not live_audio:
            devices = []
            for device in audio.devices():
                row = {"id": device.id, "name": device.name, "direction": device.direction}
                if args.check_formats and PHONE_LABEL.casefold() in device.name.casefold():
                    row["supported_sample_rates"] = [
                        rate for rate in (8000, 16000) if audio.query_format(device, rate)
                    ]
                devices.append(row)
            print(
                json.dumps(
                    {
                        "devices": devices,
                        "transport": args.transport,
                        "audio_opened": False,
                        "model_api_requests": 0,
                    },
                    indent=2,
                )
            )
            return 0
        if args._worker:
            print(json.dumps(run_worker(args, audio)))
            return 0
        device = selected_device(
            audio, args.device_id, args.expected_name, "input" if args.record else "output"
        )
        environment = worker_environment()
        environment["GUARDMATE_CELLULAR_PROBE_WORKER"] = "1"
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), *raw_args, "--_worker"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=MAX_SECONDS + 10,
            check=False,
            cwd=ROOT,
            env=environment,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if result.returncode != 0:
            # Only known, fixed diagnostic codes cross the worker boundary. Never
            # forward native errors, stderr, user text or arbitrary child output.
            failure = None
            if len(result.stdout) < 4096:
                try:
                    failure = json.loads(result.stdout)
                except (ValueError, UnicodeError):
                    pass
            if (
                isinstance(failure, dict)
                and failure.get("mode") == "manual-cellular-audio-probe-error"
                and failure.get("transport") == args.transport
                and isinstance(failure.get("reason_code"), str)
                and failure["reason_code"] in SAFE_FAILURES
            ):
                raise AudioError(SAFE_FAILURES[failure["reason_code"]])
            raise AudioError(
                "Audio probe failed; no automatic retry. Do not assume the caller heard audio."
            )
        if len(result.stdout) > 500_000:
            raise AudioError("Audio probe returned an invalid report.")
        summary = json.loads(result.stdout)
        if (
            not isinstance(summary, dict)
            or summary.get("mode") != "manual-cellular-audio-probe"
            or summary.get("device_id") != device.id
            or summary.get("device_name") != device.name
            or summary.get("transport") != args.transport
            or summary.get("direction") != device.direction
            or summary.get("call_audio_verified") is not False
            or summary.get("model_api_requests") != 0
            or summary.get("audio_operation_completed") is not True
        ):
            raise AudioError("Audio probe returned an invalid report.")
        if args.record:
            encoded = summary.pop("audio_base64", "")
            if not isinstance(encoded, str):
                raise AudioError("Audio probe returned an invalid recording.")
            recorded = base64.b64decode(encoded, validate=True)
            measured = recording_summary(recorded)
            if (
                measured["sample_rate"] != args.sample_rate
                or measured["duration_seconds"] > args.seconds
            ):
                raise AudioError("Audio probe returned audio outside the selected bounds.")
            write_new(artifact(args.wav, ".wav"), recorded)
            summary.update(measured)
        if report:
            write_new(report, json.dumps(summary, indent=2).encode("utf-8"))
        print(json.dumps(summary, indent=2))
        return 0
    except subprocess.TimeoutExpired:
        print(
            "Audio probe timed out and its worker was stopped. "
            "Outcome uncertain; no automatic retry."
        )
        return 1
    except AudioError as error:
        if args._worker:
            reason = next(
                (code for code, message in SAFE_FAILURES.items() if message == str(error)),
                "unspecified",
            )
            print(
                json.dumps(
                    {
                        "mode": "manual-cellular-audio-probe-error",
                        "transport": args.transport,
                        "reason_code": reason,
                    }
                )
            )
        else:
            # AudioError messages are controlled diagnostics, not driver strings.
            print(f"Audio probe failed: {error} No default-device fallback or retry.")
        return 1
    except (ValueError, OSError, wave.Error):
        print(
            "Audio probe failed. Check the named endpoint/artifacts. "
            "No default-device fallback or retry."
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
