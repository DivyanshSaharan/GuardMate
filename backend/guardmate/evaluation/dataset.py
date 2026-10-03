import hashlib
import re
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

from pydantic import ValidationError

from .schema import CourierStep, Scenario, Split


class DatasetError(ValueError):
    pass


def courier_trace(scenario: Scenario) -> str:
    return " | ".join(
        re.sub(r"[^a-z0-9]+", " ", step.text.casefold()).strip()
        for step in scenario.steps
        if isinstance(step, CourierStep)
    )


def load_dataset(path: Path) -> list[Scenario]:
    scenarios = []
    ids: set[str] = set()
    groups: dict[str, str] = {}
    traces: dict[str, Scenario] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        try:
            scenario = Scenario.model_validate_json(line)
        except ValidationError as error:
            # Field locations/types only: never echo arbitrary dataset inputs or secrets.
            fields = ", ".join(
                f"{'.'.join(map(str, item['loc']))}: {item['type']}"
                for item in error.errors(include_input=False, include_url=False)
            )
            raise DatasetError(f"Invalid scenario at line {number}: {fields}") from None
        if scenario.id in ids:
            raise DatasetError(f"Duplicate scenario ID: {scenario.id}")
        if scenario.group_id in groups and groups[scenario.group_id] != scenario.split:
            raise DatasetError(f"Seed group crosses splits: {scenario.group_id}")
        trace = courier_trace(scenario)
        duplicate = traces.get(trace)
        if duplicate and duplicate.split != scenario.split:
            raise DatasetError(
                f"Duplicate courier trace crosses splits: {duplicate.id}, {scenario.id}"
            )
        ids.add(scenario.id)
        groups[scenario.group_id] = scenario.split
        traces[trace] = scenario
        scenarios.append(scenario)
    if not scenarios:
        raise DatasetError("Dataset is empty.")
    return scenarios


def dataset_summary(path: Path, scenarios: list[Scenario]) -> dict:
    similarities = []
    for index, first in enumerate(scenarios):
        for second in scenarios[index + 1 :]:
            if first.split == second.split:
                continue
            ratio = SequenceMatcher(None, courier_trace(first), courier_trace(second)).ratio()
            if ratio >= 0.85:
                similarities.append({"ids": [first.id, second.id], "similarity": round(ratio, 3)})
    return {
        "dataset_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "scenario_count": len(scenarios),
        "courier_turns": sum(
            isinstance(step, CourierStep) for scenario in scenarios for step in scenario.steps
        ),
        "splits": dict(Counter(scenario.split for scenario in scenarios)),
        "categories": dict(Counter(scenario.category for scenario in scenarios)),
        "sources": dict(Counter(scenario.source for scenario in scenarios)),
        "review_status": dict(Counter(scenario.review_status for scenario in scenarios)),
        "cross_split_similarity_warnings": similarities,
        "limitations": [
            "Similarity heuristics do not prove absence of semantic/template leakage.",
            "Synthetic authored cases are not friend testing or real courier evidence.",
            "Draft examples require policy/label review before training.",
        ],
    }


def select_scenarios(scenarios: list[Scenario], split: Split, limit: int | None) -> list[Scenario]:
    selected = [scenario for scenario in scenarios if scenario.split == split]
    if not selected:
        raise DatasetError(f"No scenarios in requested split: {split}")
    if limit is not None and limit < 1:
        raise DatasetError("Limit must be positive.")
    return selected[:limit] if limit else selected
