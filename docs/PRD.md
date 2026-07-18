# ticket-triage - Product Requirements

## What we are building

ticket-triage is an HTTP service that triages inbound support tickets. A ticket (subject, body, sender, channel) arrives via a REST endpoint or a generic email-webhook JSON shape, is validated and stored idempotently by its external id, and is then classified by a background worker: an LLM provider API assigns an intent category, a priority (P1-P4), and a sentiment from fixed label sets, and writes a 2-3 sentence summary. The provider's response is parsed against a strict schema; output that fails validation is retried once with a repair prompt and otherwise falls back to a `needs_human` state in a default queue, so unvalidated model output never enters the database. Routing rules (label conditions -> queue) assign each triaged ticket to a queue. Reviewers list pending tickets, approve or correct the triage over the API, and corrections are exportable as labeled data. Every provider call records tokens, cost, and latency, exposed through a stats endpoint. A committed, labeled eval dataset and an eval runner measure classification accuracy per label, compare against a stored baseline, and fail CI on regression.

## Target user

A small support team (or the developer operating on its behalf) that receives tickets from an email provider webhook or an internal form and wants consistent first-pass triage without paying a human to read every ticket twice. They run one instance, bring their own LLM provider API credentials, and keep control: every machine decision is reviewable, correctable, and measured. Secondary audience: a reviewer of this repository evaluating how LLM features should be engineered - the eval harness, structured-output discipline, and cost/latency accounting are the point, not decoration.

This is not a helpdesk. It sits in front of one: it classifies, summarizes, and routes; humans work the queues in whatever tool they already use.

## Core features (prioritized)

1. **Ticket ingestion** (highest priority). `POST /tickets` accepts subject, body, sender, channel, and an external id; a `POST /webhooks/email` route accepts a generic email-webhook JSON shape and maps it to the same ticket. Server-side validation, idempotency by external id (replays return the existing ticket, never a duplicate).
2. **LLM classification with structured-output discipline** (highest priority). Fixed label sets - intent (`billing`, `bug`, `how_to`, `feature_request`, `account_access`, `refund`, `other`), priority (`P1`-`P4`), sentiment (`negative`, `neutral`, `positive`) - plus a 2-3 sentence summary. The provider must return JSON matching a strict schema; on parse or validation failure the service retries once with a repair prompt, then falls back to `needs_human` in the default queue. Label enums are enforced in the schema layer and by database constraints.
3. **Eval harness** (highest priority; present by Phase 2). A labeled JSONL dataset committed in the repo, an eval runner that executes the real classification pipeline and reports accuracy plus per-label precision/recall/F1 and a confusion summary per field, comparison against a committed baseline, and a CI job that fails on regression beyond a configured threshold.
4. **Routing rules.** Ordered first-match rules (`intent`/`priority`/`sentiment` conditions -> queue) stored in the database, seeded by migration, readable and replaceable over the API. No match -> the default queue.
5. **Cost and latency tracking.** Every provider call records input/output tokens, computed cost, latency, and outcome. `GET /stats` aggregates ticket counts by status and provider spend/latency (including p95).
6. **Review workflow.** List pending tickets, approve a triage, or correct the labels. Corrections store a full gold-label snapshot, re-run routing with the corrected labels, and are exportable (together with approvals) as JSONL in the same shape as the eval dataset.
7. **Provider outage path.** Timeouts on every provider call, bounded retries with exponential backoff, and a circuit breaker: after N consecutive failures the service stops calling the provider for a cooldown window and routes tickets straight to `needs_human`. An outage degrades triage; it never blocks ingestion or corrupts data.
8. **Optional API-key authentication.** When `API_KEY` is set, all routes except `GET /health` require it.

## Non-goals

- A helpdesk UI, agent inbox, or any web frontend. This is an API-only service; queues are consumed by other tools.
- Auto-responses or any outbound communication to customers.
- Live chat, phone, social, or any ingestion channel beyond the REST endpoint and the email-webhook JSON shape. Attachments are ignored.
- Multi-tenant accounts, user roles, or per-reviewer identity. One instance, one shared API key.
- Fine-tuning, embeddings, retrieval, or any model training. Inference calls to the LLM provider API only.
- A message broker or distributed queue (no Celery/Redis/RabbitMQ). Background work is a single in-process worker; the database is the queue. Throughput limits are documented in `docs/architecture.md`.
- Automatic re-triage of `needs_human` tickets after a provider outage ends; a reviewer corrects them. (A manual re-triage command is a plausible later phase, not this build.)
- SLA timers, escalation policies, or notification delivery.

