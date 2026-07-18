# ticket-triage - Architecture

## Overview

ticket-triage is a single FastAPI process with one background worker thread. HTTP routers stay thin and delegate to services; services own the pipeline stages, the SQLAlchemy models, and the one external dependency (the LLM provider API, called over httpx). The database is both the store and the work queue: ingestion writes a `received` ticket and returns immediately; the worker polls for `received` tickets, classifies and routes them, and records every provider call. There is no broker, no cache, and no frontend.

## Flow

```
            POST /tickets            POST /webhooks/email
                 |                          |
                 |    (map message_id/from/subject/text -> ticket fields)
                 v                          v
   ingest ->  validate (pydantic, server-side)
          ->  idempotency check on external_id  (exists -> return existing, 200)
          ->  insert tickets row, status = "received"      -> respond 201
                                 |
                                 |  (request cycle ends here)
                                 v
   worker (single thread, polls every WORKER_POLL_SECONDS)
          ->  pick oldest "received" ticket
          ->  classify   (services/classifier.py)
          |       build prompt (app/prompts.py, PROMPT_VERSION)
          |       call provider (services/llm_client.py: timeout, retries,
          |                      backoff, circuit breaker; llm_calls row per call)
          |       parse JSON -> validate against TriageResult schema
          |       on failure: one repair call with the invalid output + errors
          |       on second failure or provider/circuit failure:
          |            status = "needs_human", queue = DEFAULT_QUEUE,
          |            triage_error recorded, NO triage row written
          ->  route      (services/routing.py: first matching rule -> queue,
          |               no match -> DEFAULT_QUEUE)
          ->  update ticket: status = "triaged", queue, triages row
                                 |
                                 v
   review ->  GET /reviews/pending          (triaged + needs_human, oldest first)
          ->  POST /tickets/{id}/approve    (triaged -> approved)
          ->  POST /tickets/{id}/correct    (triaged|needs_human -> corrected,
          |                                  corrections row, re-route by labels)
          ->  GET /corrections/export       (gold-labeled JSONL)

   ops    ->  GET /stats                    (ticket counts, tokens, cost, latency)
   evals  ->  python -m evals.run           (dataset -> pipeline -> metrics
                                             -> compare baseline -> exit code)
```

### Why a polling worker thread (and not BackgroundTasks or a broker)

- FastAPI `BackgroundTasks` ties work to the request lifecycle: a crash or restart between response and task loses the work, and a webhook burst runs unbounded concurrent provider calls. With DB polling, `status = "received"` *is* the queue entry: restarts resume exactly where they left off, and idempotent ingestion means webhook replays never enqueue twice.
- A broker (Celery/Redis) buys parallelism and delivery guarantees this project does not need and would double the operational surface. Documented limits of the chosen design: one process, one worker, tickets triaged serially, so throughput is roughly one ticket per provider round-trip (a few seconds each) and triage latency grows linearly under a burst; a crash mid-classification re-processes that ticket on restart (at-least-once - the second attempt overwrites, the only waste is one extra provider call).
- The worker is a plain daemon thread running a synchronous loop (sync SQLAlchemy session, sync httpx client), started and stopped by the FastAPI lifespan hook. `WORKER_ENABLED=false` turns it off (tests drive `process_next_ticket()` directly). No asyncio in the pipeline keeps the failure and retry logic easy to read and test.

## Proposed folder / file tree

