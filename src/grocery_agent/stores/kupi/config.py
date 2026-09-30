import re
from urllib.parse import urlsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class KupiSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GROCERY_KUPI_", env_file=".env", extra="ignore")

    listing_url: str = "https://www.kupi.cz/slevy/ovoce-a-zelenina"
    max_pages: int = Field(default=100, ge=1, le=1000)
    request_delay_seconds: float = Field(default=1, ge=0, le=60)
    attempts: int = Field(default=3, ge=1, le=5)

    @field_validator("listing_url")
    @classmethod
    def public_category(cls, value: str) -> str:
        url = urlsplit(value)
        if (
            url.scheme != "https"
            or url.netloc != "www.kupi.cz"
            or re.fullmatch(r"/slevy/[a-z0-9]+(?:-[a-z0-9]+)*", url.path) is None
            or url.query
            or url.fragment
        ):
            raise ValueError("use an unfiltered https://www.kupi.cz/slevy/<category> URL")
        return value
