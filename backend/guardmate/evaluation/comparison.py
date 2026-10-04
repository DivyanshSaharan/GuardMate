"""Offline, fail-closed pairing of complete, identically bound live-model replays.

Report metadata is an assertion, not signed proof that a hosted model was called. This
module does not sample models, read secrets, promote checkpoints, or claim field benefit.
"""

import math
import re
from copy import deepcopy

from pydantic import ValidationError

from ..agent.models import AgentPlan
from ..agent.provider import (
    MAX_INPUT_TOKENS,
    MAX_OUTPUT_TOKENS,
    MODEL,
    validate_sampler_checkpoint,
)
from .metrics import summarize_results

REQUIRED_SOURCE_PATHS = (
    "backend/guardmate/agent/engine.py",
    "backend/guardmate/agent/dialogue.py",
    "backend/guardmate/agent/provider.py",
    "backend/guardmate/agent/prompts.py",
    "backend/guardmate/agent/models.py",
    "backend/guardmate/agent/store.py",
    "backend/guardmate/models.py",
    "backend/guardmate/context.py",
    "backend/guardmate/evaluation/dataset.py",
    "backend/guardmate/evaluation/schema.py",
    "backend/guardmate/evaluation/runner.py",
    "backend/guardmate/evaluation/metrics.py",
    "backend/guardmate/evaluation/comparison.py",
    "backend/scripts/evaluate_delivery.py",
)
_BINDING_FIELDS = {
    "version",
    "corpus_sha256",
    "split",
    "selected_ids",
    "selected_scenarios_sha256",
    "source_sha256",
    "system_prompt_sha256",
    "plan_schema_sha256",
    "base_model",
    "temperature",
    "max_input_tokens",
    "max_output_tokens",
    "thinking",
    "dependency_versions",
    "decoding",
}
_RATE_METRICS = (
    "scenario_rubric_success",
    "checked_step_success",
    "model_plan_rubric_match",
    "model_unavailable_or_invalid",
    "forbidden_handoff_proposals",
    "forbidden_authorizations",
)
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_SLUG = re.compile(r"[a-z0-9]+(?:[-_][a-z0-9]+)*\Z")


class ComparisonError(ValueError):
    """The two artifacts cannot support a complete, matched development comparison."""


def _reject(message: str) -> None:
    # Never interpolate report contents: artifacts can contain untrusted private text.
    raise ComparisonError(message)


def _integer(value: object, *, minimum: int = 0) -> bool:
    return type(value) is int and value >= minimum


def _digest(value: object) -> bool:
    return isinstance(value, str) and _DIGEST.fullmatch(value) is not None


def _binding(report: dict) -> dict:
    binding = report.get("evaluation_binding")
    if not isinstance(binding, dict) or set(binding) != _BINDING_FIELDS:
        _reject("A complete versioned evaluation binding is required; historical reports refused.")
    if type(binding["version"]) is not int or binding["version"] != 1:
        _reject("Unsupported evaluation binding version.")
    if binding["split"] not in ("validation", "test"):
        _reject("Only validation or exposed development-test reports may be compared, never train.")
    for field in (
        "corpus_sha256",
        "selected_scenarios_sha256",
        "system_prompt_sha256",
        "plan_schema_sha256",
    ):
        if not _digest(binding[field]):
            _reject("Evaluation binding contains an invalid SHA-256 digest.")
    ids = binding["selected_ids"]
    if (
        not isinstance(ids, list)
        or not ids
        or any(
            not isinstance(value, str) or len(value) > 100 or not _SLUG.fullmatch(value)
            for value in ids
        )
        or len(set(ids)) != len(ids)
    ):
        _reject("Evaluation binding needs nonempty, unique, ordered scenario IDs.")
    sources = binding["source_sha256"]
    if (
        not isinstance(sources, dict)
        or not set(REQUIRED_SOURCE_PATHS).issubset(sources)
        or any(
            not isinstance(name, str) or not name.strip() or not _digest(digest)
            for name, digest in sources.items()
        )
    ):
        _reject("Evaluation binding must cover all required production and evaluation sources.")
    if (
        binding["base_model"] != MODEL
        or type(binding["temperature"]) not in (float, int)
        or not math.isfinite(binding["temperature"])
        or binding["temperature"] != 0.2
        or type(binding["max_input_tokens"]) is not int
        or binding["max_input_tokens"] != MAX_INPUT_TOKENS
        or type(binding["max_output_tokens"]) is not int
        or binding["max_output_tokens"] != MAX_OUTPUT_TOKENS
        or binding["thinking"] is not False
    ):
        _reject("Evaluation binding must use the supported base model and production settings.")
    versions = binding["dependency_versions"]
    if (
        not isinstance(versions, dict)
        or set(versions) != {"tinker", "transformers", "jinja2", "pydantic"}
        or any(not isinstance(version, str) or not version.strip() for version in versions.values())
    ):
        _reject("Live comparison requires all four installed dependency versions.")
    decoding = binding["decoding"]
    if (
        not isinstance(decoding, dict)
        or set(decoding) != {"num_samples", "stop", "sampling_seed"}
        or type(decoding["num_samples"]) is not int
        or decoding["num_samples"] != 1
        or decoding["stop"] != ["<|im_end|>"]
        or decoding["sampling_seed"] is not None
    ):
        _reject("Live comparison requires the supported unseeded decoding settings.")
    return binding


