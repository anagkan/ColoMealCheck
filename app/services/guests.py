"""The 2-guest-meals-per-calendar-month benefit.

Separate bucket from the weekly allotment in both directions: hosting a guest
never spends the host's own meals, and a host who is over their weekly
allotment may still have guest meals available.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Attendance, AttendanceKind, Member
from app.services.club_settings import ClubConfig
from app.services.periods import month_bounds

# A guest's NetID is checked with the same rule a member's is — see
# services/netid.py, which is where that rule lives now that both collect one.


@dataclass(frozen=True)
class GuestUsage:
    month_start: date
    month_end: date
    used: int
    quota: int

    @property
    def remaining(self) -> int:
        return max(0, self.quota - self.used)

    @property
    def exhausted(self) -> bool:
        return self.used >= self.quota


def count_guest_meals(db: Session, host_id: int, start: date, end: date) -> int:
    stmt = select(func.count(Attendance.id)).where(
        Attendance.member_id == host_id,
        Attendance.kind == AttendanceKind.GUEST.value,
        Attendance.voided_at.is_(None),
        Attendance.service_date >= start,
        Attendance.service_date <= end,
    )
    return int(db.scalar(stmt) or 0)


def guest_usage(
    db: Session, host: Member, service_date: date, config: ClubConfig
) -> GuestUsage:
    start, end = month_bounds(service_date)
    return GuestUsage(
        month_start=start,
        month_end=end,
        used=count_guest_meals(db, host.id, start, end),
        quota=config.guest_meals_per_month,
    )
