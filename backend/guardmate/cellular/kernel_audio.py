"""Optional, explicit Windows WDM-KS audio for a short local routing proof.

WDM-KS can lock other users out of the endpoint while a stream is active. This
is a research probe, not a shared-mode call adapter. PortAudio's WDM-KS backend
does not implement blocking streams, so capture and playback use callbacks.
The application requests mono PCM16 at exactly 8/16 kHz without resampling or
fallback. PortAudio may still adapt the native sample format/channel layout.
Use the CLI's disposable worker for a hard bound against a hung native driver.
"""

from __future__ import annotations

import importlib
import io
import os
import sys
import threading
import wave

from .windows_audio import AudioDevice, AudioError, validate_audio

HOST_API = "Windows WDM-KS"
_UNSUPPORTED_FORMAT_CODES = {-9998, -9997, -9994, -9993}
_UNRELEASED_STREAMS: list[tuple] = []


def _rate(sample_rate):
    if type(sample_rate) is not int or sample_rate not in (8000, 16000):
        raise AudioError("Use an 8000 or 16000 Hz sample rate.")
    return sample_rate


def _portaudio_code(api, error):
    if isinstance(error, api.PortAudioError) and len(error.args) > 1 and type(error.args[1]) is int:
        return error.args[1]
    return None


