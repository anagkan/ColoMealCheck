from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import PlainTextResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.deps import require_admin, require_staff
from app.models import (
    Attendance,
    ClubSetting,
    Credential,
    MealPeriod,
    Member,
    MemberStatus,
    PlanType,
    StaffRole,
    StaffUser,
)
from app.security import hash_password
from app.services import audit as audit_service
from app.services import credentials as credential_service
from app.services import netid as netid_service
from app.services import photos as photo_service
from app.services import reports
from app.services import roster_import
from app.services.club_settings import load_config, set_value
from app.services.allotment import weekly_usage
from app.services.guests import guest_usage
from app.services.periods import (
    month_bounds,
    resolve_period,
    week_bounds,
    weekly_meal_capacity,
)
from app.services.scan import void_attendance
from app.templating import templates

router = APIRouter(prefix="/admin", tags=["admin"])

WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
ACCOUNT_NOTICES = {
    "created": "Account created.",
    "updated": "Account updated.",
    "password": "Password reset.",
}


def _today(db: Session) -> date:
    """The club's current operating day — not necessarily the calendar date, if
    a late meal is still running past midnight."""
    return resolve_period(db, datetime.now(timezone.utc)).service_date


def _parse_date(raw: str | None, fallback: date) -> date:
    if not raw:
        return fallback
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return fallback


def _root_admin_username() -> str:
    """The account owned by ADMIN_USERNAME/ADMIN_PASSWORD, not by this UI."""
    return (get_settings().admin_username or "admin").strip()


def _clean_account_fields(username: str, display_name: str, role: str) -> tuple[str, str | None, str]:
    clean_username = username.strip()
    clean_display_name = display_name.strip() or None
    if not clean_username:
        raise ValueError("Username is required.")
    if len(clean_username) > 64:
        raise ValueError("Username must be 64 characters or fewer.")
    if any(character.isspace() for character in clean_username):
        raise ValueError("Username cannot contain spaces.")
    if clean_display_name and len(clean_display_name) > 120:
        raise ValueError("Display name must be 120 characters or fewer.")
    if role not in {item.value for item in StaffRole}:
        raise ValueError("Choose either the admin or staff role.")
    return clean_username, clean_display_name, role


def _clean_account_password(password: str) -> str:
    if len(password) < 10:
        raise ValueError("Password must be at least 10 characters.")
    if len(password) > 256:
        raise ValueError("Password must be 256 characters or fewer.")
    return password


def _accounts_context(
    db: Session,
    user: StaffUser,
    *,
    error: str | None = None,
    notice: str | None = None,
    create_values: dict | None = None,
) -> dict:
    root_username = _root_admin_username()
    accounts = list(db.scalars(select(StaffUser).order_by(StaffUser.username)))
    accounts.sort(key=lambda account: (account.username != root_username, account.username.lower()))
    return {
        "user": user,
        "accounts": accounts,
        "roles": [role.value for role in StaffRole],
        "root_username": root_username,
        "error": error,
        "notice": ACCOUNT_NOTICES.get(notice or ""),
        "create_values": create_values or {},
    }


@router.get("/accounts")
def account_list(
    request: Request,
    notice: str | None = None,
    db: Session = Depends(get_db),
    user: StaffUser = Depends(require_admin),
):
    return templates.TemplateResponse(
        request,
        "admin/accounts.html",
        _accounts_context(db, user, notice=notice),
    )


@router.post("/accounts")
def create_account(
    request: Request,
    username: str = Form(...),
    display_name: str = Form(""),
    password: str = Form(...),
    role: str = Form(StaffRole.ADMIN.value),
    db: Session = Depends(get_db),
    user: StaffUser = Depends(require_admin),
):
    values = {"username": username, "display_name": display_name, "role": role}
    try:
        clean_username, clean_display_name, clean_role = _clean_account_fields(
            username, display_name, role
        )
        clean_password = _clean_account_password(password)
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "admin/accounts.html",
            _accounts_context(db, user, error=str(exc), create_values=values),
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )

    if clean_username == _root_admin_username():
        return templates.TemplateResponse(
            request,
            "admin/accounts.html",
            _accounts_context(
                db,
                user,
                error="That username is reserved for the environment-managed root admin.",
                create_values=values,
            ),
            status_code=status.HTTP_409_CONFLICT,
        )
    if db.scalar(select(StaffUser).where(StaffUser.username == clean_username)) is not None:
        return templates.TemplateResponse(
            request,
            "admin/accounts.html",
            _accounts_context(
                db, user, error="That username is already in use.", create_values=values
            ),
            status_code=status.HTTP_409_CONFLICT,
        )

    account = StaffUser(
        username=clean_username,
        display_name=clean_display_name,
        password_hash=hash_password(clean_password),
        role=clean_role,
        is_active=True,
    )
    db.add(account)
    try:
        db.commit()
    except IntegrityError:
        # The explicit lookup gives the normal friendly path; the constraint
        # still has the final word if two admins submit the same name together.
        db.rollback()
        return templates.TemplateResponse(
            request,
            "admin/accounts.html",
            _accounts_context(
                db, user, error="That username is already in use.", create_values=values
            ),
            status_code=status.HTTP_409_CONFLICT,
        )
    db.refresh(account)
    audit_service.record(
        db,
        actor=f"staff:{user.username}",
        action="staff_account.created",
        entity_type="staff_user",
        entity_id=account.id,
        detail={"username": account.username, "role": account.role},
    )
    return RedirectResponse("/admin/accounts?notice=created", status_code=303)


