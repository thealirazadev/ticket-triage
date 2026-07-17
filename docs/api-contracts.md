# ticket-triage - API Contracts

Base URL: `http://127.0.0.1:8000`. All bodies are JSON (`application/json`) except the corrections export, which streams `application/x-ndjson`. Timestamps are ISO-8601 UTC. Every response carries an `X-Request-ID` header for log correlation. This contract is agreed before any code is written; changes go through the owner.

## Authentication

- Controlled by the `API_KEY` env var. Unset: all routes open (local convenience). Set: every route below except `GET /health` requires `X-API-Key: <key>`; missing or wrong returns `401 unauthorized`. Comparison is constant-time.

## Error format

Every non-2xx response uses exactly:

```json
{
  "error": {
    "code": "string_snake_case",
    "message": "Human-readable, safe to display."
  }
}
```

| code               | status | meaning                                                    |
|--------------------|--------|------------------------------------------------------------|
| `validation_error` | 422    | Malformed body/query or failed field validation.           |
| `unauthorized`     | 401    | Missing or invalid `X-API-Key` (when auth is enabled).     |
| `not_found`        | 404    | Ticket id (or route) does not exist.                       |
| `invalid_state`    | 409    | Action not allowed from the ticket's current status.       |
| `internal_error`   | 500    | Unhandled server error; detail is in the logs only.        |

Provider failures never surface as HTTP errors on these routes: classification happens in the background worker, and its failures materialize as ticket state (`needs_human` + `triage_error`), not as 5xx responses.

## Shared shapes

Label enums (exact strings):
- `intent`: `billing` | `bug` | `how_to` | `feature_request` | `account_access` | `refund` | `other`
- `priority`: `P1` | `P2` | `P3` | `P4`
- `sentiment`: `negative` | `neutral` | `positive`
- `channel`: `email` | `web` | `api`
- `status`: `received` | `triaged` | `needs_human` | `approved` | `corrected`

**Ticket** (detail form; the list form omits `body`, `triage.summary` is included in both):

```json
{
  "id": "9a1b2c3d4e5f46708192a3b4c5d6e7f8",
  "external_id": "msg-20260718-0042",
  "channel": "email",
  "sender": "jane@example.com",
  "subject": "Cannot log in after password reset",
  "body": "Hi, I reset my password this morning and now...",
  "status": "triaged",
  "queue": "security",
  "triage_error": null,
  "triage": {
    "intent": "account_access",
    "priority": "P2",
    "sentiment": "negative",
    "summary": "Customer reset their password and can no longer log in. They are locked out of a paid account and need access restored.",
    "model": "example-model-id",
    "prompt_version": "1",
    "attempts": 1,
    "created_at": "2026-07-18T09:15:07Z"
  },
  "correction": null,
  "metrics": {
    "llm_calls": 1,
    "input_tokens": 512,
    "output_tokens": 78,
    "cost_usd": 0.000912,
    "latency_ms": 1840
  },
  "created_at": "2026-07-18T09:14:58Z",
  "updated_at": "2026-07-18T09:15:07Z"
}
```

- `triage` is `null` while `status` is `received` and for `needs_human` tickets. `metrics` sums all `llm_calls` rows for the ticket and is `null` when there are none; token fields are `null` when the provider did not report usage.
- `correction` (detail only) is `null` unless status is `corrected`, then: `{"intent", "priority", "sentiment", "note", "created_at"}`.

---

## GET /health

Liveness. Always open.

- Response `200`: `{ "status": "ok" }`

---

## POST /tickets

Ingest a ticket. Idempotent by `external_id`.

- Request:

```json
{
  "external_id": "msg-20260718-0042",
  "subject": "Cannot log in after password reset",
  "body": "Hi, I reset my password this morning and now...",
  "sender": "jane@example.com",
  "channel": "web"
}
```

  - `external_id`: required, 1-128 chars after trim. The caller's stable id (message id, form submission id).
  - `subject`: required, 1-500 chars after trim.
  - `body`: required, 1-50,000 chars.
  - `sender`: required, 1-320 chars (opaque identifier; not strictly validated as an email).
  - `channel`: required, one of the channel enum.

