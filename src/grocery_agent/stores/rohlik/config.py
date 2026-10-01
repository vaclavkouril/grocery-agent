from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class RohlikSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GROCERY_ROHLIK_", env_file=".env", extra="ignore")

    category_ids: tuple[int, ...] = (
        300102000,  # Fruit and vegetables
        300105000,  # Dairy and chilled
        300103000,  # Meat and fish
        300101000,  # Bakery
        300104000,  # Deli
        300107000,  # Frozen
        300121429,  # Plant based
        300106000,  # Food cupboard
        300108000,  # Drinks
        300112393,  # Special diets
        300124876,  # Ready meals
    )
    page_size: int = Field(default=100, ge=1, le=100)
    max_pages: int = Field(default=100, ge=1, le=1000)
    request_delay_seconds: float = Field(default=1, ge=0, le=60)
    attempts: int = Field(default=3, ge=1, le=5)
    expected_warehouse_id: int | None = Field(default=None, gt=0)

    @field_validator("category_ids")
    @classmethod
    def valid_categories(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if not value or len(value) != len(set(value)) or any(v <= 0 for v in value):
            raise ValueError("supply nonempty, unique positive category IDs")
        return value
