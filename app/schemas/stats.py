"""GET /stats response schema."""

from pydantic import BaseModel


class TicketCounts(BaseModel):
    received: int
    triaged: int
    needs_human: int
    approved: int
    corrected: int
    total: int


class LlmStats(BaseModel):
    calls: int
    ok: int
    failures: int
    failure_rate: float
    input_tokens: int
    output_tokens: int
    cost_usd: float
    avg_latency_ms: int
    p95_latency_ms: int


class LabelBreakdown(BaseModel):
    """Distribution of stored triage labels. Only observed values appear."""

    intent: dict[str, int]
    priority: dict[str, int]
    sentiment: dict[str, int]


class StatsOut(BaseModel):
    tickets: TicketCounts
    queues: dict[str, int]
    labels: LabelBreakdown
    llm: LlmStats
    since: str | None
