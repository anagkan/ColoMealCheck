"""Existing attendance keeps counting toward quota after the schema upgrade."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_guest_category_migration_preserves_existing_meals(tmp_path):
    path = Path(__file__).resolve().parents[1] / "alembic/versions/0007_guest_categories.py"
    spec = spec_from_file_location("guest_categories", path)
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    with engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE attendance (id INTEGER PRIMARY KEY, kind TEXT)"))
        connection.execute(sa.text("INSERT INTO attendance (kind) VALUES ('guest')"))
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            migration.upgrade()  # Fresh installs already have these columns.
            row = connection.execute(sa.text("SELECT * FROM attendance")).mappings().one()
            assert row["kind"] == "guest"
            assert row["guest_is_family"] == 0
            assert row["guest_is_professor"] == 0
            connection.execute(sa.text("INSERT INTO attendance (kind) VALUES ('guest')"))
            assert connection.scalar(sa.text(
                "SELECT COUNT(*) FROM attendance WHERE guest_is_family = 0 AND guest_is_professor = 0"
            )) == 2
            migration.downgrade()
            assert connection.scalar(sa.text("SELECT COUNT(*) FROM attendance")) == 2
    engine.dispose()
