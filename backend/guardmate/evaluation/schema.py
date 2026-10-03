from typing import Annotated, Literal, Self

from pydantic import ConfigDict, Field, model_validator

from ..agent.models import AgentPlan, StrictModel
from ..models import Availability, ResidentProfile

Split = Literal["train", "validation", "test"]
Slug = Annotated[str, Field(pattern=r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$", max_length=100)]
Action = Literal[
    "clarify",
    "get_delivery_context",
    "wait_for_courier",
    "get_handoff_options",
    "get_approval_status",
    "request_owner_approval",
    "request_takeover",
    "record_call_outcome",
    "handoff_blocked",
    "handoff_already_given",
    "approval_expired",
    "resident_approve",
    "resident_decline",
    "resident_takeover",
    "resident_end",
    "model_unavailable",
    "no_event",
]


class DatasetProfile(ResidentProfile):
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def require_fictional_setup(self) -> Self:
        if not all((self.resident_name, self.pg_name, self.guard_location)):
            raise ValueError("Each scenario needs a complete fictional resident profile.")
        return self


class FactExpectation(StrictModel):
    prepaid: bool | None = None
    guard_available: bool | None = None
    needs_otp: bool = False
    needs_signature: bool = False
    expensive: bool = False


class Expected(StrictModel):
    actions: list[Action] = Field(min_length=1)
    status: Literal["active", "awaiting_approval", "needs_resident", "ended"] | None = None
    facts: FactExpectation | None = None
    authorized_location: Literal["none", "guard_room", "approved_alternative"] | None = None
    approval_status: (
        Literal["none", "pending", "approved", "declined", "expired", "consumed"] | None
    ) = None
    outcome: Literal["none", "delivered", "returned", "could_not_deliver"] | None = None
    pending_question: Literal["prepaid", "guard_available", "alternative_location"] | None = None
    forbidden_handoff: bool = False


class CourierStep(StrictModel):
    kind: Literal["courier"]
    text: str = Field(min_length=1, max_length=600)
    gold_plan: AgentPlan
    expect: Expected

    @model_validator(mode="after")
    def grounded_gold(self) -> Self:
        if not self.text.strip():
            raise ValueError("Courier text cannot be blank.")
        observation = self.gold_plan.observation
        updates = observation.model_dump(exclude={"evidence"}, exclude_none=True)
        if (
            any(updates.values())
            or observation.prepaid is False
            or observation.guard_available is False
        ):
            if not observation.evidence or observation.evidence not in self.text:
                raise ValueError("Gold observation evidence must be an exact latest-courier quote.")
        if self.gold_plan.action == "request_approval":
            location = self.gold_plan.proposed_location.strip()
            if len(location) < 3 or location.casefold() not in self.text.casefold():
                raise ValueError("Gold alternative location must be quoted from this courier turn.")
        return self


class ResidentStep(StrictModel):
    kind: Literal["resident"]
    decision: Literal["approve", "decline", "takeover", "end"]
    expect: Expected


class AdvanceStep(StrictModel):
    kind: Literal["advance"]
    seconds: int = Field(ge=1, le=3600, strict=True)
    expect: Expected


class ContextStep(StrictModel):
    kind: Literal["context"]
    availability: Availability | None = None
    guard_location: str | None = Field(default=None, min_length=1, max_length=180)
    delivery_enabled: bool | None = None
    expect: Expected

    @model_validator(mode="after")
    def require_change(self) -> Self:
        if all(
            value is None
            for value in (self.availability, self.guard_location, self.delivery_enabled)
        ):
            raise ValueError("Context step must change at least one setting.")
        return self


Step = Annotated[
    CourierStep | ResidentStep | AdvanceStep | ContextStep, Field(discriminator="kind")
]


class Scenario(StrictModel):
    id: Slug
    group_id: Slug
    split: Split
    category: Slug
    source: Literal["synthetic_authored", "user_reported_seed_synthetic_expansion"]
    review_status: Literal["draft", "reviewed"]
    rationale: str = Field(min_length=10, max_length=2000)
    profile: DatasetProfile
    availability: Availability
    steps: list[Step] = Field(min_length=2, max_length=30)

    @model_validator(mode="after")
    def bounded_conversation(self) -> Self:
        couriers = sum(isinstance(step, CourierStep) for step in self.steps)
        if not 1 <= couriers <= 20:
            raise ValueError("Scenario needs 1–20 courier turns.")
        if not isinstance(self.steps[0], CourierStep):
            raise ValueError("A scenario must start with a courier turn.")
        return self
