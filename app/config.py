from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process-level configuration.

    Operational knobs that staff might want to change without a redeploy live in
    the `settings` database table instead (see app/services/club_settings.py).
    This class is only for things that must be known before the database exists.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://colonial:colonial@localhost:5432/colomealcheck"
    secret_key: str = "dev-secret-change-me"
    timezone: str = "America/New_York"
    photo_dir: Path = Path("data/photos")

    bootstrap_admin_username: str = "admin"
    bootstrap_admin_password: str = ""

    # Typed at the kiosk to authorize enrollment, overrides and forced entries.
    # The kiosk is a shared device in a dining room; a PIN is the right weight of
    # security here, and every use of it is written to the audit log.
    staff_pin: str = "1234"

    # Set only when the kiosk's reader cannot act as a keyboard and
    # bridge/reader_bridge.py is supplying scans instead — normally
    # "ws://127.0.0.1:8765", pointing at the kiosk laptop's own loopback rather
    # than at this server. Empty disables the bridge client entirely.
    kiosk_bridge_url: str = ""

    session_cookie: str = "colomeal_session"
    session_max_age_seconds: int = 60 * 60 * 12

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@lru_cache
def get_settings() -> Settings:
    return Settings()
