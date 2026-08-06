from __future__ import annotations

import os
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

# Point the app at a throwaway SQLite database before anything imports app.db.
_TMP = Path(tempfile.mkdtemp(prefix="colomeal-test-"))
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMP / 'default.db'}")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("TIMEZONE", "America/New_York")
os.environ.setdefault("PHOTO_DIR", str(_TMP / "photos"))
os.environ.setdefault("STAFF_PIN", "1234")

from sqlalchemy import create_engine, event  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app import models  # noqa: E402,F401  (registers tables on Base.metadata)
from app.db import Base  # noqa: E402
from app.models import MealPeriod, Member, MemberStatus, PlanType  # noqa: E402
from app.seeds.meal_periods import default_periods  # noqa: E402
from app.services.club_settings import ensure_defaults, load_config  # noqa: E402

EASTERN = ZoneInfo("America/New_York")


@pytest.fixture
def db():
    """A fresh SQLite database per test, seeded with the real meal schedule.

    SQLite is deliberate: it honours the partial unique indexes the duplicate
    guard depends on, so that constraint is exercised for real rather than
    stubbed. The same DDL is emitted for Postgres in production.
    """
    path = _TMP / f"{uuid.uuid4().hex}.db"
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_conn, _record):  # pragma: no cover - trivial
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = Session()

    ensure_defaults(session)
    session.add_all([MealPeriod(**row) for row in default_periods()])
    session.commit()

    try:
        yield session
    finally:
        session.close()
        engine.dispose()
        path.unlink(missing_ok=True)


@pytest.fixture
def config(db):
    return load_config(db)


@pytest.fixture
def make_member(db):
    counter = {"n": 0}

    def _make(
        first_name: str = "Test",
        last_name: str = "Member",
        plan_type: str = PlanType.PLAN_19.value,
        status: str = MemberStatus.ACTIVE.value,
        class_year: int = 2027,
        puid: str | None = None,
    ) -> Member:
        counter["n"] += 1
        member = Member(
            first_name=first_name,
            last_name=last_name,
            puid=puid or f"90000{counter['n']:04d}",
            class_year=class_year,
            plan_type=plan_type,
            status=status,
        )
        db.add(member)
        db.commit()
        db.refresh(member)
        return member

    return _make


def eastern(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """An aware Eastern-time instant, the way a kiosk scan arrives."""
    return datetime(year, month, day, hour, minute, tzinfo=EASTERN)
