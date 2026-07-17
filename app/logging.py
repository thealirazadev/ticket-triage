"""Structured JSON logging: one object per line, with per-request context."""

import json
import logging
import sys
from contextvars import ContextVar

# Request-scoped context, attached to every log line emitted during a request or
# a worker iteration. Worker lines carry ticket_id instead of request_id.
_context: ContextVar[dict | None] = ContextVar("log_context", default=None)

# LogRecord attributes present on every record; anything else is treated as an
# explicit `extra` field and merged into the JSON output.
_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()) | {
    "message",
    "asctime",
    "taskName",
}


def bind_context(**fields: object) -> object:
    """Bind context fields for the current execution and return a reset token."""
    current = dict(_context.get() or {})
    current.update({k: v for k, v in fields.items() if v is not None})
    return _context.set(current)


def reset_context(token) -> None:
    """Restore the context to the state captured by a bind_context token."""
    _context.reset(token)


class JsonFormatter(logging.Formatter):
    """Render a log record as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(_context.get() or {})
        for key, value in record.__dict__.items():
            if key not in _RESERVED:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Install the JSON formatter on the root logger, replacing prior handlers."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    # Uvicorn's access logger is noisy and duplicates request-id middleware logs.
    logging.getLogger("uvicorn.access").handlers = []
