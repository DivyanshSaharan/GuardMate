"""Operator-controlled, half-duplex turns on an already answered consenting call.

This coordinator owns one fresh conversation, never answers/hangs up a phone,
never approves a handoff, and never retries a model or audio operation.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from threading import Lock
from typing import Protocol

from ..agent.models import Conversation
from .call_errors import CallError
from .windows_audio import AudioDevice


class Backend(Protocol):
    def inspect(self) -> dict: ...
    def start(self, label: str) -> Conversation: ...
    def read(self, session_id: str) -> Conversation: ...
    def context_token(self, session: Conversation) -> str: ...
    def transcribe(self, audio: bytes) -> dict: ...
    def send(
        self, session: Conversation, text: str, *, expected_context: str | None = None
    ) -> Conversation: ...
    def synthesize(self, session: Conversation) -> bytes: ...
    def ensure_fresh(self, session: Conversation) -> None: ...
    def end(self, session: Conversation) -> Conversation: ...


class Audio(Protocol):
    def record(self, device: AudioDevice, seconds: int = 5) -> bytes: ...
    def play(self, device: AudioDevice, audio: bytes) -> None: ...


def phone_device(devices: list[AudioDevice], identifier: int, direction: str) -> AudioDevice:
    if type(identifier) is not int or identifier < 0 or direction not in ("input", "output"):
        raise CallError("Select explicit phone input and output IDs from a fresh inspection.")
    matches = [
        item
        for item in devices
        if item.id == identifier
        and item.direction == direction
        and "vivo T2x 5G".casefold() in item.name.casefold()
    ]
    if len(matches) != 1:
        raise CallError("The selected vivo phone endpoint is missing or ambiguous. Inspect again.")
    return matches[0]


def require_ready(status: dict) -> None:
    for name in (
        "setup_complete",
        "delivery_mode_active",
        "model_configured",
        "stt_ready",
        "tts_ready",
    ):
        if status.get(name) is not True:
            raise CallError(
                "Save resident settings, enable a delivery window, and configure Qwen/local speech "
                "before starting a manual call session."
            )


class ManualCallRunner:
    def __init__(
        self,
        backend: Backend,
        audio: Audio,
        input_device: AudioDevice,
        output_device: AudioDevice,
        *,
        convert_reply: Callable[[bytes], bytes],
        seconds: int = 5,
        max_turns: int = 5,
        hosted_consent: bool = False,
        emit: Callable[[dict], None] | None = None,
    ):
        if type(seconds) is not int or not 1 <= seconds <= 10:
            raise CallError("Choose a capture length from 1 to 10 seconds.")
        if type(max_turns) is not int or not 1 <= max_turns <= 10:
            raise CallError("Choose a manual test limit from 1 to 10 model turns.")
        if input_device.direction != "input" or output_device.direction != "output":
            raise CallError("Select a phone input and a phone output, not default devices.")
        for device in (input_device, output_device):
            if "vivo T2x 5G".casefold() not in device.name.casefold():
                raise CallError("This manual experiment is limited to the tested vivo T2x 5G.")
        self.backend, self.audio = backend, audio
        self.input_device, self.output_device = input_device, output_device
        self.convert_reply = convert_reply
        self.seconds, self.max_turns = seconds, max_turns
        self.hosted_consent = hosted_consent
        self.emit = emit or (lambda event: None)
        self.session: Conversation | None = None
        self.draft: str | None = None
        self._draft_context: str | None = None
        self.model_attempts = 0
        self.stopped = False
        self.uncertain = False
        self._busy = Lock()
        self._played: tuple[str, int, int] | None = None

    @contextmanager
    def _operation(self):
        if not self._busy.acquire(blocking=False):
            raise CallError("A call operation is still pending. Do not send or record again.")
        try:
            if self.stopped or self.uncertain:
                raise CallError(
                    "This runner has stopped or has an uncertain result. Handle the call manually."
                )
            yield
        except CallError as error:
            if error.uncertain:
                self.uncertain = True
                self.emit({"event": "uncertain", "automatic_retry": False})
            raise
        finally:
            self._busy.release()

    def _session(self) -> Conversation:
        if self.session is None:
            raise CallError("Start a fresh manual call session first.")
        return self.session

    @staticmethod
    def _active(session: Conversation) -> None:
        if session.status != "active":
            raise CallError(
                "GuardMate is paused or ended. Use resident controls, refresh, "
                "or handle the call manually."
            )

    def _read_owned(self) -> Conversation:
        previous = self._session()
        current = self.backend.read(previous.id)
        if current.id != previous.id:
            raise CallError("The backend returned another call session. Handle the call manually.")
        return current

    def start(self, label: str) -> Conversation:
        with self._operation():
            if self.session is not None:
                raise CallError(
                    "This runner already owns a session. Start a new process for a new call."
                )
            if not self.hosted_consent:
                raise CallError(
                    "Hosted transcript/context consent is required before starting this runner."
                )
            require_ready(self.backend.inspect())
            session = self.backend.start(label)
            self.session = session
            self.emit(
                {
                    "event": "session_started",
                    "session_id": session.id,
                    "courier_label": session.courier_label,
                }
            )
            self._active(session)
            self._speak(session)
            return session

    def listen(self) -> str:
        with self._operation():
            if self.draft is not None:
                raise CallError(
                    "Review/send or discard the current transcript before recording another turn."
                )
            if self.model_attempts >= self.max_turns:
                raise CallError("The manual model-turn limit has been reached. Stop this test.")
            session = self._read_owned()
            self._active(session)
            require_ready(self.backend.inspect())
            context = self.backend.context_token(session)
            self.session = session
            self.emit({"event": "recording", "seconds": self.seconds})
            recorded = self.audio.record(self.input_device, self.seconds)
            self.emit({"event": "local_transcription", "model_request_sent": False})
            result = self.backend.transcribe(recorded)
            # Keep the raw capture out of runner history and never pass it to send().
            del recorded
            text = result.get("text")
            if not isinstance(text, str) or not text.strip() or len(text) > 600:
                raise CallError(
                    "The local transcript is empty or invalid. No model request was sent."
                )
            if self.backend.context_token(session) != context:
                raise CallError(
                    "Delivery context changed during capture. Discard it and record a fresh turn."
                )
            self.draft, self._draft_context = text.strip(), context
            self.emit(
                {"event": "transcript_review", "text": self.draft, "model_request_sent": False}
            )
            return self.draft

    def edit(self, text: str) -> None:
        with self._operation():
            if self.draft is None:
                raise CallError("There is no captured transcript to edit.")
            if not isinstance(text, str) or not text.strip() or len(text) > 600:
                raise CallError("Use a non-empty transcript of at most 600 characters.")
            # Editing never changes the capture's revision/context or grants approval.
            self.draft = text.strip()
            self.emit(
                {"event": "transcript_review", "text": self.draft, "model_request_sent": False}
            )

    def discard(self) -> None:
        with self._operation():
            self.draft, self._draft_context = None, None
            self.emit({"event": "transcript_discarded", "model_request_sent": False})

    def send(self) -> Conversation:
        with self._operation():
            if not self.hosted_consent:
                raise CallError("Hosted transcript/context consent is required before sending.")
            session = self._session()
            self._active(session)
            if self.draft is None or self._draft_context is None:
                raise CallError("Capture and review a transcript before explicitly sending it.")
            if self.model_attempts >= self.max_turns:
                raise CallError(
                    "The manual model-turn limit has been reached. No model request was sent."
                )
            if self.backend.context_token(session) != self._draft_context:
                raise CallError(
                    "Delivery context changed. Discard the transcript and record a fresh turn."
                )
            self.model_attempts += 1
            self.emit(
                {"event": "model_pending", "attempt": self.model_attempts, "limit": self.max_turns}
            )
            updated = self.backend.send(session, self.draft, expected_context=self._draft_context)
            if updated.id != session.id or updated.revision <= session.revision:
                raise CallError(
                    "The submitted turn has an unverifiable result. Do not send it again.",
                    uncertain=True,
                )
            self.session = updated
            self.draft, self._draft_context = None, None
            # A newly checked pause/outcome notification can be spoken once, but
            # never permits another listen/send while the session is paused.
            self._speak(updated)
            return updated

    def _speak(self, session: Conversation, *, repeat: bool = False) -> None:
        if not session.messages or session.messages[-1].role != "assistant":
            raise CallError("Only the latest saved GuardMate reply can be spoken.")
        key = (session.id, session.revision, len(session.messages) - 1)
        if self._played == key and not repeat:
            raise CallError("This reply has already been played. An explicit repeat is required.")
        text = session.messages[-1].content
        self.emit({"event": "checked_reply", "text": text, "status": session.status})
        spoken = self.backend.synthesize(session)
        pcm = self.convert_reply(spoken)
        # Revalidate AFTER synthesis/resampling and immediately BEFORE native audio.
        self.backend.ensure_fresh(session)
        self.emit({"event": "playback", "listen_active": False})
        self.audio.play(self.output_device, pcm)
        self._played = key
        self.emit({"event": "playback_completed", "caller_heard_verified": False})
        if session.status != "active":
            self.emit({"event": "paused", "status": session.status, "manual_call_transfer": False})

    def speak(self, *, repeat: bool = False) -> None:
        with self._operation():
            if self.draft is not None:
                raise CallError(
                    "Send or discard the pending transcript before playing another reply."
                )
            current = self._read_owned()
            self._active(current)
            self.session = current
            self._speak(current, repeat=repeat)

    def refresh(self) -> Conversation:
        with self._operation():
            self.session = self._read_owned()
            # Never move a short answer to a different question/revision on refresh.
            self.draft, self._draft_context = None, None
            self.emit(
                {"event": "refreshed", "status": self.session.status, "transcript_discarded": True}
            )
            return self.session

    def stop(self) -> None:
        # Explicit stop may end the known session even after an uncertain job;
        # it never resends a model turn, searches for a session or hangs up a phone.
        if not self._busy.acquire(blocking=False):
            raise CallError("A call operation is pending. Stop it before ending the session.")
        try:
            if self.stopped:
                return
            if self.session is not None:
                current = self._read_owned()
                if current.status != "ended":
                    current = self.backend.end(current)
                    if current.id != self.session.id or current.status != "ended":
                        raise CallError(
                            "Session end was not confirmed. Check the saved session manually.",
                            uncertain=True,
                        )
                self.session = current
            self.draft, self._draft_context = None, None
            self.stopped = True
            self.emit({"event": "stopped", "physical_call_ended": False})
        finally:
            self._busy.release()
