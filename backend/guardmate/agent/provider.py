import importlib.util
import math
import os
from threading import Lock
from typing import Protocol

from pydantic import ValidationError

from .models import AgentPlan, ModelStatus
from .store import ConversationStore

MODEL = "Qwen/Qwen3.5-4B"
MAX_OUTPUT_TOKENS = 512
MAX_INPUT_TOKENS = 12_000
BUDGET_MICRODOLLARS = 250_000


class ModelUnavailable(Exception):
    pass


class PlanProvider(Protocol):
    def generate(self, messages: list[dict[str, str]]) -> AgentPlan: ...

    def status(self) -> ModelStatus: ...


class TinkerProvider:
    def __init__(self, store: ConversationStore):
        self.store = store
        self._client = None
        self._tokenizer = None
        self._lock = Lock()
        self.validation_errors: list[dict] = []

    def status(self) -> ModelStatus:
        installed = all(
            importlib.util.find_spec(package) for package in ("tinker", "transformers", "jinja2")
        )
        configured = installed and bool(os.environ.get("TINKER_API_KEY"))
        return ModelStatus(
            configured=configured,
            model=MODEL,
            provider="Tinker (hosted open-weight model)",
            message=(
                "Key configured for text role-play. Conversations go to Tinker; "
                "calls are not connected."
                if configured
                else "Install requirements-ai.txt and set TINKER_API_KEY in your local .env."
            ),
            reserved_usd=self.store.reserved_microdollars() / 1_000_000,
            budget_usd=BUDGET_MICRODOLLARS / 1_000_000,
        )

    def generate(self, messages: list[dict[str, str]]) -> AgentPlan:
        if not self.status().configured:
            raise ModelUnavailable("Qwen is not configured. No model request was sent.")
        with self._lock:
            try:
                import tinker
                from tinker import types
                from tinker.lib.retry_handler import RetryConfig

                if self._client is None:
                    service = tinker.ServiceClient()
                    self._client = service.create_sampling_client(
                        base_model=MODEL,
                        retry_config=RetryConfig(enable_retry_logic=False, progress_timeout=45),
                    )
                if self._tokenizer is None:
                    self._tokenizer = self._client.get_tokenizer()
                tokens = self._tokenizer.apply_chat_template(
                    messages,
                    tokenize=True,
                    return_dict=False,
                    add_generation_prompt=True,
                    enable_thinking=False,
                )
                if len(tokens) > MAX_INPUT_TOKENS:
                    raise ModelUnavailable("Conversation is too long. End it and start another.")
                prompt = types.ModelInput.from_ints(tokens)
                # Published Qwen3.5-4B uncached input/output rates; estimate, NOT a billing balance.
                reservation = math.ceil(len(tokens) * 0.33 + MAX_OUTPUT_TOKENS * 1.005)
                if not self.store.reserve(reservation, BUDGET_MICRODOLLARS):
                    raise ModelUnavailable("The $0.25 conversation-test budget has been reached.")
                result = self._client.sample(
                    prompt=prompt,
                    num_samples=1,
                    sampling_params=types.SamplingParams(
                        max_tokens=MAX_OUTPUT_TOKENS,
                        temperature=0.2,
                        stop=["<|im_end|>"],
                    ),
                ).result(timeout=45)
                text = self._tokenizer.decode(result.sequences[0].tokens, skip_special_tokens=True)
                return AgentPlan.model_validate_json(text.strip())
            except ModelUnavailable:
                raise
            except ValidationError as error:
                self.validation_errors = [
                    {"field": str(item["loc"]), "type": item["type"]}
                    for item in error.errors(include_input=False, include_url=False)
                ]
                raise ModelUnavailable(
                    "Qwen returned an invalid structured plan. No handoff was authorized."
                ) from None
            except Exception as error:
                # Never return/log SDK exceptions: they can contain credentials or prompt data.
                raise ModelUnavailable(
                    f"Qwen failed ({type(error).__name__}). No handoff was authorized; "
                    "resident help is needed."
                ) from None
