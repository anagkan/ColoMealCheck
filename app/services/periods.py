"""Turning a wall-clock instant into (service_date, meal_period).

Everything downstream keys off `service_date` — the club's operating day in
local time — rather than a UTC timestamp range. That is what makes the twice-
yearly DST shift a non-event: on the November night when 01:30 EDT happens
twice, both instants convert to the same local date and the same dinner window,
so they collapse into one meal rather than two.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import MealPeriod


@dataclass(frozen=True)
class ResolvedPeriod:
    service_date: date
    period: MealPeriod | None
    local_time: datetime


def local_tz() -> ZoneInfo:
    return get_settings().tz


def to_local(moment: datetime, tz: ZoneInfo | None = None) -> datetime:
    """Convert an instant to club-local time.

    A naive datetime is treated as already local — the kiosk never sends one,
    but tests and admin back-fills are more readable when they can.
    """
    tz = tz or local_tz()
    if moment.tzinfo is None:
        return moment.replace(tzinfo=tz)
    return moment.astimezone(tz)


def _wraps_midnight(period: MealPeriod) -> bool:
    return period.end_time < period.start_time


def _active_periods(db: Session, weekday: int) -> list[MealPeriod]:
    stmt = (
        select(MealPeriod)
        .where(MealPeriod.weekday == weekday, MealPeriod.is_active.is_(True))
        .order_by(MealPeriod.sort_order, MealPeriod.start_time)
    )
    return list(db.scalars(stmt))


def resolve_period(
    db: Session, moment: datetime, tz: ZoneInfo | None = None
) -> ResolvedPeriod:
    """Find the serving window containing `moment`.

    Returns the period and the service_date it belongs to. A window whose
    end_time is before its start_time (say, a 22:00-01:00 late meal) belongs to
    the day it *started* on, so a 00:30 scan is booked against the previous
    service date.
    """
    tz = tz or local_tz()
    local = to_local(moment, tz)
    today = local.date()
    now = local.time()

    for period in _active_periods(db, today.weekday()):
        if _wraps_midnight(period):
            if now >= period.start_time:
                return ResolvedPeriod(today, period, local)
        elif period.start_time <= now <= period.end_time:
            return ResolvedPeriod(today, period, local)

    # Nothing today — check whether we are in the tail of a window that opened
    # yesterday and ran past midnight.
    yesterday = today - timedelta(days=1)
    for period in _active_periods(db, yesterday.weekday()):
        if _wraps_midnight(period) and now <= period.end_time:
            return ResolvedPeriod(yesterday, period, local)

    return ResolvedPeriod(today, None, local)


def periods_for_day(db: Session, day: date) -> list[MealPeriod]:
    return _active_periods(db, day.weekday())


@dataclass(frozen=True)
class UpcomingPeriod:
    period: MealPeriod
    service_date: date
    starts_at: datetime
    seconds_until: int


def next_period(
    db: Session, moment: datetime, tz: ZoneInfo | None = None
) -> UpcomingPeriod | None:
    """The next window to open after `moment`, for the kiosk's closed banner.

    Looks a full week ahead rather than only at today: after Sunday dinner the
    next meal is Monday breakfast, and a club that stopped serving on some day
    still has a next meal — it is just further out. Returns None only when the
    schedule holds no active windows at all.
    """
    tz = tz or local_tz()
    local = to_local(moment, tz)

    for offset in range(8):
        day = local.date() + timedelta(days=offset)
        # Ordered by clock time, not sort_order: sort_order is a display
        # preference and nothing stops it disagreeing with the timetable.
        for period in sorted(_active_periods(db, day.weekday()), key=lambda p: p.start_time):
            starts_at = datetime.combine(day, period.start_time, tzinfo=tz)
            if starts_at <= local:
                continue
            delta = starts_at.astimezone(timezone.utc) - local.astimezone(timezone.utc)
            return UpcomingPeriod(period, day, starts_at, max(0, int(delta.total_seconds())))
    return None


# The two sides of a moment that falls between meals. Strings rather than an
# enum because they cross the wire to the kiosk and back again.
PREVIOUS = "previous"
NEXT = "next"


@dataclass(frozen=True)
class AdjacentPeriod:
    """A closed window offered to somebody standing at the kiosk between meals.

    Carries its own service_date because it need not be today's: at eight on a
    Monday morning the meal that just closed is Sunday dinner, and a check-in
    attached to it belongs to Sunday's service day and Sunday's meal week.
    """

    direction: str  # PREVIOUS or NEXT
    period: MealPeriod
    service_date: date
    seconds_away: int  # since it closed, or until it opens


def previous_period(
    db: Session, moment: datetime, tz: ZoneInfo | None = None
) -> AdjacentPeriod | None:
    """The last window to close before `moment` — next_period's mirror image.

    Not the same thing as nearest_period below: this one only ever looks
    backwards, and it searches the same full week forwards that next_period
    does, so the answer on a Monday morning is Sunday's dinner rather than
    nothing.

    A window that wraps midnight closes on the day *after* the day it is listed
    under, so the day it is listed under is not enough to order two windows by:
    a Friday late meal ending at 01:00 closes after a Saturday window that ended
    at 00:30. That is why the search keeps going for one day past the first hit
    rather than returning it — no window reaches further back than that.
    """
    tz = tz or local_tz()
    local = to_local(moment, tz)
    best: AdjacentPeriod | None = None
    best_end: datetime | None = None

    for offset in range(8):
        day = local.date() - timedelta(days=offset)
        for period in _active_periods(db, day.weekday()):
            end_day = day + timedelta(days=1) if _wraps_midnight(period) else day
            ends_at = datetime.combine(end_day, period.end_time, tzinfo=tz)
            if ends_at >= local:
                continue
            if best_end is not None and ends_at <= best_end:
                continue
            # Both sides to UTC before subtracting, for the reason
            # seconds_remaining gives: a gap spanning a DST change is not the
            # gap the clock face shows.
            delta = local.astimezone(timezone.utc) - ends_at.astimezone(timezone.utc)
            best = AdjacentPeriod(PREVIOUS, period, day, max(0, int(delta.total_seconds())))
            best_end = ends_at
        if best is not None and best.service_date != day:
            return best
    return best


def adjacent_period(
    db: Session, moment: datetime, direction: str, tz: ZoneInfo | None = None
) -> AdjacentPeriod | None:
    """The window on one side of `moment`, named so the kiosk can offer it.

    A member who is five minutes early for dinner, or five minutes late off
    lunch, should be able to eat rather than be sent away to come back — so
    between meals the kiosk names both neighbouring windows and lets them pick
    one. This resolves that pick; services/scan.py decides whether to honour it.
    """
    if direction == NEXT:
        upcoming = next_period(db, moment, tz)
        if upcoming is None:
            return None
        return AdjacentPeriod(
            NEXT, upcoming.period, upcoming.service_date, upcoming.seconds_until
        )
    if direction == PREVIOUS:
        return previous_period(db, moment, tz)
    return None


def adjacent_periods(
    db: Session, moment: datetime, tz: ZoneInfo | None = None
) -> list[AdjacentPeriod]:
    """Both windows a between-meals check-in could be attached to.

    Either may be missing — a brand-new schedule has no previous meal — and the
    kiosk shows a button per window it is actually given.
    """
    found = (adjacent_period(db, moment, direction, tz) for direction in (PREVIOUS, NEXT))
    return [item for item in found if item is not None]


def period_ends_at(resolved: ResolvedPeriod) -> datetime | None:
    """The local instant the resolved window closes.

    A window that wraps midnight closes on the day *after* its service date,
    which is the whole reason this is not just `combine(service_date, end)`.
    """
    period = resolved.period
    if period is None:
        return None
    end_day = resolved.service_date
    if _wraps_midnight(period):
        end_day = end_day + timedelta(days=1)
    return datetime.combine(end_day, period.end_time, tzinfo=resolved.local_time.tzinfo)


def seconds_remaining(resolved: ResolvedPeriod) -> int | None:
    """How much of the current window is left, or None outside service.

    The kiosk counts down from this rather than from an end timestamp on
    purpose: a laptop with a wrong clock would render an absolute end time as
    nonsense, but a duration is immune to clock skew. The same distrust of the
    kiosk clock guards replayed scans in routers/api_scan.py.
    """
    ends_at = period_ends_at(resolved)
    if ends_at is None:
        return None
    # Both sides go to UTC first. Subtracting two aware datetimes that share a
    # tzinfo makes Python ignore the offset and subtract the wall clocks, so a
    # window running across the spring-forward would report the three hours the
    # clock face moved rather than the two hours actually left.
    elapsed = ends_at.astimezone(timezone.utc) - resolved.local_time.astimezone(timezone.utc)
    return max(0, int(elapsed.total_seconds()))


def nearest_period(db: Session, moment: datetime, tz: ZoneInfo | None = None) -> MealPeriod | None:
    """The closest window on the same day, for the 'attach this scan anyway'
    path staff use when someone arrives just after service closes."""
    tz = tz or local_tz()
    local = to_local(moment, tz)
    candidates = _active_periods(db, local.date().weekday())
    if not candidates:
        return None

    def distance(period: MealPeriod) -> int:
        start = _minutes(period.start_time)
        end = _minutes(period.end_time)
        current = _minutes(local.time())
        if start <= current <= end:
            return 0
        return min(abs(current - start), abs(current - end))

    return min(candidates, key=distance)


def _minutes(value: time) -> int:
    return value.hour * 60 + value.minute


def week_bounds(day: date, week_start_day: int) -> tuple[date, date]:
    """Inclusive [start, end] of the meal week containing `day`.

    week_start_day follows date.weekday(): 0=Monday. Colonial's week starts
    Monday, so a Sunday dinner is the *last* meal of its week and the following
    Monday breakfast opens a fresh allotment.
    """
    offset = (day.weekday() - week_start_day) % 7
    start = day - timedelta(days=offset)
    return start, start + timedelta(days=6)


def month_bounds(day: date) -> tuple[date, date]:
    """Inclusive [first, last] of the calendar month containing `day` — the
    window the 2-guest-meal benefit resets on."""
    start = day.replace(day=1)
    if start.month == 12:
        next_month = start.replace(year=start.year + 1, month=1)
    else:
        next_month = start.replace(month=start.month + 1)
    return start, next_month - timedelta(days=1)


def weekly_meal_capacity(db: Session) -> int:
    """How many servings the club actually offers in a week.

    Shown in the admin schedule editor so a mistyped window is obvious: if this
    stops reading 19, someone broke the schedule.
    """
    stmt = select(MealPeriod).where(
        MealPeriod.is_active.is_(True), MealPeriod.counts_toward_allotment.is_(True)
    )
    return len(list(db.scalars(stmt)))
