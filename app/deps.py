"""FastAPI dependencies: settings, per-request DB session, optional API key."""

import hmac
from collections.abc import Iterator

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.errors import UnauthorizedError


def settings_dep() -> Settings:
    return get_settings()


def get_db(request: Request) -> Iterator[Session]:
    """Yield a session bound to the app's engine, one per request."""
    factory = request.app.state.session_factory
    session = factory()
    try:
        yield session
    finally:
        session.close()


def require_api_key(request: Request, settings: Settings = Depends(settings_dep)) -> None:
    """Enforce the optional API key. No-op when API_KEY is unset."""
    expected = settings.api_key
    if not expected:
        return
    provided = request.headers.get("X-API-Key", "")
    if not hmac.compare_digest(provided, expected):
        raise UnauthorizedError("A valid X-API-Key header is required.")
