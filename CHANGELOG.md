# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

The project is pre-1.0 and ships from `main`; no versioned release has been
tagged yet. This section records the initial feature set.

### Added

- Ticket ingestion over REST (`POST /tickets`) and a generic email webhook
  (`POST /webhooks/email`), idempotent on the source `external_id`.
- LLM-based classification (intent, priority, sentiment, summary) through a
  strictly validated `TriageResult` schema, with one repair attempt and a
  `needs_human` fallback on repeated failure.
- A single background worker that polls for received tickets and triages them,
  using the database as the queue (no broker).
- A provider client with timeouts, bounded retries with backoff, and an
  in-memory circuit breaker; one `llm_calls` row recorded per call.
- First-match routing rules with read and transactional replace
  (`GET`/`PUT /rules`), seeded with defaults via migration.
- Review workflow: `GET /reviews/pending`, `POST /tickets/{id}/approve`,
  `POST /tickets/{id}/correct`, and gold-label export
  (`GET /corrections/export`) as JSONL in the eval-dataset shape.
- Operational stats (`GET /stats`) covering ticket-flow counts, provider spend,
  and per-queue and per-label breakdowns, with `?since=` filtering.
- Liveness (`GET /health`) and readiness (`GET /ready`) probes; readiness checks
  database connectivity and reports the circuit-breaker state.
- A committed, labeled eval dataset and an eval runner that measures
  classification accuracy against a stored baseline, runnable offline with
  recorded fixtures.
- SQLAlchemy models with CHECK constraints and hand-written Alembic migrations
  (SQLite in development, Postgres in production).
- Structured JSON logging with a request-id, a single `{"error": {"code",
  "message"}}` response shape, and an optional constant-time API-key gate.

### Fixed

- Ticket text containing `</ticket>` or `</triage>` can no longer close its
  delimited block in the classification and reply prompts.
- Request bodies over 1 MiB are rejected with `413 payload_too_large` before
  auth and parsing.
- A provider 200 whose JSON is not a chat-completions envelope is recorded as
  a retryable `api_error`; malformed token counts are stored as null.
- A ticket that raises an unexpected error in the worker is skipped instead of
  being re-picked in a tight loop ahead of every other ticket.
- Concurrent approve/correct on the same ticket now returns `409` to the loser
  instead of overwriting the decision or failing with `500`.
- The README demo mock answers reply-draft calls, and the example session
  matches current output.
- A warning is logged at startup when `API_KEY` is unset.
