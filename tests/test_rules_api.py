"""Routing rules API: list, transactional replace, validation, empty set."""

from uuid import uuid4

from app.models import Ticket


def test_get_rules_returns_seeded_set(client):
    body = client.get("/rules").json()
    assert body["default_queue"] == "general"
    positions = [r["position"] for r in body["rules"]]
    assert positions == sorted(positions)
    # The P1 -> urgent rule is seeded first.
    assert body["rules"][0]["priority"] == "P1"
    assert body["rules"][0]["queue"] == "urgent"


def test_put_replaces_and_reorders(client):
    payload = {
        "rules": [
            {"intent": "billing", "queue": "money"},
            {"priority": "P1", "queue": "fires"},
        ]
    }
    response = client.put("/rules", json=payload)
    assert response.status_code == 200
    rules = response.json()["rules"]
    assert [r["position"] for r in rules] == [1, 2]
    assert rules[0]["intent"] == "billing" and rules[0]["queue"] == "money"
    assert rules[1]["priority"] == "P1"
    # Persisted: a fresh GET reflects the replacement.
    assert client.get("/rules").json()["rules"] == rules


def test_put_empty_set_is_legal(client):
    response = client.put("/rules", json={"rules": []})
    assert response.status_code == 200
    assert client.get("/rules").json()["rules"] == []


def test_put_rejects_unknown_label(client):
    response = client.put("/rules", json={"rules": [{"intent": "nope", "queue": "x"}]})
    assert response.status_code == 422


def test_put_rejects_bad_queue_slug(client):
    response = client.put("/rules", json={"rules": [{"priority": "P1", "queue": "Bad Queue!"}]})
    assert response.status_code == 422


def test_put_rejects_rule_without_condition(client):
    response = client.put("/rules", json={"rules": [{"queue": "x"}]})
    assert response.status_code == 422


def test_put_rejects_over_100_rules(client):
    rules = [{"priority": "P1", "queue": "x"} for _ in range(101)]
    response = client.put("/rules", json={"rules": rules})
    assert response.status_code == 422


def test_invalid_replacement_changes_nothing(client):
    before = client.get("/rules").json()["rules"]
    client.put("/rules", json={"rules": [{"queue": "Bad!"}]})
    after = client.get("/rules").json()["rules"]
    assert before == after


def _needs_human(db, ext):
    ticket = Ticket(
        id=uuid4().hex,
        external_id=ext,
        channel="web",
        sender="a@b.com",
        subject="s",
        body="b",
        status="needs_human",
        queue="general",
        triage_error="parse_failed",
    )
    db.add(ticket)
    db.commit()
    return ticket


def test_new_rules_apply_to_subsequent_correction(client, db):
    client.put("/rules", json={"rules": [{"sentiment": "negative", "queue": "escalations"}]})
    ticket = _needs_human(db, "r-1")
    body = client.post(
        f"/tickets/{ticket.id}/correct",
        json={"intent": "bug", "priority": "P3", "sentiment": "negative"},
    ).json()
    assert body["queue"] == "escalations"


def test_empty_rules_route_correction_to_default(client, db):
    client.put("/rules", json={"rules": []})
    ticket = _needs_human(db, "r-2")
    body = client.post(
        f"/tickets/{ticket.id}/correct",
        json={"intent": "account_access", "priority": "P1", "sentiment": "negative"},
    ).json()
    assert body["queue"] == "general"  # no rules -> default queue
