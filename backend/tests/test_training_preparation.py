"""Training previews are offline references, not tuned-model performance evidence."""

import copy
import hashlib
import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest
from guardmate.training import preparation
from guardmate.training.preparation import PreparationError, build_preview, fingerprint


class FakeTokenizer:
    chat_template = "fictional non-thinking chat template"
    eos_token_id = 99

    def __init__(self, *, prompt_length=3, completion_length=2):
        self.prompt_length = prompt_length
        self.completion_length = completion_length
        self.prompt_calls = []
        self.completions = []

    def get_vocab(self):
        return {"prefix": 11, "completion": 21, "EOS": 99}

    def apply_chat_template(self, messages, **kwargs):
        self.prompt_calls.append((copy.deepcopy(messages), dict(kwargs)))
        return [11] * self.prompt_length

    def encode(self, text, **kwargs):
        assert kwargs == {"add_special_tokens": False}
        self.completions.append(text)
        return [21] * self.completion_length


class ContentSensitiveTokenizer(FakeTokenizer):
    def get_vocab(self):
        return {str(value): value for value in range(256)}

    def apply_chat_template(self, messages, **kwargs):
        self.prompt_calls.append((copy.deepcopy(messages), dict(kwargs)))
        return [11, *hashlib.sha256(json.dumps(messages).encode()).digest()]


def seed(scenario_id="train-one", *, split="train", reviewed=False, first_text=None):
    return {
        "id": scenario_id,
        "group_id": scenario_id,
        "split": split,
        "category": "dialogue-memory",
        "source": "synthetic_authored",
        "review_status": "reviewed" if reviewed else "draft",
        "rationale": "Fictional offline preparation regression example.",
        "profile": {
            "resident_name": "Fictional resident",
            "pg_name": "Fictional PG",
            "guard_location": "the guard room",
        },
        "availability": "at_office",
        "steps": [
            {
                "kind": "courier",
                "text": first_text or "I have an order to deliver",
                "gold_plan": {"action": "clarify", "question": "prepaid", "observation": {}},
                "expect": {
                    "actions": ["clarify"],
                    "status": "active",
                    "pending_question": "prepaid",
                    "authorized_location": "none",
                },
            },
            {
                "kind": "courier",
                "text": "yes",
                "gold_plan": {
                    "action": "clarify",
                    "question": "guard_available",
                    "observation": {"prepaid": True, "evidence": "yes"},
                },
                "expect": {
                    "actions": ["clarify"],
                    "status": "active",
                    "pending_question": "guard_available",
                    "facts": {"prepaid": True, "guard_available": None},
                    "authorized_location": "none",
                },
            },
        ],
    }


def write_dataset(path, records):
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    return path


@pytest.fixture
def sources(tmp_path, monkeypatch):
    # Isolate sources so other tasks can edit their owned files during these tests.
    names = list(preparation.SOURCE_PATHS)
    paths = {}
    for index, name in enumerate(names):
        path = tmp_path / f"source-{index}.py"
        path.write_text(f"# fictional source {name}\n", encoding="utf-8")
        paths[name] = path
    monkeypatch.setattr(preparation, "SOURCE_PATHS", paths)
    return paths


@pytest.fixture
def dataset(tmp_path, sources):
    return write_dataset(tmp_path / "examples.jsonl", [seed()])


def test_completion_mask_shift_and_eos_match_production_prefix(dataset):
    tokenizer = FakeTokenizer()
    artifact = build_preview(dataset, tokenizer)
    assert len(artifact["datums"]) == 2
    for datum in artifact["datums"]:
        assert datum["input_tokens"] == [11, 11, 11, 21, 21]
        assert datum["target_tokens"] == [11, 11, 21, 21, 99]
        assert datum["weights"] == [0.0, 0.0, 1.0, 1.0, 1.0]
        assert len(datum["input_tokens"]) == len(datum["target_tokens"]) == len(datum["weights"])
    for messages, kwargs in tokenizer.prompt_calls:
        assert messages[-1]["role"] == "user"
        assert kwargs == {
            "tokenize": True,
            "return_dict": False,
            "add_generation_prompt": True,
            "enable_thinking": False,
        }
        assert all(
            message["role"] != "assistant" or not message["content"].startswith("{")
            for message in messages
        )
    assert all(json.loads(text)["action"] == "clarify" for text in tokenizer.completions)
    assert artifact["totals"] == {
        "processed_tokens": 12,
        "reserved_microdollars": 9,
        "supervised_tokens": 6.0,
        "logical_batches": 1,
    }
    assert artifact["price"]["training_microdollars_per_1000_tokens"] == 737


