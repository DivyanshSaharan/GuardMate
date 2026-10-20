"""Authenticated, supervised local phone setup and operator-controlled call turns."""

import sys
from collections import deque
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from secrets import compare_digest
from threading import Event, Lock
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from ..agent.engine import ConversationEngine, fingerprint
from ..agent.models import Conversation, ResidentDecision, TurnRequest
from ..cellular.call_audio import reply_audio
from ..cellular.call_errors import CallError
from ..cellular.call_runner import ManualCallRunner, require_ready
from ..cellular.windows_audio import PairedAudioDevice
from ..speech.service import SpeechService
from .audio import DesktopAudio
from .phone_link import (
    PhoneLinkError,
    detect_phone_link,
    open_phone_link,
    open_phone_link_install,
)


class Endpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: StrictInt = Field(ge=0, lt=0xFFFFFFFF)
    name: str = Field(min_length=1, max_length=256)
    direction: str = Field(pattern="^(input|output)$")
    host_api: str = Field(pattern="^Windows WDM-KS$")

    def pin(self) -> PairedAudioDevice:
        return PairedAudioDevice(**self.model_dump())


class StartCall(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phone_name: str = Field(min_length=3, max_length=120)
    input: Endpoint
    output: Endpoint
    label: str = Field(min_length=1, max_length=80)
    seconds: StrictInt = Field(default=5, ge=1, le=10)
    max_turns: StrictInt = Field(default=5, ge=1, le=10)
    call_answered: StrictBool
    consenting_call: StrictBool
    hosted_text_consent: StrictBool
    exclusive_audio_risk: StrictBool
    phone_endpoints_confirmed: StrictBool


class ReviewedTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=600)


