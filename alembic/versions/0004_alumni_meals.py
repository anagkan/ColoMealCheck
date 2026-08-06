"""Alumni meals: a meal with no member behind it.

Two changes, and the second is the load-bearing one: attendance.member_id has to
become nullable, because an alum is not on the roster and there is nobody to
point the row at.

Revision ID: 0004
Revises: 0003
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

COLUMNS = {
    "alumni_first_name": lambda: sa.Column("alumni_first_name", sa.String(80), nullable=True),
    "alumni_last_name": lambda: sa.Column("alumni_last_name", sa.String(80), nullable=True),
    "alumni_class_year": lambda: sa.Column("alumni_class_year", sa.Integer, nullable=True),
    "alumni_email": lambda: sa.Column("alumni_email", sa.String(255), nullable=True),
    "alumni_phone": lambda: sa.Column("alumni_phone", sa.String(32), nullable=True),
}


def _existing() -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns("attendance")}


def _member_id_is_nullable() -> bool:
    for column in sa.inspect(op.get_bind()).get_columns("attendance"):
        if column["name"] == "member_id":
            return bool(column["nullable"])
    return False


def _alter_member_id(nullable: bool) -> None:
    """Flip member_id's NOT NULL, on the dialects where that is a thing.

    SQLite cannot ALTER a column's nullability, and a batch rebuild would have to
    reconstruct the two partial unique indexes on this table by hand. It does not
    need to: the only SQLite databases here are the ones the test suite builds
    straight from models.py, which are already correct. Production is Postgres.
    """
    if op.get_bind().dialect.name == "sqlite":
        return
    op.alter_column("attendance", "member_id", existing_type=sa.Integer(), nullable=nullable)


def upgrade() -> None:
    # Adds only what is missing, for the reason 0002 and 0003 give: revision 0001
    # builds the schema from models.py, so a database created today already has
    # these columns while one stamped last week does not.
    present = _existing()
    for name, column in COLUMNS.items():
        if name not in present:
            op.add_column("attendance", column())
    if not _member_id_is_nullable():
        _alter_member_id(nullable=True)


def downgrade() -> None:
    # Alumni rows have no member to fall back on, so they cannot survive
    # member_id becoming NOT NULL again. They go first, and deliberately: the
    # alternative is a migration that fails halfway on a live database.
    op.execute(sa.text("DELETE FROM attendance WHERE kind = 'alumni'"))
    if _member_id_is_nullable():
        _alter_member_id(nullable=False)
    present = _existing()
    for name in reversed(list(COLUMNS)):
        if name in present:
            op.drop_column("attendance", name)
