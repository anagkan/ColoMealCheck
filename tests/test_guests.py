"""The 2-guest-meals-per-month benefit — the one quota that actually blocks."""
from __future__ import annotations

from datetime import date

import pytest

from app.models import Attendance, AttendanceKind, AuditLog, PlanType
from app.services import credentials as credential_service
from app.services.netid import is_valid_netid, normalize_netid
from app.services.scan import ScanOutcome, process_scan, record_guest
from tests.conftest import eastern
from tests.helpers import grant_meals

MONDAY = date(2026, 1, 5)

# Every guest is recorded as (first name, last name, NetID). The quota tests
# care how many guests there were rather than who, so the three fields are
# spelled out once here and splatted in at the call sites.
SAM = ("Sam", "Ortiz", "sortiz")
JESS = ("Jess", "Nakamura", "jnakamura")
KIM = ("Kim", "Adeyemi", "kadeyemi")


@pytest.fixture
def host(db, make_member):
    member = make_member(first_name="Robin", last_name="Diallo")
    credential_service.bind_card(db, member, "HOSTCARD")
    db.commit()
    return member


class TestGuestIdentity:
    def test_the_guest_is_recorded_by_name_and_netid(self, db, host):
        result = record_guest(db, host, *SAM, moment=eastern(2026, 1, 5, 18, 0))
        row = result.attendance
        assert row.guest_first_name == "Sam"
        assert row.guest_last_name == "Ortiz"
        assert row.guest_netid == "sortiz"

    def test_the_display_name_is_built_from_the_two_name_parts(self, db, host):
        """Reports and admin screens print guest_name; it must stay populated."""
        result = record_guest(db, host, *SAM, moment=eastern(2026, 1, 5, 18, 0))
        assert result.attendance.guest_name == "Sam Ortiz"

    def test_a_netid_is_stored_in_one_case_so_reports_can_group(self, db, host):
        result = record_guest(
            db, host, "Sam", "Ortiz", " SOrtiz ", moment=eastern(2026, 1, 5, 18, 0)
        )
        assert result.attendance.guest_netid == "sortiz"

    def test_a_guest_with_no_netid_is_recorded_with_the_reason_instead(self, db, host):
        result = record_guest(
            db,
            host,
            "Pat",
            "Ortiz",
            "",
            guest_netid_reason="visiting parent",
            moment=eastern(2026, 1, 5, 18, 0),
        )
        assert result.outcome is ScanOutcome.GUEST_RECORDED
        assert result.attendance.guest_netid is None
        assert result.attendance.guest_netid_reason == "visiting parent"

    def test_a_row_never_claims_both_a_netid_and_a_reason_for_having_none(self, db, host):
        """The two are alternatives, so a NetID always wins over a stray reason."""
        result = record_guest(
            db,
            host,
            *SAM,
            guest_netid_reason="visiting parent",
            moment=eastern(2026, 1, 5, 18, 0),
        )
        assert result.attendance.guest_netid == "sortiz"
        assert result.attendance.guest_netid_reason is None

    def test_the_host_is_the_member_on_the_row(self, db, host):
        result = record_guest(db, host, *SAM, moment=eastern(2026, 1, 5, 18, 0))
        assert result.attendance.member_id == host.id
        assert result.attendance.kind == AttendanceKind.GUEST.value

    @pytest.mark.parametrize("value", ["ak9981", "jsmith", "ab1"])
    def test_real_netids_are_accepted(self, value):
        assert is_valid_netid(value)

    @pytest.mark.parametrize("value", ["", "a", "9anagh", "ak-9981", "toolongnetid"])
    def test_malformed_netids_are_rejected(self, value):
        assert not is_valid_netid(value)

    def test_netids_are_normalized_before_they_are_checked(self):
        assert normalize_netid("  AK9981 ") == "ak9981"
        assert is_valid_netid("  AK9981 ")


