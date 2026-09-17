"""Family and professor guest meals are exempt from the monthly quota.

Revision ID: 0007
Revises: 0006
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

COLUMNS = ("guest_is_family", "guest_is_professor")


def _existing() -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns("attendance")}


def upgrade() -> None:
    # Revision 0001 creates tables from the current models on a fresh install.
    existing = _existing()
    for name in COLUMNS:
        if name not in existing:
            op.add_column(
                "attendance",
                sa.Column(name, sa.Boolean(), nullable=False, server_default=sa.false()),
            )


def downgrade() -> None:
    existing = _existing()
    for name in reversed(COLUMNS):
        if name in existing:
            op.drop_column("attendance", name)
