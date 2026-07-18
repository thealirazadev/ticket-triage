"""Review workflow: pending queue, approve, correct, and gold-label export."""

import logging
from uuid import uuid4

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.deps import get_db, require_api_key, settings_dep
from app.errors import InvalidStateError, NotFoundError
from app.models import Correction, Ticket, utcnow
from app.routers.stats import _parse_since
from app.routers.tickets import ticket_detail, ticket_list_item
from app.schemas.reviews import CorrectRequest
from app.schemas.tickets import TicketListOut, TicketOut
from app.services.export import export_lines
from app.services.routing import load_rules, resolve_queue

log = logging.getLogger("app.reviews")

_CORRECTABLE_STATUSES = ("triaged", "needs_human")

router = APIRouter(dependencies=[Depends(require_api_key)])

_PENDING_STATUSES = ("triaged", "needs_human")


def _get_ticket(db: Session, ticket_id: str) -> Ticket:
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        raise NotFoundError("No ticket exists with that id.")
    return ticket


@router.get("/reviews/pending")
def pending_reviews(
    db: Session = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> TicketListOut:
    """Tickets awaiting review (triaged or needs_human), oldest first."""
    where = Ticket.status.in_(_PENDING_STATUSES)
    total = len(db.scalars(select(Ticket).where(where)).all())
    rows = db.scalars(
        select(Ticket).where(where).order_by(Ticket.created_at).limit(limit).offset(offset)
    ).all()
    return TicketListOut(
        tickets=[ticket_list_item(db, t) for t in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post("/tickets/{ticket_id}/approve")
def approve_ticket(ticket_id: str, db: Session = Depends(get_db)) -> TicketOut:
    """Confirm the machine triage. Valid only from triaged."""
    ticket = _get_ticket(db, ticket_id)
    if ticket.status != "triaged":
        raise InvalidStateError("Only tickets in triaged status can be approved.")
    ticket.status = "approved"
    ticket.updated_at = utcnow()
    db.commit()
    log.info("ticket approved", extra={"ticket_id": ticket.id})
    return ticket_detail(db, ticket)


@router.post("/tickets/{ticket_id}/correct")
def correct_ticket(
    ticket_id: str,
    payload: CorrectRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(settings_dep),
) -> TicketOut:
    """Store gold labels and re-route by them. Valid from triaged or needs_human."""
    ticket = _get_ticket(db, ticket_id)
    if ticket.status not in _CORRECTABLE_STATUSES:
        raise InvalidStateError("Only triaged or needs_human tickets can be corrected.")

    db.add(
        Correction(
            id=uuid4().hex,
            ticket_id=ticket.id,
            intent=payload.intent,
            priority=payload.priority,
            sentiment=payload.sentiment,
            note=payload.note,
        )
    )
    ticket.status = "corrected"
    ticket.queue = resolve_queue(
        load_rules(db),
        intent=payload.intent,
        priority=payload.priority,
        sentiment=payload.sentiment,
        default_queue=settings.default_queue,
    )
    ticket.triage_error = None
    ticket.updated_at = utcnow()
    db.commit()
    log.info("ticket corrected", extra={"ticket_id": ticket.id, "queue": ticket.queue})
    return ticket_detail(db, ticket)


@router.get("/corrections/export")
def export_corrections(
    db: Session = Depends(get_db),
    since: str | None = Query(default=None),
) -> StreamingResponse:
    """Stream gold-labeled tickets (approved + corrected) as JSONL."""
    since_dt = _parse_since(since)
    return StreamingResponse(export_lines(db, since_dt), media_type="application/x-ndjson")
