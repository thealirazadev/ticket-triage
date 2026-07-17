# ticket-triage - Project Memory

Running log of what is done, what is in flight, and why non-obvious decisions were made. Update after every meaningful chunk of work; log every non-obvious decision with its reason. This file is the handoff state between work sessions.

## Completed

- 2026-07-18: Planning documentation created (README, PRD, architecture, rules, phases, design, testing, api-contracts, launch-checklist, .env.example). Status: docs under review, no implementation started.
- 2026-07-18: Phase 1 (ingest, classify, route) implemented and verified. FastAPI app with structured JSON logging + request-id middleware, single error format, optional API-key auth. SQLAlchemy models + two hand-written Alembic migrations (schema with CHECK constraints, seeded routing rules). Ingestion (`POST /tickets`, `POST /webhooks/email`) is idempotent by external_id. Provider client (httpx) with timeouts, backoff retries, and in-memory circuit breaker records one `llm_calls` row per call. Classifier enforces strict `TriageResult` parse -> one repair call -> `needs_human` fallback. First-match routing engine. Single daemon polling worker. List/detail endpoints. Base CI (ruff, black, pytest). 53 tests green, provider fully mocked via `httpx.MockTransport`.
- 2026-07-18: Phase 2 (eval harness + stats) implemented and verified. Labeled dataset (68 rows, coverage floors met). Eval runner (`python -m evals.run`) runs the real pipeline, computes per-field accuracy + per-label P/R/F1 + macro-F1 + confusion + parse-failure/cost, and gates against `evals/baseline.json` (exit 0/1/2). Offline `--fixtures` mode uses committed `evals/fixtures.jsonl` so the eval runs without a provider key; the baseline was produced from that fixtures run (offline stand-in, not a real provider run). CI eval job is path-gated and opt-in via `vars.RUN_EVAL`, so no real provider calls happen by default. `GET /stats` aggregates ticket counts and llm spend (calls, outcome split, tokens, cost, avg/p95 latency) with `?since=` filtering. 71 tests green.

## In progress

- (Phase 2 complete and committed. Next: Phase 3 - review workflow, corrections, export, rules management.)

## Decisions log

- 2026-07-18: Background work is a single polling worker thread with the database as the queue (no broker, no BackgroundTasks) - restarts resume unprocessed tickets, webhook replays cannot double-enqueue, and the failure paths stay readable. Limits documented in `docs/architecture.md`.
- 2026-07-18: LLM output enters the system only through the strict `TriageResult` schema, with one repair call and then a `needs_human` fallback; label enums are additionally CHECK-constrained in the database.
- 2026-07-18: Eval baseline and dataset are committed repo files; the CI eval job is path-gated (prompt/classifier/client/evals changes) to bound provider spend.
- 2026-07-18: Commit granularity increased at owner request mid-build: each discrete change (schema, model, migration, endpoint, service, test file) is its own commit rather than one commit per phase feature. `main.py` and the tickets router were built up incrementally so every commit stays importable.
- 2026-07-18: Timestamps serialize as ISO-8601 with a trailing `Z` via a `PlainSerializer` annotated type; SQLite returns naive datetimes, so a missing tzinfo is treated as UTC.
- 2026-07-18: The email-webhook `from` -> `sender` mapping uses a `model_validator(mode="before")` instead of a pydantic field alias; FastAPI re-applies field aliases in a way that emits a spurious `UnsupportedFieldAttributeWarning`.
- 2026-07-18: One `llm_calls` row per provider call. `complete()` records `ok`/`timeout`/`api_error`; the classifier flips a recorded `ok` row to `parse_error` when 200-response content fails `TriageResult` validation, so the parse-failed call keeps a single accurate row.
