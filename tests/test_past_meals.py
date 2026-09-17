"""Past-meal registration through Admin, including historical accounting rules."""
from datetime import date, time

import pytest

from app.models import Attendance, AuditLog, MealPeriod, Member, PlanType, StaffRole
from app.services import past_meals, reports
from app.services.allotment import weekly_usage
from app.services.club_settings import load_config, set_value
from app.services.guests import guest_usage
from app.services.periods import periods_for_day
from app.services.scan import void_attendance
from tests.conftest import eastern
from tests.helpers import grant_meals, guest_rows
from tests.test_api import admin_user, client, signed_in  # noqa: F401


@pytest.fixture
def values(db, make_member):
    member = make_member(first_name="Avery", last_name="Chen")
    day = date(2026, 1, 5)
    return {
        "kind": "member", "day": day.isoformat(),
        "meal_period_id": str(periods_for_day(db, day)[0].id),
        "member_id": str(member.id), "first_name": "Casey", "last_name": "Whitman",
        "netid": "CWhit", "class_year": "2014", "email": "CASEY@example.com",
    }


@pytest.mark.parametrize("kind", ["member", "guest", "alumni"])
def test_admin_registers_each_kind_in_selected_day_and_reports(signed_in, db, values, kind):
    values["kind"] = kind
    page = signed_in.get(f"/admin/attendance/new?day={values['day']}&kind={kind}")
    assert page.status_code == 200
    assert f"Register {kind} meal" in page.text
    assert ("Avery Chen" if kind != "alumni" else "Class year") in page.text
    response = signed_in.post("/admin/attendance/new", data=values, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin?day=2026-01-05"
    row = db.query(Attendance).one()
    assert row.kind == kind
    assert row.service_date == date(2026, 1, 5)
    assert row.meal_period_id == int(values["meal_period_id"])
    assert row.scanned_at.replace(tzinfo=None) == eastern(2026, 1, 5, 8).replace(tzinfo=None)
    assert row.entry_method == "admin"
    assert row.override_by is None
    assert row.created_at.date() > row.service_date
    if kind == "alumni":
        assert row.member_id is None
        assert row.alumni_email == "casey@example.com"
        assert row.alumni_netid == "cwhit"
        assert db.query(AuditLog).filter_by(action="alumni.recorded").one().actor == "staff:jo"
    else:
        assert row.member_id == int(values["member_id"])
    entry = db.query(AuditLog).filter_by(action="attendance.backfilled").one()
    assert entry.actor == "staff:jo"
    assert entry.entity_id == row.id
    assert entry.detail["service_date"] == values["day"]
    assert row in reports.daily_attendance(db, date(2026, 1, 5))
    dashboard = signed_in.get(response.headers["location"])
    assert dashboard.status_code == 200
    assert ("Avery Chen" if kind == "member" else "Casey Whitman") in dashboard.text
    assert "8:00 AM" in dashboard.text
    csv = signed_in.get("/admin/reports/daily.csv?week=2026-01-05")
    assert f"2026-01-05,Breakfast,{kind}" in csv.text


def test_member_duplicate_blocked_but_void_allows_registration(signed_in, db, values):
    assert signed_in.post("/admin/attendance/new", data=values).status_code == 200
    duplicate = signed_in.post("/admin/attendance/new", data=values)
    assert duplicate.status_code == 422
    assert "Already checked in for Breakfast" in duplicate.text
    assert db.query(Attendance).count() == 1
    assert db.query(AuditLog).filter_by(action="attendance.backfilled").count() == 1
    void_attendance(db, db.query(Attendance).one(), "staff:jo")
    assert signed_in.post("/admin/attendance/new", data=values).status_code == 200
    assert db.query(Attendance).filter(Attendance.voided_at.is_(None)).count() == 1


def test_backfill_uses_selected_week_and_current_plan(db, values):
    member = db.get(Member, int(values["member_id"]))
    set_value(db, "plan_19_meals", "1")
    config = load_config(db)
    grant_meals(db, member, 1, date(2026, 1, 12))
    first = past_meals.register(db, values, "staff:jo")
    assert not first.is_overage
    assert weekly_usage(db, member, first.service_date, config).used == 1
    values["meal_period_id"] = str(periods_for_day(db, first.service_date)[1].id)
    second = past_meals.register(db, values, "staff:jo")
    assert second.is_overage


def test_member_without_plan_is_still_saved(db, values):
    member = db.get(Member, int(values["member_id"]))
    member.plan_type = PlanType.NONE.value
    member.status = "inactive"
    db.commit()
    row = past_meals.register(db, values, "staff:jo")
    assert row.status_warning == "inactive"
    assert not row.is_overage
    assert db.query(Attendance).count() == 1


def test_guest_quota_uses_selected_month_and_requires_explicit_reason(signed_in, db, values, config):
    host = db.get(Member, int(values["member_id"]))
    values["kind"] = "guest"
    guest_rows(db, host, 2, date(2026, 2, 2))
    assert signed_in.post("/admin/attendance/new", data=values).status_code == 200
    assert signed_in.post("/admin/attendance/new", data=values).status_code == 200
    response = signed_in.post("/admin/attendance/new", data=values)
    assert response.status_code == 422
    assert "Staff override required" in response.text
    assert 'value="Casey"' in response.text
    assert 'value="CWhit"' in response.text
    values["override_quota"] = "on"
    values["override_reason"] = "   "
    assert signed_in.post("/admin/attendance/new", data=values).status_code == 422
    values["override_reason"] = "Approved by the steward"
    assert signed_in.post("/admin/attendance/new", data=values).status_code == 200
    assert guest_usage(db, host, date(2026, 1, 5), config).used == 3
    row = db.query(Attendance).filter_by(override_by="staff:jo").one()
    assert row.override_reason == "Approved by the steward"


@pytest.mark.parametrize("category", ["guest_is_family", "guest_is_professor"])
def test_guest_exemptions_preserved(db, values, category, config):
    host = db.get(Member, int(values["member_id"]))
    guest_rows(db, host, 2, date(2026, 1, 5))
    values.update(kind="guest", netid="", **{category: "on"})
    row = past_meals.register(db, values, "staff:jo")
    assert getattr(row, category)
    assert guest_usage(db, host, date(2026, 1, 5), config).used == 2


@pytest.mark.parametrize("changes, message", [
    ({"day": "bad"}, "valid service date"),
    ({"day": "9999-12-31"}, "already ended"),
    ({"day": "2026-01-06"}, "scheduled for this service date"),
    ({"meal_period_id": "bad"}, "Choose a meal period"),
    ({"meal_period_id": "99999"}, "scheduled for this service date"),
    ({"member_id": "99999"}, "Member not found"),
    ({"kind": "unknown"}, "Choose member, guest, or alumni"),
    ({"kind": "guest", "first_name": "  "}, "first name"),
    ({"kind": "guest", "netid": ""}, "NetID or a reason"),
    ({"kind": "guest", "netid": "bad!"}, "A NetID is"),
    ({"kind": "guest", "netid_reason": "x" * 256}, "255 characters"),
    ({"kind": "alumni", "class_year": ""}, "class year"),
    ({"kind": "alumni", "class_year": "1800"}, "between 1900 and 2200"),
    ({"kind": "alumni", "email": "", "phone": ""}, "email address or a phone number"),
    ({"kind": "alumni", "email": "bad"}, "email address does not look right"),
    ({"kind": "alumni", "phone": "123"}, "at least ten digits"),
])
def test_invalid_submissions_do_not_write(signed_in, db, values, changes, message):
    values.update(changes)
    response = signed_in.post("/admin/attendance/new", data=values)
    assert response.status_code == 422
    assert message in response.text
    assert db.query(Attendance).count() == 0
    assert db.query(AuditLog).filter_by(action="attendance.backfilled").count() == 0


def test_guest_reason_and_alumni_phone_are_accepted(db, values):
    values.update(kind="guest", netid="", netid_reason="Visiting parent")
    guest = past_meals.register(db, values, "staff:jo")
    assert guest.guest_netid_reason == "Visiting parent"
    assert guest.guest_netid is None
    values.update(kind="alumni", email="", phone="(609) 555-1234")
    alum = past_meals.register(db, values, "staff:jo")
    assert alum.alumni_phone == "6095551234"
    assert alum.alumni_email is None


def test_retired_overlapping_period_is_recorded_exactly(db, values):
    period = MealPeriod(name="Old breakfast", weekday=0, start_time=time(8, 30),
                        end_time=time(9, 30), is_active=False, counts_toward_allotment=False)
    db.add(period)
    db.commit()
    values["meal_period_id"] = str(period.id)
    row = past_meals.register(db, values, "staff:jo")
    assert row.meal_period_id == period.id
    assert not row.is_overage
    assert period in past_meals.completed_periods(db, row.service_date, eastern(2026, 1, 5, 10))


@pytest.mark.parametrize("now, allowed", [
    (eastern(2026, 1, 5, 7), False),
    (eastern(2026, 1, 5, 9), False),
    (eastern(2026, 1, 5, 10), False),
    (eastern(2026, 1, 5, 10, 1), True),
])
def test_only_completed_periods_are_offered_and_accepted(db, values, now, allowed):
    periods = past_meals.completed_periods(db, date(2026, 1, 5), now)
    assert (int(values["meal_period_id"]) in [p.id for p in periods]) == allowed
    if allowed:
        assert past_meals.register(db, values, "staff:jo", now=now)
    else:
        with pytest.raises(ValueError, match="already ended"):
            past_meals.register(db, values, "staff:jo", now=now)
        assert db.query(Attendance).count() == 0


def test_overnight_period_ends_on_next_day_and_keeps_original_service_date(db, values):
    period = MealPeriod(name="Late meal", weekday=6, start_time=time(22), end_time=time(1))
    db.add(period)
    db.commit()
    values.update(day="2026-01-04", meal_period_id=str(period.id))
    with pytest.raises(ValueError, match="already ended"):
        past_meals.register(db, values, "staff:jo", now=eastern(2026, 1, 5, 0, 30))
    row = past_meals.register(db, values, "staff:jo", now=eastern(2026, 1, 5, 1, 1))
    assert row.service_date == date(2026, 1, 4)


def test_dst_end_boundary_is_compared_as_an_instant(db):
    period = MealPeriod(name="Late meal", weekday=5, start_time=time(22), end_time=time(1, 30))
    db.add(period)
    db.commit()
    # 1:15 EST is after the first 1:30 (EDT), even though the wall clock is earlier.
    now = eastern(2026, 11, 1, 1, 15).replace(fold=1)
    assert period in past_meals.completed_periods(db, date(2026, 10, 31), now)


def test_registration_requires_admin(client, signed_in, db, admin_user, values):
    admin_user.role = StaffRole.STAFF.value
    db.commit()
    assert signed_in.get("/admin/attendance/new").status_code == 403
    assert signed_in.post("/admin/attendance/new", data=values).status_code == 403
    assert "Register past meal" not in signed_in.get("/admin").text
    signed_in.post("/logout")
    assert client.get("/admin/attendance/new", follow_redirects=False).status_code in (303, 401)
    assert client.post("/admin/attendance/new", data=values, follow_redirects=False).status_code in (303, 401)
    assert db.query(Attendance).count() == 0
