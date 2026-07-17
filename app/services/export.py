"""Gold-label JSONL assembly for GET /corrections/export.

Each line carries the ticket fields plus gold labels, using the exact field
names of evals/dataset.jsonl so an exported file loads as eval data without
transformation. Gold labels come from the correction (corrected tickets) or the
machine triage (approved tickets).
"""

import json
from collections.abc import Iterator
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Correction, Ticket, Triage


def _line(ticket: Ticket, intent: str, priority: str, sentiment: str, source: str) -> str:
    return json.dumps(
        {
            "external_id": ticket.external_id,
            "channel": ticket.channel,
            "sender": ticket.sender,
            "subject": ticket.subject,
            "body": ticket.body,
            "intent": intent,
            "priority": priority,
            "sentiment": sentiment,
            "source": source,
        }
    )


def export_lines(db: Session, since: datetime | None) -> Iterator[str]:
    """Yield JSONL lines for corrected and approved tickets, oldest first.

    All rows are gathered and sorted before the first line is yielded, so the
    stream does not depend on the request session staying open.
    """
    rows: list[tuple[datetime, str]] = []

    corrected = (
        select(Ticket, Correction)
        .join(Correction, Correction.ticket_id == Ticket.id)
        .where(Ticket.status == "corrected")
    )
    for ticket, correction in db.execute(corrected):
        if since is not None and correction.created_at < since:
            continue
        rows.append(
            (
                correction.created_at,
                _line(
                    ticket,
                    correction.intent,
                    correction.priority,
                    correction.sentiment,
                    "corrected",
                ),
            )
        )

    approved = (
        select(Ticket, Triage)
        .join(Triage, Triage.ticket_id == Ticket.id)
        .where(Ticket.status == "approved")
    )
    for ticket, triage in db.execute(approved):
        if since is not None and ticket.updated_at < since:
            continue
        rows.append(
            (
                ticket.updated_at,
                _line(ticket, triage.intent, triage.priority, triage.sentiment, "approved"),
            )
        )

    rows.sort(key=lambda row: row[0])
    for _, line in rows:
        yield line + "\n"
