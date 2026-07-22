#!/usr/bin/env bash
# Reproduce the README "Example session".
#
# Runs the REAL service (real routers, worker, SQLite, migrations) against a mocked
# provider transport via scripts/demo_server.py -- no network and no API key -- and
# captures genuine curl request/response pairs for the core flow:
#   ingest -> triage -> list pending -> correct -> stats
#
# Requires: uv, curl, jq. Output is copy-paste-ready; ids and timestamps vary per run.
set -euo pipefail

cd "$(dirname "$0")/.."

export DATABASE_URL="sqlite:///./data/demo.db"
export WORKER_ENABLED=true
export WORKER_POLL_SECONDS=0.2
export LLM_API_KEY=demo-key-not-a-real-secret
export LLM_BASE_URL=http://provider.invalid/v1
export LLM_MODEL=triage-demo-model
export LLM_PRICE_INPUT_PER_MTOK=1.0
export LLM_PRICE_OUTPUT_PER_MTOK=2.0
export API_KEY=""
export LOG_LEVEL=WARNING

BASE=http://127.0.0.1:8000

# Fresh database: migrations create the schema and seed the default routing rules.
rm -f ./data/demo.db
uv run alembic upgrade head >/dev/null 2>&1

# Start the mock-backed service in the background.
uv run uvicorn --app-dir scripts demo_server:app --port 8000 --log-level warning \
  >/tmp/demo_uvicorn.log 2>&1 &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null || true' EXIT

# Wait for readiness.
for _ in $(seq 1 50); do
  curl -sf "$BASE/health" >/dev/null 2>&1 && break
  sleep 0.2
done

show() { printf '$ %s\n' "$1"; eval "$1"; echo; }

echo "# 1. Ingest a ticket. It returns immediately with status \"received\"."
BODY='{"external_id":"demo-1","subject":"Charged twice for my subscription","body":"I was billed twice this month for order 5567 and need one of the charges reversed.","sender":"dana@example.com","channel":"web"}'
INGEST=$(curl -sS -X POST "$BASE/tickets" -H 'Content-Type: application/json' -d "$BODY")
printf "$ curl -sS -X POST %s/tickets -H 'Content-Type: application/json' \\\\\n" "$BASE"
printf "    -d '%s' | jq\n" "$BODY"
echo "$INGEST" | jq
TID=$(echo "$INGEST" | jq -r .id)
echo

echo "# 2. The background worker triages within one poll interval. Read the ticket back:"
sleep 1
show "curl -sS $BASE/tickets/$TID | jq"

echo "# 3. List what is awaiting review (triaged or needs_human), oldest first."
show "curl -sS $BASE/reviews/pending | jq"

echo "# 4. A reviewer corrects the labels; the ticket is re-routed by the new labels."
CORR='{"intent":"billing","priority":"P1","sentiment":"negative","note":"Duplicate charge, escalating to urgent."}'
printf "$ curl -sS -X POST %s/tickets/\$TID/correct -H 'Content-Type: application/json' \\\\\n" "$BASE"
printf "    -d '%s' | jq\n" "$CORR"
curl -sS -X POST "$BASE/tickets/$TID/correct" -H 'Content-Type: application/json' -d "$CORR" | jq
echo

echo "# 5. Operational totals: ticket flow and provider spend."
show "curl -sS $BASE/stats | jq"
