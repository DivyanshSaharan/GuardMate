"""One killable, explicitly selected desktop phone audio operation."""

import base64
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from guardmate.cellular.call_audio import MAX_BODY_BYTES, decode_json  # noqa: E402
from guardmate.cellular.kernel_audio import WindowsKernelAudio  # noqa: E402
from guardmate.cellular.windows_audio import PairedAudioDevice  # noqa: E402

MARKER = "GUARDMATE_DESKTOP_AUDIO_WORKER"


def endpoint_value(value) -> PairedAudioDevice:
    if (
        not isinstance(value, dict)
        or set(value) != {"id", "name", "direction", "host_api"}
        or type(value["id"]) is not int
        or not 0 <= value["id"] < 0xFFFFFFFF
        or not isinstance(value["name"], str)
        or not 1 <= len(value["name"]) <= 256
        or value["direction"] not in {"input", "output"}
        or value["host_api"] != "Windows WDM-KS"
    ):
        raise ValueError("Invalid endpoint pin")
    return PairedAudioDevice(**value)


def handle(request, *, audio=None) -> dict:
    audio = audio if audio is not None else WindowsKernelAudio()
    try:
        if request == {"version": 1, "action": "list"} and type(request["version"]) is int:
            devices = audio.paired_devices()
            if len(devices) > 128:
                raise ValueError("Too many endpoints")
            return {"ok": True, "devices": [asdict(device) for device in devices]}
        action = request.get("action")
        extra = {"record": {"seconds"}, "play": {"audio"}}.get(action)
        if extra is None or set(request) != {"version", "action", "endpoint", "phone_name"} | extra:
            raise ValueError("Invalid audio request")
        if type(request["version"]) is not int or request["version"] != 1:
            raise ValueError("Invalid protocol version")
        endpoint = endpoint_value(request["endpoint"])
        phone_name = request["phone_name"]
        if not isinstance(phone_name, str) or len(phone_name.strip()) < 3:
            raise ValueError("A paired phone name is required")
        if phone_name.casefold() not in endpoint.name.casefold():
            raise ValueError("Endpoint is not the selected phone")
        device = audio.validate_paired(endpoint)
        if action == "record":
            if endpoint.direction != "input" or type(request["seconds"]) is not int:
                raise ValueError("Invalid capture")
            if not 1 <= request["seconds"] <= 10:
                raise ValueError("Invalid capture duration")
            captured = audio.record(device, request["seconds"], 16000)
            return {
                "ok": True,
                "endpoint": asdict(endpoint),
                "audio": base64.b64encode(captured).decode("ascii"),
            }
        if endpoint.direction != "output" or not isinstance(request["audio"], str):
            raise ValueError("Invalid playback")
        audio.play_reply(device, base64.b64decode(request["audio"], validate=True))
        return {"ok": True, "endpoint": asdict(endpoint)}
    except Exception:
        return {
            "ok": False,
            "message": "The selected phone audio operation failed. Inspect the "
            "endpoints and handle the call manually; no automatic retry.",
        }


def main() -> int:
    if os.environ.get(MARKER) != "1":
        return 2
    try:
        request = decode_json(sys.stdin.buffer.read(MAX_BODY_BYTES + 1))
        response = json.dumps(handle(request), ensure_ascii=True).encode("utf-8")
        if len(response) > MAX_BODY_BYTES:
            return 2
        sys.stdout.buffer.write(response)
        sys.stdout.buffer.flush()
        return 0
    except Exception:
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
