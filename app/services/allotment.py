"""Weekly meal-plan usage.

Guest meals live in their own bucket and never move these numbers — a member
who hosts three guests in a week still has their full 19, 14 or 9 for themselves.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Attendance, AttendanceKind, MealPeriod, Member
from app.services.club_settings import ClubConfig
from app.services.periods import week_bounds


@dataclass(frozen=True)
class WeeklyUsage:
    week_start: date
    week_end: date
    used: int
    allotment: int | None

    @property
    def has_plan(self) -> bool:
        return self.allotment is not None

    @property
    def remaining(self) -> int | None:
        if self.allotment is None:
            return None
        return max(0, self.allotment - self.used)

    @property
    def is_over(self) -> bool:
        """True once the member has already spent their whole allotment.

        Evaluated *before* the meal being scanned is written, so this answers
        "would this next meal be an overage?".
        """
        return self.allotment is not None and self.used >= self.allotment


def count_member_meals(db: Session, member_id: int, start: date, end: date) -> int:
    stmt = (
        select(func.count(Attendance.id))
        .join(MealPeriod, Attendance.meal_period_id == MealPeriod.id, isouter=True)
        .where(
            Attendance.member_id == member_id,
            Attendance.kind == AttendanceKind.MEMBER.value,
            Attendance.voided_at.is_(None),
            Attendance.service_date >= start,
            Attendance.service_date <= end,
            # A period flagged as not counting (a club-wide free feed, say)
            # is recorded but does not spend the allotment. Rows with no period
            # attached still count.
            (MealPeriod.counts_toward_allotment.is_(True))
            | (Attendance.meal_period_id.is_(None)),
        )
    )
    return int(db.scalar(stmt) or 0)


def weekly_usage(
    db: Session, member: Member, service_date: date, config: ClubConfig
) -> WeeklyUsage:
    start, end = week_bounds(service_date, config.week_start_day)
    used = count_member_meals(db, member.id, start, end)
    return WeeklyUsage(
        week_start=start,
        week_end=end,
        used=used,
        allotment=config.allotment_for(member.plan_type),
    )
