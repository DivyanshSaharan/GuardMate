import importlib.util
import io
import math
import os
import re
import struct
import subprocess
import sys
import tempfile
import wave
from array import array
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from threading import BoundedSemaphore
from time import perf_counter

MAX_SECONDS = 30
SAMPLE_RATE = 16000
MAX_WAV_BYTES = MAX_SECONDS * SAMPLE_RATE * 2 + 4096
MAX_TRANSCRIPT_BYTES = 4096
MAX_TRANSCRIPT_CHARS = 600
MAX_TTS_CHARS = 1500
MAX_TTS_BYTES = 2_000_000
SPEECH_TIMEOUT = 45


class SpeechError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def _subprocess_environment() -> dict[str, str]:
    # The backend loads model credentials; local audio workers do not need those secrets.
    allowed = {
        "SYSTEMROOT",
        "WINDIR",
        "TEMP",
        "TMP",
        "PATH",
        "HOME",
        "USERPROFILE",
        "LOCALAPPDATA",
        "APPDATA",
        "LD_LIBRARY_PATH",
        "DYLD_LIBRARY_PATH",
    }
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    env.update({"OMP_NUM_THREADS": "4", "OPENBLAS_NUM_THREADS": "4", "MKL_NUM_THREADS": "4"})
    return env


def validate_recording(audio: bytes) -> int:
    """Reject malformed, truncated, non-PCM, oversized, or effectively silent input."""
    if len(audio) > MAX_WAV_BYTES:
        raise SpeechError(413, "Record at most 30 seconds of audio.")
    if (
        len(audio) < 44
        or audio[:4] != b"RIFF"
        or audio[8:12] != b"WAVE"
        or struct.unpack_from("<I", audio, 4)[0] + 8 != len(audio)
    ):
        raise SpeechError(422, "Use a complete PCM16 mono 16 kHz WAV recording.")
    # wave alone tolerates inconsistent byte-rate/alignment fields and duplicate chunks.
    # Validate the untrusted RIFF structure before handing it to a native decoder.
    offset, format_seen, data_seen = 12, False, False
    while offset < len(audio):
        if offset + 8 > len(audio):
            raise SpeechError(422, "Use a complete PCM16 mono 16 kHz WAV recording.")
        chunk_id, chunk_size = struct.unpack_from("<4sI", audio, offset)
        start = offset + 8
        end = start + chunk_size
        if end + (chunk_size % 2) > len(audio):
            raise SpeechError(422, "Use a complete PCM16 mono 16 kHz WAV recording.")
        if chunk_id == b"fmt ":
            if (
                format_seen
                or data_seen
                or chunk_size < 16
                or struct.unpack_from("<HHIIHH", audio, start)
                != (1, 1, SAMPLE_RATE, SAMPLE_RATE * 2, 2, 16)
            ):
                raise SpeechError(422, "Use PCM16 mono 16 kHz WAV audio.")
            format_seen = True
        if chunk_id == b"data":
            if data_seen or not format_seen or chunk_size % 2:
                raise SpeechError(422, "Use a complete PCM16 mono 16 kHz WAV recording.")
            data_seen = True
        offset = end + (chunk_size % 2)
    if not format_seen or not data_seen:
        raise SpeechError(422, "Use a complete PCM16 mono 16 kHz WAV recording.")
    try:
        with wave.open(io.BytesIO(audio), "rb") as recording:
            if (
                recording.getnchannels() != 1
                or recording.getsampwidth() != 2
                or recording.getframerate() != SAMPLE_RATE
                or recording.getcomptype() != "NONE"
            ):
                raise SpeechError(422, "Use PCM16 mono 16 kHz WAV audio.")
            frames = recording.getnframes()
            if frames > MAX_SECONDS * SAMPLE_RATE:
                raise SpeechError(413, "Record at most 30 seconds of audio.")
            samples = recording.readframes(frames + 1)
            if not frames or len(samples) != frames * 2:
                raise SpeechError(422, "The recording is empty or incomplete.")
    except (wave.Error, EOFError, OSError, struct.error) as error:
        raise SpeechError(422, "Use a complete PCM16 mono 16 kHz WAV recording.") from error
    pcm = array("h")
    pcm.frombytes(samples)
    if sys.byteorder != "little":
        pcm.byteswap()
    # Subtract DC bias: a constant non-zero waveform is silence too. This is not speech detection.
    mean = sum(pcm) / len(pcm)
    rms = math.sqrt(sum((sample - mean) ** 2 for sample in pcm) / len(pcm))
    if rms < 30:
        raise SpeechError(422, "The recording is silent or too quiet. Please record again.")
    return round(frames * 1000 / SAMPLE_RATE)


