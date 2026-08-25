"""Bulk roster upload: a CSV of member details, with no cards in it.

The club gets its roster as a spreadsheet — names, PUIDs, NetIDs, class years,
plans — long before anybody has tapped a card. Cards are bound one at a time at
the kiosk, so this importer deliberately has no card column: it fills the
members table and leaves every one of those people on the enrollment-gaps list
until they turn up and tap. They can eat in the meantime by typing their PUID.

Two properties the rest of the design leans on:

  * Nothing is written until staff have seen what would happen. `plan()` reads
    the file and the database and returns exactly what `apply()` would do; the
    admin screen renders that, and only a second POST commits it.
  * Re-uploading the same file is a no-op, and re-uploading a corrected one
    fixes just the corrected cells. A roster arrives in several passes in
    practice, and an importer that duplicates or refuses on the second pass is
    one staff stop using.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Member, MemberStatus, PlanType
from app.services import credentials as credential_service
from app.services import netid as netid_service

# A roster is a few hundred rows. These caps exist so a mis-picked file fails
# fast with a sentence instead of tying up the box parsing it.
MAX_BYTES = 2 * 1024 * 1024
MAX_ROWS = 5000

# Spreadsheets come from a dozen places with a dozen spellings of the same
# column. Everything is matched case- and punctuation-insensitively, so
# "Class Year", "class_year" and "CLASS-YEAR" are one column.
FIELD_ALIASES = {
    "first_name": "first_name",
    "first": "first_name",
    "firstname": "first_name",
    "given_name": "first_name",
    "last_name": "last_name",
    "last": "last_name",
    "lastname": "last_name",
    "surname": "last_name",
    "family_name": "last_name",
    "puid": "puid",
    "pu_id": "puid",
    "university_id": "puid",
    "netid": "netid",
    "net_id": "netid",
    "class_year": "class_year",
    "class": "class_year",
    "year": "class_year",
    "grad_year": "class_year",
    "graduation_year": "class_year",
    "plan_type": "plan_type",
    "plan": "plan_type",
    "meal_plan": "plan_type",
    "status": "status",
    "member_status": "status",
}

REQUIRED_COLUMNS = ("first_name", "last_name", "puid")

# The columns a file may fill, in the order the preview shows them.
KNOWN_FIELDS = ("first_name", "last_name", "puid", "netid", "class_year", "plan_type", "status")

PLAN_ALIASES = {
    "19": PlanType.PLAN_19.value,
    "plan19": PlanType.PLAN_19.value,
    "19_meals": PlanType.PLAN_19.value,
    "14": PlanType.PLAN_14.value,
    "plan14": PlanType.PLAN_14.value,
    "14_meals": PlanType.PLAN_14.value,
    "rca": PlanType.RCA_PAA.value,
    "paa": PlanType.RCA_PAA.value,
    "rca/paa": PlanType.RCA_PAA.value,
    "9": PlanType.RCA_PAA.value,
    "none": PlanType.NONE.value,
    "no_plan": PlanType.NONE.value,
}

# Wide enough for a five-year senior and an incoming freshman, narrow enough to
# catch a PUID pasted into the year column.
MIN_CLASS_YEAR = 1900
MAX_CLASS_YEAR = 2100

CREATE = "create"
UPDATE = "update"
UNCHANGED = "unchanged"
ERROR = "error"


class RosterImportError(ValueError):
    """The file cannot be read at all — bad encoding, or a missing column.

    Distinct from a row-level problem, which is reported in the preview
    alongside the rows that are fine.
    """


@dataclass
class RowPlan:
    """What would happen to one line of the file."""

    line: int  # 1-based line number in the uploaded file, header included
    action: str
    puid: str = ""
    name: str = ""
    member_id: int | None = None
    errors: list[str] = field(default_factory=list)
    # field -> (what is on file now, what the file would set). Empty for a
    # create, where every value is new by definition.
    changes: dict[str, tuple[str, str]] = field(default_factory=dict)
    values: dict[str, object] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.action != ERROR


@dataclass
class ImportPlan:
    rows: list[RowPlan] = field(default_factory=list)
    ignored_columns: list[str] = field(default_factory=list)

    def of(self, action: str) -> list[RowPlan]:
        return [row for row in self.rows if row.action == action]

    @property
    def creates(self) -> list[RowPlan]:
        return self.of(CREATE)

    @property
    def updates(self) -> list[RowPlan]:
        return self.of(UPDATE)

    @property
    def unchanged(self) -> list[RowPlan]:
        return self.of(UNCHANGED)

    @property
    def errors(self) -> list[RowPlan]:
        return self.of(ERROR)

    @property
    def writes(self) -> int:
        return len(self.creates) + len(self.updates)


@dataclass
class ImportResult:
    created: list[int] = field(default_factory=list)
    updated: list[int] = field(default_factory=list)
    unchanged: int = 0
    skipped: int = 0

    @property
    def summary(self) -> str:
        parts = [f"{len(self.created)} added", f"{len(self.updated)} updated"]
        if self.unchanged:
            parts.append(f"{self.unchanged} already matched")
        if self.skipped:
            parts.append(f"{self.skipped} skipped")
        return ", ".join(parts) + "."


def decode(raw: bytes) -> str:
    """Bytes to text, tolerating what a spreadsheet actually exports.

    Excel writes UTF-8 with a BOM on one platform and cp1252 on another, and an
    accented name in a cp1252 file is not valid UTF-8. Guessing here beats
    telling a club officer to re-export.
    """
    if len(raw) > MAX_BYTES:
        raise RosterImportError(
            f"That file is larger than {MAX_BYTES // (1024 * 1024)} MB — is it really a roster CSV?"
        )
    if not raw.strip():
        raise RosterImportError("That file is empty.")
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise RosterImportError("That file is not readable text — export it from the sheet as CSV.")


def _normalize_header(name: str) -> str:
    cleaned = (name or "").strip().lower()
    for ch in (" ", "-", ".", "#"):
        cleaned = cleaned.replace(ch, "_")
    return cleaned.strip("_")


def _clean_plan(raw: str) -> str | None:
    value = _normalize_header(raw)
    if value in {p.value for p in PlanType}:
        return value
    return PLAN_ALIASES.get(value)


def _clean_status(raw: str) -> str | None:
    value = _normalize_header(raw)
    if value in {s.value for s in MemberStatus}:
        return value
    return None


def _clean_class_year(raw: str) -> int | None:
    digits = (raw or "").strip()
    if not digits.isdigit():
        return None
    year = int(digits)
    if not MIN_CLASS_YEAR <= year <= MAX_CLASS_YEAR:
        return None
    return year


def plan(db: Session, text: str) -> ImportPlan:
    """Read the file against the database and say what each row would do.

    Never writes. The preview screen and `apply` both call this, so what staff
    approve is what runs — recomputed at apply time rather than carried across
    the two requests, in case somebody edited a member in between.
    """
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration as exc:
        raise RosterImportError("That file has no header row.") from exc

    mapping: dict[int, str] = {}
    ignored: list[str] = []
    for index, raw_name in enumerate(header):
        canonical = FIELD_ALIASES.get(_normalize_header(raw_name))
        if canonical is None:
            if raw_name.strip():
                ignored.append(raw_name.strip())
            continue
        # A column repeated in the header would silently shadow itself; first
        # one wins, the rest are reported as ignored.
        if canonical in mapping.values():
            ignored.append(raw_name.strip())
            continue
        mapping[index] = canonical

    missing = [name for name in REQUIRED_COLUMNS if name not in mapping.values()]
    if missing:
        raise RosterImportError(
            "The file is missing required column(s): "
            + ", ".join(missing)
            + ". A header row naming first_name, last_name and puid is the minimum."
        )

    result = ImportPlan(ignored_columns=ignored)
    # Duplicates inside one file are their own class of mistake — a PUID pasted
    # twice would otherwise be an update of a member this same import created.
    seen_puids: dict[str, int] = {}
    seen_netids: dict[str, int] = {}

    for offset, raw_row in enumerate(reader):
        line = offset + 2  # header is line 1
        if not any(cell.strip() for cell in raw_row):
            continue  # trailing blank lines are what a spreadsheet leaves behind
        if len(result.rows) >= MAX_ROWS:
            raise RosterImportError(
                f"That file has more than {MAX_ROWS} rows — split it, or check it is a roster."
            )
        cells = {
            name: (raw_row[index] if index < len(raw_row) else "")
            for index, name in mapping.items()
        }
        result.rows.append(_plan_row(db, line, cells, seen_puids, seen_netids))

    return result


def _plan_row(
    db: Session,
    line: int,
    cells: dict[str, str],
    seen_puids: dict[str, int],
    seen_netids: dict[str, int],
) -> RowPlan:
    errors: list[str] = []
    first = (cells.get("first_name") or "").strip()
    last = (cells.get("last_name") or "").strip()
    puid = credential_service.normalize_puid(cells.get("puid", ""))
    name = f"{first} {last}".strip()

    if not first:
        errors.append("First name is blank.")
    if not last:
        errors.append("Last name is blank.")
    puid_ok = True
    if not puid:
        errors.append("PUID is blank.")
        puid_ok = False
    elif not credential_service.is_valid_puid(puid):
        errors.append(credential_service.PUID_FORMAT_HINT)
        puid_ok = False
    elif puid in seen_puids:
        errors.append(f"PUID {puid} already appears on line {seen_puids[puid]} of this file.")
        puid_ok = False

    netid: str | None = None
    raw_netid = (cells.get("netid") or "").strip()
    if raw_netid:
        netid = netid_service.normalize_netid(raw_netid)
        if not netid_service.is_valid_netid(netid):
            errors.append(netid_service.NETID_FORMAT_HINT)
            netid = None
        elif netid in seen_netids:
            errors.append(
                f"NetID {netid} already appears on line {seen_netids[netid]} of this file."
            )
            netid = None

    class_year: int | None = None
    raw_year = (cells.get("class_year") or "").strip()
    if raw_year:
        class_year = _clean_class_year(raw_year)
        if class_year is None:
            errors.append(f"'{raw_year}' is not a class year like 2028.")

    plan_type: str | None = None
    raw_plan = (cells.get("plan_type") or "").strip()
    if raw_plan:
        plan_type = _clean_plan(raw_plan)
        if plan_type is None:
            errors.append(
                f"'{raw_plan}' is not a meal plan. Use one of: "
                + ", ".join(p.value for p in PlanType)
                + "."
            )

    status_value: str | None = None
    raw_status = (cells.get("status") or "").strip()
    if raw_status:
        status_value = _clean_status(raw_status)
        if status_value is None:
            errors.append(
                f"'{raw_status}' is not a status. Use one of: "
                + ", ".join(s.value for s in MemberStatus)
                + "."
            )

    # The PUID is the identity: no usable one means there is nothing to match a
    # row against, and the row is an error either way.
    existing = db.scalar(select(Member).where(Member.puid == puid)) if puid_ok else None

    # A NetID belongs to exactly one member. Held by somebody else, it is an
    # error; held by this same member, it is simply already correct.
    if netid:
        holder = db.scalar(select(Member).where(Member.netid == netid))
        if holder is not None and (existing is None or holder.id != existing.id):
            errors.append(f"NetID {netid} is already on file for {holder.full_name}.")
            netid = None

    if errors:
        return RowPlan(line=line, action=ERROR, puid=puid, name=name, errors=errors)

    seen_puids[puid] = line
    if netid:
        seen_netids[netid] = line

    if existing is None:
        # Blank cells on a new member fall back to the same defaults the "Add a
        # member" form offers.
        values = {
            "first_name": first,
            "last_name": last,
            "puid": puid,
            "netid": netid,
            "class_year": class_year,
            "plan_type": plan_type or PlanType.PLAN_19.value,
            "status": status_value or MemberStatus.ACTIVE.value,
        }
        return RowPlan(line=line, action=CREATE, puid=puid, name=name, values=values)

    # On an existing member a blank cell means "leave this alone", not "clear
    # it". Rosters arrive half-filled, and a missing NetID column must not wipe
    # the NetIDs collected at the kiosk.
    proposed: dict[str, object] = {"first_name": first, "last_name": last}
    if netid:
        proposed["netid"] = netid
    if class_year is not None:
        proposed["class_year"] = class_year
    if plan_type:
        proposed["plan_type"] = plan_type
    if status_value:
        proposed["status"] = status_value

    changes: dict[str, tuple[str, str]] = {}
    for key, new_value in proposed.items():
        current = getattr(existing, key)
        if current != new_value:
            changes[key] = (
                "—" if current in (None, "") else str(current),
                str(new_value),
            )

    return RowPlan(
        line=line,
        action=UPDATE if changes else UNCHANGED,
        puid=puid,
        name=name,
        member_id=existing.id,
        changes=changes,
        values={key: proposed[key] for key in changes},
    )


def apply(db: Session, import_plan: ImportPlan) -> ImportResult:
    """Write an already-computed plan. One transaction, so a roster lands whole.

    Rows marked as errors are skipped, not fatal: a file with three bad lines
    and two hundred good ones should import the two hundred, and the preview
    shows exactly which three were left behind.
    """
    result = ImportResult(
        unchanged=len(import_plan.unchanged), skipped=len(import_plan.errors)
    )

    for row in import_plan.creates:
        member = Member(**row.values)
        db.add(member)
        db.flush()  # need the id for the audit detail before the single commit
        result.created.append(member.id)

    for row in import_plan.updates:
        member = db.get(Member, row.member_id)
        if member is None:  # pragma: no cover - deleted between preview and apply
            result.skipped += 1
            continue
        for key, value in row.values.items():
            setattr(member, key, value)
        result.updated.append(member.id)

    db.commit()
    return result
