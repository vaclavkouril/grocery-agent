import re
from urllib.parse import urlsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class KupiSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GROCERY_KUPI_", env_file=".env", extra="ignore")

    # A single URL overrides the category list, retaining the original focused-scrape option.
    listing_url: str | None = None
    category_slugs: tuple[str, ...] = (
        "ovoce-a-zelenina",
        "maso-drubez-a-ryby",
        "mlecne-vyrobky-a-vejce",
        "pecivo",
        "konzervy",
        "lahudky",
        "mrazene-a-instantni-potraviny",
        "nealko-napoje",
        "alkohol",
        "sladkosti-a-slane-snacky",
        "vareni-a-peceni",
        "zdrava-vyziva",
        "pro-deti",
    )
    max_pages: int = Field(default=100, ge=1, le=1000)
    request_delay_seconds: float = Field(default=1, ge=0, le=60)
    attempts: int = Field(default=3, ge=1, le=5)

    @field_validator("listing_url")
    @classmethod
    def public_category(cls, value: str | None) -> str | None:
        if value is None:
            return value
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

    @field_validator("category_slugs")
    @classmethod
    def valid_categories(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or len(value) != len(set(value)):
            raise ValueError("supply nonempty, unique category slugs")
        if any(re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug) is None for slug in value):
            raise ValueError("invalid public category slug")
        return value

    @property
    def listing_urls(self) -> tuple[str, ...]:
        if self.listing_url is not None:
            return (self.listing_url,)
        return tuple(f"https://www.kupi.cz/slevy/{slug}" for slug in self.category_slugs)
