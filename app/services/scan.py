"""The check-in decision. This function is the product; the rest is CRUD.

Design rules that the tests pin down:

  * The path is identical whether a card was tapped or a PUID was typed. Only
    `entry_method` differs, so the two can never drift apart in behaviour.
  * Being over the weekly allotment NEVER blocks. It records the meal, flags it
    as an overage and shows an amber banner. Nobody gets turned away at the
    door over a counter.
  * Only two things actually block: an unrecognised credential, and a guest
    meal past the monthly quota. Both are releasable by staff.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    Attendance,
    AttendanceKind,
    CredentialType,
    EntryMethod,
    MealPeriod,
    Member,
)
from app.services import credentials as credential_service
from app.services.allotment import WeeklyUsage, weekly_usage
from app.services.alumni import normalize_email, normalize_phone
from app.services.audit import record as audit
from app.services.club_settings import ClubConfig, load_config
from app.services.guests import GuestUsage, guest_usage
from app.services.netid import normalize_netid
from app.services.periods import (
    PREVIOUS,
    AdjacentPeriod,
    adjacent_period,
    adjacent_periods,
    resolve_period,
)


class ScanOutcome(str, enum.Enum):
    CHECKED_IN = "checked_in"
    CHECKED_IN_OVERAGE = "checked_in_overage"
    ALREADY_CHECKED_IN = "already_checked_in"
    UNKNOWN_CREDENTIAL = "unknown_credential"
    OUTSIDE_SERVICE = "outside_service"
    GUEST_RECORDED = "guest_recorded"
    GUEST_QUOTA_EXCEEDED = "guest_quota_exceeded"
    ALUMNI_RECORDED = "alumni_recorded"
    NO_MEAL_PLAN = "no_meal_plan"

    @property
    def is_success(self) -> bool:
        return self in {
            ScanOutcome.CHECKED_IN,
            ScanOutcome.CHECKED_IN_OVERAGE,
            ScanOutcome.GUEST_RECORDED,
            ScanOutcome.ALUMNI_RECORDED,
        }


@dataclass
class ScanResult:
    outcome: ScanOutcome
    member: Member | None = None
    attendance: Attendance | None = None
    period: MealPeriod | None = None
    service_date: object | None = None
    weekly: WeeklyUsage | None = None
    guests: GuestUsage | None = None
    message: str = ""
    warnings: list[str] = field(default_factory=list)
    submitted_value: str | None = None
    submitted_type: str | None = None
    # The windows either side of a scan that landed between meals, so the kiosk
    # can offer them rather than only saying no. Empty on every other outcome.
    offers: list[AdjacentPeriod] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.outcome.is_success


def _status_warning(member: Member) -> str | None:
    """Non-active members are flagged loudly but still served.

    Membership questions are resolved by the club office, not by a card reader
    in front of a queue of hungry people.
    """
    if member.is_active_member:
        return None
    return member.status


def _attached_note(attached: AdjacentPeriod) -> str:
    """What the result screen says about a meal booked outside its own window.

    The row itself looks exactly like any other check-in, so this line is what
    tells the member which meal they just spent — the one thing about an early
    check-in that is not obvious from the screen.
    """
    if attached.direction == PREVIOUS:
        return f"Checked in after {attached.period.name} closed"
    return f"Checked in before {attached.period.name} opened"


def process_scan(
    db: Session,
    value: str,
    credential_type: str = CredentialType.CSN.value,
    moment: datetime | None = None,
    config: ClubConfig | None = None,
    force: bool = False,
    actor: str = "kiosk",
    attach: str | None = None,
) -> ScanResult:
    """Check a member in.

    `moment` defaults to now; tests inject it. `force` is the staff-authorized
    escape hatch for a genuine second meal in one period.

    `attach` is the between-meals one: a member who arrives a few minutes early
    is told which windows sit either side of now, and `attach` is which of the
    two they picked. It needs no staff PIN — being early for dinner is not an
    offence, and the alternative is asking somebody to leave and come back — but
    it is only ever honoured when nothing is open, so it can never move a meal
    off the window that is actually serving.
    """
    moment = moment or datetime.now(timezone.utc)
    config = config or load_config(db)

    member, credential = credential_service.resolve(db, value, credential_type)
    if member is None:
        return ScanResult(
            outcome=ScanOutcome.UNKNOWN_CREDENTIAL,
            message="Card not recognized — see staff to enroll.",
            submitted_value=value,
            submitted_type=credential_type,
        )

    resolved = resolve_period(db, moment)
    service_date = resolved.service_date
    period = resolved.period

    attached = adjacent_period(db, moment, attach) if period is None and attach else None
    if attached is not None:
        # The meal moves, and the service date moves with it: an early check-in
        # at midnight for tomorrow's breakfast belongs to tomorrow's service day
        # and tomorrow's meal week, not to the day the clock happens to read.
        period = attached.period
        service_date = attached.service_date

    # Both counters are read after the window is settled, never before, so they
    # are the counters for the week the meal is actually booked into.
    weekly = weekly_usage(db, member, service_date, config)
    guests = guest_usage(db, member, service_date, config)
    warnings: list[str] = []

    if attached is not None:
        warnings.append(_attached_note(attached))

    status_warning = _status_warning(member)
    if status_warning:
        warnings.append(f"Membership status: {status_warning}")

    if period is None:
        return ScanResult(
            outcome=ScanOutcome.OUTSIDE_SERVICE,
            member=member,
            service_date=service_date,
            weekly=weekly,
            guests=guests,
            warnings=warnings,
            # Named here rather than by the router, so the offer and the rule
            # that honours it are decided in the same place and cannot drift.
            offers=adjacent_periods(db, moment),
            message="No meal is being served right now.",
        )

    existing = _existing_member_meal(db, member.id, service_date, period.id)
    if existing is not None and not force:
        return ScanResult(
            outcome=ScanOutcome.ALREADY_CHECKED_IN,
            member=member,
            attendance=existing,
            period=period,
            service_date=service_date,
            weekly=weekly,
            guests=guests,
            warnings=warnings,
            message=f"Already checked in for {period.name}.",
        )

    is_overage = weekly.is_over and period.counts_toward_allotment
    if weekly.allotment is None:
        warnings.append("No meal plan on file")

    attendance = Attendance(
        member_id=member.id,
        credential_id=credential.id if credential else None,
        entry_method=credential_service.entry_method_for(credential_type),
        meal_period_id=period.id,
        service_date=service_date,
        scanned_at=moment,
        kind=AttendanceKind.MEMBER.value,
        is_overage=is_overage,
        status_warning=status_warning,
        override_by=actor if force else None,
        override_reason="forced second meal in period" if force else None,
    )
    db.add(attendance)
    try:
        db.commit()
    except IntegrityError:
        # Two taps racing each other, or a queued offline scan replaying after
        # the same meal was recorded live. The partial unique index caught it;
        # report the meal that already exists rather than erroring at the door.
        db.rollback()
        existing = _existing_member_meal(db, member.id, service_date, period.id)
        return ScanResult(
            outcome=ScanOutcome.ALREADY_CHECKED_IN,
            member=member,
            attendance=existing,
            period=period,
            service_date=service_date,
            weekly=weekly,
            guests=guests,
            warnings=warnings,
            message=f"Already checked in for {period.name}.",
        )

    db.refresh(attendance)

    # Recompute so the kiosk shows the count *including* the meal just eaten.
    weekly_after = weekly_usage(db, member, service_date, config)

    if force:
        audit(
            db,
            actor=actor,
            action="attendance.forced",
            entity_type="attendance",
            entity_id=attendance.id,
            detail={"member_id": member.id, "period": period.name},
        )

    if attached is not None:
        # Audited but not marked as an override on the row itself: override_by
        # is what lifts the one-meal-per-period duplicate guard (see models.py),
        # and an early check-in must stay under it — otherwise the member could
        # check in early and again once the window opened, and be charged twice.
        audit(
            db,
            actor=actor,
            action="attendance.outside_service",
            entity_type="attendance",
            entity_id=attendance.id,
            detail={
                "member_id": member.id,
                "period": period.name,
                "direction": attached.direction,
                "seconds_away": attached.seconds_away,
            },
        )

    if is_overage:
        outcome = ScanOutcome.CHECKED_IN_OVERAGE
        message = (
            f"Over plan — {weekly_after.used} of {weekly_after.allotment} this week."
        )
    elif weekly.allotment is None:
        outcome = ScanOutcome.NO_MEAL_PLAN
        message = "Recorded — no meal plan on file."
    else:
        outcome = ScanOutcome.CHECKED_IN
        message = f"Checked in — {weekly_after.used} of {weekly_after.allotment} this week."

    return ScanResult(
        outcome=outcome,
        member=member,
        attendance=attendance,
        period=period,
        service_date=service_date,
        weekly=weekly_after,
        guests=guests,
        warnings=warnings,
        message=message,
    )


def record_guest(
    db: Session,
    host: Member,
    guest_first_name: str = "",
    guest_last_name: str = "",
    guest_netid: str = "",
    guest_netid_reason: str = "",
    moment: datetime | None = None,
    config: ClubConfig | None = None,
    override_by: str | None = None,
    override_reason: str | None = None,
    entry_method: str = EntryMethod.CSN.value,
) -> ScanResult:
    """Log a guest meal against a host's monthly benefit.

    Unlike the weekly allotment, this one blocks: the quota is a real benefit
    with a real cost, so going past it is a staff decision that gets a name and
    a reason attached.

    Whether the guest's details are *required* is decided at the edge that
    collects them (see routers/api_scan.py), not here — this layer records what
    it is given and falls back to a plain "Guest" label, so a repair script or a
    test can write a row without a NetID it does not have.
    """
    moment = moment or datetime.now(timezone.utc)
    config = config or load_config(db)

    first = (guest_first_name or "").strip()
    last = (guest_last_name or "").strip()
    netid = normalize_netid(guest_netid)
    # A NetID and a reason for having none are mutually exclusive by
    # construction, so a row can never claim both.
    netid_reason = "" if netid else (guest_netid_reason or "").strip()
    display_name = f"{first} {last}".strip() or "Guest"

    resolved = resolve_period(db, moment)
    service_date = resolved.service_date
    period = resolved.period

    usage = guest_usage(db, host, service_date, config)
    weekly = weekly_usage(db, host, service_date, config)

    if period is None:
        return ScanResult(
            outcome=ScanOutcome.OUTSIDE_SERVICE,
            member=host,
            service_date=service_date,
            weekly=weekly,
            guests=usage,
            message="No meal is being served right now.",
        )

    if usage.exhausted and not override_by:
        return ScanResult(
            outcome=ScanOutcome.GUEST_QUOTA_EXCEEDED,
            member=host,
            period=period,
            service_date=service_date,
            weekly=weekly,
            guests=usage,
            message=(
                f"{host.first_name} has used all {usage.quota} guest meals this month. "
                "Staff override required."
            ),
        )

    attendance = Attendance(
        member_id=host.id,
        credential_id=None,
        entry_method=EntryMethod.ADMIN.value if override_by else entry_method,
        meal_period_id=period.id,
        service_date=service_date,
        scanned_at=moment,
        kind=AttendanceKind.GUEST.value,
        guest_name=display_name,
        guest_first_name=first or None,
        guest_last_name=last or None,
        guest_netid=netid or None,
        guest_netid_reason=netid_reason or None,
        is_overage=False,
        override_by=override_by,
        override_reason=override_reason,
    )
    db.add(attendance)
    db.commit()
    db.refresh(attendance)

    if override_by:
        audit(
            db,
            actor=override_by,
            action="guest.override",
            entity_type="attendance",
            entity_id=attendance.id,
            detail={
                "host_id": host.id,
                "guest_name": attendance.guest_name,
                "guest_netid": attendance.guest_netid,
                "reason": override_reason,
                "used_before": usage.used,
                "quota": usage.quota,
            },
        )

    usage_after = guest_usage(db, host, service_date, config)
    return ScanResult(
        outcome=ScanOutcome.GUEST_RECORDED,
        member=host,
        attendance=attendance,
        period=period,
        service_date=service_date,
        weekly=weekly,
        guests=usage_after,
        message=(
            f"Guest recorded — {usage_after.used} of {usage_after.quota} guest meals used "
            "this month."
        ),
    )


def record_alumni_meal(
    db: Session,
    first_name: str,
    last_name: str,
    class_year: int | None = None,
    email: str = "",
    phone: str = "",
    netid: str = "",
    moment: datetime | None = None,
    config: ClubConfig | None = None,
) -> ScanResult:
    """Log a meal eaten by an alum, against nobody.

    No quota and no allotment: an alum is not on a meal plan, so there is no
    counter to draw down and nothing here can block except the club being shut.
    The row carries its own identity — there is no member to look it up from
    later, which is why the fields are required at the edge that collects them
    (see routers/api_scan.py) rather than defaulted to a shrug here.
    """
    moment = moment or datetime.now(timezone.utc)
    config = config or load_config(db)

    first = (first_name or "").strip()
    last = (last_name or "").strip()
    email_value = normalize_email(email)
    phone_value = normalize_phone(phone)
    netid_value = normalize_netid(netid)

    resolved = resolve_period(db, moment)
    service_date = resolved.service_date
    period = resolved.period

    if period is None:
        return ScanResult(
            outcome=ScanOutcome.OUTSIDE_SERVICE,
            service_date=service_date,
            message="No meal is being served right now.",
        )

    attendance = Attendance(
        member_id=None,
        credential_id=None,
        entry_method=EntryMethod.ADMIN.value,
        meal_period_id=period.id,
        service_date=service_date,
        scanned_at=moment,
        kind=AttendanceKind.ALUMNI.value,
        alumni_first_name=first or None,
        alumni_last_name=last or None,
        alumni_class_year=class_year,
        alumni_email=email_value or None,
        alumni_phone=phone_value or None,
        alumni_netid=netid_value or None,
        is_overage=False,
    )
    db.add(attendance)
    db.commit()
    db.refresh(attendance)

    # Audited unconditionally, unlike a guest meal, which is only audited when
    # staff override the quota. An alumni meal has no member's name attached to
    # it anywhere else, so this log is the only trail it leaves.
    audit(
        db,
        actor="kiosk",
        action="alumni.recorded",
        entity_type="attendance",
        entity_id=attendance.id,
        detail={
            "name": attendance.alumni_name,
            "class_year": class_year,
            "email": attendance.alumni_email,
            "phone": attendance.alumni_phone,
            "netid": attendance.alumni_netid,
            "period": period.name,
        },
    )

    return ScanResult(
        outcome=ScanOutcome.ALUMNI_RECORDED,
        attendance=attendance,
        period=period,
        service_date=service_date,
        message=f"Alumni meal recorded for {attendance.alumni_name}.",
    )


def void_attendance(db: Session, attendance: Attendance, actor: str = "kiosk") -> None:
    """Undo a check-in.

    Soft-deletes rather than removing the row, so the audit trail survives, and
    clears the duplicate guard so the member can scan again.
    """
    attendance.voided_at = datetime.now(timezone.utc)
    db.commit()
    audit(
        db,
        actor=actor,
        action="attendance.voided",
        entity_type="attendance",
        entity_id=attendance.id,
        detail={"member_id": attendance.member_id, "kind": attendance.kind},
    )


def _existing_member_meal(
    db: Session, member_id: int, service_date, period_id: int
) -> Attendance | None:
    return (
        db.query(Attendance)
        .filter(
            Attendance.member_id == member_id,
            Attendance.service_date == service_date,
            Attendance.meal_period_id == period_id,
            Attendance.kind == AttendanceKind.MEMBER.value,
            Attendance.voided_at.is_(None),
        )
        .first()
    )
