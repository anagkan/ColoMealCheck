"""Date and window resolution — the layer DST bugs would live in."""
from __future__ import annotations

from datetime import date, time

import pytest

from app.models import MealPeriod
from app.services.periods import (
    month_bounds,
    nearest_period,
    next_period,
    resolve_period,
    seconds_remaining,
    week_bounds,
    weekly_meal_capacity,
)
from tests.conftest import eastern

# 2026-01-05 is a Monday.
MONDAY = date(2026, 1, 5)


def test_default_schedule_offers_nineteen_meals(db):
    """The 19-meal plan means 'every meal we serve'. If this number moves, the
    seeded schedule is wrong and both plans are quietly mis-sized."""
    assert weekly_meal_capacity(db) == 19


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [
        (8, 0, "Breakfast"),    # exactly at open
        (9, 59, "Breakfast"),
        (10, 0, "Breakfast"),   # exactly at close
        (11, 45, "Lunch"),      # exactly at open
        (13, 44, "Lunch"),
        (13, 45, "Lunch"),      # exactly at close
        (17, 45, "Dinner"),     # exactly at open
        (19, 45, "Dinner"),     # exactly at close
    ],
)
def test_weekday_window_edges(db, hour, minute, expected):
    resolved = resolve_period(db, eastern(2026, 1, 5, hour, minute))
    assert resolved.period is not None
    assert resolved.period.name == expected
    assert resolved.service_date == MONDAY


@pytest.mark.parametrize(
    ("hour", "minute"),
    [
        (6, 0),
        (7, 59),    # a minute before breakfast opens
        (10, 30),
        (11, 44),   # a minute before lunch opens
        (13, 46),   # a minute after lunch closes
        (15, 0),
        (17, 44),   # a minute before dinner opens
        (19, 46),   # a minute after dinner closes
        (21, 0),
    ],
)
def test_between_services_resolves_to_no_period(db, hour, minute):
    resolved = resolve_period(db, eastern(2026, 1, 5, hour, minute))
    assert resolved.period is None
    assert resolved.service_date == MONDAY


def test_weekend_serves_brunch_not_breakfast(db):
    saturday = resolve_period(db, eastern(2026, 1, 10, 12, 0))
    assert saturday.period.name == "Brunch"

    # 08:00 Saturday is breakfast on a weekday but nothing on a weekend.
    assert resolve_period(db, eastern(2026, 1, 10, 8, 0)).period is None


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [(11, 30, "Brunch"), (13, 30, "Brunch"), (17, 45, "Dinner"), (19, 45, "Dinner")],
)
def test_weekend_window_edges(db, hour, minute, expected):
    resolved = resolve_period(db, eastern(2026, 1, 10, hour, minute))
    assert resolved.period is not None
    assert resolved.period.name == expected


def test_weekend_lunch_window_is_brunch_only(db):
    """Weekday lunch opens at 11:45; on a weekend that time is inside brunch,
    and brunch has already closed by 13:45 when weekday lunch would end."""
    assert resolve_period(db, eastern(2026, 1, 10, 11, 45)).period.name == "Brunch"
    assert resolve_period(db, eastern(2026, 1, 10, 13, 40)).period is None


def test_fall_back_evening_uses_local_date_not_utc_date(db):
    """2026-11-01 19:00 EST is 2026-11-02 00:00 UTC.

    Deriving the service date from the UTC timestamp would book this Sunday
    dinner against Monday — a day with no evening service — and the member
    would be told nothing is being served while they stand holding a tray.
    """
    resolved = resolve_period(db, eastern(2026, 11, 1, 19, 0))
    assert resolved.service_date == date(2026, 11, 1)
    assert resolved.period.name == "Dinner"


def test_spring_forward_evening_uses_local_date_not_utc_date(db):
    """2026-03-07 19:30 EST is 2026-03-08 00:30 UTC — same trap, other side of
    the year, and with the offset that changes the next morning."""
    resolved = resolve_period(db, eastern(2026, 3, 7, 19, 30))
    assert resolved.service_date == date(2026, 3, 7)
    assert resolved.period.name == "Dinner"


