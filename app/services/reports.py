"""Aggregations behind the admin screens and the CSV exports.

Every query groups by `service_date`, never by a timestamp range — see the note
in models.Attendance for why.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.models import Attendance, AttendanceKind, Credential, MealPeriod, Member
from app.services.club_settings import ClubConfig
from app.services.periods import week_bounds


@dataclass
class MemberWeek:
    member: Member
    used: int
    allotment: int | None
    overages: int
    guest_meals: int

    @property
    def remaining(self) -> int | None:
        if self.allotment is None:
            return None
        return max(0, self.allotment - self.used)

    @property
    def over_by(self) -> int:
        if self.allotment is None:
            return 0
        return max(0, self.used - self.allotment)


def daily_attendance(db: Session, day: date) -> list[Attendance]:
    stmt = (
        select(Attendance)
        .where(Attendance.service_date == day, Attendance.voided_at.is_(None))
        .order_by(Attendance.scanned_at.desc())
    )
    return list(db.scalars(stmt))


def daily_counts_by_period(db: Session, day: date) -> list[tuple[str, int, int, int]]:
    """(period name, member meals, guest meals, alumni meals) for one day.

    Alumni meals are counted separately rather than folded into either column:
    they are neither a member eating their plan nor a guest spending a member's
    benefit, and adding them to one of those totals would quietly overstate it.
    """
    rows = (
        db.query(
            MealPeriod.name,
            func.sum(
                case((Attendance.kind == AttendanceKind.MEMBER.value, 1), else_=0)
            ).label("members"),
            func.sum(
                case((Attendance.kind == AttendanceKind.GUEST.value, 1), else_=0)
            ).label("guests"),
            func.sum(
                case((Attendance.kind == AttendanceKind.ALUMNI.value, 1), else_=0)
            ).label("alumni"),
        )
        .join(Attendance, Attendance.meal_period_id == MealPeriod.id)
        .filter(Attendance.service_date == day, Attendance.voided_at.is_(None))
        .group_by(MealPeriod.id, MealPeriod.name, MealPeriod.sort_order)
        .order_by(MealPeriod.sort_order)
        .all()
    )
    return [
        (name, int(members or 0), int(guests or 0), int(alumni or 0))
        for name, members, guests, alumni in rows
    ]


def weekly_usage_report(db: Session, anchor: date, config: ClubConfig) -> list[MemberWeek]:
    """Per-member usage against allotment for the week containing `anchor`.

    Includes members with zero meals — "who is not eating here" is as useful to
    the club as who is.
    """
    start, end = week_bounds(anchor, config.week_start_day)

    counts = dict(
        db.query(Attendance.member_id, func.count(Attendance.id))
        .join(MealPeriod, Attendance.meal_period_id == MealPeriod.id, isouter=True)
        .filter(
            Attendance.kind == AttendanceKind.MEMBER.value,
            Attendance.voided_at.is_(None),
            Attendance.service_date >= start,
            Attendance.service_date <= end,
            (MealPeriod.counts_toward_allotment.is_(True))
            | (Attendance.meal_period_id.is_(None)),
        )
        .group_by(Attendance.member_id)
        .all()
    )
    overages = dict(
        db.query(Attendance.member_id, func.count(Attendance.id))
        .filter(
            Attendance.kind == AttendanceKind.MEMBER.value,
            Attendance.voided_at.is_(None),
            Attendance.is_overage.is_(True),
            Attendance.service_date >= start,
            Attendance.service_date <= end,
        )
        .group_by(Attendance.member_id)
        .all()
    )
    guests = dict(
        db.query(Attendance.member_id, func.count(Attendance.id))
        .filter(
            Attendance.kind == AttendanceKind.GUEST.value,
            Attendance.voided_at.is_(None),
            Attendance.service_date >= start,
            Attendance.service_date <= end,
        )
        .group_by(Attendance.member_id)
        .all()
    )

    members = list(db.scalars(select(Member).order_by(Member.last_name, Member.first_name)))
    return [
        MemberWeek(
            member=m,
            used=counts.get(m.id, 0),
            allotment=config.allotment_for(m.plan_type),
            overages=overages.get(m.id, 0),
            guest_meals=guests.get(m.id, 0),
        )
        for m in members
    ]


def monthly_overages(db: Session, start: date, end: date) -> list[tuple[Member, int]]:
    """The billing list: who ate past their plan, and how many times."""
    rows = (
        db.query(Member, func.count(Attendance.id))
        .join(Attendance, Attendance.member_id == Member.id)
        .filter(
            Attendance.is_overage.is_(True),
            Attendance.voided_at.is_(None),
            Attendance.service_date >= start,
            Attendance.service_date <= end,
        )
        .group_by(Member.id)
        .order_by(func.count(Attendance.id).desc())
        .all()
    )
    return [(member, int(count)) for member, count in rows]


def guest_usage_report(db: Session, start: date, end: date) -> list[tuple[Member, int]]:
    rows = (
        db.query(Member, func.count(Attendance.id))
        .join(Attendance, Attendance.member_id == Member.id)
        .filter(
            Attendance.kind == AttendanceKind.GUEST.value,
            Attendance.voided_at.is_(None),
            Attendance.service_date >= start,
            Attendance.service_date <= end,
        )
        .group_by(Member.id)
        .order_by(func.count(Attendance.id).desc())
        .all()
    )
    return [(member, int(count)) for member, count in rows]


def enrollment_gaps(db: Session) -> list[Member]:
    """Active members with no live card.

    This is the "who still needs to tap in" list staff work through in week one,
    and afterwards it surfaces anyone whose card was lost and never replaced.
    """
    enrolled = select(Credential.member_id).where(Credential.is_active.is_(True))
    stmt = (
        select(Member)
        .where(Member.status == "active", Member.id.not_in(enrolled))
        .order_by(Member.last_name, Member.first_name)
    )
    return list(db.scalars(stmt))


# --------------------------------------------------------------------------
# Member analytics
#
# The reports above answer "who owes what this week". These answer "how is the
# club actually being used" — over an arbitrary window, sliceable by class year,
# plan and status. Everything is assembled from a handful of grouped queries
# keyed by member_id rather than a query per member, so a 300-member club costs
# the same six round trips as a 30-member one.
# --------------------------------------------------------------------------


@dataclass
class MemberStats:
    member: Member
    meals: int  # member meals eaten, voided rows excluded
    counted_meals: int  # of those, the ones that draw down the allotment
    guest_meals: int  # guests they hosted
    overages: int
    days_attended: int  # distinct service dates they showed up on
    last_seen: date | None
    has_card: bool
    allotment: int | None
    span_days: int

    @property
    def meals_per_week(self) -> float:
        return round(self.counted_meals / (max(self.span_days, 1) / 7), 1)

    @property
    def utilization(self) -> int | None:
        """Share of the weekly plan actually eaten, as a percentage.

        None for members with no plan — there is no denominator, and showing 0%
        would read as "never eats" rather than "pays per meal".
        """
        if not self.allotment:
            return None
        return round(100 * self.meals_per_week / self.allotment)

    @property
    def is_dormant(self) -> bool:
        """On an active plan but never turned up in the window.

        Worth surfacing: it is either someone who has quietly stopped eating
        here, or a card that was never enrolled properly.
        """
        return self.meals == 0 and self.member.status == "active"


@dataclass
class GroupStats:
    """One row of a breakdown — by class year, plan or status."""

    label: str
    members: int
    ate: int  # members with at least one meal in the window
    meals: int
    guest_meals: int
    overages: int

    @property
    def meals_per_member(self) -> float:
        return round(self.meals / self.members, 1) if self.members else 0.0

    @property
    def participation(self) -> int:
        """Percentage of the group that ate at all in the window."""
        return round(100 * self.ate / self.members) if self.members else 0


def _as_date(value) -> date | None:
    """SQLite hands back a string for some aggregates; Postgres a date."""
    if value is None or isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:  # pragma: no cover - defensive
        return None


def member_analytics(
    db: Session, start: date, end: date, config: ClubConfig
) -> list[MemberStats]:
    """Per-member usage across an arbitrary window.

    Includes members with zero meals, for the same reason weekly_usage_report
    does: who is *not* eating here is half the question.
    """
    span_days = max((end - start).days + 1, 1)

    def in_window(*extra):
        return (
            Attendance.voided_at.is_(None),
            Attendance.service_date >= start,
            Attendance.service_date <= end,
            *extra,
        )

    member_meals = dict(
        db.query(Attendance.member_id, func.count(Attendance.id))
        .filter(*in_window(Attendance.kind == AttendanceKind.MEMBER.value))
        .group_by(Attendance.member_id)
        .all()
    )
    counted_meals = dict(
        db.query(Attendance.member_id, func.count(Attendance.id))
        .join(MealPeriod, Attendance.meal_period_id == MealPeriod.id, isouter=True)
        .filter(
            *in_window(
                Attendance.kind == AttendanceKind.MEMBER.value,
                (MealPeriod.counts_toward_allotment.is_(True))
                | (Attendance.meal_period_id.is_(None)),
            )
        )
        .group_by(Attendance.member_id)
        .all()
    )
    guest_meals = dict(
        db.query(Attendance.member_id, func.count(Attendance.id))
        .filter(*in_window(Attendance.kind == AttendanceKind.GUEST.value))
        .group_by(Attendance.member_id)
        .all()
    )
    overages = dict(
        db.query(Attendance.member_id, func.count(Attendance.id))
        .filter(
            *in_window(
                Attendance.kind == AttendanceKind.MEMBER.value,
                Attendance.is_overage.is_(True),
            )
        )
        .group_by(Attendance.member_id)
        .all()
    )
    attendance_shape = dict(
        (member_id, (int(days or 0), _as_date(last)))
        for member_id, days, last in db.query(
            Attendance.member_id,
            func.count(func.distinct(Attendance.service_date)),
            func.max(Attendance.service_date),
        )
        .filter(*in_window(Attendance.kind == AttendanceKind.MEMBER.value))
        .group_by(Attendance.member_id)
        .all()
    )
    carded = {
        row.member_id
        for row in db.scalars(select(Credential).where(Credential.is_active.is_(True)))
    }

    members = list(db.scalars(select(Member).order_by(Member.last_name, Member.first_name)))
    return [
        MemberStats(
            member=m,
            meals=member_meals.get(m.id, 0),
            counted_meals=counted_meals.get(m.id, 0),
            guest_meals=guest_meals.get(m.id, 0),
            overages=overages.get(m.id, 0),
            days_attended=attendance_shape.get(m.id, (0, None))[0],
            last_seen=attendance_shape.get(m.id, (0, None))[1],
            has_card=m.id in carded,
            allotment=config.allotment_for(m.plan_type),
            span_days=span_days,
        )
        for m in members
    ]


def group_stats(rows: list[MemberStats], key) -> list[GroupStats]:
    """Roll member stats up by whatever `key(stats) -> (sort_key, label)` says."""
    buckets: dict[str, list] = {}
    order: dict[str, object] = {}
    for row in rows:
        sort_key, label = key(row)
        buckets.setdefault(label, []).append(row)
        order[label] = sort_key

    return [
        GroupStats(
            label=label,
            members=len(group),
            ate=sum(1 for r in group if r.meals),
            meals=sum(r.meals for r in group),
            guest_meals=sum(r.guest_meals for r in group),
            overages=sum(r.overages for r in group),
        )
        for label, group in sorted(buckets.items(), key=lambda item: order[item[0]])
    ]


def by_class_year(rows: list[MemberStats]) -> list[GroupStats]:
    # Sort newest class first, with "no year on file" pushed to the end rather
    # than sorting as year zero.
    return group_stats(
        rows,
        lambda r: (
            (1, 0) if r.member.class_year is None else (0, -r.member.class_year),
            str(r.member.class_year) if r.member.class_year else "No year on file",
        ),
    )


def by_plan(rows: list[MemberStats]) -> list[GroupStats]:
    order = {"plan_19": 0, "plan_14": 1, "rca_paa": 2, "none": 3}
    return group_stats(
        rows, lambda r: (order.get(r.member.plan_type, 9), r.member.plan_type)
    )


def by_status(rows: list[MemberStats]) -> list[GroupStats]:
    return group_stats(rows, lambda r: (r.member.status, r.member.status))


def to_csv(header: list[str], rows: list[list]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue()