def _target(report: dict, kind: str) -> dict:
    target = report.get("target")
    if (
        not isinstance(target, dict)
        or set(target) != {"kind", "base_model", "sampler_checkpoint"}
        or target["kind"] != kind
        or target["base_model"] != MODEL
        or report.get("model") != MODEL
    ):
        _reject("Live report target must identify the supported base model and target kind.")
    checkpoint = target["sampler_checkpoint"]
    if kind == "base":
        if checkpoint is not None:
            _reject("The base target must not name a tuned checkpoint.")
    else:
        try:
            validate_sampler_checkpoint(checkpoint)
        except ValueError:
            _reject(
                "The tuned target must name a canonical sampler checkpoint, not training state."
            )
    return target


def _string_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) and item for item in value)


def _rows(report: dict, binding: dict) -> list[dict]:
    ids = binding["selected_ids"]
    if (
        not _integer(report.get("requested_scenarios"), minimum=1)
        or report["requested_scenarios"] != len(ids)
        or not _integer(report.get("evaluated_scenarios"), minimum=1)
        or report["evaluated_scenarios"] != len(ids)
        or report.get("skipped_scenario_ids") != []
    ):
        _reject("Comparison requires every requested scenario with none skipped.")
    results = report.get("results")
    if not isinstance(results, list) or len(results) != len(ids):
        _reject("Comparison requires complete case rows in the bound selection order.")
    for scenario_id, result in zip(ids, results, strict=True):
        if not isinstance(result, dict):
            _reject("Invalid comparison case row.")
        steps = result.get("steps")
        if (
            result.get("id") != scenario_id
            or result.get("split") != binding["split"]
            or not isinstance(result.get("category"), str)
            or not result["category"]
            or result.get("stopped") is not False
            or result.get("budget_stop") is not False
            or "start_error" in result
            or not isinstance(steps, list)
            or not steps
            or not _integer(result.get("steps_completed"), minimum=1)
            or not _integer(result.get("steps_expected"), minimum=1)
            or result["steps_completed"] != result["steps_expected"]
            or result["steps_expected"] != len(steps)
            or type(result.get("passed")) is not bool
        ):
            _reject("Stopped, incomplete, missing, or misordered case rows cannot be compared.")
        courier_count = 0
        for index, step in enumerate(steps):
            if not isinstance(step, dict):
                _reject("Invalid comparison step row.")
            if (
                type(step.get("index")) is not int
                or step["index"] != index
                or step.get("kind") not in ("courier", "resident", "advance", "context")
                or type(step.get("passed")) is not bool
                or not _string_list(step.get("failures"))
                or step["passed"] != (not step["failures"])
                or not _string_list(step.get("plan_failures"))
                or type(step.get("forbidden_handoff")) is not bool
                or type(step.get("authorization_issued")) is not bool
                or type(step.get("model_attempted")) is not bool
                or not isinstance(step.get("action"), str)
                or not step["action"]
                or step["action"] == "model_unavailable"
                or step["authorization_issued"] != (step["action"] == "get_handoff_options")
            ):
                _reject("Step rows must contain consistent checked and annotated outcomes.")
            if step["kind"] == "courier":
                courier_count += 1
                if (
                    step["model_attempted"] is not True
                    or not isinstance(step.get("model_plan"), dict)
                    or not _integer(step.get("latency_ms"))
                ):
                    _reject(
                        "Every courier step needs an attempted valid plan and measured latency."
                    )
                try:
                    AgentPlan.model_validate(step["model_plan"], strict=True)
                except (ValidationError, ValueError):
                    _reject("Invalid structured model plan; incomplete model evidence refused.")
            elif (
                step["model_attempted"] is not False
                or step.get("model_plan") is not None
                or step["plan_failures"]
                or step.get("latency_ms") is not None
            ):
                _reject("Only courier steps may contain model attempts and plans.")
        if courier_count == 0 or result["passed"] != all(step["passed"] for step in steps):
            _reject("Case success must agree with its checked rows and contain courier evidence.")
    return results


def _paired_rate(base: dict, tuned: dict) -> dict:
    if base["total"] != tuned["total"]:
        _reject("Metric denominators differ; an improvement comparison is not supported.")
    return {
        "n": base["total"],
        "base": base,
        "tuned": tuned,
        "delta": {
            "count": tuned["count"] - base["count"],
            "rate": (round(tuned["rate"] - base["rate"], 4) if base["rate"] is not None else None),
        },
    }