## Success criteria per core feature

**1. Ticket ingestion**
- A valid `POST /tickets` returns `201` with a ticket id and `status: "received"`; the row exists with all submitted fields.
- Re-posting the same `external_id` returns `200` with the original ticket unchanged - no duplicate row, no re-triage, regardless of how many times the webhook replays.
- `POST /webhooks/email` maps `message_id`/`from`/`subject`/`text` to a ticket with `channel: "email"`; an empty webhook subject is stored as `(no subject)`.
- Missing/oversized/invalid fields (bad channel, body over 50,000 chars, blank subject) return `422` in the single error format; nothing is stored.

**2. LLM classification with structured-output discipline**
- Within one worker poll interval plus provider latency, a received ticket becomes `triaged` with intent, priority, sentiment, and summary all present and all drawn from the fixed label sets.
- A provider response that is not valid JSON or fails schema validation triggers exactly one repair call; if that also fails, the ticket becomes `needs_human` in `DEFAULT_QUEUE` with a recorded `triage_error`, and no triage row is written. Verified by test with a mocked provider returning garbage twice.
- No value outside the label enums can be stored: schema validation rejects it and database CHECK constraints refuse it as a second line of defense.

**3. Eval harness**
- `uv run python -m evals.run` executes the pipeline over `evals/dataset.jsonl` (60+ labeled tickets covering every label) and prints accuracy per field, precision/recall/F1 per label, a confusion summary, and the parse-failure count.
- The run compares against `evals/baseline.json` and exits non-zero when any field's accuracy or macro-F1 drops more than `EVAL_REGRESSION_THRESHOLD` below baseline; the CI eval job goes red on that exit code. Verified by test with a deterministic mocked provider.
- `--update-baseline` writes a new baseline including the dataset hash, model id, and prompt version; a dataset/baseline hash mismatch is reported as an error rather than a silent bad comparison.

**4. Routing rules**
- A triaged ticket's queue equals the first matching rule's queue (rules ordered, conditions AND-ed, null condition is a wildcard); with no match it equals `DEFAULT_QUEUE`. Verified by unit tests over the rule engine.
- `GET /rules` returns the ordered rule set; `PUT /rules` atomically replaces it, rejecting unknown labels or empty conditions with `422`.

**5. Cost and latency tracking**
- Every provider call (including failed and repair calls) produces one `llm_calls` row with tokens, cost, latency in ms, and outcome.
- `GET /stats` totals (calls, tokens, cost, average and p95 latency, failure rate, tickets by status) match direct database aggregation in tests; `?since=` filters correctly.

**6. Review workflow**
- `GET /reviews/pending` lists `triaged` and `needs_human` tickets oldest-first.
- `POST /tickets/{id}/approve` moves `triaged` -> `approved`; approving anything else returns `409 invalid_state`.
- `POST /tickets/{id}/correct` (full label snapshot) moves `triaged` or `needs_human` -> `corrected`, stores a corrections row, and re-routes the ticket by the corrected labels.
- `GET /corrections/export` streams JSONL where each line carries the ticket fields plus gold labels (from the correction, or from the approved triage) - loadable as additional eval data without transformation.

**7. Provider outage path**
- With the provider unreachable, ingestion still returns `201`; the affected ticket ends `needs_human` after the configured retries, with backoff between attempts (asserted via a mocked transport).
- After `CIRCUIT_FAILURE_THRESHOLD` consecutive failures the breaker opens: tickets go to `needs_human` without any provider call until the cooldown elapses, then one trial call closes or reopens it. Verified by unit tests on the client.

**8. Optional API-key authentication**
- With `API_KEY` unset, all routes work without a key. With it set, every route except `GET /health` returns `401` on a missing or wrong `X-API-Key`; comparison is constant-time; the key never appears in logs.
