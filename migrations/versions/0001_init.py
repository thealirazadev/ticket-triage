"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-07-18
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

# CHECK expressions are inlined so this migration is a self-contained snapshot,
# independent of later changes to app.models.
_INTENT = (
    "IN ('billing', 'bug', 'how_to', 'feature_request', " "'account_access', 'refund', 'other')"
)
_PRIORITY = "IN ('P1', 'P2', 'P3', 'P4')"
_SENTIMENT = "IN ('negative', 'neutral', 'positive')"
_CHANNEL = "IN ('email', 'web', 'api')"
_STATUS = "IN ('received', 'triaged', 'needs_human', 'approved', 'corrected')"
_TRIAGE_ERROR = "IN ('parse_failed', 'provider_error', 'circuit_open')"
_PURPOSE = "IN ('classify', 'repair')"
_OUTCOME = "IN ('ok', 'parse_error', 'timeout', 'api_error')"


def upgrade() -> None:
    op.create_table(
        "tickets",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("external_id", sa.String(length=128), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("sender", sa.String(length=320), nullable=False),
        sa.Column("subject", sa.String(length=500), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("queue", sa.String(length=64), nullable=True),
        sa.Column("triage_error", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("external_id", name="uq_tickets_external_id"),
        sa.CheckConstraint(f"channel {_CHANNEL}", name="ck_tickets_channel"),
        sa.CheckConstraint(f"status {_STATUS}", name="ck_tickets_status"),
        sa.CheckConstraint(
            f"triage_error IS NULL OR triage_error {_TRIAGE_ERROR}",
            name="ck_tickets_triage_error",
        ),
    )
    op.create_index("ix_tickets_status", "tickets", ["status"])
    op.create_index("ix_tickets_created_at", "tickets", ["created_at"])

    op.create_table(
        "triages",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("ticket_id", sa.String(length=32), nullable=False),
        sa.Column("intent", sa.String(length=32), nullable=False),
        sa.Column("priority", sa.String(length=8), nullable=False),
        sa.Column("sentiment", sa.String(length=16), nullable=False),
        sa.Column("summary", sa.String(length=600), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("prompt_version", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["ticket_id"], ["tickets.id"], name="fk_triages_ticket"),
        sa.UniqueConstraint("ticket_id", name="uq_triages_ticket_id"),
        sa.CheckConstraint(f"intent {_INTENT}", name="ck_triages_intent"),
        sa.CheckConstraint(f"priority {_PRIORITY}", name="ck_triages_priority"),
        sa.CheckConstraint(f"sentiment {_SENTIMENT}", name="ck_triages_sentiment"),
    )

    op.create_table(
        "llm_calls",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("ticket_id", sa.String(length=32), nullable=True),
        sa.Column("purpose", sa.String(length=16), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(precision=10, scale=6), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["ticket_id"], ["tickets.id"], name="fk_llm_calls_ticket"),
        sa.CheckConstraint(f"purpose {_PURPOSE}", name="ck_llm_calls_purpose"),
        sa.CheckConstraint(f"outcome {_OUTCOME}", name="ck_llm_calls_outcome"),
    )
    op.create_index("ix_llm_calls_ticket_id", "llm_calls", ["ticket_id"])
    op.create_index("ix_llm_calls_created_at", "llm_calls", ["created_at"])

    op.create_table(
        "corrections",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("ticket_id", sa.String(length=32), nullable=False),
        sa.Column("intent", sa.String(length=32), nullable=False),
        sa.Column("priority", sa.String(length=8), nullable=False),
        sa.Column("sentiment", sa.String(length=16), nullable=False),
        sa.Column("note", sa.String(length=1000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["ticket_id"], ["tickets.id"], name="fk_corrections_ticket"),
        sa.UniqueConstraint("ticket_id", name="uq_corrections_ticket_id"),
        sa.CheckConstraint(f"intent {_INTENT}", name="ck_corrections_intent"),
        sa.CheckConstraint(f"priority {_PRIORITY}", name="ck_corrections_priority"),
        sa.CheckConstraint(f"sentiment {_SENTIMENT}", name="ck_corrections_sentiment"),
    )

    op.create_table(
        "routing_rules",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("intent", sa.String(length=32), nullable=True),
        sa.Column("priority", sa.String(length=8), nullable=True),
        sa.Column("sentiment", sa.String(length=16), nullable=True),
        sa.Column("queue", sa.String(length=64), nullable=False),
        sa.UniqueConstraint("position", name="uq_routing_rules_position"),
        sa.CheckConstraint(f"intent IS NULL OR intent {_INTENT}", name="ck_routing_rules_intent"),
        sa.CheckConstraint(
            f"priority IS NULL OR priority {_PRIORITY}", name="ck_routing_rules_priority"
        ),
        sa.CheckConstraint(
            f"sentiment IS NULL OR sentiment {_SENTIMENT}", name="ck_routing_rules_sentiment"
        ),
        sa.CheckConstraint(
            "intent IS NOT NULL OR priority IS NOT NULL OR sentiment IS NOT NULL",
            name="ck_routing_rules_at_least_one",
        ),
    )


def downgrade() -> None:
    op.drop_table("routing_rules")
    op.drop_table("corrections")
    op.drop_index("ix_llm_calls_created_at", table_name="llm_calls")
    op.drop_index("ix_llm_calls_ticket_id", table_name="llm_calls")
    op.drop_table("llm_calls")
    op.drop_table("triages")
    op.drop_index("ix_tickets_created_at", table_name="tickets")
    op.drop_index("ix_tickets_status", table_name="tickets")
    op.drop_table("tickets")
