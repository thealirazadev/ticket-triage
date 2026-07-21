"""Optional API-key auth: open when unset, 401 when wrong, health always open."""

import pytest

from app.config import get_settings


@pytest.fixture
def secured_client(settings, session_factory, monkeypatch):
    monkeypatch.setenv("API_KEY", "s3cret")
    get_settings.cache_clear()
    from fastapi.testclient import TestClient

    from app import deps
    from app.main import create_app

    app = create_app()

    def _override_get_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[deps.get_db] = _override_get_db
    with TestClient(app) as test_client:
        yield test_client


def test_open_when_api_key_unset(client):
    assert client.get("/tickets").status_code == 200


def test_health_always_open(secured_client):
    assert secured_client.get("/health").status_code == 200


def test_missing_key_returns_401(secured_client):
    response = secured_client.get("/tickets")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_wrong_key_returns_401(secured_client):
    response = secured_client.get("/tickets", headers={"X-API-Key": "wrong"})
    assert response.status_code == 401


def test_correct_key_allows_access(secured_client):
    response = secured_client.get("/tickets", headers={"X-API-Key": "s3cret"})
    assert response.status_code == 200


def test_non_ascii_key_returns_401(secured_client):
    # Header bytes above 0x7f decode to a non-ASCII string; comparing those with
    # hmac.compare_digest on str raises TypeError. An unauthenticated caller must
    # get a clean 401, never an unhandled server error.
    response = secured_client.get("/tickets", headers={"X-API-Key": "kéy".encode()})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
