"""Explicit-device, bounded Windows WinMM audio for a local routing proof.

No default endpoint is used. Device names are the legacy WinMM names (at most
31 UTF-16 characters), so callers must retain the enumerated id and name together.
The module imports only the standard library and loads winmm.dll lazily.
"""

from __future__ import annotations

import ctypes
import io
import os
import struct
import sys
import time
import wave
from dataclasses import dataclass
from typing import Literal


class AudioError(Exception):
    """A safe, local audio failure, without driver or file contents."""


@dataclass(frozen=True)
class AudioDevice:
    id: int
    name: str
    direction: Literal["input", "output"]


_WORD = ctypes.c_uint16
_DWORD = ctypes.c_uint32
_UINT_PTR = ctypes.c_size_t
_HANDLE = ctypes.c_void_p
_WAVE_FORMAT_QUERY = 1
_WAVERR_BADFORMAT = 32
_WAVERR_STILLPLAYING = 33
_WHDR_DONE = 1
_WHDR_PREPARED = 2
_MAX_SECONDS = 10
_WAIT_MARGIN = 2.0
_CLEANUP_SECONDS = 0.5


class _WaveFormat(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("wFormatTag", _WORD),
        ("nChannels", _WORD),
        ("nSamplesPerSec", _DWORD),
        ("nAvgBytesPerSec", _DWORD),
        ("nBlockAlign", _WORD),
        ("wBitsPerSample", _WORD),
        ("cbSize", _WORD),
    ]


class _WaveInCaps(ctypes.Structure):
    _fields_ = [
        ("wMid", _WORD),
        ("wPid", _WORD),
        ("vDriverVersion", _DWORD),
        ("szPname", _WORD * 32),
        ("dwFormats", _DWORD),
        ("wChannels", _WORD),
        ("wReserved1", _WORD),
    ]


class _WaveOutCaps(ctypes.Structure):
    _fields_ = _WaveInCaps._fields_ + [("dwSupport", _DWORD)]


class _WaveHeader(ctypes.Structure):
    pass


_WaveHeader._fields_ = [
    ("lpData", ctypes.c_void_p),
    ("dwBufferLength", _DWORD),
    ("dwBytesRecorded", _DWORD),
    ("dwUser", _UINT_PTR),
    ("dwFlags", _DWORD),
    ("dwLoops", _DWORD),
    ("lpNext", ctypes.POINTER(_WaveHeader)),
    ("reserved", _UINT_PTR),
]

# If a broken driver refuses unprepare/close, its pointers must remain live for
# the lifetime of the process. The CLI additionally kills a bounded worker.
_UNRELEASED_BUFFERS: list[tuple] = []


def _configure(winmm) -> None:
    """Use Win32 widths even on hosts where ctypes.c_ulong is 64 bits."""
    for prefix, caps in (("waveIn", _WaveInCaps), ("waveOut", _WaveOutCaps)):
        functions = {
            "GetNumDevs": [],
            "GetDevCapsW": [_UINT_PTR, ctypes.POINTER(caps), _DWORD],
            "Open": [
                ctypes.POINTER(_HANDLE),
                _DWORD,
                ctypes.POINTER(_WaveFormat),
                _UINT_PTR,
                _UINT_PTR,
                _DWORD,
            ],
            "PrepareHeader": [_HANDLE, ctypes.POINTER(_WaveHeader), _DWORD],
            "UnprepareHeader": [_HANDLE, ctypes.POINTER(_WaveHeader), _DWORD],
            "Reset": [_HANDLE],
            "Close": [_HANDLE],
        }
        if prefix == "waveIn":
            functions.update(
                AddBuffer=[_HANDLE, ctypes.POINTER(_WaveHeader), _DWORD],
                Start=[_HANDLE],
            )
        else:
            functions["Write"] = [_HANDLE, ctypes.POINTER(_WaveHeader), _DWORD]
        for suffix, args in functions.items():
            function = getattr(winmm, prefix + suffix)
            function.argtypes = args
            function.restype = _DWORD


def _format(sample_rate: int) -> _WaveFormat:
    if type(sample_rate) is not int or sample_rate not in (8000, 16000):
        raise AudioError("Use an 8000 or 16000 Hz sample rate.")
    return _WaveFormat(1, 1, sample_rate, sample_rate * 2, 2, 16, 0)


def _caps_name(caps) -> str:
    values = []
    for value in caps.szPname:
        if value == 0:
            break
        values.append(value)
    return struct.pack("<" + "H" * len(values), *values).decode("utf-16-le", errors="replace")


