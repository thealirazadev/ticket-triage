# ticket-triage - Phases

Rule: phase N+1 does not start until the owner approves phase N. Within a phase, commit in the listed order - one commit per feature/task, Conventional Commits.

Ordering rationale: Phase 1 is the whole triage pipeline (ingest -> classify -> route) including the structured-output discipline, the provider outage path, and per-call cost/latency recording - the differentiators are load-bearing, not garnish, so they cannot land later. Phase 2 adds the measurement layer (eval harness + CI gate, stats endpoint). Phase 3 adds the human loop (review, corrections, export, rules management).

---

## Phase 1 - Ingest, classify, route

**Goal**: a ticket POSTed to the API ends up `triaged` with validated labels, a summary, a queue, and recorded provider cost/latency - or `needs_human` when the provider or its output fails. Includes config, structured logging, the error format, Alembic migrations, optional auth, the worker, and base CI (lint + tests).

### Expected commits
1. `build: scaffold project with pyproject, uv lock, ruff and black config`
2. `chore: add env example and gitignore`
3. `feat(config): add settings module reading environment variables`
4. `feat(logging): add structured json logging and request-id middleware`
5. `feat(errors): add error schema, app error types, and exception handlers`
6. `feat(app): create fastapi app and register health route`
7. `feat(db): add sqlalchemy models and initial alembic migration`
8. `feat(db): add seed migration for default routing rules`
9. `feat(auth): add optional api-key dependency for all data routes`
10. `feat(tickets): add ingestion endpoint with validation and idempotency`
11. `feat(webhooks): add email webhook ingestion route`
12. `feat(llm): add provider client with timeouts, retries, and circuit breaker`
13. `feat(classify): add strict-schema classifier with repair retry and fallback`
14. `feat(routing): add first-match rule engine`
15. `feat(worker): add polling worker wiring classify and routing`
16. `feat(tickets): add list and detail endpoints`
17. `build(ci): add workflow running lint, format check, and tests`
18. `test(phase1): add unit and integration tests with mocked provider`
19. `docs: add run and test instructions to readme`

### Definition of done
- `uv run alembic upgrade head` creates the schema (tables + CHECK constraints + seeded rules) on a fresh SQLite file; `uv run uvicorn app.main:app` starts, serves `GET /health` -> `{"status":"ok"}`, and starts the worker (log line confirms).
- `POST /tickets` and `POST /webhooks/email` validate per `docs/api-contracts.md`, return `201` with `status: "received"`, and return `200` with the existing ticket on `external_id` replay - no duplicate rows.
- The worker triages a received ticket within one poll interval plus provider latency: `status: "triaged"`, enum-valid labels, 2-3 sentence summary, queue from the first matching seeded rule (no match -> `DEFAULT_QUEUE`).
- Structured-output discipline verified by tests: invalid JSON or out-of-schema output triggers exactly one repair call; a second failure yields `needs_human` + `DEFAULT_QUEUE` + `triage_error: "parse_failed"` and no triage row. DB CHECK constraints reject out-of-enum labels.
- Outage path verified by tests against a mock transport: timeouts/429/5xx retried with backoff up to `LLM_MAX_RETRIES`; non-429 4xx not retried; after `CIRCUIT_FAILURE_THRESHOLD` consecutive failures the breaker opens and tickets go to `needs_human` (`triage_error: "circuit_open"`) with no provider call until cooldown; a trial call then closes or reopens it.
- Every provider call (ok, parse_error, timeout, api_error, repair) writes one `llm_calls` row with tokens (null when unreported), cost from env prices, latency_ms, and outcome.
- With `API_KEY` set, all routes except `/health` return `401` without the right `X-API-Key`; comparison constant-time; key never logged.
- All errors use the single JSON error format; no stack traces leak. Logs are JSON lines with request ids (worker lines carry `ticket_id`); no ticket text or secrets in logs.
- CI workflow runs `ruff check`, `black --check`, `pytest` on push/PR and is green. `uv run ruff check .`, `uv run black --check .`, `uv run pytest` pass locally; provider is mocked in all tests.

