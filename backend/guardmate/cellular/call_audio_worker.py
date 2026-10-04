"""Private native-audio child. JSON stdin/stdout only; no files or model imports."""

from __future__ import annotations

import base64
import os
import sys
from dataclasses import asdict
from pathlib import Path

# -I ignores PYTHONPATH; import only this repository's small cellular modules.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from guardmate.cellular.call_audio import (  # noqa: E402
    MARKER,
    MAX_BODY_BYTES,
    MAX_DEVICES,
    MODE,
    PHONE_LABEL,
    TRANSPORT,
    decode_json,
    device_value,
    encoded_audio,
)
from guardmate.cellular.kernel_audio import WindowsKernelAudio  # noqa: E402
from guardmate.cellular.windows_audio import AudioError, validate_audio  # noqa: E402


def handle(request, *, audio=None):
    action = request.get("action") if type(request) is dict else None
    response = {
        "version": 1,
        "mode": MODE,
        "transport": TRANSPORT,
        "action": action,
        "device": request.get("device") if type(request) is dict else None,
        "ok": False,
    }
    native_started = False
    try:
        base_keys = {"version", "mode", "transport", "action", "consent", "exclusive_risk"}
        extra = {
            "list": set(),
            "record": {"device", "seconds"},
            "play": {"device", "audio_base64", "audio_bytes"},
        }
        if (
            type(request) is not dict
            or type(action) is not str
            or action not in extra
            or set(request) != base_keys | extra[action]
            or type(request["version"]) is not int
            or request["version"] != 1
            or request["mode"] != MODE
            or request["transport"] != TRANSPORT
            or type(request["consent"]) is not bool
            or type(request["exclusive_risk"]) is not bool
        ):
            raise ValueError("Invalid request")
        if action != "list" and not (request["consent"] and request["exclusive_risk"]):
            raise ValueError("Missing acknowledgements")
        if action == "record":
            seconds = request["seconds"]
            if type(seconds) is not int or not 1 <= seconds <= 10:
                raise ValueError("Invalid capture duration")
            device = device_value(request["device"], direction="input")
        elif action == "play":
            device = device_value(request["device"], direction="output")
            wav = encoded_audio(request["audio_base64"])
            rate, pcm = validate_audio(wav, max_seconds=30)
            if (
                rate != 16000
                or type(request["audio_bytes"]) is not int
                or request["audio_bytes"] != len(wav)
            ):
                raise ValueError("Invalid playback")
        audio = audio if audio is not None else WindowsKernelAudio()
        devices = audio.devices()
        if action == "list":
            values = [
                asdict(value)
                for value in devices
                if PHONE_LABEL.casefold() in value.name.casefold()
            ]
            if len(values) > MAX_DEVICES:
                raise ValueError("Too many phone endpoints")
            for value in values:
                device_value(value)
            response.update(ok=True, devices=values)
        else:
            if device not in devices:
                response.update(code="endpoint_changed", uncertain=False)
                return response
            native_started = True
            if action == "record":
                captured = audio.record(device, seconds, 16000)
                rate, pcm = validate_audio(captured)
                if rate != 16000 or len(pcm) != seconds * 32000:
                    raise AudioError("Incomplete recording")
                response.update(
                    ok=True,
                    sample_rate=rate,
                    pcm_bytes=len(pcm),
                    audio_bytes=len(captured),
                    seconds=seconds,
                    audio_base64=base64.b64encode(captured).decode("ascii"),
                )
            else:
                audio.play_reply(device, wav)
                response.update(
                    ok=True,
                    sample_rate=rate,
                    pcm_bytes=len(pcm),
                    audio_bytes=len(wav),
                    completed=True,
                )
        return response
    except (ValueError, TypeError, KeyError, AudioError) as error:
        code = "native_failure" if native_started else "invalid_request"
        if isinstance(error, AudioError):
            if str(error) == "The WDM-KS endpoint is unavailable or occupied; no retry.":
                code = "endpoint_unavailable"
            elif str(error) == "The optional sounddevice audio dependency is unavailable.":
                code = "dependency_missing"
        response.update(ok=False, code=code, uncertain=native_started)
        return response
    except BaseException:
        response.update(ok=False, code="native_failure", uncertain=native_started)
        return response


def main() -> int:
    if os.environ.get(MARKER) != "1":
        return 2
    try:
        request = decode_json(sys.stdin.buffer.read(MAX_BODY_BYTES + 1))
        response = handle(request)
        import json

        result = json.dumps(response, ensure_ascii=True, allow_nan=False).encode("utf-8")
        if len(result) > MAX_BODY_BYTES:
            return 2
        sys.stdout.buffer.write(result)
        sys.stdout.buffer.flush()
        return 0
    except BaseException:
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
