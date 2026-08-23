"""The check-in decision: allotments, duplicates, status, entry-method parity."""
from __future__ import annotations

from datetime import date, time

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import (
    Attendance,
    AttendanceKind,
    AuditLog,
    CredentialType,
    EntryMethod,
    MealPeriod,
    MemberStatus,
    PlanType,
)
from app.services import credentials as credential_service
from app.services.scan import ScanOutcome, process_scan, void_attendance
from tests.conftest import eastern
from tests.helpers import grant_meals

MONDAY = date(2026, 1, 5)
CARD = "A1B2C3D4"


@pytest.fixture
def member_with_card(db, make_member):
    member = make_member(first_name="Avery", last_name="Chen", plan_type=PlanType.PLAN_19.value)
    credential_service.bind_card(db, member, CARD)
    db.commit()
    return member


class TestIdentification:
    def test_unknown_card_is_rejected_without_writing_anything(self, db):
        result = process_scan(db, "DEADBEEF", moment=eastern(2026, 1, 5, 12, 0))
        assert result.outcome is ScanOutcome.UNKNOWN_CREDENTIAL
        assert db.query(Attendance).count() == 0

    def test_unknown_puid_is_rejected(self, db):
        result = process_scan(
            db, "999999999", CredentialType.MANUAL_PUID.value, moment=eastern(2026, 1, 5, 12, 0)
        )
        assert result.outcome is ScanOutcome.UNKNOWN_CREDENTIAL
        assert db.query(Attendance).count() == 0

    def test_card_value_normalization_survives_reader_formatting(self, db, member_with_card):
        """The same card typed as lowercase, spaced or hyphenated is one card,
        not three enrollments."""
        result = process_scan(db, " a1b2-c3d4 ", moment=eastern(2026, 1, 5, 12, 0))
        assert result.outcome is ScanOutcome.CHECKED_IN

    def test_puid_entry_works_before_any_card_is_enrolled(self, db, make_member):
        """The club has to be usable on day one, before a single tap."""
        member = make_member(puid="905551234")
        result = process_scan(
            db, "905551234", CredentialType.MANUAL_PUID.value, moment=eastern(2026, 1, 5, 12, 0)
        )
        assert result.outcome is ScanOutcome.CHECKED_IN
        assert result.member.id == member.id
        assert result.attendance.entry_method == EntryMethod.MANUAL_PUID.value
        assert result.attendance.credential_id is None