def test_json_serializable_deterministic_fingerprint_and_schedule(dataset):
    first = build_preview(dataset, FakeTokenizer(), epochs=3, batch_size=1)
    second = build_preview(dataset, FakeTokenizer(), epochs=3, batch_size=1)
    assert first == second
    assert first["fingerprint"] == fingerprint(first)
    assert json.loads(json.dumps(first)) == first
    assert len(first["schedule"]) == 6
    for epoch in (1, 2, 3):
        batches = [batch for batch in first["schedule"] if batch["epoch"] == epoch]
        assert sorted(index for batch in batches for index in batch["datum_indices"]) == [0, 1]
    assert [batch["batch_index"] for batch in first["schedule"]] == list(range(6))
    assert first["totals"]["reserved_microdollars"] == 30  # ceiling is applied per batch


def test_real_corpus_approval_nonces_are_deterministic(sources):
    path = Path(__file__).resolve().parents[2] / "datasets/delivery/scenarios.jsonl"
    first_tokenizer = ContentSensitiveTokenizer()
    second_tokenizer = ContentSensitiveTokenizer()
    first = build_preview(path, first_tokenizer, epochs=3)
    second = build_preview(path, second_tokenizer, epochs=3)
    assert first == second
    assert len(first["datums"]) == 40
    assert first_tokenizer.prompt_calls == second_tokenizer.prompt_calls
    approvals = []
    for messages, _ in first_tokenizer.prompt_calls:
        context = json.loads(messages[0]["content"].split("\nRESIDENT_CONTEXT_DATA=", 1)[1])
        if context["approval"]:
            approvals.append(context["approval"]["id"])
    assert len(approvals) == 6
    assert first["normalization"]["approval_ids"].startswith("uuid5")


def test_normalization_changes_only_oracle_context_approval_id():
    original = "11111111-1111-4111-8111-111111111111"
    second = "22222222-2222-4222-8222-222222222222"
    context = {
        "resident": {"name": original},
        "approval": {"id": original, "location": "reception", "status": "pending"},
        "parcel_facts": {"prepaid": True},
    }
    prefix = (
        preparation.SYSTEM_PROMPT
        + "\nPLAN_SCHEMA="
        + json.dumps(preparation.AgentPlan.model_json_schema())
        + "\nRESIDENT_CONTEXT_DATA="
    )
    example = {
        "scenario_id": "fictional-seed",
        "messages": [
            {"role": "system", "content": prefix + json.dumps(context)},
            {"role": "user", "content": "My message contains " + original},
            {"role": "assistant", "content": '{"action":"wait","observation":{}}'},
        ],
    }
    before = copy.deepcopy(example)
    mapping = {}
    messages = preparation._fictional_messages(example, mapping)
    expected = copy.deepcopy(context)
    expected["approval"]["id"] = str(uuid5(NAMESPACE_URL, "guardmate-training:fictional-seed:0"))
    assert messages[0]["content"] == prefix + json.dumps(expected)
    assert messages[1:] == example["messages"][1:]
    assert example == before
    assert preparation._fictional_messages(example, mapping) == messages
    context["approval"]["id"] = second
    example["messages"][0]["content"] = prefix + json.dumps(context)
    later = preparation._fictional_messages(example, mapping)
    assert later[0]["content"] != messages[0]["content"]
    assert json.loads(later[0]["content"][len(prefix) :])["approval"]["id"] == str(
        uuid5(NAMESPACE_URL, "guardmate-training:fictional-seed:1")
    )


def test_normalization_rejects_nonproduction_prefix():
    with pytest.raises(PreparationError, match="production planner prefix"):
        preparation._fictional_messages(
            {"scenario_id": "fake", "messages": [{"role": "system", "content": "wrong"}]},
            {},
        )


def test_only_train_scenarios_and_targets_are_replayed(tmp_path, sources, monkeypatch):
    train = seed("train-one")
    validation = seed("validation-one", split="validation", first_text="Validation only question")
    test = seed("test-one", split="test", first_text="Test only question")
    validation["steps"][0]["expect"]["actions"] = ["request_takeover"]
    test["steps"][0]["expect"]["actions"] = ["request_takeover"]
    path = write_dataset(tmp_path / "all.jsonl", [train, validation, test])
    artifact = build_preview(path, FakeTokenizer())
    assert artifact["source_train_ids"] == ["train-one"]
    assert {datum["scenario_id"] for datum in artifact["datums"]} == {"train-one"}
    assert artifact["oracle"] == {
        "mode": "oracle",
        "reference_scenarios": 1,
        "passing_scenarios": 1,
    }


