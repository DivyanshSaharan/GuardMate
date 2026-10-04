"""Bounded PCM energy endpointing, with no audio, model, or network imports."""

from __future__ import annotations

import sys
from array import array
from collections import deque
from dataclasses import asdict, dataclass

from .call_errors import CallError

SAMPLE_RATE = 16000
FRAME_SAMPLES = 320
FRAME_BYTES = FRAME_SAMPLES * 2


class NoSpeech(CallError):
    """A completed bounded listen detected no sustained utterance."""


class UtteranceTooLong(CallError):
    """Speech did not end within the complete utterance budget; discard it."""


@dataclass(frozen=True)
class UtteranceConfig:
    idle_timeout_seconds: int = 15
    max_utterance_seconds: int = 10
    pre_roll_ms: int = 250
    onset_ms: int = 80
    min_speech_ms: int = 120
    end_silence_ms: int = 700
    start_rms: int = 300
    end_rms: int = 200

    def __post_init__(self):
        bounds = {
            "idle_timeout_seconds": (1, 30),
            "max_utterance_seconds": (1, 10),
            "pre_roll_ms": (0, 500),
            "onset_ms": (20, 500),
            "min_speech_ms": (20, 1000),
            "end_silence_ms": (100, 2000),
            "start_rms": (1, 32767),
            "end_rms": (1, 32767),
        }
        for name, (low, high) in bounds.items():
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise CallError(
                    "Use bounded whole-integer utterance settings; booleans are refused."
                )
        if self.end_rms > self.start_rms or self.min_speech_ms < self.onset_ms:
            raise CallError("Use coherent speech onset and ending thresholds.")
        if (
            self.pre_roll_ms + self.onset_ms + self.end_silence_ms
            >= self.max_utterance_seconds * 1000
        ):
            raise CallError(
                "The utterance budget must include pre-roll, onset, and ending silence."
            )
        if self.min_speech_ms + self.end_silence_ms >= self.max_utterance_seconds * 1000:
            raise CallError("The utterance budget must include minimum speech and ending silence.")

    @property
    def worker_timeout(self) -> int:
        return min(45, self.idle_timeout_seconds + self.max_utterance_seconds + 5)


def config_value(value) -> UtteranceConfig:
    if type(value) is not dict or set(value) != set(asdict(UtteranceConfig())):
        raise CallError("Use the complete bounded utterance configuration.")
    return UtteranceConfig(**value)


def _above(pcm: bytes, threshold: int) -> bool:
    samples = array("h")
    samples.frombytes(pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    count = len(samples)
    total = sum(samples)
    energy = count * sum(value * value for value in samples) - total * total
    # Compare DC-resistant squared RMS using integers, without floats or sqrt.
    return energy >= threshold * threshold * count * count


class UtteranceEndpointer:
    """Feed one continuous PCM16 stream; complete outcomes never include a clipped tail."""

    def __init__(self, config: UtteranceConfig | None = None):
        self.config = config if config is not None else UtteranceConfig()
        if not isinstance(self.config, UtteranceConfig):
            raise CallError("Use a validated utterance configuration.")
        self.status: str | None = None
        self._pending = bytearray()
        self._history = deque()
        self._history_bytes = 0
        self._pre_roll_bytes = max(
            self.config.pre_roll_ms * SAMPLE_RATE * 2 // 1000,
            ((self.config.onset_ms + 19) // 20) * FRAME_BYTES,
        )
        self._max_bytes = self.config.max_utterance_seconds * SAMPLE_RATE * 2
        self._clip = bytearray()
        self._active = False
        self._onset_samples = 0
        self._speech_samples = 0
        self._quiet_samples = 0
        self._total_samples = 0

    @property
    def pcm(self) -> bytes:
        if self.status != "utterance":
            raise CallError("Only a complete utterance has audio data.")
        return bytes(self._clip)

    def _remember(self, frame):
        self._history.append(frame)
        self._history_bytes += len(frame)
        while self._history_bytes > self._pre_roll_bytes and self._history:
            excess = self._history_bytes - self._pre_roll_bytes
            first = self._history.popleft()
            if len(first) > excess:
                self._history.appendleft(first[excess:])
                self._history_bytes -= excess
            else:
                self._history_bytes -= len(first)

    def _finish(self, status):
        self.status = status
        self._pending.clear()
        self._history.clear()
        self._history_bytes = 0
        if status != "utterance":
            self._clip.clear()

    def _frame(self, frame):
        self._total_samples += FRAME_SAMPLES
        if not self._active:
            self._remember(frame)
            self._onset_samples = (
                self._onset_samples + FRAME_SAMPLES if _above(frame, self.config.start_rms) else 0
            )
            if self._onset_samples * 1000 >= self.config.onset_ms * SAMPLE_RATE:
                self._active = True
                self._clip.extend(b"".join(self._history))
                self._speech_samples = self._onset_samples
                self._quiet_samples = 0
            elif self._total_samples >= self.config.idle_timeout_seconds * SAMPLE_RATE:
                self._finish("no_speech")
            return
        if len(self._clip) + len(frame) > self._max_bytes:
            self._finish("too_long")
            return
        self._clip.extend(frame)
        if _above(frame, self.config.end_rms):
            self._speech_samples += FRAME_SAMPLES
            self._quiet_samples = 0
        else:
            self._quiet_samples += FRAME_SAMPLES
        if self._quiet_samples * 1000 >= self.config.end_silence_ms * SAMPLE_RATE:
            if self._speech_samples * 1000 >= self.config.min_speech_ms * SAMPLE_RATE:
                self._finish("utterance")
            else:
                # Discard a short click/burst while retaining this same open stream.
                self._active = False
                self._clip.clear()
                self._onset_samples = 0
                self._speech_samples = 0
                self._quiet_samples = 0
                self._history.clear()
                self._history_bytes = 0
                if self._total_samples >= self.config.idle_timeout_seconds * SAMPLE_RATE:
                    self._finish("no_speech")
        elif len(self._clip) >= self._max_bytes:
            self._finish("too_long")

    def feed(self, pcm) -> str | None:
        if self.status is not None:
            raise CallError("A completed utterance listener cannot accept more audio.")
        try:
            view = memoryview(pcm).cast("B")
        except (TypeError, ValueError):
            raise CallError("Feed complete mono PCM16 frames to the utterance listener.") from None
        if not 2 <= len(view) <= SAMPLE_RATE * 2 or len(view) % 2:
            raise CallError("Feed complete bounded mono PCM16 frames to the utterance listener.")
        self._pending.extend(view)
        while len(self._pending) >= FRAME_BYTES and self.status is None:
            frame = bytes(self._pending[:FRAME_BYTES])
            del self._pending[:FRAME_BYTES]
            self._frame(frame)
        return self.status