class TestQuota:
    def test_first_two_guests_are_free(self, db, host):
        first = record_guest(db, host, *SAM, moment=eastern(2026, 1, 5, 18, 0))
        assert first.outcome is ScanOutcome.GUEST_RECORDED
        assert first.guests.used == 1
        assert first.guests.remaining == 1

        second = record_guest(db, host, *JESS, moment=eastern(2026, 1, 6, 18, 0))
        assert second.outcome is ScanOutcome.GUEST_RECORDED
        assert second.guests.remaining == 0

    def test_third_guest_is_blocked(self, db, host):
        record_guest(db, host, *SAM, moment=eastern(2026, 1, 5, 18, 0))
        record_guest(db, host, *JESS, moment=eastern(2026, 1, 6, 18, 0))

        third = record_guest(db, host, *KIM, moment=eastern(2026, 1, 7, 18, 0))
        assert third.outcome is ScanOutcome.GUEST_QUOTA_EXCEEDED
        assert third.attendance is None
        assert db.query(Attendance).filter_by(kind=AttendanceKind.GUEST.value).count() == 2

    def test_rca_paa_host_gets_the_same_two_guest_meals(self, db, make_member):
        """The guest quota is club-wide: a smaller meal plan does not shrink it."""
        member = make_member(plan_type=PlanType.RCA_PAA.value)
        credential_service.bind_card(db, member, "RCACARD")
        db.commit()

        first = record_guest(db, member, *SAM, moment=eastern(2026, 1, 5, 18, 0))
        assert first.guests.quota == 2
        second = record_guest(db, member, *JESS, moment=eastern(2026, 1, 6, 18, 0))
        assert second.outcome is ScanOutcome.GUEST_RECORDED
        assert second.guests.remaining == 0

        third = record_guest(db, member, *KIM, moment=eastern(2026, 1, 7, 18, 0))
        assert third.outcome is ScanOutcome.GUEST_QUOTA_EXCEEDED

    def test_two_guests_at_one_meal_is_allowed(self, db, host):
        """The quota is monthly, not per-sitting."""
        record_guest(db, host, *SAM, moment=eastern(2026, 1, 5, 18, 0))
        second = record_guest(db, host, *JESS, moment=eastern(2026, 1, 5, 18, 5))
        assert second.outcome is ScanOutcome.GUEST_RECORDED

    def test_guest_outside_service_hours_is_not_recorded(self, db, host):
        result = record_guest(db, host, *SAM, moment=eastern(2026, 1, 5, 15, 30))
        assert result.outcome is ScanOutcome.OUTSIDE_SERVICE
        assert db.query(Attendance).count() == 0

    def test_unnamed_guest_still_gets_a_label(self, db, host):
        """The service layer records what it is given.

        Requiring the guest's details is the popup's job and the API's job, not
        this one's — see TestGuestApi, where a blank name is refused.
        """
        result = record_guest(db, host, "   ", "  ", "", moment=eastern(2026, 1, 5, 18, 0))
        assert result.attendance.guest_name == "Guest"
        assert result.attendance.guest_netid is None


class TestOverride:
    def test_staff_override_records_the_blocked_guest(self, db, host):
        record_guest(db, host, *SAM, moment=eastern(2026, 1, 5, 18, 0))
        record_guest(db, host, *JESS, moment=eastern(2026, 1, 6, 18, 0))

        result = record_guest(
            db,
            host,
            *KIM,
            moment=eastern(2026, 1, 7, 18, 0),
            override_by="staff:jo",
            override_reason="prospective member dinner",
        )
        assert result.outcome is ScanOutcome.GUEST_RECORDED
        assert result.attendance.override_by == "staff:jo"
        assert result.attendance.override_reason == "prospective member dinner"
        assert result.guests.used == 3

    def test_override_is_written_to_the_audit_log(self, db, host):
        record_guest(db, host, *SAM, moment=eastern(2026, 1, 5, 18, 0))
        record_guest(db, host, *JESS, moment=eastern(2026, 1, 6, 18, 0))
        record_guest(
            db,
            host,
            *KIM,
            moment=eastern(2026, 1, 7, 18, 0),
            override_by="staff:jo",
            override_reason="prospective member dinner",
        )

        entry = db.query(AuditLog).filter_by(action="guest.override").one()
        assert entry.actor == "staff:jo"
        assert entry.detail["guest_name"] == "Kim Adeyemi"
        assert entry.detail["guest_netid"] == "kadeyemi"
        assert entry.detail["reason"] == "prospective member dinner"

    def test_no_audit_entry_for_a_guest_within_quota(self, db, host):
        record_guest(db, host, *SAM, moment=eastern(2026, 1, 5, 18, 0))
        assert db.query(AuditLog).filter_by(action="guest.override").count() == 0


