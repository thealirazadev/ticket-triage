# ticket-triage - Rules and Conventions

Binding for anyone implementing ticket-triage. Specific to this project; read alongside `docs/architecture.md`.

## Conventions

### Preferred libraries and patterns
- HTTP layer: FastAPI, one `APIRouter` per resource in `app/routers/`. Routers parse/validate, call a service, shape the response. No business logic, no SQLAlchemy queries beyond trivial lookups, and no provider calls in routers.
- Validation and config: Pydantic v2 models in `app/schemas/`; pydantic-settings in `app/config.py` read once via a cached `get_settings()` dependency.
- Persistence: SQLAlchemy 2.0 in `app/models.py`, session-per-request via the `get_db` dependency, sessions in the worker created per iteration. ORM/`select()` style only - no `text()` SQL with interpolated values, ever.
- Provider access: only through `services/llm_client.py`, using sync httpx with explicit timeouts. Exactly one provider integration; the provider is configuration (`LLM_BASE_URL`, `LLM_MODEL`), not code. Do not add a second client or an SDK.
- LLM output: only `schemas/triage.py::TriageResult` may turn provider text into data. No other code parses model output. Anything that fails `TriageResult` is not data - it is a logged failure.
- Background work: the single worker thread in `services/worker.py`. Do not add FastAPI `BackgroundTasks`, asyncio tasks, or any queue library. The `received` status is the queue.
- Prompts: templates and `PROMPT_VERSION` live in `app/prompts.py` only. Any prompt wording change bumps `PROMPT_VERSION` and requires an eval run before merge.
- IDs `uuid4().hex`; timestamps timezone-aware UTC, serialized ISO-8601; JSON fields snake_case.

### What to avoid
- No async endpoints or async DB drivers; the service is deliberately synchronous end to end.
- No retry/circuit-breaker libraries (tenacity etc.); the ~50-line implementation in `llm_client.py` is the point and stays inspectable.
- No ML/metrics libraries for the eval runner (sklearn etc.); precision/recall/F1 over label lists is a few functions in `evals/run.py`.
- No global mutable state except the circuit-breaker state owned by `llm_client.py`.
- No `print()` outside `evals/run.py` (whose stdout report is its interface); everywhere else, the structured logger.
- No auto-generated Alembic migrations committed unreviewed; write/verify each migration by hand.

### Naming
- PEP 8: `snake_case` functions/variables/modules, `PascalCase` classes, `UPPER_SNAKE` constants.
- Modules named for their role: `llm_client.py`, `classifier.py`, `routing.py`, `worker.py`, `stats.py`, `export.py`.
- Request models end in `Request`, response models in `Out`; the LLM output schema is `TriageResult`.
- Env vars `UPPER_SNAKE`, mirrored by lowercase `Settings` fields. Label values are exact strings from the sets in `docs/architecture.md` - never introduce synonyms or new labels ad hoc.

### Commit format
- Conventional Commits: `type(scope): subject`, imperative, lower case, no trailing period, under ~70 chars.
- Types: `feat`, `fix`, `docs`, `test`, `refactor`, `chore`, `build`. Scopes: `config`, `logging`, `errors`, `db`, `auth`, `tickets`, `webhooks`, `llm`, `classify`, `routing`, `worker`, `reviews`, `rules`, `stats`, `evals`, `ci`.
- One commit per feature/task, exactly as listed per phase in `docs/phases.md`. No batching, no noise splits.

### Dependencies and lockfile
- `uv` with `pyproject.toml`; every dependency pinned exact (`==`); `uv.lock` committed. Dependency changes are their own `build:` commit including the updated lockfile.
- Only dependencies named in `docs/architecture.md` are pre-approved. No blanket upgrades or "latest" pulls without approval.

### Database migrations
- Every schema change is an Alembic migration in `migrations/versions/`, forward-only, applied with `alembic upgrade head`. Never edit an applied migration; fixes are new migrations. Seed data (default routing rules) is also a migration, so every environment converges from `upgrade head` alone.
- Migrations must run on both SQLite and Postgres; avoid dialect-specific DDL, keep CHECK constraints portable.

## Error handling and logging

### Failures to handle explicitly
- **Provider call**: timeout, connection error, 429, 5xx -> bounded retries with backoff; final failure -> `llm_calls` outcome `timeout`/`api_error`, breaker counter incremented, ticket -> `needs_human` with `triage_error = "provider_error"`. Non-429 4xx -> no retry, same fallback, cause logged (auth and config errors must be loud).
- **Provider output**: JSON parse or schema failure -> one repair call; second failure -> `needs_human`, `triage_error = "parse_failed"`, invalid output logged truncated (500 chars max), never stored.
- **Database**: session errors roll back; the worker catches per-ticket exceptions, logs with `ticket_id`, and continues the loop - one poisoned ticket must not kill the worker. An unexpected worker crash is logged CRITICAL and the thread restarts on next process start (documented, not hidden).
- **Eval runner**: missing key/base-url, unreadable dataset, or baseline hash mismatch -> clear `error:` message on stderr and exit 2. A provider failure for a single row marks that row failed and continues.

