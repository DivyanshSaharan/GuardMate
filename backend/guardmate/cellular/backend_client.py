"""One-attempt, loopback-only HTTP client for the manually answered call runner.

Importing this module does not load application settings, a model SDK, or audio
drivers. Server error bodies and native exception strings never become diagnostics.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import math
import re
from collections.abc import Callable
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, ValidationError

from ..agent.models import Conversation, ModelStatus, ResidentDecision, StartRequest, TurnRequest
from ..models import Dashboard
from .call_errors import CallError
from .windows_audio import AudioError, validate_audio

MAX_RESPONSE_BYTES = 2_000_000
MAX_JSON_BYTES = 256 * 1024
_BASE_URL = re.compile(r"http://(?:127\.0\.0\.1|\[::1\])(?::[0-9]{1,5})?/?\Z")


class _ConversationResponse(Conversation):
    id: StrictStr
    courier_label: StrictStr
    revision: StrictInt = Field(ge=0)
    turn_count: StrictInt = Field(ge=0)
    dialogue_version: StrictInt = Field(ge=0)


class _ModelResponse(ModelStatus):
    configured: StrictBool
    reserved_usd: float = Field(ge=0, allow_inf_nan=False)
    budget_usd: float = Field(ge=0, allow_inf_nan=False)


class _SpeechStatus(BaseModel):
    stt_ready: StrictBool
    tts_ready: StrictBool


class _Transcript(BaseModel):
    # Return a whitelist, never private extra metadata supplied by a backend.
    model_config = ConfigDict(extra="ignore")
    text: StrictStr = Field(min_length=1, max_length=600)
    duration_ms: StrictInt = Field(gt=0, le=10_000)
    processing_ms: StrictInt = Field(ge=0)


def _fingerprint(dashboard: Dashboard) -> str:
    # Keep this identical to agent.engine.fingerprint without importing its model
    # provider or the application. The runner needs only this persisted stamp.
    value = dashboard.profile.model_dump_json() + dashboard.context.availability.value
    return hashlib.sha256(value.encode()).hexdigest()


def _session_id(value: str) -> str:
    try:
        if type(value) is not str or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError):
        raise CallError("A canonical conversation ID is required.") from None
    return value


class BackendClient:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8765",
        timeout: float = 60,
        *,
        connection_factory: Callable | None = None,
    ):
        if type(base_url) is not str or _BASE_URL.fullmatch(base_url) is None:
            raise CallError("Use an HTTP backend on literal 127.0.0.1 or [::1], with no URL path.")
        try:
            parsed = urlsplit(base_url)
            port = parsed.port if parsed.port is not None else 80
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            raise CallError("Use a valid loopback backend port.") from None
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise CallError("Use a positive finite backend timeout.")
        self._host, self._port, self._timeout = parsed.hostname, port, timeout
        self._connection_factory = connection_factory or http.client.HTTPConnection
        self._started_ids: set[str] = set()

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict | None = None,
        audio: bytes | None = None,
        speech: bool = False,
        expected_status: int = 200,
    ) -> bytes:
        mutation = method != "GET"
        limit = MAX_RESPONSE_BYTES if speech else MAX_JSON_BYTES
        body = audio
        headers = {"Accept": "audio/wav" if speech else "application/json"}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif audio is not None:
            headers["Content-Type"] = "audio/wav"
        connection = None
        try:
            # HTTPConnection uses this exact host: no proxy environment, URL
            # redirects, DNS host aliases, redirect handling, or automatic retry.
            connection = self._connection_factory(self._host, self._port, timeout=self._timeout)
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            if response.status != expected_status:
                uncertain = mutation and not 400 <= response.status < 500
                if response.status == 409:
                    message = "Backend state changed; refresh before continuing."
                elif response.status == 404:
                    message = "The backend conversation was not found."
                elif 300 <= response.status < 400:
                    message = "Backend redirects are not permitted."
                else:
                    message = "The backend rejected or could not complete the request."
                raise CallError(message, uncertain=uncertain)
            content_type = response.getheader("Content-Type", "")
            expected_type = "audio/wav" if speech else "application/json"
            if content_type.split(";", 1)[0].strip().lower() != expected_type:
                raise CallError(
                    "The backend returned an unexpected response type.", uncertain=mutation
                )
            content_length = response.getheader("Content-Length")
            if content_length is not None:
                if re.fullmatch(r"[0-9]+", content_length) is None:
                    raise CallError(
                        "The backend returned an invalid response length.", uncertain=mutation
                    )
                if int(content_length) > limit:
                    raise CallError(
                        "The backend response exceeded the safety limit.", uncertain=mutation
                    )
            data = response.read(limit + 1)
            if not data or len(data) > limit:
                raise CallError(
                    "The backend response was empty or exceeded the safety limit.",
                    uncertain=mutation,
                )
            if content_length is not None and len(data) != int(content_length):
                raise CallError("The backend response was incomplete.", uncertain=mutation)
            return data
        except CallError:
            raise
        except (OSError, http.client.HTTPException, ValueError, TypeError):
            raise CallError(
                "The backend request failed; it was not retried.", uncertain=mutation
            ) from None
        finally:
            if connection is not None:
                try:
                    connection.close()
                except (OSError, http.client.HTTPException):
                    pass

    @staticmethod
    def _parse(model: type[BaseModel], data: bytes, *, uncertain: bool = False):
        try:
            return model.model_validate_json(data, strict=True)
        except (ValidationError, ValueError, RecursionError):
            raise CallError(
                "The backend returned invalid response data.", uncertain=uncertain
            ) from None

    def _dashboard(self) -> Dashboard:
        return self._parse(Dashboard, self._request("GET", "/api/dashboard"))

    def inspect(self) -> dict:
        dashboard = self._dashboard()
        model = self._parse(_ModelResponse, self._request("GET", "/api/agent/status"))
        speech = self._parse(_SpeechStatus, self._request("GET", "/api/speech/status"))
        return {
            "setup_complete": dashboard.context.setup_complete,
            "delivery_mode_active": dashboard.context.delivery_mode_active,
            "model_configured": model.configured,
            "stt_ready": speech.stt_ready,
            "tts_ready": speech.tts_ready,
            "model": model.model,
            "reserved_usd": model.reserved_usd,
            "budget_usd": model.budget_usd,
        }

    def _conversation(self, data: bytes, *, expected_id: str | None = None, uncertain=False):
        session = self._parse(_ConversationResponse, data, uncertain=uncertain)
        try:
            _session_id(session.id)
        except CallError:
            raise CallError(
                "The backend returned an invalid conversation ID.", uncertain=uncertain
            ) from None
        if expected_id is not None and session.id != expected_id:
            raise CallError("The backend returned a different conversation.", uncertain=uncertain)
        return Conversation.model_validate_json(session.model_dump_json())

    def _validate_session(self, session: Conversation) -> Conversation:
        if not isinstance(session, Conversation):
            raise CallError("A validated conversation is required.")
        try:
            data = session.model_dump_json()
        except (ValueError, TypeError):
            raise CallError("A validated conversation is required.") from None
        return self._conversation(data.encode("utf-8"))

    @staticmethod
    def _latest_reply(session: Conversation) -> tuple[int, str]:
        if not session.messages or session.messages[-1].role != "assistant":
            raise CallError("Only the latest saved assistant reply can be spoken.")
        text = session.messages[-1].content
        if not text.strip():
            raise CallError("The latest assistant reply is empty.")
        return len(session.messages) - 1, text

    def start(self, label: str) -> Conversation:
        try:
            request = StartRequest.model_validate({"courier_label": label}, strict=True)
        except ValidationError:
            raise CallError("Use a courier label of at most 80 characters.") from None
        session = self._conversation(
            self._request(
                "POST", "/api/conversations", payload=request.model_dump(), expected_status=201
            ),
            uncertain=True,
        )
        if (
            UUID(session.id).version != 4
            or session.id in self._started_ids
            or session.courier_label != request.courier_label
            or session.status != "active"
            or session.revision != 0
            or session.turn_count != 0
            or len(session.messages) != 1
            or session.messages[0].role != "assistant"
        ):
            raise CallError("The backend did not return a fresh conversation.", uncertain=True)
        self._started_ids.add(session.id)
        return session

    def read(self, session_id: str) -> Conversation:
        session_id = _session_id(session_id)
        return self._conversation(
            self._request("GET", f"/api/conversations/{session_id}"), expected_id=session_id
        )

    def send(
        self, session: Conversation, text: str, *, expected_context: str | None = None
    ) -> Conversation:
        session = self._validate_session(session)
        if session.status not in ("active", "awaiting_approval"):
            raise CallError("The conversation is paused or ended.")
        request_data = {"revision": session.revision, "text": text}
        if expected_context is not None:
            request_data["expected_context"] = expected_context
        try:
            request = TurnRequest.model_validate(request_data, strict=True)
        except ValidationError:
            raise CallError("Use a valid courier message and captured context hash.") from None
        if not request.text.strip():
            raise CallError("Use a nonempty courier message.")
        payload = request.model_dump(exclude_none=True)
        if expected_context is not None and payload.get("expected_context") != expected_context:
            raise CallError("The captured context binding could not be validated.")
        updated = self._conversation(
            self._request("POST", f"/api/conversations/{session.id}/turns", payload=payload),
            expected_id=session.id,
            uncertain=True,
        )
        previous = len(session.messages)
        if (
            updated.revision != session.revision + 1
            or updated.turn_count != session.turn_count + 1
            or updated.courier_label != session.courier_label
            or len(updated.messages) != previous + 2
            or updated.messages[:previous] != session.messages
            or updated.messages[previous].role != "courier"
            or updated.messages[previous].content != request.text.strip()
            or updated.messages[-1].role != "assistant"
            or not updated.messages[-1].content.strip()
        ):
            raise CallError(
                "The backend turn could not be verified; do not resend it.", uncertain=True
            )
        return updated

    def ensure_fresh(self, session: Conversation) -> None:
        session = self._validate_session(session)
        index, text = self._latest_reply(session)
        current = self.read(session.id)
        current_index, current_text = self._latest_reply(current)
        dashboard = self._dashboard()
        if (
            current.revision != session.revision
            or current.status != session.status
            or current_index != index
            or current_text != text
            or current.reply_context_fingerprint != session.reply_context_fingerprint
            or not dashboard.context.delivery_mode_active
            or session.reply_context_fingerprint != _fingerprint(dashboard)
        ):
            raise CallError("The saved reply or delivery context changed; do not play it.")

    def context_token(self, session: Conversation) -> str:
        """Bind a new capture to its exact conversation and current resident plan.

        Unlike speech freshness, fresh courier input may follow a resident
        decision or settings update. It must not silently target another turn.
        """
        session = self._validate_session(session)
        current = self.read(session.id)
        dashboard = self._dashboard()
        if (
            current.revision != session.revision
            or current.status != session.status
            or len(current.messages) != len(session.messages)
            or not current.messages
            or current.messages[-1] != session.messages[-1]
            or not dashboard.context.setup_complete
            or not dashboard.context.delivery_mode_active
        ):
            raise CallError(
                "The conversation or delivery plan changed; discard and recapture the draft."
            )
        return _fingerprint(dashboard)

    def synthesize(self, session: Conversation) -> bytes:
        session = self._validate_session(session)
        index, _ = self._latest_reply(session)
        self.ensure_fresh(session)
        audio = self._request(
            "POST",
            f"/api/conversations/{session.id}/speech",
            payload={"revision": session.revision, "message_index": index},
            speech=True,
        )
        self.ensure_fresh(session)
        return audio

    def transcribe(self, wav: bytes) -> dict[str, str | int]:
        try:
            rate, _ = validate_audio(wav)
        except AudioError:
            raise CallError("Use complete PCM16 mono 16 kHz audio of at most 10 seconds.") from None
        if rate != 16000:
            raise CallError("Use complete PCM16 mono 16 kHz audio of at most 10 seconds.")
        transcript = self._parse(
            _Transcript,
            self._request("POST", "/api/speech/transcribe", audio=wav),
            uncertain=True,
        )
        if not transcript.text.strip():
            raise CallError("The backend returned an empty transcript.", uncertain=True)
        return transcript.model_dump()

    def end(self, session: Conversation) -> Conversation:
        session = self._validate_session(session)
        if session.status == "ended":
            raise CallError("The conversation is already ended.")
        request = ResidentDecision(decision="end", revision=session.revision)
        updated = self._conversation(
            self._request(
                "PUT",
                f"/api/conversations/{session.id}/resident",
                payload=request.model_dump(exclude_none=True),
            ),
            expected_id=session.id,
            uncertain=True,
        )
        if (
            updated.revision != session.revision + 1
            or updated.turn_count != session.turn_count
            or updated.status != "ended"
            or updated.authorized_location is not None
            or updated.courier_label != session.courier_label
            or len(updated.messages) != len(session.messages) + 1
            or updated.messages[:-1] != session.messages
            or updated.messages[-1].role != "resident"
        ):
            raise CallError(
                "The backend end request could not be verified; do not repeat it.", uncertain=True
            )
        return updated
