"""A NetID on the member record.

Nullable with no backfill, for the reason 0002 gives about guests: members
enrolled before this column existed have no NetID on file, and inventing one
would be worse than leaving the gap visible. New enrollments require it at the
edge that collects them, and the admin members list shows a dash for everyone
still missing one — which is the backfill worklist.

Revision ID: 0005
Revises: 0004
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

COLUMN = "netid"
INDEX = "ix_members_netid"


def _inspector():
    return sa.inspect(op.get_bind())


def _existing_columns() -> set[str]:
    return {c["name"] for c in _inspector().get_columns("members")}


def _existing_indexes() -> set[str]:
    return {i["name"] for i in _inspector().get_indexes("members")}


def upgrade() -> None:
    # Adds only if missing: revision 0001 builds the schema from models.py, so a
    # database created today already has the column while one stamped last week
    # does not.
    if COLUMN not in _existing_columns():
        op.add_column("members", sa.Column(COLUMN, sa.String(32), nullable=True))
    # Unique, but only over the rows that have one — both Postgres and SQLite
    # allow repeated NULLs in a unique index, so the members who predate this
    # column do not collide with each other.
    if INDEX not in _existing_indexes():
        op.create_index(INDEX, "members", [COLUMN], unique=True)


def downgrade() -> None:
    if INDEX in _existing_indexes():
        op.drop_index(INDEX, table_name="members")
    if COLUMN in _existing_columns():
        op.drop_column("members", COLUMN)
