from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from app.models import Attendance, AttendanceKind, Member
from app.services.periods import periods_for_day


def grant_meals(
    db: Session, member: Member, count: int, starting: date
) -> list[Attendance]:
    """Back-fill `count` member meals starting from `starting`.

    Walks real serving windows day by day so every row lands on a distinct
    (service_date, meal_period) pair — otherwise the duplicate-guard index
    would reject the fixture itself.
    """
    created: list[Attendance] = []
    day = starting
    guard = 0
    while len(created) < count:
        guard += 1
        if guard > 60:  # pragma: no cover - fixture safety net
            raise RuntimeError("ran out of serving windows building fixture")
        for period in periods_for_day(db, day):
            if len(created) >= count:
                break
            row = Attendance(
                member_id=member.id,
                meal_period_id=period.id,
                entry_method="csn",
                service_date=day,
                scanned_at=datetime.combine(day, period.start_time),
                kind=AttendanceKind.MEMBER.value,
            )
            db.add(row)
            created.append(row)
        day += timedelta(days=1)
    db.commit()
    return created


def guest_rows(db: Session, host: Member, count: int, on: date) -> None:
    """Guest meals do not carry the one-per-period constraint, so they can all
    sit on the same day."""
    periods = periods_for_day(db, on)
    period = periods[0] if periods else None
    for i in range(count):
        db.add(
            Attendance(
                member_id=host.id,
                meal_period_id=period.id if period else None,
                entry_method="csn",
                service_date=on,
                scanned_at=datetime.combine(on, period.start_time) if period else None,
                kind=AttendanceKind.GUEST.value,
                guest_name=f"Guest {i + 1}",
                guest_first_name="Guest",
                guest_last_name=str(i + 1),
                guest_netid=f"guest{i + 1}",
            )
        )
    db.commit()
