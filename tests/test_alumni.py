"""Alumni meals — the one kind of meal with no member behind it."""
from __future__ import annotations

import pytest

from app.models import Attendance, AttendanceKind, AuditLog
from app.services import credentials as credential_service
from app.services.alumni import (
    is_valid_email,
    is_valid_phone,
    normalize_email,
    normalize_phone,
)
from app.services.allotment import weekly_usage
from app.services.guests import guest_usage
from app.services.scan import ScanOutcome, process_scan, record_alumni_meal
from tests.conftest import eastern

DINNER = eastern(2026, 1, 5, 18, 0)

# The alum used throughout: name, class year, and one contact detail.
CASEY = {"first_name": "Casey", "last_name": "Whitman", "class_year": 2014}


class TestRecording:
    def test_an_alumni_meal_is_recorded_against_no_member(self, db):
        result = record_alumni_meal(db, **CASEY, email="casey@example.com", moment=DINNER)
        assert result.outcome is ScanOutcome.ALUMNI_RECORDED
        row = result.attendance
        assert row.kind == AttendanceKind.ALUMNI.value
        assert row.member_id is None

    def test_the_alum_is_recorded_by_name_and_class_year(self, db):
        row = record_alumni_meal(
            db, **CASEY, email="casey@example.com", moment=DINNER
        ).attendance
        assert row.alumni_first_name == "Casey"
        assert row.alumni_last_name == "Whitman"
        assert row.alumni_class_year == 2014
        assert row.alumni_name == "Casey Whitman"

    def test_an_email_is_stored_in_one_case_so_reports_can_group(self, db):
        row = record_alumni_meal(
            db, **CASEY, email="  Casey@Example.COM ", moment=DINNER
        ).attendance
        assert row.alumni_email == "casey@example.com"

    def test_a_phone_number_is_stored_without_its_punctuation(self, db):
        row = record_alumni_meal(
            db, **CASEY, phone="(609) 555-1234", moment=DINNER
        ).attendance
        assert row.alumni_phone == "6095551234"

    def test_either_contact_detail_alone_is_recorded(self, db):
        by_email = record_alumni_meal(
            db, **CASEY, email="casey@example.com", moment=DINNER
        ).attendance
        by_phone = record_alumni_meal(
            db, **CASEY, phone="6095551234", moment=DINNER
        ).attendance
        assert (by_email.alumni_email, by_email.alumni_phone) == (
            "casey@example.com",
            None,
        )
        assert (by_phone.alumni_email, by_phone.alumni_phone) == (None, "6095551234")

    def test_both_contact_details_are_kept_when_both_are_given(self, db):
        """Unlike a guest's NetID and its stand-in reason, these are not
        alternatives on the record — only on the form. Two ways to reach an alum
        is better than one, so neither is dropped."""
        row = record_alumni_meal(
            db,
            **CASEY,
            email="casey@example.com",
            phone="6095551234",
            moment=DINNER,
        ).attendance
        assert row.alumni_email == "casey@example.com"
        assert row.alumni_phone == "6095551234"

    def test_a_netid_is_recorded_when_the_alum_still_has_one(self, db):
        row = record_alumni_meal(
            db, **CASEY, email="casey@example.com", netid="cwhitman", moment=DINNER
        ).attendance
        assert row.alumni_netid == "cwhitman"

    def test_a_netid_is_stored_in_one_case_like_every_other_netid(self, db):
        row = record_alumni_meal(
            db, **CASEY, email="casey@example.com", netid=" CWhitman ", moment=DINNER
        ).attendance
        assert row.alumni_netid == "cwhitman"

    def test_an_alum_with_no_netid_is_recorded_all_the_same(self, db):
        """Optional means optional: an alum whose NetID lapsed years ago is not
        held at the door over it, and no reason is demanded in its place the way
        a guest's missing NetID demands one."""
        result = record_alumni_meal(
            db, **CASEY, email="casey@example.com", moment=DINNER
        )
        assert result.outcome is ScanOutcome.ALUMNI_RECORDED
        assert result.attendance.alumni_netid is None

    def test_the_meal_lands_in_the_period_being_served(self, db):
        result = record_alumni_meal(db, **CASEY, email="c@example.com", moment=DINNER)
        assert result.period.name == "Dinner"
        assert result.attendance.meal_period_id == result.period.id

    def test_an_alumni_meal_outside_service_hours_is_not_recorded(self, db):
        result = record_alumni_meal(
            db, **CASEY, email="c@example.com", moment=eastern(2026, 1, 5, 15, 30)
        )
        assert result.outcome is ScanOutcome.OUTSIDE_SERVICE
        assert db.query(Attendance).count() == 0

    def test_it_is_never_flagged_as_an_overage(self, db):
        """There is no plan to be over: an alum is not on one."""
        row = record_alumni_meal(
            db, **CASEY, email="c@example.com", moment=DINNER
        ).attendance
        assert row.is_overage is False

    def test_every_alumni_meal_is_audited(self, db):
        """The audit log is the only other place this person's name appears —
        there is no member row to find them from later."""
        record_alumni_meal(
            db, **CASEY, email="casey@example.com", netid="cwhitman", moment=DINNER
        )
        entry = db.query(AuditLog).filter_by(action="alumni.recorded").one()
        assert entry.detail["name"] == "Casey Whitman"
        assert entry.detail["class_year"] == 2014
        assert entry.detail["email"] == "casey@example.com"
        assert entry.detail["netid"] == "cwhitman"

    def test_there_is_no_quota_to_run_out_of(self, db):
        for _ in range(5):
            result = record_alumni_meal(
                db, **CASEY, email="casey@example.com", moment=DINNER
            )
            assert result.outcome is ScanOutcome.ALUMNI_RECORDED
        assert db.query(Attendance).filter_by(kind=AttendanceKind.ALUMNI.value).count() == 5


