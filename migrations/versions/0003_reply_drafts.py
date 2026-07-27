"""reply drafts table and reply provider purposes

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-27
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

_STATE = "IN ('draft', 'approved', 'discarded')"
# purpose CHECK before and after adding the two reply-draft purposes.
_PURPOSE_OLD = "purpose IN ('classify', 'repair')"
_PURPOSE_NEW = "purpose IN ('classify', 'repair', 'reply_draft', 'reply_repair')"


def upgrade() -> None:
    op.create_table(
        "reply_drafts",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("ticket_id", sa.String(length=32), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("prompt_version", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["ticket_id"], ["tickets.id"], name="fk_reply_drafts_ticket"),
        sa.UniqueConstraint("ticket_id", name="uq_reply_drafts_ticket_id"),
        sa.CheckConstraint(f"state {_STATE}", name="ck_reply_drafts_state"),
    )

    # SQLite cannot alter a CHECK in place; recreate the table with the widened
    # purpose set so reply-draft calls satisfy the backstop constraint.
    with op.batch_alter_table("llm_calls", recreate="always") as batch_op:
        batch_op.drop_constraint("ck_llm_calls_purpose", type_="check")
        batch_op.create_check_constraint("ck_llm_calls_purpose", _PURPOSE_NEW)


def downgrade() -> None:
    with op.batch_alter_table("llm_calls", recreate="always") as batch_op:
        batch_op.drop_constraint("ck_llm_calls_purpose", type_="check")
        batch_op.create_check_constraint("ck_llm_calls_purpose", _PURPOSE_OLD)

    op.drop_table("reply_drafts")