class TestAllotment:
    @pytest.mark.parametrize(
        ("plan", "allotment"),
        [
            (PlanType.PLAN_19.value, 19),
            (PlanType.PLAN_14.value, 14),
            (PlanType.RCA_PAA.value, 9),
        ],
    )
    def test_meal_within_plan_is_a_clean_check_in(self, db, make_member, plan, allotment):
        member = make_member(plan_type=plan)
        credential_service.bind_card(db, member, "CARD1")
        db.commit()

        result = process_scan(db, "CARD1", moment=eastern(2026, 1, 5, 12, 0))
        assert result.outcome is ScanOutcome.CHECKED_IN
        assert result.attendance.is_overage is False
        assert result.weekly.used == 1
        assert result.weekly.allotment == allotment

    def test_nineteenth_meal_is_still_within_plan(self, db, member_with_card):
        grant_meals(db, member_with_card, 18, MONDAY)
        # 18 back-filled meals run Monday breakfast through Sunday brunch, so
        # the last free window of the week is Sunday dinner.
        result = process_scan(db, CARD, moment=eastern(2026, 1, 11, 18, 0))
        assert result.outcome is ScanOutcome.CHECKED_IN
        assert result.weekly.used == 19
        assert result.attendance.is_overage is False

    def test_twentieth_meal_is_recorded_as_an_overage_not_refused(self, db, member_with_card):
        """The decision the club actually cares about: nobody is turned away.

        A 19-plan member can only exceed their allotment if the club serves
        more than 19 windows in a week, so this adds a Sunday late meal — the
        realistic way it happens.
        """
        db.add(
            MealPeriod(
                name="Late Meal",
                weekday=6,  # Sunday
                start_time=time(22, 0),
                end_time=time(23, 30),
                counts_toward_allotment=True,
                is_active=True,
                sort_order=40,
            )
        )
        db.commit()
        grant_meals(db, member_with_card, 19, MONDAY)

        result = process_scan(db, CARD, moment=eastern(2026, 1, 11, 22, 30))

        assert result.outcome is ScanOutcome.CHECKED_IN_OVERAGE
        assert result.ok is True
        assert result.attendance is not None
        assert result.attendance.is_overage is True
        assert result.weekly.used == 20

    def test_fifteenth_meal_is_an_overage_on_the_fourteen_plan(self, db, make_member):
        member = make_member(plan_type=PlanType.PLAN_14.value)
        credential_service.bind_card(db, member, "CARD14")
        db.commit()
        grant_meals(db, member, 14, MONDAY)

        result = process_scan(db, "CARD14", moment=eastern(2026, 1, 11, 18, 0))
        assert result.outcome is ScanOutcome.CHECKED_IN_OVERAGE
        assert result.weekly.allotment == 14
        assert result.weekly.used == 15

    def test_tenth_meal_is_an_overage_on_the_rca_paa_plan(self, db, make_member):
        member = make_member(plan_type=PlanType.RCA_PAA.value)
        credential_service.bind_card(db, member, "CARDRCA")
        db.commit()
        grant_meals(db, member, 9, MONDAY)

        result = process_scan(db, "CARDRCA", moment=eastern(2026, 1, 11, 18, 0))
        assert result.outcome is ScanOutcome.CHECKED_IN_OVERAGE
        assert result.weekly.allotment == 9
        assert result.weekly.used == 10

    def test_allotment_resets_the_following_monday(self, db, member_with_card):
        """No rollover, and no carried-over overage: a fresh 19 every Monday."""
        grant_meals(db, member_with_card, 19, MONDAY)

        next_monday = process_scan(db, CARD, moment=eastern(2026, 1, 12, 8, 0))
        assert next_monday.outcome is ScanOutcome.CHECKED_IN
        assert next_monday.weekly.used == 1
        assert next_monday.attendance.is_overage is False

    def test_sunday_dinner_does_not_spend_next_weeks_allotment(self, db, member_with_card):
        grant_meals(db, member_with_card, 18, MONDAY)
        sunday = process_scan(db, CARD, moment=eastern(2026, 1, 11, 18, 0))
        assert sunday.weekly.used == 19
        assert sunday.weekly.week_start == MONDAY

        monday = process_scan(db, CARD, moment=eastern(2026, 1, 12, 8, 0))
        assert monday.weekly.week_start == date(2026, 1, 12)
        assert monday.weekly.used == 1

    def test_member_with_no_plan_is_recorded_and_flagged(self, db, make_member):
        member = make_member(plan_type=PlanType.NONE.value)
        credential_service.bind_card(db, member, "NOPLAN")
        db.commit()

        result = process_scan(db, "NOPLAN", moment=eastern(2026, 1, 5, 12, 0))
        assert result.outcome is ScanOutcome.NO_MEAL_PLAN
        assert result.attendance is not None
        assert "No meal plan on file" in result.warnings


