"""End-to-end tests through the real HTTP surface.

These also render every admin template, so a broken Jinja reference fails here
rather than in front of a club officer.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta

import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.main import app
from app.models import (
    Attendance,
    AttendanceKind,
    AuditLog,
    Credential,
    MealPeriod,
    Member,
    PlanType,
    StaffRole,
    StaffUser,
)
from app.security import hash_password
from app.seeds.meal_periods import default_periods
from app.services import credentials as credential_service
from app.services.periods import local_tz

STAFF_PIN = "1234"


@pytest.fixture
def client(db):
    """A TestClient wired to the same throwaway session the fixtures use."""
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def admin_user(db):
    user = StaffUser(
        username="jo",
        display_name="Jo Steward",
        password_hash=hash_password("correct-horse"),
        role=StaffRole.ADMIN.value,
        is_active=True,
    )
    db.add(user)
    db.commit()
    return user


@pytest.fixture
def signed_in(client, admin_user):
    response = client.post(
        "/login", data={"username": "jo", "password": "correct-horse"}, follow_redirects=False
    )
    assert response.status_code == 303
    return client


@pytest.fixture
def member(db, make_member):
    person = make_member(first_name="Avery", last_name="Chen", puid="905550001")
    credential_service.bind_card(db, person, "04A1B2C3D4E5F601")
    db.commit()
    return person


@pytest.fixture
def closed_service(db):
    """A schedule whose only window opens a few hours from now.

    The club is therefore definitely closed at this instant but does have a next
    meal to name — which is the state the banner's 'Next Meal:' line exists for.
    Anchored to the real clock because the kiosk route reads it directly.
    """
    db.query(MealPeriod).delete()
    opens = datetime.now(local_tz()) + timedelta(hours=3)
    db.add(
        MealPeriod(
            name="Supper",
            weekday=opens.weekday(),
            start_time=opens.time().replace(second=0, microsecond=0),
            end_time=(opens + timedelta(hours=1)).time().replace(second=0, microsecond=0),
            counts_toward_allotment=True,
            is_active=True,
            sort_order=1,
        )
    )
    db.commit()
    return db


@pytest.fixture
def between_meals(db):
    """A schedule with one window that closed an hour ago and one that opens in
    three hours, so right now falls squarely between two meals.

    Anchored to the real clock for the reason closed_service is: the scan
    endpoint reads it directly, and what these tests are about is what a member
    is offered when nothing is open at the instant they tap.
    """
    db.query(MealPeriod).delete()
    now = datetime.now(local_tz())
    closed = now - timedelta(hours=1)
    opens = now + timedelta(hours=3)
    windows = [("Lunch", closed - timedelta(hours=2), closed), ("Dinner", opens, opens + timedelta(hours=2))]
    for name, start, end in windows:
        db.add(
            MealPeriod(
                name=name,
                # Taken from the window's own start, not from today: three hours
                # from now can be tomorrow, and an hour ago can be yesterday.
                weekday=start.weekday(),
                start_time=start.time().replace(second=0, microsecond=0),
                end_time=end.time().replace(second=0, microsecond=0),
                counts_toward_allotment=True,
                is_active=True,
                sort_order=1,
            )
        )
    db.commit()
    return db


@pytest.fixture
def wide_service(db):
    """Replace the schedule with one all-day window per day.

    These tests are about quota and authorization logic, not about what time of
    day the suite happens to run at. Tests that care about the real schedule
    (the 19-meal capacity check) deliberately do not use this.
    """
    db.query(MealPeriod).delete()
    for row in default_periods():
        if row["name"] == "Lunch" or row["name"] == "Brunch":
            row["start_time"] = time(0, 0)
            row["end_time"] = time(23, 59)
            db.add(MealPeriod(**row))
    db.commit()
    return db


class TestHealth:
    def test_healthz_reports_the_database(self, client):
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json()["ok"] is True


class TestKioskPage:
    def test_kiosk_renders(self, client):
        response = client.get("/")
        assert response.status_code == 200
        assert "Tap your TigerCard" in response.text

    def test_kiosk_offers_the_no_card_path(self, client):
        """The typed-ID route must be visible without staff help: the ID box is
        on the page itself, so a member with no card clicks straight into it."""
        page = client.get("/").text
        assert "Enter your Princeton ID" in page
        assert 'id="manualInput"' in page
        assert "noCardBtn" not in page

    def test_kiosk_id_box_accepts_only_a_nine_digit_puid(self, client):
        """Enforced in kiosk.js too, but the markup has to carry the rule for a
        member who reaches the box before the script has loaded."""
        page = client.get("/").text
        assert 'pattern="[0-9]{9}"' in page
        assert 'maxlength="9"' in page

    def test_status_endpoint_answers(self, client):
        body = client.get("/api/status").json()
        assert "serving" in body and "service_date" in body

    def test_status_carries_the_countdown(self, client, wide_service):
        """The banner counts down from a duration, not an end timestamp — a
        kiosk laptop with a wrong clock must not render a nonsense countdown."""
        body = client.get("/api/status").json()
        assert body["serving"] is True
        assert isinstance(body["seconds_remaining"], int)
        assert body["seconds_remaining"] >= 0

    def test_status_has_no_countdown_outside_service(self, client, db):
        db.query(MealPeriod).delete()
        db.commit()
        body = client.get("/api/status").json()
        assert body["serving"] is False
        assert body["seconds_remaining"] is None

    def test_status_names_the_next_meal(self, client, db):
        """Drives 'Next Meal: X in H:MM:SS' when the club is closed."""
        body = client.get("/api/status").json()
        assert body["next_period_name"] in {"Breakfast", "Lunch", "Brunch", "Dinner"}
        assert isinstance(body["seconds_until_next"], int)
        assert body["seconds_until_next"] > 0

    def test_status_has_no_next_meal_without_a_schedule(self, client, db):
        db.query(MealPeriod).delete()
        db.commit()
        body = client.get("/api/status").json()
        assert body["next_period_name"] is None
        assert body["seconds_until_next"] is None

    def test_kiosk_shows_the_meal_banner(self, client, wide_service):
        page = client.get("/").text
        assert 'id="mealBanner"' in page
        assert 'data-serving="true"' in page
        assert "Now serving" in page

    def test_kiosk_banner_names_the_next_meal_when_closed(self, client, closed_service):
        page = client.get("/").text
        assert 'data-serving="false"' in page
        assert "Next Meal: Supper" in page
        assert "Outside Meal Hours" not in page

    def test_kiosk_banner_seeds_the_wait_for_the_next_meal(self, client, closed_service):
        page = client.get("/").text
        assert 'nextPeriodName: "Supper"' in page
        assert "secondsUntilNext: 0\n" not in page

    def test_kiosk_banner_falls_back_when_nothing_is_scheduled(self, client, db):
        """With no schedule at all there is no next meal to name, so the banner
        must not render 'Next Meal: None'."""
        db.query(MealPeriod).delete()
        db.commit()
        page = client.get("/").text
        assert 'data-serving="false"' in page
        assert "Outside Meal Hours" in page
        assert "Next Meal:" not in page

    def test_kiosk_seeds_the_countdown_in_the_markup(self, client, wide_service):
        """Rendered server-side so the first paint is right, rather than
        flashing a placeholder until the first poll lands."""
        page = client.get("/").text
        assert "secondsRemaining:" in page
        assert "secondsRemaining: 0\n" not in page

    def test_kiosk_bar_links_to_enrollment_and_the_admin(self, client):
        page = client.get("/").text
        assert '<a class="bar-link" href="/enroll">Enroll</a>' in page
        assert '<a class="bar-link" href="/admin">Admin</a>' in page

    def test_kiosk_has_no_enrollment_panel(self, client):
        """Enrollment lives on its own page, not layered over check-in."""
        page = client.get("/").text
        assert "memberSearch" not in page
        assert "staffPin" not in page


class TestEnrollPage:
    def test_enroll_page_renders(self, client):
        page = client.get("/enroll")
        assert page.status_code == 200
        assert "Enroll a member" in page.text

    def test_enroll_page_collects_the_new_member_details(self, client):
        """Enrolling is what creates the member, so the form must ask for
        everything that identifies them — not just search for someone."""
        page = client.get("/enroll").text
        for field in ('id="firstName"', 'id="lastName"', 'id="puid"', 'id="classYear"'):
            assert field in page

    def test_enroll_page_keeps_the_replacement_card_path(self, client):
        assert 'id="memberSearch"' in client.get("/enroll").text

    def test_enroll_page_is_reachable_without_signing_in(self, client):
        """Staff use it at the kiosk, gated by the PIN on submit, not a login."""
        assert client.get("/enroll", headers={"accept": "text/html"}).status_code == 200

    def test_card_value_is_prefilled_from_the_query(self, client):
        """Carried across from an unrecognized tap so nobody taps twice."""
        page = client.get("/enroll", params={"value": "A1B2C3D4"}).text
        assert 'value="A1B2C3D4"' in page

    def test_prefill_is_escaped(self, client):
        page = client.get("/enroll", params={"value": '"><script>x</script>'}).text
        assert "<script>x</script>" not in page


class TestScanApi:
    def test_card_scan_checks_in(self, client, member, wide_service):
        response = client.post("/api/scan", json={"value": "04A1B2C3D4E5F601"})
        assert response.status_code == 200
        body = response.json()
        assert body["outcome"] == "checked_in"
        assert body["member"]["full_name"] == "Avery Chen"

    def test_typed_puid_uses_the_same_endpoint_and_shape(self, client, member, wide_service):
        card = client.post("/api/scan", json={"value": "04A1B2C3D4E5F601"}).json()
        typed = client.post(
            "/api/scan", json={"value": "905550001", "credential_type": "manual_puid"}
        ).json()
        assert card.keys() == typed.keys()
        assert typed["member"]["id"] == card["member"]["id"]

    def test_unknown_card_is_reported_with_the_value_echoed_back(self, client):
        body = client.post("/api/scan", json={"value": "NOSUCHCARD"}).json()
        assert body["outcome"] == "unknown_credential"
        # Echoed so the kiosk can enroll without a second tap.
        assert body["submitted_value"] == "NOSUCHCARD"

    def test_a_stale_replayed_timestamp_is_ignored(self, client, member, wide_service):
        """A kiosk with a wrong clock must not file meals under 2019."""
        body = client.post(
            "/api/scan",
            json={"value": "04A1B2C3D4E5F601", "occurred_at": "2019-01-01T12:00:00+00:00"},
        ).json()
        if body["service_date"]:
            assert not body["service_date"].startswith("2019")

    def test_forcing_a_second_meal_needs_authorization(self, client, member, wide_service):
        client.post("/api/scan", json={"value": "04A1B2C3D4E5F601"})
        refused = client.post("/api/scan", json={"value": "04A1B2C3D4E5F601", "force": True})
        assert refused.status_code == 403

        allowed = client.post(
            "/api/scan", json={"value": "04A1B2C3D4E5F601", "force": True, "staff_pin": STAFF_PIN}
        )
        assert allowed.status_code == 200


class TestCheckInAnywayApi:
    """What the kiosk is handed between meals, and what it may send back."""

    def test_a_closed_club_names_the_meals_either_side(self, client, member, between_meals):
        body = client.post("/api/scan", json={"value": "04A1B2C3D4E5F601"}).json()
        assert body["outcome"] == "outside_service"
        offers = {offer["direction"]: offer for offer in body["offers"]}
        assert offers["previous"]["period_name"] == "Lunch"
        assert offers["next"]["period_name"] == "Dinner"
        # A duration, not a timestamp — the kiosk's own clock is not trusted.
        assert 0 < offers["next"]["seconds_away"] <= 3 * 60 * 60

    def test_picking_the_next_meal_records_it(self, client, member, between_meals, db):
        body = client.post(
            "/api/scan", json={"value": "04A1B2C3D4E5F601", "attach": "next"}
        ).json()
        assert body["outcome"] == "checked_in"
        assert body["period_name"] == "Dinner"
        assert "Checked in before Dinner opened" in body["warnings"]
        assert db.query(Attendance).count() == 1

    def test_picking_the_previous_meal_records_it(self, client, member, between_meals):
        body = client.post(
            "/api/scan", json={"value": "04A1B2C3D4E5F601", "attach": "previous"}
        ).json()
        assert body["outcome"] == "checked_in"
        assert body["period_name"] == "Lunch"

    def test_it_needs_no_staff_authorization(self, client, member, between_meals):
        """Unlike forcing a second meal. Being early for dinner is not an
        offence, and fetching staff for it would defeat the point."""
        response = client.post(
            "/api/scan", json={"value": "04A1B2C3D4E5F601", "attach": "next"}
        )
        assert response.status_code == 200

    def test_only_the_two_directions_are_accepted(self, client, member, between_meals):
        response = client.post(
            "/api/scan", json={"value": "04A1B2C3D4E5F601", "attach": "whenever"}
        )
        assert response.status_code == 422

    def test_a_serving_club_offers_nothing_and_ignores_a_choice(
        self, client, member, wide_service
    ):
        body = client.post(
            "/api/scan", json={"value": "04A1B2C3D4E5F601", "attach": "next"}
        ).json()
        assert body["offers"] == []
        assert body["period_name"] in {"Lunch", "Brunch"}  # the window that is open


class TestEnrollment:
    def test_enrollment_requires_staff_authorization(self, client, db, make_member):
        person = make_member()
        response = client.post(
            "/api/enroll",
            json={"member_id": person.id, "value": "04A1B2C3D4E5F602", "staff_pin": "0000"},
        )
        assert response.status_code == 403

    def test_enrollment_with_the_pin_binds_the_card(self, client, db, make_member, wide_service):
        person = make_member(first_name="Robin", last_name="Diallo")
        response = client.post(
            "/api/enroll",
            json={"member_id": person.id, "value": "04A1B2C3D4E5F602", "staff_pin": STAFF_PIN},
        )
        assert response.status_code == 200

        found = client.post("/api/scan", json={"value": "04A1B2C3D4E5F602"}).json()
        assert found["member"]["full_name"] == "Robin Diallo"

    @pytest.mark.parametrize(
        "value",
        [
            "04A1B2C3D4E5F6",  # fourteen digits — a truncated read
            "04A1B2C3D4E5F6012",  # seventeen
            "04A1B2C3D4E5F60G",  # G is not hexadecimal
            "NEWCARD",
        ],
    )
    def test_a_card_that_is_not_a_16_digit_csn_is_refused(
        self, client, db, make_member, value
    ):
        person = make_member()
        response = client.post(
            "/api/enroll",
            json={"member_id": person.id, "value": value, "staff_pin": STAFF_PIN},
        )
        assert response.status_code == 422
        assert "16 hexadecimal digits" in response.json()["detail"]
        assert db.query(Credential).count() == 0

    def test_a_csn_is_accepted_however_the_reader_punctuates_it(
        self, client, db, make_member
    ):
        person = make_member()
        response = client.post(
            "/api/enroll",
            json={
                "member_id": person.id,
                "value": "04a1-b2c3-d4e5-f601",
                "staff_pin": STAFF_PIN,
            },
        )
        assert response.status_code == 200
        assert db.query(Credential).one().value == "04A1B2C3D4E5F601"

    def test_member_search_backs_the_enrollment_screen(self, client, member):
        results = client.get("/api/members/search", params={"q": "Chen"}).json()
        assert any(r["puid"] == "905550001" for r in results)

    def test_search_ignores_one_character_terms(self, client, member):
        assert client.get("/api/members/search", params={"q": "C"}).json() == []


class TestNewMemberEnrollment:
    """Enrolling somebody not yet on file — the ordinary case."""

    payload = {
        "first_name": "Sam",
        "last_name": "Okafor",
        "puid": "905559999",
        "netid": "sokafor",
        "class_year": 2028,
        "plan_type": "plan_14",
        "value": "04A1B2C3D4E5F603",
        "staff_pin": STAFF_PIN,
    }

    def test_it_creates_the_member_and_links_the_card(self, client, db, wide_service):
        response = client.post("/api/enroll/new", json=self.payload)
        assert response.status_code == 200
        body = response.json()
        assert body["member"]["full_name"] == "Sam Okafor"
        assert body["member"]["class_year"] == 2028
        assert body["member"]["plan_type"] == "plan_14"

        # The point of doing both in one step: they can eat immediately.
        scanned = client.post("/api/scan", json={"value": "04A1B2C3D4E5F603"}).json()
        assert scanned["member"]["id"] == body["member"]["id"]

    def test_it_requires_staff_authorization(self, client, db):
        refused = client.post("/api/enroll/new", json={**self.payload, "staff_pin": "0000"})
        assert refused.status_code == 403
        assert db.query(Member).count() == 0

    def test_class_year_is_optional(self, client, db):
        body = {**self.payload, "class_year": None}
        assert client.post("/api/enroll/new", json=body).status_code == 200

    def test_a_duplicate_puid_is_refused_and_names_the_holder(
        self, client, db, make_member
    ):
        make_member(first_name="Avery", last_name="Chen", puid="905559999")
        response = client.post("/api/enroll/new", json=self.payload)
        assert response.status_code == 409
        assert "Avery Chen" in response.json()["detail"]

    def test_a_card_already_in_use_is_refused_rather_than_moved(
        self, client, db, member
    ):
        """A card that scans as someone else is a mis-tap, not a transfer —
        silently reassigning it would strand the first member."""
        response = client.post(
            "/api/enroll/new", json={**self.payload, "value": "04A1B2C3D4E5F601"}
        )
        assert response.status_code == 409
        assert "Avery Chen" in response.json()["detail"]
        assert db.query(Member).filter(Member.puid == "905559999").count() == 0

    def test_an_unreadable_photo_leaves_no_half_created_member(self, client, db):
        """The member and their card are one transaction. A photo that fails to
        decode must take the whole thing down, or staff retry the enrollment
        and hit their own duplicate-PUID error."""
        response = client.post(
            "/api/enroll/new",
            json={
                **self.payload,
                "photo_data_url": "data:image/jpeg;base64,bm90YW5pbWFnZQ==",
            },
        )
        assert response.status_code == 400
        assert db.query(Member).count() == 0
        assert db.query(Credential).count() == 0

        # The retry, without the bad photo, works.
        assert client.post("/api/enroll/new", json=self.payload).status_code == 200

    @pytest.mark.parametrize("year", [1234, 987, 9999])
    def test_a_class_year_outside_the_allowed_range_reads_as_a_sentence(
        self, client, db, year
    ):
        """Schema rejections land in front of staff too.

        The default 422 body puts a list of error objects in `detail`, which the
        enrollment page printed as "[object Object]" — the one thing that cannot
        be acted on. A mistyped year is an ordinary typo and has to say so.
        """
        response = client.post("/api/enroll/new", json={**self.payload, "class_year": year})
        assert response.status_code == 422
        detail = response.json()["detail"]
        assert isinstance(detail, str)
        assert "Class year" in detail
        assert db.query(Member).count() == 0

    def test_a_missing_staff_pin_names_the_field_it_wants(self, client, db):
        payload = {k: v for k, v in self.payload.items() if k != "staff_pin"}
        response = client.post("/api/enroll/new", json=payload)
        assert response.status_code == 422
        assert response.json()["detail"] == "Staff PIN is required."
        assert db.query(Member).count() == 0

    @pytest.mark.parametrize("puid", ["90555999", "9055599999", "90555999X", "905-55-999"])
    def test_a_puid_that_is_not_nine_digits_is_refused(self, client, db, puid):
        response = client.post("/api/enroll/new", json={**self.payload, "puid": puid})
        assert response.status_code == 422
        assert "nine digits" in response.json()["detail"]
        assert db.query(Member).count() == 0

    @pytest.mark.parametrize("value", ["04A1B2C3D4E5F6", "04A1B2C3D4E5F60G"])
    def test_a_card_that_is_not_a_16_digit_csn_leaves_no_member_behind(
        self, client, db, value
    ):
        """The card is checked before the member is written, so a bad tap does
        not create somebody staff then have to hunt down and delete."""
        response = client.post("/api/enroll/new", json={**self.payload, "value": value})
        assert response.status_code == 422
        assert "16 hexadecimal digits" in response.json()["detail"]
        assert db.query(Member).count() == 0

    def test_the_enrollment_is_audited(self, client, db):
        client.post("/api/enroll/new", json=self.payload)
        entry = db.query(AuditLog).filter(AuditLog.action == "member.enrolled").one()
        assert entry.detail["puid"] == "905559999"
        assert entry.detail["netid"] == "sokafor"
        assert entry.detail["class_year"] == 2028

    def test_the_netid_is_stored_on_the_member(self, client, db, wide_service):
        response = client.post("/api/enroll/new", json=self.payload)
        assert response.status_code == 200
        assert db.query(Member).one().netid == "sokafor"
        assert response.json()["member"]["netid"] == "sokafor"

    def test_a_netid_typed_in_capitals_is_stored_lowercase(self, client, db):
        """One spelling on file, so a roster from anywhere else can be matched
        against it — the same rule guest NetIDs follow."""
        client.post("/api/enroll/new", json={**self.payload, "netid": " SOkafor "})
        assert db.query(Member).one().netid == "sokafor"

    @pytest.mark.parametrize("netid", ["", "   "])
    def test_a_member_without_a_netid_is_refused(self, client, db, netid):
        """Required here, unlike a guest's: every member is a Princeton
        affiliate, so there is no 'has none' case to make room for."""
        response = client.post("/api/enroll/new", json={**self.payload, "netid": netid})
        assert response.status_code == 422
        assert "NetID" in response.json()["detail"]
        assert db.query(Member).count() == 0

    def test_a_missing_netid_field_reads_as_a_sentence(self, client, db):
        payload = {k: v for k, v in self.payload.items() if k != "netid"}
        response = client.post("/api/enroll/new", json=payload)
        assert response.status_code == 422
        assert isinstance(response.json()["detail"], str)
        assert db.query(Member).count() == 0

    @pytest.mark.parametrize("netid", ["9sam", "s", "sam-okafor", "waytoolongnetid"])
    def test_a_malformed_netid_is_refused(self, client, db, netid):
        response = client.post("/api/enroll/new", json={**self.payload, "netid": netid})
        assert response.status_code == 422
        assert "NetID" in response.json()["detail"]
        assert db.query(Member).count() == 0

    def test_a_duplicate_netid_is_refused_and_names_the_holder(
        self, client, db, make_member
    ):
        """A NetID identifies one person, so a second claim on it is the same
        mistake as a duplicate PUID and gets the same answer."""
        existing = make_member(first_name="Avery", last_name="Chen", puid="905550002")
        existing.netid = "sokafor"
        db.commit()

        response = client.post("/api/enroll/new", json=self.payload)
        assert response.status_code == 409
        assert "Avery Chen" in response.json()["detail"]
        assert db.query(Member).filter(Member.puid == "905559999").count() == 0

    def test_members_can_be_found_by_netid(self, client, db):
        client.post("/api/enroll/new", json=self.payload)
        results = client.get("/api/members/search", params={"q": "sokafor"}).json()
        assert [r["netid"] for r in results] == ["sokafor"]


class TestGuestApi:
    def guest(self, member, first="Kim", last="Adeyemi", netid="kadeyemi", **extra):
        return {
            "member_id": member.id,
            "guest_first_name": first,
            "guest_last_name": last,
            "guest_netid": netid,
            **extra,
        }

    def test_guest_blocked_at_quota_then_released_by_override(self, client, member, wide_service):
        for first, last, netid in (("Sam", "Ortiz", "sortiz"), ("Jess", "Nakamura", "jn")):
            body = client.post(
                "/api/guest", json=self.guest(member, first, last, netid)
            ).json()
            assert body["outcome"] == "guest_recorded"

        blocked = client.post("/api/guest", json=self.guest(member)).json()
        assert blocked["outcome"] == "guest_quota_exceeded"

        released = client.post(
            "/api/guest",
            json=self.guest(
                member, staff_pin=STAFF_PIN, override_reason="prospective member"
            ),
        ).json()
        assert released["outcome"] == "guest_recorded"

    def test_override_with_a_bad_pin_is_refused(self, client, member, wide_service):
        response = client.post(
            "/api/guest",
            json=self.guest(member, staff_pin="9999", override_reason="nope"),
        )
        assert response.status_code == 403

    def test_the_guests_name_and_netid_are_stored(self, client, db, member, wide_service):
        response = client.post("/api/guest", json=self.guest(member))
        assert response.status_code == 200

        row = db.query(Attendance).filter_by(kind=AttendanceKind.GUEST.value).one()
        assert row.member_id == member.id  # the host
        assert (row.guest_first_name, row.guest_last_name) == ("Kim", "Adeyemi")
        assert row.guest_netid == "kadeyemi"

    def test_a_netid_typed_in_capitals_is_stored_lowercase(
        self, client, db, member, wide_service
    ):
        client.post("/api/guest", json=self.guest(member, netid="KAdeyemi"))
        row = db.query(Attendance).filter_by(kind=AttendanceKind.GUEST.value).one()
        assert row.guest_netid == "kadeyemi"

    @pytest.mark.parametrize(
        "missing", [{"guest_first_name": ""}, {"guest_last_name": "  "}]
    )
    def test_a_guest_without_a_full_name_is_refused(
        self, client, db, member, wide_service, missing
    ):
        response = client.post("/api/guest", json={**self.guest(member), **missing})
        assert response.status_code == 422
        assert "first and last name" in response.json()["detail"]
        assert db.query(Attendance).count() == 0

    def test_a_guest_with_neither_a_netid_nor_a_reason_is_refused(
        self, client, db, member, wide_service
    ):
        response = client.post("/api/guest", json=self.guest(member, netid=" "))
        assert response.status_code == 422
        assert "NetID" in response.json()["detail"]
        assert db.query(Attendance).count() == 0

    def test_a_guest_with_no_netid_is_recorded_once_a_reason_is_given(
        self, client, db, member, wide_service
    ):
        """The visiting parent, the alum, the prospective member's sibling."""
        response = client.post(
            "/api/guest",
            json=self.guest(member, netid="", guest_netid_reason="visiting parent"),
        )
        assert response.status_code == 200
        assert response.json()["outcome"] == "guest_recorded"

        row = db.query(Attendance).filter_by(kind=AttendanceKind.GUEST.value).one()
        assert row.guest_netid is None
        assert row.guest_netid_reason == "visiting parent"

    def test_a_reason_alongside_a_netid_is_dropped(self, client, db, member, wide_service):
        """Exactly one of the two ever lands on a row."""
        client.post(
            "/api/guest", json=self.guest(member, guest_netid_reason="visiting parent")
        )
        row = db.query(Attendance).filter_by(kind=AttendanceKind.GUEST.value).one()
        assert row.guest_netid == "kadeyemi"
        assert row.guest_netid_reason is None

    def test_a_blank_reason_does_not_stand_in_for_a_netid(
        self, client, db, member, wide_service
    ):
        response = client.post(
            "/api/guest", json=self.guest(member, netid="", guest_netid_reason="   ")
        )
        assert response.status_code == 422
        assert db.query(Attendance).count() == 0

    @pytest.mark.parametrize("netid", ["9kim", "k", "kim-adeyemi", "waytoolongnetid"])
    def test_a_malformed_netid_is_refused(self, client, db, member, wide_service, netid):
        """Caught at the popup, where somebody is standing there to fix it."""
        response = client.post("/api/guest", json=self.guest(member, netid=netid))
        assert response.status_code == 422
        assert "NetID" in response.json()["detail"]
        assert db.query(Attendance).count() == 0

    def test_the_refusal_is_a_sentence_the_kiosk_can_show(
        self, client, member, wide_service
    ):
        """The popup prints `detail` straight into its error line, so it has to
        be a string — not pydantic's list of field errors."""
        response = client.post("/api/guest", json=self.guest(member, netid=""))
        assert isinstance(response.json()["detail"], str)