```
ticket-triage/
├── app/
│   ├── __init__.py
│   ├── main.py                 # app factory, routers, exception handlers, lifespan (worker start/stop)
│   ├── config.py               # Settings (pydantic-settings): env vars, defaults
│   ├── logging.py              # structured JSON logging; request-id middleware
│   ├── errors.py               # AppError types + exception handlers + error JSON shape
│   ├── deps.py                 # get_settings, get_db (session per request), require_api_key
│   ├── db.py                   # engine + sessionmaker from DATABASE_URL
│   ├── models.py               # SQLAlchemy models: Ticket, Triage, LlmCall, Correction, RoutingRule
│   ├── prompts.py              # classification + repair prompt templates, PROMPT_VERSION
│   ├── routers/
│   │   ├── __init__.py
│   │   ├── health.py           # GET /health
│   │   ├── tickets.py          # POST /tickets, GET /tickets, GET /tickets/{id}
│   │   ├── webhooks.py         # POST /webhooks/email
│   │   ├── reviews.py          # GET /reviews/pending, approve, correct, GET /corrections/export
│   │   ├── rules.py            # GET /rules, PUT /rules
│   │   └── stats.py            # GET /stats
│   ├── schemas/
│   │   ├── __init__.py
│   │   ├── tickets.py          # TicketCreateRequest, EmailWebhookRequest, TicketOut, TicketListOut
│   │   ├── triage.py           # TriageResult: the strict LLM-output schema (enums + summary)
│   │   ├── reviews.py          # CorrectRequest, ExportLine
│   │   ├── rules.py            # RuleIn, RuleOut, RulesReplaceRequest
│   │   ├── stats.py            # StatsOut
│   │   └── errors.py           # ErrorBody, ErrorResponse
│   └── services/
│       ├── __init__.py
│       ├── llm_client.py       # httpx call to provider: timeouts, retries+backoff, circuit breaker,
│       │                       #   llm_calls recording (tokens, cost, latency, outcome)
│       ├── classifier.py       # classify_ticket(text fields) -> TriageResult | TriageFailure
│       ├── routing.py          # resolve_queue(labels, rules) -> queue name
│       ├── worker.py           # polling loop; process_next_ticket(session) does one ticket
│       ├── stats.py            # aggregate queries for GET /stats
│       └── export.py           # gold-label JSONL assembly for corrections export
├── migrations/                 # Alembic environment
│   ├── env.py
│   └── versions/               # 0001_init.py, 0002_seed_routing_rules.py, ...
├── evals/
│   ├── __init__.py
│   ├── run.py                  # eval runner CLI (python -m evals.run)
│   ├── dataset.jsonl           # labeled tickets, committed (60+ rows)
│   └── baseline.json           # stored baseline metrics, committed
├── tests/
│   ├── conftest.py             # fixtures: temp SQLite DB (alembic upgrade), TestClient,
│   │                           #   mocked provider transport, ticket factory
│   ├── test_ingestion.py
│   ├── test_llm_client.py
│   ├── test_classifier.py
│   ├── test_routing.py
│   ├── test_worker.py
│   ├── test_reviews.py
│   ├── test_rules_api.py
│   ├── test_stats.py
│   ├── test_auth.py
│   └── test_eval_runner.py
├── .github/
│   └── workflows/
│       └── ci.yml              # test job (ruff/black/pytest); eval job (path-gated + manual)
├── .env.example
├── .gitignore
├── alembic.ini
├── pyproject.toml
├── uv.lock
└── README.md
```

## Tech stack with rationale

Major versions below; exact versions are pinned (`==`) in `pyproject.toml` at install time and `uv.lock` is committed.

- **Python 3.12** - current stable; this repo's baseline (the sibling askdocs project targets 3.11+, but nothing here needs to support below 3.12, so the floor is raised deliberately).
- **FastAPI 0.115 + Uvicorn 0.34** - typed request/response models, OpenAPI for free, dependency injection for auth/session/settings. Endpoints are synchronous (threadpool) to match the sync worker and DB layer.
- **Pydantic v2 + pydantic-settings 2** - one typing system for request validation, the strict `TriageResult` schema that gates LLM output, and env-driven config read once at startup.
- **SQLAlchemy 2.0 + Alembic 1.x** - ORM with typed models and parameterized queries everywhere, and real migrations. Chosen over stdlib `sqlite3` (which askdocs uses) because this service must run the same schema on SQLite in dev and Postgres in prod; SQLAlchemy abstracts the dialect and Alembic owns schema history including the seeded routing rules.
- **SQLite (dev) / PostgreSQL 16 via `psycopg` 3 (prod)** - `DATABASE_URL` selects the backend. `psycopg[binary]` sits in an optional `prod` dependency group so dev installs stay light. Enum-valued columns get CHECK constraints so both backends refuse out-of-set labels.
- **httpx 0.28** - the only HTTP client, used synchronously with explicit connect/read timeouts for every call to the LLM provider API. Retries with backoff and the circuit breaker are implemented in `llm_client.py` (~50 lines) rather than pulling in a retry library. Tests use `httpx.MockTransport`, so no mocking dependency is needed.
- **LLM provider API** - a chat-completions-style HTTP endpoint; `LLM_BASE_URL`, `LLM_MODEL`, and `LLM_API_KEY` come from env, so the provider is swappable config, not code. The service requests JSON output by prompt instruction and trusts nothing: every response passes through `TriageResult` validation. Token usage is read from the standard `usage` object when present; when absent, tokens are recorded as null and cost as 0 (never estimated).
- **pytest 8 / Ruff / Black** - tests, lint, format; managed with `uv`, lockfile committed.

