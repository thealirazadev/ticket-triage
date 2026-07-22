"""The single background worker: poll for received tickets and triage them.

The `received` status is the queue: on restart, unprocessed tickets are picked
up again. One poisoned ticket is contained (logged, skipped) and never kills the
loop. There is no asyncio and no broker; the loop is a plain daemon thread.
"""

import logging
import threading
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.models import Ticket, Triage, utcnow
from app.services.classifier import TriageFailure, classify_ticket
from app.services.llm_client import CircuitBreaker, LlmClient
from app.services.routing import load_rules, resolve_queue

log = logging.getLogger("app.worker")


def process_next_ticket(db: Session, client: LlmClient, settings: Settings) -> bool:
    """Triage the oldest received ticket. Returns True if one was processed.

    A per-ticket failure sets needs_human with a triage_error and is not
    re-raised, so the caller's loop keeps running.
    """
    ticket = db.scalars(
        select(Ticket).where(Ticket.status == "received").order_by(Ticket.created_at).limit(1)
    ).first()
    if ticket is None:
        return False

    try:
        outcome = classify_ticket(
            db,
            client,
            ticket_id=ticket.id,
            subject=ticket.subject,
            sender=ticket.sender,
            channel=ticket.channel,
            body=ticket.body,
        )
    except TriageFailure as failure:
        ticket.status = "needs_human"
        ticket.queue = settings.default_queue
        ticket.triage_error = failure.reason
        ticket.updated_at = utcnow()
        db.commit()
        log.info(
            "ticket needs human",
            extra={"ticket_id": ticket.id, "error_code": failure.reason},
        )
        return True
    except Exception:
        # Unexpected failure: roll back and leave the ticket for a later poll,
        # but do not let one ticket take down the worker.
        db.rollback()
        log.exception("ticket processing error", extra={"ticket_id": ticket.id})
        return True

    triage = Triage(
        id=uuid4().hex,
        ticket_id=ticket.id,
        intent=outcome.result.intent,
        priority=outcome.result.priority,
        sentiment=outcome.result.sentiment,
        summary=outcome.result.summary,
        model=settings.llm_model,
        prompt_version=_prompt_version(),
        attempts=outcome.attempts,
    )
    db.add(triage)
    queue = resolve_queue(
        load_rules(db),
        intent=outcome.result.intent,
        priority=outcome.result.priority,
        sentiment=outcome.result.sentiment,
        default_queue=settings.default_queue,
    )
    ticket.status = "triaged"
    ticket.queue = queue
    ticket.triage_error = None
    ticket.updated_at = utcnow()
    db.commit()
    log.info(
        "ticket triaged",
        extra={"ticket_id": ticket.id, "queue": queue, "attempts": outcome.attempts},
    )
    return True


def _prompt_version() -> str:
    from app.prompts import PROMPT_VERSION

    return PROMPT_VERSION


class Worker:
    """Owns the polling thread and the shared provider client."""

    def __init__(self, settings: Settings, session_factory: sessionmaker[Session]):
        self._settings = settings
        self._session_factory = session_factory
        self._client = LlmClient(settings)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def breaker(self) -> CircuitBreaker:
        """The shared provider client's circuit breaker, exposed for the
        readiness probe."""
        return self._client.breaker

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="triage-worker", daemon=True)
        self._thread.start()
        log.info("worker started", extra={"poll_seconds": self._settings.worker_poll_seconds})

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._settings.worker_poll_seconds + 5)
        self._client.close()
        log.info("worker stopped")

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                with self._session_factory() as db:
                    processed = process_next_ticket(db, self._client, self._settings)
            except Exception:
                # A loop-level failure (e.g. DB unavailable) must not kill the
                # thread silently; log and back off one interval.
                log.critical("worker loop error", exc_info=True)
                processed = False
            if not processed:
                self._stop.wait(self._settings.worker_poll_seconds)
