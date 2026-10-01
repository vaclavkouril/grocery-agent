from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, Field, model_validator

from grocery_agent.models.common import DomainModel, MoneyAmount, NonEmpty, StoreId
from grocery_agent.models.offer import Currency, Offer
from grocery_agent.models.product import Unit


class CatalogueQuery(DomainModel):
    source_id: StoreId
    scope: NonEmpty | None = None
    category: NonEmpty | None = None
    retailers: tuple[StoreId, ...] = ()
    search: Annotated[str | None, Field(max_length=200)] = None
    unit: Unit | None = None
    currency: Currency = Currency.CZK
    max_unit_price: MoneyAmount | None = None
    allow_loyalty: bool = False
    include_from: bool = False
    include_unavailable: bool = False
    sort: Literal["name", "unit_price"] = "name"
    offset: Annotated[int, Field(strict=True, ge=0)] = 0
    limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 50

    @model_validator(mode="after")
    def comparable_units(self) -> Self:
        if self.unit is None and (self.sort == "unit_price" or self.max_unit_price is not None):
            raise ValueError("unit-price filtering/sorting requires a unit")
        if len(set(self.retailers)) != len(self.retailers):
            raise ValueError("retailers must be unique")
        return self


class CatalogueState(DomainModel):
    source_id: StoreId
    run_id: str
    batch_finished_at: AwareDatetime
    published_at: AwareDatetime
    fresh_until: AwareDatetime
    latest_run_id: str
    latest_run_status: str
    degraded: bool = False
    warnings: tuple[str, ...] = ()


class CatalogueItem(DomainModel):
    offer: Offer
    observed_at: AwareDatetime


class CataloguePage(DomainModel):
    generated_at: AwareDatetime
    state: CatalogueState
    total: int
    offset: int
    limit: int
    items: tuple[CatalogueItem, ...]
