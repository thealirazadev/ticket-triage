# ticket-triage - Testing

## Strategy

- **pytest** is the framework. The LLM provider API is always mocked in tests - `pytest` makes zero network calls and needs no `LLM_API_KEY`. Real provider calls happen in exactly two places: manual QA and the CI eval job (which is a quality gate, not a test).
- **Unit tests** for the pure logic: `TriageResult` parsing/normalization (fences, casing, extra keys, every rejection path), the repair-then-fallback sequence, the rule engine (order, wildcards, AND conditions, no-match default), cost math, eval metric math (precision/recall/F1/macro-F1, confusion, parse-failure accounting), and the regression gate (pass, fail, hash mismatch).
- **`llm_client` tests** run against `httpx.MockTransport`: timeout and 5xx retries with backoff (sleep patched), 429 retried, 400 not retried, breaker open/cooldown/trial-call transitions, and one `llm_calls` row per call with correct outcome/latency/cost.
- **Integration tests** drive the HTTP API through FastAPI's `TestClient` with a real temporary SQLite database migrated by `alembic upgrade head` (so migrations, CHECK constraints, and seeds are genuinely exercised) and the provider transport mocked. The worker thread stays off (`WORKER_ENABLED=false`); tests invoke `worker.process_next_ticket(session)` directly for deterministic triage.
- **Manual QA** covers what automation cannot: real-provider triage quality, eval report readability, and the checklists in `docs/phases.md`.

### What is mocked vs real

- **Mocked**: every provider HTTP exchange, via `httpx.MockTransport` handlers returning canned chat-completions-style bodies (valid triage JSON, garbage, fenced JSON, missing `usage`, 429/500/timeout). No mocking library needed.
- **Real (but temporary)**: SQLite database per test (tmp path), Alembic migrations, the full FastAPI stack, and the eval runner's metric/gate code (fed by a deterministic fake classifier).

### Test layout

- `tests/conftest.py` - fixtures: `db` (tmp SQLite + `alembic upgrade head`), `client` (TestClient with overridden settings, worker off), `provider` (configurable MockTransport handler), `make_ticket` (factory posting a valid ticket).
- `tests/test_ingestion.py` - create, replay idempotency (200, single row), webhook mapping (empty subject, extra fields), every validation bound (subject/body/sender/external_id lengths, bad channel).
- `tests/test_llm_client.py` - timeouts, retry/backoff, 429 vs 400, breaker lifecycle, `llm_calls` recording.
- `tests/test_classifier.py` - clean parse, repair success (attempts=2), double failure -> `TriageFailure(parse_failed)`, normalization, truncation of long bodies.
- `tests/test_routing.py` - rule order, wildcards, AND semantics, empty rule set, default queue.
- `tests/test_worker.py` - received -> triaged happy path, needs_human on parse/provider/circuit failures, per-ticket exception containment, restart-resume (re-run picks up remaining received tickets).
- `tests/test_reviews.py` - pending ordering, approve/correct state machine (every `409`), correction re-routing, export line shape and `since` filter.
- `tests/test_rules_api.py` - list, transactional replace, all validation rejections, empty set.
- `tests/test_stats.py` - aggregates match direct queries, `since` filter, zeros on empty DB.
- `tests/test_auth.py` - open when unset, `401` missing/wrong, health always open.
- `tests/test_eval_runner.py` - metric math on hand-computed fixtures, gate exit codes, dataset-hash mismatch, `--update-baseline`, `--limit`.

Coverage target: every documented error `code`, every `triage_error` value, every `llm_calls` outcome, and every eval exit code produced by at least one test. Meaningful paths over percentages.

## Exact commands

From the project root:

```bash
uv sync                              # install from pyproject + committed uv.lock
uv run alembic upgrade head          # apply migrations (dev database)
uv run ruff check .                  # lint
uv run black --check .               # format check (black . to apply)
uv run pytest                        # full suite, no network, no secrets
uv run pytest tests/test_worker.py   # one file
```

Build gate (Python service - clean sync plus importable app plus migratable schema):

```bash
uv sync
uv run python -c "import app.main"
uv run alembic upgrade head
```

Eval (real provider; costs money; not part of pytest):

```bash
uv run python -m evals.run                     # full run + baseline gate
uv run python -m evals.run --limit 5           # cheap smoke
uv run python -m evals.run --update-baseline   # after an approved metric change
```

## Definition of done for any feature

A feature (one commit per `docs/phases.md`) is done only when, from a clean checkout:

```bash
uv sync
uv run ruff check .
uv run black --check .
uv run python -c "import app.main"
uv run alembic upgrade head
uv run pytest
```

all succeed. After creating or editing files, run these and fix every error before reporting done. If the same failure survives two fix attempts, stop and report per `docs/rules.md`. Changes touching prompts, the classifier, or the client additionally require an eval run (locally or via the CI eval job) before merge.
