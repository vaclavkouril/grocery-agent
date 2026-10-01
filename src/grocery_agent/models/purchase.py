"""Explicit sale constraints and checkout costs, separate from a price quotation."""

from typing import Self

from pydantic import model_validator

from grocery_agent.models.common import DomainModel, ExactDecimal, MoneyAmount, NonEmpty
from grocery_agent.models.normalization import canonical_quantity
from grocery_agent.models.product import Quantity


class PurchaseTerms(DomainModel):
    vat_included: bool
    vat_rate_percent: ExactDecimal | None = None
    minimum_quantity: Quantity | None = None
    quantity_increment: Quantity | None = None
    # Amounts apply to the same price_basis as current_price. None means unknown,
    # not free. Refundable deposits still count towards money due at checkout.
    deposit_per_basis: MoneyAmount | None = None
    mandatory_fee_per_basis: MoneyAmount | None = None
    membership_required: bool = False
    conditions: NonEmpty | None = None

    @model_validator(mode="after")
    def coherent_terms(self) -> Self:
        if self.vat_rate_percent is not None and not 0 <= self.vat_rate_percent <= 100:
            raise ValueError("VAT rate must be between zero and 100 percent")
        if (self.minimum_quantity is None) != (self.quantity_increment is None):
            raise ValueError("minimum quantity and increment must both be known or unknown")
        if self.minimum_quantity and self.quantity_increment:
            _, minimum_unit = canonical_quantity(
                self.minimum_quantity.amount, self.minimum_quantity.unit
            )
            _, increment_unit = canonical_quantity(
                self.quantity_increment.amount, self.quantity_increment.unit
            )
            if minimum_unit != increment_unit:
                raise ValueError("minimum quantity and increment must have compatible units")
        return self


class PurchaseCost(DomainModel):
    quantity: Quantity
    merchandise_total: MoneyAmount
    deposit_total: MoneyAmount | None
    mandatory_fee_total: MoneyAmount | None
    total: MoneyAmount | None
    vat_included: bool = True
