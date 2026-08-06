from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import StaffUser
from app.security import verify_password
from app.templating import templates

router = APIRouter(tags=["auth"])


@router.get("/login")
def login_form(request: Request):
    return templates.TemplateResponse(request, "admin/login.html", {"error": None})


@router.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.scalar(select(StaffUser).where(StaffUser.username == username.strip()))
    if user is None or not user.is_active or not verify_password(password, user.password_hash):
        # Deliberately one message for all three failures.
        return templates.TemplateResponse(
            request,
            "admin/login.html",
            {"error": "Incorrect username or password."},
            status_code=401,
        )

    request.session["user_id"] = user.id
    return RedirectResponse("/admin", status_code=303)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