def validate_audio(audio: bytes) -> tuple[int, bytes]:
    """Validate the complete RIFF container and PCM metadata before playback."""
    if type(audio) is not bytes or not 44 <= len(audio) <= 324096:
        raise AudioError("Use a complete PCM16 mono WAV of at most 10 seconds.")
    if audio[:4] != b"RIFF" or audio[8:12] != b"WAVE":
        raise AudioError("Use a complete PCM16 mono WAV of at most 10 seconds.")
    if struct.unpack_from("<I", audio, 4)[0] + 8 != len(audio):
        raise AudioError("The WAV length is inconsistent or incomplete.")
    offset = 12
    fmt = None
    pcm = None
    while offset < len(audio):
        if offset + 8 > len(audio):
            raise AudioError("The WAV length is inconsistent or incomplete.")
        kind = audio[offset : offset + 4]
        length = struct.unpack_from("<I", audio, offset + 4)[0]
        start = offset + 8
        end = start + length
        padded_end = end + length % 2
        if padded_end > len(audio):
            raise AudioError("The WAV length is inconsistent or incomplete.")
        if kind == b"fmt ":
            if fmt is not None or length not in (16, 18):
                raise AudioError("The WAV must contain one standard PCM format.")
            fmt = struct.unpack_from("<HHIIHH", audio, start)
            if length == 18 and struct.unpack_from("<H", audio, start + 16)[0]:
                raise AudioError("The WAV must contain one standard PCM format.")
        elif kind == b"data":
            if pcm is not None or fmt is None:
                raise AudioError("The WAV must contain one PCM data chunk after its format.")
            pcm = audio[start:end]
        offset = padded_end
    if fmt is None or pcm is None:
        raise AudioError("The WAV is missing its format or audio data.")
    tag, channels, rate, byte_rate, align, bits = fmt
    if (
        tag != 1
        or channels != 1
        or rate not in (8000, 16000)
        or byte_rate != rate * 2
        or align != 2
        or bits != 16
        or not pcm
        or len(pcm) % 2
        or len(pcm) > rate * 2 * _MAX_SECONDS
    ):
        raise AudioError("Use a complete PCM16 mono WAV of at most 10 seconds.")
    return rate, pcm


