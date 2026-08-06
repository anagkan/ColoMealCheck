"""Idempotent first-boot seeding: settings, meal periods, bootstrap admin.

Run on every container start. Existing rows are never overwritten, so editing
the schedule in the admin UI survives a restart.
"""
from __future__ import annotations

import secrets

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal
from app.models import MealPeriod, StaffRole, StaffUser
from app.security import hash_password
from app.seeds.meal_periods import default_periods
from app.services.club_settings import ensure_defaults


def seed_meal_periods(db: Session) -> int:
    if db.query(MealPeriod).count() > 0:
        return 0
    rows = [MealPeriod(**row) for row in default_periods()]
    db.add_all(rows)
    db.commit()
    return len(rows)


def seed_admin(db: Session) -> str | None:
    """Create the first admin if there are no staff users at all.

    Returns a generated password if one was generated, so the caller can print
    it to the container log exactly once.
    """
    if db.query(StaffUser).count() > 0:
        return None

    settings = get_settings()
    generated = None
    password = settings.bootstrap_admin_password
    if not password:
        password = secrets.token_urlsafe(12)
        generated = password

    db.add(
        StaffUser(
            username=settings.bootstrap_admin_username,
            display_name="Bootstrap Admin",
            password_hash=hash_password(password),
            role=StaffRole.ADMIN.value,
            is_active=True,
        )
    )
    db.commit()
    return generated


def main() -> None:
    settings = get_settings()
    settings.photo_dir.mkdir(parents=True, exist_ok=True)

    with SessionLocal() as db:
        ensure_defaults(db)
        created = seed_meal_periods(db)
        if created:
            print(f"[seed] created {created} meal periods")
        generated = seed_admin(db)
        if generated:
            print("=" * 62)
            print("[seed] Created the first admin account:")
            print(f"[seed]   username: {settings.bootstrap_admin_username}")
            print(f"[seed]   password: {generated}")
            print("[seed] Log in at /admin and change it. This is shown once.")
            print("=" * 62)


if __name__ == "__main__":
    main()
