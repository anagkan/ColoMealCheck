"""Princeton NetIDs, wherever they are collected.

This started life inside the guest popup, which was the only place that asked
for one. Members are identified by a NetID too now, so the rule lives here
rather than in services/guests.py — a member enrollment validating itself
against something imported from the guest module would be a lie about what the
code means.

A NetID is a short lowercase alphanumeric handle beginning with a letter —
"ak9981", "jsmith". Checked only where a human types one in, for the same reason
PUIDs are: that is the one moment somebody is standing there to correct it.
"""
from __future__ import annotations

import re

_NETID_RE = re.compile(r"[a-z][a-z0-9]{1,7}")

NETID_FORMAT_HINT = "A NetID is 2–8 letters and digits starting with a letter, like ak9981."


def normalize_netid(value: str) -> str:
    """NetIDs are case-insensitive; store one spelling so reports can group."""
    return (value or "").strip().replace(" ", "").lower()


def is_valid_netid(value: str) -> bool:
    return bool(_NETID_RE.fullmatch(normalize_netid(value)))
