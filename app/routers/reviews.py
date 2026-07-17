"""Review workflow: pending queue, approve, correct, and gold-label export."""

import logging

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.deps import get_db, require_api_key
from app.models import Ticket
from app.routers.tickets import ticket_list_item
from app.schemas.tickets import TicketListOut

log = logging.getLogger("app.reviews")

router = APIRouter(dependencies=[Depends(require_api_key)])

_PENDING_STATUSES = ("triaged", "needs_human")


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
