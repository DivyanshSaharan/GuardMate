"""Offline, source-bound supervised-training preparation; never contacts Tinker."""

import hashlib
import json
import random
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from ..agent.models import AgentPlan
from ..agent.prompts import SYSTEM_PROMPT
from ..agent.provider import MAX_INPUT_TOKENS, MAX_OUTPUT_TOKENS, MODEL
from ..evaluation.dataset import DatasetError, load_dataset, select_scenarios
from ..evaluation.runner import GoldPlanProvider, run_evaluation

VERSION = 1
LORA_RANK = 16
LEARNING_RATE = 1e-4
FIXED_SEED = 42
MAX_BATCHES = 30
MAX_TRAINING_MICRODOLLARS = 1_750_000
TRAINING_RATE_MILLI_MICRODOLLARS = 737
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SOURCE_PATHS = {
    name: PACKAGE_ROOT / name
    for name in (
        "agent/prompts.py",
        "agent/models.py",
        "agent/engine.py",
        "agent/dialogue.py",
        "agent/provider.py",
        "agent/store.py",
        "models.py",
        "context.py",
        "evaluation/runner.py",
        "evaluation/metrics.py",
        "evaluation/schema.py",
        "evaluation/dataset.py",
        "training/preparation.py",
        "training/ledger.py",
        "training/worker.py",
        "training/supervisor.py",
    )
}
SOURCE_PATHS["backend/scripts/train_delivery.py"] = (
    PACKAGE_ROOT.parent / "scripts/train_delivery.py"
)


class PreparationError(ValueError):
    """Preparation cannot safely produce an immutable, bounded training artifact."""


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise PreparationError("Training artifact must contain finite JSON values.") from error


def fingerprint(artifact: dict) -> str:
    """Hash every field except the artifact's own fingerprint, without mutating it."""
    if not isinstance(artifact, dict):
        raise PreparationError("Training artifact must be a JSON object.")
    return hashlib.sha256(
        _canonical({key: value for key, value in artifact.items() if key != "fingerprint"})
    ).hexdigest()


