"""Bounded child processes for a user-confirmed pair of phone audio endpoints."""

import base64
import json
import os
import subprocess
from dataclasses import asdict
from pathlib import Path
from threading import Event, Lock

from ..cellular.call_audio import MAX_BODY_BYTES, decode_json, worker_environment
from ..cellular.call_errors import CallError
from ..cellular.windows_audio import AudioDevice, PairedAudioDevice, validate_audio
from .audio_worker import MARKER, endpoint_value


class DesktopAudio:
    def __init__(self, root: Path):
        self.root = root
        self._process = None
        self._process_lock = Lock()
        self._cancelled = Event()

    def reset(self) -> None:
        self._cancelled.clear()

    def cancel(self) -> None:
        self._cancelled.set()
        with self._process_lock:
            if self._process is not None and self._process.poll() is None:
                self._process.kill()

    def _invoke(self, request: dict, timeout: int) -> dict:
        from ..frozen_runtime import worker_command

        environment = worker_environment()
        environment[MARKER] = "1"
        body = json.dumps(request, ensure_ascii=True).encode("utf-8")
        if len(body) > MAX_BODY_BYTES:
            raise CallError("The phone audio request exceeds its size bound.")
        process = None
        try:
            with self._process_lock:
                if request["action"] != "list" and self._cancelled.is_set():
                    raise CallError("The operator stopped phone audio.", uncertain=True)
                process = subprocess.Popen(
                    worker_command("desktop-audio", root=self.root),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    env=environment,
                    shell=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    if os.name == "nt"
                    else 0,
                )
                self._process = process
            output, _ = process.communicate(body, timeout=timeout)
            if process.returncode != 0 or len(output) > MAX_BODY_BYTES:
                raise ValueError("Invalid worker result")
            result = decode_json(output)
            if result.get("ok") is not True:
                raise ValueError("Audio refused")
            return result
        except Exception:
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate(timeout=3)
            raise CallError(
                "Phone audio stopped or failed. Inspect the endpoints before another operation.",
                uncertain=request["action"] != "list",
            ) from None
        finally:
            with self._process_lock:
                self._process = None

    def devices(self) -> list[PairedAudioDevice]:
        result = self._invoke({"version": 1, "action": "list"}, 12)
        values = result.get("devices")
        if not isinstance(values, list) or len(values) > 128:
            raise CallError("The phone audio inventory was invalid.")
        try:
            devices = [endpoint_value(value) for value in values]
        except ValueError:
            raise CallError("The phone audio inventory was invalid.") from None
        if len({(device.id, device.direction) for device in devices}) != len(devices):
            raise CallError("The phone audio inventory was ambiguous.")
        return devices

    def bind(self, phone_name: str, source: PairedAudioDevice, target: PairedAudioDevice):
        return BoundPhoneAudio(self, phone_name, source, target)


class BoundPhoneAudio:
    def __init__(
        self,
        audio: DesktopAudio,
        phone_name: str,
        source: PairedAudioDevice,
        target: PairedAudioDevice,
    ):
        self.audio, self.phone_name = audio, phone_name.strip()
        self.source, self.target = source, target
        if len(self.phone_name) < 3 or source.direction != "input" or target.direction != "output":
            raise CallError("Choose both endpoints for the explicitly paired phone.")
        for endpoint in (source, target):
            if self.phone_name.casefold() not in endpoint.name.casefold():
                raise CallError("Both endpoint names must contain the paired phone name.")

    def validate(self, source: AudioDevice, target: AudioDevice) -> None:
        if (source, target) != (self.source.audio_device(), self.target.audio_device()):
            raise CallError("The selected phone identity changed; inspect again.")
        current = self.audio.devices()
        if self.source not in current or self.target not in current:
            raise CallError("A selected phone endpoint changed or disconnected; inspect again.")

    def _request(self, action: str, device: AudioDevice, **extra) -> dict:
        endpoint = self.source if action == "record" else self.target
        if device != endpoint.audio_device():
            raise CallError("Refusing an unselected audio endpoint.")
        result = self.audio._invoke(
            {
                "version": 1,
                "action": action,
                "endpoint": asdict(endpoint),
                "phone_name": self.phone_name,
                **extra,
            },
            20 if action == "record" else 40,
        )
        if result.get("endpoint") != asdict(endpoint):
            raise CallError("The phone audio worker returned a different endpoint.", uncertain=True)
        return result

    def record(self, device: AudioDevice, seconds: int = 5) -> bytes:
        result = self._request("record", device, seconds=seconds)
        try:
            captured = base64.b64decode(result["audio"], validate=True)
            rate, pcm = validate_audio(captured)
            if rate != 16000 or len(pcm) != seconds * 32000:
                raise ValueError("Incomplete capture")
            return captured
        except Exception:
            raise CallError(
                "The phone capture was invalid. Do not submit it.", uncertain=True
            ) from None

    def play(self, device: AudioDevice, wav: bytes) -> None:
        rate, _ = validate_audio(wav, max_seconds=30)
        if rate != 16000:
            raise CallError("Phone reply playback requires mono PCM16 at 16 kHz.")
        self._request("play", device, audio=base64.b64encode(wav).decode("ascii"))
