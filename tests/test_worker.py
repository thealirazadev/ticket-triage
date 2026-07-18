"""Worker: received -> triaged, needs_human fallbacks, containment, resume."""

import httpx

from app.models import Ticket, Triage
from app.services.llm_client import LlmClient
from app.services.worker import process_next_ticket
from tests.conftest import chat_response, triage_json


def _received_ticket(db, external_id="w-1", **overrides):
    fields = {
        "id": external_id.replace("-", ""),
        "external_id": external_id,
        "channel": "web",
        "sender": "a@b.com",
        "subject": "Locked out",
        "body": "I reset my password and cannot log in.",
        "status": "received",
    }
    fields.update(overrides)
    ticket = Ticket(**fields)
    db.add(ticket)
    db.commit()
    return ticket


def test_received_becomes_triaged_with_queue(db, settings, make_llm_client):
    ticket = _received_ticket(db)
    client = make_llm_client(
        lambda _r: httpx.Response(
            200, json=chat_response(triage_json(intent="account_access", priority="P2"))
        )
    )
    processed = process_next_ticket(db, client, settings)
    assert processed is True

    db.refresh(ticket)
    assert ticket.status == "triaged"
    assert ticket.queue == "security"  # account_access -> security seeded rule
    assert ticket.triage_error is None
    triage = db.query(Triage).filter_by(ticket_id=ticket.id).one()
    assert triage.intent == "account_access"
    assert triage.attempts == 1
    assert triage.prompt_version == "1"


def test_p1_routes_to_urgent(db, settings, make_llm_client):
    ticket = _received_ticket(db)
    client = make_llm_client(
        lambda _r: httpx.Response(200, json=chat_response(triage_json(intent="bug", priority="P1")))
    )
    process_next_ticket(db, client, settings)
    db.refresh(ticket)
    assert ticket.queue == "urgent"


def test_no_match_routes_to_default_queue(db, settings, make_llm_client):
    ticket = _received_ticket(db)
    client = make_llm_client(
        lambda _r: httpx.Response(
            200, json=chat_response(triage_json(intent="other", priority="P4"))
        )
    )
    process_next_ticket(db, client, settings)
    db.refresh(ticket)
    assert ticket.queue == "general"


def test_parse_failure_sets_needs_human(db, settings, make_llm_client):
    ticket = _received_ticket(db)
    client = make_llm_client(lambda _r: httpx.Response(200, json=chat_response("garbage")))
    process_next_ticket(db, client, settings)

    db.refresh(ticket)
    assert ticket.status == "needs_human"
    assert ticket.queue == "general"
    assert ticket.triage_error == "parse_failed"
    assert db.query(Triage).filter_by(ticket_id=ticket.id).first() is None


def test_provider_error_sets_needs_human(db, settings, make_llm_client, no_backoff):
    ticket = _received_ticket(db)
    client = make_llm_client(lambda _r: httpx.Response(500))
    process_next_ticket(db, client, settings)

    db.refresh(ticket)
    assert ticket.status == "needs_human"
    assert ticket.triage_error == "provider_error"


def test_circuit_open_sets_needs_human_without_call(db, settings):
    ticket = _received_ticket(db)
    calls = {"n": 0}

    def handler(_request):
        calls["n"] += 1
        return httpx.Response(500)

    client = LlmClient(settings, transport=httpx.MockTransport(handler))
    # Force the breaker open so no provider call is made.
    client.breaker.record_failure()
    client.breaker._open_until = float("inf")
    try:
        process_next_ticket(db, client, settings)
    finally:
        client.close()

    db.refresh(ticket)
    assert ticket.status == "needs_human"
    assert ticket.triage_error == "circuit_open"
    assert calls["n"] == 0


def test_no_received_ticket_returns_false(db, settings, make_llm_client):
    client = make_llm_client(lambda _r: httpx.Response(200, json=chat_response(triage_json())))
    assert process_next_ticket(db, client, settings) is False


def test_oldest_first_and_resume(db, settings, make_llm_client):
    first = _received_ticket(db, external_id="old-1")
    second = _received_ticket(db, external_id="new-2")
    client = make_llm_client(lambda _r: httpx.Response(200, json=chat_response(triage_json())))

    process_next_ticket(db, client, settings)
    db.refresh(first)
    db.refresh(second)
    assert first.status == "triaged"
    assert second.status == "received"  # still queued

    # A subsequent poll picks up the remaining received ticket (restart-resume).
    process_next_ticket(db, client, settings)
    db.refresh(second)
    assert second.status == "triaged"