def _file_hash(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as error:
        raise PreparationError("A required training source could not be read.") from error


def _tokenizer_binding(tokenizer) -> dict:
    template = getattr(tokenizer, "chat_template", None)
    if not isinstance(template, (str, dict)) or not template:
        raise PreparationError("Tokenizer needs a nonempty chat template.")
    eos = getattr(tokenizer, "eos_token_id", None)
    if type(eos) is not int or eos < 0:
        raise PreparationError("Tokenizer needs one nonnegative integer EOS token.")
    try:
        vocab = tokenizer.get_vocab()
    except Exception as error:
        raise PreparationError("Tokenizer vocabulary could not be inspected.") from error
    if (
        not isinstance(vocab, dict)
        or not vocab
        or any(
            not isinstance(key, str) or type(value) is not int or value < 0
            for key, value in vocab.items()
        )
        or eos not in vocab.values()
    ):
        raise PreparationError("Tokenizer vocabulary or EOS binding is invalid.")
    return {
        "class": f"{type(tokenizer).__module__}.{type(tokenizer).__qualname__}",
        "chat_template_sha256": hashlib.sha256(_canonical(template)).hexdigest(),
        "vocab_sha256": hashlib.sha256(_canonical(vocab)).hexdigest(),
        "eos_token_id": eos,
    }


def _token_list(value, label: str) -> list[int]:
    if (
        not isinstance(value, list)
        or not value
        or any(type(token) is not int or token < 0 for token in value)
    ):
        raise PreparationError(f"Tokenizer produced invalid {label} tokens.")
    return value


def _fictional_messages(example: dict, nonce_maps: dict[str, dict[str, str]]) -> list[dict]:
    """Stabilize only the fictional oracle's random approval nonce, never courier text."""
    messages = [dict(message) for message in example["messages"]]
    prefix = (
        SYSTEM_PROMPT
        + "\nPLAN_SCHEMA="
        + json.dumps(AgentPlan.model_json_schema())
        + "\nRESIDENT_CONTEXT_DATA="
    )
    if (
        not messages
        or messages[0].get("role") != "system"
        or not messages[0].get("content", "").startswith(prefix)
    ):
        raise PreparationError("Oracle prompt must exactly match the production planner prefix.")
    try:
        context = json.loads(messages[0]["content"][len(prefix) :])
        if not isinstance(context, dict):
            raise ValueError
        approval = context.get("approval")
        if approval is None:
            return messages
        if not isinstance(approval, dict) or not isinstance(approval.get("id"), str):
            raise ValueError
        original_id = approval["id"]
        UUID(original_id)
    except (ValueError, TypeError) as error:
        raise PreparationError("Oracle approval context could not be normalized safely.") from error
    mapping = nonce_maps.setdefault(example["scenario_id"], {})
    if original_id not in mapping:
        mapping[original_id] = str(
            uuid5(NAMESPACE_URL, f"guardmate-training:{example['scenario_id']}:{len(mapping)}")
        )
    approval["id"] = mapping[original_id]
    # Match production json.dumps defaults/order: only the fictional nonce is replaced.
    messages[0]["content"] = prefix + json.dumps(context)
    return messages


def _prepare_datum(
    example: dict, tokenizer, eos: int, nonce_maps: dict[str, dict[str, str]]
) -> dict:
    messages = _fictional_messages(example, nonce_maps)
    if (
        len(messages) < 2
        or messages[-1].get("role") != "assistant"
        or messages[-2].get("role") != "user"
    ):
        raise PreparationError("Each training target must follow a latest courier message.")
    try:
        target = AgentPlan.model_validate_json(messages[-1]["content"])
        target_fields = target.model_dump(mode="json", exclude_defaults=True)
        target_fields.setdefault("observation", {})
        canonical_target = _canonical(target_fields)
        prefix = _token_list(
            tokenizer.apply_chat_template(
                [dict(message) for message in messages[:-1]],
                tokenize=True,
                return_dict=False,
                add_generation_prompt=True,
                enable_thinking=False,
            ),
            "prompt",
        )
        completion = _token_list(
            tokenizer.encode(canonical_target.decode("utf-8"), add_special_tokens=False),
            "completion",
        ) + [eos]
    except PreparationError:
        raise
    except Exception as error:
        raise PreparationError("A training target could not be rendered safely.") from error
    if len(prefix) > MAX_INPUT_TOKENS:
        raise PreparationError("Training prompt exceeds the production input-token bound.")
    if len(completion) > MAX_OUTPUT_TOKENS:
        raise PreparationError("Training completion exceeds the production output-token bound.")
    sequence = prefix + completion
    return {
        "scenario_id": example["scenario_id"],
        "step_index": example["step_index"],
        "input_tokens": sequence[:-1],
        "target_tokens": sequence[1:],
        "weights": [0.0] * (len(prefix) - 1) + [1.0] * len(completion),
    }


def build_preview(
    dataset_path: Path, tokenizer, *, epochs: int = 1, batch_size: int = 4, seed: int = FIXED_SEED
) -> dict:
    """Replay train references and prepare masked datums; drafts remain explicitly unreviewed.

    The artifact contains fictional train-only token data. It is an estimate, not a
    provider invoice cap, and grants no authority to allocate or train a hosted model.
    """
    if type(epochs) is not int or not 1 <= epochs <= 3:
        raise PreparationError("Epochs must be an integer between 1 and 3.")
    if type(batch_size) is not int or not 1 <= batch_size <= 16:
        raise PreparationError("Batch size must be an integer between 1 and 16.")
    if type(seed) is not int or seed != FIXED_SEED:
        raise PreparationError("This training recipe requires seed 42.")
    dataset_path = Path(dataset_path)
    source_hashes = {
        "dataset_sha256": _file_hash(dataset_path),
        **{name: _file_hash(path) for name, path in SOURCE_PATHS.items()},
    }
    tokenizer_binding = _tokenizer_binding(tokenizer)
    try:
        scenarios = select_scenarios(load_dataset(dataset_path), "train", None)
        report = run_evaluation(scenarios, GoldPlanProvider(), "oracle")
    except (DatasetError, OSError) as error:
        raise PreparationError("A valid train-only dataset is required.") from error
    if (
        report["evaluated_scenarios"] != len(scenarios)
        or report["skipped_scenario_ids"]
        or any(not result["passed"] or result["stopped"] for result in report["results"])
    ):
        raise PreparationError("Every train reference must pass the offline policy replay.")
    examples = [example for result in report["results"] for example in result["training_examples"]]
    if not examples or any(example["split"] != "train" for example in examples):
        raise PreparationError("Only nonempty train-split reference targets may be prepared.")
    nonce_maps: dict[str, dict[str, str]] = {}
    datums = [
        _prepare_datum(example, tokenizer, tokenizer_binding["eos_token_id"], nonce_maps)
        for example in examples
    ]
    schedule = []
    randomizer = random.Random(seed)
    for epoch in range(1, epochs + 1):
        indices = list(range(len(datums)))
        randomizer.shuffle(indices)
        for start in range(0, len(indices), batch_size):
            batch = indices[start : start + batch_size]
            # Count all context/completion tokens, conservatively including final EOS.
            processed_tokens = sum(len(datums[index]["input_tokens"]) + 1 for index in batch)
            reserve = (processed_tokens * TRAINING_RATE_MILLI_MICRODOLLARS + 999) // 1000
            schedule.append(
                {
                    "epoch": epoch,
                    "batch_index": len(schedule),
                    "datum_indices": batch,
                    "processed_tokens": processed_tokens,
                    "reserved_microdollars": reserve,
                }
            )
    if len(schedule) > MAX_BATCHES:
        raise PreparationError("Training schedule exceeds 30 logical batches.")
    reserved_microdollars = sum(batch["reserved_microdollars"] for batch in schedule)
    if reserved_microdollars > MAX_TRAINING_MICRODOLLARS:
        raise PreparationError("Training schedule exceeds the $1.75 estimated reservation bound.")
    # Re-read sources after replay/tokenization so mid-preparation edits are not silently bound.
    if source_hashes != {
        "dataset_sha256": _file_hash(dataset_path),
        **{name: _file_hash(path) for name, path in SOURCE_PATHS.items()},
    }:
        raise PreparationError(
            "Training sources changed during preparation; build a fresh preview."
        )
    if tokenizer_binding != _tokenizer_binding(tokenizer):
        raise PreparationError("Tokenizer changed during preparation; build a fresh preview.")
    artifact = {
        "version": VERSION,
        "model": MODEL,
        "config": {
            "rank": LORA_RANK,
            "learning_rate": LEARNING_RATE,
            "epochs": epochs,
            "batch_size": batch_size,
            "seed": seed,
        },
        "price": {
            "training_microdollars_per_1000_tokens": TRAINING_RATE_MILLI_MICRODOLLARS,
            "checked_on": "2026-10-04",
            "storage_usd_per_gb_month": 0.10,
            "note": "Published gross estimates, not account balance or an invoice ceiling.",
        },
        "source_hashes": source_hashes,
        "prompt_sha256": hashlib.sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest(),
        "plan_schema_sha256": hashlib.sha256(_canonical(AgentPlan.model_json_schema())).hexdigest(),
        "tokenizer": tokenizer_binding,
        "normalization": {
            "approval_ids": (
                "uuid5(NAMESPACE_URL, guardmate-training:{scenario_id}:{first_seen_ordinal})"
            ),
            "scope": (
                "Only fictional oracle resident-context approval.id; all other text unchanged."
            ),
        },
        "source_train_ids": [scenario.id for scenario in scenarios],
        "review_ready": all(scenario.review_status == "reviewed" for scenario in scenarios),
        "oracle": {
            "mode": "oracle",
            "reference_scenarios": len(scenarios),
            "passing_scenarios": sum(result["passed"] for result in report["results"]),
        },
        "datums": datums,
        "schedule": schedule,
        "totals": {
            "processed_tokens": sum(batch["processed_tokens"] for batch in schedule),
            "reserved_microdollars": reserved_microdollars,
            "supervised_tokens": sum(sum(datum["weights"]) for datum in datums) * epochs,
            "logical_batches": len(schedule),
        },
    }
    artifact["fingerprint"] = fingerprint(artifact)
    return artifact
