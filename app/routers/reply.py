"""Suggested reply drafts: fetch, edit, approve, discard.

A draft is generated once by the worker after triage (see app/services/worker).
These routes are the human-in-the-loop review of that draft. Nothing here sends
anything: approving a draft only records the human's decision.
"""

import logging

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.deps import get_db, require_api_key
from app.errors import InvalidStateError, NotFoundError
from app.models import LlmCall, ReplyDraft, Ticket, utcnow
from app.schemas.reply import ReplyDraftOut, ReplyEditRequest
from app.schemas.tickets import MetricsOut

log = logging.getLogger("app.reply")

router = APIRouter(dependencies=[Depends(require_api_key)])

# Provider-call purposes that belong to draft generation, so draft cost/latency
# is reported separately from triage.
_REPLY_PURPOSES = ("reply_draft", "reply_repair")


def _get_draft(db: Session, ticket_id: str) -> ReplyDraft:
    if db.get(Ticket, ticket_id) is None:
        raise NotFoundError("No ticket exists with that id.")
    draft = db.scalars(select(ReplyDraft).where(ReplyDraft.ticket_id == ticket_id)).first()
    if draft is None:
        raise NotFoundError("No reply draft exists for that ticket.")
    return draft


def _reply_metrics(db: Session, ticket_id: str) -> MetricsOut | None:
    calls = db.scalars(
        select(LlmCall).where(LlmCall.ticket_id == ticket_id, LlmCall.purpose.in_(_REPLY_PURPOSES))
    ).all()
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


def _draft_out(db: Session, draft: ReplyDraft) -> ReplyDraftOut:
    return ReplyDraftOut(
        ticket_id=draft.ticket_id,
        body=draft.body,
        state=draft.state,
        model=draft.model,
        prompt_version=draft.prompt_version,
        attempts=draft.attempts,
        metrics=_reply_metrics(db, draft.ticket_id),
        created_at=draft.created_at,
        updated_at=draft.updated_at,
    )


@router.get("/tickets/{ticket_id}/reply")
def get_reply_draft(ticket_id: str, db: Session = Depends(get_db)) -> ReplyDraftOut:
    """The suggested reply draft for a ticket."""
    return _draft_out(db, _get_draft(db, ticket_id))


@router.put("/tickets/{ticket_id}/reply")
def edit_reply_draft(
    ticket_id: str,
    payload: ReplyEditRequest,
    db: Session = Depends(get_db),
) -> ReplyDraftOut:
    """Replace the draft body. Valid only while the draft is unresolved."""
    draft = _get_draft(db, ticket_id)
    if draft.state != "draft":
        raise InvalidStateError("Only a draft in the draft state can be edited.")
    draft.body = payload.body
    draft.updated_at = utcnow()
    db.commit()
    log.info("reply draft edited", extra={"ticket_id": ticket_id})
    return _draft_out(db, draft)


@router.post("/tickets/{ticket_id}/reply/approve")
def approve_reply_draft(ticket_id: str, db: Session = Depends(get_db)) -> ReplyDraftOut:
    """Mark the draft approved. Never sends it; records the human's decision."""
    return _transition(db, ticket_id, "approved")


@router.post("/tickets/{ticket_id}/reply/discard")
def discard_reply_draft(ticket_id: str, db: Session = Depends(get_db)) -> ReplyDraftOut:
    """Discard the draft."""
    return _transition(db, ticket_id, "discarded")


def _transition(db: Session, ticket_id: str, target: str) -> ReplyDraftOut:
    draft = _get_draft(db, ticket_id)
    if draft.state != "draft":
        raise InvalidStateError(f"Only a draft in the draft state can be {target}.")
    draft.state = target
    draft.updated_at = utcnow()
    db.commit()
    log.info("reply draft transitioned", extra={"ticket_id": ticket_id, "state": target})
    return _draft_out(db, draft)