- Success `201`: the Ticket shape with `status: "received"`, `queue: null`, `triage: null`.
- Idempotent replay `200`: the same `external_id` returns the **existing** ticket in whatever state it is now (possibly already `triaged`). Body fields of the replay are ignored; nothing is updated. This makes webhook retries safe.
- Errors: `422 validation_error`, `401 unauthorized`.

```bash
curl -X POST http://127.0.0.1:8000/tickets \
  -H "Content-Type: application/json" \
  -d '{"external_id":"t-1","subject":"Refund please","body":"I was charged twice.","sender":"a@b.com","channel":"web"}'
```

---

## POST /webhooks/email

Generic email-webhook JSON shape, mapped onto the same ticket. Unknown extra fields are ignored (webhook providers add their own).

- Request:

```json
{
  "message_id": "<CAF7x9K@mail.example.com>",
  "from": "jane@example.com",
  "subject": "Cannot log in",
  "text": "Hi, I reset my password this morning and..."
}
```

  - `message_id` -> `external_id` (required, 1-128), `from` -> `sender` (required, 1-320), `text` -> `body` (required, 1-50,000), `subject` -> `subject` (optional; empty or missing is stored as `(no subject)`), `channel` fixed to `email`.

- Responses and errors: identical to `POST /tickets` (`201` create, `200` idempotent replay, `422`, `401`).

---

## GET /tickets

List tickets, newest first.

- Query params (all optional): `status`, `queue`, `intent`, `priority` (enum-validated; `intent`/`priority` filter on the triage row), `limit` (1-200, default 50), `offset` (>= 0, default 0).
- Success `200`:

```json
{
  "tickets": [ { "...ticket list form (no body, no correction)..." : "..." } ],
  "total": 132,
  "limit": 50,
  "offset": 0
}
```

- Empty result is `200` with `"tickets": []`. Errors: `422` on bad filter values, `401`.

---

## GET /tickets/{id}

- Success `200`: the full Ticket detail shape (including `body`, `correction`, `metrics`).
- Errors: `404 not_found`, `401`.

---

## GET /reviews/pending

The review queue: tickets in `triaged` or `needs_human`, oldest first (work the backlog in arrival order).

- Query params: `limit` (1-200, default 50), `offset`.
- Success `200`: same envelope as `GET /tickets` (`tickets`, `total`, `limit`, `offset`); `needs_human` tickets show `triage: null` and their `triage_error`.
- Errors: `401`.

---

## POST /tickets/{id}/approve

Confirm the machine triage. Valid only from `triaged`.

- Request: empty body.
- Success `200`: the updated Ticket detail, `status: "approved"`. Queue and triage are unchanged; the triage labels become gold labels for export.
- Errors:
  - `409 invalid_state` - status is not `triaged` (including `needs_human`, which has no labels to approve - correct it instead).
  - `404 not_found`, `401`.

---

## POST /tickets/{id}/correct

Set the correct labels. Valid from `triaged` or `needs_human`. A full snapshot: all three labels are required even when only one differs.

- Request:

```json
{
  "intent": "billing",
  "priority": "P3",
  "sentiment": "negative",
  "note": "Duplicate charge, not a refund request."
}
```

  - `intent`/`priority`/`sentiment`: required, enum-validated. `note`: optional, max 1000 chars.

- Success `200`: the updated Ticket detail with `status: "corrected"`, a `correction` object, and `queue` re-resolved by running the routing rules against the corrected labels.
- Errors: `409 invalid_state` (status is `received`, `approved`, or already `corrected`), `422`, `404`, `401`.

---

## GET /corrections/export

