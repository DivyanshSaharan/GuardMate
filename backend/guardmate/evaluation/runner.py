import hashlib
import importlib.metadata
import json
import math
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from ..agent.engine import ConversationEngine
from ..agent.models import AgentPlan, ModelStatus, ResidentDecision, TurnRequest
from ..agent.prompts import SYSTEM_PROMPT
from ..agent.provider import (
    MAX_INPUT_TOKENS,
    MAX_OUTPUT_TOKENS,
    MODEL,
    ModelUnavailable,
    PlanProvider,
    validate_sampler_checkpoint,
)
from ..agent.store import ConversationStore
from ..context import build_context
from ..models import Dashboard, DeliveryMode, ResidentProfile, TodayOverride
from .metrics import compare_plan, compare_state, summarize_results
from .schema import AdvanceStep, ContextStep, CourierStep, ResidentStep, Scenario

# Same conservative rates/limits as the current production adapter, not a billing quote.
WORST_CALL_MICRODOLLARS = math.ceil(MAX_INPUT_TOKENS * 0.33 + MAX_OUTPUT_TOKENS * 1.005)

EVALUATION_SOURCE_PATHS = (
    "backend/guardmate/models.py",
    "backend/guardmate/context.py",
    "backend/guardmate/agent/models.py",
    "backend/guardmate/agent/prompts.py",
    "backend/guardmate/agent/engine.py",
    "backend/guardmate/agent/dialogue.py",
    "backend/guardmate/agent/provider.py",
    "backend/guardmate/agent/store.py",
    "backend/guardmate/evaluation/dataset.py",
    "backend/guardmate/evaluation/schema.py",
    "backend/guardmate/evaluation/runner.py",
    "backend/guardmate/evaluation/metrics.py",
    "backend/guardmate/evaluation/comparison.py",
    "backend/scripts/evaluate_delivery.py",
)


class EvaluationBindingError(ValueError):
    """A report cannot claim a stable, fully identified evaluation environment."""


