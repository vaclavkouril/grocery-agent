import re
import tomllib
from datetime import time
from pathlib import Path
from typing import Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import field_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from grocery_agent.models.common import StoreId


class CollectorSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GROCERY_COLLECTOR_", env_file=".env", extra="ignore"
    )

    sources: tuple[StoreId, ...] = ("kupi",)
    daily_at: str = "02:00"
    timezone: str = "Europe/Prague"
    run_on_startup: bool = True
    # At most one scheduled round per local calendar day; no automatic retry storm.

    @field_validator("sources")
    @classmethod
    def valid_sources(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or len(set(value)) != len(value):
            raise ValueError("collector sources must be nonempty and unique")
        return value

    @field_validator("daily_at")
    @classmethod
    def valid_time(cls, value: str) -> str:
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("daily_at must be HH:MM in 24-hour format")
        return value

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("unknown collector timezone") from exc
        return value

    @property
    def local_time(self) -> time:
        return time.fromisoformat(self.daily_at)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return env_settings, dotenv_settings, init_settings, file_secret_settings

    @classmethod
    def load(cls, path: Path) -> Self:
        with path.open("rb") as file:
            payload = tomllib.load(file)
        unknown = payload.keys() - cls.model_fields.keys()
        if unknown:
            raise ValueError(f"unknown collector settings: {', '.join(sorted(unknown))}")
        return cls(**payload)
