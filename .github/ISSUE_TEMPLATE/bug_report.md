---
name: Bug report
about: Report a defect in ticket-triage
title: "bug: "
labels: bug
assignees: ""
---

## Summary

A clear, concise description of what the bug is.

## To reproduce

Steps to reproduce the behavior, ideally as `curl` calls or a failing test:

1. Start the service (`uv run uvicorn app.main:app`) or run the pipeline directly.
2. Send '...'
3. Observe '...'

**Request payload** (redact real ticket content and any secrets):

```json
{ "external_id": "...", "subject": "...", "body": "...", "sender": "...", "channel": "web" }
```

## Expected behavior

What you expected to happen, including the expected HTTP status and error code
if a request failed. Errors should follow the `{"error": {"code", "message"}}`
shape.

## Actual behavior

What actually happened. Paste the response body and any relevant **structured
log lines** here. Do not paste API keys or real ticket content (see
[SECURITY.md](../../SECURITY.md)).

## Environment

- ticket-triage version or commit:
- Python version (`python --version`):
- `uv` version (`uv --version`):
- Database (SQLite dev / Postgres):
- OS:

## Additional context

Anything else that helps, e.g. whether the worker was enabled, the circuit
breaker state from `GET /ready`, or a minimal reproduction.
