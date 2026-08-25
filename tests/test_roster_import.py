"""The roster CSV importer, at the service layer and through the admin screen.

The importer's whole reason to exist is that the club has member details long
before it has card serials, so most of what is asserted here is about what it
*doesn't* do: never touches credentials, never clears a value a blank cell
leaves out, never writes anything before staff confirm.
"""
from __future__ import annotations

from html import unescape

import pytest
from sqlalchemy import select

from fastapi.testclient import TestClient

from app.db import get_db
from app.main import app
from app.models import (
    AuditLog,
    Credential,
    Member,
    MemberStatus,
    PlanType,
    StaffRole,
    StaffUser,
)
from app.security import hash_password
from app.services import credentials as credential_service
from app.services import roster_import

HEADER = "first_name,last_name,puid,netid,class_year,plan_type,status\n"


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def staff_of(db, client):
    """Sign in as a staff user of the given role, and hand back the client."""

    def _sign_in(role: str) -> TestClient:
        db.add(
            StaffUser(
                username="jo",
                display_name="Jo Steward",
                password_hash=hash_password("correct-horse"),
                role=role,
                is_active=True,
            )
        )
        db.commit()
        response = client.post(
            "/login",
            data={"username": "jo", "password": "correct-horse"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        return client

    return _sign_in


@pytest.fixture
def signed_in(staff_of):
    return staff_of(StaffRole.ADMIN.value)


def csv_of(*rows: str) -> str:
    return HEADER + "".join(row if row.endswith("\n") else row + "\n" for row in rows)


class TestParsing:
    def test_a_minimal_file_plans_one_creation_per_row(self, db):
        plan = roster_import.plan(
            db,
            "first_name,last_name,puid\n"
            "Avery,Chen,905550001\n"
            "Sam,Okafor,905550002\n",
        )
        assert len(plan.creates) == 2
        assert [row.name for row in plan.creates] == ["Avery Chen", "Sam Okafor"]
        assert plan.errors == []

    def test_headers_are_matched_loosely(self, db):
        plan = roster_import.plan(
            db, "First Name,Last Name,PUID,NetID,Class Year\nAvery,Chen,905550001,ac9981,2028\n"
        )
        row = plan.creates[0]
        assert row.values["netid"] == "ac9981"
        assert row.values["class_year"] == 2028

    def test_unknown_columns_are_reported_not_fatal(self, db):
        plan = roster_import.plan(
            db, "first_name,last_name,puid,dorm\nAvery,Chen,905550001,Blair\n"
        )
        assert plan.ignored_columns == ["dorm"]
        assert len(plan.creates) == 1

    def test_a_missing_required_column_is_fatal(self, db):
        with pytest.raises(roster_import.RosterImportError, match="puid"):
            roster_import.plan(db, "first_name,last_name\nAvery,Chen\n")

    def test_blank_lines_are_skipped(self, db):
        plan = roster_import.plan(
            db, "first_name,last_name,puid\nAvery,Chen,905550001\n\n,,\n"
        )
        assert len(plan.rows) == 1

    def test_defaults_fill_in_for_blank_optional_cells(self, db):
        plan = roster_import.plan(db, csv_of("Avery,Chen,905550001,,,,"))
        values = plan.creates[0].values
        assert values["plan_type"] == PlanType.PLAN_19.value
        assert values["status"] == MemberStatus.ACTIVE.value
        assert values["netid"] is None
        assert values["class_year"] is None

    def test_plan_and_status_spellings_are_normalized(self, db):
        plan = roster_import.plan(db, csv_of("Avery,Chen,905550001,,2028,14,Abroad"))
        values = plan.creates[0].values
        assert values["plan_type"] == PlanType.PLAN_14.value
        assert values["status"] == MemberStatus.ABROAD.value

    def test_excel_byte_order_mark_and_cp1252_both_decode(self):
        assert roster_import.decode(b"\xef\xbb\xbffirst_name\n").startswith("first_name")
        assert "Zo\xeb" in roster_import.decode(b"first_name\nZo\xeb\n")


class TestRowProblems:
    @pytest.mark.parametrize(
        "row,fragment",
        [
            (",Chen,905550001,,,,", "First name is blank"),
            ("Avery,,905550001,,,,", "Last name is blank"),
            ("Avery,Chen,,,,,", "PUID is blank"),
            ("Avery,Chen,90555,,,,", "nine digits"),
            ("Avery,Chen,905550001,9bad,,,", "NetID"),
            ("Avery,Chen,905550001,,two thousand,,", "class year"),
            ("Avery,Chen,905550001,,2028,platinum,", "meal plan"),
            ("Avery,Chen,905550001,,2028,,graduated", "status"),
        ],
    )
    def test_bad_cells_are_row_errors(self, db, row, fragment):
        plan = roster_import.plan(db, csv_of(row))
        assert len(plan.errors) == 1
        assert fragment in " ".join(plan.errors[0].errors)

    def test_a_bad_row_does_not_stop_the_good_ones(self, db):
        plan = roster_import.plan(
            db, csv_of("Avery,Chen,905550001,,,,", "Sam,Okafor,nope,,,,")
        )
        assert len(plan.creates) == 1
        assert len(plan.errors) == 1

    def test_a_puid_repeated_in_the_file_is_an_error(self, db):
        plan = roster_import.plan(
            db, csv_of("Avery,Chen,905550001,,,,", "Avery,Chen,905550001,,,,")
        )
        assert len(plan.creates) == 1
        assert "line 2" in plan.errors[0].errors[0]

    def test_a_netid_repeated_in_the_file_is_an_error(self, db):
        plan = roster_import.plan(
            db, csv_of("Avery,Chen,905550001,ac9981,,,", "Sam,Okafor,905550002,ac9981,,,")
        )
        assert len(plan.creates) == 1
        assert "line 2" in " ".join(plan.errors[0].errors)

    def test_a_netid_held_by_another_member_is_an_error(self, db, make_member):
        holder = make_member(first_name="Sam", last_name="Okafor", puid="905550002")
        holder.netid = "so1234"
        db.commit()

        plan = roster_import.plan(db, csv_of("Avery,Chen,905550001,so1234,,,"))
        assert "already on file for Sam Okafor" in " ".join(plan.errors[0].errors)

    def test_a_member_keeping_their_own_netid_is_not_a_conflict(self, db, make_member):
        member = make_member(first_name="Avery", last_name="Chen", puid="905550001")
        member.netid = "ac9981"
        db.commit()

        plan = roster_import.plan(db, csv_of("Avery,Chen,905550001,ac9981,2027,plan_19,active"))
        assert plan.errors == []
        assert len(plan.unchanged) == 1


class TestApplying:
    def test_creating_writes_members_with_no_credentials(self, db):
        plan = roster_import.plan(
            db, csv_of("Avery,Chen,905550001,ac9981,2028,plan_14,active")
        )
        result = roster_import.apply(db, plan)

        assert len(result.created) == 1
        member = db.scalar(select(Member).where(Member.puid == "905550001"))
        assert member.netid == "ac9981"
        assert member.class_year == 2028
        assert member.plan_type == PlanType.PLAN_14.value
        # The point of the whole feature: nobody arrives with a card.
        assert db.scalars(select(Credential)).all() == []

    def test_an_imported_member_can_check_in_by_puid_before_enrolling(self, db):
        roster_import.apply(db, roster_import.plan(db, csv_of("Avery,Chen,905550001,,,,")))

        member, credential = credential_service.resolve(db, "905550001", "manual_puid")
        assert member is not None and member.full_name == "Avery Chen"
        assert credential is None

    def test_reuploading_the_same_file_changes_nothing(self, db):
        text = csv_of("Avery,Chen,905550001,ac9981,2028,plan_14,active")
        roster_import.apply(db, roster_import.plan(db, text))

        second = roster_import.plan(db, text)
        assert len(second.unchanged) == 1
        assert second.writes == 0
        assert roster_import.apply(db, second).created == []
        assert db.scalar(select(Member).where(Member.puid == "905550001")) is not None
        assert len(db.scalars(select(Member)).all()) == 1

    def test_a_corrected_file_updates_only_what_changed(self, db):
        roster_import.apply(
            db, roster_import.plan(db, csv_of("Avery,Chen,905550001,ac9981,2028,plan_19,active"))
        )
        plan = roster_import.plan(
            db, csv_of("Avery,Chen,905550001,ac9981,2028,plan_14,active")
        )

        assert list(plan.updates[0].changes) == ["plan_type"]
        assert plan.updates[0].changes["plan_type"] == ("plan_19", "plan_14")
        roster_import.apply(db, plan)
        assert db.scalar(select(Member).where(Member.puid == "905550001")).plan_type == "plan_14"

    def test_a_blank_cell_leaves_the_stored_value_alone(self, db, make_member):
        member = make_member(first_name="Avery", last_name="Chen", puid="905550001")
        member.netid = "ac9981"
        member.status = MemberStatus.ABROAD.value
        db.commit()

        plan = roster_import.plan(db, "first_name,last_name,puid\nAvery,Chen,905550001\n")
        roster_import.apply(db, plan)

        db.refresh(member)
        assert member.netid == "ac9981"
        assert member.status == MemberStatus.ABROAD.value

    def test_updating_never_disturbs_an_enrolled_card(self, db, make_member):
        member = make_member(first_name="Avery", last_name="Chen", puid="905550001")
        credential_service.bind_card(db, member, "04A1B2C3D4E5F601")
        db.commit()

        roster_import.apply(
            db, roster_import.plan(db, csv_of("Avery,Chen,905550001,,2028,plan_14,active"))
        )

        cards = db.scalars(select(Credential).where(Credential.member_id == member.id)).all()
        assert len(cards) == 1 and cards[0].is_active

    def test_bad_rows_are_skipped_and_the_rest_land(self, db):
        plan = roster_import.plan(
            db, csv_of("Avery,Chen,905550001,,,,", "Sam,Okafor,nope,,,,")
        )
        result = roster_import.apply(db, plan)

        assert len(result.created) == 1
        assert result.skipped == 1
        assert db.scalar(select(Member).where(Member.puid == "905550001")) is not None

    def test_planning_writes_nothing(self, db):
        roster_import.plan(db, csv_of("Avery,Chen,905550001,,,,"))
        assert db.scalars(select(Member)).all() == []


def _posted_csv(html: str) -> str:
    """The CSV as the confirm form would submit it, read back out of the page."""
    body = html.split('<textarea name="csv_text"', 1)[1].split(">", 1)[1]
    return unescape(body.split("</textarea>", 1)[0])


class TestAdminScreen:
    def test_the_page_is_admin_only(self, staff_of):
        """A bulk roster rewrite sits with settings and the audit log, not with
        the day-to-day member screens every staff account can reach."""
        staff = staff_of(StaffRole.STAFF.value)
        assert staff.get("/admin/members/import").status_code == 403

    def test_import_is_not_mistaken_for_a_member_id(self, signed_in):
        response = signed_in.get("/admin/members/import")
        assert response.status_code == 200
        assert "Import roster" in response.text

    def test_uploading_previews_without_writing(self, signed_in, db):
        response = signed_in.post(
            "/admin/members/import",
            files={"upload": ("roster.csv", csv_of("Avery,Chen,905550001,,2028,,"), "text/csv")},
        )
        assert response.status_code == 200
        assert "Avery Chen" in response.text
        assert "roster.csv" in response.text
        assert db.scalars(select(Member)).all() == []

    def test_confirming_writes_and_audits(self, signed_in, db):
        """Drives the two POSTs the way a browser does, taking the second one's
        payload out of the preview's own form — the file rides between the two
        requests in that textarea, so an escaping or newline slip there would
        silently import a different roster than the one on screen."""
        text = csv_of("Avery,Chen,905550001,,2028,,", "Sam,Okafor,905550002,,2027,,")
        preview = signed_in.post(
            "/admin/members/import", files={"upload": ("roster.csv", text, "text/csv")}
        )
        assert "Avery Chen" in preview.text and "Sam Okafor" in preview.text

        response = signed_in.post(
            "/admin/members/import",
            data={
                "csv_text": _posted_csv(preview.text),
                "filename": "roster.csv",
                "confirm": "1",
            },
        )
        assert response.status_code == 200
        assert "2 added" in response.text
        assert len(db.scalars(select(Member)).all()) == 2

        entry = db.scalar(select(AuditLog).where(AuditLog.action == "roster.imported"))
        assert entry.detail["created"] == 2
        assert entry.detail["filename"] == "roster.csv"

    def test_a_file_with_no_usable_header_says_so(self, signed_in):
        response = signed_in.post(
            "/admin/members/import",
            files={"upload": ("notes.csv", "hello,world\n1,2\n", "text/csv")},
        )
        assert response.status_code == 200
        assert "missing required column" in response.text

    def test_posting_nothing_asks_for_a_file(self, signed_in):
        response = signed_in.post("/admin/members/import", data={"csv_text": ""})
        assert "Choose a CSV file" in response.text