class SpeechService:
    def __init__(
        self,
        root: Path,
        *,
        whisper_cli: Path | None = None,
        whisper_model: Path | None = None,
        piper_model: Path | None = None,
        piper_available: Callable[[], bool] | None = None,
    ):
        default_cli = root / ".cache" / "voice" / "whisper-v1.8.2" / "Release" / "whisper-cli.exe"
        if os.name != "nt":
            default_cli = default_cli.with_suffix("")
        self.whisper_cli = whisper_cli or Path(
            os.environ.get("GUARDMATE_WHISPER_CLI", str(default_cli))
        )
        self.whisper_model = whisper_model or Path(
            os.environ.get("GUARDMATE_WHISPER_MODEL", str(root / "models/speech/ggml-base.en.bin"))
        )
        self.piper_model = piper_model or Path(
            os.environ.get(
                "GUARDMATE_PIPER_MODEL", str(root / "models/speech/en_US-ljspeech-high.onnx")
            )
        )
        self._piper_available = piper_available or (
            lambda: importlib.util.find_spec("piper") is not None
        )
        # No work queue or parallel native inference in this single-resident prototype.
        self._job = BoundedSemaphore(1)

    def status(self) -> dict[str, bool | str | int]:
        stt_ready = self.whisper_cli.is_file() and self.whisper_model.is_file()
        tts_ready = (
            self.piper_model.is_file()
            and Path(str(self.piper_model) + ".json").is_file()
            and self._piper_available()
        )
        message = (
            "Local speech assets are present. English browser role-play only; no phone connection."
            if stt_ready and tts_ready
            else "Local speech needs whisper.cpp, an English Whisper model, and Piper voice assets."
        )
        return {
            "stt_ready": stt_ready,
            "tts_ready": tts_ready,
            "message": message,
            "max_seconds": MAX_SECONDS,
        }

    @contextmanager
    def _slot(self):
        if not self._job.acquire(blocking=False):
            raise SpeechError(429, "Local speech is busy. Please try again when it finishes.")
        try:
            yield
        finally:
            self._job.release()

    @staticmethod
    def _run(command: list[str], directory: Path, text: str | None = None) -> None:
        try:
            result = subprocess.run(
                command,
                input=text.encode("utf-8") if text is not None else None,
                stdin=subprocess.DEVNULL if text is None else None,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=SPEECH_TIMEOUT,
                check=False,
                cwd=directory,
                env=_subprocess_environment(),
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except subprocess.TimeoutExpired as error:
            raise SpeechError(
                504, "Local speech took too long. Please try a shorter message."
            ) from error
        except OSError as error:
            raise SpeechError(503, "The local speech runtime could not start.") from error
        if result.returncode != 0:
            raise SpeechError(503, "Local speech failed. Check the local speech setup.")

    def transcribe(self, audio: bytes) -> dict[str, str | int]:
        duration_ms = validate_recording(audio)
        if not self.status()["stt_ready"]:
            raise SpeechError(503, "Local transcription is not configured yet.")
        with self._slot(), tempfile.TemporaryDirectory(prefix="guardmate-speech-") as raw_dir:
            started = perf_counter()
            directory = Path(raw_dir)
            recording = directory / "recording.wav"
            output = directory / "transcript"
            recording.write_bytes(audio)
            self._run(
                [
                    str(self.whisper_cli.resolve()),
                    "-m",
                    str(self.whisper_model.resolve()),
                    "-f",
                    str(recording),
                    "-l",
                    "en",
                    "-t",
                    "4",
                    "-p",
                    "1",
                    "-ng",
                    "-otxt",
                    "-of",
                    str(output),
                    "-np",
                    "-nt",
                    "-nf",
                ],
                directory,
            )
            transcript = output.with_suffix(".txt")
            try:
                with transcript.open("rb") as file:
                    raw_text = file.read(MAX_TRANSCRIPT_BYTES + 1)
                if len(raw_text) > MAX_TRANSCRIPT_BYTES:
                    raise SpeechError(422, "The transcript is too long. Record a shorter message.")
                text = " ".join(raw_text.decode("utf-8").split())
            except (OSError, UnicodeError) as error:
                raise SpeechError(
                    503, "Local transcription did not produce a readable result."
                ) from error
            if not text or re.fullmatch(
                r"[\[\(]?(?:blank[_ ]audio|silence|music)[\]\)]?", text, re.I
            ):
                raise SpeechError(422, "No speech was recognized. Please record again.")
            if len(text) > MAX_TRANSCRIPT_CHARS:
                raise SpeechError(422, "The transcript is too long. Record a shorter message.")
            return {
                "text": text,
                "duration_ms": duration_ms,
                "processing_ms": round((perf_counter() - started) * 1000),
            }

    def synthesize(self, text: str) -> bytes:
        if not text.strip() or len(text) > MAX_TTS_CHARS:
            raise SpeechError(422, "This saved reply cannot be spoken. Read its text instead.")
        if not self.status()["tts_ready"]:
            raise SpeechError(503, "Local reply speech is not configured yet.")
        with self._slot(), tempfile.TemporaryDirectory(prefix="guardmate-speech-") as raw_dir:
            directory = Path(raw_dir)
            output = directory / "reply.wav"
            worker = Path(__file__).with_name("worker.py")
            self._run(
                [sys.executable, str(worker), str(self.piper_model.resolve()), str(output)],
                directory,
                text,
            )
            try:
                with output.open("rb") as file:
                    audio = file.read(MAX_TTS_BYTES + 1)
                if len(audio) > MAX_TTS_BYTES:
                    raise SpeechError(503, "The spoken reply exceeded the local audio limit.")
                with wave.open(io.BytesIO(audio), "rb") as spoken:
                    if (
                        spoken.getnchannels() != 1
                        or spoken.getsampwidth() != 2
                        or not 8000 <= spoken.getframerate() <= 48000
                        or not 0 < spoken.getnframes() / spoken.getframerate() <= 60
                        or len(spoken.readframes(spoken.getnframes() + 1))
                        != spoken.getnframes() * 2
                    ):
                        raise SpeechError(503, "Local reply speech produced invalid audio.")
            except (OSError, wave.Error, EOFError) as error:
                raise SpeechError(
                    503, "Local reply speech did not produce a readable result."
                ) from error
            return audio
