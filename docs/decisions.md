# Design decisions

## 1. A triage layer in front of a helpdesk, not a helpdesk

The service classifies, summarizes, and routes tickets; people work the queues in whatever tool
they already use. It is API only, with no web frontend or agent inbox. It sends nothing to
customers. It accepts two ingestion shapes (the REST endpoint and a generic email-webhook JSON
body) and ignores attachments. One instance serves one team behind one optional shared API key,
with no tenants, roles, or per-reviewer identity. The model is used for inference only: no
fine-tuning, embeddings, or retrieval. Keeping the surface this small is what makes every machine
decision reviewable and measurable.

## 2. Label sets are a contract

Intent (`billing`, `bug`, `how_to`, `feature_request`, `account_access`, `refund`, `other`),
priority (`P1` to `P4`), and sentiment (`negative`, `neutral`, `positive`) are fixed sets defined
once in `app/schemas/triage.py`. They are enforced twice: by the schema that parses model output
and by CHECK constraints in the database, so an out-of-set value cannot be stored even if a code
path forgets to validate. Routing rules, the eval dataset, corrections, and `/stats` breakdowns
all key on these exact strings, so a change to a label set, to the meaning of `PROMPT_VERSION`,
or to the eval regression gate ships with a baseline update in the same pull request. Synonyms or
ad hoc labels are never introduced.

## 3. Model output enters only through a strict schema

Provider text becomes data in exactly one place, the `TriageResult` model. Labels are lowercased
or uppercased and trimmed before the enum check, a cheap normalization that avoids pointless
repair calls, and code fences around the JSON are tolerated. On a parse or validation failure the
classifier makes one repair call carrying the invalid output and the errors. A second failure
writes no triage row and moves the ticket to `needs_human` in the default queue with
`triage_error = "parse_failed"`. Invalid output is only ever logged as a truncated snippet. This
rejects both trusting the model to return valid JSON and unbounded repair loops that spend budget
on a model that cannot recover.

## 4. Transport failures and output failures are different failures

A provider 200 whose body is not decodable JSON (a proxy error page, a truncated response) is
treated as a retryable `api_error`, not as a parse failure. The envelope is a transport concern;
a valid envelope carrying an invalid triage is an output concern and stays on the repair path.
Keeping them apart means a provider that returns garbage 200s trips the circuit breaker instead of
looping through repairs. For the same reason, a non-string `message.content` (null, or a list of
content parts) is coerced to text inside the client, so the schema in decision 3 remains the only
code that interprets provider text.

## 5. The database is the queue

Background work is a single polling worker thread; `status = "received"` is the pending entry.
A restart resumes exactly where it stopped, and because ingestion is idempotent by external id, a
webhook replay cannot enqueue the same ticket twice. FastAPI `BackgroundTasks` would tie work to
the request lifecycle and lose it on a crash, and a broker would add parallelism and delivery
guarantees this service does not need at the cost of more infrastructure. The accepted cost is
serial triage in one process, throughput bounded by the provider round trip, and at-least-once
processing where a crash mid-classification re-runs one ticket.

## 6. An outage degrades triage, never ingestion

Every provider call has explicit timeouts. Timeouts, connection errors, 429, and 5xx are retried
a bounded number of times with exponential backoff and jitter; other 4xx responses fail at once,
since retrying a bad request only costs money. After a configured number of consecutive failures
an in-memory circuit breaker opens, the worker skips the provider for a cooldown window, and
tickets go straight to `needs_human` with `triage_error = "circuit_open"`; one trial call then
closes or reopens it. Ingestion never touches the provider, so `POST /tickets` stays available.
Tickets that fell back during an outage are not re-triaged automatically; a reviewer corrects
them. The readiness probe fails only when the database is unreachable and reports the breaker
state for observability, because an open breaker still leaves the API able to accept and hold
tickets. Retries and the breaker are a short hand-written implementation rather than a library,
so the state machine stays inspectable.

## 7. The model labels, rules route

The model never chooses a queue. Routing is an ordered, first-match list of rules over intent,
priority, and sentiment, with conditions combined by AND and a null condition acting as a
wildcard; no match means `DEFAULT_QUEUE`. Rules live in the database, are seeded by a migration
so every environment converges from `alembic upgrade head`, and are replaced as a whole ordered
set through `PUT /rules`, which keeps evaluation order explicit and avoids partially applied
edits. An empty set is legal. New rules apply to future triage; existing tickets are not
re-routed, except that a correction re-routes its ticket by the corrected labels.

