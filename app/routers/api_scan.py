"""Kiosk-facing API: check in, host a guest, undo, enroll a card.

Only the privileged actions (enrollment, guest override, forced second meal)
require staff authorization. Plain check-in does not — the kiosk sits in the
dining room on the club LAN and a queue at dinner is no place for a login.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import authorize_privileged_action, current_user
from app.models import (
    Attendance,
    AttendanceKind,
    Credential,
    CredentialType,
    Member,
    MemberStatus,
    PlanType,
    StaffUser,
)
from app.schemas import (
    AdjacentPeriodOut,
    AlumniMealRequest,
    AlumniSummary,
    EnrollNewRequest,
    EnrollRequest,
    GuestRequest,
    GuestUsageOut,
    MemberSummary,
    ScanRequest,
    ScanResponse,
    UndoRequest,
    WeeklyUsageOut,
)
from app.services import alumni as alumni_service
from app.services import credentials as credential_service
from app.services import netid as netid_service
from app.services import photos as photo_service
from app.services.audit import record as audit
from app.services.club_settings import load_config
from app.services.periods import next_period, resolve_period, seconds_remaining
from app.services.scan import (
    ScanResult,
    process_scan,
    record_alumni_meal,
    record_guest,
    void_attendance,
)

router = APIRouter(prefix="/api", tags=["kiosk"])


def member_summary(member: Member) -> MemberSummary:
    return MemberSummary(
        id=member.id,
        first_name=member.first_name,
        last_name=member.last_name,
        full_name=member.full_name,
        puid=member.puid,
        netid=member.netid,
        class_year=member.class_year,
        plan_type=member.plan_type,
        status=member.status,
        photo_url=f"/photos/{member.photo_path}" if member.photo_path else None,
    )


def to_response(result: ScanResult, db: Session) -> ScanResponse:
    config = load_config(db)
    weekly = None
    if result.weekly is not None:
        weekly = WeeklyUsageOut(
            used=result.weekly.used,
            allotment=result.weekly.allotment,
            remaining=result.weekly.remaining,
            week_start=result.weekly.week_start,
            week_end=result.weekly.week_end,
            is_over=result.weekly.is_over,
        )
    guests = None
    if result.guests is not None:
        guests = GuestUsageOut(
            used=result.guests.used,
            quota=result.guests.quota,
            remaining=result.guests.remaining,
            month_start=result.guests.month_start,
        )
    row = result.attendance
    alumni = None
    if row is not None and row.kind == AttendanceKind.ALUMNI.value:
        alumni = AlumniSummary(
            full_name=row.alumni_name,
            class_year=row.alumni_class_year,
            email=row.alumni_email,
            phone=row.alumni_phone,
            netid=row.alumni_netid,
        )
    return ScanResponse(
        outcome=result.outcome.value,
        ok=result.ok,
        message=result.message,
        warnings=result.warnings,
        member=member_summary(result.member) if result.member else None,
        alumni=alumni,
        period_name=result.period.name if result.period else None,
        service_date=result.service_date,
        weekly=weekly,
        guests=guests,
        attendance_id=result.attendance.id if result.attendance else None,
        checked_in_at=result.attendance.scanned_at if result.attendance else None,
        undo_seconds=config.undo_window_seconds,
        result_seconds=config.result_screen_seconds,
        submitted_value=result.submitted_value,
        submitted_type=result.submitted_type,
        offers=[
            AdjacentPeriodOut(
                direction=offer.direction,
                period_name=offer.period.name,
                service_date=offer.service_date,
                seconds_away=offer.seconds_away,
            )
            for offer in result.offers
        ],
    )


@router.post("/scan", response_model=ScanResponse)
def scan(
    payload: ScanRequest,
    request: Request,
    db: Session = Depends(get_db),
    user: StaffUser | None = Depends(current_user),
) -> ScanResponse:
    actor = "kiosk"
    if payload.force:
        actor = authorize_privileged_action(payload.staff_pin, user, "force a second meal")

    moment, stale = _trusted_moment(payload.occurred_at)
    result = process_scan(
        db,
        value=payload.value,
        credential_type=payload.credential_type,
        moment=moment,
        force=payload.force,
        actor=actor,
        # Unauthorized on purpose, unlike `force` above: a member who is early
        # for dinner is answering an offer the kiosk made them, and putting a
        # staff PIN in front of it would mean fetching somebody every time.
        attach=payload.attach,
    )

    if stale:
        _note_untrusted_time(db, result, payload.occurred_at, actor)

    return to_response(result, db)


# A replayed scan may legitimately be minutes or hours old, but it should never
# be days old or in the future — that would be a wrong clock on the kiosk
# laptop, and honouring it would file meals under the wrong service date.
MAX_REPLAY_AGE_SECONDS = 24 * 60 * 60


def _trusted_moment(occurred_at: datetime | None) -> tuple[datetime | None, bool]:
    """Decide whether to believe the kiosk's clock.

    Returns the moment to record against — None meaning "use server time" — and
    whether a time the kiosk did claim was thrown away. The caller needs that
    second value: substituting server time silently is what let a scan replayed
    days late land in the wrong meal week with nothing to show for it.

    A scan with no claimed time at all is not stale, just ordinary: the kiosk is
    online and the server's own clock is the right answer.
    """
    if occurred_at is None:
        return None, False
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    age = (now - occurred_at).total_seconds()
    if age < -60 or age > MAX_REPLAY_AGE_SECONDS:
        return None, True  # fall back to server time, and say so
    return occurred_at, False


def _note_untrusted_time(
    db: Session, result: ScanResult, claimed_at: datetime, actor: str
) -> None:
    """Say out loud that a meal was filed under a different day than it claimed.

    The meal is still recorded — nobody is turned away over a clock — but it has
    landed on today's service date rather than the one it was eaten on, which
    moves it into the wrong meal week and spends the wrong allotment. None of
    that is visible in the row itself, so it goes on the screen for whoever is
    standing there and into the audit log for whoever is not.

    Shared by all three meal routes: a guest meal and an alumni meal replayed
    late are misfiled exactly as a check-in is, and it would be a strange kind of
    honesty to mention it for one and not the others.
    """
    result.warnings.append("Scan time was not usable — recorded against today instead.")
    if result.attendance is None:
        return
    audit(
        db,
        actor=actor,
        action="attendance.untrusted_time",
        entity_type="attendance",
        entity_id=result.attendance.id,
        detail={
            "claimed_at": claimed_at.isoformat(),
            "recorded_service_date": str(result.service_date),
        },
    )


@router.post("/guest", response_model=ScanResponse)
def guest(
    payload: GuestRequest,
    db: Session = Depends(get_db),
    user: StaffUser | None = Depends(current_user),
) -> ScanResponse:
    host = _resolve_host(db, payload)

    # Checked here rather than in the service: this is the one place a person is
    # standing at the popup and can fix what they typed.
    first = payload.guest_first_name.strip()
    last = payload.guest_last_name.strip()
    if not first or not last:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Enter the guest's first and last name.",
        )

    # A NetID, or a reason there isn't one — never neither. Skipping the field
    # silently is the one outcome worth designing against: it is how a guest
    # list turns back into a list of first names nobody can trace.
    netid = netid_service.normalize_netid(payload.guest_netid)
    netid_reason = payload.guest_netid_reason.strip()
    if not netid and not netid_reason:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Enter the guest's Princeton NetID, or tick “Guest has no NetID” "
            "and say why.",
        )
    if netid and not netid_service.is_valid_netid(netid):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, netid_service.NETID_FORMAT_HINT
        )

    override_by = None
    if payload.staff_pin or payload.override_reason:
        override_by = authorize_privileged_action(
            payload.staff_pin, user, "override the guest meal quota"
        )

    moment, stale = _trusted_moment(payload.occurred_at)
    result = record_guest(
        db,
        host=host,
        guest_first_name=first,
        guest_last_name=last,
        guest_netid=netid,
        guest_netid_reason=netid_reason,
        moment=moment,
        override_by=override_by,
        override_reason=payload.override_reason,
    )
    if stale:
        _note_untrusted_time(db, result, payload.occurred_at, override_by or "kiosk")
    return to_response(result, db)


def _resolve_host(db: Session, payload: GuestRequest) -> Member:
    """Find the member hosting a guest, however the kiosk named them.

    An id is taken at face value — the server handed it out in the first place.
    A raw card or PUID goes through the same resolver a check-in uses, so an
    offline guest meal identifies its host by exactly the means a tap does, and
    a card that turns out not to be enrolled is refused here in the same words
    it would have been refused at the door.
    """
    if payload.member_id is not None:
        host = db.get(Member, payload.member_id)
        if host is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Member not found")
        return host

    if not payload.host_value:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Choose the member hosting this guest.",
        )

    host, _ = credential_service.resolve(
        db, payload.host_value, payload.host_credential_type
    )
    if host is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "The host's card is not recognized — see staff to enroll it.",
        )
    return host


@router.post("/alumni", response_model=ScanResponse)
def alumni_meal(
    payload: AlumniMealRequest,
    db: Session = Depends(get_db),
) -> ScanResponse:
    """Record a meal for an alum — no member, no host, no quota.

    Every check lives here rather than in the service, for the reason the guest
    route gives: this is the one moment somebody is standing at the popup and
    can fix what they typed. Afterwards there is no card and no PUID to look the
    person up by, so a wrong contact detail is wrong forever.
    """
    first = payload.first_name.strip()
    last = payload.last_name.strip()
    if not first or not last:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Enter the alum's first and last name.",
        )

    if payload.class_year is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Enter the alum's class year."
        )

    # Either contact detail is enough on its own — an alum who gives a phone
    # number should not be made to invent an email address — but neither is not.
    # A record nobody can be reached from is the outcome worth designing
    # against, exactly as with a guest's missing NetID.
    email = alumni_service.normalize_email(payload.email)
    phone = alumni_service.normalize_phone(payload.phone)
    if not email and not phone:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, alumni_service.CONTACT_REQUIRED_HINT
        )
    if email and not alumni_service.is_valid_email(email):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, alumni_service.EMAIL_FORMAT_HINT
        )
    if phone and not alumni_service.is_valid_phone(phone):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, alumni_service.PHONE_FORMAT_HINT
        )

    # Optional, and outside the either-or above: many alumni keep a NetID for
    # life and it is the cleanest link back to the member they used to be, but
    # plenty have let theirs lapse and it reaches nobody either way. Blank is a
    # complete answer; a malformed one is still a typo worth catching.
    alumni_netid = netid_service.normalize_netid(payload.netid)
    if alumni_netid and not netid_service.is_valid_netid(alumni_netid):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, netid_service.NETID_FORMAT_HINT
        )

    moment, stale = _trusted_moment(payload.occurred_at)
    result = record_alumni_meal(
        db,
        first_name=first,
        last_name=last,
        class_year=payload.class_year,
        email=email,
        phone=phone,
        netid=alumni_netid,
        moment=moment,
    )
    if stale:
        _note_untrusted_time(db, result, payload.occurred_at, "kiosk")
    return to_response(result, db)


@router.post("/undo", response_model=ScanResponse)
def undo(
    payload: UndoRequest,
    db: Session = Depends(get_db),
    user: StaffUser | None = Depends(current_user),
) -> ScanResponse:
    """Undo a check-in made moments ago.

    Time-boxed to the configured undo window so the kiosk cannot be used to
    quietly erase last week's meals — that is an admin action, with a name on
    it.
    """
    attendance = db.get(Attendance, payload.attendance_id)
    if attendance is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Check-in not found")
    if attendance.voided_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Already undone")

    config = load_config(db)
    scanned_at = attendance.scanned_at
    if scanned_at.tzinfo is None:
        scanned_at = scanned_at.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - scanned_at).total_seconds()
    if age > config.undo_window_seconds:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "That check-in is too old to undo here — an admin can remove it.",
        )

    actor = f"staff:{user.username}" if user else "kiosk"
    void_attendance(db, attendance, actor=actor)

    return ScanResponse(
        outcome="undone",
        ok=True,
        message="Check-in undone.",
        undo_seconds=config.undo_window_seconds,
        result_seconds=config.result_screen_seconds,
    )


@router.get("/members/search")
def search_members(
    q: str = "",
    limit: int = 12,
    db: Session = Depends(get_db),
) -> list[dict]:
    """Name/PUID/NetID search used by the enrollment screen.

    NetID is searchable for the same reason it is stored: it is what the rest of
    Princeton knows a member by, so it is what staff will have to hand when a
    roster from somewhere else is the thing they are working from.
    """
    term = (q or "").strip()
    if len(term) < 2:
        return []
    pattern = f"%{term}%"
    stmt = (
        select(Member)
        .where(
            or_(
                Member.first_name.ilike(pattern),
                Member.last_name.ilike(pattern),
                Member.puid.ilike(pattern),
                Member.netid.ilike(pattern),
            )
        )
        .order_by(Member.last_name, Member.first_name)
        .limit(min(limit, 50))
    )
    members = list(db.scalars(stmt))
    return [
        {
            "id": m.id,
            "full_name": m.full_name,
            "puid": m.puid,
            "netid": m.netid,
            "class_year": m.class_year,
            "status": m.status,
            "plan_type": m.plan_type,
            "has_card": bool(credential_service.active_credentials(db, m.id)),
            "photo_url": f"/photos/{m.photo_path}" if m.photo_path else None,
        }
        for m in members
    ]


@router.post("/enroll", response_model=ScanResponse)
def enroll(
    payload: EnrollRequest,
    db: Session = Depends(get_db),
    user: StaffUser | None = Depends(current_user),
) -> ScanResponse:
    """Bind a card to an existing member and optionally store their photo.

    Staff-gated: this is the step that decides whose meals a card spends.
    """
    actor = authorize_privileged_action(payload.staff_pin, user, "enroll a card")

    if payload.credential_type == CredentialType.CSN.value and not (
        credential_service.is_valid_csn(payload.value)
    ):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, credential_service.CSN_FORMAT_HINT
        )

    member = db.get(Member, payload.member_id)
    if member is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Member not found")

    credential = credential_service.bind_card(
        db, member, payload.value, credential_type=payload.credential_type
    )

    if payload.photo_data_url:
        try:
            filename = photo_service.save_data_url(payload.photo_data_url, member.id)
        except photo_service.PhotoError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        old = member.photo_path
        member.photo_path = filename
        photo_service.delete_photo(old)

    db.commit()
    db.refresh(member)

    audit(
        db,
        actor=actor,
        action="credential.enrolled",
        entity_type="member",
        entity_id=member.id,
        detail={
            "credential_id": credential.id,
            "type": credential.type,
            "photo_captured": bool(payload.photo_data_url),
        },
    )

    resolved = resolve_period(db, datetime.now(timezone.utc))
    return ScanResponse(
        outcome="enrolled",
        ok=True,
        message=f"Card enrolled for {member.full_name}.",
        member=member_summary(member),
        period_name=resolved.period.name if resolved.period else None,
        service_date=resolved.service_date,
    )


@router.post("/enroll/new", response_model=ScanResponse)
def enroll_new(
    payload: EnrollNewRequest,
    db: Session = Depends(get_db),
    user: StaffUser | None = Depends(current_user),
) -> ScanResponse:
    """Create a member and bind their card in one step.

    The usual enrollment: the person is not on file yet, so their details and
    their card are written together. Both conflict checks below refuse rather
    than merge — at this desk a clash almost always means a typo in the PUID or
    a stray tap of the wrong card, and quietly moving a card off an existing
    member would be much harder to notice than an error message.
    """
    actor = authorize_privileged_action(payload.staff_pin, user, "enroll a member")

    if payload.plan_type not in {p.value for p in PlanType}:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Unknown meal plan.")
    if payload.status not in {s.value for s in MemberStatus}:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Unknown member status.")

    puid = credential_service.normalize_puid(payload.puid)
    if not puid:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "A PUID is required.")
    if not credential_service.is_valid_puid(puid):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, credential_service.PUID_FORMAT_HINT
        )

    # Required here, unlike a guest's, and with no "has no NetID" escape hatch:
    # a guest may genuinely be a visiting parent with none, but every member of
    # this club is a Princeton affiliate who has one. The column itself is
    # nullable only so that members enrolled before it existed stay valid.
    netid = netid_service.normalize_netid(payload.netid)
    if not netid:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "A Princeton NetID is required."
        )
    if not netid_service.is_valid_netid(netid):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, netid_service.NETID_FORMAT_HINT
        )

    if payload.credential_type == CredentialType.CSN.value and not (
        credential_service.is_valid_csn(payload.value)
    ):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, credential_service.CSN_FORMAT_HINT
        )

    clash = db.scalar(select(Member).where(Member.puid == puid))
    if clash is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"PUID {puid} is already on file for {clash.full_name}. "
            "Use 'Already in the system' to link this card to them.",
        )

    # A NetID identifies one person, so a second member claiming one is the same
    # mistake as a duplicate PUID and gets the same answer: name the holder and
    # refuse, rather than create a second row for somebody already on file.
    netid_clash = db.scalar(select(Member).where(Member.netid == netid))
    if netid_clash is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"NetID {netid} is already on file for {netid_clash.full_name}. "
            "Use 'Already in the system' to link this card to them.",
        )

    card_value = credential_service.normalize_value(payload.value)
    held = db.scalar(
        select(Credential).where(
            Credential.type == payload.credential_type,
            Credential.value == card_value,
            Credential.is_active.is_(True),
        )
    )
    if held is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"That card is already linked to {held.member.full_name}. "
            "Check you tapped the right card.",
        )

    member = Member(
        first_name=payload.first_name.strip(),
        last_name=payload.last_name.strip(),
        puid=puid,
        netid=netid,
        class_year=payload.class_year,
        plan_type=payload.plan_type,
        status=payload.status,
    )
    db.add(member)
    db.flush()  # assigns member.id, which bind_card and the photo name both need

    credential = credential_service.bind_card(
        db, member, payload.value, credential_type=payload.credential_type
    )

    if payload.photo_data_url:
        try:
            member.photo_path = photo_service.save_data_url(payload.photo_data_url, member.id)
        except photo_service.PhotoError as exc:
            db.rollback()
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    db.commit()
    db.refresh(member)

    audit(
        db,
        actor=actor,
        action="member.enrolled",
        entity_type="member",
        entity_id=member.id,
        detail={
            "puid": member.puid,
            "netid": member.netid,
            "class_year": member.class_year,
            "plan_type": member.plan_type,
            "credential_id": credential.id,
            "type": credential.type,
            "photo_captured": bool(payload.photo_data_url),
        },
    )

    resolved = resolve_period(db, datetime.now(timezone.utc))
    return ScanResponse(
        outcome="enrolled",
        ok=True,
        message=f"{member.full_name} enrolled and card linked.",
        member=member_summary(member),
        period_name=resolved.period.name if resolved.period else None,
        service_date=resolved.service_date,
    )


@router.get("/status")
def kiosk_status(db: Session = Depends(get_db)) -> dict:
    """Drives the idle screen's service label and the meal banner countdown."""
    now = datetime.now(timezone.utc)
    resolved = resolve_period(db, now)
    period = resolved.period
    upcoming = next_period(db, now)
    return {
        "service_date": resolved.service_date.isoformat(),
        "local_time": resolved.local_time.isoformat(),
        "period_name": period.name if period else None,
        "period_ends": period.end_time.strftime("%-I:%M %p") if period else None,
        "serving": period is not None,
        # Durations, not timestamps — see services/periods.py.
        "seconds_remaining": seconds_remaining(resolved),
        "next_period_name": upcoming.period.name if upcoming else None,
        "seconds_until_next": upcoming.seconds_until if upcoming else None,
    }