def test_offsets_differ_across_the_spring_transition(db):
    """Sanity-check the fixture itself: these two dinners really are on
    different UTC offsets, so the tests above are exercising the transition."""
    before = eastern(2026, 3, 7, 18, 0)
    after = eastern(2026, 3, 9, 18, 0)
    assert before.utcoffset() != after.utcoffset()
    assert resolve_period(db, before).period.name == "Dinner"
    assert resolve_period(db, after).period.name == "Dinner"


def test_window_past_midnight_belongs_to_the_day_it_opened(db):
    """A late meal is still that evening's meal at 00:30."""
    db.add(
        MealPeriod(
            name="Late Meal",
            weekday=4,  # Friday
            start_time=time(22, 0),
            end_time=time(1, 0),
            counts_toward_allotment=True,
            is_active=True,
            sort_order=40,
        )
    )
    db.commit()

    friday_night = resolve_period(db, eastern(2026, 1, 9, 23, 30))
    assert friday_night.period.name == "Late Meal"
    assert friday_night.service_date == date(2026, 1, 9)

    after_midnight = resolve_period(db, eastern(2026, 1, 10, 0, 30))
    assert after_midnight.period.name == "Late Meal"
    assert after_midnight.service_date == date(2026, 1, 9)  # Friday, not Saturday


def test_nearest_period_handles_arriving_just_after_close(db):
    got = nearest_period(db, eastern(2026, 1, 5, 20, 20))
    assert got is not None and got.name == "Dinner"


class TestNextPeriod:
    """Feeds the kiosk banner's 'Next Meal: X in H:MM:SS' line."""

    def test_it_finds_the_next_window_later_the_same_day(self, db):
        # 10:30 Monday: breakfast has closed, lunch opens at 11:45.
        upcoming = next_period(db, eastern(2026, 1, 5, 10, 30))
        assert upcoming is not None
        assert upcoming.period.name == "Lunch"
        assert upcoming.seconds_until == 75 * 60

    def test_it_rolls_over_to_the_next_day(self, db):
        # 21:00 Monday: nothing left today, breakfast opens 08:00 Tuesday.
        upcoming = next_period(db, eastern(2026, 1, 5, 21, 0))
        assert upcoming.period.name == "Breakfast"
        assert upcoming.seconds_until == 11 * 60 * 60

    def test_it_rolls_across_the_weekend_boundary(self, db):
        """After Sunday dinner the next meal is Monday breakfast — not brunch,
        which is what a naive 'same weekday' lookup would return."""
        upcoming = next_period(db, eastern(2026, 1, 11, 20, 0))  # Sunday
        assert upcoming.period.name == "Breakfast"

    def test_saturday_night_leads_to_brunch_not_breakfast(self, db):
        upcoming = next_period(db, eastern(2026, 1, 10, 20, 0))  # Saturday
        assert upcoming.period.name == "Brunch"

    def test_during_a_meal_it_reports_the_one_after(self, db):
        upcoming = next_period(db, eastern(2026, 1, 5, 12, 0))  # inside Lunch
        assert upcoming.period.name == "Dinner"

    def test_it_is_none_when_nothing_is_scheduled(self, db):
        db.query(MealPeriod).delete()
        db.commit()
        assert next_period(db, eastern(2026, 1, 5, 12, 0)) is None

    def test_it_ignores_inactive_windows(self, db):
        for row in db.query(MealPeriod).filter(MealPeriod.name == "Lunch").all():
            row.is_active = False
        db.commit()
        upcoming = next_period(db, eastern(2026, 1, 5, 10, 30))
        assert upcoming.period.name == "Dinner"

    def test_it_orders_by_clock_time_not_sort_order(self, db):
        """sort_order is a display preference. A window mis-ordered against the
        timetable must not become the 'next' meal ahead of an earlier one."""
        db.add(
            MealPeriod(
                name="Late Snack",
                weekday=0,
                start_time=time(21, 0),
                end_time=time(22, 0),
                counts_toward_allotment=False,
                is_active=True,
                sort_order=1,  # sorts first despite opening last
            )
        )
        db.commit()
        assert next_period(db, eastern(2026, 1, 5, 10, 30)).period.name == "Lunch"


