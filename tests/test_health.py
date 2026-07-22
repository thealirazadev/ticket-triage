"""Liveness and readiness probes: DB check, breaker reporting, 503 on DB down."""

from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app import deps
from app.main import create_app
from app.services.worker import Worker


def test_health_is_ok(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_ok_with_worker_disabled(client):
    # The test app runs with WORKER_ENABLED=false, so there is no breaker to
    # report; readiness is still healthy because the database is reachable.
    response = client.get("/ready")
    assert response.status_code == 200
    body = response.json()
    assert body == {"status": "ready", "database": "ok", "circuit_breaker": "disabled"}


def test_ready_reports_open_breaker(client, settings, session_factory):
    # A constructed (unstarted) worker shares a real breaker we can trip directly;
    # no thread runs and no provider call is made.
    worker = Worker(settings, session_factory)
    try:
        for _ in range(settings.circuit_failure_threshold):
            worker.breaker.record_failure()
        client.app.state.worker = worker
        body = client.get("/ready").json()
        assert body["circuit_breaker"] == "open"
        assert body["status"] == "ready"  # open breaker does not fail readiness
    finally:
        client.app.state.worker = None
        worker._client.close()


def test_ready_returns_503_when_database_unreachable(settings):
    app = create_app()

    class _BrokenSession:
        def execute(self, *_args, **_kwargs):
            raise OperationalError("SELECT 1", {}, Exception("connection refused"))

        def close(self):
            pass

    def _broken_db():
        yield _BrokenSession()

    app.dependency_overrides[deps.get_db] = _broken_db
    with TestClient(app) as test_client:
        response = test_client.get("/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["database"] == "unavailable"
