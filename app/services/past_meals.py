"""Admin registration of meals in completed serving windows."""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Attendance, AttendanceKind, CredentialType, EntryMethod, MealPeriod, Member
from app.services import alumni, netid
from app.services.audit import record as audit
from app.services.periods import ResolvedPeriod, local_tz, period_ends_at
from app.services.scan import ScanOutcome, process_scan, record_alumni_meal, record_guest


def completed_periods(db: Session, day: date, now: datetime) -> list[MealPeriod]:
    # Include retired windows: attendance may need correcting after a schedule
    # change. The schedule has no effective-date history, so use its saved times.
    if day > now.astimezone(local_tz()).date():
        return []
    periods = db.scalars(
        select(MealPeriod).where(MealPeriod.weekday == day.weekday())
        .order_by(MealPeriod.start_time, MealPeriod.sort_order, MealPeriod.id)
    )
    return [period for period in periods if _completed(period, day, now)]


def _meal(period: MealPeriod, day: date) -> ResolvedPeriod:
    return ResolvedPeriod(
        day, period, datetime.combine(day, period.start_time, tzinfo=local_tz())
    )


def _completed(period: MealPeriod, day: date, now: datetime) -> bool:
    ends = period_ends_at(_meal(period, day))
    return ends.astimezone(timezone.utc) < now.astimezone(timezone.utc)


def _text(values: dict[str, str], key: str, label: str, limit: int, required=False) -> str:
    value = values.get(key, "").strip()
    if required and not value:
        raise ValueError(f"Enter {label}.")
    if len(value) > limit:
        raise ValueError(f"{label.capitalize()} must be {limit} characters or fewer.")
    return value


def _integer(values: dict[str, str], key: str, message: str) -> int:
    try:
        return int(values.get(key, ""))
    except ValueError:
        raise ValueError(message) from None


def register(
    db: Session, values: dict[str, str], actor: str, now: datetime | None = None
) -> Attendance:
    """Validate a completed date/period and apply the ordinary attendance rules.

    The serving-window start is the representative meal timestamp; created_at
    and the audit entry retain the actual time this correction was made.
    """
    now = now or datetime.now(timezone.utc)
    try:
        day = date.fromisoformat(values.get("day", ""))
    except ValueError:
        raise ValueError("Choose a valid service date.") from None
    if day > now.astimezone(local_tz()).date():
        raise ValueError("Choose a meal period that has already ended.")
    period_id = _integer(values, "meal_period_id", "Choose a meal period.")
    period = db.get(MealPeriod, period_id)
    if period is None or period.weekday != day.weekday():
        raise ValueError("Choose a meal period scheduled for this service date.")
    if not _completed(period, day, now):
        raise ValueError("Choose a meal period that has already ended.")
    meal = _meal(period, day)
    moment = meal.local_time
    kind = values.get("kind", "")
    if kind not in {item.value for item in AttendanceKind}:
        raise ValueError("Choose member, guest, or alumni meal.")

    member = None
    if kind in (AttendanceKind.MEMBER.value, AttendanceKind.GUEST.value):
        member_id = _integer(values, "member_id", "Choose a member or guest host.")
        member = db.get(Member, member_id)
        if member is None:
            raise ValueError("Member not found. Choose a member or guest host.")

    if kind == AttendanceKind.MEMBER.value:
        result = process_scan(
            db, member.puid, credential_type=CredentialType.MANUAL_PUID.value,
            moment=moment, meal=meal, actor=actor, entry_method=EntryMethod.ADMIN.value,
        )
    else:
        first = _text(values, "first_name", "a first name", 80, required=True)
        last = _text(values, "last_name", "a last name", 80, required=True)
        netid_value = _text(values, "netid", "NetID", 32)
        if netid_value and not netid.is_valid_netid(netid_value):
            raise ValueError(netid.NETID_FORMAT_HINT)

        if kind == AttendanceKind.GUEST.value:
            # guest_name is a legacy 120-character display column.
            if len(f"{first} {last}") > 120:
                raise ValueError("The guest's full name must be 120 characters or fewer.")
            reason = _text(values, "netid_reason", "a reason for no NetID", 255)
            family = values.get("guest_is_family") == "on"
            professor = values.get("guest_is_professor") == "on"
            if not netid_value and not reason and not (family or professor):
                raise ValueError("Enter the guest's NetID or a reason for having none.")
            override = values.get("override_quota") == "on"
            override_reason = _text(
                values, "override_reason", "a guest quota override reason", 255, required=override
            )
            result = record_guest(
                db, host=member, guest_first_name=first, guest_last_name=last,
                guest_netid=netid_value, guest_netid_reason=reason,
                guest_is_family=family, guest_is_professor=professor,
                moment=moment, meal=meal, entry_method=EntryMethod.ADMIN.value,
                override_by=actor if override else None,
                override_reason=override_reason if override else None,
            )
        else:
            year = _integer(values, "class_year", "Enter the alum's class year.")
            if not 1900 <= year <= 2200:
                raise ValueError("Class year must be between 1900 and 2200.")
            email = _text(values, "email", "an email address", 255)
            phone = _text(values, "phone", "a phone number", 32)
            if not email and not phone:
                raise ValueError(alumni.CONTACT_REQUIRED_HINT)
            if email and not alumni.is_valid_email(email):
                raise ValueError(alumni.EMAIL_FORMAT_HINT)
            if phone and not alumni.is_valid_phone(phone):
                raise ValueError(alumni.PHONE_FORMAT_HINT)
            result = record_alumni_meal(
                db, first_name=first, last_name=last, class_year=year,
                email=email, phone=phone, netid=netid_value,
                moment=moment, meal=meal, actor=actor,
            )

    # NO_MEAL_PLAN is also recorded, despite not being a ScanResult.ok outcome.
    if result.outcome in (ScanOutcome.ALREADY_CHECKED_IN, ScanOutcome.GUEST_QUOTA_EXCEEDED):
        raise ValueError(result.message)
    if result.attendance is None:
        raise ValueError(result.message)
    audit(
        db, actor=actor, action="attendance.backfilled", entity_type="attendance",
        entity_id=result.attendance.id,
        detail={
            "kind": kind, "member_id": result.attendance.member_id,
            "service_date": day.isoformat(), "meal_period_id": period.id,
            "period": period.name, "timestamp_basis": "period_start",
        },
    )
    return result.attendance