class TestSecondsRemaining:
    """Feeds the kiosk banner's countdown."""

    def test_it_counts_down_within_the_window(self, db):
        # Lunch closes at 13:45; at 13:00 that is 45 minutes away.
        resolved = resolve_period(db, eastern(2026, 1, 5, 13, 0))
        assert seconds_remaining(resolved) == 45 * 60

    def test_it_is_the_full_window_at_the_moment_of_opening(self, db):
        resolved = resolve_period(db, eastern(2026, 1, 5, 11, 45))
        assert seconds_remaining(resolved) == 2 * 60 * 60

    def test_it_is_zero_at_the_close(self, db):
        resolved = resolve_period(db, eastern(2026, 1, 5, 13, 45))
        assert seconds_remaining(resolved) == 0

    def test_it_is_none_outside_service(self, db):
        resolved = resolve_period(db, eastern(2026, 1, 5, 15, 0))
        assert resolved.period is None
        assert seconds_remaining(resolved) is None

    def test_a_window_past_midnight_counts_into_the_next_day(self, db):
        """The close is on the day *after* the service date here. Combining the
        end time with the service date would return a negative duration and the
        banner would read 0:00 for three hours."""
        db.add(
            MealPeriod(
                name="Late Meal",
                weekday=4,  # Friday
                start_time=time(22, 0),
                end_time=time(1, 0),
                counts_toward_allotment=True,
                is_active=True,
                sort_order=40,
            )
        )
        db.commit()

        resolved = resolve_period(db, eastern(2026, 1, 9, 23, 30))
        assert resolved.service_date == date(2026, 1, 9)
        assert seconds_remaining(resolved) == 90 * 60  # 23:30 -> 01:00

        after_midnight = resolve_period(db, eastern(2026, 1, 10, 0, 30))
        assert seconds_remaining(after_midnight) == 30 * 60

    def test_it_survives_the_spring_forward_transition(self, db):
        """2026-03-08 02:00 EST becomes 03:00 EDT. A window running across it
        loses an hour of wall clock, and the countdown must agree — this is the
        one case where naive time arithmetic would be off by 3600."""
        db.add(
            MealPeriod(
                name="Overnight",
                weekday=5,  # Saturday
                start_time=time(23, 0),
                end_time=time(4, 0),
                counts_toward_allotment=False,
                is_active=True,
                sort_order=50,
            )
        )
        db.commit()

        # The clock face reads 01:00 -> 04:00, three hours. But 02:00 never
        # happens, so only two real hours remain and that is what the member
        # standing at the kiosk actually gets.
        resolved = resolve_period(db, eastern(2026, 3, 8, 1, 0))
        assert resolved.period is not None
        assert seconds_remaining(resolved) == 2 * 60 * 60


class TestWeekBounds:
    def test_week_starts_monday(self):
        start, end = week_bounds(date(2026, 1, 7), week_start_day=0)
        assert start == date(2026, 1, 5)
        assert end == date(2026, 1, 11)

    def test_sunday_is_the_last_day_of_its_week(self):
        start, _ = week_bounds(date(2026, 1, 11), week_start_day=0)
        assert start == date(2026, 1, 5)

    def test_sunday_dinner_and_monday_breakfast_are_different_weeks(self):
        """The boundary members will actually notice: eating Sunday night must
        not consume the allotment that opens Monday morning."""
        sunday, _ = week_bounds(date(2026, 1, 11), week_start_day=0)
        monday, _ = week_bounds(date(2026, 1, 12), week_start_day=0)
        assert sunday != monday

    def test_week_start_day_is_configurable(self):
        start, end = week_bounds(date(2026, 1, 7), week_start_day=6)  # Sunday
        assert start == date(2026, 1, 4)
        assert end == date(2026, 1, 10)


class TestMonthBounds:
    def test_ordinary_month(self):
        assert month_bounds(date(2026, 5, 17)) == (date(2026, 5, 1), date(2026, 5, 31))

    def test_february(self):
        assert month_bounds(date(2026, 2, 10)) == (date(2026, 2, 1), date(2026, 2, 28))

    def test_december_rolls_the_year(self):
        assert month_bounds(date(2026, 12, 25)) == (date(2026, 12, 1), date(2026, 12, 31))
