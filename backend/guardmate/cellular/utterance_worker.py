"""Private isolated JSONL worker for one bounded, continuous phone utterance."""

from __future__ import annotations

import base64
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from guardmate.cellular.call_audio import (  # noqa: E402
    MAX_BODY_BYTES,
    TRANSPORT,
    decode_json,
    device_value,
)
from guardmate.cellular.kernel_audio import WindowsKernelAudio  # noqa: E402
from guardmate.cellular.utterance import (  # noqa: E402
    NoSpeech,
    UtteranceTooLong,
    config_value,
)
from guardmate.cellular.utterance_audio import MARKER, MODE  # noqa: E402
from guardmate.cellular.windows_audio import AudioError, validate_audio  # noqa: E402


def handle(request, *, emit, audio=None):
    request = request if type(request) is dict else {}
    identity = {
        key: request.get(key)
        for key in ("version", "mode", "transport", "action", "device", "config")
    }
    response = {**identity, "event": "result", "ok": False}
    native_started = False
    armed = False
    try:
        if (
            set(request) != set(identity) | {"consent", "exclusive_risk"}
            or type(request["version"]) is not int
            or request["version"] != 1
            or request["mode"] != MODE
            or request["transport"] != TRANSPORT
            or request["action"] != "listen"
            or request["consent"] is not True
            or request["exclusive_risk"] is not True
        ):
            raise ValueError("Invalid listening request")
        device = device_value(request["device"], direction="input")
        config = config_value(request["config"])
        identity["config"] = asdict(config)
        audio = audio if audio is not None else WindowsKernelAudio()
        if device not in audio.devices():
            response.update(code="endpoint_changed", uncertain=False)
            return response

        def ready():
            nonlocal armed
            if armed:
                raise ValueError("Duplicate native armed signal")
            armed = True
            emit({**identity, "event": "armed"})

        native_started = True
        captured = audio.listen_for_utterance(device, config=config, on_armed=ready)
        rate, pcm = validate_audio(captured)
        minimum = (config.min_speech_ms + config.end_silence_ms) * 32
        if (
            not armed
            or rate != 16000
            or not minimum <= len(pcm) <= config.max_utterance_seconds * 32000
        ):
            raise AudioError("Invalid completed utterance")
        response.update(
            ok=True,
            sample_rate=rate,
            audio_bytes=len(captured),
            pcm_bytes=len(pcm),
            audio_base64=base64.b64encode(captured).decode("ascii"),
        )
        return response
    except (NoSpeech, UtteranceTooLong) as error:
        if not armed:
            response.update(code="native_failure", uncertain=True)
        else:
            response.update(
                code="no_speech" if isinstance(error, NoSpeech) else "too_long", uncertain=False
            )
        return response
    except BaseException as error:
        code = "native_failure" if native_started else "invalid_request"
        if isinstance(error, AudioError):
            if str(error) == "The WDM-KS endpoint is unavailable or occupied; no retry.":
                code = "endpoint_unavailable"
            elif str(error) == "The optional sounddevice audio dependency is unavailable.":
                code = "dependency_missing"
        response.update(code=code, uncertain=native_started)
        return response


def main() -> int:
    if os.environ.get(MARKER) != "1":
        return 2
    total = 0

    def emit(value):
        nonlocal total
        body = json.dumps(value, ensure_ascii=True, allow_nan=False).encode() + b"\n"
        total += len(body)
        if total > MAX_BODY_BYTES:
            raise ValueError("Worker output exceeded its bound")
        sys.stdout.buffer.write(body)
        sys.stdout.buffer.flush()

    try:
        request = decode_json(sys.stdin.buffer.read(MAX_BODY_BYTES + 1))
        emit(handle(request, emit=emit))
        return 0
    except BaseException:
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
