"""Provider client: retries/backoff, 429 vs 400, breaker lifecycle, call rows."""

import httpx
import pytest

from app.services.llm_client import CircuitBreaker, CircuitOpenError, LlmClient, ProviderError
from tests.conftest import chat_response, triage_json

MESSAGES = [{"role": "user", "content": "hi"}]


def _count_handler(responder):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return responder(calls["n"], request)

    handler.calls = calls
    return handler


def test_ok_records_one_row_with_cost_and_latency(db, make_llm_client):
    client = make_llm_client(lambda _r: httpx.Response(200, json=chat_response(triage_json())))
    result = client.complete(db, MESSAGES, purpose="classify", ticket_id=None)
    db.commit()

    assert "account_access" in result.content
    from app.models import LlmCall

    rows = db.query(LlmCall).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.outcome == "ok"
    assert row.input_tokens == 100
    assert row.output_tokens == 20
    # 100/1e6 * 1.0 + 20/1e6 * 2.0 = 0.00014
    assert float(row.cost_usd) == pytest.approx(0.00014)
    assert row.latency_ms >= 0


def test_missing_usage_records_null_tokens_zero_cost(db, make_llm_client):
    def handler(_request):
        return httpx.Response(200, json={"choices": [{"message": {"content": triage_json()}}]})

    client = make_llm_client(handler)
    client.complete(db, MESSAGES, purpose="classify")
    db.commit()

    from app.models import LlmCall

    row = db.query(LlmCall).one()
    assert row.input_tokens is None
    assert row.output_tokens is None
    assert float(row.cost_usd) == 0.0


def test_timeout_retries_up_to_max_then_fails(db, make_llm_client, no_backoff):
    def responder(_n, _request):
        raise httpx.ReadTimeout("slow")

    handler = _count_handler(responder)
    client = make_llm_client(handler)
    with pytest.raises(ProviderError) as excinfo:
        client.complete(db, MESSAGES, purpose="classify")
    db.commit()

    assert excinfo.value.outcome == "timeout"
    assert handler.calls["n"] == 3  # 1 initial + LLM_MAX_RETRIES (2)
    from app.models import LlmCall

    assert db.query(LlmCall).one().outcome == "timeout"


def test_500_retried_then_succeeds(db, make_llm_client, no_backoff):
    def responder(n, _request):
        if n < 3:
            return httpx.Response(500)
        return httpx.Response(200, json=chat_response(triage_json()))

    handler = _count_handler(responder)
    client = make_llm_client(handler)
    result = client.complete(db, MESSAGES, purpose="classify")
    db.commit()

    assert handler.calls["n"] == 3
    assert result.content
    from app.models import LlmCall

    assert db.query(LlmCall).one().outcome == "ok"


def test_429_is_retried(db, make_llm_client, no_backoff):
    def responder(n, _request):
        if n == 1:
            return httpx.Response(429)
        return httpx.Response(200, json=chat_response(triage_json()))

    handler = _count_handler(responder)
    client = make_llm_client(handler)
    client.complete(db, MESSAGES, purpose="classify")
    assert handler.calls["n"] == 2


def test_400_not_retried(db, make_llm_client, no_backoff):
    handler = _count_handler(lambda _n, _r: httpx.Response(400))
    client = make_llm_client(handler)
    with pytest.raises(ProviderError) as excinfo:
        client.complete(db, MESSAGES, purpose="classify")
    db.commit()

    assert excinfo.value.outcome == "api_error"
    assert handler.calls["n"] == 1
    from app.models import LlmCall

    assert db.query(LlmCall).one().outcome == "api_error"


def test_malformed_200_body_recorded_as_api_error(db, make_llm_client, no_backoff):
    # A 200 whose body is not JSON (e.g. a proxy HTML error page) must not escape
    # the client as an unhandled decode error: it is a provider failure.
    handler = _count_handler(lambda _n, _r: httpx.Response(200, text="<html>not json</html>"))
    client = make_llm_client(handler)
    with pytest.raises(ProviderError) as excinfo:
        client.complete(db, MESSAGES, purpose="classify")
    db.commit()

    assert excinfo.value.outcome == "api_error"
    assert handler.calls["n"] == 3  # retried like a transient failure, then recorded
    from app.models import LlmCall

    assert db.query(LlmCall).one().outcome == "api_error"
    # The failure must count toward the breaker so a broken provider trips it.
    assert client.breaker._consecutive_failures == 1


def test_408_recorded_as_timeout(db, make_llm_client, no_backoff):
    # A 408 Request Timeout is a timeout outcome, not a generic api_error; the
    # recorded outcome must reflect that for accurate stats.
    handler = _count_handler(lambda _n, _r: httpx.Response(408))
    client = make_llm_client(handler)
    with pytest.raises(ProviderError) as excinfo:
        client.complete(db, MESSAGES, purpose="classify")
    db.commit()

    assert excinfo.value.outcome == "timeout"
    assert handler.calls["n"] == 1  # 408 is not in the retryable set
    from app.models import LlmCall

    assert db.query(LlmCall).one().outcome == "timeout"


def test_breaker_opens_after_threshold_then_skips_calls(db, make_llm_client, no_backoff):
    handler = _count_handler(lambda _n, _r: httpx.Response(400))
    client = make_llm_client(handler)
    for _ in range(5):  # CIRCUIT_FAILURE_THRESHOLD
        with pytest.raises(ProviderError):
            client.complete(db, MESSAGES, purpose="classify")
    # Breaker now open: the next call is skipped, no HTTP request made.
    with pytest.raises(CircuitOpenError):
        client.complete(db, MESSAGES, purpose="classify")
    assert handler.calls["n"] == 5


def test_breaker_unit_open_cooldown_and_trial(monkeypatch):
    now = {"t": 1000.0}
    monkeypatch.setattr("app.services.llm_client.time.monotonic", lambda: now["t"])
    breaker = CircuitBreaker(threshold=2, cooldown_seconds=30)

    assert breaker.allow() is True
    breaker.record_failure()
    breaker.record_failure()  # opens
    assert breaker.allow() is False

    now["t"] += 31  # cooldown elapsed -> one trial allowed
    assert breaker.allow() is True
    breaker.record_success()  # trial closes the circuit
    assert breaker.allow() is True


def test_client_uses_bearer_header_from_settings(settings):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json=chat_response(triage_json()))

    client = LlmClient(settings, transport=httpx.MockTransport(handler))
    try:
        from app.db import make_engine, make_session_factory

        session = make_session_factory(make_engine(settings.database_url))()
        client.complete(session, MESSAGES, purpose="classify")
        session.close()
    finally:
        client.close()
    assert captured["auth"] == "Bearer test-key"
