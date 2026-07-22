"""Aggregate queries for GET /stats: ticket flow counts and provider spend.

Latency percentiles are computed in memory over the recorded calls, which is
fine at this scale (documented limit).
"""

import math
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import InstrumentedAttribute, Session

from app.models import STATUSES, LlmCall, Ticket, Triage
from app.schemas.stats import LabelBreakdown, LlmStats, StatsOut, TicketCounts

_FAILURE_OUTCOMES = ("timeout", "api_error", "parse_error")


def _percentile(sorted_values: list[int], pct: float) -> int:
    if not sorted_values:
        return 0
    rank = math.ceil(pct / 100 * len(sorted_values))
    return sorted_values[max(rank - 1, 0)]


def _queue_counts(db: Session, since: datetime | None) -> dict[str, int]:
    stmt = (
        select(Ticket.queue, func.count()).where(Ticket.queue.is_not(None)).group_by(Ticket.queue)
    )
    if since is not None:
        stmt = stmt.where(Ticket.created_at >= since)
    return {queue: count for queue, count in db.execute(stmt).all()}


def _label_counts(
    db: Session, column: InstrumentedAttribute, since: datetime | None
) -> dict[str, int]:
    stmt = select(column, func.count()).group_by(column)
    if since is not None:
        stmt = stmt.where(Triage.created_at >= since)
    return {value: count for value, count in db.execute(stmt).all()}


def compute_stats(db: Session, since: datetime | None, since_raw: str | None) -> StatsOut:
    ticket_stmt = select(Ticket.status, func.count()).group_by(Ticket.status)
    if since is not None:
        ticket_stmt = ticket_stmt.where(Ticket.created_at >= since)
    counts = dict(db.execute(ticket_stmt).all())
    tickets = TicketCounts(
        received=counts.get("received", 0),
        triaged=counts.get("triaged", 0),
        needs_human=counts.get("needs_human", 0),
        approved=counts.get("approved", 0),
        corrected=counts.get("corrected", 0),
        total=sum(counts.get(status, 0) for status in STATUSES),
    )

    queues = _queue_counts(db, since)
    labels = LabelBreakdown(
        intent=_label_counts(db, Triage.intent, since),
        priority=_label_counts(db, Triage.priority, since),
        sentiment=_label_counts(db, Triage.sentiment, since),
    )

    call_stmt = select(LlmCall)
    if since is not None:
        call_stmt = call_stmt.where(LlmCall.created_at >= since)
    calls = db.scalars(call_stmt).all()
    n = len(calls)
    ok = sum(1 for c in calls if c.outcome == "ok")
    failures = sum(1 for c in calls if c.outcome in _FAILURE_OUTCOMES)
    latencies = sorted(c.latency_ms for c in calls)
    llm = LlmStats(
        calls=n,
        ok=ok,
        failures=failures,
        failure_rate=round(failures / n, 4) if n else 0.0,
        input_tokens=sum(c.input_tokens or 0 for c in calls),
        output_tokens=sum(c.output_tokens or 0 for c in calls),
        cost_usd=round(float(sum(c.cost_usd for c in calls)), 6),
        avg_latency_ms=int(sum(latencies) / n) if n else 0,
        p95_latency_ms=_percentile(latencies, 95),
    )
    return StatsOut(tickets=tickets, queues=queues, labels=labels, llm=llm, since=since_raw)
