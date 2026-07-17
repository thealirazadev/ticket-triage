"""FastAPI application factory: logging, error handlers, request-id middleware,
routers, and the worker lifespan."""

import logging
import time
from uuid import uuid4

from fastapi import FastAPI, Request

from app.config import get_settings
from app.db import make_engine, make_session_factory
from app.errors import register_exception_handlers
from app.logging import bind_context, configure_logging, reset_context
from app.routers import health

log = logging.getLogger("app.request")


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    engine = make_engine(settings.database_url)
    session_factory = make_session_factory(engine)

    app = FastAPI(title="ticket-triage")
    app.state.engine = engine
    app.state.session_factory = session_factory

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or uuid4().hex
        token = bind_context(request_id=request_id, route=request.url.path)
        started = time.monotonic()
        try:
            response = await call_next(request)
        finally:
            reset_context(token)
        duration_ms = int((time.monotonic() - started) * 1000)
        response.headers["X-Request-ID"] = request_id
        log.info(
            "request completed",
            extra={
                "request_id": request_id,
                "route": request.url.path,
                "method": request.method,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
            },
        )
        return response

    register_exception_handlers(app)
    app.include_router(health.router)
    return app


app = create_app()
