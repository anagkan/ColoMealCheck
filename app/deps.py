"""Shared FastAPI dependencies: sessions, current user, PIN checks."""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import StaffUser
from app.security import check_staff_pin


def current_user(request: Request, db: Session = Depends(get_db)) -> StaffUser | None:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    user = db.get(StaffUser, user_id)
    if user is None or not user.is_active:
        return None
    return user


def require_staff(user: StaffUser | None = Depends(current_user)) -> StaffUser:
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign in required")
    return user


def require_admin(user: StaffUser = Depends(require_staff)) -> StaffUser:
    if not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin access required")
    return user


def authorize_privileged_action(
    pin: str | None, user: StaffUser | None, action: str
) -> str:
    """Privileged kiosk actions accept either a signed-in staff session or the
    shared staff PIN typed at the kiosk.

    Returns an actor string for the audit log — never an anonymous one.
    """
    if user is not None:
        return f"staff:{user.username}"
    if pin and check_staff_pin(pin):
        return "kiosk-pin"
    raise HTTPException(
        status.HTTP_403_FORBIDDEN, f"Staff authorization required to {action}."
    )