def _account_detail_context(
    user: StaffUser,
    account: StaffUser,
    *,
    error: str | None = None,
    notice: str | None = None,
) -> dict:
    return {
        "user": user,
        "account": account,
        "roles": [role.value for role in StaffRole],
        "is_root": account.username == _root_admin_username(),
        "error": error,
        "notice": ACCOUNT_NOTICES.get(notice or ""),
    }


@router.get("/accounts/{account_id}")
def account_detail(
    account_id: int,
    request: Request,
    notice: str | None = None,
    db: Session = Depends(get_db),
    user: StaffUser = Depends(require_admin),
):
    account = db.get(StaffUser, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    return templates.TemplateResponse(
        request,
        "admin/account_detail.html",
        _account_detail_context(user, account, notice=notice),
    )


@router.post("/accounts/{account_id}")
def update_account(
    account_id: int,
    request: Request,
    username: str = Form(...),
    display_name: str = Form(""),
    role: str = Form(...),
    is_active: str = Form(""),
    db: Session = Depends(get_db),
    user: StaffUser = Depends(require_admin),
):
    account = db.get(StaffUser, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    if account.username == _root_admin_username():
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "The root admin is managed through environment variables.",
        )

    try:
        clean_username, clean_display_name, clean_role = _clean_account_fields(
            username, display_name, role
        )
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "admin/account_detail.html",
            _account_detail_context(user, account, error=str(exc)),
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )

    active = is_active == "on"
    if account.id == user.id and (clean_role != StaffRole.ADMIN.value or not active):
        return templates.TemplateResponse(
            request,
            "admin/account_detail.html",
            _account_detail_context(
                user, account, error="You cannot demote or deactivate your own account."
            ),
            status_code=status.HTTP_409_CONFLICT,
        )
    if clean_username == _root_admin_username():
        return templates.TemplateResponse(
            request,
            "admin/account_detail.html",
            _account_detail_context(
                user,
                account,
                error="That username is reserved for the environment-managed root admin.",
            ),
            status_code=status.HTTP_409_CONFLICT,
        )
    duplicate = db.scalar(
        select(StaffUser).where(
            StaffUser.username == clean_username,
            StaffUser.id != account.id,
        )
    )
    if duplicate is not None:
        return templates.TemplateResponse(
            request,
            "admin/account_detail.html",
            _account_detail_context(user, account, error="That username is already in use."),
            status_code=status.HTTP_409_CONFLICT,
        )

    before = {
        "username": account.username,
        "display_name": account.display_name,
        "role": account.role,
        "is_active": account.is_active,
    }
    account.username = clean_username
    account.display_name = clean_display_name
    account.role = clean_role
    account.is_active = active
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return templates.TemplateResponse(
            request,
            "admin/account_detail.html",
            _account_detail_context(user, account, error="That username is already in use."),
            status_code=status.HTTP_409_CONFLICT,
        )
    after = {
        "username": account.username,
        "display_name": account.display_name,
        "role": account.role,
        "is_active": account.is_active,
    }
    if before != after:
        audit_service.record(
            db,
            actor=f"staff:{user.username}",
            action="staff_account.updated",
            entity_type="staff_user",
            entity_id=account.id,
            detail={"before": before, "after": after},
        )
    return RedirectResponse(f"/admin/accounts/{account.id}?notice=updated", status_code=303)


