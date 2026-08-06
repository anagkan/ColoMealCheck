from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.config import get_settings

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _local_time(value: datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        return value.strftime("%-I:%M %p")
    return value.astimezone(get_settings().tz).strftime("%-I:%M %p")


def _pretty_date(value: date | None) -> str:
    if value is None:
        return ""
    return value.strftime("%a %-d %b %Y")


def _plan_label(value: str) -> str:
    return {
        "plan_19": "19 meals",
        "plan_14": "14 meals",
        "rca_paa": "RCA/PAA (9 meals)",
        "none": "No plan",
    }.get(value, value)


def _entry_label(value: str) -> str:
    return {
        "csn": "Card",
        "prox": "Card (prox)",
        "pacs": "Card (PACS)",
        "manual_puid": "Typed ID",
        "admin": "Admin",
    }.get(value, value)


templates.env.filters["localtime"] = _local_time
templates.env.filters["prettydate"] = _pretty_date
templates.env.filters["planlabel"] = _plan_label
templates.env.filters["entrylabel"] = _entry_label
