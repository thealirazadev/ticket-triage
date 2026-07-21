"""Measure pipeline overhead excluding provider time.

The provider is replaced with an instant mocked transport, so the numbers
reflect only the work this service does per ticket: request validation and the
idempotent insert on ingest, then prompt assembly, JSON parse, schema
validation, routing-rule evaluation, and the database writes on triage. Provider
round-trip time is deliberately excluded (the mock returns immediately), so this
is a floor on per-ticket cost, not an end-to-end latency figure.

Run:  uv run python benchmarks/pipeline.py [N]

Prints hardware/conditions and a tickets/sec table for ingest, triage, and the
combined path. No network is used and no real provider is called.
"""

import os
import platform
import sys
import tempfile
import time
from statistics import median

import httpx

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _instant_transport() -> httpx.MockTransport:
    body = {
        "choices": [
            {
                "message": {
                    "content": (
                        '{"intent":"account_access","priority":"P2",'
                        '"sentiment":"negative","summary":"Locked out after a reset."}'
                    )
                }
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
    }
    return httpx.MockTransport(lambda _r: httpx.Response(200, json=body))


def _prepare(dirpath: str):
    from alembic import command
    from alembic.config import Config

    from app.config import get_settings
    from app.db import make_engine, make_session_factory

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False, dir=dirpath)
    tmp.close()
    os.environ.update(
        {
            "DATABASE_URL": f"sqlite:///{tmp.name}",
            "WORKER_ENABLED": "false",
            "LLM_API_KEY": "bench",
            "LLM_BASE_URL": "http://provider.invalid",
            "LLM_MODEL": "bench-model",
            "LLM_MAX_RETRIES": "2",
            "DEFAULT_QUEUE": "general",
            "LOG_LEVEL": "CRITICAL",
        }
    )
    get_settings.cache_clear()

    cfg = Config(os.path.join(_ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(_ROOT, "migrations"))
    command.upgrade(cfg, "head")

    settings = get_settings()
    factory = make_session_factory(make_engine(settings.database_url))
    return settings, factory, tmp.name


def _run_once(n: int, dirpath: str) -> tuple[float, float]:
    """Ingest n tickets, then triage all n. Returns (ingest_s, triage_s)."""
    from app.routers.tickets import ingest_ticket
    from app.services.llm_client import LlmClient
    from app.services.worker import process_next_ticket

    settings, factory, db_path = _prepare(dirpath)
    prefix = f"bench-{time.time_ns()}"
    client = LlmClient(settings, transport=_instant_transport())
    try:
        ingest_start = time.perf_counter()
        with factory() as db:
            for i in range(n):
                ingest_ticket(
                    db,
                    external_id=f"{prefix}-{i}",
                    channel="web",
                    sender="user@example.com",
                    subject="Cannot log in",
                    body="I reset my password and now I am locked out of my account.",
                )
        ingest_s = time.perf_counter() - ingest_start

        triage_start = time.perf_counter()
        with factory() as db:
            processed = 0
            while process_next_ticket(db, client, settings):
                processed += 1
        triage_s = time.perf_counter() - triage_start
        assert processed == n, f"expected {n} processed, got {processed}"
    finally:
        client.close()
        os.unlink(db_path)
    return ingest_s, triage_s


def _measure(n: int, repeats: int, dirpath: str) -> tuple[float, float, float]:
    ingest_rates: list[float] = []
    triage_rates: list[float] = []
    combined_rates: list[float] = []
    for _ in range(repeats):
        ingest_s, triage_s = _run_once(n, dirpath)
        ingest_rates.append(n / ingest_s)
        triage_rates.append(n / triage_s)
        combined_rates.append(n / (ingest_s + triage_s))
    return median(ingest_rates), median(triage_rates), median(combined_rates)


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 2000
    repeats = 5

    # Two storage backends. "disk" is the real durable path (one commit fsync per
    # ticket); "tmpfs" runs the identical code and migrations against a RAM-backed
    # filesystem, isolating the pipeline's CPU/ORM cost from disk-sync latency.
    backends = [("disk", tempfile.gettempdir())]
    if os.path.isdir("/dev/shm") and os.access("/dev/shm", os.W_OK):
        backends.append(("tmpfs (RAM)", "/dev/shm"))

    _run_once(50, tempfile.gettempdir())  # warm up imports and first-call costs

    results = [(label, *_measure(n, repeats, path)) for label, path in backends]

    print("ticket-triage pipeline benchmark (provider time excluded)")
    print(f"python     {platform.python_version()}")
    print(f"platform   {platform.platform()}")
    print(f"processor  {platform.processor() or 'unknown'}")
    print(f"cpu_count  {os.cpu_count()}")
    print("mode       single thread, mocked instant transport (no network)")
    print(f"n          {n} tickets per run, {repeats} runs, median reported")
    print()
    header = f"{'storage':<14}{'ingest/sec':>14}{'triage/sec':>14}{'combined/sec':>14}"
    print(header)
    print("-" * len(header))
    for label, ingest, triage, combined in results:
        print(f"{label:<14}{ingest:>14,.0f}{triage:>14,.0f}{combined:>14,.0f}")


if __name__ == "__main__":
    main()
