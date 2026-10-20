import hashlib
import json
import math
from threading import BoundedSemaphore
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from ..agent.models import AgentPlan
from ..agent.provider import MAX_INPUT_TOKENS, MAX_OUTPUT_TOKENS, MODEL
from .store import CloudError


class PlannerMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: str = Field(pattern=r"^(system|user|assistant)$")
    content: str = Field(min_length=1, max_length=64_000)


class InferenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    messages: list[PlannerMessage] = Field(min_length=1, max_length=100)

    def provider_messages(self):
        messages = [message.model_dump() for message in self.messages]
        if len(json.dumps(messages, ensure_ascii=False).encode()) > 256_000:
            raise CloudError("request_too_large", 413)
        return messages


class AccountBudgetStore:
    """A single provider attempt; never accesses the historical global budget store."""

    def __init__(self, store, account_id, request_id, payload_digest):
        self.store, self.account_id = store, account_id
        self.request_id, self.payload_digest = request_id, payload_digest
        self.claimed = False
        self.error = None
        self.budget_microdollars = store.account(account_id)["allowance"]
        if type(self.budget_microdollars) is not int or self.budget_microdollars < 0:
            raise CloudError("account_unavailable", 401)

    def reserved_microdollars(self):
        return self.store.account(self.account_id)["reserved"]

    def reserve(self, amount, limit):
        maximum = math.ceil(MAX_INPUT_TOKENS * 0.33 + MAX_OUTPUT_TOKENS * 1.005)
        if (
            self.claimed
            or limit != self.budget_microdollars
            or type(amount) is not int
            or not 0 < amount <= maximum
        ):
            self.error = CloudError("invalid_dispatch")
            raise self.error
        try:
            self.store.reserve_and_claim(
                self.account_id, self.request_id, self.payload_digest, amount
            )
        except CloudError as error:
            self.error = error
            raise
        self.claimed = True
        return True


def default_provider_factory(ledger):
    from ..agent.provider import TinkerProvider

    return TinkerProvider(
        ledger, budget_microdollars=ledger.budget_microdollars, budget_label="account allowance"
    )


class CloudService:
    def __init__(self, store, provider_factory=None, *, max_concurrent=1):
        self.store = store
        self.provider_factory = provider_factory or default_provider_factory
        self._slots = BoundedSemaphore(max_concurrent)

    def infer(self, account_id, request: InferenceRequest):
        if not self._slots.acquire(blocking=False):
            raise CloudError("inference_busy", 429)
        try:
            return self._infer(account_id, request)
        finally:
            self._slots.release()

    def _infer(self, account_id, request: InferenceRequest):
        messages = request.provider_messages()
        payload = {
            "model": MODEL,
            "max_input_tokens": MAX_INPUT_TOKENS,
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            "messages": messages,
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        request_id = str(request.request_id)
        existing = self.store.request_record(account_id, request_id)
        if existing is not None:
            self.store.reject_duplicate(existing, digest)
        adapter = AccountBudgetStore(self.store, account_id, request_id, digest)
        if self.store.account_status(account_id)["remaining_microdollars"] == 0:
            raise CloudError("account_budget_exhausted", 402)
        try:
            plan = AgentPlan.model_validate(self.provider_factory(adapter).generate(messages))
            if not adapter.claimed:
                raise CloudError("provider_did_not_reserve", 503)
            self.store.finish_request(account_id, request_id, completed=True)
        except Exception:
            if adapter.claimed:
                try:
                    self.store.finish_request(account_id, request_id, completed=False)
                except Exception:
                    # The committed reservation survives a failed/uncertain status update.
                    pass
            if adapter.error is not None:
                raise adapter.error from None
            raise CloudError("inference_unavailable", 503) from None
        return {
            "request_id": request_id,
            "plan": plan.model_dump(mode="json"),
            **self.store.account_status(account_id),
        }
