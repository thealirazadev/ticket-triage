"""Liveness and readiness probes. Both are open and never require the API key."""

import logging

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.deps import get_db

log = logging.getLogger("app.health")

router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _breaker_state(request: Request) -> str:
    """Report the worker's circuit-breaker state, or "disabled" when the worker
    is off (the API path never calls the provider itself)."""
    worker = getattr(request.app.state, "worker", None)
    if worker is None:
        return "disabled"
    return worker.breaker.state


@router.get("/ready")
def ready(request: Request, response: Response, db: Session = Depends(get_db)) -> dict[str, str]:
    """Readiness probe: verify database connectivity and report the provider
    circuit-breaker state. Returns 503 when the database is unreachable so a load
    balancer stops routing traffic; an open breaker is reported but does not fail
    readiness, since the API can still accept and queue tickets."""
    database = "ok"
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError:
        database = "unavailable"
        log.warning("readiness database check failed", exc_info=True)

    ready_ok = database == "ok"
    if not ready_ok:
        response.status_code = 503
    return {
        "status": "ready" if ready_ok else "not_ready",
        "database": database,
        "circuit_breaker": _breaker_state(request),
    }
