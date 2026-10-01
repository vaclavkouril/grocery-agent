from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class CatalogueSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GROCERY_CATALOGUE_", env_file=".env", extra="ignore"
    )
    max_age_hours: Annotated[int, Field(ge=1, le=168)] = 36
    timezone: str = "Europe/Prague"
    allow_cached_on_failure: bool = True

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("unknown catalogue timezone") from exc
        return value
