"""Alumni meals: a meal eaten by somebody who is not on the roster.

An alum has no card, no PUID and no host. What the club needs instead is a way
to reach them afterwards, which is why a name and class year alone are not
enough — one contact detail has to come with it. Which one is the alum's choice:
an email address and a phone number are equally good answers to "how do we get
hold of this person", so the rule is *at least one*, never both.

The formats are checked here for the same reason NetIDs are (see guests.py): the
kiosk is the one moment somebody is standing there to correct a typo, and an
unreachable contact detail makes the whole record worthless.
"""
from __future__ import annotations

import re

# Deliberately permissive: this is a typo check, not an RFC 5322 parser. It
# catches the mistakes people actually make at a kiosk — a missing @, a trailing
# comma, a domain with no dot — and lets everything else through rather than
# refusing an address that turns out to be real.
_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s.]+(\.[^@\s.]+)+")

# Kept as digits with an optional leading +, so two spellings of one number
# ("(609) 555-1234" and "609-555-1234") land on the same string and a report can
# group them. Ten digits is a US number; the upper bound is E.164's fifteen.
_PHONE_DIGITS_RE = re.compile(r"\+?[0-9]{10,15}")
_PHONE_STRIP_RE = re.compile(r"[\s().\-]")

EMAIL_FORMAT_HINT = "That email address does not look right — check for a typo."
PHONE_FORMAT_HINT = (
    "A phone number is at least ten digits, like 609-555-1234."
)
CONTACT_REQUIRED_HINT = (
    "Enter an email address or a phone number for this alum — either one is enough."
)


def normalize_email(value: str) -> str:
    """Addresses are case-insensitive in practice; store one spelling."""
    return (value or "").strip().lower()


def is_valid_email(value: str) -> bool:
    return bool(_EMAIL_RE.fullmatch(normalize_email(value)))


def normalize_phone(value: str) -> str:
    """Drop the punctuation people type and keep the digits that identify it."""
    return _PHONE_STRIP_RE.sub("", (value or "").strip())


def is_valid_phone(value: str) -> bool:
    return bool(_PHONE_DIGITS_RE.fullmatch(normalize_phone(value)))
