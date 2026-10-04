"""Opt-in automatic, half-duplex conversation after a manually answered call.

This coordinator automates admission of local transcripts, not answering a phone
or judging ASR confidence. It never retries, approves a parcel handoff, rewrites
caller words, or ends a saved conversation merely because it has paused.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock
from time import monotonic
from typing import Protocol

from ..agent.models import Conversation
from .call_errors import CallError
from .call_runner import ManualCallRunner
from .utterance import NoSpeech, UtteranceTooLong
from .windows_audio import AudioDevice


class UtteranceListener(Protocol):
    def listen(
        self, device: AudioDevice, *, on_armed: Callable[[], None] | None = None
    ) -> bytes: ...


@dataclass(frozen=True)
class AutomaticCallResult:
    status: str
    reason: str
    session_id: str | None
    model_attempts: int


class _AdmissionDeadline(CallError):
    pass


def transcript_problem(session: Conversation, text: str) -> str | None:
    """Detect only explicit warning signs; absence is not ASR confidence.

    Normalize a copy for inspection, never the text submitted to the backend.
    Semantic safety, parcel facts and resident authority remain in the engine.
    """
    if type(text) is not str or not text.strip() or len(text) > 600:
        return "invalid_transcript"
    normalized = " ".join(text.casefold().split())
    if not any(character.isalpha() for character in normalized):
        return "invalid_transcript"
    if re.search(
        r"[\[(]\s*(?:blank[_ ]audio|silence|music|inaudible|unintelligible|noise)\s*[\])]",
        normalized,
    ) or normalized.strip(" .!?") in {
        "blank_audio",
        "blank audio",
        "silence",
        "music",
        "inaudible",
        "unintelligible",
    }:
        return "inaudible_transcript"
    if normalized.strip(" .!?") in {
        "thank you for watching",
        "thanks for watching",
        "please subscribe",
        "subscribe to my channel",
        "thanks for watching this video",
    }:
        return "obvious_hallucination"
    if session.messages and session.messages[-1].role == "assistant":
        reply = " ".join(session.messages[-1].content.casefold().split())
        if normalized == reply:
            return "assistant_echo"
    tokens = re.findall(r"\w+", normalized)
    if len(tokens) >= 3:
        for width in range(1, len(tokens) // 3 + 1):
            if len(tokens) % width == 0 and tokens == tokens[:width] * (len(tokens) // width):
                return "repeated_transcript"
    bare = normalized.strip(" .!?,")
    if bare in {"ok", "okay", "sure", "right", "correct", "hmm", "uh huh", "uh-huh"}:
        return "ambiguous_confirmation"
    if bare in {"yes", "no", "yeah", "yep", "nope"} and session.pending_question not in {
        "prepaid",
        "guard_available",
    }:
        return "noncontextual_confirmation"
    return None


class AutomaticCallCoordinator:
    def __init__(
        self,
        runner: ManualCallRunner,
        *,
        audio: UtteranceListener,
        automatic_submission_consent: bool,
        max_seconds: int = 180,
        emit: Callable[[dict], None] | None = None,
        clock: Callable[[], float] = monotonic,
    ):
        if automatic_submission_consent is not True:
            raise CallError(
                "Explicit consent to automatically submit unreviewed transcripts is required."
            )
        if type(max_seconds) is not int or not 1 <= max_seconds <= 600:
            raise CallError("Choose an automatic admission window from 1 to 600 seconds.")
        if not callable(getattr(audio, "listen", None)) or not callable(clock):
            raise CallError("An utterance listener and monotonic clock are required.")
        self.runner, self.audio = runner, audio
        self.automatic_submission_consent = automatic_submission_consent
        self.max_seconds = max_seconds
        self.emit = emit or runner.emit
        self._clock = clock
        self._deadline: float | None = None
        self._started = False
        self._busy = Lock()

    def _now(self) -> float:
        value = self._clock()
        if type(value) not in (int, float) or not math.isfinite(value):
            raise CallError("The automatic admission clock is unavailable.")
        return value

    def _check_deadline(self) -> None:
        if self._deadline is not None and self._now() >= self._deadline:
            raise _AdmissionDeadline("The automatic operation admission window has ended.")

    def _result(self, status: str, reason: str) -> AutomaticCallResult:
        session = self.runner.session
        result = AutomaticCallResult(
            status=status,
            reason=reason,
            session_id=session.id if session else None,
            model_attempts=self.runner.model_attempts,
        )
        self.emit(
            {
                "event": "automatic_stopped",
                "status": result.status,
                "reason": result.reason,
                "session_id": result.session_id,
                "model_attempts": result.model_attempts,
                "saved_session_ended_by_coordinator": False,
                "physical_call_ended": False,
            }
        )
        return result

    def _armed(self) -> None:
        self.emit(
            {
                "event": "utterance_armed",
                "caller_may_speak": True,
                "physical_call_state_verified": False,
            }
        )

    def run(self, label: str) -> AutomaticCallResult:
        if not self._busy.acquire(blocking=False):
            raise CallError("An automatic call run is already pending.")
        previous_admission = self.runner.admission_check

        def admit():
            self._check_deadline()
            if previous_admission is not None:
                previous_admission()

        try:
            if self.automatic_submission_consent is not True:
                raise CallError("Automatic transcript submission consent is required.")
            if self._started or self.runner.session is not None:
                raise CallError(
                    "Start a fresh runner for each automatic call; no restart or retry."
                )
            self._started = True
            self._deadline = self._now() + self.max_seconds
            self.runner.admission_check = admit
            self.emit(
                {
                    "event": "automatic_submission_disclosure",
                    "operator_review_each_turn": False,
                    "unreviewed_transcripts_uploaded_automatically": True,
                    "raw_audio_upload_to_hosted_model": False,
                    "hosted_data": "Recognized text, history and resident delivery context.",
                    "asr_confidence_available": False,
                    "automatic_retry": False,
                    "max_turns": self.runner.max_turns,
                    "admission_seconds": self.max_seconds,
                    "in_flight_barge_in": False,
                }
            )
            current = self.runner.start(label)
            while True:
                if current.status != "active":
                    return self._result(
                        "ended" if current.status == "ended" else "paused", current.status
                    )
                if self.runner.model_attempts >= self.runner.max_turns:
                    return self._result("limit", "model_turn_limit")
                self._check_deadline()
                text = self.runner.listen(
                    capture=lambda: self.audio.listen(
                        self.runner.input_device, on_armed=self._armed
                    )
                )
                problem = transcript_problem(self.runner.session, text)
                if problem is not None:
                    # Keep the original rejected draft only in runner RAM, not
                    # backend history. Never rewrite or rebind it to a new turn.
                    self.emit(
                        {
                            "event": "automatic_transcript_paused",
                            "reason": problem,
                            "model_request_sent": False,
                            "asr_confidence_available": False,
                        }
                    )
                    return self._result("paused", problem)
                self._check_deadline()
                self.emit(
                    {
                        "event": "automatic_submission",
                        "operator_reviewed": False,
                        "automatic_retry": False,
                    }
                )
                current = self.runner.send()
        except _AdmissionDeadline:
            return self._result("deadline", "admission_deadline")
        except NoSpeech as error:
            if error.uncertain:
                raise
            return self._result("paused", "no_speech")
        except UtteranceTooLong as error:
            if error.uncertain:
                raise
            return self._result("paused", "utterance_too_long")
        finally:
            self.runner.admission_check = previous_admission
            self._busy.release()
            # No runner.stop(): it issues resident=end and declines pending
            # approvals. Pauses/errors must retain the known saved conversation.
