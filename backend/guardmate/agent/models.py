from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Observation(StrictModel):
    prepaid: bool | None = None
    guard_available: bool | None = None
    needs_otp: bool = False
    needs_signature: bool = False
    expensive: bool = False
    evidence: str = Field(default="", max_length=600)


class AgentPlan(StrictModel):
    action: Literal[
        "answer",
        "clarify",
        "wait",
        "handoff",
        "request_approval",
        "request_takeover",
        "record_outcome",
    ]
    topic: Literal["availability", "directions", "identity", "approval_status"] = "availability"
    question: Literal["prepaid", "guard_available", "alternative_location", "delivery_question"] = (
        "delivery_question"
    )
    target: Literal["guard_room", "approved_alternative"] = "guard_room"
    proposed_location: str = Field(default="", max_length=180)
    reason: str = Field(default="", max_length=240)
    outcome: Literal["delivered", "returned", "could_not_deliver"] = "could_not_deliver"
    observation: Observation = Field(default_factory=Observation)


class ParcelFacts(BaseModel):
    prepaid: bool | None = None
    guard_available: bool | None = None
    needs_otp: bool = False
    needs_signature: bool = False
    expensive: bool = False


class Approval(BaseModel):
    id: str
    location: str
    reason: str
    status: Literal["pending", "approved", "declined", "expired", "consumed"] = "pending"
    expires_at: datetime
    profile_fingerprint: str


class Message(BaseModel):
    role: Literal["courier", "assistant", "resident"]
    content: str
    at: datetime


class AgentEvent(BaseModel):
    action: str
    detail: str
    at: datetime
    latency_ms: int | None = None
    model_action: str | None = None
    model_question: str | None = None
    model_observation: dict[str, bool | None] | None = None


class Conversation(BaseModel):
    id: str
    status: Literal["active", "awaiting_approval", "needs_resident", "ended"] = "active"
    facts: ParcelFacts = Field(default_factory=ParcelFacts)
    messages: list[Message] = Field(default_factory=list)
    events: list[AgentEvent] = Field(default_factory=list)
    approval: Approval | None = None
    authorized_location: str | None = None
    courier_reported_outcome: str | None = None
    created_at: datetime
    turn_count: int = 0
    revision: int = 0
    pending_question: Literal["prepaid", "guard_available", "alternative_location"] | None = None
    dialogue_version: int = 0


class TurnRequest(StrictModel):
    text: str = Field(min_length=1, max_length=600)
    revision: int = Field(ge=0)


class ResidentDecision(StrictModel):
    decision: Literal["approve", "decline", "takeover", "end"]
    approval_id: str | None = None
    revision: int = Field(ge=0)


class ModelStatus(BaseModel):
    configured: bool
    model: str
    provider: str
    message: str
    reserved_usd: float
    budget_usd: float
    voice_connected: Literal[False] = False