class WindowsAudio:
    """WinMM access bound to a freshly checked, explicit endpoint on every use.

    ``winmm`` is an optional injected API used by hardware-free unit tests.
    Native driver calls themselves are synchronous; callers requiring a hard
    wall-clock bound against a hung driver must use a disposable subprocess.
    """

    def __init__(self, *, winmm=None):
        self._winmm = winmm

    def _api(self):
        if self._winmm is None:
            if os.name != "nt":
                raise AudioError("Windows WinMM audio is available only on Windows.")
            try:
                winmm = ctypes.WinDLL("winmm.dll")
                _configure(winmm)
            except (OSError, AttributeError):
                raise AudioError("Windows audio is unavailable.") from None
            self._winmm = winmm
        return self._winmm

    def devices(self) -> list[AudioDevice]:
        api = self._api()
        devices = []
        try:
            for prefix, caps_type, direction in (
                ("waveIn", _WaveInCaps, "input"),
                ("waveOut", _WaveOutCaps, "output"),
            ):
                count = getattr(api, prefix + "GetNumDevs")()
                if not 0 <= count <= 65535:
                    raise AudioError("Windows audio device enumeration failed.")
                for identifier in range(count):
                    caps = caps_type()
                    result = getattr(api, prefix + "GetDevCapsW")(
                        identifier, ctypes.byref(caps), ctypes.sizeof(caps)
                    )
                    if result != 0:
                        raise AudioError("Windows audio device enumeration failed.")
                    name = _caps_name(caps)
                    if name:
                        devices.append(AudioDevice(identifier, name, direction))
        except (OSError, ValueError):
            raise AudioError("Windows audio device enumeration failed.") from None
        return devices

    def _validate_device(self, device: AudioDevice, direction=None) -> None:
        if (
            not isinstance(device, AudioDevice)
            or type(device.id) is not int
            or not 0 <= device.id < 0xFFFFFFFF
            or device.direction not in ("input", "output")
            or not isinstance(device.name, str)
            or not device.name
        ):
            raise AudioError("Select an explicit enumerated audio device; defaults are refused.")
        if direction is not None and device.direction != direction:
            raise AudioError("Select an audio device with the required direction.")
        if device not in self.devices():
            raise AudioError("The selected audio device changed or disconnected; enumerate again.")

    def query_format(self, device: AudioDevice, sample_rate: int) -> bool:
        fmt = _format(sample_rate)
        self._validate_device(device)
        prefix = "waveIn" if device.direction == "input" else "waveOut"
        try:
            result = getattr(self._api(), prefix + "Open")(
                None, device.id, ctypes.byref(fmt), 0, 0, _WAVE_FORMAT_QUERY
            )
        except (OSError, ValueError):
            raise AudioError("The audio format query failed.") from None
        if result == _WAVERR_BADFORMAT:
            return False
        if result != 0:
            raise AudioError("The audio format query failed.")
        return True

    @staticmethod
    def _check(result: int, action: str) -> None:
        if result != 0:
            raise AudioError(f"Windows audio could not {action}.")

    @staticmethod
    def _wait(header: _WaveHeader, duration: float) -> None:
        deadline = time.monotonic() + duration + _WAIT_MARGIN
        while not header.dwFlags & _WHDR_DONE:
            if time.monotonic() >= deadline:
                raise AudioError("The audio device did not finish within its time limit.")
            time.sleep(0.01)

    @staticmethod
    def _cleanup(api, prefix, handle, header, buffer, prepared) -> bool:
        """Reset, unprepare, then close; retain pointers if release fails."""
        interrupted = None

        def call(suffix, *args):
            nonlocal interrupted
            try:
                return getattr(api, prefix + suffix)(*args)
            except BaseException as error:
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    interrupted = error
                return -1

        reset_result = call("Reset", handle)
        released = not prepared
        if prepared:
            deadline = time.monotonic() + _CLEANUP_SECONDS
            for _ in range(50):
                result = call(
                    "UnprepareHeader", handle, ctypes.byref(header), ctypes.sizeof(header)
                )
                released = result == 0
                if result != _WAVERR_STILLPLAYING or time.monotonic() >= deadline:
                    break
                try:
                    time.sleep(0.01)
                except BaseException as error:
                    if isinstance(error, (KeyboardInterrupt, SystemExit)):
                        interrupted = error
                    break
        close_result = call("Close", handle)
        if not released or close_result != 0:
            _UNRELEASED_BUFFERS.append((api, handle, header, buffer))
        if interrupted is not None:
            raise interrupted
        return reset_result == 0 and released and close_result == 0

    def _stream(self, device, fmt, pcm, duration, *, recording):
        prefix = "waveIn" if recording else "waveOut"
        api = self._api()
        handle = _HANDLE()
        buffer = ctypes.create_string_buffer(pcm, len(pcm))
        header = _WaveHeader(lpData=ctypes.addressof(buffer), dwBufferLength=len(pcm))
        self._validate_device(device, "input" if recording else "output")
        opened = False
        prepared = False
        try:
            self._check(
                getattr(api, prefix + "Open")(
                    ctypes.byref(handle), device.id, ctypes.byref(fmt), 0, 0, 0
                ),
                "open the selected device",
            )
            if not handle.value:
                raise AudioError("Windows audio did not return a device handle.")
            opened = True
            # A hotplug can reorder numeric ids during Open. Check the actual
            # opened handle before handing any audio buffer to the driver.
            caps = _WaveInCaps() if recording else _WaveOutCaps()
            self._check(
                getattr(api, prefix + "GetDevCapsW")(
                    handle.value, ctypes.byref(caps), ctypes.sizeof(caps)
                ),
                "verify the opened device",
            )
            if _caps_name(caps) != device.name:
                raise AudioError(
                    "The selected audio device changed while opening; enumerate again."
                )
            self._check(
                getattr(api, prefix + "PrepareHeader")(
                    handle, ctypes.byref(header), ctypes.sizeof(header)
                ),
                "prepare its audio buffer",
            )
            prepared = True
            if recording:
                self._check(
                    api.waveInAddBuffer(handle, ctypes.byref(header), ctypes.sizeof(header)),
                    "queue the recording buffer",
                )
                self._check(api.waveInStart(handle), "start recording")
            else:
                self._check(
                    api.waveOutWrite(handle, ctypes.byref(header), ctypes.sizeof(header)),
                    "queue playback",
                )
            self._wait(header, duration)
            if recording:
                if header.dwBytesRecorded != len(pcm):
                    raise AudioError("The audio device returned an incomplete recording.")
                return buffer.raw
        except (OSError, ValueError):
            raise AudioError("Windows audio access failed.") from None
        finally:
            if opened:
                pending_error = sys.exc_info()[0] is not None
                prepared = prepared or bool(header.dwFlags & _WHDR_PREPARED)
                if not self._cleanup(api, prefix, handle, header, buffer, prepared):
                    if not pending_error:
                        raise AudioError("Windows audio could not safely release the device.")

    def record(self, device: AudioDevice, seconds: int, sample_rate: int = 16000) -> bytes:
        fmt = _format(sample_rate)
        if type(seconds) is not int or not 1 <= seconds <= _MAX_SECONDS:
            raise AudioError("Record a whole number of seconds from 1 through 10.")
        pcm = self._stream(device, fmt, bytes(sample_rate * seconds * 2), seconds, recording=True)
        output = io.BytesIO()
        with wave.open(output, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
            wav.writeframes(pcm)
        return output.getvalue()

    def play(self, device: AudioDevice, audio: bytes) -> None:
        sample_rate, pcm = validate_audio(audio)
        self._stream(
            device,
            _format(sample_rate),
            pcm,
            len(pcm) / (sample_rate * 2),
            recording=False,
        )