class TestDuplicateGuard:
    def test_second_scan_in_the_same_period_is_refused(self, db, member_with_card):
        first = process_scan(db, CARD, moment=eastern(2026, 1, 5, 12, 0))
        assert first.outcome is ScanOutcome.CHECKED_IN

        second = process_scan(db, CARD, moment=eastern(2026, 1, 5, 12, 30))
        assert second.outcome is ScanOutcome.ALREADY_CHECKED_IN
        assert second.attendance.id == first.attendance.id
        assert db.query(Attendance).count() == 1

    def test_guard_is_member_scoped_not_credential_scoped(self, db, member_with_card):
        """Tap the card, then type the PUID: still the same person, same meal."""
        process_scan(db, CARD, moment=eastern(2026, 1, 5, 12, 0))
        typed = process_scan(
            db,
            member_with_card.puid,
            CredentialType.MANUAL_PUID.value,
            moment=eastern(2026, 1, 5, 12, 5),
        )
        assert typed.outcome is ScanOutcome.ALREADY_CHECKED_IN
        assert db.query(Attendance).count() == 1

    def test_later_period_the_same_day_is_a_new_meal(self, db, member_with_card):
        process_scan(db, CARD, moment=eastern(2026, 1, 5, 12, 0))  # lunch
        dinner = process_scan(db, CARD, moment=eastern(2026, 1, 5, 18, 0))
        assert dinner.outcome is ScanOutcome.CHECKED_IN
        assert dinner.weekly.used == 2

    def test_staff_can_force_a_genuine_second_meal(self, db, member_with_card):
        process_scan(db, CARD, moment=eastern(2026, 1, 5, 12, 0))
        forced = process_scan(
            db, CARD, moment=eastern(2026, 1, 5, 12, 40), force=True, actor="staff:jo"
        )
        assert forced.outcome is ScanOutcome.CHECKED_IN
        assert forced.attendance.override_by == "staff:jo"
        assert db.query(Attendance).count() == 2

    def test_database_rejects_a_duplicate_written_behind_the_services_back(
        self, db, member_with_card
    ):
        """The guard is a real partial unique index, not just a service check —
        so a racing second tap or a replayed offline scan cannot slip past."""
        first = process_scan(db, CARD, moment=eastern(2026, 1, 5, 12, 0))
        db.add(
            Attendance(
                member_id=member_with_card.id,
                meal_period_id=first.attendance.meal_period_id,
                entry_method=EntryMethod.CSN.value,
                service_date=first.attendance.service_date,
                scanned_at=eastern(2026, 1, 5, 12, 10),
                kind=AttendanceKind.MEMBER.value,
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    def test_voided_check_in_frees_the_slot_for_a_rescan(self, db, member_with_card):
        first = process_scan(db, CARD, moment=eastern(2026, 1, 5, 12, 0))
        void_attendance(db, first.attendance, actor="staff:jo")

        again = process_scan(db, CARD, moment=eastern(2026, 1, 5, 12, 15))
        assert again.outcome is ScanOutcome.CHECKED_IN
        assert again.weekly.used == 1  # the voided meal no longer counts


class TestServiceWindows:
    def test_scan_outside_service_records_nothing(self, db, member_with_card):
        result = process_scan(db, CARD, moment=eastern(2026, 1, 5, 15, 30))
        assert result.outcome is ScanOutcome.OUTSIDE_SERVICE
        assert result.member is not None  # the kiosk still shows who they are
        assert db.query(Attendance).count() == 0

    def test_it_offers_the_meals_either_side_instead_of_only_refusing(
        self, db, member_with_card
    ):
        """15:30 Monday is between lunch and dinner. Both are named, so the
        kiosk can put a button on each rather than sending the member away."""
        result = process_scan(db, CARD, moment=eastern(2026, 1, 5, 15, 30))
        assert [(o.direction, o.period.name) for o in result.offers] == [
            ("previous", "Lunch"),
            ("next", "Dinner"),
        ]

    def test_a_check_in_inside_a_window_offers_nothing(self, db, member_with_card):
        """The offer is only ever an answer to 'nothing is open'. Carrying it on
        an ordinary check-in would put the buttons on a screen where picking one
        could only move a meal off the window that is actually serving."""
        result = process_scan(db, CARD, moment=eastern(2026, 1, 5, 12, 0))
        assert result.offers == []


class TestCheckInAnyway:
    """The member who is a few minutes early — or a few minutes late.

    Nothing here is staff-authorized: being early for dinner is not an offence,
    and the alternative is telling somebody to leave and come back.
    """

    def test_the_next_meal_can_be_checked_into_early(self, db, member_with_card):
        # 17:30 Monday, fifteen minutes before dinner opens.
        result = process_scan(db, CARD, moment=eastern(2026, 1, 5, 17, 30), attach="next")
        assert result.outcome is ScanOutcome.CHECKED_IN
        assert result.period.name == "Dinner"
        assert result.attendance.service_date == MONDAY
        assert result.weekly.used == 1

    def test_the_previous_meal_can_be_checked_into_late(self, db, member_with_card):
        result = process_scan(
            db, CARD, moment=eastern(2026, 1, 5, 14, 0), attach="previous"
        )
        assert result.outcome is ScanOutcome.CHECKED_IN
        assert result.period.name == "Lunch"
        assert result.attendance.service_date == MONDAY

    def test_the_result_says_which_meal_was_spent(self, db, member_with_card):
        """The row looks like any other check-in, so this line is the only thing
        that tells the member their tap went against a meal that is not open."""
        early = process_scan(db, CARD, moment=eastern(2026, 1, 5, 17, 30), attach="next")
        assert "Checked in before Dinner opened" in early.warnings

        late = process_scan(db, CARD, moment=eastern(2026, 1, 5, 14, 0), attach="previous")
        assert "Checked in after Lunch closed" in late.warnings

    def test_an_early_check_in_is_still_one_meal(self, db, member_with_card):
        """The duplicate guard is the whole reason an early check-in is not
        recorded as a staff override: a member who checks in early and taps
        again once dinner opens must not be charged for two dinners."""
        early = process_scan(db, CARD, moment=eastern(2026, 1, 5, 17, 30), attach="next")
        again = process_scan(db, CARD, moment=eastern(2026, 1, 5, 18, 0))
        assert again.outcome is ScanOutcome.ALREADY_CHECKED_IN
        assert again.attendance.id == early.attendance.id
        assert db.query(Attendance).count() == 1

    def test_it_is_ignored_while_a_meal_is_being_served(self, db, member_with_card):
        """Otherwise a tap at noon could be booked against breakfast or dinner,
        which is not a choice anybody standing in the lunch queue should have."""
        result = process_scan(db, CARD, moment=eastern(2026, 1, 5, 12, 0), attach="previous")
        assert result.period.name == "Lunch"
        assert result.warnings == []

    def test_a_meal_on_the_day_before_is_booked_against_that_day(self, db, member_with_card):
        """07:00 Monday reaches back to Sunday dinner. Filing it under Monday
        would put the meal in the wrong week as well as the wrong day — Sunday
        is the last day of its meal week, Monday the first of the next."""
        result = process_scan(
            db, CARD, moment=eastern(2026, 1, 5, 7, 0), attach="previous"
        )
        assert result.period.name == "Dinner"
        assert result.attendance.service_date == date(2026, 1, 4)  # Sunday

    def test_it_is_audited(self, db, member_with_card):
        process_scan(db, CARD, moment=eastern(2026, 1, 5, 17, 30), attach="next")
        entry = db.query(AuditLog).filter(
            AuditLog.action == "attendance.outside_service"
        ).one()
        assert entry.detail["period"] == "Dinner"
        assert entry.detail["direction"] == "next"

    def test_an_unknown_direction_changes_nothing(self, db, member_with_card):
        result = process_scan(
            db, CARD, moment=eastern(2026, 1, 5, 15, 30), attach="sideways"
        )
        assert result.outcome is ScanOutcome.OUTSIDE_SERVICE
        assert db.query(Attendance).count() == 0

    def test_with_no_schedule_there_is_nothing_to_attach_to(self, db, member_with_card):
        db.query(MealPeriod).delete()
        db.commit()
        result = process_scan(db, CARD, moment=eastern(2026, 1, 5, 15, 30), attach="next")
        assert result.outcome is ScanOutcome.OUTSIDE_SERVICE
        assert result.offers == []
        assert db.query(Attendance).count() == 0


class TestMembershipStatus:
    @pytest.mark.parametrize(
        "status",
        [MemberStatus.INACTIVE.value, MemberStatus.ABROAD.value, MemberStatus.ALUM.value],
    )
    def test_non_active_member_is_flagged_but_still_fed(self, db, make_member, status):
        member = make_member(status=status)
        credential_service.bind_card(db, member, "STATUS1")
        db.commit()

        result = process_scan(db, "STATUS1", moment=eastern(2026, 1, 5, 12, 0))
        assert result.ok is True
        assert result.attendance.status_warning == status
        assert any(status in w for w in result.warnings)

    def test_active_member_carries_no_status_warning(self, db, member_with_card):
        result = process_scan(db, CARD, moment=eastern(2026, 1, 5, 12, 0))
        assert result.attendance.status_warning is None
        assert result.warnings == []


class TestEntryMethodParity:
    """Every rule must behave identically whether the card was tapped or the
    PUID typed. Parametrizing over both is what stops the two paths drifting."""

    @pytest.fixture
    def parity_member(self, db, make_member):
        # A 14-plan member, so an overage is reachable inside a normal week of
        # 19 serving windows.
        member = make_member(plan_type=PlanType.PLAN_14.value)
        credential_service.bind_card(db, member, "PARITY")
        db.commit()
        return member

    @pytest.fixture
    def scan_as(self, db, parity_member):
        def _scan(method: str, moment):
            if method == "card":
                return process_scan(db, "PARITY", moment=moment)
            return process_scan(
                db, parity_member.puid, CredentialType.MANUAL_PUID.value, moment=moment
            )

        return _scan

    @pytest.mark.parametrize("method", ["card", "puid"])
    def test_overage_flag_is_identical(self, db, parity_member, scan_as, method):
        # 14 back-filled meals reach Friday lunch, so Friday dinner is the 15th.
        grant_meals(db, parity_member, 14, MONDAY)
        result = scan_as(method, eastern(2026, 1, 9, 18, 0))
        assert result.outcome is ScanOutcome.CHECKED_IN_OVERAGE
        assert result.attendance.is_overage is True
        assert result.weekly.used == 15

    @pytest.mark.parametrize("method", ["card", "puid"])
    def test_period_resolution_is_identical(self, scan_as, method):
        result = scan_as(method, eastern(2026, 1, 5, 18, 0))
        assert result.period.name == "Dinner"

    @pytest.mark.parametrize("method", ["card", "puid"])
    def test_outside_service_is_identical(self, scan_as, method):
        assert scan_as(method, eastern(2026, 1, 5, 15, 30)).outcome is ScanOutcome.OUTSIDE_SERVICE

    @pytest.mark.parametrize("method", ["card", "puid"])
    def test_entry_method_is_recorded_for_reporting(self, scan_as, method):
        """Staff want to know how often people forget their cards."""
        result = scan_as(method, eastern(2026, 1, 5, 12, 0))
        expected = EntryMethod.CSN.value if method == "card" else EntryMethod.MANUAL_PUID.value
        assert result.attendance.entry_method == expected
