# ticket-triage

[![ci](https://github.com/thealirazadev/ticket-triage/actions/workflows/ci.yml/badge.svg)](https://github.com/thealirazadev/ticket-triage/actions/workflows/ci.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

ticket-triage is a support-ticket triage service. Tickets arrive over REST or a generic email-webhook JSON shape; the service classifies each one (intent, priority, sentiment) via an LLM provider API using strictly validated structured output, writes a short summary, and routes it to a queue through configurable rules. Humans review and correct the triage through the API, corrections are exportable as labeled data, and a committed eval dataset plus an eval runner measure classification accuracy against a stored baseline so regressions fail CI instead of shipping.

## Stack

- Python 3.12
- FastAPI + Uvicorn (HTTP API)
- Pydantic v2 / pydantic-settings (validation and configuration)
- SQLAlchemy 2 + Alembic (persistence and migrations; SQLite in dev, Postgres in prod)
- httpx (calls to the LLM provider API, with timeouts, retries, and a circuit breaker)
- pytest, Ruff, Black (tests, lint, format), managed with `uv` and a committed lockfile

See `docs/PRD.md` for scope, `docs/architecture.md` for the design, and `docs/phases.md` for the build order.

## Install

Requires [`uv`](https://docs.astral.sh/uv/) and Python 3.12.

```bash
uv sync                      # install from pyproject + committed uv.lock
cp .env.example .env         # then fill in the LLM provider values
```

`.env` is git-ignored. At minimum set `LLM_API_KEY`, `LLM_BASE_URL`, and
`LLM_MODEL` for triage to run against a real provider; every other variable has
a default (see the table in `docs/architecture.md`).

## Run

```bash
uv run alembic upgrade head          # create the schema and seed routing rules
uv run uvicorn app.main:app          # start the API and the triage worker
```

The service listens on `http://127.0.0.1:8000`. Ingest a ticket:

```bash
curl -X POST http://127.0.0.1:8000/tickets \
  -H "Content-Type: application/json" \
  -d '{"external_id":"t-1","subject":"Refund please","body":"I was charged twice.","sender":"a@b.com","channel":"web"}'
```

The ticket returns immediately with `status: "received"`; the background worker
classifies it within one poll interval and it becomes `triaged` (or
`needs_human` if the provider or its output fails). Poll `GET /tickets/{id}` to
watch the transition, and see `docs/api-contracts.md` for every route.

Ticket text is sent to the configured LLM provider for classification; that is
inherent to the product. Review provider data-retention terms before use.

## Review and routing rules

Reviewers work the queues over the API. List what needs attention (triaged and
needs_human, oldest first), then approve a correct triage or correct the labels:

```bash
curl http://127.0.0.1:8000/reviews/pending

curl -X POST http://127.0.0.1:8000/tickets/<id>/approve

curl -X POST http://127.0.0.1:8000/tickets/<id>/correct \
  -H "Content-Type: application/json" \
  -d '{"intent":"billing","priority":"P3","sentiment":"negative","note":"Duplicate charge."}'
```

A correction stores a full gold-label snapshot and re-routes the ticket by the
corrected labels. Export approvals and corrections as JSONL in the eval-dataset
shape (append the lines to `evals/dataset.jsonl` to grow the eval set):

```bash
curl http://127.0.0.1:8000/corrections/export        # optional ?since=<ISO-8601>
```

Routing rules are first-match (ordered, conditions AND-ed, a null condition is a
wildcard); no match falls to `DEFAULT_QUEUE`. Read or atomically replace them:

```bash
curl http://127.0.0.1:8000/rules

curl -X PUT http://127.0.0.1:8000/rules \
  -H "Content-Type: application/json" \
  -d '{"rules":[{"priority":"P1","queue":"urgent"},{"intent":"billing","queue":"billing"}]}'
```

Rules apply at triage and correction time; already-routed tickets are not
re-routed. See `docs/api-contracts.md` for the full request/response shapes.

## Design decisions

The trade-offs below are the load-bearing ones; the full rationale is in
`docs/architecture.md`.

- **A polling worker thread, not a broker or `BackgroundTasks`.** The database
  is the queue: `status = "received"` *is* the pending entry, so a restart
  resumes exactly where it left off and idempotent ingestion means a webhook
  replay cannot enqueue the same ticket twice. `BackgroundTasks` ties the work
  to the request lifecycle (a crash between response and task loses it, and a
  webhook burst spawns unbounded concurrent provider calls). A broker
  (Celery/Redis) buys parallelism and delivery guarantees this service does not
  need and would double the operational surface. The accepted cost: one process,
  tickets triaged serially, throughput bounded by the provider round-trip, and
  at-least-once processing where a crash mid-classification re-runs one ticket.

- **Structured output: strict schema, one repair, then `needs_human`.** Provider
  text enters the system only through the `TriageResult` pydantic model
  (enum-checked labels, bounded summary); the label enums are additionally
  CHECK-constrained in the database. On a parse or validation failure the
  classifier makes exactly one repair call carrying the invalid output and the
  errors; a second failure writes no triage row and moves the ticket to
  `needs_human` with `triage_error = "parse_failed"`. Invalid model output is
  never persisted, only logged as a truncated snippet. Rejected: trusting the
  model to always return valid JSON, and unbounded repair loops that burn budget
  on a model that cannot recover.

- **Circuit-open routes to `needs_human` with no auto-re-triage.** An in-memory
  consecutive-failure breaker opens after a threshold and, while open, skips the
  provider entirely and marks tickets `needs_human` with
  `triage_error = "circuit_open"` (cheap, no timeout waits). Tickets accumulate
  in the human queue during an outage rather than waiting invisibly for
  recovery, and they are not automatically re-triaged afterward: re-triage is an
  explicit human action, so a burst of provider failures never turns into a
  surprise batch of provider calls later. Ingestion never touches the provider,
  so `POST /tickets` stays fast and available throughout.

- **Replaying an `external_id` returns 200 with the existing ticket.** Ingestion
  is idempotent on `external_id` (the source system's key), backstopped by a
  UNIQUE constraint and a race-safe insert-then-reselect. A first insert returns
  201; a replay returns 200 with the already-stored ticket rather than a
  duplicate row or a 409, so at-least-once webhook delivery is safe to retry.

- **Rules are replaced as a full ordered set (`PUT /rules`), not per-rule CRUD.**
  Routing is first-match over an ordered list, so order is part of the meaning.
  A single transactional replace makes the whole rule set atomic and its
  evaluation order explicit in the request body; per-rule create/update/delete
  would invite partially-applied edits and ambiguous reordering. An empty set is
  legal (everything falls to `DEFAULT_QUEUE`).

- **The committed eval baseline is an offline stand-in.** `evals/baseline.json`
  was produced from the recorded `evals/fixtures.jsonl` run so the eval harness
  and its regression gate work with no provider key and no spend. It is a
  scaffold, not a real quality bar: regenerate it from a real-provider run with
  `--update-baseline` before trusting the gate. The real-provider eval job is
  path-gated and opt-in in CI so it never spends budget on unrelated changes.

## Benchmark

How much throughput the service's own code costs per ticket, with provider time
excluded (the provider is a mocked transport that returns instantly). This is a
ceiling on the pipeline, not an end-to-end figure: in production throughput is
bounded by the provider round-trip, which is seconds per ticket.

```bash
uv run python benchmarks/pipeline.py        # defaults to 2000 tickets x 5 runs
```

Measured on Linux 6.8.0 x86_64, 12 CPUs, Python 3.12.13, SQLite, single thread,
2000 tickets per run, 5 runs, median reported:

| storage                | ingest/sec | triage/sec | combined ingest to routed/sec |
|------------------------|-----------:|-----------:|------------------------------:|
| disk (durable commits) |        140 |        111 |                            54 |
| tmpfs (RAM-backed)     |      1,115 |        475 |                           308 |

`ingest` is the `POST /tickets` path (validation, idempotency check, insert);
`triage` is one `process_next_ticket` (prompt assembly, JSON parse, schema
validation, rule evaluation, writes); `combined` is both stages per ticket.

The two rows say different things. The disk row is bound by durability, not by
this code: every ticket costs one committing `fsync` on ingest and another on
triage, so ~54 tickets/sec is really this machine's commit latency. The tmpfs row
runs the identical code and migrations against a RAM-backed filesystem, which
isolates the pipeline's own CPU cost at ~308 tickets/sec combined. Either way the
service's overhead is far below the provider round-trip, so the provider, not the
pipeline, sets real-world throughput.

## Test

```bash
uv run ruff check .                  # lint
uv run black --check .               # format check (black . to apply)
uv run pytest                        # full suite; provider mocked, no network, no secrets
```

## Evals

`evals/dataset.jsonl` is a committed, labeled dataset (68 synthetic tickets
covering every label). The eval runner executes the real classification pipeline
per row and reports per-field accuracy, per-label precision/recall/F1, a
confusion summary, and parse-failure/cost accounting, then gates the result
against `evals/baseline.json`.

Run it offline with the committed recorded fixtures (no provider key needed):

```bash
uv run python -m evals.run --fixtures evals/fixtures.jsonl        # full run + gate
uv run python -m evals.run --fixtures evals/fixtures.jsonl --limit 5   # smoke run (gate skipped)
```

Against the real provider (spends provider budget), omit `--fixtures` so the
pipeline calls the configured `LLM_BASE_URL`:

```bash
uv run python -m evals.run                      # requires LLM_API_KEY/LLM_BASE_URL/LLM_MODEL
uv run python -m evals.run --update-baseline    # rewrite the baseline after an approved change
```

Exit codes: `0` pass (or gate not applicable), `1` regression beyond
`EVAL_REGRESSION_THRESHOLD`, `2` a config or dataset/baseline-hash error. Set
`NO_COLOR=1` for plain output. The committed `evals/baseline.json` was produced
from the recorded fixtures as an offline stand-in; regenerate it from a real
provider run with `--update-baseline` before relying on it as a quality bar.

The CI `eval` job runs the real-provider eval, gated to manual dispatch or to
pull requests that change classification behavior (`app/prompts.py`,
`app/services/classifier.py`, `app/services/llm_client.py`, or `evals/**`) with a
funded key configured. It never runs on the default test job, which needs no
secrets.