def test_drafts_allow_preview_without_being_marked_reviewed(dataset):
    before = dataset.read_bytes()
    artifact = build_preview(dataset, FakeTokenizer())
    assert artifact["review_ready"] is False
    assert dataset.read_bytes() == before
    assert json.loads(before)["review_status"] == "draft"


def test_review_readiness_requires_every_train_scenario(tmp_path, sources):
    records = [seed("train-one", reviewed=True), seed("train-two")]
    path = write_dataset(tmp_path / "review.jsonl", records)
    assert build_preview(path, FakeTokenizer())["review_ready"] is False
    records[-1]["review_status"] = "reviewed"
    write_dataset(path, records)
    assert build_preview(path, FakeTokenizer())["review_ready"] is True


def test_oracle_failure_blocks_rendering(tmp_path, sources):
    record = seed()
    record["steps"][0]["expect"]["actions"] = ["request_takeover"]
    path = write_dataset(tmp_path / "bad-reference.jsonl", [record])
    tokenizer = FakeTokenizer()
    with pytest.raises(PreparationError, match="reference must pass"):
        build_preview(path, tokenizer)
    assert tokenizer.prompt_calls == []


@pytest.mark.parametrize("epochs", [0, 4, True, 1.0, "1"])
def test_invalid_epoch_bounds(dataset, epochs):
    with pytest.raises(PreparationError, match="Epochs"):
        build_preview(dataset, FakeTokenizer(), epochs=epochs)


@pytest.mark.parametrize("batch_size", [0, 17, True, 1.0, "4"])
def test_invalid_batch_size_bounds(dataset, batch_size):
    with pytest.raises(PreparationError, match="Batch size"):
        build_preview(dataset, FakeTokenizer(), batch_size=batch_size)


@pytest.mark.parametrize("seed_value", [0, 43, True, 42.0, "42"])
def test_recipe_seed_is_fixed(dataset, seed_value):
    with pytest.raises(PreparationError, match="seed 42"):
        build_preview(dataset, FakeTokenizer(), seed=seed_value)


def test_prompt_and_completion_limits_do_not_truncate(dataset):
    with pytest.raises(PreparationError, match="input-token"):
        build_preview(dataset, FakeTokenizer(prompt_length=12001))
    with pytest.raises(PreparationError, match="output-token"):
        build_preview(dataset, FakeTokenizer(completion_length=512))
    artifact = build_preview(dataset, FakeTokenizer(prompt_length=12000, completion_length=511))
    assert len(artifact["datums"][0]["target_tokens"]) == 12511
    assert artifact["datums"][0]["target_tokens"][-1] == 99


def test_batch_count_limit_is_enforced(tmp_path, sources):
    records = [seed(f"train-{index}") for index in range(6)]
    path = write_dataset(tmp_path / "many.jsonl", records)
    with pytest.raises(PreparationError, match="30 logical batches"):
        build_preview(path, FakeTokenizer(), epochs=3, batch_size=1)


def test_total_cost_limit_is_enforced(dataset, monkeypatch):
    monkeypatch.setattr(preparation, "MAX_TRAINING_MICRODOLLARS", 8)
    with pytest.raises(PreparationError, match="reservation bound"):
        build_preview(dataset, FakeTokenizer())


def test_every_source_and_dataset_changes_fingerprint(dataset, sources):
    first = build_preview(dataset, FakeTokenizer())
    for name, path in sources.items():
        before = path.read_bytes()
        path.write_bytes(before + b"# changed source\n")
        changed = build_preview(dataset, FakeTokenizer())
        assert changed["source_hashes"][name] != first["source_hashes"][name]
        assert changed["fingerprint"] != first["fingerprint"]
        path.write_bytes(before)
    record = json.loads(dataset.read_text(encoding="utf-8"))
    record["rationale"] += " Extra provenance note."
    write_dataset(dataset, [record])
    changed = build_preview(dataset, FakeTokenizer())
    assert changed["source_hashes"]["dataset_sha256"] != first["source_hashes"]["dataset_sha256"]
    assert changed["fingerprint"] != first["fingerprint"]


