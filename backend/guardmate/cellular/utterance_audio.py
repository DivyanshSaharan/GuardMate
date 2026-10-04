"""One bounded, secret-free native child per automatically ended utterance."""

from __future__ import annotations

import json
import os
import subprocess
import threading
from dataclasses import asdict

from .call_audio import (
    MAX_BODY_BYTES,
    SAFE_FAILURES,
    TRANSPORT,
    LocalCallAudio,
    decode_json,
    device_value,
    encoded_audio,
    worker_environment,
)
from .call_errors import CallError
from .utterance import NoSpeech, UtteranceConfig, UtteranceTooLong, config_value
from .windows_audio import AudioDevice, AudioError, validate_audio

MODE = "guardmate-automatic-utterance-audio"
MARKER = "GUARDMATE_UTTERANCE_AUDIO_WORKER"


def _listener_run(command, *, input, timeout, cwd, env, on_event, **_kwargs):
    """Consume flushed JSONL incrementally; cap bytes and kill on any protocol failure."""
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
    state = {"result": None, "failed": False, "interrupted": None}

    def kill():
        try:
            process.kill()
        except OSError:
            pass

    def read_output():
        pending = bytearray()
        total = 0
        try:
            while block := process.stdout.read1(65536):
                total += len(block)
                if total > MAX_BODY_BYTES:
                    raise ValueError("Worker output exceeded its bound")
                pending.extend(block)
                while (boundary := pending.find(b"\n")) != -1:
                    line = bytes(pending[:boundary])
                    del pending[: boundary + 1]
                    value = decode_json(line)
                    if state["result"] is not None:
                        raise ValueError("Data after final worker result")
                    if value.get("event") == "armed":
                        on_event(value)
                    elif value.get("event") == "result":
                        state["result"] = line
                    else:
                        raise ValueError("Unexpected worker event")
            if pending or state["result"] is None:
                raise ValueError("Incomplete worker event stream")
        except BaseException as error:
            state["failed"] = True
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                state["interrupted"] = error
            kill()

    def write_input():
        try:
            process.stdin.write(input)
            process.stdin.close()
        except (OSError, ValueError):
            state["failed"] = True
            kill()

    reader = threading.Thread(target=read_output, daemon=True)
    writer = threading.Thread(target=write_input, daemon=True)
    try:
        reader.start()
        writer.start()
        process.wait(timeout=timeout)
    except BaseException as error:
        kill()
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
    if state["interrupted"] is not None:
        raise state["interrupted"]
    if state["failed"] or reader.is_alive() or writer.is_alive():
        raise subprocess.SubprocessError("Invalid native event stream")
    return subprocess.CompletedProcess(command, process.returncode, state["result"])


class UtteranceCallAudio(LocalCallAudio):
    """The previous manual adapter, plus one continuously open input stream per listen."""

    def __init__(
        self, root, *, config=None, consent=False, exclusive_risk=False, run=None, listen_run=None
    ):
        super().__init__(root, consent=consent, exclusive_risk=exclusive_risk, run=run)
        self.config = config if config is not None else UtteranceConfig()
        if not isinstance(self.config, UtteranceConfig):
            raise CallError("Use a validated utterance configuration.")
        self._listen_run = listen_run or _listener_run

    def listen(self, device: AudioDevice, *, on_armed=None) -> bytes:
        if not (self.consent and self.exclusive_risk):
            raise CallError(
                "A consenting call and acknowledgement of exclusive audio risk are required."
            )
        try:
            pinned = device_value(asdict(device), direction="input")
        except (TypeError, ValueError):
            raise CallError("Select an explicit, current vivo T2x 5G capture endpoint.") from None
        if on_armed is not None and not callable(on_armed):
            raise CallError("Use a callable local armed notification.")
        settings = asdict(self.config)
        request = {
            "version": 1,
            "mode": MODE,
            "transport": TRANSPORT,
            "action": "listen",
            "device": asdict(pinned),
            "config": settings,
            "consent": True,
            "exclusive_risk": True,
        }
        identity = {
            key: request[key]
            for key in ("version", "mode", "transport", "action", "device", "config")
        }
        armed = {"seen": False}

        def validate_identity(value):
            try:
                returned_config = config_value(value.get("config"))
            except CallError:
                raise ValueError("Invalid worker configuration") from None
            if (
                any(value.get(key) != expected for key, expected in identity.items())
                or type(value.get("version")) is not int
                or device_value(value.get("device"), direction="input") != pinned
                or returned_config != self.config
            ):
                raise ValueError("Invalid worker identity")

        def ready(value):
            validate_identity(value)
            if (
                set(value) != set(identity) | {"event"}
                or value["event"] != "armed"
                or armed["seen"]
            ):
                raise ValueError("Invalid armed notification")
            armed["seen"] = True
            if on_armed is not None:
                on_armed()

        environment = worker_environment()
        environment[MARKER] = "1"
        command = [
            str(self.root / ".venv" / "Scripts" / "python.exe"),
            "-I",
            "-B",
            str(self.root / "backend" / "guardmate" / "cellular" / "utterance_worker.py"),
        ]
        try:
            result = self._listen_run(
                command,
                input=json.dumps(request, allow_nan=False).encode(),
                timeout=self.config.worker_timeout,
                cwd=str(self.root),
                env=environment,
                on_event=ready,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise CallError(
                "The utterance audio worker timed out; do not retry automatically.", uncertain=True
            ) from None
        except Exception:
            raise CallError(
                "The utterance audio worker failed; do not retry automatically.", uncertain=True
            ) from None
        try:
            response = decode_json(result.stdout)
            validate_identity(response)
            if (
                result.returncode != 0
                or response.get("event") != "result"
                or type(response.get("ok")) is not bool
            ):
                raise ValueError("Invalid worker result")
            keys = set(identity) | {"event", "ok"}
            if not response["ok"]:
                if (
                    set(response) != keys | {"code", "uncertain"}
                    or type(response["uncertain"]) is not bool
                ):
                    raise ValueError("Invalid worker failure")
                if response["code"] in ("no_speech", "too_long"):
                    if not armed["seen"] or response["uncertain"]:
                        raise ValueError("Invalid known outcome")
                    if response["code"] == "no_speech":
                        raise NoSpeech("No sustained speech arrived within the listening window.")
                    raise UtteranceTooLong(
                        "Speech did not finish within the complete utterance budget."
                    )
                if response["code"] not in SAFE_FAILURES:
                    raise ValueError("Invalid failure code")
                raise CallError(SAFE_FAILURES[response["code"]], uncertain=True)
            detail = {"audio_base64", "audio_bytes", "pcm_bytes", "sample_rate"}
            if not armed["seen"] or set(response) != keys | detail:
                raise ValueError("Unarmed or incomplete utterance")
            captured = encoded_audio(response["audio_base64"])
            rate, pcm = validate_audio(captured)
            minimum = (self.config.min_speech_ms + self.config.end_silence_ms) * 32
            if (
                rate != 16000
                or not minimum <= len(pcm) <= self.config.max_utterance_seconds * 32000
            ):
                raise ValueError("Invalid utterance length")
            for key, expected in (
                ("sample_rate", rate),
                ("audio_bytes", len(captured)),
                ("pcm_bytes", len(pcm)),
            ):
                if type(response[key]) is not int or response[key] != expected:
                    raise ValueError("Inconsistent utterance")
            return captured
        except CallError:
            raise
        except (ValueError, TypeError, KeyError, AttributeError, AudioError):
            raise CallError(
                "The utterance worker returned an invalid result; do not retry automatically.",
                uncertain=True,
            ) from None
