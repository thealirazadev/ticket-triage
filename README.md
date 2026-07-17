# ticket-triage

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
