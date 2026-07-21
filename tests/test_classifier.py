"""Classifier: clean parse, fenced JSON, normalization, repair, double failure."""

import httpx
import pytest

from app.models import LlmCall
from app.schemas.triage import TriageResult
from app.services.classifier import TriageFailure, classify_ticket, parse_triage
from tests.conftest import chat_response, triage_json


def _sequence_handler(bodies):
    """Return each body in turn as a 200 chat response."""
    state = {"i": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        body = bodies[min(state["i"], len(bodies) - 1)]
        state["i"] += 1
        return httpx.Response(200, json=chat_response(body))

    return handler


def _classify(db, client):
    # ticket_id is None here: these are unit tests of the classifier, not tied to
    # a persisted ticket (llm_calls.ticket_id is nullable for such calls).
    return classify_ticket(
        db,
        client,
        ticket_id=None,
        subject="Locked out",
        sender="a@b.com",
        channel="web",
        body="I reset my password and cannot log in.",
    )


def test_parse_clean_json():
    result, error = parse_triage(triage_json())
    assert error == ""
    assert isinstance(result, TriageResult)
    assert result.intent == "account_access"


def test_parse_strips_code_fences():
    fenced = "```json\n" + triage_json() + "\n```"
    result, error = parse_triage(fenced)
    assert error == ""
    assert result.priority == "P2"


def test_parse_normalizes_case_and_whitespace():
    raw = '{"intent": " Billing ", "priority": "p1", "sentiment": "NEGATIVE", "summary": "x"}'
    result, _ = parse_triage(raw)
    assert result.intent == "billing"
    assert result.priority == "P1"
    assert result.sentiment == "negative"


def test_parse_rejects_unknown_label():
    result, error = parse_triage(triage_json(intent="angry"))
    assert result is None
    assert "intent" in error


def test_parse_rejects_non_json():
    result, error = parse_triage("not json at all")
    assert result is None
    assert "JSON" in error


def test_parse_ignores_extra_keys():
    raw = '{"intent":"bug","priority":"P2","sentiment":"neutral","summary":"s","extra":1}'
    result, error = parse_triage(raw)
    assert error == ""
    assert result.intent == "bug"


def test_clean_classification_attempts_one(db, make_llm_client):
    client = make_llm_client(_sequence_handler([triage_json()]))
    outcome = _classify(db, client)
    db.commit()
    assert outcome.attempts == 1
    assert outcome.result.intent == "account_access"
    assert db.query(LlmCall).one().outcome == "ok"


def test_repair_succeeds_attempts_two(db, make_llm_client):
    client = make_llm_client(_sequence_handler(["garbage", triage_json(intent="bug")]))
    outcome = _classify(db, client)
    db.commit()
    assert outcome.attempts == 2
    assert outcome.result.intent == "bug"

    rows = db.query(LlmCall).order_by(LlmCall.created_at).all()
    assert len(rows) == 2
    outcomes = sorted(r.outcome for r in rows)
    assert outcomes == ["ok", "parse_error"]
    purposes = {r.purpose for r in rows}
    assert purposes == {"classify", "repair"}


def test_double_failure_raises_parse_failed(db, make_llm_client):
    client = make_llm_client(_sequence_handler(["garbage", "still garbage"]))
    with pytest.raises(TriageFailure) as excinfo:
        _classify(db, client)
    db.commit()
    assert excinfo.value.reason == "parse_failed"

    rows = db.query(LlmCall).all()
    assert len(rows) == 2
    assert all(r.outcome == "parse_error" for r in rows)


def test_provider_error_raises_provider_failure(db, make_llm_client, no_backoff):
    client = make_llm_client(lambda _r: httpx.Response(400))
    with pytest.raises(TriageFailure) as excinfo:
        _classify(db, client)
    assert excinfo.value.reason == "provider_error"


def test_non_string_content_falls_back_cleanly(db, make_llm_client, no_backoff):
    # Some providers return message.content as a list of content parts, not a
    # string. That must flow through the normal parse-fail -> repair -> needs_human
    # path, not crash the classifier with an AttributeError.
    def handler(_request: httpx.Request) -> httpx.Response:
        body = {
            "choices": [{"message": {"content": ["not", "a", "string"]}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
        return httpx.Response(200, json=body)

    client = make_llm_client(handler)
    with pytest.raises(TriageFailure) as excinfo:
        _classify(db, client)
    db.commit()
    assert excinfo.value.reason == "parse_failed"
    # Both the classify and repair calls are recorded as parse_error.
    rows = db.query(LlmCall).all()
    assert len(rows) == 2
    assert all(r.outcome == "parse_error" for r in rows)


def test_long_body_is_truncated_for_prompt(db, make_llm_client):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["len"] = len(request.content)
        return httpx.Response(200, json=chat_response(triage_json()))

    client = make_llm_client(handler)
    classify_ticket(
        db,
        client,
        ticket_id=None,
        subject="s",
        sender="a@b.com",
        channel="web",
        body="x" * 40_000,
    )
    # Prompt body is truncated near MAX_BODY_CHARS, far below the full 40k body.
    assert captured["len"] < 20_000
