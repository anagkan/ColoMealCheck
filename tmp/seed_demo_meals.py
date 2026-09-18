"""Populate the local screenshot roster through the app's past-meal workflow.

Run inside the API container with this file supplied on standard input.
Only members with the demo NetID and PUID pair are eligible.
"""

from collections import Counter
from datetime import datetime, timedelta
from random import Random

from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.models import Attendance, Member
from app.services import past_meals


now = datetime.now(get_settings().tz)
end = now.date() - timedelta(days=1)
start = end - timedelta(days=6)
rng = Random(20260918)
actor = "demo:screenshot-seed"
added = Counter()
skipped = Counter()


with SessionLocal() as db:
    members = {}
    for number in range(1, 41):
        member = db.scalar(select(Member).where(Member.netid == f"demo{number:03d}"))
        if member is None or member.puid != f"999{number:06d}":
            raise RuntimeError(f"Expected fictional roster member demo{number:03d}.")
        members[number] = member

    existing = set()
    for row in db.scalars(select(Attendance).where(
        Attendance.service_date.between(start, end),
        Attendance.voided_at.is_(None),
    )):
        name = (row.guest_name if row.kind == "guest" else
                row.alumni_name if row.kind == "alumni" else "")
        existing.add((row.service_date.isoformat(), row.meal_period_id,
                      row.kind, row.member_id, name))

    def register(day, period, kind, member=None, **details):
        name = (f"{details['first_name']} {details['last_name']}"
                if kind != "member" else "")
        key = (day.isoformat(), period.id, kind, member.id if member else None, name)
        if key in existing:
            skipped[kind] += 1
            return
        values = {
            "day": day.isoformat(), "meal_period_id": str(period.id),
            "kind": kind, **details,
        }
        if member:
            values["member_id"] = str(member.id)
        past_meals.register(db, values, actor=actor, now=now)
        existing.add(key)
        added[kind] += 1

    guest_names = [
        ("Avery", "Bennett"), ("Rowan", "Chen"), ("Quinn", "Patel"),
        ("Harper", "Rivera"), ("Morgan", "Brooks"), ("Parker", "Kim"),
        ("Alex", "Reed"),
    ]
    alumni_names = [
        ("Vivian", "Cooper"), ("Jasper", "Ahmed"), ("Nora", "Ellis"),
        ("Caleb", "Santos"), ("June", "Sawyer"), ("Simon", "Wells"),
        ("Daphne", "Ross"),
    ]

    for offset in range(7):
        day = start + timedelta(days=offset)
        periods = [p for p in past_meals.completed_periods(db, day, now) if p.is_active]
        if not periods:
            raise RuntimeError(f"No service periods for {day}.")
        for period in periods:
            for number in range(1, 29):
                member = members[number]
                probability = {"Breakfast": 0.42, "Lunch": 0.72,
                               "Dinner": 0.88, "Brunch": 0.80}.get(period.name, 0.6)
                if member.plan_type == "rca_paa":
                    probability *= 0.7
                elif member.plan_type == "none":
                    probability *= 0.35
                # Two frequent diners exercise RCA/PAA overage reporting.
                chosen = rng.random() < probability
                if chosen or number in (3, 10):
                    register(day, period, "member", member)

        dinner = next((p for p in periods if p.name == "Dinner"), periods[-1])
        # Keep two active members dormant and show a few membership warnings.
        if offset in (2, 5):
            register(day, dinner, "member", members[32 if offset == 2 else 36])

        first, last = guest_names[offset]
        register(day, dinner, "guest", members[offset + 1],
                 first_name=first, last_name=last, netid=f"guest{offset + 1:03d}")
        register(day, dinner, "guest", members[offset + 8],
                 first_name="Jamie", last_name=members[offset + 8].last_name,
                 guest_is_family="on")
        if offset % 2 == 0:
            register(day, dinner, "guest", members[offset + 15],
                     first_name="Robin", last_name="Whitaker", guest_is_professor="on")
        if offset in (1, 4):
            register(day, dinner, "guest", members[offset + 20],
                     first_name="Casey", last_name="Monroe",
                     netid_reason="Fictional visiting friend for screenshot demo")
        # A third ordinary guest for this host demonstrates an audited override.
        if offset in (3, 6):
            details = {"first_name": "Taylor", "last_name": "Bennett", "netid": "guest099"}
            if offset == 6:
                details.update(override_quota="on", override_reason="Fictional screenshot demo: additional guest")
            register(day, dinner, "guest", members[1], **details)

        first, last = alumni_names[offset]
        register(day, dinner, "alumni", first_name=first, last_name=last,
                 class_year=str(2026 - offset), email=f"demo.alum{offset + 1}@example.com")
        if day.weekday() in (5, 6):
            register(day, periods[0], "alumni", first_name="Emery", last_name="Lawson",
                     class_year="2020", phone="6095550101")

    rows = list(db.scalars(select(Attendance).where(
        Attendance.service_date.between(start, end), Attendance.voided_at.is_(None)
    )))
    print(f"Date range: {start} through {end}")
    print("Added:", dict(added))
    print("Already present:", dict(skipped))
    print("Totals:", dict(Counter(row.kind for row in rows)))
    print("Overages:", sum(row.is_overage for row in rows))
    print("Membership warnings:", sum(bool(row.status_warning) for row in rows))
    print("Guest overrides:", sum(row.kind == "guest" and bool(row.override_by) for row in rows))
    print("By day:", dict(sorted(Counter(str(row.service_date) for row in rows).items())))
