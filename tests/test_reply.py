"""Suggested reply drafts: parsing, generation, worker storage, malformed
fallback, edit/approve/discard transitions, and draft cost accounting."""

import json
from uuid import uuid4

import httpx
import pytest

from app.models import LlmCall, ReplyDraft, Ticket
from app.schemas.reply import ReplyDraftResult
from app.services.classifier import ReplyDraftFailure, generate_reply_draft, parse_reply
from app.services.worker import process_next_ticket
from tests.conftest import chat_response, triage_json


def reply_json(
    text: str = "Thanks for reaching out. We are looking into your account access.",
) -> str:
    return json.dumps({"reply": text})


def _sequence_handler(bodies):
    """Return each body in turn as a 200 chat response; last body repeats."""
    state = {"i": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        body = bodies[min(state["i"], len(bodies) - 1)]
        state["i"] += 1
        return httpx.Response(200, json=chat_response(body))

    return handler


def _generate(db, client):
    return generate_reply_draft(
        db,
        client,
        ticket_id=None,
        subject="Locked out",
        sender="a@b.com",
        channel="web",
        body="I reset my password and cannot log in.",
        intent="account_access",
        priority="P2",
        sentiment="negative",
        summary="Customer is locked out after a password reset.",
    )


# --- parse_reply --------------------------------------------------------------


def test_parse_reply_clean():
    result, error = parse_reply(reply_json("Hello there."))
    assert error == ""
    assert isinstance(result, ReplyDraftResult)
    assert result.reply == "Hello there."


def test_parse_reply_strips_fences_and_whitespace():
    fenced = "```json\n" + json.dumps({"reply": "  Hi Dana  "}) + "\n```"
    result, error = parse_reply(fenced)
    assert error == ""
    assert result.reply == "Hi Dana"


def test_parse_reply_rejects_non_json():
    result, error = parse_reply("not json at all")
    assert result is None
    assert "JSON" in error


def test_parse_reply_rejects_missing_key():
    result, error = parse_reply('{"message": "hi"}')
    assert result is None
    assert "reply" in error


def test_parse_reply_rejects_too_long():
    result, _ = parse_reply(reply_json("x" * 2001))
    assert result is None


# --- generate_reply_draft -----------------------------------------------------


def test_clean_draft_attempts_one_records_cost(db, make_llm_client):
    client = make_llm_client(_sequence_handler([reply_json("We can help with that.")]))
    outcome = _generate(db, client)
    db.commit()
    assert outcome.attempts == 1
    assert outcome.result.reply == "We can help with that."

    row = db.query(LlmCall).one()
    assert row.purpose == "reply_draft"
    assert row.outcome == "ok"
    # 100/1e6 * 1.0 + 20/1e6 * 2.0 = 0.00014 (conftest prices and token counts)
    assert float(row.cost_usd) == pytest.approx(0.00014)
    assert row.latency_ms >= 0


def test_repair_succeeds_attempts_two(db, make_llm_client):
    client = make_llm_client(_sequence_handler(["garbage", reply_json("Fixed reply.")]))
    outcome = _generate(db, client)
    db.commit()
    assert outcome.attempts == 2
    assert outcome.result.reply == "Fixed reply."

    rows = db.query(LlmCall).order_by(LlmCall.created_at).all()
    assert [r.purpose for r in rows] == ["reply_draft", "reply_repair"]
    assert sorted(r.outcome for r in rows) == ["ok", "parse_error"]


def test_double_failure_raises_and_leaves_no_draft(db, make_llm_client):
    client = make_llm_client(_sequence_handler(["garbage", "still garbage"]))
    with pytest.raises(ReplyDraftFailure) as excinfo:
        _generate(db, client)
    db.commit()
    assert excinfo.value.reason == "parse_failed"

    rows = db.query(LlmCall).all()
    assert len(rows) == 2
    assert all(r.outcome == "parse_error" for r in rows)
    assert db.query(ReplyDraft).count() == 0


def test_provider_error_raises_provider_failure(db, make_llm_client, no_backoff):
    client = make_llm_client(lambda _r: httpx.Response(500))
    with pytest.raises(ReplyDraftFailure) as excinfo:
        _generate(db, client)
    assert excinfo.value.reason == "provider_error"


# --- worker integration -------------------------------------------------------


def _received(db, ext="r-1"):
    ticket = Ticket(
        id=uuid4().hex,
        external_id=ext,
        channel="web",
        sender="a@b.com",
        subject="Locked out",
        body="I reset my password and cannot log in.",
        status="received",
    )
    db.add(ticket)
    db.commit()
    return ticket


def test_worker_generates_and_stores_draft(db, settings, make_llm_client):
    ticket = _received(db)
    client = make_llm_client(_sequence_handler([triage_json(), reply_json("Hello, we can help.")]))
    assert process_next_ticket(db, client, settings) is True

    db.refresh(ticket)
    assert ticket.status == "triaged"
    draft = db.query(ReplyDraft).filter_by(ticket_id=ticket.id).one()
    assert draft.state == "draft"
    assert draft.body == "Hello, we can help."
    assert draft.prompt_version == "1"
    assert draft.attempts == 1

    reply_calls = db.query(LlmCall).filter_by(ticket_id=ticket.id, purpose="reply_draft").all()
    assert len(reply_calls) == 1
    assert reply_calls[0].outcome == "ok"


def test_worker_malformed_draft_leaves_ticket_triaged_no_draft(db, settings, make_llm_client):
    ticket = _received(db)
    client = make_llm_client(_sequence_handler([triage_json(), "garbage", "still garbage"]))
    assert process_next_ticket(db, client, settings) is True

    db.refresh(ticket)
    assert ticket.status == "triaged"  # triage unaffected by a draft failure
    assert ticket.triage_error is None
    assert db.query(ReplyDraft).filter_by(ticket_id=ticket.id).first() is None

    # The failed draft attempts are still recorded for cost accounting.
    reply_calls = (
        db.query(LlmCall)
        .filter(
            LlmCall.ticket_id == ticket.id, LlmCall.purpose.in_(("reply_draft", "reply_repair"))
        )
        .all()
    )
    assert len(reply_calls) == 2
    assert all(c.outcome == "parse_error" for c in reply_calls)


# --- endpoints ----------------------------------------------------------------


def _ticket_with_draft(db, ext="d-1", state="draft", body="Draft reply text."):
    ticket = Ticket(
        id=uuid4().hex,
        external_id=ext,
        channel="web",
        sender="a@b.com",
        subject="s",
        body="b",
        status="triaged",
        queue="general",
    )
    db.add(ticket)
    db.flush()
    db.add(
        ReplyDraft(
            id=uuid4().hex,
            ticket_id=ticket.id,
            body=body,
            state=state,
            model="example-model-id",
            prompt_version="1",
            attempts=1,
        )
    )
    db.commit()
    return ticket


def test_get_draft_returns_body_and_state(client, db):
    ticket = _ticket_with_draft(db)
    response = client.get(f"/tickets/{ticket.id}/reply")
    assert response.status_code == 200
    body = response.json()
    assert body["ticket_id"] == ticket.id
    assert body["state"] == "draft"
    assert body["body"] == "Draft reply text."
    assert body["metrics"] is None  # no llm_calls recorded for this fixture


def test_get_draft_unknown_ticket_is_404(client):
    assert client.get("/tickets/nope/reply").status_code == 404


def test_get_draft_ticket_without_draft_is_404(client, db):
    ticket = Ticket(
        id=uuid4().hex,
        external_id="nd-1",
        channel="web",
        sender="a@b.com",
        subject="s",
        body="b",
        status="received",
    )
    db.add(ticket)
    db.commit()
    response = client.get(f"/tickets/{ticket.id}/reply")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_edit_draft_replaces_body(client, db):
    ticket = _ticket_with_draft(db)
    response = client.put(f"/tickets/{ticket.id}/reply", json={"body": "Edited reply."})
    assert response.status_code == 200
    assert response.json()["body"] == "Edited reply."
    assert response.json()["state"] == "draft"


def test_edit_blank_body_is_422(client, db):
    ticket = _ticket_with_draft(db)
    assert client.put(f"/tickets/{ticket.id}/reply", json={"body": "   "}).status_code == 422


def test_edit_oversized_body_is_422(client, db):
    ticket = _ticket_with_draft(db)
    assert client.put(f"/tickets/{ticket.id}/reply", json={"body": "x" * 2001}).status_code == 422


def test_edit_after_approve_is_409(client, db):
    ticket = _ticket_with_draft(db, state="approved")
    response = client.put(f"/tickets/{ticket.id}/reply", json={"body": "late edit"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "invalid_state"


def test_approve_transitions_to_approved(client, db):
    ticket = _ticket_with_draft(db)
    response = client.post(f"/tickets/{ticket.id}/reply/approve")
    assert response.status_code == 200
    assert response.json()["state"] == "approved"


def test_approve_again_is_409(client, db):
    ticket = _ticket_with_draft(db)
    client.post(f"/tickets/{ticket.id}/reply/approve")
    again = client.post(f"/tickets/{ticket.id}/reply/approve")
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "invalid_state"


def test_discard_transitions_to_discarded(client, db):
    ticket = _ticket_with_draft(db)
    response = client.post(f"/tickets/{ticket.id}/reply/discard")
    assert response.status_code == 200
    assert response.json()["state"] == "discarded"


def test_discard_after_discard_is_409(client, db):
    ticket = _ticket_with_draft(db, state="discarded")
    assert client.post(f"/tickets/{ticket.id}/reply/discard").status_code == 409


def test_reply_metrics_counts_only_reply_purposes(client, db):
    ticket = _ticket_with_draft(db)
    db.add(
        LlmCall(
            id=uuid4().hex,
            ticket_id=ticket.id,
            purpose="classify",
            model="m",
            input_tokens=100,
            output_tokens=20,
            cost_usd=0.001,
            latency_ms=50,
            outcome="ok",
        )
    )
    db.add(
        LlmCall(
            id=uuid4().hex,
            ticket_id=ticket.id,
            purpose="reply_draft",
            model="m",
            input_tokens=30,
            output_tokens=10,
            cost_usd=0.002,
            latency_ms=40,
            outcome="ok",
        )
    )
    db.commit()

    metrics = client.get(f"/tickets/{ticket.id}/reply").json()["metrics"]
    assert metrics["llm_calls"] == 1  # the classify call is excluded
    assert metrics["input_tokens"] == 30
    assert metrics["output_tokens"] == 10
    assert metrics["latency_ms"] == 40
    assert metrics["cost_usd"] == 0.002
