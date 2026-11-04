"""Request body size cap, enforced before any route, auth, or JSON parsing.

Field-level ``max_length`` checks only run after the whole body has been read
and parsed, and the email webhook ignores unknown fields, so without a cap a
single request could make the server buffer an arbitrarily large body. The cap
is far above the largest valid ticket (a 50,000-character body, even fully
JSON-escaped).
"""

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

MAX_BODY_BYTES = 1024 * 1024

_MESSAGE = "Request body is too large."


def _too_large() -> JSONResponse:
    return JSONResponse(
        status_code=413, content={"error": {"code": "payload_too_large", "message": _MESSAGE}}
    )


class BodySizeLimitMiddleware:
    """Reject bodies over ``max_bytes`` with 413: up front from Content-Length,
    and while streaming for chunked bodies that declare no length."""

    def __init__(self, app: ASGIApp, max_bytes: int = MAX_BODY_BYTES):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = 0
                if declared > self.max_bytes:
                    await _too_large()(scope, receive, send)
                    return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    # Raised while the route reads its body, before any response
                    # starts; the app's HTTPException handler renders the 413.
                    raise HTTPException(status_code=413, detail=_MESSAGE)
            return message

        await self.app(scope, limited_receive, send)