### Manual test checklist
- [ ] Start the server; `curl /health` returns `{"status":"ok"}`; worker start logged.
- [ ] POST a ticket (real provider configured); poll `GET /tickets/{id}` until `triaged`; labels are from the enums, summary reads sensibly, queue matches the seeded rules, `metrics` shows tokens/cost/latency.
- [ ] POST the same `external_id` again; confirm `200`, same id, still one row, state untouched.
- [ ] POST to `/webhooks/email` with an extra unknown field and no subject; confirm `201`, `channel: "email"`, subject `(no subject)`.
- [ ] POST with `channel: "fax"`; confirm `422 validation_error` in the standard shape.
- [ ] POST a body over 50,000 chars; confirm `422`; POST a 40,000-char body; confirm it triages (long input path).
- [ ] Set `LLM_BASE_URL` to an unreachable address; POST a ticket; confirm ingestion still `201`, then `needs_human` with `triage_error: "provider_error"` after the retry window; `llm_calls` rows show `timeout`/`api_error`.
- [ ] Keep posting until the breaker opens (threshold 5); confirm WARNING log, and the next ticket goes `needs_human` (`circuit_open`) near-instantly with no new provider call.
- [ ] Stop the server with a ticket still `received`; restart; confirm the worker picks it up.
- [ ] Set `API_KEY`; confirm `401` without header, success with it, `/health` still open.
- [ ] Inspect logs: JSON lines, request ids, ticket ids; no subject/body text, no keys.

---

## Phase 2 - Eval harness and operational stats

**Goal**: classification quality is measured, gated in CI, and operational cost is visible. Ships the labeled dataset, the eval runner with baseline comparison, the CI eval job, and `GET /stats`.

### Expected commits
1. `feat(evals): add labeled ticket dataset`
2. `feat(evals): add eval runner with per-label metrics and confusion summary`
3. `feat(evals): add baseline comparison with regression exit code`
4. `chore(evals): commit initial baseline from a real provider run`
5. `build(ci): add path-gated eval job using the provider secret`
6. `feat(stats): add stats endpoint with cost and latency aggregates`
7. `test(phase2): cover metric math, regression gate, and stats`
8. `docs: document eval workflow in readme`

