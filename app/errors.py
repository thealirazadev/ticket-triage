"""Application error types, the single error JSON shape, and exception handlers.

Every non-2xx response uses ``{"error": {"code", "message"}}``. Stack traces,
SQL, provider payloads, and other internals never reach the client; they are
logged with the request id for correlation.
"""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger("app.errors")


class AppError(Exception):
    """Base for expected, client-facing failures with a stable code and status."""

    status_code: int = 500
    code: str = "internal_error"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class InvalidStateError(AppError):
    status_code = 409
    code = "invalid_state"


class UnauthorizedError(AppError):
    status_code = 401
    code = "unauthorized"


class ValidationError(AppError):
    status_code = 422
    code = "validation_error"


def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code, content={"error": {"code": code, "message": message}}
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Wire every exception path to the single error format."""

    @app.exception_handler(AppError)
    async def _handle_app_error(_: Request, exc: AppError) -> JSONResponse:
        return _error_response(exc.status_code, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Collapse pydantic's detail array into one friendly sentence; never
        # expose the raw error list (it can echo submitted values).
        first = exc.errors()[0] if exc.errors() else None
        if first:
            loc = ".".join(str(p) for p in first.get("loc", ()) if p != "body")
            detail = first.get("msg", "Invalid request.")
            message = f"{loc}: {detail}" if loc else detail
        else:
            message = "Request validation failed."
        return _error_response(422, "validation_error", message)

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        mapping = {
            401: "unauthorized",
            404: "not_found",
            409: "invalid_state",
            422: "validation_error",
        }
        code = mapping.get(exc.status_code, "internal_error")
        message = exc.detail if isinstance(exc.detail, str) else "Request could not be processed."
        return _error_response(exc.status_code, code, message)

    @app.exception_handler(Exception)
    async def _handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error", extra={"error_code": "internal_error"})
        return _error_response(500, "internal_error", "An unexpected error occurred.")