class TestMonthlyReset:
    def test_quota_resets_on_the_first(self, db, host):
        record_guest(db, host, *SAM, moment=eastern(2026, 1, 30, 18, 0))
        record_guest(db, host, *JESS, moment=eastern(2026, 1, 31, 18, 0))
        assert (
            record_guest(db, host, *KIM, moment=eastern(2026, 1, 31, 18, 30)).outcome
            is ScanOutcome.GUEST_QUOTA_EXCEEDED
        )

        february = record_guest(db, host, *KIM, moment=eastern(2026, 2, 1, 18, 0))
        assert february.outcome is ScanOutcome.GUEST_RECORDED
        assert february.guests.used == 1

    def test_quota_is_calendar_month_not_rolling_thirty_days(self, db, host):
        """Two guests on 31 January do not restrict 1 February at all."""
        record_guest(db, host, *SAM, moment=eastern(2026, 1, 31, 18, 0))
        record_guest(db, host, *JESS, moment=eastern(2026, 1, 31, 18, 5))
        result = record_guest(db, host, *KIM, moment=eastern(2026, 2, 1, 18, 0))
        assert result.guests.remaining == 1


class TestSeparateBuckets:
    """Guest meals and the weekly allotment never touch each other."""

    def test_hosting_guests_does_not_spend_the_hosts_weekly_meals(self, db, host):
        record_guest(db, host, *SAM, moment=eastern(2026, 1, 5, 18, 0))
        record_guest(db, host, *JESS, moment=eastern(2026, 1, 5, 18, 5))

        result = process_scan(db, "HOSTCARD", moment=eastern(2026, 1, 5, 18, 10))
        assert result.outcome is ScanOutcome.CHECKED_IN
        assert result.weekly.used == 1  # only the host's own meal

    def test_guest_meals_are_available_even_when_the_host_is_over_plan(self, db, make_member):
        member = make_member(plan_type=PlanType.PLAN_14.value)
        credential_service.bind_card(db, member, "OVERCARD")
        db.commit()
        grant_meals(db, member, 14, MONDAY)

        over = process_scan(db, "OVERCARD", moment=eastern(2026, 1, 9, 18, 0))
        assert over.outcome is ScanOutcome.CHECKED_IN_OVERAGE

        guest = record_guest(db, member, *SAM, moment=eastern(2026, 1, 9, 18, 5))
        assert guest.outcome is ScanOutcome.GUEST_RECORDED

    def test_a_guest_meal_is_never_flagged_as_an_overage(self, db, host):
        result = record_guest(db, host, *SAM, moment=eastern(2026, 1, 5, 18, 0))
        assert result.attendance.is_overage is False

    def test_guest_row_does_not_trip_the_hosts_duplicate_guard(self, db, host):
        """A host checks in, then brings a guest to the same sitting."""
        process_scan(db, "HOSTCARD", moment=eastern(2026, 1, 5, 18, 0))
        guest = record_guest(db, host, *SAM, moment=eastern(2026, 1, 5, 18, 2))
        assert guest.outcome is ScanOutcome.GUEST_RECORDED
        assert db.query(Attendance).count() == 2
