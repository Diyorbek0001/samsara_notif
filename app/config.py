from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str = ""
    token_encryption_key: str = ""
    samsara_api_token: str = ""
    telegram_fault_group_id: int | None = None
    telegram_group_id: int | None = None
    telegram_fault_topic_id: int | None = None
    telegram_safety_topic_id: int | None = None
    telegram_system_topic_id: int | None = None
    admin_telegram_ids: str = ""
    support_telegram_ids: str = ""
    safety_team_telegram_ids: str = ""
    maintenance_team_telegram_ids: str = ""
    database_url: str = "sqlite+aiosqlite:///./fleet_faults.db"
    samsara_base_url: str = "https://api.samsara.com"
    samsara_fault_poll_seconds: int = Field(30, ge=5)
    safety_poll_seconds: int = Field(15, ge=5)
    safety_notification_delay_seconds: int = Field(90, ge=0, le=600)
    samsara_request_timeout_seconds: float = Field(30, gt=0)
    samsara_max_retries: int = Field(5, ge=1, le=10)
    timezone: str = "America/New_York"
    log_level: str = "INFO"
    log_file: str = "logs/fault-monitor.log"
    send_initial_active_faults: bool = False
    send_initial_safety_events: bool = False
    safety_initial_lookback_minutes: int = Field(10, ge=1, le=1440)
    safety_min_severity: str = "low"
    safety_allowed_behaviors: str = ""
    safety_ignored_behaviors: str = ""
    send_cleared_alerts: bool = True
    send_repeat_alerts: bool = False
    repeat_alert_after_hours: int = Field(24, ge=1)
    fault_batch_window_seconds: int = Field(10, ge=0)
    fault_stale_after_minutes: int = Field(15, ge=1)
    fault_reopen_window_hours: int = Field(24, ge=0)
    enable_location_lookup: bool = True
    enable_driver_lookup: bool = False
    samsara_mock_mode: bool = False
    samsara_mock_fixture: Path = Path("tests/fixtures/samsara/fault_feed_initial.json")
    worker_lock_name: str = "fleet_samsara_monitor"

    @field_validator(
        "telegram_fault_group_id",
        "telegram_group_id",
        "telegram_fault_topic_id",
        "telegram_safety_topic_id",
        "telegram_system_topic_id",
        mode="before",
    )
    @classmethod
    def parse_optional_id(cls, value: object) -> object:
        return None if value in (None, "") else value

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        ZoneInfo(value)
        return value

    @field_validator("database_url")
    @classmethod
    def async_postgres_url(cls, value: str) -> str:
        if value.startswith("postgres://"):
            return "postgresql+asyncpg://" + value.removeprefix("postgres://")
        if value.startswith("postgresql://"):
            return "postgresql+asyncpg://" + value.removeprefix("postgresql://")
        return value

    @property
    def authorized_ids(self) -> frozenset[int]:
        values = (
            f"{self.admin_telegram_ids},{self.support_telegram_ids},"
            f"{self.safety_team_telegram_ids},{self.maintenance_team_telegram_ids}"
        )
        return frozenset(
            int(item.strip())
            for item in values.split(",")
            if item.strip().lstrip("-").isdigit()
        )

    @property
    def authorized_usernames(self) -> frozenset[str]:
        values = (
            f"{self.admin_telegram_ids},{self.support_telegram_ids},"
            f"{self.safety_team_telegram_ids},{self.maintenance_team_telegram_ids}"
        )
        return frozenset(
            item.strip().lstrip("@").lower()
            for item in values.split(",")
            if item.strip()
            and not item.strip().lstrip("-").isdigit()
            and item.strip().lstrip("@")
        )

    def is_authorized(self, user_id: int | None, username: str | None) -> bool:
        return (
            user_id is not None and user_id in self.authorized_ids
        ) or (
            username is not None and username.lower().lstrip("@") in self.authorized_usernames
        )

    @property
    def admin_ids(self) -> frozenset[int]:
        return frozenset(
            int(item.strip()) for item in self.admin_telegram_ids.split(",")
            if item.strip().lstrip("-").isdigit()
        )

    @property
    def admin_usernames(self) -> frozenset[str]:
        return frozenset(
            item.strip().lstrip("@").lower()
            for item in self.admin_telegram_ids.split(",")
            if item.strip() and not item.strip().lstrip("-").isdigit()
        )

    def is_admin(self, user_id: int | None, username: str | None) -> bool:
        return (user_id is not None and user_id in self.admin_ids) or (
            username is not None
            and username.lower().lstrip("@") in self.admin_usernames
        )

    @property
    def group_id(self) -> int | None:
        return self.telegram_group_id or self.telegram_fault_group_id

    @property
    def allowed_safety_behaviors(self) -> frozenset[str]:
        return frozenset(item.strip().lower() for item in self.safety_allowed_behaviors.split(",") if item.strip())

    @property
    def ignored_safety_behaviors(self) -> frozenset[str]:
        return frozenset(item.strip().lower() for item in self.safety_ignored_behaviors.split(",") if item.strip())

    def require_monitor(self) -> None:
        missing = []
        if not self.samsara_mock_mode and not self.samsara_api_token:
            missing.append("SAMSARA_API_TOKEN")
        if missing:
            raise ValueError("Missing required configuration: " + ", ".join(missing))

    def require_bot(self) -> None:
        missing = []
        if not self.bot_token:
            missing.append("BOT_TOKEN")
        if not self.token_encryption_key:
            missing.append("TOKEN_ENCRYPTION_KEY")
        if missing:
            raise ValueError("Missing required configuration: " + ", ".join(missing))


@lru_cache
def get_settings() -> Settings:
    return Settings()
