"""The one provider integration: a sync httpx call to a chat-completions-style
LLM provider API, with explicit timeouts, bounded retries with backoff, a
process-wide circuit breaker, and one llm_calls row per call.

The provider is configuration (LLM_BASE_URL, LLM_MODEL), not code. Nothing here
parses triage output; that is the classifier's job. This module only decides
whether a call succeeded at the HTTP level and records its cost/latency.
"""

import json
import logging
import random
import time
from dataclasses import dataclass
from uuid import uuid4

import httpx
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import LlmCall

log = logging.getLogger("app.llm")

# 4xx statuses other than 429 are the caller's fault; retrying wastes money.
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class ProviderError(Exception):
    """A provider call failed at the HTTP level after exhausting retries."""

    def __init__(self, outcome: str, detail: str = ""):
        super().__init__(detail or outcome)
        self.outcome = outcome  # "timeout" or "api_error"


class CircuitOpenError(Exception):
    """The breaker is open; no provider call was made."""


@dataclass
class LlmResult:
    content: str
    input_tokens: int | None
    output_tokens: int | None
    call: LlmCall


class CircuitBreaker:
    """In-memory consecutive-failure breaker. State is deliberately ephemeral:
    a restart resets it, worst case one early trial call."""

    def __init__(self, threshold: int, cooldown_seconds: float):
        self._threshold = threshold
        self._cooldown = cooldown_seconds
        self._consecutive_failures = 0
        self._open_until = 0.0

    def allow(self) -> bool:
        """Return True if a call may proceed. After cooldown, allows one trial."""
        if self._open_until and time.monotonic() < self._open_until:
            return False
        return True

    def record_success(self) -> None:
        if self._open_until:
            log.warning("circuit closed", extra={"circuit_state": "closed"})
        self._consecutive_failures = 0
        self._open_until = 0.0

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._threshold:
            self._open_until = time.monotonic() + self._cooldown
            log.warning(
                "circuit opened",
                extra={"circuit_state": "open", "circuit_failures": self._consecutive_failures},
            )


class LlmClient:
    """Synchronous provider client. One instance is shared by the worker so the
    breaker state and connection pool persist across tickets. Tests and the eval
    fixtures mode inject a transport."""

    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        self._settings = settings
        self._breaker = CircuitBreaker(
            settings.circuit_failure_threshold, settings.circuit_cooldown_seconds
        )
        timeout = httpx.Timeout(settings.llm_timeout_seconds, connect=5.0)
        headers = {"Content-Type": "application/json"}
        if settings.llm_api_key:
            headers["Authorization"] = f"Bearer {settings.llm_api_key}"
        self._client = httpx.Client(
            base_url=settings.llm_base_url or "http://provider.invalid",
            timeout=timeout,
            headers=headers,
            transport=transport,
        )

    @property
    def breaker(self) -> CircuitBreaker:
        return self._breaker

    def close(self) -> None:
        self._client.close()

    def _cost(self, input_tokens: int | None, output_tokens: int | None) -> float:
        cost = 0.0
        if input_tokens:
            cost += input_tokens / 1_000_000 * self._settings.llm_price_input_per_mtok
        if output_tokens:
            cost += output_tokens / 1_000_000 * self._settings.llm_price_output_per_mtok
        return round(cost, 6)

    def complete(
        self,
        db: Session,
        messages: list[dict],
        purpose: str,
        ticket_id: str | None = None,
    ) -> LlmResult:
        """Call the provider once (with internal retries) and record one
        llm_calls row. Raises CircuitOpenError when the breaker is open, or
        ProviderError on final HTTP failure."""
        if not self._breaker.allow():
            raise CircuitOpenError()

        payload = {
            "model": self._settings.llm_model,
            "messages": messages,
            "temperature": 0,
        }
        started = time.monotonic()
        outcome, content, usage, detail = self._attempt_with_retries(payload)
        latency_ms = int((time.monotonic() - started) * 1000)

        input_tokens = usage.get("prompt_tokens") if usage else None
        output_tokens = usage.get("completion_tokens") if usage else None
        call = LlmCall(
            id=uuid4().hex,
            ticket_id=ticket_id,
            purpose=purpose,
            model=self._settings.llm_model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=self._cost(input_tokens, output_tokens),
            latency_ms=latency_ms,
            outcome=outcome,
        )
        db.add(call)
        db.flush()

        if outcome != "ok":
            self._breaker.record_failure()
            log.warning(
                "provider call failed",
                extra={"llm_outcome": outcome, "ticket_id": ticket_id, "llm_purpose": purpose},
            )
            raise ProviderError(outcome, detail)

        self._breaker.record_success()
        return LlmResult(
            content=content or "",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            call=call,
        )

    def _attempt_with_retries(self, payload: dict) -> tuple[str, str | None, dict | None, str]:
        """Return (outcome, content, usage, detail). outcome is ok/timeout/api_error."""
        attempts = self._settings.llm_max_retries + 1
        last_outcome = "api_error"
        last_detail = ""
        for attempt in range(attempts):
            try:
                response = self._client.post("/chat/completions", json=payload)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_outcome, last_detail = "timeout", type(exc).__name__
                if attempt < attempts - 1:
                    self._sleep_backoff(attempt)
                    continue
                return last_outcome, None, None, last_detail

            if response.status_code == 200:
                try:
                    content, usage = self._extract(response)
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    # A 200 whose body is not decodable JSON is a broken provider
                    # response, not triage data. Record it as a failure so cost and
                    # outcome are accounted for and the breaker can trip, instead of
                    # letting an unhandled decode error escape the client.
                    last_outcome = "api_error"
                    last_detail = f"malformed body: {type(exc).__name__}"
                    if attempt < attempts - 1:
                        self._sleep_backoff(attempt)
                        continue
                    return last_outcome, None, None, last_detail
                return "ok", content, usage, ""

            last_outcome = "timeout" if response.status_code in {408} else "api_error"
            last_detail = f"status={response.status_code}"
            if response.status_code in _RETRYABLE_STATUS and attempt < attempts - 1:
                self._sleep_backoff(attempt)
                continue
            return "api_error", None, None, last_detail
        return last_outcome, None, None, last_detail

    @staticmethod
    def _extract(response: httpx.Response) -> tuple[str | None, dict | None]:
        data = response.json()
        content = None
        choices = data.get("choices") or []
        if choices:
            content = (choices[0].get("message") or {}).get("content")
        return content, data.get("usage")

    @staticmethod
    def _sleep_backoff(attempt: int) -> None:
        # Roughly 0.5s then 2s, with jitter, to avoid hammering a struggling API.
        base = 0.5 * (4**attempt)
        time.sleep(base + random.uniform(0, 0.25))
