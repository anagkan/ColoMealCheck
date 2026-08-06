"""Resolving a credential to a member, and binding cards to members.

The rest of the app deals in (type, value) pairs and never in "CSNs". Two
consequences worth keeping:

  * A manual PUID is resolved against members.puid directly. It needs no
    enrollment, so the club is usable on day one, before anyone has tapped in.
  * If TigerCards turn out to emit a randomized Seos serial, a different reader
    emitting PROX or PACS values drops in here without touching the rules.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Credential, CredentialType, EntryMethod, Member

# A TigerCard CSN is a 16-digit hexadecimal serial, and a PUID is nine decimal
# digits. Both are checked at enrollment only: that is where a mistyped or
# mis-scanned value first enters the system, and it is the one moment somebody
# is standing there to correct it. Scans are left unchecked on purpose, so a
# card already on file keeps working even if it predates these rules.
_CSN_RE = re.compile(r"[0-9A-F]{16}")
_PUID_RE = re.compile(r"[0-9]{9}")

CSN_FORMAT_HINT = "A card CSN is 16 hexadecimal digits (0-9, A-F)."
PUID_FORMAT_HINT = "A PUID is nine digits, like 905559999."


def is_valid_csn(value: str) -> bool:
    """True if `value` is a 16-digit hex CSN, once normalized."""
    return bool(_CSN_RE.fullmatch(normalize_value(value)))


def is_valid_puid(value: str) -> bool:
    """True if `value` is a nine-digit PUID, once normalized."""
    return bool(_PUID_RE.fullmatch(normalize_puid(value)))


def normalize_value(value: str) -> str:
    """Card readers vary in case and padding; PUIDs get typed with stray spaces.

    Normalizing on the way in and on lookup keeps '00A1B2C3' and 'a1b2c3'
    from enrolling as two different cards.
    """
    cleaned = (value or "").strip().replace(" ", "").replace("-", "").upper()
    return cleaned


def normalize_puid(value: str) -> str:
    return (value or "").strip().replace(" ", "").replace("-", "")


def entry_method_for(credential_type: str) -> str:
    mapping = {
        CredentialType.CSN.value: EntryMethod.CSN.value,
        CredentialType.PROX.value: EntryMethod.PROX.value,
        CredentialType.PACS.value: EntryMethod.PACS.value,
        CredentialType.MANUAL_PUID.value: EntryMethod.MANUAL_PUID.value,
    }
    return mapping.get(credential_type, EntryMethod.CSN.value)


def resolve(
    db: Session, value: str, credential_type: str
) -> tuple[Member | None, Credential | None]:
    """Look up the member behind a scan or a typed ID.

    Returns (member, credential). `credential` is None for manual PUID entry —
    no card was involved, and attendance.credential_id is nullable for exactly
    this reason.
    """
    if credential_type == CredentialType.MANUAL_PUID.value:
        puid = normalize_puid(value)
        if not puid:
            return None, None
        member = db.scalar(select(Member).where(Member.puid == puid))
        return member, None

    normalized = normalize_value(value)
    if not normalized:
        return None, None
    credential = db.scalar(
        select(Credential).where(
            Credential.type == credential_type,
            Credential.value == normalized,
            Credential.is_active.is_(True),
        )
    )
    if credential is None:
        return None, None
    return credential.member, credential


def bind_card(
    db: Session,
    member: Member,
    value: str,
    credential_type: str = CredentialType.CSN.value,
    label: str | None = None,
    replace_existing: bool = True,
) -> Credential:
    """Enroll a card to a member.

    By default this revokes the member's other active cards of the same type: a
    person carries one TigerCard, and a reissue should retire the lost one
    rather than leave two live credentials.
    """
    normalized = normalize_value(value)
    now = datetime.now(timezone.utc)

    # If this exact card is already live on someone, retire that binding first —
    # this is what makes a transferred or re-issued card land cleanly.
    existing = db.scalar(
        select(Credential).where(
            Credential.type == credential_type,
            Credential.value == normalized,
            Credential.is_active.is_(True),
        )
    )
    if existing is not None:
        if existing.member_id == member.id:
            return existing
        existing.is_active = False
        existing.revoked_at = now

    if replace_existing:
        others = db.scalars(
            select(Credential).where(
                Credential.member_id == member.id,
                Credential.type == credential_type,
                Credential.is_active.is_(True),
            )
        )
        for other in others:
            other.is_active = False
            other.revoked_at = now

    credential = Credential(
        member_id=member.id,
        type=credential_type,
        value=normalized,
        label=label,
        is_active=True,
        enrolled_at=now,
    )
    db.add(credential)
    db.flush()
    return credential


def revoke(db: Session, credential: Credential) -> None:
    credential.is_active = False
    credential.revoked_at = datetime.now(timezone.utc)
    db.flush()


def active_credentials(db: Session, member_id: int) -> list[Credential]:
    stmt = select(Credential).where(
        Credential.member_id == member_id, Credential.is_active.is_(True)
    )
    return list(db.scalars(stmt))
