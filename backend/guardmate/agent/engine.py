import hashlib
import re
from collections.abc import Callable
from datetime import datetime, timedelta
from threading import RLock
from time import perf_counter
from uuid import uuid4

from fastapi import HTTPException

from ..models import Availability, Dashboard
from .dialogue import (
    QUESTIONS,
    apply_model_observation,
    is_waiting,
    observe_courier,
    question_from_reply,
    recover_legacy_memory,
)
from .models import (
    AgentEvent,
    AgentPlan,
    Approval,
    Conversation,
    Message,
    ResidentDecision,
    TurnRequest,
)
from .prompts import build_messages
from .provider import ModelUnavailable, PlanProvider
from .store import ConversationStore

MAX_TURNS = 20
APPROVAL_SECONDS = 90


def fingerprint(dashboard: Dashboard) -> str:
    value = dashboard.profile.model_dump_json() + dashboard.context.availability.value
    return hashlib.sha256(value.encode()).hexdigest()


class ConversationEngine:
    def __init__(
        self,
        store: ConversationStore,
        provider: PlanProvider,
        dashboard: Callable[[], Dashboard],
        clock: Callable[[], datetime],
    ):
        self.store, self.provider, self.dashboard, self.clock = store, provider, dashboard, clock
        # Single-resident, single-process prototype. Commands serialize to prevent double approvals.
        self._lock = RLock()

    def start(self) -> Conversation:
        with self._lock:
            context = self.dashboard().context
            if not context.delivery_mode_active:
                raise HTTPException(409, "Save your preferences and enable delivery mode first.")
            if not self.provider.status().configured:
                raise HTTPException(409, "Configure Qwen before starting a conversation.")
            now = self.clock()
            session = Conversation(
                id=str(uuid4()),
                created_at=now,
                dialogue_version=1,
                messages=[
                    Message(
                        role="assistant",
                        content="Hello, I'm GuardMate, the resident's AI delivery assistant. "
                        "How can I help with this delivery?",
                        at=now,
                    )
                ],
            )
            self.store.write(session)
            return session

    def read(self, session_id: str) -> Conversation:
        with self._lock:
            session = self.store.read(session_id)
            if session is None:
                raise HTTPException(404, "Conversation not found.")
            approval = session.approval
            if approval and approval.status in ("pending", "approved"):
                dashboard = self.dashboard()
                if (
                    self.clock() >= approval.expires_at
                    or approval.profile_fingerprint != fingerprint(dashboard)
                    or not dashboard.context.delivery_mode_active
                ):
                    approval.status = "expired"
                    session.status = "needs_resident"
                    session.authorized_location = None
                    self._reply(
                        session,
                        "The approval expired or the delivery plan changed. "
                        "Please do not leave the parcel; the resident needs to decide.",
                        "approval_expired",
                        "No permission inferred from silence or stale settings.",
                    )
                    self.store.write(session)
            return session

    def turn(self, session_id: str, request: TurnRequest) -> Conversation:
        with self._lock:
            session = self.read(session_id)
            self._check_revision(session, request.revision)
            if session.status in ("needs_resident", "ended"):
                raise HTTPException(
                    409, "The agent is paused. End this role-play or use resident controls."
                )
            if session.turn_count >= MAX_TURNS:
                raise HTTPException(409, "Twenty-turn limit reached. End this conversation.")
            if not self.dashboard().context.delivery_mode_active:
                raise HTTPException(409, "Delivery mode has ended. No handoff is authorized.")
            text = request.text.strip()
            if not text:
                raise HTTPException(422, "Enter a courier message.")
            recover_legacy_memory(session)
            observe_courier(session, text)
            session.messages.append(Message(role="courier", content=text, at=self.clock()))
            session.turn_count += 1
            self._detect_exceptions(session, text)
            started = perf_counter()
            try:
                plan = self.provider.generate(build_messages(self.dashboard(), session))
            except ModelUnavailable as error:
                session.status = "needs_resident"
                self._reply(
                    session,
                    "I couldn't safely continue. Please do not leave the parcel; "
                    "the resident needs to help.",
                    "model_unavailable",
                    str(error),
                )
            else:
                # Re-read resident state AFTER the potentially slow model call.
                self._execute(session, plan, text, self.dashboard())
                session.events[-1].model_action = plan.action
                session.events[-1].model_question = (
                    plan.question if plan.action == "clarify" else None
                )
                session.events[-1].model_observation = plan.observation.model_dump(
                    exclude={"evidence"}
                )
            session.events[-1].latency_ms = round((perf_counter() - started) * 1000)
            self.store.write(session)
            return session

    def decide(self, session_id: str, request: ResidentDecision) -> Conversation:
        with self._lock:
            session = self.read(session_id)
            self._check_revision(session, request.revision)
            if session.status == "ended":
                raise HTTPException(409, "This conversation has already ended.")
            if request.decision in ("end", "takeover"):
                session.status = "ended" if request.decision == "end" else "needs_resident"
                session.authorized_location = None
                if session.approval and session.approval.status in ("pending", "approved"):
                    session.approval.status = "declined"
                content = (
                    "The resident ended this role-play."
                    if request.decision == "end"
                    else "The resident will handle this delivery personally. "
                    "This text prototype does not transfer a phone call."
                )
            else:
                approval = session.approval
                if (
                    approval is None
                    or approval.status != "pending"
                    or approval.id != request.approval_id
                ):
                    raise HTTPException(409, "There is no matching pending approval.")
                approval.status = "approved" if request.decision == "approve" else "declined"
                session.status = "active" if request.decision == "approve" else "needs_resident"
                content = (
                    f"Resident UI approved once: {approval.location}. "
                    "Parcel safety checks still apply."
                    if request.decision == "approve"
                    else "Resident UI declined. Do not leave the parcel there."
                )
            session.messages.append(Message(role="resident", content=content, at=self.clock()))
            session.events.append(
                AgentEvent(action=f"resident_{request.decision}", detail=content, at=self.clock())
            )
            session.revision += 1
            self.store.write(session)
            return session

    @staticmethod
    def _check_revision(session: Conversation, revision: int) -> None:
        if session.revision != revision:
            raise HTTPException(409, "Conversation changed. Refresh it before trying again.")

    @staticmethod
    def _detect_exceptions(session: Conversation, text: str) -> None:
        # Conservative English backstop; this is not a multilingual safety guarantee.
        lowered = text.casefold()
        if re.search(r"\b(otp|one[- ]time password|verification code)\b", lowered):
            session.facts.needs_otp = True
        if re.search(r"\b(signature|sign here|sign for|sign it)\b", lowered):
            session.facts.needs_signature = True
        if re.search(
            r"\b(cod|cash on delivery|collect payment|pay me|payment required)\b", lowered
        ):
            session.facts.prepaid = False
        if re.search(r"\b(expensive|high[- ]value|valuable)\b", lowered):
            session.facts.expensive = True

    def _execute(
        self, session: Conversation, plan: AgentPlan, text: str, dashboard: Dashboard
    ) -> None:
        apply_model_observation(session, plan.observation, text)
        if not dashboard.context.delivery_mode_active:
            self._takeover(session, "Delivery mode has ended. Please do not leave the parcel.")
            return
        if session.facts.needs_otp or session.facts.needs_signature or session.facts.expensive:
            self._takeover(
                session,
                "This parcel needs the resident's personal help. "
                "I cannot provide an OTP, sign or approve a high-value delivery.",
            )
            return
        if session.facts.prepaid is False:
            self._takeover(
                session, "I handle prepaid parcels only. The resident must handle payment."
            )
            return
        if is_waiting(text, session.pending_question) and plan.action != "request_takeover":
            self._wait(session, dashboard, text)
            return
        if plan.action == "wait":
            if (
                session.pending_question == "prepaid"
                and session.facts.prepaid is True
                or session.pending_question == "guard_available"
                and session.facts.guard_available is True
            ):
                target = (
                    "approved_alternative"
                    if session.approval and session.approval.status == "approved"
                    else "guard_room"
                )
                self._handoff(session, AgentPlan(action="handoff", target=target), dashboard)
            else:
                self._wait(session, dashboard, text)
            return
        if plan.action == "request_takeover":
            self._takeover(
                session,
                "The resident needs to handle this delivery personally. "
                "Please keep the parcel with you for now.",
            )
        elif plan.action == "request_approval":
            self._request_approval(session, plan, text, dashboard)
        elif plan.action == "handoff":
            self._handoff(session, plan, dashboard)
        elif plan.action == "record_outcome":
            reported = bool(
                re.search(
                    r"\b(delivered|handed|accepted|received|returned|"
                    r"could not deliver|couldn't deliver)\b",
                    text.casefold(),
                )
            ) and not bool(re.search(r"\b(will|going to|haven't|not yet)\b", text.casefold()))
            if not reported:
                self._reply(
                    session,
                    "Has the parcel actually been received, returned, or not delivered yet?",
                    "clarify",
                    "No completed outcome reported.",
                )
            elif plan.outcome == "delivered" and session.authorized_location is None:
                self._takeover(
                    session,
                    "I cannot confirm an authorized handoff. "
                    "The resident needs to check this delivery.",
                )
            else:
                session.courier_reported_outcome = plan.outcome
                session.status = "ended"
                self._reply(
                    session,
                    "Thank you. I've recorded your report for the resident. "
                    "Receipt has not been independently verified.",
                    "record_call_outcome",
                    f"Courier reported: {plan.outcome}; not verified.",
                )
        elif plan.action == "clarify":
            answered = (
                plan.question == "prepaid"
                and session.facts.prepaid is not None
                or plan.question == "guard_available"
                and session.facts.guard_available is not None
            )
            if answered:
                # A model may propose a stale question; facts/permission checks still own progress.
                if session.facts.guard_available is False:
                    self._reply(
                        session,
                        "Security isn't available. What exact alternative place are you proposing? "
                        "I need the resident's approval before changing the handoff.",
                        "clarify",
                        "alternative_location",
                    )
                else:
                    target = (
                        "approved_alternative"
                        if session.approval and session.approval.status == "approved"
                        else "guard_room"
                    )
                    self._handoff(session, AgentPlan(action="handoff", target=target), dashboard)
                return
            replies = {
                "prepaid": "Is this parcel already paid for?",
                "guard_available": "Is security there to accept the parcel in person?",
                "alternative_location": "What exact alternative place are you proposing? "
                "I need the resident's approval before changing the handoff.",
                "delivery_question": "What do you need to know about this delivery?",
            }
            self._reply(session, replies[plan.question], "clarify", plan.question)
        else:
            self._answer(session, plan.topic, dashboard)

    def _wait(self, session: Conversation, dashboard: Dashboard, text: str) -> None:
        checking = bool(
            re.search(r"\b(let me check|i'll check|checking|one moment|hold on)\b", text.casefold())
        )
        if checking:
            reply = (
                "Sure, take your time. I'll wait. Please keep the parcel with you while you check."
            )
        elif session.pending_question == "guard_available":
            reply = (
                f"No problem. Please check for security at {dashboard.profile.guard_location}. "
                "I'll wait; "
                "don't leave the parcel unattended."
            )
        else:
            reply = "Sure, take a moment to check. I'll wait for your confirmation."
        self._reply(session, reply, "wait_for_courier", session.pending_question or "checking")

    def _handoff(self, session: Conversation, plan: AgentPlan, dashboard: Dashboard) -> None:
        if session.approval and session.approval.status == "pending":
            self._reply(
                session,
                "We are still waiting for the resident's decision. "
                "Please keep the parcel with you.",
                "handoff_blocked",
                "Resident approval is pending.",
            )
            return
        if session.authorized_location:
            self._reply(
                session,
                "The handoff instructions were already given. "
                "Has security received the parcel, or is there a problem?",
                "handoff_already_given",
                "No second authorization.",
            )
            return
        if session.facts.prepaid is not True:
            self._reply(session, "Is this parcel already paid for?", "clarify", "prepaid")
            return
        if plan.target == "approved_alternative":
            approval = session.approval
            if (
                not approval
                or approval.status != "approved"
                or self.clock() >= approval.expires_at
                or approval.profile_fingerprint != fingerprint(dashboard)
            ):
                self._reply(
                    session,
                    "I don't have a valid resident approval for another place. "
                    "Please keep the parcel with you.",
                    "handoff_blocked",
                    "Alternative lacks resident approval.",
                )
                return
            location = approval.location
            approval.status = "consumed"
            reply = f"The resident approved this handoff once: {location}. "
            reply += "Please let me know when you have handed it over."
        else:
            if dashboard.context.availability != Availability.AT_OFFICE:
                self._takeover(
                    session,
                    "The resident needs to confirm this handoff personally. "
                    "Please keep the parcel with you.",
                )
                return
            if session.facts.guard_available is False:
                self._reply(
                    session,
                    "Security isn't available. What exact alternative place "
                    "are you proposing? I need the resident's approval first.",
                    "clarify",
                    "alternative_location",
                )
                return
            if session.facts.guard_available is not True:
                self._reply(
                    session,
                    "Is security there to accept the parcel in person? "
                    "Please don't leave it unattended.",
                    "clarify",
                    "guard_available",
                )
                return
            location = dashboard.profile.guard_location
            reply = f"Please hand the prepaid parcel to security at {location}. "
            reply += "Do not leave it unattended. Please tell me once security has accepted it."
        session.authorized_location = location
        session.pending_question = None
        self._reply(session, reply, "get_handoff_options", f"Authorized once: {location}")

    def _request_approval(
        self, session: Conversation, plan: AgentPlan, text: str, dashboard: Dashboard
    ) -> None:
        if session.approval:
            self._reply(
                session,
                "There is already a resident decision for this proposal. "
                "Please wait for that decision; don't leave the parcel elsewhere.",
                "get_approval_status",
                session.approval.status,
            )
            return
        location = plan.proposed_location.strip()
        if len(location) < 3 or location.casefold() not in text.casefold():
            self._reply(
                session,
                "What exact alternative place are you proposing? I won't invent a drop location.",
                "clarify",
                "Ungrounded location rejected.",
            )
            return
        session.approval = Approval(
            id=str(uuid4()),
            location=location,
            reason=plan.reason or "Alternative handoff requested.",
            expires_at=self.clock() + timedelta(seconds=APPROVAL_SECONDS),
            profile_fingerprint=fingerprint(dashboard),
        )
        session.authorized_location = None
        session.pending_question = None
        session.status = "awaiting_approval"
        self._reply(
            session,
            "I've asked the resident to approve that location. "
            "Please keep the parcel with you while we wait. Silence is not approval.",
            "request_owner_approval",
            f"Pending for {APPROVAL_SECONDS}s: {location}",
        )

    def _answer(self, session: Conversation, topic: str, dashboard: Dashboard) -> None:
        profile, context = dashboard.profile, dashboard.context
        if topic == "identity":
            reply = (
                f"I'm the AI delivery assistant for {profile.resident_name} at {profile.pg_name}."
            )
        elif topic == "directions":
            reply = f"The saved guard location is {profile.guard_location}. "
            reply += profile.guard_directions + " " if profile.guard_directions else ""
            reply += "These are directions only; please wait for handoff instructions."
        elif topic == "approval_status":
            status = session.approval.status if session.approval else "not requested"
            reply = f"Resident approval is {status}. "
            reply += "Please don't leave the parcel until an approved handoff is given."
        else:
            replies = {
                Availability.AT_OFFICE: "The resident's saved availability is at the office. "
                "I can help arrange a prepaid handoff to security.",
                Availability.AT_PG: "The resident has marked themselves at the PG. "
                "Please contact them for personal receipt; "
                "I cannot promise they are answering now.",
                Availability.ASK_ME: "I don't have confirmed availability for the resident. "
                "Please contact them before handing over the parcel.",
            }
            reply = replies[context.availability]
        self._reply(session, reply, "get_delivery_context", topic)

    def _takeover(self, session: Conversation, reply: str) -> None:
        session.status = "needs_resident"
        session.pending_question = None
        session.authorized_location = None
        if session.approval and session.approval.status in ("pending", "approved"):
            session.approval.status = "declined"
        self._reply(session, reply, "request_takeover", "Paused for resident; no phone transfer.")

    def _reply(self, session: Conversation, reply: str, action: str, detail: str) -> None:
        now = self.clock()
        session.messages.append(Message(role="assistant", content=reply, at=now))
        session.events.append(AgentEvent(action=action, detail=detail, at=now))
        session.revision += 1
        if action == "clarify":
            session.pending_question = detail if detail in QUESTIONS else question_from_reply(reply)
