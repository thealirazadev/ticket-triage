"""The ticket-triage service wired to a mocked provider transport, for the README demo.

This is the real application (real routers, real worker thread, real SQLite, real
migrations) with exactly one thing replaced: the worker's ``LlmClient`` is constructed
with an ``httpx.MockTransport`` that returns a fixed, valid classification (and a fixed
reply draft) instead of calling a provider. No network and no API key are involved, so
``scripts/demo.sh`` can capture genuine request/response pairs deterministically.

Run standalone with:  uv run uvicorn --app-dir scripts demo_server:app
"""

from __future__ import annotations

import json
import os

import httpx

# Sensible demo defaults; scripts/demo.sh overrides these before import. setdefault
# lets the shell environment win while keeping standalone `uvicorn ...` runnable.
os.environ.setdefault("DATABASE_URL", "sqlite:///./data/demo.db")
os.environ.setdefault("WORKER_ENABLED", "true")
os.environ.setdefault("WORKER_POLL_SECONDS", "0.2")
os.environ.setdefault("LLM_API_KEY", "demo-key-not-a-real-secret")
os.environ.setdefault("LLM_BASE_URL", "http://provider.invalid/v1")
os.environ.setdefault("LLM_MODEL", "triage-demo-model")
os.environ.setdefault("LLM_PRICE_INPUT_PER_MTOK", "1.0")
os.environ.setdefault("LLM_PRICE_OUTPUT_PER_MTOK", "2.0")
os.environ.setdefault("API_KEY", "")
os.environ.setdefault("LOG_LEVEL", "WARNING")

from app.config import Settings  # noqa: E402
from app.services import worker as worker_mod  # noqa: E402
from app.services.llm_client import LlmClient as _RealLlmClient  # noqa: E402

# The fixed classification the mock provider returns for the demo ticket.
_TRIAGE = {
    "intent": "billing",
    "priority": "P2",
    "sentiment": "negative",
    "summary": (
        "Customer was billed twice for their monthly subscription and wants one "
        "duplicate charge reversed."
    ),
}


# The fixed reply draft the mock provider returns once the ticket is triaged.
_REPLY = {
    "reply": (
        "Hi Dana, sorry about the duplicate charge on your subscription. I am looking "
        "into order 5567 now and will follow up once I have checked the billing record."
    )
}


def _handler(request: httpx.Request) -> httpx.Response:
    # The worker makes a classify call and then a reply-draft call; answer each
    # with the shape its prompt asks for, so the draft is not a parse failure.
    messages = json.loads(request.content)["messages"]
    if messages[0]["content"].startswith("You are a support agent drafting"):
        content, usage = _REPLY, {"prompt_tokens": 512, "completion_tokens": 41}
    else:
        content, usage = _TRIAGE, {"prompt_tokens": 418, "completion_tokens": 39}
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": json.dumps(content)}}], "usage": usage},
    )


def _mock_llm_client(settings: Settings) -> _RealLlmClient:
    return _RealLlmClient(settings, transport=httpx.MockTransport(_handler))


# The worker constructs its client as ``LlmClient(settings)``; swap in the mocked one
# before the app (and its worker thread) is built.
worker_mod.LlmClient = _mock_llm_client

from app.main import create_app  # noqa: E402

app = create_app()
