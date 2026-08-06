"""Password hashing, kiosk tokens and the staff PIN check."""
from __future__ import annotations

import hashlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError

from app.config import get_settings

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """Kiosk device tokens are high-entropy already, so a plain SHA-256 is
    sufficient and keeps the per-request lookup cheap."""
    return hashlib.sha256(token.encode()).hexdigest()


def check_staff_pin(pin: str) -> bool:
    expected = get_settings().staff_pin
    return hmac.compare_digest((pin or "").strip(), expected)