def _selected_scenarios_sha256(scenarios: list[Scenario]) -> str:
    canonical = json.dumps(
        [scenario.model_dump(mode="json") for scenario in scenarios],
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _legacy_fingerprints() -> dict:
    # Keep the original keys/encoding for historical-report diagnostics.
    return {
        "system_prompt_sha256": hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest(),
        "plan_schema_sha256": hashlib.sha256(
            json.dumps(AgentPlan.model_json_schema(), sort_keys=True).encode()
        ).hexdigest(),
        "source_sha256": {
            name: hashlib.sha256(
                (Path(__file__).resolve().parents[1] / "agent" / name).read_bytes()
            ).hexdigest()
            for name in ("engine.py", "dialogue.py", "provider.py", "prompts.py")
        },
        "model": MODEL,
        "temperature": 0.2,
        "max_input_tokens": MAX_INPUT_TOKENS,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "thinking": False,
    }


def _source_sha256() -> dict[str, str]:
    root = Path(__file__).resolve().parents[3]
    try:
        return {
            name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in EVALUATION_SOURCE_PATHS
        }
    except OSError:
        # No partial binding, and do not expose a path or untrusted filesystem error.
        raise EvaluationBindingError(
            "Evaluation source files could not be fingerprinted."
        ) from None


def _dependency_versions() -> dict[str, str | None]:
    versions = {}
    for package in ("tinker", "transformers", "jinja2", "pydantic"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    return versions


def _evaluation_target(provider: PlanProvider, mode: str) -> dict:
    underlying = provider
    while isinstance(underlying, BoundedProvider | RecordingProvider):
        underlying = underlying.provider
    if (
        mode == "oracle"
        and not isinstance(provider, GoldPlanProvider)
        or mode != "oracle"
        and isinstance(underlying, GoldPlanProvider)
    ):
        raise ValueError(
            "Oracle results must be labelled oracle and cannot masquerade as a baseline."
        )
    checkpoint = getattr(underlying, "sampler_checkpoint", None)
    if checkpoint is not None:
        checkpoint = validate_sampler_checkpoint(checkpoint)
    if mode == "live-tuned-model" and checkpoint is None:
        raise ValueError("Tuned evaluation requires an explicit sampler checkpoint.")
    if mode != "live-tuned-model" and checkpoint is not None:
        raise ValueError("A sampler checkpoint cannot be labelled base-model or oracle evaluation.")
    return {
        "kind": {"oracle": "oracle", "live-base-model": "base", "live-tuned-model": "tuned"}[mode],
        "base_model": MODEL,
        "sampler_checkpoint": checkpoint,
    }


def _evaluation_binding(
    scenarios: list[Scenario], corpus_sha256: str, split: str, fingerprints: dict
) -> dict:
    return {
        "version": 1,
        "corpus_sha256": corpus_sha256,
        "split": split,
        "selected_ids": [scenario.id for scenario in scenarios],
        "selected_scenarios_sha256": _selected_scenarios_sha256(scenarios),
        "source_sha256": _source_sha256(),
        "system_prompt_sha256": fingerprints["system_prompt_sha256"],
        "plan_schema_sha256": fingerprints["plan_schema_sha256"],
        "base_model": MODEL,
        "temperature": 0.2,
        "max_input_tokens": MAX_INPUT_TOKENS,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "thinking": False,
        "dependency_versions": _dependency_versions(),
        "decoding": {
            "num_samples": 1,
            "stop": ["<|im_end|>"],
            "sampling_seed": None,
        },
    }


class EvaluationBudgetStop(ModelUnavailable):
    pass


class BoundedProvider:
    """Pre-admit worst-case cost per attempt; never relax the underlying persistent cap."""

    def __init__(self, provider: PlanProvider, max_calls: int, max_microdollars: int):
        if max_calls < 1 or max_microdollars < 1:
            raise ValueError("Evaluation requires positive call and cost limits.")
        self.provider = provider
        self.max_calls = max_calls
        self.max_microdollars = max_microdollars
        self.attempts = 0
        self.admitted_microdollars = 0

    def status(self) -> ModelStatus:
        return self.provider.status()

    def generate(self, messages: list[dict[str, str]]) -> AgentPlan:
        if (
            self.attempts >= self.max_calls
            or self.admitted_microdollars + WORST_CALL_MICRODOLLARS > self.max_microdollars
        ):
            raise EvaluationBudgetStop("Evaluation call/cost limit reached before sampling.")
        self.attempts += 1
        self.admitted_microdollars += WORST_CALL_MICRODOLLARS
        return self.provider.generate(messages)


class GoldPlanProvider:
    """Oracle harness check only. Its output must never be called model/baseline evidence."""

    plan: AgentPlan | None = None

    def status(self) -> ModelStatus:
        return ModelStatus(
            configured=True,
            model="gold-plan-oracle-NOT-A-MODEL",
            provider="Authored reference plans; zero inference",
            message="Harness/policy replay only; not a model baseline.",
            reserved_usd=0,
            budget_usd=0,
        )

    def generate(self, messages: list[dict[str, str]]) -> AgentPlan:
        if self.plan is None:
            raise ModelUnavailable("No authored reference plan is available.")
        return self.plan.model_copy(deep=True)


class RecordingProvider:
    def __init__(self, provider: PlanProvider):
        self.provider = provider
        self.plan: AgentPlan | None = None
        self.messages: list[dict[str, str]] = []
        self.called = False
        self.stopped_by_budget = False

    def reset(self) -> None:
        self.plan = None
        self.messages = []
        self.called = False
        self.stopped_by_budget = False

    def status(self) -> ModelStatus:
        return self.provider.status()

    def generate(self, messages: list[dict[str, str]]) -> AgentPlan:
        self.called = True
        self.messages = [dict(message) for message in messages]
        try:
            self.plan = self.provider.generate(messages)
            return self.plan
        except EvaluationBudgetStop:
            self.stopped_by_budget = True
            self.called = False  # The underlying provider was not called.
            raise


def run_scenario(scenario: Scenario, provider: PlanProvider, directory: Path) -> dict:
    # Simulated time advances only on explicit steps; no waiting and no real resident data.
    now = datetime(2026, 10, 5, 6, tzinfo=UTC)
    profile = ResidentProfile.model_validate(scenario.profile.model_dump())
    mode = DeliveryMode(enabled=True, expires_at=now + timedelta(hours=2))
    override = TodayOverride(
        status=scenario.availability, local_date=now.astimezone(ZoneInfo("Asia/Kolkata")).date()
    )

    def dashboard() -> Dashboard:
        return Dashboard(
            profile=profile,
            delivery_mode=mode,
            context=build_context(profile, mode, override, now),
            server_time=now,
        )

    recorder = RecordingProvider(provider)
    engine = ConversationEngine(ConversationStore(directory), recorder, dashboard, lambda: now)
    rows: list[dict] = []
    training_examples: list[dict] = []
    session = None
    try:
        session = engine.start()
    except HTTPException as error:
        return {
            "id": scenario.id,
            "split": scenario.split,
            "category": scenario.category,
            "passed": False,
            "stopped": True,
            "start_error": f"HTTP {error.status_code}",
            "steps": [],
            "training_examples": [],
            "budget_stop": False,
        }
    for index, step in enumerate(scenario.steps):
        recorder.reset()
        previous_event_count = len(session.events)
        operation_error = None
        started = perf_counter()
        try:
            if isinstance(step, CourierStep):
                if isinstance(provider, GoldPlanProvider):
                    provider.plan = step.gold_plan
                session = engine.turn(
                    session.id, TurnRequest(text=step.text, revision=session.revision)
                )
            elif isinstance(step, ResidentStep):
                session = engine.decide(
                    session.id,
                    ResidentDecision(
                        decision=step.decision,
                        approval_id=session.approval.id if session.approval else None,
                        revision=session.revision,
                    ),
                )
            elif isinstance(step, AdvanceStep):
                now += timedelta(seconds=step.seconds)
                session = engine.read(session.id)
            elif isinstance(step, ContextStep):
                if step.availability is not None:
                    override.status = step.availability
                if step.guard_location is not None:
                    profile = ResidentProfile.model_validate(
                        {**profile.model_dump(), "guard_location": step.guard_location}
                    )
                if step.delivery_enabled is not None:
                    mode = DeliveryMode(
                        enabled=step.delivery_enabled,
                        expires_at=now + timedelta(hours=2) if step.delivery_enabled else None,
                    )
                session = engine.read(session.id)
        except HTTPException as error:
            operation_error = f"HTTP {error.status_code}"
            session = engine.read(session.id)
        action = (
            session.events[-1].action if len(session.events) > previous_event_count else "no_event"
        )
        failures = compare_state(session, step.expect, action, profile.guard_location)
        if operation_error:
            failures.append(f"operation: {operation_error}")
        if action == "model_unavailable":
            failures.append("model_unavailable")
        plan_failures = (
            compare_plan(recorder.plan, step.gold_plan, step.text)
            if isinstance(step, CourierStep) and recorder.plan is not None
            else []
        )
        row = {
            "index": index,
            "kind": step.kind,
            "action": action,
            "passed": not failures,
            "failures": failures,
            "plan_failures": plan_failures,
            "model_attempted": recorder.called,
            "model_plan": recorder.plan.model_dump(mode="json") if recorder.plan else None,
            "forbidden_handoff": step.expect.forbidden_handoff,
            "authorization_issued": action == "get_handoff_options",
            "authorized_location": session.authorized_location,
            "approval_status": session.approval.status if session.approval else None,
            "availability": dashboard().context.availability.value,
            "availability_source": dashboard().context.availability_source,
            "simulated_time": now.isoformat(),
            "status": session.status,
            "courier_reported_outcome": session.courier_reported_outcome,
            "latency_ms": round((perf_counter() - started) * 1000) if recorder.called else None,
            "reply": session.messages[-1].content,
        }
        rows.append(row)
        if (
            isinstance(step, CourierStep)
            and recorder.messages
            and isinstance(provider, GoldPlanProvider)
        ):
            target = step.gold_plan.model_dump(mode="json", exclude_defaults=True)
            target.setdefault("observation", {})
            training_examples.append(
                {
                    "scenario_id": scenario.id,
                    "group_id": scenario.group_id,
                    "step_index": index,
                    "split": scenario.split,
                    "source": scenario.source,
                    "review_status": scenario.review_status,
                    "messages": [
                        *recorder.messages,
                        {
                            "role": "assistant",
                            "content": json.dumps(target, ensure_ascii=True),
                        },
                    ],
                }
            )
        if recorder.stopped_by_budget:
            break
    completed = len(rows) == len(scenario.steps)
    return {
        "id": scenario.id,
        "split": scenario.split,
        "category": scenario.category,
        "passed": completed and all(row["passed"] for row in rows),
        "stopped": not completed or any(row["action"] == "model_unavailable" for row in rows),
        "steps_completed": len(rows),
        "steps_expected": len(scenario.steps),
        "steps": rows,
        "courier_reported_outcome": session.courier_reported_outcome,
        "training_examples": training_examples,
        "budget_stop": recorder.stopped_by_budget,
    }


def run_evaluation(
    scenarios: list[Scenario],
    provider: PlanProvider,
    mode: str,
    *,
    corpus_sha256: str | None = None,
    split: str | None = None,
) -> dict:
    if mode not in ("oracle", "live-base-model", "live-tuned-model"):
        raise ValueError("Unsupported evaluation mode.")
    target = _evaluation_target(provider, mode)
    bound = corpus_sha256 is not None or split is not None
    if bound:
        if not isinstance(corpus_sha256, str) or not re.fullmatch(
            r"[a-fA-F0-9]{64}", corpus_sha256
        ):
            raise EvaluationBindingError("Evaluation requires a 64-character corpus SHA-256.")
        if split not in ("train", "validation", "test"):
            raise EvaluationBindingError("Evaluation requires an explicit supported split.")
        if not scenarios or any(scenario.split != split for scenario in scenarios):
            raise EvaluationBindingError("Every selected scenario must belong to the stated split.")
        selected_ids = [scenario.id for scenario in scenarios]
        if any(not identifier for identifier in selected_ids) or len(selected_ids) != len(
            set(selected_ids)
        ):
            raise EvaluationBindingError("Selected scenario IDs must be nonempty and unique.")
        corpus_sha256 = corpus_sha256.lower()

    # Snapshot before any generation; a post-replay-only hash could bind a result to
    # code or annotations that were never used for that replay.
    fingerprints = _legacy_fingerprints()
    selected_sha256 = _selected_scenarios_sha256(scenarios)
    model = provider.status().model
    binding = _evaluation_binding(scenarios, corpus_sha256, split, fingerprints) if bound else None
    results = []
    skipped = []
    with TemporaryDirectory(prefix="guardmate-eval-") as temporary:
        for index, scenario in enumerate(scenarios):
            result = run_scenario(scenario, provider, Path(temporary) / str(index))
            results.append(result)
            if result["budget_stop"] or any(
                row["action"] == "model_unavailable" for row in result["steps"]
            ):
                skipped = [remaining.id for remaining in scenarios[index + 1 :]]
                break  # No automatic retries or continuing to spend after a provider failure.
    final_fingerprints = _legacy_fingerprints()
    final_binding = (
        _evaluation_binding(scenarios, corpus_sha256, split, final_fingerprints) if bound else None
    )
    try:
        final_target = _evaluation_target(provider, mode)
    except ValueError:
        raise EvaluationBindingError("Evaluation target changed during replay.") from None
    if (
        final_fingerprints != fingerprints
        or final_binding != binding
        or _selected_scenarios_sha256(scenarios) != selected_sha256
        or final_target != target
        or provider.status().model != model
    ):
        raise EvaluationBindingError(
            "Evaluation inputs, source or target changed during replay; "
            "no stable report is available."
        )
    summary = summarize_results(results)
    if mode == "oracle":
        # Reference replay is not evidence of a model's correctness, latency or safety.
        summary = {
            "reference_replay_success": summary["scenario_rubric_success"],
            "reference_step_success": summary["checked_step_success"],
            "stopped_scenarios": summary["stopped_scenarios"],
        }
    report = {
        "mode": mode,
        "model": model,
        "target": target,
        "requested_scenarios": len(scenarios),
        "evaluated_scenarios": len(results),
        "skipped_scenario_ids": skipped,
        "summary": summary,
        "results": results,
        "fingerprints": fingerprints,
        "limitations": [
            "Scripted synthetic-seed replay; not adaptive human role-play or field effectiveness.",
            "Exact reference-plan match is one rubric, not the only acceptable natural dialogue.",
            "Safety counts apply only to explicitly annotated forbidden-handoff steps.",
            "Courier reports are not independently verified delivery receipts.",
            "Time is simulated; latency is text-model/policy time, not spoken response latency.",
        ],
    }
    if binding is not None:
        report["evaluation_binding"] = binding
    else:
        report["limitations"].append(
            "No explicit corpus hash and split binding; this report is diagnostic, "
            "not eligible for paired comparison."
        )
    return report
