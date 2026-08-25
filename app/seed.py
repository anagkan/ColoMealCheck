"""Idempotent first-boot seeding: settings, meal periods, admin account.

Run on every container start. Existing rows are never overwritten, so editing
the schedule in the admin UI survives a restart. The admin account is the one
exception — see `seed_admin`.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal
from app.models import MealPeriod, StaffRole, StaffUser
from app.security import hash_password, verify_password
from app.seeds.meal_periods import default_periods
from app.services.club_settings import ensure_defaults

# Marks the account ADMIN_USERNAME/ADMIN_PASSWORD manage, so that renaming the
# admin in .env renames that row rather than leaving a second live admin behind
# with a password nobody remembers setting.
MANAGED_DISPLAY_NAME = "Bootstrap Admin"


def seed_meal_periods(db: Session) -> int:
    if db.query(MealPeriod).count() > 0:
        return 0
    rows = [MealPeriod(**row) for row in default_periods()]
    db.add_all(rows)
    db.commit()
    return len(rows)


@dataclass
class AdminSeedResult:
    """What `seed_admin` did, so `main` can say so in the container log."""

    generated_password: str | None = None
    note: str | None = None


def _managed_account(db: Session, username: str) -> StaffUser | None:
    """The row ADMIN_USERNAME refers to.

    Matched by username first. Failing that, an account still carrying the
    marker display name is treated as the same account under an old name — that
    is what makes changing ADMIN_USERNAME a rename instead of leaving the
    previous admin live alongside the new one.
    """
    existing = db.scalar(select(StaffUser).where(StaffUser.username == username))
    if existing is not None:
        return existing
    return db.scalar(select(StaffUser).where(StaffUser.display_name == MANAGED_DISPLAY_NAME))


def seed_admin(db: Session) -> AdminSeedResult:
    """Reconcile the admin account with ADMIN_USERNAME / ADMIN_PASSWORD.

    With a password set, .env is the source of truth and is re-applied on every
    boot: edit it, restart, and you can log in with the new one. Without that,
    the variables were silently dead after the first boot — the account already
    existed, so nothing read them again — and there is no screen in the app that
    changes an admin password instead.

    With the password left blank the account is not managed here at all: one is
    generated and printed on a fresh database, exactly as before, and an
    existing account is left untouched.
    """
    settings = get_settings()
    username = (settings.admin_username or "admin").strip()
    password = settings.admin_password

    if not password:
        if db.query(StaffUser).count() > 0:
            return AdminSeedResult()
        generated = secrets.token_urlsafe(12)
        db.add(
            StaffUser(
                username=username,
                display_name=MANAGED_DISPLAY_NAME,
                password_hash=hash_password(generated),
                role=StaffRole.ADMIN.value,
                is_active=True,
            )
        )
        db.commit()
        return AdminSeedResult(generated_password=generated)

    account = _managed_account(db, username)
    if account is None:
        db.add(
            StaffUser(
                username=username,
                display_name=MANAGED_DISPLAY_NAME,
                password_hash=hash_password(password),
                role=StaffRole.ADMIN.value,
                is_active=True,
            )
        )
        db.commit()
        return AdminSeedResult(note=f"created admin '{username}' from ADMIN_PASSWORD")

    changes = []
    if account.username != username:
        changes.append(f"renamed '{account.username}' to '{username}'")
        account.username = username
    if not verify_password(password, account.password_hash):
        # Only rehash on a real change, so a restart is not a password write and
        # the log does not claim an update on every boot.
        changes.append("password updated from ADMIN_PASSWORD")
        account.password_hash = hash_password(password)
    if not account.is_active:
        # A locked-out admin with the password in .env is a dead end while no
        # screen exists to re-enable one.
        changes.append("reactivated")
        account.is_active = True
    if account.role != StaffRole.ADMIN.value:
        changes.append("restored admin role")
        account.role = StaffRole.ADMIN.value

    if not changes:
        return AdminSeedResult()
    db.commit()
    return AdminSeedResult(note=f"admin '{username}': " + ", ".join(changes))


def main() -> None:
    settings = get_settings()
    settings.photo_dir.mkdir(parents=True, exist_ok=True)

    with SessionLocal() as db:
        ensure_defaults(db)
        created = seed_meal_periods(db)
        if created:
            print(f"[seed] created {created} meal periods")
        result = seed_admin(db)
        if result.note:
            print(f"[seed] {result.note}")
        if result.generated_password:
            print("=" * 62)
            print("[seed] Created the first admin account:")
            print(f"[seed]   username: {settings.admin_username}")
            print(f"[seed]   password: {result.generated_password}")
            print("[seed] This is shown once. To choose your own, set")
            print("[seed] ADMIN_USERNAME and ADMIN_PASSWORD in .env and restart.")
            print("=" * 62)


if __name__ == "__main__":
    main()
