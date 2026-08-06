"""Card binding, reissue and revocation.

A lost TigerCard is the routine event this has to survive without losing a
member's meal history.
"""
from __future__ import annotations

import pytest

from app.models import Attendance, Credential, CredentialType
from app.services import credentials as credential_service
from app.services.scan import ScanOutcome, process_scan
from tests.conftest import eastern


class TestNormalization:
    @pytest.mark.parametrize(
        "raw", ["a1b2c3d4", "A1B2C3D4", " A1B2C3D4 ", "A1-B2-C3-D4", "A1 B2 C3 D4"]
    )
    def test_equivalent_card_values_resolve_to_one_member(self, db, make_member, raw):
        member = make_member()
        credential_service.bind_card(db, member, "A1B2C3D4")
        db.commit()

        found, credential = credential_service.resolve(db, raw, CredentialType.CSN.value)
        assert found is not None and found.id == member.id
        assert credential is not None

    def test_puid_lookup_tolerates_typed_whitespace(self, db, make_member):
        member = make_member(puid="905551234")
        found, credential = credential_service.resolve(
            db, " 905551234 ", CredentialType.MANUAL_PUID.value
        )
        assert found.id == member.id
        assert credential is None  # no card was involved

    def test_empty_input_resolves_to_nobody(self, db):
        assert credential_service.resolve(db, "", CredentialType.CSN.value) == (None, None)
        assert credential_service.resolve(db, "   ", CredentialType.MANUAL_PUID.value) == (
            None,
            None,
        )


class TestReissue:
    def test_new_card_replaces_the_old_one(self, db, make_member):
        member = make_member()
        credential_service.bind_card(db, member, "OLDCARD")
        db.commit()

        credential_service.bind_card(db, member, "NEWCARD")
        db.commit()

        active = credential_service.active_credentials(db, member.id)
        assert [c.value for c in active] == ["NEWCARD"]

        old = db.query(Credential).filter_by(value="OLDCARD").one()
        assert old.is_active is False
        assert old.revoked_at is not None

    def test_old_card_stops_working_and_new_card_works(self, db, make_member):
        member = make_member()
        credential_service.bind_card(db, member, "OLDCARD")
        db.commit()
        credential_service.bind_card(db, member, "NEWCARD")
        db.commit()

        assert (
            process_scan(db, "OLDCARD", moment=eastern(2026, 1, 5, 12, 0)).outcome
            is ScanOutcome.UNKNOWN_CREDENTIAL
        )
        assert (
            process_scan(db, "NEWCARD", moment=eastern(2026, 1, 5, 12, 0)).outcome
            is ScanOutcome.CHECKED_IN
        )

    def test_meal_history_survives_a_reissue(self, db, make_member):
        """The whole point of separating members from credentials."""
        member = make_member()
        credential_service.bind_card(db, member, "OLDCARD")
        db.commit()
        process_scan(db, "OLDCARD", moment=eastern(2026, 1, 5, 12, 0))
        process_scan(db, "OLDCARD", moment=eastern(2026, 1, 5, 18, 0))

        credential_service.bind_card(db, member, "NEWCARD")
        db.commit()
        process_scan(db, "NEWCARD", moment=eastern(2026, 1, 6, 12, 0))

        rows = db.query(Attendance).filter_by(member_id=member.id).all()
        assert len(rows) == 3

    def test_rebinding_the_same_card_is_a_no_op(self, db, make_member):
        member = make_member()
        first = credential_service.bind_card(db, member, "SAMECARD")
        db.commit()
        second = credential_service.bind_card(db, member, "SAMECARD")
        db.commit()

        assert first.id == second.id
        assert len(credential_service.active_credentials(db, member.id)) == 1

    def test_card_moved_to_another_member_leaves_the_first_without_one(self, db, make_member):
        """Guards against a mis-scan during enrollment silently giving two
        people the same live credential."""
        alice = make_member(first_name="Alice")
        bob = make_member(first_name="Bob")
        credential_service.bind_card(db, alice, "SHARED")
        db.commit()

        credential_service.bind_card(db, bob, "SHARED")
        db.commit()

        assert credential_service.active_credentials(db, alice.id) == []
        assert [c.value for c in credential_service.active_credentials(db, bob.id)] == ["SHARED"]

        result = process_scan(db, "SHARED", moment=eastern(2026, 1, 5, 12, 0))
        assert result.member.id == bob.id


class TestRevocation:
    def test_revoked_card_no_longer_identifies_anyone(self, db, make_member):
        member = make_member()
        credential = credential_service.bind_card(db, member, "GONE")
        db.commit()

        credential_service.revoke(db, credential)
        db.commit()

        assert (
            process_scan(db, "GONE", moment=eastern(2026, 1, 5, 12, 0)).outcome
            is ScanOutcome.UNKNOWN_CREDENTIAL
        )

    def test_puid_entry_still_works_after_a_card_is_revoked(self, db, make_member):
        """Someone who lost their card can still eat while waiting for a new
        one from the ID office."""
        member = make_member(puid="905559999")
        credential = credential_service.bind_card(db, member, "LOST")
        db.commit()
        credential_service.revoke(db, credential)
        db.commit()

        result = process_scan(
            db, "905559999", CredentialType.MANUAL_PUID.value, moment=eastern(2026, 1, 5, 12, 0)
        )
        assert result.outcome is ScanOutcome.CHECKED_IN

    def test_revoked_value_can_be_reissued_to_someone_else(self, db, make_member):
        """Only active credentials are unique, so a recycled card number does
        not collide with the retired binding."""
        alice = make_member(first_name="Alice")
        bob = make_member(first_name="Bob")
        credential = credential_service.bind_card(db, alice, "RECYCLED")
        db.commit()
        credential_service.revoke(db, credential)
        db.commit()

        credential_service.bind_card(db, bob, "RECYCLED")
        db.commit()

        result = process_scan(db, "RECYCLED", moment=eastern(2026, 1, 5, 12, 0))
        assert result.member.id == bob.id
