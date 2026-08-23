from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.models import CredentialType, MemberStatus, PlanType


class ScanRequest(BaseModel):
    """One endpoint serves both the reader and the keypad.

    `credential_type` is the only thing that differs between a tapped card and
    a typed PUID — everything downstream is shared.
    """

    value: str = Field(min_length=1, max_length=128)
    credential_type: str = CredentialType.CSN.value
    force: bool = False
    staff_pin: str | None = None
    # Which of the two meals either side of now a between-meals check-in is
    # for — the member's answer to the offer in ScanResponse.offers. A Literal
    # rather than a plain string so a typo is refused here rather than silently
    # resolving to no window at all.
    attach: Literal["previous", "next"] | None = None
    # Set by the kiosk when replaying a scan it queued while the server was
    # unreachable, so a meal eaten at 18:05 is not recorded as 18:40. Live
    # scans send it too; the server ignores anything implausible.
    occurred_at: datetime | None = None


class GuestRequest(BaseModel):
    """A guest meal hosted by `member_id`.

    The guest fields are declared optional here and required by the route
    instead, so a missing one comes back as a sentence the kiosk can show
    ("Enter the guest's first and last name.") rather than as pydantic's nested
    error list, which the popup would have nothing sensible to do with.

    A guest is identified by `guest_netid`, or — for the visiting parent, the
    alum, the sibling who has none — by `guest_netid_reason` saying why there
    isn't one. The route requires exactly one of the two.
    """

    member_id: int
    guest_first_name: str = Field(default="", max_length=80)
    guest_last_name: str = Field(default="", max_length=80)
    guest_netid: str = Field(default="", max_length=32)
    guest_netid_reason: str = Field(default="", max_length=255)
    staff_pin: str | None = None
    override_reason: str | None = Field(default=None, max_length=255)


class AlumniMealRequest(BaseModel):
    """A meal eaten by an alum, hosted by nobody.

    Like GuestRequest, the fields are declared loosely here and required by the
    route, so a refusal reads as a sentence the kiosk can print.

    Name and class year say which alum. `email` and `phone` are alternatives —
    at least one has to arrive, and the route says so in those words. `netid` is
    optional and stands outside that rule: it identifies the alum but does not
    reach them, so it can neither be required nor stand in for a contact detail.
    """

    first_name: str = Field(default="", max_length=80)
    last_name: str = Field(default="", max_length=80)
    class_year: int | None = Field(default=None, ge=1900, le=2200)
    email: str = Field(default="", max_length=255)
    phone: str = Field(default="", max_length=32)
    netid: str = Field(default="", max_length=32)


class UndoRequest(BaseModel):
    attendance_id: int


class EnrollRequest(BaseModel):
    """Bind a card to somebody already on file — the replacement-card path."""

    member_id: int
    value: str = Field(min_length=1, max_length=128)
    credential_type: str = CredentialType.CSN.value
    photo_data_url: str | None = None
    staff_pin: str


class EnrollNewRequest(BaseModel):
    """Enrol someone who is not in the system yet.

    This is the ordinary case: a member arrives at the start of term holding a
    TigerCard nobody has seen before, and one trip through this form is what
    creates them. Their identity and their card are written in the same
    transaction, so there is never a member row with no way to check in.
    """

    first_name: str = Field(min_length=1, max_length=80)
    last_name: str = Field(min_length=1, max_length=80)
    puid: str = Field(min_length=1, max_length=20)
    # Declared loosely and required by the route, like the guest fields: a
    # missing NetID comes back as a sentence the enrollment page can print.
    netid: str = Field(default="", max_length=32)
    class_year: int | None = Field(default=None, ge=1900, le=2200)
    plan_type: str = PlanType.PLAN_19.value
    status: str = MemberStatus.ACTIVE.value
    value: str = Field(min_length=1, max_length=128)
    credential_type: str = CredentialType.CSN.value
    photo_data_url: str | None = None
    staff_pin: str


class MemberSummary(BaseModel):
    id: int
    first_name: str
    last_name: str
    full_name: str
    puid: str
    netid: str | None
    class_year: int | None
    plan_type: str
    status: str
    photo_url: str | None


class WeeklyUsageOut(BaseModel):
    used: int
    allotment: int | None
    remaining: int | None
    week_start: date
    week_end: date
    is_over: bool


class GuestUsageOut(BaseModel):
    used: int
    quota: int
    remaining: int
    month_start: date


class AlumniSummary(BaseModel):
    """Who ate, on a row with no member behind it.

    The kiosk's result screen is built around `member`; an alumni meal has none,
    so it needs its own answer to "whose name goes on the screen" rather than
    falling through to the card-not-recognized case.
    """

    full_name: str
    class_year: int | None
    email: str | None
    phone: str | None
    netid: str | None


class AdjacentPeriodOut(BaseModel):
    """A meal the kiosk may offer to attach a between-meals check-in to.

    `seconds_away` is a duration rather than a timestamp, for the reason the
    banner countdown is: the kiosk laptop's own clock is not to be trusted, and
    "starts in 12 min" survives a wrong one where "starts at 17:45" does not.
    """

    direction: Literal["previous", "next"]
    period_name: str
    service_date: date
    seconds_away: int


class ScanResponse(BaseModel):
    outcome: str
    ok: bool
    message: str
    warnings: list[str] = []
    member: MemberSummary | None = None
    alumni: AlumniSummary | None = None
    period_name: str | None = None
    service_date: date | None = None
    weekly: WeeklyUsageOut | None = None
    guests: GuestUsageOut | None = None
    attendance_id: int | None = None
    checked_in_at: datetime | None = None
    undo_seconds: int = 60
    result_seconds: int = 6
    # Echoed back so the kiosk can offer "enroll this card" without the user
    # having to tap again.
    submitted_value: str | None = None
    submitted_type: str | None = None
    # Only ever populated on outcome="outside_service": the meals the member may
    # check in against anyway. Empty means there is nothing to offer, so the
    # kiosk shows no box at all.
    offers: list[AdjacentPeriodOut] = []
