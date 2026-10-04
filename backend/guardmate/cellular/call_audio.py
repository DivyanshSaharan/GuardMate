"""Disposable native workers and in-memory WAV conversion for a manual call."""

from __future__ import annotations

import base64
import io
import json
import os
import struct
import subprocess
import sys
import threading
import wave
from array import array
from dataclasses import asdict
from pathlib import Path

from .call_errors import CallError
from .windows_audio import AudioDevice, AudioError, validate_audio

MODE = "guardmate-manual-call-audio"
MARKER = "GUARDMATE_MANUAL_AUDIO_WORKER"
TRANSPORT = "wdm-ks"
PHONE_LABEL = "vivo T2x 5G"
MAX_BODY_BYTES = 1_400_000
MAX_DEVICES = 128
SAFE_FAILURES = {
    "invalid_request": "The manual audio worker refused an invalid request.",
    "endpoint_changed": "The selected phone endpoint changed or disconnected.",
    "endpoint_unavailable": "The WDM-KS endpoint is unavailable or occupied; no retry.",
    "dependency_missing": "The optional sounddevice audio dependency is unavailable.",
    "native_failure": "The manual audio operation failed; do not retry automatically.",
}


def _invalid_json(*_args):
    raise ValueError("Unsupported JSON value")


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def decode_json(body: bytes) -> dict:
    if type(body) is not bytes or not body or len(body) > MAX_BODY_BYTES:
        raise ValueError("Invalid worker body")
    value = json.loads(
        body.decode("utf-8"),
        object_pairs_hook=_object,
        parse_constant=_invalid_json,
        parse_float=_invalid_json,
    )
    if type(value) is not dict:
        raise ValueError("Invalid worker body")
    return value


def device_value(value, *, direction=None) -> AudioDevice:
    if type(value) is not dict or set(value) != {"id", "name", "direction"}:
        raise ValueError("Invalid explicit device")
    identifier, name, flow = value["id"], value["name"], value["direction"]
    if (
        type(identifier) is not int
        or not 0 <= identifier < 65535
        or type(name) is not str
        or not 1 <= len(name) <= 256
        or PHONE_LABEL.casefold() not in name.casefold()
        or flow not in ("input", "output")
        or (direction is not None and flow != direction)
    ):
        raise ValueError("Invalid explicit phone device")
    return AudioDevice(identifier, name, flow)


def encoded_audio(value) -> bytes:
    if type(value) is not str or not 1 <= len(value) <= MAX_BODY_BYTES:
        raise ValueError("Invalid encoded audio")
    return base64.b64decode(value, validate=True)


def worker_environment() -> dict[str, str]:
    allowed = {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH"}
    environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    environment[MARKER] = "1"
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


class _OutputLimit(Exception):
    pass


def _bounded_run(command, *, input, timeout, cwd, env, **_kwargs):
    """A subprocess.run-compatible runner with a hard in-memory stdout cap."""
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        cwd=cwd,
        env=env,
        shell=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
    )
    chunks = []
    state = {"size": 0, "overflow": False, "pipe_failed": False}

    def read_output():
        try:
            while block := process.stdout.read(65536):
                remaining = MAX_BODY_BYTES - state["size"]
                if len(block) > remaining:
                    state["overflow"] = True
                    process.kill()
                    break
                chunks.append(block)
                state["size"] += len(block)
        except (OSError, ValueError):
            state["pipe_failed"] = True

    def write_input():
        try:
            process.stdin.write(input)
            process.stdin.close()
        except (OSError, ValueError):
            state["pipe_failed"] = True

    reader = threading.Thread(target=read_output, daemon=True)
    writer = threading.Thread(target=write_input, daemon=True)
    try:
        reader.start()
        writer.start()
        process.wait(timeout=timeout)
    except BaseException as error:
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        if isinstance(error, subprocess.TimeoutExpired):
            raise subprocess.TimeoutExpired(command, timeout) from None
        raise
    finally:
        if reader.ident is not None:
            reader.join(timeout=1)
        if writer.ident is not None:
            writer.join(timeout=1)
        if not reader.is_alive():
            process.stdout.close()
        if not writer.is_alive():
            process.stdin.close()
    if state["overflow"]:
        raise _OutputLimit
    if state["pipe_failed"] or reader.is_alive() or writer.is_alive():
        raise subprocess.SubprocessError("Worker pipe failed")
    return subprocess.CompletedProcess(command, process.returncode, b"".join(chunks))


