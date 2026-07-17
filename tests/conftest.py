"""Shared test fixtures.

The provider is always mocked via httpx.MockTransport, so the suite makes zero
network calls and needs no LLM_API_KEY. Each test gets a fresh temporary SQLite
database migrated by ``alembic upgrade head`` so migrations, CHECK constraints,
and seeds are genuinely exercised. The worker thread stays off; triage is driven
by calling ``process_next_ticket`` directly.
"""

import json
from collections.abc import Callable

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app import deps
from app.config import Settings, get_settings
from app.db import make_engine, make_session_factory
from app.main import create_app
from app.services.llm_client import LlmClient

_PROJECT_ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent

_BASE_ENV = {
    "WORKER_ENABLED": "false",
    "LLM_API_KEY": "test-key",
    "LLM_BASE_URL": "http://provider.test/v1",
    "LLM_MODEL": "test-model",
    "API_KEY": "",
    "DEFAULT_QUEUE": "general",
    "LLM_MAX_RETRIES": "2",
    "LLM_TIMEOUT_SECONDS": "5",
    "CIRCUIT_FAILURE_THRESHOLD": "5",
    "CIRCUIT_COOLDOWN_SECONDS": "60",
    "LLM_PRICE_INPUT_PER_MTOK": "1.0",
    "LLM_PRICE_OUTPUT_PER_MTOK": "2.0",
    "LOG_LEVEL": "WARNING",
}


@pytest.fixture
def settings(tmp_path, monkeypatch) -> Settings:
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    for key, value in _BASE_ENV.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()

    cfg = Config(str(_PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_PROJECT_ROOT / "migrations"))
    command.upgrade(cfg, "head")

    resolved = get_settings()
    yield resolved
    get_settings.cache_clear()


@pytest.fixture
def no_backoff(monkeypatch) -> None:
    """Make retry backoff instant so retry tests run fast."""
    monkeypatch.setattr("app.services.llm_client.time.sleep", lambda _s: None)


@pytest.fixture
def session_factory(settings):
    engine = make_engine(settings.database_url)
    return make_session_factory(engine)


@pytest.fixture
def db(session_factory) -> Session:
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client(settings, session_factory) -> TestClient:
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


@pytest.fixture
def make_llm_client(settings) -> Callable[[Callable], LlmClient]:
    created: list[LlmClient] = []

    def _make(handler: Callable[[httpx.Request], httpx.Response]) -> LlmClient:
        client = LlmClient(settings, transport=httpx.MockTransport(handler))
        created.append(client)
        return client

    yield _make
    for client in created:
        client.close()


@pytest.fixture
def make_ticket(client) -> Callable[..., httpx.Response]:
    counter = {"n": 0}

    def _make(**overrides) -> httpx.Response:
        counter["n"] += 1
        payload = {
            "external_id": f"ext-{counter['n']}",
            "subject": "Cannot log in",
            "body": "I reset my password and now I am locked out of my account.",
            "sender": "user@example.com",
            "channel": "web",
        }
        payload.update(overrides)
        return client.post("/tickets", json=payload)

    return _make


# --- provider response helpers ------------------------------------------------


def chat_response(content: str, prompt_tokens: int = 100, completion_tokens: int = 20) -> dict:
    """A well-formed chat-completions-style body carrying `content`."""
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    }


def triage_json(
    intent: str = "account_access",
    priority: str = "P2",
    sentiment: str = "negative",
    summary: str = "Customer is locked out after a password reset. They need access restored.",
) -> str:
    return json.dumps(
        {"intent": intent, "priority": priority, "sentiment": sentiment, "summary": summary}
    )


def ok_handler(content: str | None = None) -> Callable[[httpx.Request], httpx.Response]:
    body = content if content is not None else triage_json()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=chat_response(body))

    return handler
