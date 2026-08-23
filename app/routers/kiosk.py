from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.config import get_settings
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
        request,
        "kiosk/enroll.html",
        {
            "prefill_value": value.strip(),
            "bridge_url": get_settings().kiosk_bridge_url,
        },
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
            "bridge_url": get_settings().kiosk_bridge_url,
        },
    )


# --- Progressive web app -----------------------------------------------------
#
# Both of these are ordinary files in app/static, but neither can be served from
# the /static mount.
#
# The service worker's scope is capped by the directory it is served from, so a
# worker at /static/sw.js could only ever control /static/* — never the door
# screen at "/", which is the entire point of it. Serving it from the root is
# what gives it root scope.
#
# The manifest is here for a duller reason: Python's mimetypes module has never
# heard of .webmanifest, so StaticFiles hands it over as application/octet-stream
# and the browser declines to parse it.

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@router.get("/sw.js", include_in_schema=False)
def service_worker() -> FileResponse:
    return FileResponse(
        _STATIC_DIR / "sw.js",
        media_type="application/javascript",
        # The worker is the one file that must never be served stale: it is what
        # decides how stale everything else is allowed to be. no-cache makes the
        # browser revalidate it on every update check rather than sit on a copy
        # for up to a day.
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/manifest.webmanifest", include_in_schema=False)
def web_manifest() -> FileResponse:
    return FileResponse(
        _STATIC_DIR / "manifest.webmanifest",
        media_type="application/manifest+json",
    )
