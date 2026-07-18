"""SQLAlchemy 2.0 models. Enum columns carry CHECK constraints so both SQLite
and Postgres refuse out-of-set values as a final backstop behind schema
validation."""

from datetime import UTC, datetime

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import DateTime

from app.schemas.triage import INTENTS, PRIORITIES, SENTIMENTS

# DB-level enums beyond the LLM label sets.
CHANNELS: tuple[str, ...] = ("email", "web", "api")
STATUSES: tuple[str, ...] = ("received", "triaged", "needs_human", "approved", "corrected")
TRIAGE_ERRORS: tuple[str, ...] = ("parse_failed", "provider_error", "circuit_open")
LLM_PURPOSES: tuple[str, ...] = ("classify", "repair")
LLM_OUTCOMES: tuple[str, ...] = ("ok", "parse_error", "timeout", "api_error")


def _in_check(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Ticket(Base):
    __tablename__ = "tickets"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    external_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    sender: Mapped[str] = mapped_column(String(320), nullable=False)
    subject: Mapped[str] = mapped_column(String(500), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="received")
    queue: Mapped[str | None] = mapped_column(String(64), nullable=True)
    triage_error: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    __table_args__ = (
        CheckConstraint(_in_check("channel", CHANNELS), name="ck_tickets_channel"),
        CheckConstraint(_in_check("status", STATUSES), name="ck_tickets_status"),
        CheckConstraint(
            f"triage_error IS NULL OR {_in_check('triage_error', TRIAGE_ERRORS)}",
            name="ck_tickets_triage_error",
        ),
    )


class Triage(Base):
    __tablename__ = "triages"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    ticket_id: Mapped[str] = mapped_column(ForeignKey("tickets.id"), unique=True, nullable=False)
    intent: Mapped[str] = mapped_column(String(32), nullable=False)
    priority: Mapped[str] = mapped_column(String(8), nullable=False)
    sentiment: Mapped[str] = mapped_column(String(16), nullable=False)
    summary: Mapped[str] = mapped_column(String(600), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(16), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        CheckConstraint(_in_check("intent", INTENTS), name="ck_triages_intent"),
        CheckConstraint(_in_check("priority", PRIORITIES), name="ck_triages_priority"),
        CheckConstraint(_in_check("sentiment", SENTIMENTS), name="ck_triages_sentiment"),
    )


class LlmCall(Base):
    __tablename__ = "llm_calls"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    ticket_id: Mapped[str | None] = mapped_column(ForeignKey("tickets.id"), nullable=True)
    purpose: Mapped[str] = mapped_column(String(16), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[float] = mapped_column(Numeric(10, 6), nullable=False, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        CheckConstraint(_in_check("purpose", LLM_PURPOSES), name="ck_llm_calls_purpose"),
        CheckConstraint(_in_check("outcome", LLM_OUTCOMES), name="ck_llm_calls_outcome"),
    )


class Correction(Base):
    __tablename__ = "corrections"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    ticket_id: Mapped[str] = mapped_column(ForeignKey("tickets.id"), unique=True, nullable=False)
    intent: Mapped[str] = mapped_column(String(32), nullable=False)
    priority: Mapped[str] = mapped_column(String(8), nullable=False)
    sentiment: Mapped[str] = mapped_column(String(16), nullable=False)
    note: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        CheckConstraint(_in_check("intent", INTENTS), name="ck_corrections_intent"),
        CheckConstraint(_in_check("priority", PRIORITIES), name="ck_corrections_priority"),
        CheckConstraint(_in_check("sentiment", SENTIMENTS), name="ck_corrections_sentiment"),
    )


class RoutingRule(Base):
    __tablename__ = "routing_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    position: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    intent: Mapped[str | None] = mapped_column(String(32), nullable=True)
    priority: Mapped[str | None] = mapped_column(String(8), nullable=True)
    sentiment: Mapped[str | None] = mapped_column(String(16), nullable=True)
    queue: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        UniqueConstraint("position", name="uq_routing_rules_position"),
        CheckConstraint(
            f"intent IS NULL OR {_in_check('intent', INTENTS)}", name="ck_routing_rules_intent"
        ),
        CheckConstraint(
            f"priority IS NULL OR {_in_check('priority', PRIORITIES)}",
            name="ck_routing_rules_priority",
        ),
        CheckConstraint(
            f"sentiment IS NULL OR {_in_check('sentiment', SENTIMENTS)}",
            name="ck_routing_rules_sentiment",
        ),
        CheckConstraint(
            "intent IS NOT NULL OR priority IS NOT NULL OR sentiment IS NOT NULL",
            name="ck_routing_rules_at_least_one",
        ),
    )
