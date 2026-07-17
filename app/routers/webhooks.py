"""Generic email-webhook ingestion, mapped onto the same ticket shape.

Unknown extra fields are ignored (webhook providers add their own); an empty or
missing subject is stored as "(no subject)". Channel is fixed to "email".
"""

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from app.deps import get_db, require_api_key
from app.routers.tickets import ingest_ticket, ticket_detail
from app.schemas.tickets import EmailWebhookRequest, TicketOut

router = APIRouter(dependencies=[Depends(require_api_key)])

_NO_SUBJECT = "(no subject)"


@router.post("/webhooks/email", status_code=201)
def ingest_email(
    payload: EmailWebhookRequest,
    response: Response,
    db: Session = Depends(get_db),
) -> TicketOut:
    subject = payload.subject if payload.subject else _NO_SUBJECT
    ticket, created = ingest_ticket(
        db,
        external_id=payload.message_id,
        channel="email",
        sender=payload.sender,
        subject=subject,
        body=payload.text,
    )
    if not created:
        response.status_code = 200
    return ticket_detail(db, ticket)