No other runtime dependencies. Anything beyond this list requires approval per `docs/rules.md`.

## Data model

Five tables, all keyed by `uuid4().hex` TEXT ids except `routing_rules` (small, integer-ordered). Timestamps are timezone-aware UTC, serialized as ISO-8601.

### `tickets`

| column        | type      | notes                                                              |
|---------------|-----------|--------------------------------------------------------------------|
| id            | TEXT PK   | uuid4 hex                                                          |
| external_id   | TEXT      | UNIQUE NOT NULL; idempotency key from the source system            |
| channel       | TEXT      | CHECK in (`email`, `web`, `api`)                                   |
| sender        | TEXT      | NOT NULL; email address or opaque identifier, max 320 chars        |
| subject       | TEXT      | NOT NULL, 1-500 chars (webhook empty subject stored as `(no subject)`) |
| body          | TEXT      | NOT NULL, 1-50,000 chars                                           |
| status        | TEXT      | CHECK in (`received`, `triaged`, `needs_human`, `approved`, `corrected`) |
| queue         | TEXT NULL | assigned queue name; null until triage                             |
| triage_error  | TEXT NULL | why triage fell back: `parse_failed`, `provider_error`, `circuit_open` |
| created_at    | TIMESTAMP | UTC                                                                |
| updated_at    | TIMESTAMP | UTC                                                                |

Status transitions: `received -> triaged`, `received -> needs_human`, `triaged -> approved`, `triaged -> corrected`, `needs_human -> corrected`. Nothing else; endpoints guard with `409 invalid_state`.

### `triages` (current machine triage; absent for `needs_human` tickets)

| column         | type    | notes                                                       |
|----------------|---------|-------------------------------------------------------------|
| id             | TEXT PK | uuid4 hex                                                   |
| ticket_id      | TEXT FK | UNIQUE -> tickets.id (one current triage per ticket)        |
| intent         | TEXT    | CHECK in the intent set (see Label sets)                    |
| priority       | TEXT    | CHECK in (`P1`,`P2`,`P3`,`P4`)                              |
| sentiment      | TEXT    | CHECK in (`negative`,`neutral`,`positive`)                  |
| summary        | TEXT    | 1-600 chars, 2-3 sentences requested from the model         |
| model          | TEXT    | `LLM_MODEL` at classification time                          |
| prompt_version | TEXT    | `PROMPT_VERSION` at classification time                     |
| attempts       | INTEGER | 1 = clean parse, 2 = repair call was needed                 |
| created_at     | TIMESTAMP |                                                            |

### `llm_calls` (one row per provider HTTP call, success or not)

| column        | type      | notes                                                       |
|---------------|-----------|-------------------------------------------------------------|
| id            | TEXT PK   | uuid4 hex                                                   |
| ticket_id     | TEXT FK NULL | null for eval-runner calls made outside ticket flow      |
| purpose       | TEXT      | CHECK in (`classify`, `repair`)                             |
| model         | TEXT      |                                                             |
| input_tokens  | INTEGER NULL | from provider `usage`; null when not reported            |
| output_tokens | INTEGER NULL |                                                          |
| cost_usd      | NUMERIC(10,6) | tokens x env prices; 0 when prices unset or tokens null |
| latency_ms    | INTEGER   | wall time of the HTTP call including retries' final attempt |
| outcome       | TEXT      | CHECK in (`ok`, `parse_error`, `timeout`, `api_error`)      |
| created_at    | TIMESTAMP |                                                             |

`parse_error` marks a call that returned 200 but whose body failed JSON/schema validation (the repair call gets its own row). `GET /stats` aggregates this table.

### `corrections` (human gold labels for corrected tickets)

| column     | type      | notes                                                   |
|------------|-----------|---------------------------------------------------------|
| id         | TEXT PK   | uuid4 hex                                               |
| ticket_id  | TEXT FK   | UNIQUE -> tickets.id                                    |
| intent     | TEXT      | CHECK, same sets as triages; full snapshot, not a diff  |
| priority   | TEXT      | CHECK                                                   |
| sentiment  | TEXT      | CHECK                                                   |
| note       | TEXT NULL | optional free-text reviewer note, max 1000 chars        |
| created_at | TIMESTAMP |                                                         |

A correction stores all three labels even when only one changed, so `GET /corrections/export` needs no reconstruction. Approvals need no row: an approved ticket's gold labels are its triage labels.