class Playback(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repeat: StrictBool = False


class EngineBackend:
    """Reuse the existing call runner against the very same local policy engine."""

    def __init__(self, engine: ConversationEngine, speech: SpeechService):
        self.engine, self.speech = engine, speech

    def inspect(self) -> dict:
        dashboard = self.engine.dashboard()
        speech = self.speech.status()
        return {
            "setup_complete": dashboard.context.setup_complete,
            "delivery_mode_active": dashboard.context.delivery_mode_active,
            "model_configured": self.engine.provider.status().configured,
            "stt_ready": speech["stt_ready"],
            "tts_ready": speech["tts_ready"],
        }

    def start(self, label: str) -> Conversation:
        return self.engine.start(label)

    def read(self, session_id: str) -> Conversation:
        return self.engine.read(session_id)

    def context_token(self, session: Conversation) -> str:
        current = self.engine.read(session.id)
        if current.revision != session.revision:
            raise CallError("The conversation changed. Refresh before recording another turn.")
        return fingerprint(self.engine.dashboard())

    def transcribe(self, audio: bytes) -> dict:
        return self.speech.transcribe(audio)

    def send(self, session: Conversation, text: str, *, expected_context=None) -> Conversation:
        try:
            return self.engine.turn(
                session.id,
                TurnRequest(
                    text=text, revision=session.revision, expected_context=expected_context
                ),
            )
        except HTTPException:
            raise
        except Exception:
            raise CallError(
                "The submitted turn has an uncertain result. Handle the call manually; "
                "do not submit it again.",
                uncertain=True,
            ) from None

    def ensure_fresh(self, session: Conversation) -> None:
        current = self.engine.read(session.id)
        dashboard = self.engine.dashboard()
        if (
            current.revision != session.revision
            or not dashboard.context.delivery_mode_active
            or current.reply_context_fingerprint != fingerprint(dashboard)
        ):
            raise CallError("The saved reply is stale. Refresh and handle the call manually.")

    def synthesize(self, session: Conversation) -> bytes:
        self.ensure_fresh(session)
        if not session.messages or session.messages[-1].role != "assistant":
            raise CallError("Only the latest saved assistant reply can be played.")
        return self.speech.synthesize(session.messages[-1].content)

    def end(self, session: Conversation) -> Conversation:
        return self.engine.decide(
            session.id, ResidentDecision(decision="end", revision=session.revision)
        )


class ReviewedCallRunner(ManualCallRunner):
    """Keep native transmission behind a separate, explicit GUI playback action."""

    def _speak(self, session: Conversation, *, repeat: bool = False) -> None:
        self.emit({"event": "reply_ready", "status": session.status})

    def play_latest(self, repeat: bool = False) -> None:
        with self._operation():
            if self.draft is not None:
                raise CallError("Send or discard the captured transcript before reply playback.")
            current = self._read_owned()
            self.session = current
            ManualCallRunner._speak(self, current, repeat=repeat)


class DesktopCalls:
    def __init__(
        self,
        engine: ConversationEngine,
        speech: SpeechService,
        root: Path,
        *,
        audio=None,
        phone_detector=None,
    ):
        self.backend = EngineBackend(engine, speech)
        self.audio = audio if audio is not None else DesktopAudio(root)
        self.phone_detector = phone_detector or detect_phone_link
        self.runner = None
        self._lock = Lock()
        self._cancelled = Event()
        self._busy = False
        self._stopping = False
        self.error = ""
        self.events = deque(maxlen=40)

    def emit(self, event: dict) -> None:
        # A bounded event-name list is sufficient for progress; do not log caller text.
        self.events.append({"event": event.get("event", "status")})

    def snapshot(self) -> dict:
        runner = self.runner
        return {
            "busy": self._busy,
            "stopping": self._stopping,
            "stopped": runner.stopped if runner else True,
            "uncertain": runner.uncertain if runner else False,
            "draft": runner.draft if runner else None,
            "conversation": runner.session.model_dump(mode="json")
            if runner and runner.session
            else None,
            "model_attempts": runner.model_attempts if runner else 0,
            "max_turns": runner.max_turns if runner else 0,
            "events": list(self.events),
            "error": self.error,
            "physical_call_ended": False,
        }

    def _admit(self) -> None:
        if self._cancelled.is_set():
            raise CallError(
                "Operator stopped the call assistant. Physical hangup is manual.", uncertain=True
            )

    @contextmanager
    def operation(self):
        if not self._lock.acquire(blocking=False):
            raise HTTPException(409, "A call operation is pending. Wait or stop the assistant.")
        self._busy = True
        self.error = ""
        try:
            yield
        except CallError as error:
            self.error = str(error)
            raise HTTPException(409, str(error)) from None
        except HTTPException:
            raise
        except Exception:
            self.error = "The call operation failed. Handle the call manually; no automatic retry."
            if self.runner is not None:
                self.runner.uncertain = True
            raise HTTPException(503, self.error) from None
        finally:
            if self._cancelled.is_set() and self.runner is not None:
                try:
                    self.runner.stop()
                except Exception:
                    self.error = "Assistant stopped; review/end the saved conversation manually."
                self._stopping = False
            self._busy = False
            self._lock.release()

    def start(self, request: StartCall) -> dict:
        with self.operation():
            if self.runner is not None and not self.runner.stopped:
                raise HTTPException(409, "Stop the current call assistant before starting another.")
            if not all(
                (
                    request.call_answered,
                    request.consenting_call,
                    request.hosted_text_consent,
                    request.exclusive_audio_risk,
                    request.phone_endpoints_confirmed,
                )
            ):
                raise HTTPException(
                    422,
                    "Confirm the answered call, both-party consent, hosted text "
                    "processing, exclusive audio risk and selected phone endpoints.",
                )
            if not request.label.strip():
                raise HTTPException(422, "Enter a label for this new call session.")
            self._cancelled.clear()
            self.audio.reset()
            if not self.phone_detector():
                raise HTTPException(409, "Install and pair Phone Link before starting a call.")
            require_ready(self.backend.inspect())
            bound = self.audio.bind(request.phone_name, request.input.pin(), request.output.pin())
            bound.validate(request.input.pin().audio_device(), request.output.pin().audio_device())
            self._stopping = False
            self.events.clear()
            self.runner = ReviewedCallRunner(
                self.backend,
                bound,
                request.input.pin().audio_device(),
                request.output.pin().audio_device(),
                convert_reply=reply_audio,
                seconds=request.seconds,
                max_turns=request.max_turns,
                hosted_consent=True,
                emit=self.emit,
                admission_check=self._admit,
                endpoint_check=bound.validate,
            )
            self.runner.start(request.label.strip())
        return self.snapshot()

    def command(self, name: str, *, text: str | None = None, repeat=False) -> dict:
        with self.operation():
            if self.runner is None:
                raise HTTPException(409, "Start an explicitly confirmed, answered call first.")
            self._admit()
            if name == "capture":
                self.runner.listen()
            elif name == "review":
                self.runner.edit(text)
            elif name == "send":
                self.runner.edit(text)
                self.runner.send()
            elif name == "discard":
                self.runner.discard()
            elif name == "play":
                self.runner.play_latest(repeat=repeat)
            elif name == "refresh":
                self.runner.refresh()
            else:
                raise HTTPException(404)
        return self.snapshot()

    def stop(self) -> dict:
        self._cancelled.set()
        self._stopping = True
        self.audio.cancel()
        if self._lock.acquire(blocking=False):
            try:
                if self.runner is not None:
                    try:
                        self.runner.stop()
                    except Exception:
                        self.error = (
                            "Assistant stopped; review/end the saved conversation manually."
                        )
                self._stopping = False
            finally:
                self._lock.release()
        return self.snapshot()


def build_desktop_router(
    engine: ConversationEngine, speech: SpeechService, root: Path, *, token: str, controller=None
) -> APIRouter:
    if not isinstance(token, str) or len(token) < 24:
        raise ValueError("A private local desktop session token is required.")

    def guard(request: Request):
        try:
            parsed = urlsplit("http://" + request.headers.get("host", ""))
            host = parsed.hostname
            _port = parsed.port
        except ValueError:
            raise HTTPException(403, "Use the desktop launcher's local address.") from None
        if (
            host not in {"127.0.0.1", "::1"}
            or request.client is None
            or request.client.host not in {"127.0.0.1", "::1"}
        ):
            raise HTTPException(403, "Desktop controls are available only on literal loopback.")
        supplied = request.headers.get("authorization", "").removeprefix("Bearer ")
        cookie = request.cookies.get("guardmate_local", "")
        if not (
            compare_digest(supplied.encode(), token.encode())
            or compare_digest(cookie.encode(), token.encode())
        ):
            raise HTTPException(401, "Open GuardMate from the desktop launcher.")
        if request.method not in {"GET", "HEAD"}:
            origin = request.headers.get("origin")
            if origin is not None and origin != f"{request.url.scheme}://{request.url.netloc}":
                raise HTTPException(403, "Cross-origin desktop controls are refused.")
            if request.headers.get("sec-fetch-site") == "cross-site":
                raise HTTPException(403, "Cross-site desktop controls are refused.")

    calls = controller if controller is not None else DesktopCalls(engine, speech, root)
    router = APIRouter(prefix="/api/desktop", dependencies=[Depends(guard)])
    router.desktop_calls = calls

    @router.get("/status")
    def status():
        installed = False
        message = "Pair Phone Link and allow calls in Microsoft's UI."
        if sys.platform == "win32":
            try:
                installed = calls.phone_detector()
            except PhoneLinkError as error:
                message = str(error)
        return {
            "desktop": True,
            "windows": sys.platform == "win32",
            "phone_link_installed": installed,
            "phone_link_message": message,
            "physical_call_state_verified": False,
            **calls.backend.inspect(),
        }

    @router.post("/phone-link/open")
    def phone_open():
        try:
            open_phone_link()
        except PhoneLinkError as error:
            raise HTTPException(503, str(error)) from None
        return {"opened": True, "pairing_is_manual": True}

    @router.post("/phone-link/install")
    def phone_install():
        try:
            open_phone_link_install()
        except PhoneLinkError as error:
            raise HTTPException(503, str(error)) from None
        return {"opened": True, "installation_is_manual": True}

    @router.post("/devices/scan")
    def devices():
        with calls.operation():
            return {
                "devices": [asdict(device) for device in calls.audio.devices()],
                "audio_stream_started": False,
                "physical_call_state_verified": False,
            }

    @router.get("/call")
    def call_status():
        return calls.snapshot()

    @router.post("/call/start")
    def start(request: StartCall):
        return calls.start(request)

    @router.post("/call/capture")
    def capture():
        return calls.command("capture")

    @router.post("/call/review")
    def review(request: ReviewedTurn):
        return calls.command("review", text=request.text)

    @router.post("/call/send")
    def send(request: ReviewedTurn):
        return calls.command("send", text=request.text)

    @router.post("/call/discard")
    def discard():
        return calls.command("discard")

    @router.post("/call/play")
    def play(request: Playback):
        return calls.command("play", repeat=request.repeat)

    @router.post("/call/refresh")
    def refresh():
        return calls.command("refresh")

    @router.post("/call/stop")
    def stop():
        return calls.stop()

    return router
