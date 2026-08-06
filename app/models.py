from __future__ import annotations

import enum
from datetime import date, datetime, time, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PlanType(str, enum.Enum):
    PLAN_19 = "plan_19"
    PLAN_14 = "plan_14"
    RCA_PAA = "rca_paa"
    NONE = "none"


class MemberStatus(str, enum.Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    LEAVE = "leave"
    ABROAD = "abroad"
    ALUM = "alum"


class CredentialType(str, enum.Enum):
    """How a member was identified.

    The rules engine never learns what a CSN is — it asks for a member given a
    (type, value) pair. If Princeton's cards turn out to emit a randomized Seos
    serial, PROX or PACS slots in here and nothing downstream changes.
    """

    CSN = "csn"
    PROX = "prox"
    PACS = "pacs"
    MANUAL_PUID = "manual_puid"


class EntryMethod(str, enum.Enum):
    CSN = "csn"
    PROX = "prox"
    PACS = "pacs"
    MANUAL_PUID = "manual_puid"
    ADMIN = "admin"


class AttendanceKind(str, enum.Enum):
    MEMBER = "member"
    GUEST = "guest"
    # An alum eating here on their own account, hosted by nobody. The only kind
    # of meal with no member behind it at all — see Attendance.
    ALUMNI = "alumni"


class StaffRole(str, enum.Enum):
    STAFF = "staff"
    ADMIN = "admin"


class Member(Base):
    __tablename__ = "members"

    id: Mapped[int] = mapped_column(primary_key=True)
    first_name: Mapped[str] = mapped_column(String(80))
    last_name: Mapped[str] = mapped_column(String(80))
    puid: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    # The second identifier, alongside the PUID: a PUID is what is printed on
    # the card, a NetID is what every other Princeton system knows the member
    # by, which is what makes a roster reconcilable against anything else.
    #
    # Nullable, and unique when set. Nullable because members enrolled before
    # this column existed have no NetID on file and inventing one would be worse
    # than leaving the gap visible — the same call migration 0002 made for
    # guests. New enrollments require it at the edge that collects them; see
    # routers/api_scan.py::enroll_new.
    netid: Mapped[str | None] = mapped_column(
        String(32), unique=True, index=True, nullable=True
    )
    class_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    plan_type: Mapped[str] = mapped_column(String(16), default=PlanType.PLAN_19.value)
    status: Mapped[str] = mapped_column(String(16), default=MemberStatus.ACTIVE.value)
    photo_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    credentials: Mapped[list[Credential]] = relationship(
        back_populates="member", cascade="all, delete-orphan"
    )

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def is_active_member(self) -> bool:
        return self.status == MemberStatus.ACTIVE.value


class Credential(Base):
    """A physical card bound to a member.

    A reissued TigerCard adds a row and revokes the old one, so a member's meal
    history stays intact across a lost card. Only *active* credentials are
    unique: a revoked value may legitimately reappear later.
    """

    __tablename__ = "credentials"
    __table_args__ = (
        Index(
            "uq_credentials_active_value",
            "type",
            "value",
            unique=True,
            postgresql_where=text("is_active"),
            sqlite_where=text("is_active"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id", ondelete="CASCADE"), index=True)
    type: Mapped[str] = mapped_column(String(16), default=CredentialType.CSN.value)
    value: Mapped[str] = mapped_column(String(128), index=True)
    label: Mapped[str | None] = mapped_column(String(80), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    enrolled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    member: Mapped[Member] = relationship(back_populates="credentials")


class MealPeriod(Base):
    """One serving window on one weekday.

    weekday is 0=Monday .. 6=Sunday, matching Python's date.weekday().
    """

    __tablename__ = "meal_periods"
    __table_args__ = (UniqueConstraint("weekday", "name", name="uq_meal_period_weekday_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(40))
    weekday: Mapped[int] = mapped_column(Integer, index=True)
    start_time: Mapped[time] = mapped_column(Time)
    end_time: Mapped[time] = mapped_column(Time)
    counts_toward_allotment: Mapped[bool] = mapped_column(Boolean, default=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<MealPeriod {self.name} wd={self.weekday} {self.start_time}-{self.end_time}>"


class Attendance(Base):
    """One meal eaten. Member, guest and alumni meals share this table.

    For a guest row, member_id is the *host* and the guest_* columns identify
    the person who actually ate.

    For an alumni row there is no member at all: an alum is not on the roster,
    hosts nobody and is hosted by nobody, so member_id is NULL and the alumni_*
    columns carry the whole identity. That is why member_id is nullable — every
    query that counts meals against a member already filters by kind, so the
    NULL rows fall out of those reports on their own.
    """

    __tablename__ = "attendance"
    __table_args__ = (
        Index("ix_attendance_member_service_date", "member_id", "service_date"),
        Index("ix_attendance_service_date_period", "service_date", "meal_period_id"),
        # The duplicate guard, enforced by the database and not only by the
        # service layer: a member may hold one un-voided member-meal per period
        # per day. Three deliberate exclusions:
        #   * guest rows — a host may bring guests to a meal they are eating;
        #   * voided rows — an undone check-in must not block a re-scan;
        #   * rows with override_by set — a staff-forced second meal is a
        #     deliberate, audited act, and the guard exists to catch accidents.
        Index(
            "uq_attendance_one_member_meal_per_period",
            "member_id",
            "service_date",
            "meal_period_id",
            unique=True,
            postgresql_where=text(
                "kind = 'member' AND voided_at IS NULL AND override_by IS NULL"
            ),
            sqlite_where=text(
                "kind = 'member' AND voided_at IS NULL AND override_by IS NULL"
            ),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    member_id: Mapped[int | None] = mapped_column(
        ForeignKey("members.id", ondelete="CASCADE"), index=True, nullable=True
    )
    credential_id: Mapped[int | None] = mapped_column(
        ForeignKey("credentials.id", ondelete="SET NULL"), nullable=True
    )
    entry_method: Mapped[str] = mapped_column(String(16), default=EntryMethod.CSN.value)
    meal_period_id: Mapped[int | None] = mapped_column(
        ForeignKey("meal_periods.id", ondelete="SET NULL"), nullable=True
    )

    # service_date is the club's operating day in local time, computed once at
    # write time. Every report groups by this column rather than by a timestamp
    # range, so a DST transition can never create or hide a meal.
    service_date: Mapped[date] = mapped_column(Date, index=True)
    scanned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    kind: Mapped[str] = mapped_column(String(16), default=AttendanceKind.MEMBER.value)
    # A guest is identified, not just labelled: a NetID is what lets the club
    # answer "who was this?" a month later, when "Sam" is not an answer.
    # guest_name is kept as the display form ("First Last") that every report
    # and admin screen already prints, and is derived from the two name parts.
    guest_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    guest_first_name: Mapped[str | None] = mapped_column(String(80), nullable=True)
    guest_last_name: Mapped[str | None] = mapped_column(String(80), nullable=True)
    guest_netid: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Not everyone who eats here has a NetID — a visiting parent, an alum, a
    # prospective member's sibling. Rather than let the field be skipped, the
    # popup makes the gap explicit and asks why, and the answer is recorded
    # here. Exactly one of guest_netid and guest_netid_reason is ever set.
    guest_netid_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # An alum has no PUID on file and no card to tap, so the only way to know
    # who ate is to ask. Name and class year say which alum; an email address or
    # a phone number is what lets the club reach them afterwards — one of the
    # two is required, and the collecting edge decides which (see
    # routers/api_scan.py), the same way it does for a guest's NetID.
    alumni_first_name: Mapped[str | None] = mapped_column(String(80), nullable=True)
    alumni_last_name: Mapped[str | None] = mapped_column(String(80), nullable=True)
    alumni_class_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    alumni_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    alumni_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Optional, and not a substitute for the contact detail above: many alumni
    # keep a NetID for life and it is the cleanest way to tie this meal to the
    # member they used to be, but plenty have long since let it lapse. Asked
    # for, never insisted on.
    alumni_netid: Mapped[str | None] = mapped_column(String(32), nullable=True)

    is_overage: Mapped[bool] = mapped_column(Boolean, default=False)
    status_warning: Mapped[str | None] = mapped_column(String(32), nullable=True)
    override_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    override_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    member: Mapped[Member | None] = relationship()
    meal_period: Mapped[MealPeriod | None] = relationship()

    @property
    def alumni_name(self) -> str:
        """The display form of an alumni row, built from the two name parts.

        A property rather than a stored column: unlike guest_name, which has to
        keep working for rows written before guests had name parts, every alumni
        row has both parts by construction.
        """
        return f"{self.alumni_first_name or ''} {self.alumni_last_name or ''}".strip()


class StaffUser(Base):
    __tablename__ = "staff_users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    display_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default=StaffRole.STAFF.value)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    @property
    def is_admin(self) -> bool:
        return self.role == StaffRole.ADMIN.value


class ClubSetting(Base):
    """Operational knobs staff can change without a redeploy."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(120))
    action: Mapped[str] = mapped_column(String(64), index=True)
    entity_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