class WindowsKernelAudio:
    """Explicit WDM-KS endpoints; importing this module loads no audio library."""

    host_api = HOST_API

    def __init__(self, *, sounddevice=None):
        self._sounddevice = sounddevice

    def _api(self):
        if self._sounddevice is None:
            if os.name != "nt":
                raise AudioError("Windows WDM-KS audio is available only on Windows.")
            try:
                self._sounddevice = importlib.import_module("sounddevice")
            except Exception:
                raise AudioError(
                    "The optional sounddevice audio dependency is unavailable."
                ) from None
        return self._sounddevice

    def devices(self) -> list[AudioDevice]:
        api = self._api()
        try:
            hosts = api.query_hostapis()
            identifiers = {index for index, host in enumerate(hosts) if host["name"] == HOST_API}
            devices = []
            for identifier, device in enumerate(api.query_devices()):
                if device["hostapi"] not in identifiers or not device["name"]:
                    continue
                for direction in ("input", "output"):
                    if device[f"max_{direction}_channels"] >= 1:
                        devices.append(AudioDevice(identifier, device["name"], direction))
            return devices
        except Exception:
            raise AudioError("Windows WDM-KS device enumeration failed.") from None

    def _validate(self, device, direction=None):
        if (
            not isinstance(device, AudioDevice)
            or type(device.id) is not int
            or not 0 <= device.id < 0xFFFFFFFF
            or device.direction not in ("input", "output")
            or not isinstance(device.name, str)
            or not device.name
        ):
            raise AudioError("Select an explicit WDM-KS device; defaults are refused.")
        if direction is not None and device.direction != direction:
            raise AudioError("Select an audio device with the required direction.")
        if device not in self.devices():
            raise AudioError("The selected WDM-KS device changed or disconnected; enumerate again.")

    def query_format(self, device: AudioDevice, sample_rate: int) -> bool:
        """Ask PortAudio about the application format; never start a stream."""
        _rate(sample_rate)
        self._validate(device)
        api = self._api()
        try:
            check = (
                api.check_input_settings
                if device.direction == "input"
                else api.check_output_settings
            )
            check(device=device.id, channels=1, dtype="int16", samplerate=sample_rate)
        except Exception as error:
            code = _portaudio_code(api, error)
            if code in _UNSUPPORTED_FORMAT_CODES:
                return False
            if code == -9985:
                raise AudioError(
                    "The WDM-KS endpoint is unavailable or occupied; no retry."
                ) from None
            raise AudioError("The WDM-KS audio format query failed.") from None
        return True

    @staticmethod
    def _cleanup(stream, resources, completed):
        failed = False
        interrupted = None
        if not completed:
            try:
                stream.abort(ignore_errors=False)
            except BaseException as error:
                failed = True
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    interrupted = error
        try:
            stream.close(ignore_errors=False)
        except BaseException as error:
            failed = True
            _UNRELEASED_STREAMS.append((stream, resources))
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                interrupted = error
        if interrupted is not None:
            raise interrupted
        return not failed

    def _stream(self, device, sample_rate, pcm, *, recording):
        api = self._api()
        direction = "input" if recording else "output"
        target = len(pcm)
        storage = bytearray(target) if recording else pcm
        data = memoryview(storage)
        blocksize = sample_rate // 50
        silence = memoryview(bytes(blocksize * 2))
        finished = threading.Event()
        state = {"position": 0, "failed": None}

        def callback(buffer, frames, _clock, status):
            try:
                view = memoryview(buffer).cast("B")
                if not recording:
                    # Even a callback that aborts must fill its whole output block.
                    for start in range(0, len(view), len(silence)):
                        count = min(len(silence), len(view) - start)
                        view[start : start + count] = silence[:count]
                if (
                    type(frames) is not int
                    or not 1 <= frames <= sample_rate
                    or len(view) != frames * 2
                    or status
                ):
                    state["failed"] = "The WDM-KS callback reported incomplete audio."
                    raise api.CallbackAbort
                position = state["position"]
                count = min(len(view), target - position)
                if recording:
                    data[position : position + count] = view[:count]
                else:
                    view[:count] = data[position : position + count]
                state["position"] += count
                if state["position"] == target:
                    raise api.CallbackStop
            except (api.CallbackStop, api.CallbackAbort):
                raise
            except BaseException:
                state["failed"] = "The WDM-KS audio callback failed."
                raise api.CallbackAbort from None

        self._validate(device, direction)
        stream = None
        completed = False
        resources = (callback, data, silence, finished, state)
        try:
            constructor = api.RawInputStream if recording else api.RawOutputStream
            stream = constructor(
                device=device.id,
                channels=1,
                dtype="int16",
                samplerate=sample_rate,
                blocksize=blocksize,
                callback=callback,
                finished_callback=finished.set,
                clip_off=True,
                dither_off=True,
            )
            # Recheck the pin and the returned application format before Start.
            self._validate(device, direction)
            if (
                stream.device != device.id
                or stream.channels != 1
                or stream.dtype != "int16"
                or stream.samplerate != sample_rate
            ):
                raise AudioError(
                    "The WDM-KS stream did not retain the requested device and format."
                )
            stream.start()
            if not finished.wait(target / (sample_rate * 2) + 2.0):
                raise AudioError("The WDM-KS device did not finish within its time limit.")
            if state["failed"] is not None:
                raise AudioError(state["failed"])
            if state["position"] != target:
                raise AudioError("The WDM-KS device returned incomplete audio.")
            completed = True
            return bytes(storage) if recording else None
        except AudioError:
            raise
        except Exception as error:
            if _portaudio_code(api, error) == -9985:
                raise AudioError(
                    "The WDM-KS endpoint is unavailable or occupied; no retry."
                ) from None
            raise AudioError("Windows WDM-KS audio access failed.") from None
        finally:
            if stream is not None:
                pending_error = sys.exc_info()[0] is not None
                if not self._cleanup(stream, resources, completed) and not pending_error:
                    raise AudioError("Windows WDM-KS audio could not safely release the device.")

    def record(self, device: AudioDevice, seconds: int, sample_rate: int = 16000) -> bytes:
        _rate(sample_rate)
        if type(seconds) is not int or not 1 <= seconds <= 10:
            raise AudioError("Record a whole number of seconds from 1 through 10.")
        pcm = self._stream(device, sample_rate, bytes(sample_rate * seconds * 2), recording=True)
        output = io.BytesIO()
        with wave.open(output, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
            wav.writeframes(pcm)
        recording = output.getvalue()
        validate_audio(recording)
        return recording

    def play(self, device: AudioDevice, audio: bytes) -> None:
        sample_rate, pcm = validate_audio(audio)
        self._stream(device, sample_rate, pcm, recording=False)

    def play_reply(self, device: AudioDevice, audio: bytes) -> None:
        """Play one complete bounded 16 kHz reply, without chunk boundaries."""
        sample_rate, pcm = validate_audio(audio, max_seconds=30)
        if sample_rate != 16000:
            raise AudioError("Manual call replies must be 16000 Hz PCM16 mono WAV.")
        self._stream(device, sample_rate, pcm, recording=False)

    def listen_for_utterance(self, device: AudioDevice, *, config=None, on_armed=None) -> bytes:
        """One input stream, armed after real frames arrive, closed before returning."""
        from .utterance import NoSpeech, UtteranceConfig, UtteranceEndpointer, UtteranceTooLong

        config = config if config is not None else UtteranceConfig()
        endpointer = UtteranceEndpointer(config)
        api = self._api()
        self._validate(device, "input")
        armed = threading.Event()
        finished = threading.Event()
        state = {"failed": None}

        def callback(buffer, frames, _clock, status):
            try:
                view = memoryview(buffer).cast("B")
                if (
                    type(frames) is not int
                    or not 1 <= frames <= 16000
                    or len(view) != frames * 2
                    or status
                ):
                    state["failed"] = "The WDM-KS callback reported incomplete audio."
                    raise api.CallbackAbort
                armed.set()
                if endpointer.feed(view) is not None:
                    raise api.CallbackStop
            except (api.CallbackStop, api.CallbackAbort):
                raise
            except BaseException:
                state["failed"] = "The WDM-KS audio callback failed."
                raise api.CallbackAbort from None

        stream = None
        completed = False
        resources = (callback, armed, finished, state, endpointer)
        try:
            stream = api.RawInputStream(
                device=device.id,
                channels=1,
                dtype="int16",
                samplerate=16000,
                blocksize=320,
                callback=callback,
                finished_callback=finished.set,
                clip_off=True,
                dither_off=True,
            )
            self._validate(device, "input")
            if (
                stream.device != device.id
                or stream.channels != 1
                or stream.dtype != "int16"
                or stream.samplerate != 16000
            ):
                raise AudioError(
                    "The WDM-KS stream did not retain the requested device and format."
                )
            stream.start()
            if not armed.wait(3.0):
                raise AudioError("The WDM-KS listener did not receive valid input frames.")
            if on_armed is not None:
                on_armed()
            if not finished.wait(config.idle_timeout_seconds + config.max_utterance_seconds + 2.0):
                raise AudioError("The WDM-KS device did not finish within its time limit.")
            if state["failed"] is not None:
                raise AudioError(state["failed"])
            if endpointer.status not in ("utterance", "no_speech", "too_long"):
                raise AudioError("The WDM-KS device returned incomplete audio.")
            completed = True
        except AudioError:
            raise
        except Exception as error:
            if _portaudio_code(api, error) == -9985:
                raise AudioError(
                    "The WDM-KS endpoint is unavailable or occupied; no retry."
                ) from None
            raise AudioError("Windows WDM-KS audio access failed.") from None
        finally:
            if stream is not None:
                pending_error = sys.exc_info()[0] is not None
                if not self._cleanup(stream, resources, completed) and not pending_error:
                    raise AudioError("Windows WDM-KS audio could not safely release the device.")
        if endpointer.status == "no_speech":
            raise NoSpeech("No sustained speech arrived within the listening window.")
        if endpointer.status == "too_long":
            raise UtteranceTooLong("Speech did not finish within the complete utterance budget.")
        output = io.BytesIO()
        with wave.open(output, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(endpointer.pcm)
        recording = output.getvalue()
        validate_audio(recording)
        return recording