def _piper_pcm(audio: bytes) -> tuple[int, bytes]:
    if type(audio) is not bytes or not 44 <= len(audio) <= 2_000_000:
        raise CallError("Use a complete local PCM16 mono reply WAV of at most 30 seconds.")
    if audio[:4] != b"RIFF" or audio[8:12] != b"WAVE":
        raise CallError("The local reply is not a complete PCM WAV.")
    if struct.unpack_from("<I", audio, 4)[0] + 8 != len(audio):
        raise CallError("The local reply WAV length is inconsistent.")
    offset, fmt, pcm = 12, None, None
    while offset < len(audio):
        if offset + 8 > len(audio):
            raise CallError("The local reply WAV has an incomplete chunk.")
        kind = audio[offset : offset + 4]
        length = struct.unpack_from("<I", audio, offset + 4)[0]
        start = offset + 8
        end = start + length
        padded = end + length % 2
        if padded > len(audio) or (length % 2 and audio[end] != 0):
            raise CallError("The local reply WAV has an invalid chunk or padding.")
        if kind == b"fmt ":
            if fmt is not None or length not in (16, 18):
                raise CallError("The local reply must have one standard PCM format.")
            fmt = struct.unpack_from("<HHIIHH", audio, start)
            if length == 18 and struct.unpack_from("<H", audio, start + 16)[0] != 0:
                raise CallError("The local reply has unsupported format extensions.")
        elif kind == b"data":
            if fmt is None or pcm is not None:
                raise CallError("The local reply must have one data chunk after its format.")
            pcm = audio[start:end]
        offset = padded
    if fmt is None or pcm is None:
        raise CallError("The local reply is missing its format or audio data.")
    tag, channels, rate, byte_rate, align, bits = fmt
    if (
        tag != 1
        or channels != 1
        or not 8000 <= rate <= 48000
        or byte_rate != rate * 2
        or align != 2
        or bits != 16
        or not pcm
        or len(pcm) % 2
        or len(pcm) > rate * 2 * 30
    ):
        raise CallError(
            "Use PCM16 mono local speech from 8000 through 48000 Hz, at most 30 seconds."
        )
    return rate, pcm


