"""Review workflow: pending ordering, approve/correct state machine, export."""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.models import Ticket, Triage


def _triaged(db, ext, *, intent="bug", priority="P3", sentiment="neutral", created_at=None):
    ticket = Ticket(
        id=uuid4().hex,
        external_id=ext,
        channel="web",
        sender="a@b.com",
        subject="Something broke",
        body="Details of the problem go here.",
        status="triaged",
        queue="technical",
        created_at=created_at or datetime.now(UTC),
    )
    db.add(ticket)
    db.flush()
    db.add(
        Triage(
            id=uuid4().hex,
            ticket_id=ticket.id,
            intent=intent,
            priority=priority,
            sentiment=sentiment,
            summary="A short machine summary of the ticket.",
            model="example-model-id",
            prompt_version="1",
            attempts=1,
        )
    )
    db.commit()
    return ticket


def _needs_human(db, ext, *, created_at=None):
    ticket = Ticket(
        id=uuid4().hex,
        external_id=ext,
        channel="web",
        sender="a@b.com",
        subject="Unclassifiable",
        body="The provider could not classify this.",
        status="needs_human",
        queue="general",
        triage_error="parse_failed",
        created_at=created_at or datetime.now(UTC),
    )
    db.add(ticket)
    db.commit()
    return ticket


def test_pending_lists_oldest_first_with_error_visible(client, db):
    old = datetime.now(UTC) - timedelta(hours=2)
    _triaged(db, "t-old", created_at=old)
    _needs_human(db, "t-new", created_at=datetime.now(UTC))

    body = client.get("/reviews/pending").json()
    assert body["total"] == 2
    assert [t["external_id"] for t in body["tickets"]] == ["t-old", "t-new"]
    needs_human = body["tickets"][1]
    assert needs_human["triage"] is None
    assert needs_human["triage_error"] == "parse_failed"


def test_approve_transitions_and_leaves_pending(client, db):
    ticket = _triaged(db, "t-ap")
    response = client.post(f"/tickets/{ticket.id}/approve")
    assert response.status_code == 200
    assert response.json()["status"] == "approved"
    pending = client.get("/reviews/pending").json()
    assert all(t["id"] != ticket.id for t in pending["tickets"])


def test_approve_again_is_409(client, db):
    ticket = _triaged(db, "t-ap2")
    client.post(f"/tickets/{ticket.id}/approve")
    again = client.post(f"/tickets/{ticket.id}/approve")
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "invalid_state"


def test_approve_needs_human_is_409(client, db):
    ticket = _needs_human(db, "t-nh")
    response = client.post(f"/tickets/{ticket.id}/approve")
    assert response.status_code == 409


def test_approve_unknown_id_is_404(client):
    assert client.post("/tickets/nope/approve").status_code == 404


def test_correct_needs_human_reroutes_and_stores_snapshot(client, db):
    ticket = _needs_human(db, "t-corr")
    response = client.post(
        f"/tickets/{ticket.id}/correct",
        json={
            "intent": "account_access",
            "priority": "P2",
            "sentiment": "negative",
            "note": "Locked out, not a parse issue.",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "corrected"
    assert body["queue"] == "security"  # account_access -> security seeded rule
    assert body["correction"]["intent"] == "account_access"
    assert body["correction"]["note"] == "Locked out, not a parse issue."
    assert body["triage_error"] is None


def test_correct_triaged_reroutes_by_priority(client, db):
    ticket = _triaged(db, "t-corr2", intent="bug", priority="P3")
    response = client.post(
        f"/tickets/{ticket.id}/correct",
        json={"intent": "bug", "priority": "P1", "sentiment": "negative"},
    )
    assert response.json()["queue"] == "urgent"  # P1 -> urgent


def test_second_correct_is_409(client, db):
    ticket = _triaged(db, "t-corr3")
    labels = {"intent": "bug", "priority": "P3", "sentiment": "neutral"}
    client.post(f"/tickets/{ticket.id}/correct", json=labels)
    again = client.post(f"/tickets/{ticket.id}/correct", json=labels)
    assert again.status_code == 409


def test_correct_missing_label_is_422(client, db):
    ticket = _triaged(db, "t-corr4")
    response = client.post(
        f"/tickets/{ticket.id}/correct", json={"intent": "bug", "priority": "P3"}
    )
    assert response.status_code == 422


def test_correct_unknown_id_is_404(client):
    response = client.post(
        "/tickets/nope/correct",
        json={"intent": "bug", "priority": "P3", "sentiment": "neutral"},
    )
    assert response.status_code == 404


def test_export_covers_approved_and_corrected(client, db):
    approved = _triaged(db, "t-exp-a", intent="billing", priority="P2", sentiment="negative")
    client.post(f"/tickets/{approved.id}/approve")
    corrected = _needs_human(db, "t-exp-c")
    client.post(
        f"/tickets/{corrected.id}/correct",
        json={"intent": "refund", "priority": "P3", "sentiment": "negative"},
    )

    response = client.get("/corrections/export")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    lines = [json.loads(line) for line in response.text.splitlines() if line]
    by_source = {line["source"]: line for line in lines}
    assert set(by_source) == {"approved", "corrected"}
    assert by_source["approved"]["intent"] == "billing"  # from the triage
    assert by_source["corrected"]["intent"] == "refund"  # from the correction
    # Field names match the eval dataset shape.
    expected_keys = {
        "external_id",
        "channel",
        "sender",
        "subject",
        "body",
        "intent",
        "priority",
        "sentiment",
        "source",
    }
    assert set(lines[0]) == expected_keys


def test_export_roundtrips_into_eval_loader(client, db, tmp_path):
    ticket = _triaged(db, "t-rt", intent="bug", priority="P1", sentiment="negative")
    client.post(f"/tickets/{ticket.id}/approve")
    exported = client.get("/corrections/export").text

    from evals.run import load_dataset

    path = tmp_path / "extra.jsonl"
    path.write_text(exported)
    rows = load_dataset(str(path))
    assert rows[0]["intent"] == "bug"
    assert rows[0]["external_id"] == "t-rt"


def test_export_since_filter(client, db):
    old = _triaged(db, "t-old-exp", created_at=datetime.now(UTC) - timedelta(days=2))
    client.post(f"/tickets/{old.id}/approve")
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    response = client.get("/corrections/export", params={"since": future})
    assert response.status_code == 200
    assert response.text.strip() == ""


def test_export_empty_database(client):
    response = client.get("/corrections/export")
    assert response.status_code == 200
    assert response.text == ""
