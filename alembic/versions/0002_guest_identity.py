"""Record a guest's name parts and Princeton NetID.

Revision ID: 0002
Revises: 0001
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

# Nullable, with no backfill: guest rows written before this revision were only
# ever given a free-text name, and inventing a NetID for them would be worse
# than leaving the gap visible.
COLUMNS = {
    "guest_first_name": lambda: sa.Column("guest_first_name", sa.String(80), nullable=True),
    "guest_last_name": lambda: sa.Column("guest_last_name", sa.String(80), nullable=True),
    "guest_netid": lambda: sa.Column("guest_netid", sa.String(32), nullable=True),
}


def _existing() -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns("attendance")}


def upgrade() -> None:
    # Adds only what is missing, because revision 0001 creates the schema from
    # models.py rather than from transcribed DDL: a database stamped at 0001
    # *before* this change lacks these columns, while one created from the
    # current metadata already has them. Both must end up at 0002.
    present = _existing()
    for name, column in COLUMNS.items():
        if name not in present:
            op.add_column("attendance", column())


def downgrade() -> None:
    present = _existing()
    for name in reversed(list(COLUMNS)):
        if name in present:
            op.drop_column("attendance", name)