### Friendly API errors vs detailed logs
- API error responses carry a short human message and a stable machine `code` - never stack traces, SQL, file paths, provider payloads, or httpx internals. Full detail (exception type, upstream status, request id) goes to the log.
- A catch-all handler converts any unhandled exception into `500 internal_error` with the standard body; the trace goes to the log only.

### One consistent JSON error format

```json
{
  "error": {
    "code": "invalid_state",
    "message": "Only tickets in triaged status can be approved."
  }
}
```

Defined codes: `validation_error` (422), `unauthorized` (401), `not_found` (404), `invalid_state` (409), `internal_error` (500). FastAPI/pydantic validation errors are translated into this shape; the default `detail` array is never exposed.

### Structured logging from day one
- `app/logging.py` configures stdlib `logging` to one JSON object per line: `timestamp`, `level`, `logger`, `message`, plus context (`request_id`, `route`, `status_code`, `duration_ms`, `ticket_id`, `error_code`, `llm_outcome`, `circuit_state` as applicable).
- Request-id middleware assigns an id per request, includes it in every log line for that request, and echoes it as `X-Request-ID`. Worker log lines carry `ticket_id` instead.
- Never name a log `extra` field after a reserved `LogRecord` attribute (`filename`, `module`, ...); prefix (`ticket_subject`) instead.

## Security

- **No hardcoded secrets** in source, tests, fixtures, or the eval dataset. Real values in `.env` (git-ignored); `.env.example` kept current with dummies. CI secrets only via the CI secret store.
- **Server-side validation of all input**: field lengths and enums per `docs/api-contracts.md` on every route, including the webhook route (which must also tolerate and ignore unknown extra fields, since webhook providers add them).
- **Parameterized queries only** - guaranteed by using the SQLAlchemy ORM/`select()` exclusively; raw interpolated SQL is banned.
- **Prompt injection**: ticket subject/body/sender are untrusted and are embedded in prompts as delimited data with an instruction that they are content to classify, not instructions. The blast radius is capped by design: the model can only produce a `TriageResult`, so the worst case of a hostile ticket is a wrong label or slanted summary - reviewable, correctable, and measured by the eval set. Summaries are model text derived from untrusted input: clients rendering them must escape as plain text (the API never returns HTML).
- **PII discipline**: ticket bodies are customer data. Never log subject/body/sender content - log ids, lengths, and label values only. The provider necessarily receives ticket text; that is inherent to the product and stated in the README/launch checklist, not hidden.
- **Auth**: optional single API key. When `API_KEY` is set, every route except `GET /health` requires `X-API-Key`, enforced by a dependency using constant-time comparison (`hmac.compare_digest`). The key never appears in logs. Protected/open routes are listed in `docs/api-contracts.md`.
- **Secrets in transit**: `LLM_API_KEY` is sent only to `LLM_BASE_URL` as a bearer header; it is never logged, never echoed in errors, never written to `llm_calls`.

## Simplicity / YAGNI / KISS

- Build only what the current phase in `docs/phases.md` requires. No speculative endpoints, flags, or config.
- Prefer the boring solution: one worker thread, first-match routing, plain dict-based metrics in the eval runner. No plugin systems, no strategy patterns, no per-provider adapters.
- Rule of three before any abstraction. No new wrapper classes, managers, or utils modules without owner approval.
- Before submitting: one self-review pass - "fewer lines without hurting readability?" If yes, rewrite first. A solution over ~150 lines needs a written justification before continuing.
- Validate at the boundary (request schemas, `TriageResult`) and trust internal invariants; do not re-check enums in every layer the data passes through (the DB CHECK constraints are the final backstop, not a pattern to repeat).

## Code style

- Comments sparse, only for non-obvious logic and invariants (the breaker state machine, the repair-call contract). No commented-out code.
- Docstrings concise: one line for simple functions; a short paragraph for services with error behavior and units.
- No emoji anywhere: code, comments, docstrings, logs, docs, commits.
- No authorship attribution of any kind - no "generated by", no tool or assistant names, no co-author trailers - in source, comments, or commit messages.
- Black formats, Ruff lints; both clean before any feature is done.

## Boundaries - never without asking the owner first

- No wholesale file deletion or rewrite; targeted edits only, destructive changes flagged first.
- `docs/PRD.md` and `docs/architecture.md` are the source of truth - never modify them without flagging the proposed change and reason.
- No new dependency without approval (see the pre-approved list in `docs/architecture.md`).
- Ambiguous task -> ask, do not assume.
- Two failed attempts at the same fix -> stop, report what was tried, observed behavior, and a hypothesis. No churning.
- Mid-phase requests not in `docs/PRD.md` -> ask whether to (a) add to the current phase, (b) create a new phase, or (c) log to the Backlog in `docs/phases.md`. Never silently absorb scope.
- Label sets, `PROMPT_VERSION` semantics, and the eval regression gate are contract surface: changing any of them requires owner sign-off plus a baseline update in the same PR.
