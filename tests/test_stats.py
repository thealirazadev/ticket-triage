"""GET /stats: aggregates match direct queries, since filter, zeros on empty."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.models import LlmCall, Ticket, Triage


def _ticket(status, created_at=None):
    return Ticket(
        id=uuid4().hex,
        external_id=uuid4().hex,
        channel="web",
        sender="a@b.com",
        subject="s",
        body="b",
        status=status,
        created_at=created_at or datetime.now(UTC),
    )


def _ticket_with_triage(
    db,
    *,
    queue="general",
    intent="billing",
    priority="P2",
    sentiment="neutral",
    created_at=None,
):
    ts = created_at or datetime.now(UTC)
    ticket = Ticket(
        id=uuid4().hex,
        external_id=uuid4().hex,
        channel="web",
        sender="a@b.com",
        subject="s",
        body="b",
        status="triaged",
        queue=queue,
        created_at=ts,
    )
    db.add(ticket)
    db.flush()  # persist the ticket before its FK-bound triage
    db.add(
        Triage(
            id=uuid4().hex,
            ticket_id=ticket.id,
            intent=intent,
            priority=priority,
            sentiment=sentiment,
            summary="x",
            model="m",
            prompt_version="1",
            created_at=ts,
        )
    )
    return ticket


def _call(outcome, latency, inp=None, out=None, cost=0, created_at=None):
    return LlmCall(
        id=uuid4().hex,
        ticket_id=None,
        purpose="classify",
        model="example-model-id",
        input_tokens=inp,
        output_tokens=out,
        cost_usd=cost,
        latency_ms=latency,
        outcome=outcome,
        created_at=created_at or datetime.now(UTC),
    )


def test_empty_database_returns_zeros(client):
    body = client.get("/stats").json()
    assert body["tickets"]["total"] == 0
    assert body["llm"] == {
        "calls": 0,
        "ok": 0,
        "failures": 0,
        "failure_rate": 0.0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": 0.0,
        "avg_latency_ms": 0,
        "p95_latency_ms": 0,
    }
    assert body["since"] is None


def test_aggregates_match_expected(client, db):
    for status in ["received", "triaged", "triaged", "approved", "needs_human", "corrected"]:
        db.add(_ticket(status))
    for outcome, latency in [
        ("ok", 100),
        ("ok", 200),
        ("ok", 300),
        ("timeout", 400),
        ("parse_error", 500),
    ]:
        db.add(_call(outcome, latency, inp=10, out=5, cost=0.001))
    db.commit()

    body = client.get("/stats").json()
    assert body["tickets"] == {
        "received": 1,
        "triaged": 2,
        "needs_human": 1,
        "approved": 1,
        "corrected": 1,
        "total": 6,
    }
    llm = body["llm"]
    assert llm["calls"] == 5
    assert llm["ok"] == 3
    assert llm["failures"] == 2  # timeout + parse_error
    assert llm["failure_rate"] == 0.4
    assert llm["input_tokens"] == 50
    assert llm["output_tokens"] == 25
    assert llm["cost_usd"] == 0.005
    assert llm["avg_latency_ms"] == 300  # (100+200+300+400+500)/5
    assert llm["p95_latency_ms"] == 500  # nearest-rank over 5 values


def test_since_filter(client, db):
    old = datetime.now(UTC) - timedelta(days=2)
    recent = datetime.now(UTC)
    db.add(_ticket("triaged", created_at=old))
    db.add(_ticket("triaged", created_at=recent))
    db.add(_call("ok", 100, created_at=old))
    db.add(_call("ok", 200, created_at=recent))
    db.commit()

    cutoff = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    body = client.get("/stats", params={"since": cutoff}).json()
    assert body["tickets"]["triaged"] == 1
    assert body["llm"]["calls"] == 1
    assert body["since"] == cutoff


def test_future_since_returns_zeros(client, db):
    db.add(_ticket("triaged"))
    db.commit()
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    body = client.get("/stats", params={"since": future}).json()
    assert body["tickets"]["total"] == 0
    assert body["llm"]["calls"] == 0


def test_bad_since_returns_422(client):
    response = client.get("/stats", params={"since": "not-a-date"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_empty_breakdown_is_empty(client):
    body = client.get("/stats").json()
    assert body["queues"] == {}
    assert body["labels"] == {"intent": {}, "priority": {}, "sentiment": {}}


def test_queue_and_label_breakdown(client, db):
    _ticket_with_triage(db, queue="urgent", intent="bug", priority="P1", sentiment="negative")
    _ticket_with_triage(db, queue="urgent", intent="bug", priority="P2", sentiment="negative")
    _ticket_with_triage(
        db, queue="security", intent="account_access", priority="P2", sentiment="neutral"
    )
    db.add(_ticket("received"))  # no queue: excluded from the queue breakdown
    db.commit()

    body = client.get("/stats").json()
    assert body["queues"] == {"urgent": 2, "security": 1}
    assert body["labels"]["intent"] == {"bug": 2, "account_access": 1}
    assert body["labels"]["priority"] == {"P1": 1, "P2": 2}
    assert body["labels"]["sentiment"] == {"negative": 2, "neutral": 1}


def test_breakdown_respects_since(client, db):
    old = datetime.now(UTC) - timedelta(days=2)
    recent = datetime.now(UTC)
    _ticket_with_triage(db, queue="urgent", intent="bug", created_at=old)
    _ticket_with_triage(db, queue="security", intent="billing", created_at=recent)
    db.commit()

    cutoff = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    body = client.get("/stats", params={"since": cutoff}).json()
    assert body["queues"] == {"security": 1}
    assert body["labels"]["intent"] == {"billing": 1}
