import importlib.util
import math
import os
import re
from threading import Lock
from typing import Protocol

from pydantic import ValidationError

from .models import AgentPlan, ModelStatus
from .store import ConversationStore

MODEL = "Qwen/Qwen3.5-4B"
MAX_OUTPUT_TOKENS = 512
MAX_INPUT_TOKENS = 12_000
BUDGET_MICRODOLLARS = 250_000
_SAMPLER_CHECKPOINT = re.compile(
    # Native model IDs are session-id:train:sequence, not HTTP hostnames/ports.
    r"tinker://(?=[A-Za-z0-9_:-]{1,200}/)[A-Za-z0-9][A-Za-z0-9_-]*"
    r"(?::[A-Za-z0-9][A-Za-z0-9_-]*)*/sampler_weights/"
    r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}"
)


def validate_sampler_checkpoint(value: str) -> str:
    """Accept only a canonical sampler export, never full training state or a URL alias."""
    if not isinstance(value, str) or _SAMPLER_CHECKPOINT.fullmatch(value) is None or ".." in value:
        raise ValueError(
            "Checkpoint must be a canonical tinker://model-id/sampler_weights/checkpoint-name path."
        )
    return value


class ModelUnavailable(Exception):
    pass


class PlanProvider(Protocol):
    def generate(self, messages: list[dict[str, str]]) -> AgentPlan: ...

    def status(self) -> ModelStatus: ...


class TinkerProvider:
    def __init__(self, store: ConversationStore, *, sampler_checkpoint: str | None = None):
        self.store = store
        self._sampler_checkpoint = (
            validate_sampler_checkpoint(sampler_checkpoint)
            if sampler_checkpoint is not None
            else None
        )
        self._checkpoint_verified = False
        self._client = None
        self._tokenizer = None
        self._lock = Lock()
        self.validation_errors: list[dict] = []

    @property
    def sampler_checkpoint(self) -> str | None:
        return self._sampler_checkpoint

    def status(self) -> ModelStatus:
        installed = all(
            importlib.util.find_spec(package) for package in ("tinker", "transformers", "jinja2")
        )
        configured = installed and bool(os.environ.get("TINKER_API_KEY"))
        checkpoint_message = ""
        if self.sampler_checkpoint is not None:
            verification = (
                "base model verified" if self._checkpoint_verified else "not yet verified"
            )
            checkpoint_message = (
                f" Sampler checkpoint configured: {self.sampler_checkpoint} ({verification})."
            )
        return ModelStatus(
            configured=configured,
            model=MODEL,
            provider="Tinker (hosted open-weight model)",
            message=(
                "Key configured for text role-play. Conversations go to Tinker; "
                "calls are not connected."
                if configured
                else "Install requirements-ai.txt and set TINKER_API_KEY in your local .env."
            )
            + checkpoint_message,
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
                    retry_config = RetryConfig(enable_retry_logic=False, progress_timeout=45)
                    if self.sampler_checkpoint is None:
                        self._client = service.create_sampling_client(
                            base_model=MODEL, retry_config=retry_config
                        )
                    else:
                        self._client = service.create_sampling_client(
                            model_path=self.sampler_checkpoint, retry_config=retry_config
                        )
                if self.sampler_checkpoint is not None and not self._checkpoint_verified:
                    # Public SDK metadata lookup; never tokenize/reserve/sample a different model.
                    # SamplingClient has get_base_model(), not TrainingClient.get_info().
                    if self._client.get_base_model() != MODEL:
                        raise ModelUnavailable(
                            "Checkpoint base model could not be verified as Qwen/Qwen3.5-4B. "
                            "No model sample was sent."
                        )
                    self._checkpoint_verified = True
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
