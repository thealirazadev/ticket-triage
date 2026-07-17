# ticket-triage - Launch Checklist

Unchecked skeleton to complete before treating an instance as production-ready. Check items only when verified.

## Configuration and secrets
- [ ] Production env vars set (not `.env.example` dummies): `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL`, `DATABASE_URL` (Postgres), `API_KEY`.
- [ ] `.env` git-ignored and never committed; no secret anywhere in repo history or CI logs.
- [ ] `LLM_PRICE_INPUT_PER_MTOK` / `LLM_PRICE_OUTPUT_PER_MTOK` set to the real provider prices so cost tracking is truthful.
- [ ] `DEFAULT_QUEUE`, worker, circuit-breaker, and timeout settings reviewed for production volume.
- [ ] Provider account has a spend limit or alert configured independently of this service.

## Database
- [ ] `alembic upgrade head` applied cleanly to the production Postgres; `alembic_version` matches the repo head.
- [ ] Full pytest suite has been run at least once against Postgres (not only SQLite) to catch dialect drift.
- [ ] Backup/restore procedure for the database documented and tested.

## Application behavior
- [ ] Uvicorn runs without `--reload`; no debug tracebacks ever reach clients.
- [ ] Every error path returns the single JSON error format; unknown routes return `404` in that shape; unhandled exceptions return `internal_error` and log the trace.
- [ ] Idempotency verified against the real webhook source: replayed deliveries create no duplicates.
- [ ] Provider outage drill performed: breaker opens, tickets degrade to `needs_human`, ingestion stays available, breaker recovers after cooldown.
- [ ] Empty states verified: fresh-database responses for tickets, pending, export, and stats are well-formed.

## Security
- [ ] `API_KEY` set to a strong value; all data routes confirmed `401` without it; `GET /health` open; comparison constant-time.
- [ ] Server bound to the intended interface; TLS via a fronting reverse proxy if reachable beyond localhost; basic rate limiting at the proxy considered for the webhook route.
- [ ] Logs reviewed in production mode: no ticket subject/body/sender content, no `LLM_API_KEY` or `API_KEY`, request ids present.
- [ ] Understood and documented for operators: ticket text is sent to the configured LLM provider; provider data-retention terms reviewed.

## Quality and evals
- [ ] CI green on the release commit: ruff, black, pytest.
- [ ] Eval run on the release commit passes the baseline gate; `evals/baseline.json` matches the shipped model id and prompt version.
- [ ] `GET /stats` reviewed after a soak period: needs_human rate, failure rate, p95 latency, and cost per ticket are within expectations.
- [ ] Error tracking / log aggregation connected; alerting on 5xx rate, worker crash logs, and circuit-open events.

## Operations
- [ ] Process supervision in place (systemd/container restart policy); worker confirmed to resume `received` tickets after a restart.
- [ ] Runbook notes written: how to correct a `needs_human` backlog, how to change routing rules safely, how to rotate `LLM_API_KEY` and `API_KEY`.
- [ ] README install/run/test instructions followed end-to-end on a clean machine and confirmed accurate.
