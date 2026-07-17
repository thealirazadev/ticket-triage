"""Ticket ingestion and reads.

Ingestion is idempotent by external_id: a replay returns the existing ticket,
never a duplicate row, even under rapid concurrent retries (the UNIQUE
constraint is the backstop). This router also hosts the response serializers
reused by the review routes.
"""

import logging
from uuid import uuid4

from fastapi import APIRouter, Depends, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.deps import get_db, require_api_key
from app.models import Correction, LlmCall, Ticket, Triage
from app.schemas.tickets import (
    CorrectionOut,
    MetricsOut,
    TicketCreateRequest,
    TicketListItem,
    TicketOut,
    TriageOut,
)

log = logging.getLogger("app.tickets")

router = APIRouter(dependencies=[Depends(require_api_key)])


def ingest_ticket(
    db: Session,
    *,
    external_id: str,
    channel: str,
    sender: str,
    subject: str,
    body: str,
) -> tuple[Ticket, bool]:
    """Insert a received ticket, or return the existing one for a known
    external_id. Returns (ticket, created)."""
    existing = db.scalars(select(Ticket).where(Ticket.external_id == external_id)).first()
    if existing is not None:
        return existing, False

    ticket = Ticket(
        id=uuid4().hex,
        external_id=external_id,
        channel=channel,
        sender=sender,
        subject=subject,
        body=body,
        status="received",
    )
    db.add(ticket)
    try:
        db.commit()
    except IntegrityError:
        # A concurrent request won the race on external_id; return that row.
        db.rollback()
        existing = db.scalars(select(Ticket).where(Ticket.external_id == external_id)).first()
        if existing is None:
            raise
        return existing, False
    db.refresh(ticket)
    log.info("ticket received", extra={"ticket_id": ticket.id, "channel": channel})
    return ticket, True


@router.post("/tickets", status_code=201)
def create_ticket(
    payload: TicketCreateRequest,
    response: Response,
    db: Session = Depends(get_db),
) -> TicketOut:
    ticket, created = ingest_ticket(
        db,
        external_id=payload.external_id,
        channel=payload.channel,
        sender=payload.sender,
        subject=payload.subject,
        body=payload.body,
    )
    if not created:
        response.status_code = 200
    return ticket_detail(db, ticket)


# --- serializers reused by the review routes ---------------------------------


def _triage_out(db: Session, ticket_id: str) -> TriageOut | None:
    row = db.scalars(select(Triage).where(Triage.ticket_id == ticket_id)).first()
    if row is None:
        return None
    return TriageOut(
        intent=row.intent,
        priority=row.priority,
        sentiment=row.sentiment,
        summary=row.summary,
        model=row.model,
        prompt_version=row.prompt_version,
        attempts=row.attempts,
        created_at=row.created_at,
    )


def _correction_out(db: Session, ticket_id: str) -> CorrectionOut | None:
    row = db.scalars(select(Correction).where(Correction.ticket_id == ticket_id)).first()
    if row is None:
        return None
    return CorrectionOut(
        intent=row.intent,
        priority=row.priority,
        sentiment=row.sentiment,
        note=row.note,
        created_at=row.created_at,
    )


def _metrics_out(db: Session, ticket_id: str) -> MetricsOut | None:
    calls = db.scalars(select(LlmCall).where(LlmCall.ticket_id == ticket_id)).all()
    if not calls:
        return None
    inputs = [c.input_tokens for c in calls if c.input_tokens is not None]
    outputs = [c.output_tokens for c in calls if c.output_tokens is not None]
    return MetricsOut(
        llm_calls=len(calls),
        input_tokens=sum(inputs) if inputs else None,
        output_tokens=sum(outputs) if outputs else None,
        cost_usd=float(sum(c.cost_usd for c in calls)),
        latency_ms=sum(c.latency_ms for c in calls),
    )


def ticket_list_item(db: Session, ticket: Ticket) -> TicketListItem:
    return TicketListItem(
        id=ticket.id,
        external_id=ticket.external_id,
        channel=ticket.channel,
        sender=ticket.sender,
        subject=ticket.subject,
        status=ticket.status,
        queue=ticket.queue,
        triage_error=ticket.triage_error,
        triage=_triage_out(db, ticket.id),
        created_at=ticket.created_at,
        updated_at=ticket.updated_at,
    )


def ticket_detail(db: Session, ticket: Ticket) -> TicketOut:
    correction = _correction_out(db, ticket.id) if ticket.status == "corrected" else None
    return TicketOut(
        id=ticket.id,
        external_id=ticket.external_id,
        channel=ticket.channel,
        sender=ticket.sender,
        subject=ticket.subject,
        body=ticket.body,
        status=ticket.status,
        queue=ticket.queue,
        triage_error=ticket.triage_error,
        triage=_triage_out(db, ticket.id),
        correction=correction,
        metrics=_metrics_out(db, ticket.id),
        created_at=ticket.created_at,
        updated_at=ticket.updated_at,
    )