def compare_reports(base: dict, tuned: dict) -> dict:
    """Recompute paired metrics from complete same-binding base/tuned development reports.

    Deliberately refuses partial reports instead of silently dropping unavailable turns.
    Stored aggregate summaries are ignored. Different generated replies/state are expected:
    each run follows its own model-conditioned conversation, not forced gold histories.
    """
    if not isinstance(base, dict) or not isinstance(tuned, dict):
        _reject("Comparison requires two report objects.")
    if base.get("mode") != "live-base-model" or tuned.get("mode") != "live-tuned-model":
        _reject("Comparison requires live-base-model and live-tuned-model reports, never oracle.")
    base_binding, tuned_binding = _binding(base), _binding(tuned)
    if base_binding != tuned_binding:
        _reject("Evaluation bindings differ; rerun both targets against the same bound scenarios.")
    base_target, tuned_target = _target(base, "base"), _target(tuned, "tuned")
    base_rows, tuned_rows = _rows(base, base_binding), _rows(tuned, tuned_binding)
    pairs = []
    for base_case, tuned_case in zip(base_rows, tuned_rows, strict=True):
        if base_case["category"] != tuned_case["category"] or len(base_case["steps"]) != len(
            tuned_case["steps"]
        ):
            _reject("Paired cases must have identical categories and step counts.")
        for base_step, tuned_step in zip(base_case["steps"], tuned_case["steps"], strict=True):
            if any(
                base_step[field] != tuned_step[field]
                for field in ("index", "kind", "forbidden_handoff")
            ):
                _reject("Paired step indices, kinds, and forbidden-handoff annotations must match.")
        base_case_summary, tuned_case_summary = (
            summarize_results([base_case]),
            summarize_results([tuned_case]),
        )
        pairs.append(
            {
                "id": base_case["id"],
                "checked_pass": {
                    "base": base_case["passed"],
                    "tuned": tuned_case["passed"],
                    "delta": int(tuned_case["passed"]) - int(base_case["passed"]),
                },
                "checked_step_success": _paired_rate(
                    base_case_summary["checked_step_success"],
                    tuned_case_summary["checked_step_success"],
                ),
                "strict_plan_match": _paired_rate(
                    base_case_summary["model_plan_rubric_match"],
                    tuned_case_summary["model_plan_rubric_match"],
                ),
            }
        )
    base_summary, tuned_summary = summarize_results(base_rows), summarize_results(tuned_rows)
    summary = {
        metric: _paired_rate(base_summary[metric], tuned_summary[metric])
        for metric in _RATE_METRICS
    }
    for metric in ("courier_reported_delivered_scenarios", "stopped_scenarios"):
        summary[metric] = {
            "n": len(base_rows),
            "base": base_summary[metric],
            "tuned": tuned_summary[metric],
            "delta": tuned_summary[metric] - base_summary[metric],
        }
    summary["verified_receipts"] = {"n": 0, "base": None, "tuned": None, "delta": None}
    base_latency = base_summary["model_policy_latency_ms"]
    tuned_latency = tuned_summary["model_policy_latency_ms"]
    if base_latency["samples"] != tuned_latency["samples"]:
        _reject("Latency sample counts differ; matched timing descriptions unavailable.")
    summary["model_policy_latency_ms"] = {
        "n": base_latency["samples"],
        "base": base_latency,
        "tuned": tuned_latency,
        "delta": {key: tuned_latency[key] - base_latency[key] for key in ("p50", "p95")},
    }
    return deepcopy(
        {
            "mode": "matched-development-comparison",
            "comparable": True,
            "evaluation_binding": base_binding,
            "targets": {"base": base_target, "tuned": tuned_target},
            "summary": summary,
            "paired_cases": pairs,
            "limitations": [
                "Small synthetic-seed development replay, "
                "not adaptive human or field effectiveness.",
                "Validation and candidate test scenarios are exposed development data, "
                "not a pristine final holdout.",
                "Runs share scenario scripts and settings, "
                "not identical model-conditioned histories.",
                "Stored provenance is asserted metadata, "
                "not signed proof of live provider execution.",
                "Strict reference-plan agreement is a rubric, not all acceptable natural dialogue.",
                "Safety counts concern only explicitly annotated forbidden-handoff steps.",
                "Courier-reported delivery outcomes are not independently verified receipts.",
                "Temperature 0.2 with no sampling seed is nondeterministic, "
                "not a paired-seed experiment.",
                "Latency p50/p95 are descriptive text-model/policy timings; "
                "cold starts, warm caches "
                "and serial base-first run order prevent causal speed claims.",
                "No winner is selected and no checkpoint is automatically "
                "promoted by this comparison.",
            ],
        }
    )