class TestAlumniApi:
    """An alumni meal is attached to nobody, so the popup is the only chance
    anyone gets to record who ate and how to reach them."""

    def alum(self, **extra):
        return {
            "first_name": "Casey",
            "last_name": "Whitman",
            "class_year": 2014,
            "email": "casey@example.com",
            **extra,
        }

    def test_it_records_the_meal_against_no_member(self, client, db, wide_service):
        response = client.post("/api/alumni", json=self.alum())
        assert response.status_code == 200
        body = response.json()
        assert body["outcome"] == "alumni_recorded"
        assert body["member"] is None

        row = db.query(Attendance).filter_by(kind=AttendanceKind.ALUMNI.value).one()
        assert row.member_id is None
        assert (row.alumni_first_name, row.alumni_last_name) == ("Casey", "Whitman")
        assert row.alumni_class_year == 2014
        assert row.alumni_email == "casey@example.com"

    def test_the_response_names_the_alum_so_the_kiosk_can_show_it(
        self, client, wide_service
    ):
        """With no member on the response, the result screen would otherwise
        tell an alum their card was not recognized."""
        body = client.post("/api/alumni", json=self.alum()).json()
        assert body["alumni"]["full_name"] == "Casey Whitman"
        assert body["alumni"]["class_year"] == 2014

    def test_no_staff_pin_is_needed(self, client, wide_service):
        """Same reasoning as a guest meal: there is no queue-side login here."""
        assert client.post("/api/alumni", json=self.alum()).status_code == 200

    def test_a_phone_number_alone_is_enough(self, client, db, wide_service):
        response = client.post(
            "/api/alumni", json=self.alum(email="", phone="609-555-1234")
        )
        assert response.status_code == 200
        row = db.query(Attendance).filter_by(kind=AttendanceKind.ALUMNI.value).one()
        assert row.alumni_email is None
        assert row.alumni_phone == "6095551234"

    def test_an_email_alone_is_enough(self, client, db, wide_service):
        assert client.post("/api/alumni", json=self.alum(phone="")).status_code == 200
        row = db.query(Attendance).filter_by(kind=AttendanceKind.ALUMNI.value).one()
        assert row.alumni_phone is None

    def test_neither_contact_detail_is_refused(self, client, db, wide_service):
        response = client.post("/api/alumni", json=self.alum(email="", phone="  "))
        assert response.status_code == 422
        assert "email address or a phone number" in response.json()["detail"]
        assert db.query(Attendance).count() == 0

    @pytest.mark.parametrize("missing", [{"first_name": ""}, {"last_name": "  "}])
    def test_an_alum_without_a_full_name_is_refused(
        self, client, db, wide_service, missing
    ):
        response = client.post("/api/alumni", json=self.alum(**missing))
        assert response.status_code == 422
        assert "first and last name" in response.json()["detail"]
        assert db.query(Attendance).count() == 0

    def test_a_missing_class_year_is_refused(self, client, db, wide_service):
        response = client.post("/api/alumni", json=self.alum(class_year=None))
        assert response.status_code == 422
        assert "class year" in response.json()["detail"]
        assert db.query(Attendance).count() == 0

    @pytest.mark.parametrize("email", ["casey", "casey@", "casey@example"])
    def test_a_malformed_email_is_refused(self, client, db, wide_service, email):
        response = client.post("/api/alumni", json=self.alum(email=email))
        assert response.status_code == 422
        assert db.query(Attendance).count() == 0

    @pytest.mark.parametrize("phone", ["555-1234", "60955", "not a phone"])
    def test_a_malformed_phone_number_is_refused(self, client, db, wide_service, phone):
        response = client.post("/api/alumni", json=self.alum(email="", phone=phone))
        assert response.status_code == 422
        assert db.query(Attendance).count() == 0

    def test_every_refusal_is_a_sentence_the_kiosk_can_show(
        self, client, wide_service
    ):
        response = client.post("/api/alumni", json=self.alum(email="", phone=""))
        assert isinstance(response.json()["detail"], str)

    def test_a_netid_is_stored_when_one_is_given(self, client, db, wide_service):
        response = client.post("/api/alumni", json=self.alum(netid=" CWhitman "))
        assert response.status_code == 200
        row = db.query(Attendance).filter_by(kind=AttendanceKind.ALUMNI.value).one()
        assert row.alumni_netid == "cwhitman"
        assert response.json()["alumni"]["netid"] == "cwhitman"

    def test_the_netid_is_optional(self, client, db, wide_service):
        """Unlike a member's, and unlike a guest's — an alum whose NetID lapsed
        is not held at the door, and owes no reason in its place."""
        response = client.post("/api/alumni", json=self.alum())
        assert response.status_code == 200
        row = db.query(Attendance).filter_by(kind=AttendanceKind.ALUMNI.value).one()
        assert row.alumni_netid is None

    def test_a_netid_does_not_stand_in_for_a_contact_detail(
        self, client, db, wide_service
    ):
        """It identifies the alum; it does not reach them. The email-or-phone
        rule is untouched by it."""
        response = client.post(
            "/api/alumni", json=self.alum(email="", phone="", netid="cwhitman")
        )
        assert response.status_code == 422
        assert "email address or a phone number" in response.json()["detail"]
        assert db.query(Attendance).count() == 0

    @pytest.mark.parametrize("netid", ["9casey", "c", "casey-whitman", "waytoolongnetid"])
    def test_a_malformed_netid_is_refused(self, client, db, wide_service, netid):
        """Optional does not mean unchecked: a typo is still worth catching
        while the alum is standing there."""
        response = client.post("/api/alumni", json=self.alum(netid=netid))
        assert response.status_code == 422
        assert "NetID" in response.json()["detail"]
        assert db.query(Attendance).count() == 0

    def test_an_alumni_meal_can_be_undone_from_the_kiosk(self, client, db, wide_service):
        recorded = client.post("/api/alumni", json=self.alum()).json()
        undone = client.post("/api/undo", json={"attendance_id": recorded["attendance_id"]})
        assert undone.status_code == 200
        row = db.query(Attendance).filter_by(kind=AttendanceKind.ALUMNI.value).one()
        assert row.voided_at is not None

    def test_it_shows_up_on_the_dashboard_and_in_the_export(
        self, signed_in, wide_service
    ):
        """The row has no member page to be found from, so the day view and the
        CSV are the only places it can be read."""
        signed_in.post(
            "/api/alumni", json=self.alum(phone="609-555-1234", netid="cwhitman")
        )

        page = signed_in.get("/admin").text
        assert "Casey Whitman" in page
        assert "Class of 2014" in page
        assert "cwhitman" in page

        csv = signed_in.get("/admin/reports/daily.csv").text
        assert "Casey Whitman" in csv
        assert "casey@example.com" in csv
        assert "6095551234" in csv
        assert "cwhitman" in csv