@router.post("/accounts/{account_id}/password")
def reset_account_password(
    account_id: int,
    request: Request,
    password: str = Form(...),
    db: Session = Depends(get_db),
    user: StaffUser = Depends(require_admin),
):
    account = db.get(StaffUser, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    if account.username == _root_admin_username():
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Change the root admin password through ADMIN_PASSWORD.",
        )
    try:
        clean_password = _clean_account_password(password)
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "admin/account_detail.html",
            _account_detail_context(user, account, error=str(exc)),
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )

    account.password_hash = hash_password(clean_password)
    db.commit()
    audit_service.record(
        db,
        actor=f"staff:{user.username}",
        action="staff_account.password_reset",
        entity_type="staff_user",
        entity_id=account.id,
        detail={"username": account.username},
    )
    return RedirectResponse(f"/admin/accounts/{account.id}?notice=password", status_code=303)


@router.get("")
def dashboard(
    request: Request,
    day: str | None = None,
    db: Session = Depends(get_db),
    user: StaffUser = Depends(require_staff),
):
    target = _parse_date(day, _today(db))
    return templates.TemplateResponse(
        request,
        "admin/today.html",
        {
            "user": user,
            "day": target,
            "counts": reports.daily_counts_by_period(db, target),
            "rows": reports.daily_attendance(db, target),
            "gaps": len(reports.enrollment_gaps(db)),
        },
    )


@router.get("/members")
def member_list(
    request: Request,
    q: str = "",
    status_filter: str = "",
    db: Session = Depends(get_db),
    user: StaffUser = Depends(require_staff),
):
    stmt = select(Member).order_by(Member.last_name, Member.first_name)
    term = q.strip()
    if term:
        pattern = f"%{term}%"
        stmt = stmt.where(
            Member.first_name.ilike(pattern)
            | Member.last_name.ilike(pattern)
            | Member.puid.ilike(pattern)
            | Member.netid.ilike(pattern)
        )
    if status_filter:
        stmt = stmt.where(Member.status == status_filter)

    members = list(db.scalars(stmt))
    enrolled = {
        row.member_id
        for row in db.scalars(select(Credential).where(Credential.is_active.is_(True)))
    }
    return templates.TemplateResponse(
        request,
        "admin/members.html",
        {
            "user": user,
            "members": members,
            "enrolled": enrolled,
            "q": term,
            "status_filter": status_filter,
            "statuses": [s.value for s in MemberStatus],
            "plans": [p.value for p in PlanType],
        },
    )


def _clean_netid(raw: str, db: Session, exclude_member_id: int | None = None) -> str | None:
    """Validate a NetID typed into an admin form, and check nobody else holds it.

    Returns None for a blank one. Blank is allowed here and refused at the kiosk
    on purpose: this form is also how a member who predates the column gets
    edited, and making an unrelated plan change wait on a NetID hunt would just
    teach staff to avoid the form.
    """
    value = netid_service.normalize_netid(raw)
    if not value:
        return None
    if not netid_service.is_valid_netid(value):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, netid_service.NETID_FORMAT_HINT
        )
    stmt = select(Member).where(Member.netid == value)
    if exclude_member_id is not None:
        stmt = stmt.where(Member.id != exclude_member_id)
    holder = db.scalar(stmt)
    if holder is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"NetID {value} is already on file for {holder.full_name}.",
        )
    return value


@router.post("/members")
def create_member(
    first_name: str = Form(...),
    last_name: str = Form(...),
    puid: str = Form(...),
    netid: str = Form(""),
    class_year: str = Form(""),
    plan_type: str = Form(PlanType.PLAN_19.value),
    status_value: str = Form(MemberStatus.ACTIVE.value),
    db: Session = Depends(get_db),
    user: StaffUser = Depends(require_staff),
):
    clean_puid = credential_service.normalize_puid(puid)
    if db.scalar(select(Member).where(Member.puid == clean_puid)):
        raise HTTPException(status.HTTP_409_CONFLICT, f"PUID {clean_puid} is already on file.")

    member = Member(
        first_name=first_name.strip(),
        last_name=last_name.strip(),
        puid=clean_puid,
        netid=_clean_netid(netid, db),
        class_year=int(class_year) if class_year.strip().isdigit() else None,
        plan_type=plan_type,
        status=status_value,
    )
    db.add(member)
    db.commit()
    db.refresh(member)
    audit_service.record(
        db,
        actor=f"staff:{user.username}",
        action="member.created",
        entity_type="member",
        entity_id=member.id,
        detail={"puid": member.puid, "netid": member.netid},
    )
    return RedirectResponse(f"/admin/members/{member.id}", status_code=303)


