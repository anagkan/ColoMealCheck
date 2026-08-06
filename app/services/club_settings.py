"""Operational knobs, stored in the database so staff can change them.

Every number the rules engine cares about is read through here — there are no
magic 19s, 14s, 9s or 2s in the service code.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models import ClubSetting, PlanType

DEFAULTS: dict[str, tuple[str, str]] = {
    "week_start_day": ("0", "First day of the meal week. 0=Monday .. 6=Sunday."),
    "guest_meals_per_month": ("2", "Guest meals each member may host per calendar month."),
    "plan_19_meals": ("19", "Weekly meal allotment for the 19-meal plan."),
    "plan_14_meals": ("14", "Weekly meal allotment for the 14-meal plan."),
    "plan_rca_paa_meals": ("9", "Weekly meal allotment for the RCA/PAA plan."),
    "result_screen_seconds": ("6", "How long the kiosk shows a check-in result."),
    "undo_window_seconds": ("60", "How long after a check-in the kiosk offers Undo."),
}


@dataclass(frozen=True)
class ClubConfig:
    week_start_day: int
    guest_meals_per_month: int
    plan_19_meals: int
    plan_14_meals: int
    plan_rca_paa_meals: int
    result_screen_seconds: int
    undo_window_seconds: int

    def allotment_for(self, plan_type: str) -> int | None:
        """Weekly meal allowance, or None for members with no meal plan."""
        if plan_type == PlanType.PLAN_19.value:
            return self.plan_19_meals
        if plan_type == PlanType.PLAN_14.value:
            return self.plan_14_meals
        if plan_type == PlanType.RCA_PAA.value:
            return self.plan_rca_paa_meals
        return None


def load_config(db: Session) -> ClubConfig:
    stored = {row.key: row.value for row in db.query(ClubSetting).all()}

    def as_int(key: str) -> int:
        raw = stored.get(key, DEFAULTS[key][0])
        try:
            return int(raw)
        except (TypeError, ValueError):
            return int(DEFAULTS[key][0])

    return ClubConfig(
        week_start_day=as_int("week_start_day"),
        guest_meals_per_month=as_int("guest_meals_per_month"),
        plan_19_meals=as_int("plan_19_meals"),
        plan_14_meals=as_int("plan_14_meals"),
        plan_rca_paa_meals=as_int("plan_rca_paa_meals"),
        result_screen_seconds=as_int("result_screen_seconds"),
        undo_window_seconds=as_int("undo_window_seconds"),
    )


def ensure_defaults(db: Session) -> None:
    existing = {row.key for row in db.query(ClubSetting).all()}
    for key, (value, description) in DEFAULTS.items():
        if key not in existing:
            db.add(ClubSetting(key=key, value=value, description=description))
    db.commit()


def set_value(db: Session, key: str, value: str) -> None:
    row = db.get(ClubSetting, key)
    if row is None:
        row = ClubSetting(key=key, value=value, description=DEFAULTS.get(key, ("", ""))[1])
        db.add(row)
    else:
        row.value = value
    db.commit()
