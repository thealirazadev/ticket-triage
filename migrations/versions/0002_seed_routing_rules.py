"""seed default routing rules

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-18
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

_RULES = [
    {"position": 1, "intent": None, "priority": "P1", "sentiment": None, "queue": "urgent"},
    {"position": 2, "intent": "billing", "priority": None, "sentiment": None, "queue": "billing"},
    {"position": 3, "intent": "refund", "priority": None, "sentiment": None, "queue": "billing"},
    {"position": 4, "intent": "bug", "priority": None, "sentiment": None, "queue": "technical"},
    {
        "position": 5,
        "intent": "account_access",
        "priority": None,
        "sentiment": None,
        "queue": "security",
    },
]


def _table() -> sa.Table:
    return sa.table(
        "routing_rules",
        sa.column("position", sa.Integer),
        sa.column("intent", sa.String),
        sa.column("priority", sa.String),
        sa.column("sentiment", sa.String),
        sa.column("queue", sa.String),
    )


def upgrade() -> None:
    op.bulk_insert(_table(), _RULES)


def downgrade() -> None:
    positions = tuple(r["position"] for r in _RULES)
    op.execute(_table().delete().where(sa.column("position").in_(positions)))