class TestTouchesNobodysCounters:
    """An alumni meal is nobody's meal but the alum's."""

    @pytest.fixture
    def member(self, db, make_member):
        person = make_member(first_name="Robin", last_name="Diallo")
        credential_service.bind_card(db, person, "ALUMHOST")
        db.commit()
        return person

    def test_it_does_not_spend_any_members_weekly_allotment(self, db, member, config):
        record_alumni_meal(db, **CASEY, email="c@example.com", moment=DINNER)
        assert weekly_usage(db, member, DINNER.date(), config).used == 0

        result = process_scan(db, "ALUMHOST", moment=eastern(2026, 1, 5, 18, 10))
        assert result.outcome is ScanOutcome.CHECKED_IN
        assert result.weekly.used == 1  # the member's own meal, and only that

    def test_it_does_not_spend_any_members_guest_meals(self, db, member, config):
        record_alumni_meal(db, **CASEY, email="c@example.com", moment=DINNER)
        usage = guest_usage(db, member, DINNER.date(), config)
        assert usage.used == 0
        assert usage.remaining == 2


class TestContactFormats:
    @pytest.mark.parametrize(
        "value", ["casey@example.com", "c.whitman@alumni.princeton.edu", "a@b.co"]
    )
    def test_real_addresses_are_accepted(self, value):
        assert is_valid_email(value)

    @pytest.mark.parametrize(
        "value", ["", "casey", "casey@", "@example.com", "casey@example", "a b@c.com"]
    )
    def test_malformed_addresses_are_rejected(self, value):
        assert not is_valid_email(value)

    def test_addresses_are_normalized_before_they_are_checked(self):
        assert normalize_email("  Casey@Example.COM ") == "casey@example.com"
        assert is_valid_email("  Casey@Example.COM ")

    @pytest.mark.parametrize(
        "value", ["6095551234", "(609) 555-1234", "609-555-1234", "+16095551234"]
    )
    def test_real_phone_numbers_are_accepted_however_they_are_punctuated(self, value):
        assert is_valid_phone(value)

    @pytest.mark.parametrize("value", ["", "555-1234", "609555", "phone", "6095551234x"])
    def test_malformed_phone_numbers_are_rejected(self, value):
        assert not is_valid_phone(value)

    def test_one_number_has_one_spelling_once_normalized(self):
        assert normalize_phone("(609) 555-1234") == normalize_phone("609.555.1234")