Gold-labeled tickets as JSONL - the future-eval-data export. Includes `corrected` tickets (labels from the correction) and `approved` tickets (labels from the triage).

- Query params: `since` (optional ISO-8601; filters on the approval/correction time).
- Success `200`, `Content-Type: application/x-ndjson`, one object per line, same field names as `evals/dataset.jsonl`:

```json
{"external_id":"msg-20260718-0042","channel":"email","sender":"jane@example.com","subject":"Cannot log in after password reset","body":"Hi, I reset...","intent":"account_access","priority":"P2","sentiment":"negative","source":"corrected"}
```

  - `source`: `approved` | `corrected` (strip it before appending lines to the eval dataset, or keep it; the eval runner ignores unknown keys).
- Empty export is `200` with an empty body. Errors: `422` on bad `since`, `401`.

---

## GET /rules

Ordered routing rules.

- Success `200`:

```json
{
  "rules": [
    { "id": 1, "position": 1, "intent": null, "priority": "P1", "sentiment": null, "queue": "urgent" },
    { "id": 2, "position": 2, "intent": "billing", "priority": null, "sentiment": null, "queue": "billing" }
  ],
  "default_queue": "general"
}
```

- Errors: `401`.

---

## PUT /rules

Atomically replace the entire ordered rule set. Positions are assigned from array order.

- Request:

```json
{
  "rules": [
    { "priority": "P1", "queue": "urgent" },
    { "intent": "billing", "queue": "billing" }
  ]
}
```

  - 0-100 rules. Each rule: `queue` required (slug, 1-64 chars, `[a-z0-9_-]`); `intent`/`priority`/`sentiment` optional, enum-validated; at least one of the three must be present. An empty `rules` array is legal (everything falls to the default queue).

- Success `200`: same shape as `GET /rules`, reflecting the new set. Replacement is transactional; on `422` nothing changes. Already-routed tickets are not re-routed (rules apply at triage/correction time).
- Errors: `422 validation_error`, `401`.

---

## GET /stats

Operational totals: ticket flow and provider spend.

- Query params: `since` (optional ISO-8601; filters both ticket counts and llm aggregates by `created_at`).
- Success `200`:

```json
{
  "tickets": {
    "received": 3,
    "triaged": 41,
    "needs_human": 2,
    "approved": 25,
    "corrected": 6,
    "total": 77
  },
  "llm": {
    "calls": 84,
    "ok": 80,
    "failures": 4,
    "failure_rate": 0.0476,
    "input_tokens": 43210,
    "output_tokens": 6544,
    "cost_usd": 0.1287,
    "avg_latency_ms": 1620,
    "p95_latency_ms": 3480
  },
  "since": null
}
```

  - `failures` counts `timeout` + `api_error` + `parse_error` outcomes. Token sums skip null rows; `cost_usd` is 0 when prices are unconfigured. Latency percentiles are computed over recorded calls (documented limit: in-memory computation, fine at this scale).
- Errors: `422` on bad `since`, `401`.

---

## Status code summary

| Route                        | Success   | Common errors        |
|------------------------------|-----------|----------------------|
| `GET /health`                | 200       | -                    |
| `POST /tickets`              | 201 / 200 | 422, 401             |
| `POST /webhooks/email`       | 201 / 200 | 422, 401             |
| `GET /tickets`               | 200       | 422, 401             |
| `GET /tickets/{id}`          | 200       | 404, 401             |
| `GET /reviews/pending`       | 200       | 401                  |
| `POST /tickets/{id}/approve` | 200       | 409, 404, 401        |
| `POST /tickets/{id}/correct` | 200       | 409, 422, 404, 401   |
| `GET /corrections/export`    | 200       | 422, 401             |
| `GET /rules`                 | 200       | 401                  |
| `PUT /rules`                 | 200       | 422, 401             |
| `GET /stats`                 | 200       | 422, 401             |

All error bodies use the single error format. Unknown routes return `404 not_found` in the same shape.
