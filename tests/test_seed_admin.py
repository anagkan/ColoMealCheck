"""The admin account reconciles against ADMIN_USERNAME / ADMIN_PASSWORD.

These variables are the only way to change an admin password — there is no
screen in the app that does it — so the thing worth pinning down is that they
are re-read on every boot, not just the first one.
"""
from __future__ import annotations

import pytest

from app.config import Settings, get_settings
from app.models import StaffRole, StaffUser
from app.security import hash_password, verify_password
from app.seed import MANAGED_DISPLAY_NAME, seed_admin


@pytest.fixture
def env(monkeypatch):
    """Set ADMIN_* for one call to seed_admin, past the settings cache."""

    def apply(username: str = "admin", password: str = "") -> None:
        monkeypatch.setenv("ADMIN_USERNAME", username)
        monkeypatch.setenv("ADMIN_PASSWORD", password)
        get_settings.cache_clear()
        monkeypatch.setattr("app.seed.get_settings", lambda: Settings())

    yield apply
    get_settings.cache_clear()


def _admins(db) -> list[StaffUser]:
    return db.query(StaffUser).all()


def test_generates_and_reports_a_password_on_an_empty_database(db, env):
    env(password="")
    result = seed_admin(db)

    assert result.generated_password
    account = _admins(db)[0]
    assert account.username == "admin"
    assert account.role == StaffRole.ADMIN.value
    assert verify_password(result.generated_password, account.password_hash)


def test_uses_the_configured_password_instead_of_generating_one(db, env):
    env(username="steward", password="from-dot-env")
    result = seed_admin(db)

    assert result.generated_password is None
    account = _admins(db)[0]
    assert account.username == "steward"
    assert verify_password("from-dot-env", account.password_hash)


def test_a_changed_password_is_applied_on_the_next_boot(db, env):
    env(password="first-one")
    seed_admin(db)

    env(password="second-one")
    result = seed_admin(db)

    account = _admins(db)[0]
    assert verify_password("second-one", account.password_hash)
    assert not verify_password("first-one", account.password_hash)
    assert "password updated" in result.note


def test_an_unchanged_password_is_not_rewritten(db, env):
    """A restart is not a password write, and must not claim to be one."""
    env(password="steady")
    seed_admin(db)
    before = _admins(db)[0].password_hash

    result = seed_admin(db)

    assert result.note is None
    assert _admins(db)[0].password_hash == before


def test_a_changed_username_renames_rather_than_adding_a_second_admin(db, env):
    env(username="admin", password="shared")
    seed_admin(db)

    env(username="jo", password="shared")
    result = seed_admin(db)

    accounts = _admins(db)
    assert len(accounts) == 1, "the old admin must not be left live alongside the new"
    assert accounts[0].username == "jo"
    assert "renamed" in result.note


def test_a_blank_password_leaves_an_existing_account_alone(db, env):
    env(password="chosen")
    seed_admin(db)

    env(password="")
    result = seed_admin(db)

    assert result.generated_password is None
    assert result.note is None
    assert verify_password("chosen", _admins(db)[0].password_hash)


def test_a_deactivated_managed_admin_is_restored(db, env):
    """Otherwise .env holds the password to an account that cannot sign in, and
    nothing in the app can re-enable it."""
    env(password="chosen")
    seed_admin(db)
    account = _admins(db)[0]
    account.is_active = False
    account.role = StaffRole.STAFF.value
    db.commit()

    seed_admin(db)

    account = _admins(db)[0]
    assert account.is_active
    assert account.role == StaffRole.ADMIN.value


def test_other_staff_accounts_are_untouched(db, env):
    db.add(
        StaffUser(
            username="pat",
            display_name="Pat Cook",
            password_hash=hash_password("pats-own"),
            role=StaffRole.STAFF.value,
            is_active=True,
        )
    )
    db.commit()

    env(username="admin", password="chosen")
    seed_admin(db)

    pat = db.query(StaffUser).filter_by(username="pat").one()
    assert verify_password("pats-own", pat.password_hash)
    assert pat.role == StaffRole.STAFF.value
    managed = db.query(StaffUser).filter_by(display_name=MANAGED_DISPLAY_NAME).one()
    assert managed.username == "admin"


def test_the_old_bootstrap_variable_names_still_work(db, monkeypatch):
    """An .env written before the rename must keep signing its admin in."""
    monkeypatch.delenv("ADMIN_USERNAME", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.setenv("BOOTSTRAP_ADMIN_USERNAME", "legacy")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", "legacy-secret")
    get_settings.cache_clear()
    monkeypatch.setattr("app.seed.get_settings", lambda: Settings())
    try:
        seed_admin(db)
    finally:
        get_settings.cache_clear()

    account = _admins(db)[0]
    assert account.username == "legacy"
    assert verify_password("legacy-secret", account.password_hash)
