from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, Field, model_validator

from grocery_agent.models.common import DomainModel, MoneyAmount, NonEmpty, StoreId
from grocery_agent.models.offer import Currency, Offer
from grocery_agent.models.product import Unit


class CatalogueQuery(DomainModel):
    source_id: StoreId
    profile_fingerprint: Annotated[str | None, Field(pattern=r"^(?:legacy|[0-9a-f]{24})$")] = None
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
    warning_codes: tuple[str, ...] = ()
    profile_fingerprint: str = "legacy"
    coverage_complete: bool = False
    actual_scope: str | None = None
    actual_scopes: tuple[str, ...] = ()


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


class CatalogueSnapshot(DomainModel):
    """Pinned catalogue inputs used by a request."""

    source_ids: tuple[StoreId, ...]
    profile_fingerprints: tuple[str, ...] = ()
    run_ids: tuple[str, ...] = ()
    complete: bool = True
    warnings: tuple[str, ...] = ()

    @model_validator(mode="after")
    def aligned_runs(self) -> Self:
        if self.run_ids and len(self.run_ids) != len(self.source_ids):
            raise ValueError("one pinned run is required for each source")
        if self.profile_fingerprints and len(self.profile_fingerprints) != len(self.source_ids):
            raise ValueError("one profile fingerprint is required for each source")
        return self
