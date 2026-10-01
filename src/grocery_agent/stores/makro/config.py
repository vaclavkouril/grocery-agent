import re
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class MakroSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GROCERY_MAKRO_", env_file=".env", extra="ignore")

    session_path: Path = Path("data/makro/session.json")
    # A local alias prevents customer-specific prices from appearing nationally applicable.
    account_scope: str = Field(default="local", pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    store_name: str | None = None
    category_paths: tuple[str, ...] = ("potraviny", "nepotravinové-zboží")
    max_pages: int = Field(default=1000, ge=1, le=2000)
    request_delay_seconds: float = Field(default=1, ge=0, le=60)
    browser_headless: bool = True
    browser_executable: Path | None = None
    navigation_timeout_ms: int = Field(default=45000, ge=1000, le=120000)

    @field_validator("category_paths")
    @classmethod
    def valid_categories(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or len(set(value)) != len(value):
            raise ValueError("supply unique nonempty category paths")
        for path in value:
            if re.fullmatch(r"[\w-]+(?:/[\w-]+)*", path) is None or path.split("/")[0] not in {
                "potraviny",
                "nepotravinové-zboží",
            }:
                raise ValueError("choose ordinary Makro category paths")
        if any(a != b and b.startswith(a + "/") for a in value for b in value):
            raise ValueError("category paths must not overlap")
        return value
