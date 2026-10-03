from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from ..agent.engine import ConversationEngine, fingerprint
from .service import MAX_WAV_BYTES, SpeechError, SpeechService


class SpeakRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0, strict=True)
    message_index: int = Field(ge=0, strict=True)


def build_speech_router(engine: ConversationEngine, service: SpeechService) -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.get("/speech/status")
    def status():
        return service.status()

    @router.post("/speech/transcribe")
    async def transcribe(request: Request):
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "audio/wav":
            raise HTTPException(415, "Send a PCM16 mono 16 kHz WAV recording.")
        length = request.headers.get("content-length")
        if length is not None:
            try:
                if int(length) < 0:
                    raise ValueError
                if int(length) > MAX_WAV_BYTES:
                    raise HTTPException(413, "Record at most 30 seconds of audio.")
            except ValueError as error:
                raise HTTPException(400, "Invalid audio request length.") from error
        audio = bytearray()
        async for chunk in request.stream():
            if len(audio) + len(chunk) > MAX_WAV_BYTES:
                raise HTTPException(413, "Record at most 30 seconds of audio.")
            audio.extend(chunk)
        try:
            return await run_in_threadpool(service.transcribe, bytes(audio))
        except SpeechError as error:
            raise HTTPException(error.status_code, error.message) from error

    def latest_reply(session_id: str, request: SpeakRequest) -> str:
        # read() re-checks approval expiry and resident context before selecting the saved reply.
        session = engine.read(session_id)
        if session.revision != request.revision:
            raise HTTPException(409, "Conversation changed. Refresh it before playing a reply.")
        if (
            request.message_index >= len(session.messages)
            or request.message_index != len(session.messages) - 1
            or session.messages[request.message_index].role != "assistant"
        ):
            raise HTTPException(422, "Only the latest saved GuardMate reply can be spoken.")
        dashboard = engine.dashboard()
        if (
            not dashboard.context.delivery_mode_active
            or session.reply_context_fingerprint != fingerprint(dashboard)
        ):
            raise HTTPException(
                409,
                "This saved reply is from an older delivery context. "
                "Send a fresh text turn or start a new role-play before using speech.",
            )
        return session.messages[request.message_index].content

    @router.post("/conversations/{session_id}/speech")
    def speak(session_id: UUID, request: SpeakRequest):
        text = latest_reply(str(session_id), request)
        try:
            audio = service.synthesize(text)
        except SpeechError as error:
            raise HTTPException(error.status_code, error.message) from error
        # No model lock is held during synthesis; discard any reply made stale in the meantime.
        if latest_reply(str(session_id), request) != text:
            raise HTTPException(409, "Conversation changed. Refresh it before playing a reply.")
        return Response(audio, media_type="audio/wav", headers={"Cache-Control": "no-store"})

    return router