### Definition of done
- `evals/dataset.jsonl` has 60+ labeled rows meeting the coverage floor in `docs/architecture.md` (every intent >= 6, every priority >= 8, every sentiment >= 10, length variance), synthetic, no real personal data.
- `uv run python -m evals.run` executes the real pipeline per row and prints, per field: accuracy, per-label precision/recall/F1, macro-F1, confusion summary, and parse-failure count, formatted per `docs/design.md`; writes git-ignored `evals/latest.json`.
- Baseline gate: exit 0 within threshold, exit 1 on regression (any field's accuracy or macro-F1 more than `EVAL_REGRESSION_THRESHOLD` below baseline, or parse-failure rate up by more), exit 2 on config errors or dataset-hash mismatch, each with a clear message. `--update-baseline`, `--limit`, `--dataset`, `--baseline`, `--no-fail-on-regression` work as documented.
- `evals/baseline.json` is committed, produced by a real provider run, and records model, prompt_version, dataset hash, n, and metrics.
- CI: eval job runs on PRs touching `app/prompts.py`, `app/services/classifier.py`, `app/services/llm_client.py`, or `evals/**`, and on manual dispatch; it uses the `LLM_API_KEY` secret and fails the check on exit 1/2. The regular test job still never needs secrets.
- `GET /stats` returns ticket counts by status and llm aggregates (calls, outcome split, tokens, cost, avg/p95 latency) matching direct DB aggregation in tests; `?since=` filters; bad `since` is `422`.
- Metric math and the regression gate are unit-tested with a deterministic mocked provider (no network in pytest). Lint/format/tests green.

### Manual test checklist
- [ ] Run the eval with a real key; read the report; metrics are plausible and every label appears in the per-label table.
- [ ] Run with `--limit 5`; confirm a fast smoke run and a clear note that the gate is skipped/partial.
- [ ] Deliberately degrade the prompt (scratch edit), rerun; confirm regression detection, exit 1, red verdict; revert.
- [ ] Edit one dataset row, rerun without updating baseline; confirm exit 2 hash-mismatch message; run `--update-baseline`; confirm new baseline written; revert both.
- [ ] Pipe the report to a file and run with `NO_COLOR=1`; confirm no ANSI codes and identical content.
- [ ] Run the eval with `LLM_API_KEY` unset; confirm `error:` on stderr and exit 2 (no traceback).
- [ ] `GET /stats` after the Phase 1 manual tickets; totals match expectations; `?since=<tomorrow>` returns zeros; `?since=garbage` returns `422`.
- [ ] Trigger the CI eval job by dispatch on a branch; confirm it runs, spends ~1 dataset of calls, and reports status on the PR.

---

## Phase 3 - Review workflow and rules management

**Goal**: humans close the loop. Pending review list, approve, correct (with re-routing), gold-label export, and routing-rule read/replace over the API.

### Expected commits
1. `feat(reviews): add pending review list endpoint`
2. `feat(reviews): add approve endpoint with state guard`
3. `feat(reviews): add correct endpoint storing corrections and re-routing`
4. `feat(reviews): add gold-label jsonl export`
5. `feat(rules): add list and replace endpoints`
6. `test(phase3): cover review flow, export, and rules endpoints`
7. `docs: update readme with review and rules examples`

### Definition of done
- `GET /reviews/pending` lists `triaged` + `needs_human` oldest-first with the documented envelope.
- `POST /tickets/{id}/approve`: `triaged -> approved`; any other status returns `409 invalid_state`; unknown id `404`.
- `POST /tickets/{id}/correct`: full label snapshot required; valid from `triaged` and `needs_human`; writes one corrections row, sets `corrected`, and re-resolves `queue` from the corrected labels via the rule engine; `409` from other statuses; second correct on the same ticket is `409`.
- `GET /corrections/export` streams JSONL matching the eval dataset field names (plus `source`), covering corrected and approved tickets; `?since=` filters; an exported file appended to `evals/dataset.jsonl` is accepted by the eval runner (ignoring `source`).
- `GET /rules` and `PUT /rules` behave per contract: transactional replacement, array order = position, validation of enums/slug/at-least-one-condition, empty set legal; a subsequent triage uses the new rules; existing tickets are not re-routed.
- Every documented error code for these routes is produced by at least one test. Lint/format/tests green.

### Manual test checklist
- [ ] Create and triage a few tickets plus one forced `needs_human`; `GET /reviews/pending` shows them oldest-first with `triage_error` visible on the fallback one.
- [ ] Approve a `triaged` ticket; confirm `approved` and that it leaves the pending list.
- [ ] Approve it again; confirm `409 invalid_state`. Approve the `needs_human` one; confirm `409`.
- [ ] Correct the `needs_human` ticket with labels that hit a different rule; confirm `corrected`, the corrections row (via detail response), and the new queue.
- [ ] Correct with a missing label field; confirm `422`.
- [ ] Export corrections; confirm one line per approved/corrected ticket, fields match the dataset shape; append a line to a scratch dataset copy and run the eval `--dataset` against it to prove round-trip.
- [ ] `PUT /rules` with a reordered set; confirm `GET /rules` reflects it and a fresh ticket routes by the new order; `PUT` with `queue: "Bad Queue!"` returns `422` and changes nothing.
- [ ] `PUT /rules` with `{"rules": []}`; confirm a fresh ticket lands in `DEFAULT_QUEUE`.

---

## Phase verification (run at the end of every phase)

- [ ] `uv run alembic upgrade head` on a fresh database, then `uv run uvicorn app.main:app` starts clean (no warnings/errors in the log).
- [ ] `uv run pytest` all green with the provider mocked; `uv run ruff check .` and `uv run black --check .` clean.
- [ ] Console/log check during a manual run: structured JSON only, request ids present, no ticket text, no secrets, no tracebacks in responses.
- [ ] Unhappy paths:
  - [ ] Malformed JSON body -> `422 validation_error` in the standard shape.
  - [ ] Duplicate submission (same `external_id` twice, including concurrent-ish rapid fire) -> one row, `200` replays.
  - [ ] Provider unreachable -> ingestion unaffected; tickets degrade to `needs_human`; breaker opens and closes as configured.
  - [ ] Restart mid-work: kill the process with tickets in `received`; on restart they get processed; no duplicates, no lost tickets.
  - [ ] Unknown ticket id -> `404`; wrong-state actions -> `409`; wrong/missing API key (when set) -> `401`.
  - [ ] Long inputs: 50,001-char body rejected; 40,000-char body triaged; 500-char subject accepted, 501 rejected.
- [ ] Empty states: `GET /tickets`, `GET /reviews/pending`, `GET /corrections/export`, `GET /stats` on a fresh database return well-formed empty/zero responses, not errors.
- [ ] Persistence: restart and confirm tickets, triages, corrections, and rules survive.

## Backlog

(Empty. Add deferred or out-of-scope items here with a one-line rationale as they are discovered. Do not implement backlog items without moving them into a phase first.)
