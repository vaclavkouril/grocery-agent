import re
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class TescoSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GROCERY_TESCO_", env_file=".env", extra="ignore")

    # None discovers all ordinary top-level departments from the public catalog navigation.
    category_slugs: tuple[str, ...] | None = None
    max_pages: int = Field(default=500, ge=1, le=1000)
    request_delay_seconds: float = Field(default=1, ge=0, le=60)
    attempts: int = Field(default=3, ge=1, le=5)
    browser_headless: bool = False
    browser_executable: Path | None = None
    navigation_timeout_ms: int = Field(default=30000, ge=1000, le=120000)

    @field_validator("category_slugs")
    @classmethod
    def valid_categories(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is None:
            return value
        if not value or len(set(value)) != len(value):
            raise ValueError("supply nonempty, unique category slugs")
        if any(re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug) is None for slug in value):
            raise ValueError("invalid public category slug")
        if set(value) & {"top-vyber", "novinky"}:
            raise ValueError("choose ordinary catalog departments, not curated promotional lists")
        return value
