"""Why a guest has no NetID, for the guests who do not have one.

Revision ID: 0003
Revises: 0002
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

COLUMN = "guest_netid_reason"


def _existing() -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns("attendance")}


def upgrade() -> None:
    # Adds only if missing, for the same reason 0002 does: revision 0001 builds
    # the schema from models.py, so a database created today already has this
    # column while one stamped at 0002 last week does not.
    if COLUMN not in _existing():
        op.add_column("attendance", sa.Column(COLUMN, sa.String(255), nullable=True))


def downgrade() -> None:
    if COLUMN in _existing():
        op.drop_column("attendance", COLUMN)
