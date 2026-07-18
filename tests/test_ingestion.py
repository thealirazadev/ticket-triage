"""Ingestion: create, idempotent replay, webhook mapping, validation bounds."""


def test_create_ticket_returns_201_received(make_ticket):
    response = make_ticket()
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "received"
    assert body["queue"] is None
    assert body["triage"] is None
    assert body["metrics"] is None
    assert body["id"]
    assert body["created_at"].endswith("Z")


def test_replay_same_external_id_returns_200_single_row(client, make_ticket):
    first = make_ticket(external_id="dup-1")
    assert first.status_code == 201
    ticket_id = first.json()["id"]

    replay = make_ticket(external_id="dup-1", subject="changed", body="different body text here")
    assert replay.status_code == 200
    assert replay.json()["id"] == ticket_id
    # Replay body fields are ignored: nothing changed.
    assert replay.json()["subject"] == "Cannot log in"

    listed = client.get("/tickets").json()
    assert listed["total"] == 1


def test_rapid_duplicate_submissions_create_one_row(client):
    payload = {
        "external_id": "race-1",
        "subject": "hi",
        "body": "some body",
        "sender": "a@b.com",
        "channel": "api",
    }
    statuses = [client.post("/tickets", json=payload).status_code for _ in range(5)]
    assert statuses.count(201) == 1
    assert statuses.count(200) == 4
    assert client.get("/tickets").json()["total"] == 1


def test_webhook_maps_fields_and_defaults_subject(client):
    response = client.post(
        "/webhooks/email",
        json={
            "message_id": "<abc@mail>",
            "from": "jane@example.com",
            "text": "please help",
            "x_provider_meta": "ignored-unknown-field",
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["channel"] == "email"
    assert body["subject"] == "(no subject)"
    assert body["sender"] == "jane@example.com"
    assert body["external_id"] == "<abc@mail>"


def test_webhook_empty_subject_stored_as_placeholder(client):
    response = client.post(
        "/webhooks/email",
        json={"message_id": "m2", "from": "a@b.com", "text": "body", "subject": "   "},
    )
    assert response.status_code == 201
    assert response.json()["subject"] == "(no subject)"


def test_bad_channel_returns_422(client):
    response = client.post(
        "/tickets",
        json={
            "external_id": "x",
            "subject": "s",
            "body": "b",
            "sender": "a@b.com",
            "channel": "fax",
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_oversized_body_returns_422(client):
    response = client.post(
        "/tickets",
        json={
            "external_id": "big",
            "subject": "s",
            "body": "x" * 50_001,
            "sender": "a@b.com",
            "channel": "web",
        },
    )
    assert response.status_code == 422


def test_large_body_within_bound_accepted(make_ticket):
    response = make_ticket(external_id="long", body="x" * 40_000)
    assert response.status_code == 201


def test_blank_subject_returns_422(client):
    response = client.post(
        "/tickets",
        json={
            "external_id": "b",
            "subject": "   ",
            "body": "b",
            "sender": "a@b.com",
            "channel": "web",
        },
    )
    assert response.status_code == 422


def test_subject_length_bounds(client):
    ok = client.post(
        "/tickets",
        json={
            "external_id": "s500",
            "subject": "a" * 500,
            "body": "b",
            "sender": "a@b.com",
            "channel": "web",
        },
    )
    assert ok.status_code == 201
    too_long = client.post(
        "/tickets",
        json={
            "external_id": "s501",
            "subject": "a" * 501,
            "body": "b",
            "sender": "a@b.com",
            "channel": "web",
        },
    )
    assert too_long.status_code == 422


def test_missing_field_returns_422(client):
    response = client.post("/tickets", json={"external_id": "x", "subject": "s"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_get_unknown_ticket_returns_404(client):
    response = client.get("/tickets/does-not-exist")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_list_empty_database(client):
    body = client.get("/tickets").json()
    assert body == {"tickets": [], "total": 0, "limit": 50, "offset": 0}


def test_list_bad_filter_returns_422(client):
    assert client.get("/tickets", params={"status": "bogus"}).status_code == 422
    assert client.get("/tickets", params={"priority": "P9"}).status_code == 422