def test_source_hashes_bind_prompt_schema_and_recipe(dataset):
    artifact = build_preview(dataset, FakeTokenizer())
    assert len(artifact["prompt_sha256"]) == len(artifact["plan_schema_sha256"]) == 64
    assert (
        artifact["source_hashes"]["dataset_sha256"]
        == hashlib.sha256(dataset.read_bytes()).hexdigest()
    )
    assert "training/preparation.py" in artifact["source_hashes"]
    assert "training/worker.py" in artifact["source_hashes"]
    assert "training/supervisor.py" in artifact["source_hashes"]
    assert "backend/scripts/train_delivery.py" in artifact["source_hashes"]
    for critical_source in (
        "training/ledger.py",
        "models.py",
        "context.py",
        "agent/store.py",
        "evaluation/schema.py",
        "evaluation/dataset.py",
    ):
        assert critical_source in artifact["source_hashes"]
    assert artifact["config"] == {
        "rank": 16,
        "learning_rate": 1e-4,
        "epochs": 1,
        "batch_size": 4,
        "seed": 42,
    }


def test_tokenizer_vocab_and_template_changes_bind_preview(dataset):
    first = build_preview(dataset, FakeTokenizer())
    tokenizer = FakeTokenizer()
    tokenizer.chat_template += " amended"
    changed = build_preview(dataset, tokenizer)
    assert (
        changed["tokenizer"]["chat_template_sha256"] != first["tokenizer"]["chat_template_sha256"]
    )
    assert changed["fingerprint"] != first["fingerprint"]
    tokenizer = FakeTokenizer()
    tokenizer.get_vocab = lambda: {"prefix": 11, "completion": 21, "EOS": 99, "added": 101}
    changed = build_preview(dataset, tokenizer)
    assert changed["tokenizer"]["vocab_sha256"] != first["tokenizer"]["vocab_sha256"]


@pytest.mark.parametrize("bad", [{}, {"EOS": True}, {"EOS": -1}, {"EOS": "99"}])
def test_invalid_vocab_rejected(dataset, bad):
    tokenizer = FakeTokenizer()
    tokenizer.get_vocab = lambda: bad
    with pytest.raises(PreparationError, match="vocabulary"):
        build_preview(dataset, tokenizer)


def test_missing_eos_rejected(dataset):
    tokenizer = FakeTokenizer()
    tokenizer.eos_token_id = None
    with pytest.raises(PreparationError, match="EOS token"):
        build_preview(dataset, tokenizer)


def test_missing_template_rejected(dataset):
    tokenizer = FakeTokenizer()
    tokenizer.chat_template = None
    with pytest.raises(PreparationError, match="chat template"):
        build_preview(dataset, tokenizer)


def test_mid_preparation_edit_is_rejected(dataset, sources):
    tokenizer = FakeTokenizer()
    original_encode = tokenizer.encode
    path = next(iter(sources.values()))

    def edited_encode(text, **kwargs):
        path.write_text("# modified while preparing\n", encoding="utf-8")
        return original_encode(text, **kwargs)

    tokenizer.encode = edited_encode
    with pytest.raises(PreparationError, match="sources changed"):
        build_preview(dataset, tokenizer)


def test_mid_preparation_tokenizer_edit_is_rejected(dataset):
    tokenizer = FakeTokenizer()
    original_encode = tokenizer.encode

    def edited_encode(text, **kwargs):
        tokenizer.chat_template += " changed"
        return original_encode(text, **kwargs)

    tokenizer.encode = edited_encode
    with pytest.raises(PreparationError, match="Tokenizer changed"):
        build_preview(dataset, tokenizer)


def test_fingerprint_ignores_only_itself_and_does_not_mutate():
    artifact = {"a": [1, 2], "b": {"c": True}, "fingerprint": "stale"}
    before = copy.deepcopy(artifact)
    assert fingerprint(artifact) == fingerprint({"b": {"c": True}, "a": [1, 2]})
    assert artifact == before
    artifact["a"][0] = 9
    assert fingerprint(artifact) != fingerprint(before)


@pytest.mark.parametrize("artifact", [[], {"bad": float("nan")}, {"bad": object()}])
def test_non_json_fingerprint_is_rejected(artifact):
    with pytest.raises(PreparationError):
        fingerprint(artifact)


def test_missing_train_split_is_rejected(tmp_path, sources):
    path = write_dataset(tmp_path / "validation-only.jsonl", [seed(split="validation")])
    with pytest.raises(PreparationError, match="train-only dataset"):
        build_preview(path, FakeTokenizer())


def test_missing_required_source_is_rejected(dataset, sources):
    path = next(iter(sources.values()))
    path.unlink()
    with pytest.raises(PreparationError, match="source could not be read"):
        build_preview(dataset, FakeTokenizer())
