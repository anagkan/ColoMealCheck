"""Colonial's default service schedule.

Weekdays run breakfast / lunch / dinner; weekends run brunch / dinner. That is
3x5 + 2x2 = 19 servings per week, which is what makes the 19-meal plan mean
"every meal we serve". The 14-meal plan is simply a smaller *number* — a
14-plan member who eats breakfast just spends one of their 14 on it.

Staff can edit all of this in the admin UI; this is only the starting point.
"""
from __future__ import annotations

from datetime import time

MONDAY, TUESDAY, WEDNESDAY, THURSDAY, FRIDAY, SATURDAY, SUNDAY = range(7)

WEEKDAY_SERVICE = [
    ("Breakfast", time(8, 0), time(10, 0), 10),
    ("Lunch", time(11, 45), time(13, 45), 20),
    ("Dinner", time(17, 45), time(19, 45), 30),
]

WEEKEND_SERVICE = [
    ("Brunch", time(11, 30), time(13, 30), 15),
    ("Dinner", time(17, 45), time(19, 45), 30),
]


def default_periods() -> list[dict]:
    rows: list[dict] = []
    for weekday in (MONDAY, TUESDAY, WEDNESDAY, THURSDAY, FRIDAY):
        for name, start, end, order in WEEKDAY_SERVICE:
            rows.append(
                {
                    "name": name,
                    "weekday": weekday,
                    "start_time": start,
                    "end_time": end,
                    "counts_toward_allotment": True,
                    "is_active": True,
                    "sort_order": order,
                }
            )
    for weekday in (SATURDAY, SUNDAY):
        for name, start, end, order in WEEKEND_SERVICE:
            rows.append(
                {
                    "name": name,
                    "weekday": weekday,
                    "start_time": start,
                    "end_time": end,
                    "counts_toward_allotment": True,
                    "is_active": True,
                    "sort_order": order,
                }
            )
    return rows
