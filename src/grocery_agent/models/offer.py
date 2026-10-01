from datetime import date
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, HttpUrl, computed_field, model_validator

from grocery_agent.models.common import DomainModel, ExactDecimal, MoneyAmount, NonEmpty
from grocery_agent.models.normalization import canonical_quantity
from grocery_agent.models.product import Quantity, StoreProduct, Unit
from grocery_agent.models.purchase import PurchaseCost, PurchaseTerms


class Currency(StrEnum):
    CZK = "CZK"
    EUR = "EUR"


class Availability(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class PriceQualifier(StrEnum):
    EXACT = "exact"
    FROM = "from"


class PromotionType(StrEnum):
    ADVERTISED = "advertised"
    PRICE_CUT = "price_cut"
    LOYALTY = "loyalty"
    MULTIBUY = "multibuy"
    COUPON = "coupon"
    OTHER = "other"


class DiscountReference(StrEnum):
    REGULAR_PRICE = "regular_price"
    UNSPECIFIED = "unspecified"


class Promotion(DomainModel):
    kind: PromotionType
    label: NonEmpty | None = None
    requires_loyalty: bool = False
    minimum_purchase: int = Field(default=1, ge=1)
    conditions: NonEmpty | None = None
    advertised_discount_percent: Annotated[ExactDecimal, Field(ge=0, le=100)] | None = None
    discount_reference: DiscountReference = DiscountReference.REGULAR_PRICE

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
    price_qualifier: PriceQualifier = PriceQualifier.EXACT


class Offer(DomainModel):
    product: StoreProduct
    offer_key: NonEmpty = "standard"
    scope: NonEmpty = "national"
    current_price: MoneyAmount
    price_qualifier: PriceQualifier = PriceQualifier.EXACT
    regular_price: MoneyAmount | None = None
    currency: Currency = Currency.CZK
    price_basis: Quantity
    promotion: Promotion | None = None
    valid_from: date | None = None
    valid_until: date | None = None
    availability: Availability = Availability.UNKNOWN
    purchase_terms: PurchaseTerms | None = None
    source_url: HttpUrl

    @model_validator(mode="after")
    def coherent_offer(self) -> Self:
        if self.valid_from and self.valid_until and self.valid_until < self.valid_from:
            raise ValueError("valid_until must be on or after valid_from (inclusive dates)")
        if self.product.variable_weight and self.price_basis.unit not in {Unit.KG, Unit.G}:
            raise ValueError("variable-weight products require a mass price basis")
        if self.purchase_terms and self.purchase_terms.minimum_quantity:
            _, sale_unit = canonical_quantity(
                self.purchase_terms.minimum_quantity.amount,
                self.purchase_terms.minimum_quantity.unit,
            )
            _, basis_unit = canonical_quantity(self.price_basis.amount, self.price_basis.unit)
            if sale_unit != basis_unit:
                raise ValueError("purchase quantities must use the price-basis unit")
        if self.promotion:
            discount = self.promotion.advertised_discount_percent
            regular_reference = self.promotion.discount_reference == DiscountReference.REGULAR_PRICE
            if self.regular_price is not None and self.current_price > self.regular_price:
                raise ValueError("a promoted current price cannot exceed the regular price")
            if self.promotion.kind == PromotionType.PRICE_CUT or (
                discount is not None and regular_reference
            ):
                if self.regular_price is None or self.regular_price <= 0:
                    raise ValueError("discount promotions require a positive regular price")
                if self.current_price >= self.regular_price:
                    raise ValueError("discount promotions require a lower current price")
            if discount is not None and regular_reference and self.discount_percent is not None:
                if abs(discount - self.discount_percent) > Decimal(1):
                    raise ValueError("advertised discount disagrees with prices by over one point")
        # Derived prices must be representable too, not fail only when read downstream.
        _ = self.unit_price
        _ = self.minimum_purchase_cost
        return self

    def purchase_cost(self, required: Quantity | None = None) -> PurchaseCost | None:
        """Round demand up to sale constraints; never substitute an estimated weight.

        A returned total covers merchandise and stated item charges, not basket-level
        delivery/service fees. Unknown fees retain the merchandise subtotal only.
        """
        terms = self.purchase_terms
        if (
            terms is None
            or not terms.vat_included
            or terms.minimum_quantity is None
            or terms.quantity_increment is None
            or self.price_qualifier != PriceQualifier.EXACT
            or (
                self.promotion
                and self.promotion.kind in {PromotionType.COUPON, PromotionType.OTHER}
            )
        ):
            return None
        minimum, unit = canonical_quantity(
            terms.minimum_quantity.amount, terms.minimum_quantity.unit
        )
        increment, _ = canonical_quantity(
            terms.quantity_increment.amount, terms.quantity_increment.unit
        )
        basis, _ = canonical_quantity(self.price_basis.amount, self.price_basis.unit)
        demand = minimum
        if required:
            demand, demand_unit = canonical_quantity(required.amount, required.unit)
            if demand_unit != unit:
                contents = self.product.quantity
                if unit not in {Unit.PACKAGE, Unit.PIECE} or contents is None:
                    raise ValueError("required quantity cannot be converted to sale units")
                content_amount, content_unit = canonical_quantity(contents.amount, contents.unit)
                if content_unit != demand_unit:
                    raise ValueError("required quantity does not match product contents")
                demand /= content_amount
        if self.promotion:
            demand = max(demand, basis * self.promotion.minimum_purchase)
        quantity = (
            minimum
            + (max(Decimal(0), demand - minimum) / increment).to_integral_value(
                rounding=ROUND_CEILING
            )
            * increment
        )
        multiplier = quantity / basis

        def total(value: Decimal | None) -> Decimal | None:
            return (
                None
                if value is None
                else (value * multiplier).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            )

        merchandise = total(self.current_price)
        assert merchandise is not None
        deposit = total(terms.deposit_per_basis)
        fee = total(terms.mandatory_fee_per_basis)
        return PurchaseCost(
            quantity=Quantity(amount=quantity, unit=Unit(unit)),
            merchandise_total=merchandise,
            deposit_total=deposit,
            mandatory_fee_total=fee,
            total=merchandise + deposit + fee if deposit is not None and fee is not None else None,
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def minimum_purchase_cost(self) -> PurchaseCost | None:
        return self.purchase_cost()

    @computed_field  # type: ignore[prop-decorator]
    @property
    def discount_percent(self) -> Decimal | None:
        if (
            self.price_qualifier != PriceQualifier.EXACT
            or not self.regular_price
            or self.current_price >= self.regular_price
        ):
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
            if contents.unit != Unit.PACKAGE:
                content_amount, unit = canonical_quantity(contents.amount, contents.unit)
                amount *= content_amount
        return UnitPrice(
            amount=(self.current_price / amount).quantize(
                Decimal("0.0001"), rounding=ROUND_HALF_UP
            ),
            unit=Unit(unit),
            price_qualifier=self.price_qualifier,
        )