### `routing_rules`

| column    | type       | notes                                              |
|-----------|------------|----------------------------------------------------|
| id        | INTEGER PK | autoincrement                                      |
| position  | INTEGER    | UNIQUE; evaluation order, ascending                |
| intent    | TEXT NULL  | null = wildcard; else CHECK against intent set     |
| priority  | TEXT NULL  | null = wildcard                                    |
| sentiment | TEXT NULL  | null = wildcard                                    |
| queue     | TEXT       | NOT NULL; slug 1-64 chars `[a-z0-9_-]`             |

First match wins; a rule's non-null conditions are AND-ed; every rule must have at least one non-null condition. Seeded by migration `0002`:

| position | condition            | queue     |
|----------|----------------------|-----------|
| 1        | priority = P1        | urgent    |
| 2        | intent = billing     | billing   |
| 3        | intent = refund      | billing   |
| 4        | intent = bug         | technical |
| 5        | intent = account_access | security |

No match -> `DEFAULT_QUEUE` (env, default `general`). `needs_human` tickets always get `DEFAULT_QUEUE`.

## Label sets

Fixed; changing them is a PRD-level change plus a migration (CHECK constraints) plus a new prompt version plus a baseline update.

- **intent**: `billing`, `bug`, `how_to`, `feature_request`, `account_access`, `refund`, `other`
- **priority**: `P1` (outage, data loss, security incident), `P2` (core feature broken, no workaround), `P3` (degraded, workaround exists), `P4` (question or minor request). The definitions live in the classification prompt.
- **sentiment**: `negative`, `neutral`, `positive`

## Classification contract (structured-output discipline)

- `app/prompts.py` holds the templates and `PROMPT_VERSION` (bump on any wording change; it is recorded on every triage and in the eval baseline, so metric shifts are attributable).
- The prompt states the task, the exact label sets with the priority definitions, and requires a single JSON object with keys `intent`, `priority`, `sentiment`, `summary` and nothing else. Ticket fields are embedded inside clearly delimited blocks and explicitly framed as data to classify, not instructions to follow; bodies over 12,000 chars are truncated for the prompt (logged; the full body stays in the DB). Temperature 0.
- The response body's message content is parsed as JSON (tolerating surrounding code fences) and validated by the `TriageResult` pydantic model: enum-checked labels (values lowercased/trimmed before checking, except priority which is uppercased - cheap normalization avoids pointless repair calls), summary 1-600 chars. Unknown extra keys are ignored.
- On parse or validation failure: exactly one repair call containing the invalid output and the validation errors, demanding corrected JSON only. On a second failure: no triage row, ticket -> `needs_human` + `DEFAULT_QUEUE` + `triage_error = "parse_failed"`. Invalid model output is never persisted anywhere except as a truncated snippet in a WARNING log line.

## Provider resilience

All in `services/llm_client.py`, unit-tested against `httpx.MockTransport`:

- **Timeouts**: connect 5s, read `LLM_TIMEOUT_SECONDS` (default 30) on every call. No unbounded waits.
- **Retries**: on timeout, connection error, 429, or 5xx - up to `LLM_MAX_RETRIES` (default 2) extra attempts with exponential backoff plus jitter (roughly 0.5s then 2s). 4xx other than 429 fails immediately (retrying a bad request wastes money).
- **Circuit breaker**: in-memory consecutive-failure counter. At `CIRCUIT_FAILURE_THRESHOLD` (default 5) the circuit opens for `CIRCUIT_COOLDOWN_SECONDS` (default 60): the worker skips the provider entirely and marks tickets `needs_human` with `triage_error = "circuit_open"` (cheap, no timeout waits). After cooldown, one trial call: success closes the circuit, failure reopens it. State transitions are logged at WARNING.
- Trade-off, stated plainly: during an outage tickets accumulate in the human queue rather than waiting invisibly for recovery; they are not auto-re-triaged (see Non-goals). Ingestion never touches the provider, so `POST /tickets` stays fast and available throughout.

## Eval harness

