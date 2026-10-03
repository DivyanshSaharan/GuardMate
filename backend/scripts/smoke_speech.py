"""Opt-in local speech roundtrip with synthetic audio; no model API or real recordings."""

import argparse
import io
import json
import re
import sys
import wave
from array import array
from pathlib import Path
from time import perf_counter

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from guardmate.speech import SpeechError, SpeechService  # noqa: E402

PHRASES = (
    "I have a prepaid parcel. The guard is here.",
    "Nobody is at the guard room. Can you ask the resident?",
    "I gave it to the guard.",
)


def normalized(text: str) -> str:
    return " ".join(re.findall(r"[a-z]+", text.casefold()))


def pcm16_16k(audio: bytes) -> bytes:
    """Resample the known local Piper mono PCM output for the synthetic smoke check."""
    with wave.open(io.BytesIO(audio), "rb") as source:
        if source.getnchannels() != 1 or source.getsampwidth() != 2:
            raise ValueError("Expected mono PCM16 Piper output.")
        rate = source.getframerate()
        samples = array("h", source.readframes(source.getnframes()))
    if sys.byteorder != "little":
        samples.byteswap()
    output = array("h")
    for index in range(len(samples) * 16000 // rate):
        position = index * rate / 16000
        left = int(position)
        fraction = position - left
        value = samples[left] * (1 - fraction) + samples[min(left + 1, len(samples) - 1)] * fraction
        output.append(round(value))
    if sys.byteorder != "little":
        output.byteswap()
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(16000)
        target.writeframes(output.tobytes())
    return buffer.getvalue()


def artifact(value: str) -> Path:
    path = Path(value).resolve()
    if not path.is_relative_to((ROOT / ".data/evaluation").resolve()):
        raise argparse.ArgumentTypeError("Use an ignored .data/evaluation/ artifact path.")
    if path.exists():
        raise argparse.ArgumentTypeError("Refusing to overwrite an existing artifact.")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Use installed native speech runtimes.")
    parser.add_argument("--output", type=artifact, help="Save a synthetic-data JSON report.")
    parser.add_argument("--sample", type=artifact, help="Retain the first synthetic Piper WAV.")
    args = parser.parse_args()
    if args.output and args.output.suffix != ".json":
        parser.error("Report must be JSON.")
    if args.sample and args.sample.suffix != ".wav":
        parser.error("Sample must be WAV.")
    if (args.output or args.sample) and not args.run:
        parser.error("Artifact generation requires --run.")
    service = SpeechService(ROOT)
    status = service.status()
    if not args.run:
        print(json.dumps({"speech": status, "speech_jobs": 0, "model_api_requests": 0}, indent=2))
        return 0 if status["stt_ready"] and status["tts_ready"] else 1
    rows = []
    try:
        for index, phrase in enumerate(PHRASES):
            started = perf_counter()
            spoken = service.synthesize(phrase)
            tts_ms = round((perf_counter() - started) * 1000)
            if index == 0 and args.sample:
                args.sample.parent.mkdir(parents=True, exist_ok=True)
                with args.sample.open("xb") as target:
                    target.write(spoken)
            result = service.transcribe(pcm16_16k(spoken))
            # Exact normalized match is diagnostic only, not proof of natural-microphone accuracy.
            rows.append(
                {
                    "source": "synthetic_piper_roundtrip",
                    "intended": phrase,
                    **result,
                    "tts_ms": tts_ms,
                    "normalized_exact_match": normalized(phrase) == normalized(str(result["text"])),
                }
            )
    except (SpeechError, OSError, ValueError) as error:
        print(f"Local speech smoke failed ({type(error).__name__}); no model API request was made.")
        return 1
    report = {
        "model_api_requests": 0,
        "real_recordings": 0,
        "synthetic_roundtrips": rows,
        "limitations": [
            "Generated Piper speech, not a real microphone, accent, noisy-call or friend test.",
            "Local speech only; conversation planning remains hosted and untuned.",
            "Timings include fresh native process/model initialization for each speech job.",
            "No cellular connection or end-of-speech-to-reply-audio measurement.",
        ],
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as target:
            json.dump(report, target, indent=2)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
