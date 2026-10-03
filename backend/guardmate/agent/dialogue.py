"""Grounded dialogue memory; no permission is granted by this module."""

import re
from typing import Literal

from .models import Conversation, Observation, ParcelFacts

Question = Literal["prepaid", "guard_available", "alternative_location"]

QUESTIONS: dict[Question, str] = {
    "prepaid": "Is this parcel already paid for?",
    "guard_available": "Is security there to accept the parcel in person?",
    "alternative_location": "What exact alternative place are you proposing?",
}


def normalize(text: str) -> str:
    return text.casefold().replace("’", "'").strip()


def question_from_reply(text: str) -> Question | None:
    lowered = normalize(text)
    return next(
        (field for field, question in QUESTIONS.items() if question.casefold() in lowered), None
    )


def is_waiting(text: str, question: Question | None) -> bool:
    return question is not None and bool(
        re.search(
            r"\b(don't know|do not know|not sure|no idea|let me check|i'll check|"
            r"i will check|checking|give me a moment|one moment|hold on)\b",
            normalize(text),
        )
    )


def confirmation(field: str, evidence: str, question: Question | None) -> bool | None:
    lowered = normalize(evidence)
    relevant_question = question == ("prepaid" if field == "prepaid" else "guard_available")
    if relevant_question and is_waiting(lowered, question):
        return None
    if "?" in lowered or re.search(r"\b(will|going to|might|maybe|should be)\b", lowered):
        return None
    if field == "prepaid":
        if re.search(
            r"\b(not (?:prepaid|already paid|paid for)|unpaid|cod|cash on delivery)\b", lowered
        ):
            return False
        if re.search(r"\b(prepaid|already paid|paid for)\b", lowered):
            # 'not sure whether prepaid' is not a confirmation.
            if re.search(r"\b(not sure|don't know|do not know|is it|whether|if)\b", lowered):
                return None
            return True
    else:
        guard_named = bool(re.search(r"\b(guard|security)\b", lowered))
        if guard_named or relevant_question:
            if re.search(
                r"\b(not here|not there|not available|not present|no (?:a )?guard|"
                r"no security|nobody|no one|absent|unavailable|"
                r"(?:isn't|aren't|wasn't) (?:here|there|available|present|"
                r"a guard|a security guard))\b",
                lowered,
            ):
                return False
            if re.search(
                r"\b(not sure|don't know|do not know|whether|if|is he|is security)\b", lowered
            ):
                return None
            if re.search(r"\b(here|there|present|available)\b", lowered):
                return True
    if relevant_question:
        # Resolve pronouns/short answers against the outstanding question, not keywords alone.
        if re.fullmatch(
            r"(yes|yeah|yep|correct|that's right|yes (?:he|she|they) (?:is|are) there)[.! ]*",
            lowered,
        ):
            return True
        if re.fullmatch(r"(no|nope|not yet)[.! ]*", lowered):
            return False
    return None


def observe_courier(session: Conversation, text: str) -> bool:
    """Commit clear answers BEFORE the model sees context. Return whether caller is checking."""
    question = session.pending_question
    waiting = is_waiting(text, question)
    for field in ("prepaid", "guard_available"):
        value = confirmation(field, text, question)
        if value is not None:
            if field != "prepaid" or session.facts.prepaid is not False:
                setattr(session.facts, field, value)
            if field == "guard_available" and value is False:
                session.authorized_location = None
        elif waiting and question == field:
            # Unknown does not mean 'no'; withdraw a stale positive confirmation.
            if field != "prepaid" or session.facts.prepaid is not False:
                setattr(session.facts, field, None)
            session.authorized_location = None
    return waiting


def apply_model_observation(session: Conversation, observation: Observation, text: str) -> None:
    evidence = observation.evidence.strip()
    if not evidence or normalize(evidence) not in normalize(text):
        return
    for field in ("prepaid", "guard_available"):
        value = getattr(observation, field)
        if value is None or confirmation(field, evidence, session.pending_question) != value:
            continue
        if field != "prepaid" or session.facts.prepaid is not False:
            setattr(session.facts, field, value)
        if field == "guard_available" and value is False:
            session.authorized_location = None
    session.facts.needs_otp |= observation.needs_otp
    session.facts.needs_signature |= observation.needs_signature
    session.facts.expensive |= observation.expensive


def recover_legacy_memory(session: Conversation) -> None:
    """Recover clear answers from old broken sessions without inventing resident approval."""
    if session.dialogue_version >= 1:
        return
    recovered = Conversation(
        id=session.id,
        created_at=session.created_at,
        facts=ParcelFacts.model_validate(session.facts.model_dump()),
    )
    for message in session.messages:
        if message.role == "assistant":
            question = question_from_reply(message.content)
            if question:
                recovered.pending_question = question
        elif message.role == "courier":
            observe_courier(recovered, message.content)
    session.facts = recovered.facts
    session.pending_question = recovered.pending_question
    session.dialogue_version = 1