# --------------------------------------------------------------------------
# Roster import
#
# Declared above /members/{member_id} on purpose: FastAPI matches in
# declaration order, and "import" would otherwise be handed to that route as a
# member id and rejected as a bad integer.
# --------------------------------------------------------------------------


@router.get("/members/import")
def import_form(
    request: Request,
    db: Session = Depends(get_db),
    user: StaffUser = Depends(require_admin),
):
    return templates.TemplateResponse(
        request,
        "admin/member_import.html",
        {
            "user": user,
            "plan": None,
            "csv_text": "",
            "filename": "",
            "error": None,
            "result": None,
            "known_fields": roster_import.KNOWN_FIELDS,
        },
    )


@router.post("/members/import")
async def import_preview(
    request: Request,
    upload: UploadFile | None = None,
    csv_text: str = Form(""),
    filename: str = Form(""),
    confirm: str = Form(""),
    db: Session = Depends(get_db),
    user: StaffUser = Depends(require_admin),
):
    """Preview an uploaded roster, or commit the one already previewed.

    Both halves live in one route because they are one decision. The first POST
    carries a file and gets a plan back; the second carries that same file's
    text and `confirm`, and only then does anything get written. The plan is
    recomputed from the text on the way in, so what commits is measured against
    the database as it is now, not as it was when the preview rendered.
    """
    text = csv_text
    name = filename
    if upload is not None and upload.filename:
        text = ""
        name = upload.filename
        try:
            text = roster_import.decode(await upload.read())
        except roster_import.RosterImportError as exc:
            return _import_page(request, user, error=str(exc))

    if not text.strip():
        return _import_page(request, user, error="Choose a CSV file to upload.")

    try:
        parsed = roster_import.plan(db, text)
    except roster_import.RosterImportError as exc:
        return _import_page(request, user, error=str(exc), csv_text=text, filename=name)

    if not confirm:
        return _import_page(request, user, plan=parsed, csv_text=text, filename=name)

    result = roster_import.apply(db, parsed)
    audit_service.record(
        db,
        actor=f"staff:{user.username}",
        action="roster.imported",
        detail={
            "filename": name,
            "created": len(result.created),
            "updated": len(result.updated),
            "unchanged": result.unchanged,
            "skipped": result.skipped,
            "member_ids": result.created + result.updated,
        },
    )
    return _import_page(request, user, result=result, plan=parsed)


def _import_page(
    request: Request,
    user: StaffUser,
    plan=None,
    csv_text: str = "",
    filename: str = "",
    error: str | None = None,
    result=None,
):
    return templates.TemplateResponse(
        request,
        "admin/member_import.html",
        {
            "user": user,
            "plan": plan,
            "csv_text": csv_text,
            "filename": filename,
            "error": error,
            "result": result,
            "known_fields": roster_import.KNOWN_FIELDS,
        },
    )


@router.get("/members/{member_id}")
def member_detail(
    member_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: StaffUser = Depends(require_staff),
):
    member = db.get(Member, member_id)
    if member is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Member not found")

    config = load_config(db)
    today = _today(db)
    week_start, week_end = week_bounds(today, config.week_start_day)
    month_start, month_end = month_bounds(today)

    recent = list(
        db.scalars(
            select(Attendance)
            .where(Attendance.member_id == member.id)
            .order_by(Attendance.service_date.desc(), Attendance.scanned_at.desc())
            .limit(40)
        )
    )
    return templates.TemplateResponse(
        request,
        "admin/member_detail.html",
        {
            "user": user,
            "member": member,
            "credentials": db.scalars(
                select(Credential)
                .where(Credential.member_id == member.id)
                .order_by(Credential.enrolled_at.desc())
            ).all(),
            "weekly": weekly_usage(db, member, today, config),
            "guests": guest_usage(db, member, today, config),
            "recent": recent,
            "week_start": week_start,
            "week_end": week_end,
            "month_start": month_start,
            "month_end": month_end,
            "statuses": [s.value for s in MemberStatus],
            "plans": [p.value for p in PlanType],
        },
    )