def reply_audio(piper_wav: bytes) -> bytes:
    """Convert the entire bounded local reply to 16 kHz in memory, without clipping."""
    rate, pcm = _piper_pcm(piper_wav)
    if rate != 16000:
        samples = array("h")
        samples.frombytes(pcm)
        if sys.byteorder != "little":
            samples.byteswap()
        count = (len(samples) * 16000 + rate - 1) // rate
        output = array("h")
        for index in range(count):
            left, fraction = divmod(index * rate, 16000)
            right = min(left + 1, len(samples) - 1)
            weighted = samples[left] * (16000 - fraction) + samples[right] * fraction
            output.append((weighted + 8000) // 16000)
        if sys.byteorder != "little":
            output.byteswap()
        pcm = output.tobytes()
    stream = io.BytesIO()
    with wave.open(stream, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(16000)
        target.writeframes(pcm)
    converted = stream.getvalue()
    validate_audio(converted, max_seconds=30)
    return converted


class LocalCallAudio:
    """No native recording/playback without both explicit constructor acknowledgements."""

    def __init__(self, root: Path, *, consent=False, exclusive_risk=False, run=None):
        self.root = Path(root).resolve()
        self.consent = consent is True
        self.exclusive_risk = exclusive_risk is True
        self._run = run or _bounded_run

    def _invoke(self, action, *, device=None, seconds=None, audio=None):
        live = action != "list"
        if live and not (self.consent and self.exclusive_risk):
            raise CallError(
                "A consenting call and acknowledgement of exclusive audio risk are required."
            )
        request = {
            "version": 1,
            "mode": MODE,
            "transport": TRANSPORT,
            "action": action,
            "consent": self.consent,
            "exclusive_risk": self.exclusive_risk,
        }
        if live:
            try:
                pinned = device_value(
                    asdict(device), direction="input" if action == "record" else "output"
                )
            except (TypeError, ValueError):
                raise CallError("Select an explicit, current vivo T2x 5G endpoint.") from None
            request["device"] = asdict(pinned)
        if seconds is not None:
            request["seconds"] = seconds
        if audio is not None:
            request["audio_base64"] = base64.b64encode(audio).decode("ascii")
            request["audio_bytes"] = len(audio)
        body = json.dumps(request, ensure_ascii=True, allow_nan=False).encode("utf-8")
        if len(body) > MAX_BODY_BYTES:
            raise CallError("The manual audio request exceeds its size bound.")
        executable = self.root / ".venv" / "Scripts" / "python.exe"
        worker = self.root / "backend" / "guardmate" / "cellular" / "call_audio_worker.py"
        timeout = {"list": 10, "record": 20, "play": 40}[action]
        try:
            result = self._run(
                [str(executable), "-I", "-B", str(worker)],
                input=body,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=timeout,
                cwd=str(self.root),
                env=worker_environment(),
            )
        except subprocess.TimeoutExpired:
            raise CallError(
                "The manual audio worker timed out; do not retry automatically.", uncertain=live
            ) from None
        except Exception:
            raise CallError(
                "The manual audio worker failed; do not retry automatically.", uncertain=live
            ) from None
        try:
            response = decode_json(result.stdout)
            if (
                result.returncode != 0
                or response.get("version") != 1
                or type(response.get("version")) is not int
                or response.get("mode") != MODE
                or response.get("transport") != TRANSPORT
                or response.get("action") != action
                or type(response.get("ok")) is not bool
                or response.get("device") != request.get("device")
            ):
                raise ValueError("Invalid worker identity")
            if live and device_value(response["device"]) != pinned:
                raise ValueError("Invalid worker device")
            base_keys = {"version", "mode", "transport", "action", "ok", "device"}
            if not response["ok"]:
                if set(response) != base_keys | {"code", "uncertain"}:
                    raise ValueError("Invalid worker error")
                if response["code"] not in SAFE_FAILURES or type(response["uncertain"]) is not bool:
                    raise ValueError("Invalid worker error")
                raise CallError(
                    SAFE_FAILURES[response["code"]], uncertain=live or response["uncertain"]
                )
            if action == "list":
                if set(response) != base_keys | {"devices"}:
                    raise ValueError("Invalid inventory")
                values = response["devices"]
                if type(values) is not list or len(values) > MAX_DEVICES:
                    raise ValueError("Invalid inventory")
                devices = [device_value(value) for value in values]
                if len({(value.id, value.direction) for value in devices}) != len(devices):
                    raise ValueError("Duplicate endpoint")
                return devices
            detail_keys = {"sample_rate", "audio_bytes", "pcm_bytes"}
            if action == "record":
                if set(response) != base_keys | detail_keys | {"seconds", "audio_base64"}:
                    raise ValueError("Invalid capture")
                captured = encoded_audio(response["audio_base64"])
                rate, pcm = validate_audio(captured)
                if (
                    rate != 16000
                    or len(pcm) != seconds * 32000
                    or type(response["seconds"]) is not int
                    or response["seconds"] != seconds
                ):
                    raise ValueError("Incomplete capture")
            else:
                if (
                    set(response) != base_keys | detail_keys | {"completed"}
                    or response["completed"] is not True
                ):
                    raise ValueError("Invalid playback completion")
                captured = audio
                rate, pcm = validate_audio(audio, max_seconds=30)
            for key, expected in (
                ("sample_rate", rate),
                ("audio_bytes", len(captured)),
                ("pcm_bytes", len(pcm)),
            ):
                if type(response[key]) is not int or response[key] != expected:
                    raise ValueError("Inconsistent worker audio")
            return captured if action == "record" else None
        except CallError:
            raise
        except (ValueError, TypeError, KeyError, AttributeError, AudioError):
            raise CallError(
                "The manual audio worker returned an invalid result; do not retry automatically.",
                uncertain=live,
            ) from None

    def devices(self) -> list[AudioDevice]:
        return self._invoke("list")

    def record(self, device: AudioDevice, seconds: int = 5) -> bytes:
        if type(seconds) is not int or not 1 <= seconds <= 10:
            raise CallError("Record a whole number of seconds from 1 through 10.")
        return self._invoke("record", device=device, seconds=seconds)

    def play(self, device: AudioDevice, wav: bytes) -> None:
        try:
            rate, _ = validate_audio(wav, max_seconds=30)
            if rate != 16000:
                raise AudioError("Incorrect reply sample rate")
        except AudioError:
            raise CallError(
                "Manual call playback requires a complete 16000 Hz PCM16 mono reply, "
                "at most 30 seconds."
            ) from None
        self._invoke("play", device=device, audio=wav)