class TestAdminAuth:
    def test_admin_requires_sign_in(self, client):
        """A browser gets sent to the login form; an API client gets a 401."""
        browser = client.get(
            "/admin", headers={"accept": "text/html"}, follow_redirects=False
        )
        assert browser.status_code == 303
        assert browser.headers["location"] == "/login"

        assert client.get("/admin", headers={"accept": "application/json"}).status_code == 401

    def test_bad_password_is_rejected(self, client, admin_user):
        response = client.post("/login", data={"username": "jo", "password": "wrong"})
        assert response.status_code == 401

    def test_sign_in_opens_the_dashboard(self, signed_in):
        response = signed_in.get("/admin")
        assert response.status_code == 200


class TestAdminPages:
    """Renders every template. A typo in a Jinja expression fails right here."""

    @pytest.mark.parametrize(
        "path",
        ["/admin", "/admin/members", "/admin/analytics", "/admin/reports",
         "/admin/schedule", "/admin/settings", "/admin/audit"],
    )
    def test_page_renders(self, signed_in, member, path):
        response = signed_in.get(path)
        assert response.status_code == 200, response.text[:400]

    def test_member_detail_renders(self, signed_in, member):
        response = signed_in.get(f"/admin/members/{member.id}")
        assert response.status_code == 200
        assert "Avery Chen" in response.text

    def test_schedule_page_reports_nineteen_weekly_meals(self, signed_in):
        assert "serves <strong>19</strong> meals per week" in signed_in.get(
            "/admin/schedule"
        ).text

    def test_the_admin_form_stores_a_netid(self, signed_in, db):
        signed_in.post(
            "/admin/members",
            data={
                "first_name": "Noel",
                "last_name": "Ward",
                "puid": "905559876",
                "netid": " NWard ",
                "class_year": "2029",
                "plan_type": PlanType.PLAN_19.value,
                "status_value": "active",
            },
        )
        assert db.query(Member).filter_by(puid="905559876").one().netid == "nward"

    def test_the_admin_form_leaves_a_blank_netid_empty(self, signed_in, db):
        """Blank is allowed here and refused at the kiosk on purpose: this form
        is also how a member who predates the column gets edited."""
        signed_in.post(
            "/admin/members",
            data={
                "first_name": "Noel",
                "last_name": "Ward",
                "puid": "905559876",
                "netid": "",
                "class_year": "",
                "plan_type": PlanType.PLAN_19.value,
                "status_value": "active",
            },
        )
        assert db.query(Member).filter_by(puid="905559876").one().netid is None

    def test_editing_a_member_can_add_a_netid_later(self, signed_in, db, member):
        """The backfill path for everyone enrolled before the column existed."""
        assert member.netid is None
        signed_in.post(
            f"/admin/members/{member.id}",
            data={
                "first_name": member.first_name,
                "last_name": member.last_name,
                "puid": member.puid,
                "netid": "achen",
                "class_year": "2028",
                "plan_type": member.plan_type,
                "status_value": member.status,
                "notes": "",
            },
        )
        db.refresh(member)
        assert member.netid == "achen"

    def test_a_netid_another_member_holds_is_refused_on_edit(
        self, signed_in, db, member, make_member
    ):
        other = make_member(first_name="Uma", last_name="Novak", puid="905554444")
        other.netid = "unovak"
        db.commit()

        response = signed_in.post(
            f"/admin/members/{member.id}",
            data={
                "first_name": member.first_name,
                "last_name": member.last_name,
                "puid": member.puid,
                "netid": "unovak",
                "class_year": "",
                "plan_type": member.plan_type,
                "status_value": member.status,
                "notes": "",
            },
        )
        assert response.status_code == 409
        assert "Uma Novak" in response.json()["detail"]
        db.refresh(member)
        assert member.netid is None  # left exactly as it was, not half-edited

    def test_a_member_keeps_their_own_netid_across_an_edit(self, signed_in, db, member):
        """The uniqueness check must not treat a member as their own clash."""
        member.netid = "achen"
        db.commit()
        response = signed_in.post(
            f"/admin/members/{member.id}",
            data={
                "first_name": member.first_name,
                "last_name": member.last_name,
                "puid": member.puid,
                "netid": "achen",
                "class_year": "",
                "plan_type": PlanType.PLAN_14.value,
                "status_value": member.status,
                "notes": "",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        db.refresh(member)
        assert member.netid == "achen"
        assert member.plan_type == PlanType.PLAN_14.value

    def test_a_malformed_netid_is_refused_by_the_admin_form(self, signed_in, db, member):
        response = signed_in.post(
            f"/admin/members/{member.id}",
            data={
                "first_name": member.first_name,
                "last_name": member.last_name,
                "puid": member.puid,
                "netid": "9avery",
                "class_year": "",
                "plan_type": member.plan_type,
                "status_value": member.status,
                "notes": "",
            },
        )
        assert response.status_code == 422

    def test_members_can_be_searched_by_netid(self, signed_in, db, member):
        member.netid = "achen"
        db.commit()
        page = signed_in.get("/admin/members", params={"q": "achen"}).text
        assert "Avery Chen" in page

    def test_creating_a_member_rejects_a_duplicate_puid(self, signed_in, member):
        response = signed_in.post(
            "/admin/members",
            data={
                "first_name": "Other",
                "last_name": "Person",
                "puid": "905550001",
                "class_year": "2029",
                "plan_type": PlanType.PLAN_14.value,
                "status_value": "active",
            },
        )
        assert response.status_code == 409


class TestAnalytics:
    @pytest.fixture
    def cohort(self, db, make_member):
        """Three members across two class years, so grouping has something to
        group and sorting has something to reorder."""
        return [
            make_member(first_name="Ada", last_name="Zhang", class_year=2027,
                        puid="905551111"),
            make_member(first_name="Ben", last_name="Alvarez", class_year=2028,
                        puid="905552222"),
            make_member(first_name="Cy", last_name="Mbeki", class_year=2028,
                        puid="905553333"),
        ]

    def test_every_member_appears(self, signed_in, cohort):
        page = signed_in.get("/admin/analytics").text
        for person in cohort:
            assert person.full_name in page

    def test_it_breaks_down_by_class_year(self, signed_in, cohort):
        page = signed_in.get("/admin/analytics").text
        assert "By class year" in page
        assert "2027" in page and "2028" in page

    @pytest.mark.parametrize(
        "sort",
        ["name", "class_year", "plan", "status", "card", "meals", "per_week",
         "utilization", "days", "guests", "overages", "last_seen"],
    )
    @pytest.mark.parametrize("direction", ["asc", "desc"])
    def test_every_sort_column_works(self, signed_in, cohort, sort, direction):
        response = signed_in.get(
            "/admin/analytics", params={"sort": sort, "dir": direction}
        )
        assert response.status_code == 200, response.text[:400]

    def test_sorting_by_class_year_reorders_the_table(self, signed_in, cohort):
        def first_of(direction):
            table = signed_in.get(
                "/admin/analytics", params={"sort": "class_year", "dir": direction}
            ).text.split("<h2>Members</h2>")[1]
            return min(
                ("Ada Zhang", "Ben Alvarez"), key=lambda name: table.index(name)
            )

        assert first_of("asc") == "Ada Zhang"  # class of 2027 first
        assert first_of("desc") == "Ben Alvarez"  # class of 2028 first

    @pytest.mark.parametrize("direction", ["asc", "desc"])
    def test_members_with_no_class_year_sink_to_the_bottom_either_way(
        self, signed_in, cohort, make_member, direction
    ):
        """"No year on file" is not the highest class year. It belongs last
        whichever way the column points, or every descending sort opens with
        the rows that have nothing to say."""
        make_member(first_name="Uma", last_name="Novak", class_year=None,
                    puid="905554444")
        table = signed_in.get(
            "/admin/analytics", params={"sort": "class_year", "dir": direction}
        ).text.split("<h2>Members</h2>")[1]
        assert table.index("Uma Novak") > table.index("Ada Zhang")

    @pytest.mark.parametrize("direction", ["asc", "desc"])
    def test_members_who_never_ate_sink_to_the_bottom_either_way(
        self, signed_in, db, member, make_member, wide_service, direction
    ):
        make_member(first_name="Uma", last_name="Novak", puid="905554444")
        signed_in.post("/api/scan", json={"value": "04A1B2C3D4E5F601"})
        table = signed_in.get(
            "/admin/analytics", params={"sort": "last_seen", "dir": direction}
        ).text.split("<h2>Members</h2>")[1]
        assert table.index("Uma Novak") > table.index("Avery Chen")

    def test_an_unknown_sort_column_falls_back_rather_than_erroring(
        self, signed_in, cohort
    ):
        assert signed_in.get(
            "/admin/analytics", params={"sort": "'; DROP TABLE members;--"}
        ).status_code == 200

    def test_filtering_by_class_year_narrows_the_table(self, signed_in, cohort):
        page = signed_in.get("/admin/analytics", params={"class_year": "2027"}).text
        table = page.split('<h2>Members</h2>')[1]
        assert "Ada Zhang" in table
        assert "Ben Alvarez" not in table

    def test_filtering_by_name_narrows_the_table(self, signed_in, cohort):
        table = signed_in.get(
            "/admin/analytics", params={"q": "Mbeki"}
        ).text.split('<h2>Members</h2>')[1]
        assert "Cy Mbeki" in table
        assert "Ada Zhang" not in table

    def test_a_backwards_date_range_is_swapped_not_rejected(self, signed_in, cohort):
        response = signed_in.get(
            "/admin/analytics", params={"start": "2026-05-01", "end": "2026-04-01"}
        )
        assert response.status_code == 200
        assert "1 Apr 2026" in response.text

    def test_meals_are_counted_in_the_window(self, signed_in, db, member, wide_service):
        signed_in.post("/api/scan", json={"value": "04A1B2C3D4E5F601"})
        table = signed_in.get("/admin/analytics").text.split('<h2>Members</h2>')[1]
        assert "Avery Chen" in table

    def test_csv_export_matches_the_filters(self, signed_in, cohort):
        response = signed_in.get("/admin/analytics.csv", params={"class_year": "2027"})
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")
        body = response.text
        assert "905551111" in body
        assert "905552222" not in body

    def test_analytics_requires_sign_in(self, client):
        assert client.get(
            "/admin/analytics", headers={"accept": "application/json"}
        ).status_code == 401


class TestReportExports:
    @pytest.mark.parametrize(
        "name", ["weekly-usage", "overages", "guests", "enrollment-gaps", "daily"]
    )
    def test_csv_downloads(self, signed_in, member, name):
        response = signed_in.get(f"/admin/reports/{name}.csv")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")
        assert "attachment;" in response.headers["content-disposition"]

    def test_unknown_report_is_a_404(self, signed_in):
        assert signed_in.get("/admin/reports/nonsense.csv").status_code == 404

    def test_weekly_usage_csv_lists_every_member(self, signed_in, member):
        body = signed_in.get("/admin/reports/weekly-usage.csv").text
        assert "905550001" in body
        assert body.splitlines()[0].startswith("last_name,first_name,puid")

    def test_enrollment_gaps_lists_members_without_a_card(self, signed_in, make_member):
        make_member(first_name="Noel", last_name="Ward", puid="905559876")
        body = signed_in.get("/admin/reports/enrollment-gaps.csv").text
        assert "905559876" in body