## 8. Ticket text is untrusted input

Subject, body, and sender are embedded in the prompt as clearly delimited data, with an
instruction that they are content to classify rather than instructions to follow. The blast
radius of a hostile ticket is capped by decision 3: the model can only produce a `TriageResult`,
so the worst case is a wrong label or a slanted summary, which is reviewable, correctable, and
measured by the eval set. Summaries are model text derived from untrusted input, and the API
never returns HTML, so clients render them as plain text. Ticket content is never logged; logs
carry ids, lengths, and label values. The provider necessarily receives ticket text, and that is
stated rather than hidden.

## 9. Every machine decision is reviewable, and corrections become eval data

Reviewers list `triaged` and `needs_human` tickets oldest first, approve a triage, or correct it.
A correction stores a full gold-label snapshot rather than a diff, so it stands on its own as a
labeled example. Approvals and corrections export as JSONL in the same shape as
`evals/dataset.jsonl`, so reviewed traffic can be appended to the eval set without
transformation. Ticket state transitions are guarded, and an action from the wrong state returns
`409 invalid_state` instead of silently succeeding.

## 10. Every provider call is accounted for

Each provider HTTP call, including failed and repair calls, writes one `llm_calls` row with
tokens, cost, latency, and outcome. When a 200 response fails schema validation, the existing row
is updated to `parse_error` rather than adding a second one, so the count stays one row per call.
Token counts come from the provider's `usage` object; when it is missing they are stored as null
and cost as zero rather than estimated. Prices come from configuration so reported cost matches
the real provider. `GET /stats` aggregates these rows (calls, outcomes, tokens, cost, average and
p95 latency) and breaks labels down from stored triages, so it reflects what the model actually
decided.

## 11. Classification changes are gated by a committed eval

The labeled dataset and the baseline are committed files. The runner executes the real
classification pipeline per row and reports accuracy, per-label precision, recall, and F1,
macro-F1, a confusion summary, and parse failures, using a few plain functions rather than an ML
library. The baseline records the dataset hash, model id, and prompt version, so a metric shift
is attributable. A drop beyond `EVAL_REGRESSION_THRESHOLD` exits 1, and a dataset that no longer
matches the baseline hash exits 2 rather than producing a silently meaningless comparison. Any
prompt wording change bumps `PROMPT_VERSION` and needs an eval run before merge.

## 12. Offline replay by default, real-provider evals on opt-in

The committed fixtures and baseline were recorded from one real-provider run, so the test suite
replays real model behavior through the gate with no network and no secrets. The real-provider
eval job in CI spends money, so it runs only on manual dispatch, or on a pull request that touches
the prompt, classifier, client, or eval files when a funded key has been configured. When the
first real run disagreed with the dataset on priority, the fix was to relabel the dataset to the
documented priority definitions rather than regenerate the baseline from the disagreeing run,
which would have hidden the problem.

## 13. Reply drafts are best-effort and never sent

After a ticket is triaged and committed, the worker generates a suggested reply through the same
discipline as classification: a strict `ReplyDraftResult` schema, one repair call, and on failure
no draft at all rather than a bad one. A draft failure never changes the ticket's triaged state.
Drafts are only fetched, edited, approved, or discarded through the API, so the service still
sends nothing to customers. Draft calls use their own `llm_calls` purposes so their cost is
separable from triage, and reply prompts carry a separate `REPLY_PROMPT_VERSION` so editing them
does not falsely signal a classification change to the eval gate.

## 14. Synchronous, few dependencies, portable schema

The service is synchronous end to end: sync endpoints, a sync database layer, and sync httpx.
Dependencies are limited to the stack listed in `docs/architecture.md`, pinned exactly with a
committed lockfile, and upgraded to the lowest patched version when a security fix requires it,
so any regression is easy to attribute. Queries go through the SQLAlchemy ORM only, never
interpolated SQL. Migrations are written and reviewed by hand, never edited once applied, and kept
portable so the same schema runs on SQLite in development and Postgres in production.
