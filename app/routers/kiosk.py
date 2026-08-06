from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.db import get_db
from app.services.club_settings import load_config
from app.services.periods import next_period, resolve_period, seconds_remaining
from app.templating import templates

router = APIRouter(tags=["kiosk"])


@router.get("/enroll")
def enroll_page(request: Request, value: str = ""):
    """Card enrollment, on its own page rather than a panel over the kiosk.

    Staff work through this while a queue may be forming behind them, so it
    must not occupy the check-in screen. `value` is prefilled when a member
    arrives at an unrecognized card and staff follow the link from the result.
    """
    return templates.TemplateResponse(
        request, "kiosk/enroll.html", {"prefill_value": value.strip()}
    )


@router.get("/")
def kiosk(request: Request, db: Session = Depends(get_db)):
    config = load_config(db)
    now = datetime.now(timezone.utc)
    resolved = resolve_period(db, now)
    upcoming = next_period(db, now)
    return templates.TemplateResponse(
        request,
        "kiosk/index.html",
        {
            "period": resolved.period,
            "service_date": resolved.service_date,
            "result_seconds": config.result_screen_seconds,
            "undo_seconds": config.undo_window_seconds,
            # Seeds the banner and its countdown in the markup, so the screen is
            # correct on first paint rather than flashing a placeholder until
            # the first poll lands.
            "seconds_remaining": seconds_remaining(resolved),
            "next_period": upcoming.period if upcoming else None,
            "seconds_until_next": upcoming.seconds_until if upcoming else None,
        },
    )
