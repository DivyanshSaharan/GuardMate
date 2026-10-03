"""Bounded local Piper worker. Runs in a killable subprocess; never imports application state."""

import json
import sys
import wave
from pathlib import Path

MAX_OUTPUT_BYTES = 2_000_000


class BoundedAudioFile:
    def __init__(self, path: Path):
        self.file = path.open("w+b")

    def write(self, data: bytes):
        if self.file.tell() + len(data) > MAX_OUTPUT_BYTES:
            raise ValueError("Audio limit exceeded")
        return self.file.write(data)

    def __getattr__(self, name):
        return getattr(self.file, name)


def synthesize(model: Path, output: Path, text: str) -> None:
    if not text.strip() or len(text) > 1500:
        raise ValueError("Invalid text length")
    config = json.loads(Path(str(model) + ".json").read_text(encoding="utf-8"))
    if config.get("phoneme_type") != "espeak" or not config.get("espeak", {}).get(
        "voice", ""
    ).startswith("en"):
        raise ValueError("An English local espeak voice is required")
    from piper import PiperVoice

    voice = PiperVoice.load(str(model), use_cuda=False)
    bounded = BoundedAudioFile(output)
    try:
        with wave.open(bounded, "wb") as wav:
            voice.synthesize_wav(text, wav)
    finally:
        bounded.file.close()


def main() -> int:
    try:
        if len(sys.argv) != 3:
            return 1
        raw_text = sys.stdin.buffer.read(6001)
        if len(raw_text) > 6000:
            return 1
        synthesize(Path(sys.argv[1]), Path(sys.argv[2]), raw_text.decode("utf-8"))
    except Exception:
        # No private assistant text, model paths, or native errors are emitted to process logs.
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
