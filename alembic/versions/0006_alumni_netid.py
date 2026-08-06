"""An optional NetID on an alumni meal.

Optional and no substitute for the email-or-phone rule that revision 0004's
columns exist for: a NetID says which alum this was, not how to reach them.
Many keep one for life; plenty have let theirs lapse.

Revision ID: 0006
Revises: 0005
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

COLUMN = "alumni_netid"


def _existing() -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns("attendance")}


def upgrade() -> None:
    # Adds only if missing, as every revision after 0001 does: that revision
    # builds the schema from models.py, so a database created today already has
    # this column while one stamped last week does not.
    if COLUMN not in _existing():
        op.add_column("attendance", sa.Column(COLUMN, sa.String(32), nullable=True))


def downgrade() -> None:
    if COLUMN in _existing():
        op.drop_column("attendance", COLUMN)
