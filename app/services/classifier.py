"""Structured-output discipline: turn a ticket into a validated TriageResult, or
fail cleanly to needs_human.

The provider text enters the system only through TriageResult. On a parse or
schema failure the classifier makes exactly one repair call; a second failure
raises TriageFailure and no triage is ever persisted. Invalid model output is
logged truncated, never stored.
"""

import json
import logging
from dataclasses import dataclass

from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.orm import Session

from app.prompts import (
    build_classify_messages,
    build_repair_messages,
    build_reply_messages,
    build_reply_repair_messages,
)
from app.schemas.reply import ReplyDraftResult
from app.schemas.triage import TriageResult
from app.services.llm_client import CircuitOpenError, LlmClient, LlmResult, ProviderError

log = logging.getLogger("app.classify")

_SNIPPET_MAX = 500


class TriageFailure(Exception):
    """Triage could not be produced. reason is a tickets.triage_error value."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason  # "parse_failed" | "provider_error" | "circuit_open"


class ReplyDraftFailure(Exception):
    """A reply draft could not be produced. reason mirrors TriageFailure; the
    caller leaves no draft (a human writes one) rather than storing garbage."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason  # "parse_failed" | "provider_error" | "circuit_open"


@dataclass
class TriageOutcome:
    result: TriageResult
    attempts: int  # 1 = clean parse, 2 = repair call was needed


@dataclass
class ReplyDraftOutcome:
    result: ReplyDraftResult
    attempts: int  # 1 = clean parse, 2 = repair call was needed


def parse_triage(content: str) -> tuple[TriageResult | None, str]:
    """Parse provider text into a TriageResult. Returns (result, "") on success
    or (None, error_description) on any parse/validation failure."""
    raw = _strip_fences(content)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON: {exc}"
    if not isinstance(data, dict):
        return None, "response was not a JSON object"
    try:
        return TriageResult.model_validate(data), ""
    except PydanticValidationError as exc:
        return None, _format_validation_errors(exc)


def parse_reply(content: str) -> tuple[ReplyDraftResult | None, str]:
    """Parse provider text into a ReplyDraftResult. Returns (result, "") on
    success or (None, error_description) on any parse/validation failure."""
    raw = _strip_fences(content)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON: {exc}"
    if not isinstance(data, dict):
        return None, "response was not a JSON object"
    try:
        return ReplyDraftResult.model_validate(data), ""
    except PydanticValidationError as exc:
        return None, _format_validation_errors(exc)


def classify_ticket(
    db: Session,
    client: LlmClient,
    *,
    ticket_id: str | None,
    subject: str,
    sender: str,
    channel: str,
    body: str,
) -> TriageOutcome:
    """Classify one ticket. Raises TriageFailure on provider, circuit, or
    double-parse failure."""
    messages = build_classify_messages(subject, sender, channel, body)
    first = _call(db, client, messages, "classify", ticket_id)

    parsed, errors = parse_triage(first.content)
    if parsed is not None:
        return TriageOutcome(parsed, attempts=1)

    # First response failed validation: mark the recorded call and repair once.
    first.call.outcome = "parse_error"
    log.warning(
        "triage parse failed",
        extra={"ticket_id": ticket_id, "attempt": 1, "snippet": first.content[:_SNIPPET_MAX]},
    )
    repair_messages = build_repair_messages(subject, sender, channel, body, first.content, errors)
    second = _call(db, client, repair_messages, "repair", ticket_id)

    parsed, errors = parse_triage(second.content)
    if parsed is not None:
        return TriageOutcome(parsed, attempts=2)

    second.call.outcome = "parse_error"
    log.warning(
        "triage parse failed",
        extra={"ticket_id": ticket_id, "attempt": 2, "snippet": second.content[:_SNIPPET_MAX]},
    )
    raise TriageFailure("parse_failed")


def generate_reply_draft(
    db: Session,
    client: LlmClient,
    *,
    ticket_id: str | None,
    subject: str,
    sender: str,
    channel: str,
    body: str,
    intent: str,
    priority: str,
    sentiment: str,
    summary: str,
) -> ReplyDraftOutcome:
    """Draft a suggested reply grounded in the ticket and its triage. Same
    structured-output discipline as classify_ticket: strict parse -> one repair
    -> ReplyDraftFailure. Raises ReplyDraftFailure on provider, circuit, or
    double-parse failure so the caller can leave no draft."""
    messages = build_reply_messages(
        subject, sender, channel, body, intent, priority, sentiment, summary
    )
    first = _reply_call(db, client, messages, "reply_draft", ticket_id)

    parsed, errors = parse_reply(first.content)
    if parsed is not None:
        return ReplyDraftOutcome(parsed, attempts=1)

    first.call.outcome = "parse_error"
    log.warning(
        "reply draft parse failed",
        extra={"ticket_id": ticket_id, "attempt": 1, "snippet": first.content[:_SNIPPET_MAX]},
    )
    repair_messages = build_reply_repair_messages(
        subject, sender, channel, body, intent, priority, sentiment, summary, first.content, errors
    )
    second = _reply_call(db, client, repair_messages, "reply_repair", ticket_id)

    parsed, errors = parse_reply(second.content)
    if parsed is not None:
        return ReplyDraftOutcome(parsed, attempts=2)

    second.call.outcome = "parse_error"
    log.warning(
        "reply draft parse failed",
        extra={"ticket_id": ticket_id, "attempt": 2, "snippet": second.content[:_SNIPPET_MAX]},
    )
    raise ReplyDraftFailure("parse_failed")


def _call(
    db: Session, client: LlmClient, messages: list[dict], purpose: str, ticket_id: str | None
) -> LlmResult:
    try:
        return client.complete(db, messages, purpose=purpose, ticket_id=ticket_id)
    except CircuitOpenError as exc:
        raise TriageFailure("circuit_open") from exc
    except ProviderError as exc:
        raise TriageFailure("provider_error") from exc


def _reply_call(
    db: Session, client: LlmClient, messages: list[dict], purpose: str, ticket_id: str | None
) -> LlmResult:
    try:
        return client.complete(db, messages, purpose=purpose, ticket_id=ticket_id)
    except CircuitOpenError as exc:
        raise ReplyDraftFailure("circuit_open") from exc
    except ProviderError as exc:
        raise ReplyDraftFailure("provider_error") from exc


def _strip_fences(content: str) -> str:
    text = content.strip()
    if text.startswith("```"):
        text = text[3:]
        if text[:4].lower() == "json":
            text = text[4:]
        if text.endswith("```"):
            text = text[:-3]
    return text.strip()


def _format_validation_errors(exc: PydanticValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()))
        parts.append(f"{loc}: {err.get('msg')}")
    return "; ".join(parts)
