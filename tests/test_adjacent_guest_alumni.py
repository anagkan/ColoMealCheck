"""Guest and alumni meals use the same adjacent service windows as members."""
from datetime import date

import pytest

from app.models import Attendance, AuditLog, MealPeriod
from app.services.scan import ScanOutcome, record_alumni_meal, record_guest
from tests.conftest import eastern


@pytest.fixture(params=["guest", "alumni"])
def record_meal(request, db, make_member):
    if request.param == "guest":
        host = make_member()
        return lambda **kwargs: record_guest(db, host, "Sam", "Ortiz", "sortiz", **kwargs)
    return lambda **kwargs: record_alumni_meal(
        db, "Casey", "Whitman", class_year=2014, email="casey@example.com", **kwargs
    )


def test_closed_service_offers_meals_without_recording(db, record_meal):
    result = record_meal(moment=eastern(2026, 1, 5, 15, 30))
    assert result.outcome is ScanOutcome.OUTSIDE_SERVICE
    assert [(offer.direction, offer.period.name) for offer in result.offers] == [
        ("previous", "Lunch"), ("next", "Dinner")
    ]
    assert db.query(Attendance).count() == 0


@pytest.mark.parametrize("direction, name", [("previous", "Lunch"), ("next", "Dinner")])
def test_choice_records_and_audits_the_selected_meal(db, record_meal, direction, name):
    moment = eastern(2026, 1, 5, 15, 30)
    result = record_meal(moment=moment, attach=direction)
    assert result.ok
    assert result.period.name == name
    assert result.attendance.service_date == date(2026, 1, 5)
    assert result.attendance.scanned_at.replace(tzinfo=None) == moment.replace(tzinfo=None)
    assert result.attendance.override_by is None
    assert name in result.warnings[0]
    audit = db.query(AuditLog).filter_by(action="attendance.outside_service").one()
    assert audit.entity_id == result.attendance.id
    assert audit.detail["direction"] == direction
    assert audit.detail["period"] == name


@pytest.mark.parametrize("direction", ["previous", "next"])
def test_open_service_takes_precedence(db, record_meal, direction):
    result = record_meal(moment=eastern(2026, 1, 5, 12), attach=direction)
    assert result.ok
    assert result.period.name == "Lunch"
    assert result.warnings == []
    assert db.query(AuditLog).filter_by(action="attendance.outside_service").count() == 0


@pytest.mark.parametrize("direction", [None, "previous", "next", "invalid"])
def test_empty_schedule_cannot_record_a_meal(db, record_meal, direction):
    db.query(MealPeriod).delete()
    db.commit()
    result = record_meal(moment=eastern(2026, 1, 5, 15, 30), attach=direction)
    assert result.outcome is ScanOutcome.OUTSIDE_SERVICE
    assert result.offers == []
    assert db.query(Attendance).count() == 0


@pytest.mark.parametrize("moment, direction, expected", [
    (eastern(2026, 2, 1, 0, 30), "previous", date(2026, 1, 31)),
    (eastern(2026, 1, 31, 23), "next", date(2026, 2, 1)),
])
def test_service_date_follows_the_chosen_meal(record_meal, moment, direction, expected):
    result = record_meal(moment=moment, attach=direction)
    assert result.ok
    assert result.attendance.service_date == expected


def test_guest_quota_uses_the_selected_month_and_still_requires_override(db, make_member):
    host = make_member()
    for _ in range(2):
        record_guest(db, host, "Sam", "Ortiz", "sortiz", moment=eastern(2026, 1, 31, 18))
    late = dict(moment=eastern(2026, 2, 1, 0, 30), attach="previous")
    blocked = record_guest(db, host, "Sam", "Ortiz", "sortiz", **late)
    assert blocked.outcome is ScanOutcome.GUEST_QUOTA_EXCEEDED
    assert blocked.guests.month_start == date(2026, 1, 1)
    allowed = record_guest(
        db, host, "Sam", "Ortiz", "sortiz", **late,
        override_by="staff:jo", override_reason="Approved guest",
    )
    assert allowed.ok
    assert allowed.attendance.service_date == date(2026, 1, 31)
    assert allowed.attendance.override_by == "staff:jo"
    assert allowed.guests.used == 3
    early = record_guest(
        db, host, "Sam", "Ortiz", "sortiz",
        moment=eastern(2026, 1, 31, 23), attach="next",
    )
    assert early.ok
    assert early.guests.month_start == date(2026, 2, 1)
    assert early.guests.used == 1


@pytest.mark.parametrize("category", ["guest_is_family", "guest_is_professor"])
def test_exempt_guest_can_choose_a_meal_with_an_exhausted_quota(db, make_member, category):
    host = make_member()
    for _ in range(2):
        record_guest(db, host, "Sam", "Ortiz", "sortiz", moment=eastern(2026, 1, 5, 12))
    result = record_guest(
        db, host, "Sam", "Ortiz", moment=eastern(2026, 1, 5, 15, 30),
        attach="next", **{category: True},
    )
    assert result.ok
    assert result.guests.used == 2
    assert result.attendance.override_by is None
