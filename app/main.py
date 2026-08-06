from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings
from app.db import engine
from app.routers import admin, api_scan, auth, kiosk

settings = get_settings()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings.photo_dir.mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(title="ColoMealCheck", lifespan=lifespan)

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    session_cookie=settings.session_cookie,
    max_age=settings.session_max_age_seconds,
    same_site="lax",
    # The kiosk deployment is plain HTTP on the club LAN, so the cookie cannot
    # be Secure-only or admin login would break. See README for the trade-off.
    https_only=False,
)

_static_dir = Path(__file__).parent / "static"
_static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

settings.photo_dir.mkdir(parents=True, exist_ok=True)
app.mount("/photos", StaticFiles(directory=str(settings.photo_dir)), name="photos")

app.include_router(kiosk.router)
app.include_router(api_scan.router)
app.include_router(auth.router)
app.include_router(admin.router)


@app.exception_handler(HTTPException)
async def unauthorized_to_login(request: Request, exc: HTTPException):
    """Send a signed-out browser to the login form instead of raw JSON.

    API clients (the kiosk) still get the JSON error — they check status codes,
    and a redirect would be useless to them.
    """
    wants_html = "text/html" in request.headers.get("accept", "")
    if exc.status_code == status.HTTP_401_UNAUTHORIZED and wants_html:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    return await http_exception_handler(request, exc)


"""Names for the fields staff can actually see, so a refusal names the box on
the form rather than the attribute on the model."""
_FIELD_LABELS = {
    "class_year": "Class year",
    "credential_type": "Credential type",
    "email": "Email address",
    "first_name": "First name",
    "guest_first_name": "Guest first name",
    "guest_last_name": "Guest last name",
    "guest_netid": "Guest NetID",
    "guest_netid_reason": "Reason for no NetID",
    "last_name": "Last name",
    "member_id": "Member",
    "netid": "NetID",
    "occurred_at": "Scan time",
    "phone": "Phone number",
    "photo_data_url": "Photo",
    "plan_type": "Meal plan",
    "puid": "PUID",
    "staff_pin": "Staff PIN",
    "value": "Card number",
}


def _field_label(loc: tuple) -> str:
    for part in reversed(loc):
        if isinstance(part, str) and part not in {"body", "query", "path"}:
            return _FIELD_LABELS.get(part, part.replace("_", " ").capitalize())
    return "The request"


def _describe(errors: list[dict]) -> str:
    """One sentence per bad field, at most three — enough to fix the form."""
    parts = []
    for err in errors[:3]:
        kind = err.get("type", "")
        if kind == "json_invalid":
            parts.append("The request was not valid JSON")
            continue
        label = _field_label(tuple(err.get("loc", ())))
        if kind == "missing":
            parts.append(f"{label} is required")
            continue
        msg = err.get("msg") or "is not valid"
        parts.append(f"{label}: {msg[0].lower() + msg[1:]}")
    if not parts:
        return "Some of what was submitted is not valid."
    return "; ".join(parts) + "."


@app.exception_handler(RequestValidationError)
async def readable_validation_error(_request: Request, exc: RequestValidationError):
    """Answer a schema rejection with a sentence, like every other refusal here.

    FastAPI's default 422 body puts a list of error objects in `detail`. The
    kiosk and the enrollment page both show `detail` to whoever is standing at
    the desk, so the default rendered as "[object Object]" — a dead end for a
    class year of 1234 or a missing PIN, which are both ordinary typos with an
    obvious fix. Same shape as an HTTPException so the pages need no special
    case: `detail` is always a string they can print.
    """
    return JSONResponse(
        {"detail": _describe(exc.errors())},
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
    )


@app.get("/healthz")
def healthz() -> JSONResponse:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - the endpoint's job is to report this
        return JSONResponse({"ok": False, "database": str(exc)}, status_code=503)
    return JSONResponse({"ok": True, "database": "up"})