- `evals/dataset.jsonl`: 60+ hand-labeled synthetic tickets, one JSON object per line: `{"external_id", "channel", "sender", "subject", "body", "intent", "priority", "sentiment"}`. Coverage floor: every intent at least 6 times, every priority at least 8, every sentiment at least 10; length variance including at least 3 bodies over 2,000 chars and 3 one-liners. No real personal data.
- `evals/run.py` (`uv run python -m evals.run`): for each row, calls `classifier.classify_ticket` (the real pipeline - real prompt, real client, DB not involved), then computes per field (intent, priority, sentiment): accuracy, per-label precision/recall/F1, macro-F1, and a confusion summary (top mismatched gold->predicted pairs). Parse failures count as wrong on all fields and are reported separately. Writes `evals/latest.json` (git-ignored), prints the report per `docs/design.md`.
- `evals/baseline.json` (committed): `{created_at, model, prompt_version, dataset_sha256, n, metrics: {intent|priority|sentiment: {accuracy, macro_f1}}, parse_failure_rate}`. The gate fails (exit 1) when any field's accuracy or macro-F1 is more than `EVAL_REGRESSION_THRESHOLD` (default 0.02) below baseline, or parse_failure_rate exceeds baseline by more than the threshold. A `dataset_sha256` mismatch is exit 2 with an explicit message (changed dataset invalidates the comparison; rerun with `--update-baseline` in the same PR).
- Flags: `--dataset`, `--baseline`, `--limit N` (smoke runs), `--update-baseline`, `--no-fail-on-regression`. Exit codes: 0 pass, 1 regression, 2 config/runtime error.
- CI (`.github/workflows/ci.yml`): job `test` (ruff, black --check, pytest; provider always mocked, no secrets) on every push/PR. Job `eval` calls the real provider, so it needs the `LLM_API_KEY` secret plus `LLM_BASE_URL`/`LLM_MODEL` config; it runs on pull requests touching `app/prompts.py`, `app/services/classifier.py`, `app/services/llm_client.py`, or `evals/**`, and on manual dispatch. Path-gating keeps spend bounded: roughly 60-70 provider calls per eval run, only when classification behavior can actually have changed.

## Where state lives

- **Database (`DATABASE_URL`)** - the only persistent state: tickets, triages, llm_calls, corrections, routing_rules, plus Alembic's `alembic_version`. SQLite file under `./data/` in dev (directory created at startup); Postgres in prod.
- **Process memory** - circuit-breaker state (counter + open-until timestamp) and the settings object. Deliberately ephemeral: a restart resets the breaker, which is acceptable (worst case is one early trial call).
- **Repo files** - eval dataset and baseline are versioned artifacts, not runtime state. `evals/latest.json` is a git-ignored local artifact.
- **Client/UI state** - none; there is no frontend.

## External dependencies and required environment variables

External dependencies at runtime: the LLM provider API (network) and the database. Nothing else.

| variable                   | required | default                     | purpose                                            |
|----------------------------|----------|-----------------------------|----------------------------------------------------|
| `LLM_API_KEY`              | yes      | -                           | Auth for the LLM provider API.                     |
| `LLM_BASE_URL`             | yes      | -                           | Provider base URL (chat-completions-style).        |
| `LLM_MODEL`                | yes      | -                           | Model id sent to the provider.                     |
| `LLM_TIMEOUT_SECONDS`      | no       | `30`                        | Read timeout per provider call.                    |
| `LLM_MAX_RETRIES`          | no       | `2`                         | Extra attempts on timeout/429/5xx.                 |
| `LLM_PRICE_INPUT_PER_MTOK` | no       | `0`                         | USD per 1M input tokens, for cost rows.            |
| `LLM_PRICE_OUTPUT_PER_MTOK`| no       | `0`                         | USD per 1M output tokens.                          |
| `DATABASE_URL`             | no       | `sqlite:///./data/tickets.db` | SQLAlchemy URL; Postgres in prod.                |
| `API_KEY`                  | no       | (unset)                     | When set, all routes except `/health` require it.  |
| `DEFAULT_QUEUE`            | no       | `general`                   | Queue for no-match routing and `needs_human`.      |
| `WORKER_ENABLED`           | no       | `true`                      | Start the triage worker thread.                    |
| `WORKER_POLL_SECONDS`      | no       | `2`                         | Worker poll interval.                              |
| `CIRCUIT_FAILURE_THRESHOLD`| no       | `5`                         | Consecutive failures before the circuit opens.     |
| `CIRCUIT_COOLDOWN_SECONDS` | no       | `60`                        | How long the circuit stays open.                   |
| `EVAL_REGRESSION_THRESHOLD`| no       | `0.02`                      | Allowed metric drop vs baseline.                   |
| `LOG_LEVEL`                | no       | `INFO`                      | Structured logger level.                           |

`.env.example` mirrors this table with dummy values.