@router.post("/members/{member_id}")
def update_member(
    member_id: int,
    first_name: str = Form(...),
    last_name: str = Form(...),
    puid: str = Form(...),
    netid: str = Form(""),
    class_year: str = Form(""),
    plan_type: str = Form(...),
    status_value: str = Form(...),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    user: StaffUser = Depends(require_staff),
):
    member = db.get(Member, member_id)
    if member is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Member not found")

    # Checked before anything is assigned: a clash must leave the member exactly
    # as they were, not half-edited.
    clean_netid = _clean_netid(netid, db, exclude_member_id=member.id)

    before = {"plan_type": member.plan_type, "status": member.status}
    member.first_name = first_name.strip()
    member.last_name = last_name.strip()
    member.puid = credential_service.normalize_puid(puid)
    member.netid = clean_netid
    member.class_year = int(class_year) if class_year.strip().isdigit() else None
    member.plan_type = plan_type
    member.status = status_value
    member.notes = notes.strip() or None
    db.commit()

    after = {"plan_type": member.plan_type, "status": member.status}
    if before != after:
        # Plan and status changes affect what members are entitled to, so they
        # get their own audit entry rather than a generic "edited".
        audit_service.record(
            db,
            actor=f"staff:{user.username}",
            action="member.entitlement_changed",
            entity_type="member",
            entity_id=member.id,
            detail={"before": before, "after": after},
        )
    return RedirectResponse(f"/admin/members/{member_id}", status_code=303)


@router.post("/members/{member_id}/photo")
async def upload_photo(
    member_id: int,
    photo: UploadFile,
    db: Session = Depends(get_db),
    user: StaffUser = Depends(require_staff),
):
    member = db.get(Member, member_id)
    if member is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Member not found")

    raw = await photo.read()
    try:
        filename = photo_service.save_image_bytes(raw, member.id)
    except photo_service.PhotoError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    old = member.photo_path
    member.photo_path = filename
    db.commit()
    photo_service.delete_photo(old)
    return RedirectResponse(f"/admin/members/{member_id}", status_code=303)


@router.post("/credentials/{credential_id}/revoke")
def revoke_credential(
    credential_id: int,
    db: Session = Depends(get_db),
    user: StaffUser = Depends(require_staff),
):
    credential = db.get(Credential, credential_id)
    if credential is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Credential not found")
    credential_service.revoke(db, credential)
    db.commit()
    audit_service.record(
        db,
        actor=f"staff:{user.username}",
        action="credential.revoked",
        entity_type="member",
        entity_id=credential.member_id,
        detail={"credential_id": credential.id, "value": credential.value},
    )
    return RedirectResponse(f"/admin/members/{credential.member_id}", status_code=303)


