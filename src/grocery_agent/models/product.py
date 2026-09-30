from enum import StrEnum
from uuid import UUID

from pydantic import computed_field, field_validator

from grocery_agent.models.common import DomainModel, NonEmpty, PositiveAmount, StoreId
from grocery_agent.models.normalization import normalize_gtin, normalize_name


class Unit(StrEnum):
    PIECE = "piece"
    PACKAGE = "package"
    KG = "kg"
    G = "g"
    L = "l"
    ML = "ml"
    SERVING = "serving"


class Quantity(DomainModel):
    amount: PositiveAmount
    unit: Unit


class ProductDetails(DomainModel):
    name: NonEmpty
    brand: NonEmpty | None = None
    category: NonEmpty | None = None
    quantity: Quantity | None = None
    gtin: str | None = None
    variable_weight: bool = False

    @field_validator("gtin")
    @classmethod
    def valid_gtin(cls, value: str | None) -> str | None:
        return normalize_gtin(value) if value is not None else None

    @field_validator("name")
    @classmethod
    def usable_name(cls, value: str) -> str:
        if not normalize_name(value):
            raise ValueError("product name must contain letters or numbers")
        return value

    @computed_field  # type: ignore[prop-decorator]
    @property
    def normalized_name(self) -> str:
        return normalize_name(self.name)


class Product(ProductDetails):
    """Resolved identity; acquisition does not need to supply one."""

    id: UUID


class StoreProduct(ProductDetails):
    store_id: StoreId
    sku: NonEmpty
