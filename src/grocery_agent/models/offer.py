from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, HttpUrl, computed_field, model_validator

from grocery_agent.models.common import DomainModel, ExactDecimal, MoneyAmount, NonEmpty
from grocery_agent.models.normalization import canonical_quantity
from grocery_agent.models.product import Quantity, StoreProduct, Unit


class Currency(StrEnum):
    CZK = "CZK"
    EUR = "EUR"


class Availability(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class PromotionType(StrEnum):
    PRICE_CUT = "price_cut"
    LOYALTY = "loyalty"
    MULTIBUY = "multibuy"
    COUPON = "coupon"
    OTHER = "other"


class Promotion(DomainModel):
    kind: PromotionType
    label: NonEmpty | None = None
    requires_loyalty: bool = False
    minimum_purchase: int = Field(default=1, ge=1)
    conditions: NonEmpty | None = None
    advertised_discount_percent: Annotated[ExactDecimal, Field(ge=0, le=100)] | None = None

    @model_validator(mode="after")
    def coherent_terms(self) -> Self:
        if self.kind == PromotionType.LOYALTY and not self.requires_loyalty:
            raise ValueError("loyalty promotions must require loyalty")
        if self.kind == PromotionType.MULTIBUY and self.minimum_purchase < 2:
            raise ValueError("multibuy promotions must require at least two price-basis units")
        if self.kind in {PromotionType.COUPON, PromotionType.OTHER} and not self.conditions:
            raise ValueError("coupon/other promotions require human-readable conditions")
        return self


class UnitPrice(DomainModel):
    amount: MoneyAmount
    unit: Unit


class Offer(DomainModel):
    product: StoreProduct
    offer_key: NonEmpty = "standard"
    scope: NonEmpty = "national"
    current_price: MoneyAmount
    regular_price: MoneyAmount | None = None
    currency: Currency = Currency.CZK
    price_basis: Quantity
    promotion: Promotion | None = None
    valid_from: date | None = None
    valid_until: date | None = None
    availability: Availability = Availability.UNKNOWN
    source_url: HttpUrl

    @model_validator(mode="after")
    def coherent_offer(self) -> Self:
        if self.valid_from and self.valid_until and self.valid_until < self.valid_from:
            raise ValueError("valid_until must be on or after valid_from (inclusive dates)")
        if self.product.variable_weight and self.price_basis.unit not in {Unit.KG, Unit.G}:
            raise ValueError("variable-weight products require a mass price basis")
        if self.promotion:
            discount = self.promotion.advertised_discount_percent
            if self.regular_price is not None and self.current_price > self.regular_price:
                raise ValueError("a promoted current price cannot exceed the regular price")
            if self.promotion.kind == PromotionType.PRICE_CUT or discount is not None:
                if self.regular_price is None or self.regular_price <= 0:
                    raise ValueError("discount promotions require a positive regular price")
                if self.current_price >= self.regular_price:
                    raise ValueError("discount promotions require a lower current price")
            if discount is not None and self.discount_percent is not None:
                if abs(discount - self.discount_percent) > Decimal(1):
                    raise ValueError("advertised discount disagrees with prices by over one point")
        # Derived prices must be representable too, not fail only when read downstream.
        self.unit_price
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def discount_percent(self) -> Decimal | None:
        if not self.regular_price or self.current_price >= self.regular_price:
            return None
        return ((1 - self.current_price / self.regular_price) * 100).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def unit_price(self) -> UnitPrice:
        """Price per kg/l/piece/package. Conditional purchase terms still apply."""
        basis = self.price_basis
        amount, unit = canonical_quantity(basis.amount, basis.unit)
        contents = self.product.quantity
        if basis.unit in {Unit.PIECE, Unit.PACKAGE} and contents is not None:
            if contents.unit not in {Unit.PIECE, Unit.PACKAGE}:
                content_amount, unit = canonical_quantity(contents.amount, contents.unit)
                amount *= content_amount
        return UnitPrice(
            amount=(self.current_price / amount).quantize(
                Decimal("0.0001"), rounding=ROUND_HALF_UP
            ),
            unit=Unit(unit),
        )