@router.post("/attendance/{attendance_id}/void")
def void_row(
    attendance_id: int,
    db: Session = Depends(get_db),
    user: StaffUser = Depends(require_staff),
):
    row = db.get(Attendance, attendance_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    void_attendance(db, row, actor=f"staff:{user.username}")
    return RedirectResponse(f"/admin?day={row.service_date.isoformat()}", status_code=303)


@router.get("/schedule")
def schedule(
    request: Request,
    db: Session = Depends(get_db),
    user: StaffUser = Depends(require_staff),
):
    periods = list(
        db.scalars(select(MealPeriod).order_by(MealPeriod.weekday, MealPeriod.sort_order))
    )
    by_day: dict[int, list[MealPeriod]] = {i: [] for i in range(7)}
    for period in periods:
        by_day[period.weekday].append(period)
    return templates.TemplateResponse(
        request,
        "admin/schedule.html",
        {
            "user": user,
            "by_day": by_day,
            "weekday_names": WEEKDAY_NAMES,
            "capacity": weekly_meal_capacity(db),
            "config": load_config(db),
        },
    )


@router.post("/schedule")
def add_period(
    name: str = Form(...),
    weekday: int = Form(...),
    start_time: str = Form(...),
    end_time: str = Form(...),
    counts: str = Form("on"),
    db: Session = Depends(get_db),
    user: StaffUser = Depends(require_admin),
):
    db.add(
        MealPeriod(
            name=name.strip(),
            weekday=int(weekday),
            start_time=time.fromisoformat(start_time),
            end_time=time.fromisoformat(end_time),
            counts_toward_allotment=counts == "on",
            is_active=True,
            sort_order=int(time.fromisoformat(start_time).hour),
        )
    )
    db.commit()
    return RedirectResponse("/admin/schedule", status_code=303)


@router.post("/schedule/{period_id}/delete")
def delete_period(
    period_id: int,
    db: Session = Depends(get_db),
    user: StaffUser = Depends(require_admin),
):
    period = db.get(MealPeriod, period_id)
    if period is not None:
        # Deactivate rather than delete: past attendance rows point here, and
        # the history should keep saying which meal it was.
        period.is_active = False
        db.commit()
    return RedirectResponse("/admin/schedule", status_code=303)


@router.get("/settings")
def settings_page(
    request: Request,
    db: Session = Depends(get_db),
    user: StaffUser = Depends(require_admin),
):
    rows = list(db.scalars(select(ClubSetting).order_by(ClubSetting.key)))
    return templates.TemplateResponse(
        request, "admin/settings.html", {"user": user, "rows": rows}
    )


@router.post("/settings")
async def update_settings(
    request: Request,
    db: Session = Depends(get_db),
    user: StaffUser = Depends(require_admin),
):
    form = await request.form()
    for key, value in form.items():
        set_value(db, key, str(value))
    audit_service.record(
        db,
        actor=f"staff:{user.username}",
        action="settings.updated",
        detail=dict(form),
    )
    return RedirectResponse("/admin/settings", status_code=303)


@router.get("/reports")
def reports_page(
    request: Request,
    week: str | None = None,
    month: str | None = None,
    db: Session = Depends(get_db),
    user: StaffUser = Depends(require_staff),
):
    config = load_config(db)
    today = _today(db)
    week_anchor = _parse_date(week, today)
    month_anchor = _parse_date(month, today)
    month_start, month_end = month_bounds(month_anchor)
    week_start, week_end = week_bounds(week_anchor, config.week_start_day)

    return templates.TemplateResponse(
        request,
        "admin/reports.html",
        {
            "user": user,
            "week_rows": reports.weekly_usage_report(db, week_anchor, config),
            "week_start": week_start,
            "week_end": week_end,
            "overages": reports.monthly_overages(db, month_start, month_end),
            "guest_rows": reports.guest_usage_report(db, month_start, month_end),
            "gaps": reports.enrollment_gaps(db),
            "month_start": month_start,
            "month_end": month_end,
        },
    )


@router.get("/reports/{name}.csv")
def report_csv(
    name: str,
    week: str | None = None,
    month: str | None = None,
    db: Session = Depends(get_db),
    user: StaffUser = Depends(require_staff),
):
    config = load_config(db)
    today = _today(db)
    week_anchor = _parse_date(week, today)
    month_start, month_end = month_bounds(_parse_date(month, today))
    week_start, week_end = week_bounds(week_anchor, config.week_start_day)

    if name == "weekly-usage":
        rows = [
            [
                r.member.last_name,
                r.member.first_name,
                r.member.puid,
                r.member.netid or "",
                r.member.class_year or "",
                r.member.plan_type,
                r.member.status,
                r.used,
                r.allotment if r.allotment is not None else "",
                r.over_by,
                r.guest_meals,
            ]
            for r in reports.weekly_usage_report(db, week_anchor, config)
        ]
        body = reports.to_csv(
            [
                "last_name",
                "first_name",
                "puid",
                "netid",
                "class_year",
                "plan",
                "status",
                "meals_used",
                "allotment",
                "over_by",
                "guest_meals_hosted",
            ],
            rows,
        )
        filename = f"weekly-usage-{week_start}-to-{week_end}.csv"

    elif name == "overages":
        body = reports.to_csv(
            ["last_name", "first_name", "puid", "netid", "plan", "overage_meals"],
            [
                [m.last_name, m.first_name, m.puid, m.netid or "", m.plan_type, count]
                for m, count in reports.monthly_overages(db, month_start, month_end)
            ],
        )
        filename = f"overages-{month_start:%Y-%m}.csv"

    elif name == "guests":
        body = reports.to_csv(
            ["last_name", "first_name", "puid", "netid", "guest_meals_hosted"],
            [
                [m.last_name, m.first_name, m.puid, m.netid or "", count]
                for m, count in reports.guest_usage_report(db, month_start, month_end)
            ],
        )
        filename = f"guest-meals-{month_start:%Y-%m}.csv"

    elif name == "enrollment-gaps":
        body = reports.to_csv(
            ["last_name", "first_name", "puid", "netid", "class_year"],
            [
                [m.last_name, m.first_name, m.puid, m.netid or "", m.class_year or ""]
                for m in reports.enrollment_gaps(db)
            ],
        )
        filename = "enrollment-gaps.csv"

    elif name == "daily":
        day = _parse_date(week, today)
        # Alumni rows carry their own identity — there is no member row to join
        # back to — so the export has to include it or the contact details the
        # kiosk collected never leave the database.
        body = reports.to_csv(
            ["service_date", "period", "kind", "member", "puid", "guest_name",
             "guest_netid", "guest_netid_reason", "alumni_name",
             "alumni_class_year", "alumni_netid", "alumni_email", "alumni_phone",
             "entry_method", "overage", "scanned_at"],
            [
                [
                    r.service_date,
                    r.meal_period.name if r.meal_period else "",
                    r.kind,
                    r.member.full_name if r.member else "",
                    r.member.puid if r.member else "",
                    r.guest_name or "",
                    r.guest_netid or "",
                    r.guest_netid_reason or "",
                    r.alumni_name,
                    r.alumni_class_year or "",
                    r.alumni_netid or "",
                    r.alumni_email or "",
                    r.alumni_phone or "",
                    r.entry_method,
                    "yes" if r.is_overage else "",
                    r.scanned_at.isoformat() if r.scanned_at else "",
                ]
                for r in reports.daily_attendance(db, day)
            ],
        )
        filename = f"attendance-{day}.csv"

    else:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown report")

    return PlainTextResponse(
        body,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# How each sortable column on the analytics page turns into a sort key. Sorting
# happens here rather than in SQL because several columns (meals per week,
# utilization) are derived properties, and a member list is small enough that
# the difference is not measurable.
ANALYTICS_SORTS = {
    "name": lambda r: (r.member.last_name.lower(), r.member.first_name.lower()),
    "class_year": lambda r: r.member.class_year or 0,
    "plan": lambda r: r.member.plan_type,
    "status": lambda r: r.member.status,
    "card": lambda r: r.has_card,
    "meals": lambda r: r.meals,
    "per_week": lambda r: r.meals_per_week,
    "utilization": lambda r: r.utilization or 0,
    "days": lambda r: r.days_attended,
    "guests": lambda r: r.guest_meals,
    "overages": lambda r: r.overages,
    "last_seen": lambda r: r.last_seen or date.min,
}

# Rows with nothing to sort by belong at the bottom whichever way the column is
# pointing — "no class year on file" is not the highest class year, and someone
# who has never eaten here is not the most recently seen. Folding these into the
# sort key instead would flip them to the top on every descending sort.
ANALYTICS_MISSING = {
    "class_year": lambda r: r.member.class_year is None,
    "utilization": lambda r: r.utilization is None,
    "last_seen": lambda r: r.last_seen is None,
}

DEFAULT_ANALYTICS_WINDOW_DAYS = 30


def _analytics_rows(
    db: Session,
    start: str | None,
    end: str | None,
    q: str,
    class_year: str,
    plan: str,
    status_filter: str,
    sort: str,
    direction: str,
) -> dict:
    """Shared by the analytics page and its CSV, so the export is always exactly
    what is on screen — same window, same filters, same order."""
    config = load_config(db)
    today = _today(db)
    end_date = _parse_date(end, today)
    start_date = _parse_date(
        start, end_date - timedelta(days=DEFAULT_ANALYTICS_WINDOW_DAYS - 1)
    )
    if start_date > end_date:
        start_date, end_date = end_date, start_date

    rows = reports.member_analytics(db, start_date, end_date, config)
    # The breakdowns describe the whole club, not the filtered slice — otherwise
    # filtering to one class year would show that class as 100% of the club.
    breakdowns = {
        "class_years": reports.by_class_year(rows),
        "plans": reports.by_plan(rows),
        "statuses": reports.by_status(rows),
    }
    years = sorted(
        {r.member.class_year for r in rows if r.member.class_year}, reverse=True
    )

    term = q.strip()
    if term:
        needle = term.lower()
        rows = [
            r
            for r in rows
            if needle in r.member.first_name.lower()
            or needle in r.member.last_name.lower()
            or needle in r.member.puid.lower()
            or needle in (r.member.netid or "")
        ]
    if class_year.strip().isdigit():
        rows = [r for r in rows if r.member.class_year == int(class_year)]
    if plan:
        rows = [r for r in rows if r.member.plan_type == plan]
    if status_filter:
        rows = [r for r in rows if r.member.status == status_filter]

    sort_key = sort if sort in ANALYTICS_SORTS else "name"
    descending = direction == "desc"
    # rows arrive ordered by name, and Python's sort is stable in both
    # directions, so equal values stay alphabetical rather than shuffling.
    is_missing = ANALYTICS_MISSING.get(sort_key)
    if is_missing is None:
        rows.sort(key=ANALYTICS_SORTS[sort_key], reverse=descending)
    else:
        known = [r for r in rows if not is_missing(r)]
        known.sort(key=ANALYTICS_SORTS[sort_key], reverse=descending)
        rows = known + [r for r in rows if is_missing(r)]

    return {
        "rows": rows,
        "breakdowns": breakdowns,
        "years": years,
        "start": start_date,
        "end": end_date,
        "sort": sort_key,
        "dir": "desc" if descending else "asc",
        "q": term,
        "class_year": class_year,
        "plan": plan,
        "status_filter": status_filter,
    }


@router.get("/analytics")
def analytics_page(
    request: Request,
    start: str | None = None,
    end: str | None = None,
    q: str = "",
    class_year: str = "",
    plan: str = "",
    status_filter: str = "",
    sort: str = "name",
    dir: str = "asc",
    db: Session = Depends(get_db),
    user: StaffUser = Depends(require_staff),
):
    view = _analytics_rows(db, start, end, q, class_year, plan, status_filter, sort, dir)
    rows = view["rows"]

    def sort_link(column: str) -> str:
        """A header link that sorts by `column`, flipping direction if it is
        already the sorted column. Counts and dates start descending — the
        interesting end of "who ate most" is the top."""
        if view["sort"] == column:
            next_dir = "asc" if view["dir"] == "desc" else "desc"
        else:
            next_dir = "asc" if column in ("name", "plan", "status") else "desc"
        params = {
            "start": view["start"].isoformat(),
            "end": view["end"].isoformat(),
            "q": view["q"],
            "class_year": view["class_year"],
            "plan": view["plan"],
            "status_filter": view["status_filter"],
            "sort": column,
            "dir": next_dir,
        }
        return "/admin/analytics?" + urlencode({k: v for k, v in params.items() if v})

    return templates.TemplateResponse(
        request,
        "admin/analytics.html",
        {
            "user": user,
            **view,
            "sort_link": sort_link,
            "csv_query": urlencode(
                {
                    k: v
                    for k, v in {
                        "start": view["start"].isoformat(),
                        "end": view["end"].isoformat(),
                        "q": view["q"],
                        "class_year": view["class_year"],
                        "plan": view["plan"],
                        "status_filter": view["status_filter"],
                        "sort": view["sort"],
                        "dir": view["dir"],
                    }.items()
                    if v
                }
            ),
            "totals": {
                "members": len(rows),
                "active": sum(1 for r in rows if r.member.status == "active"),
                "meals": sum(r.meals for r in rows),
                "guest_meals": sum(r.guest_meals for r in rows),
                "overages": sum(r.overages for r in rows),
                "no_card": sum(1 for r in rows if not r.has_card),
                "dormant": sum(1 for r in rows if r.is_dormant),
            },
            "statuses": [s.value for s in MemberStatus],
            "plans": [p.value for p in PlanType],
        },
    )


@router.get("/analytics.csv")
def analytics_csv(
    start: str | None = None,
    end: str | None = None,
    q: str = "",
    class_year: str = "",
    plan: str = "",
    status_filter: str = "",
    sort: str = "name",
    dir: str = "asc",
    db: Session = Depends(get_db),
    user: StaffUser = Depends(require_staff),
):
    view = _analytics_rows(db, start, end, q, class_year, plan, status_filter, sort, dir)
    body = reports.to_csv(
        [
            "last_name",
            "first_name",
            "puid",
            "netid",
            "class_year",
            "plan",
            "status",
            "card_linked",
            "meals",
            "meals_per_week",
            "plan_used_pct",
            "days_attended",
            "guest_meals_hosted",
            "overages",
            "last_seen",
        ],
        [
            [
                r.member.last_name,
                r.member.first_name,
                r.member.puid,
                r.member.netid or "",
                r.member.class_year or "",
                r.member.plan_type,
                r.member.status,
                "yes" if r.has_card else "no",
                r.meals,
                r.meals_per_week,
                r.utilization if r.utilization is not None else "",
                r.days_attended,
                r.guest_meals,
                r.overages,
                r.last_seen.isoformat() if r.last_seen else "",
            ]
            for r in view["rows"]
        ],
    )
    filename = f"member-analytics-{view['start']}-to-{view['end']}.csv"
    return PlainTextResponse(
        body,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/audit")
def audit_page(
    request: Request,
    db: Session = Depends(get_db),
    user: StaffUser = Depends(require_admin),
):
    return templates.TemplateResponse(
        request,
        "admin/audit.html",
        {"user": user, "entries": audit_service.recent(db, limit=300)},
    )
