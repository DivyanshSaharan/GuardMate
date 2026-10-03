from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query

from .engine import ConversationEngine
from .models import (
    Conversation,
    ConversationSummary,
    ModelStatus,
    ResidentDecision,
    StartRequest,
    TurnRequest,
)


def build_router(engine: ConversationEngine) -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.get("/agent/status", response_model=ModelStatus)
    def model_status() -> ModelStatus:
        return engine.provider.status()

    @router.post("/conversations", response_model=Conversation, status_code=201)
    def start_conversation(request: StartRequest | None = None) -> Conversation:
        return engine.start(request.courier_label if request else "")

    @router.get("/conversations", response_model=list[ConversationSummary])
    def list_conversations(
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> list[ConversationSummary]:
        return engine.list_sessions(limit)

    @router.get("/conversations/{session_id}", response_model=Conversation)
    def read_conversation(session_id: UUID) -> Conversation:
        return engine.read(str(session_id))

    @router.post("/conversations/{session_id}/turns", response_model=Conversation)
    def courier_turn(session_id: UUID, request: TurnRequest) -> Conversation:
        return engine.turn(str(session_id), request)

    @router.put("/conversations/{session_id}/resident", response_model=Conversation)
    def resident_decision(session_id: UUID, request: ResidentDecision) -> Conversation:
        return engine.decide(str(session_id), request)

    return router
