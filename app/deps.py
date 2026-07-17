"""FastAPI dependencies: settings and the per-request DB session."""

from collections.abc import Iterator

from fastapi import Request
from sqlalchemy.orm import Session

from app.config import Settings, get_settings


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
