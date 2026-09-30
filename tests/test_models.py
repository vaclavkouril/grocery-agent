from datetime import datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from grocery_agent.models.observation import PriceObservation
from grocery_agent.models.offer import Offer
from grocery_agent.models.product import StoreProduct, Unit


@pytest.mark.parametrize("price", ["-1", "NaN", "Infinity", "1.12345", 19.9, True])
def test_invalid_money_is_rejected(candidate: dict[str, Any], price: Any) -> None:
    candidate["current_price"] = price
    with pytest.raises(ValidationError):
        Offer.model_validate(candidate)


@pytest.mark.parametrize(
    "field,value",
    [
        ("name", ""),
        ("name", "   "),
        ("name", "!!!"),
        ("sku", ""),
        ("store_id", "Albert"),
        ("gtin", "8594000000007"),
        ("gtin", "123"),
    ],
)
def test_invalid_identity(candidate: dict[str, Any], field: str, value: Any) -> None:
    candidate["product"][field] = value
    with pytest.raises(ValidationError):
        Offer.model_validate(candidate)


@pytest.mark.parametrize(
    "basis",
    [
        {"amount": "1", "unit": "bag"},
        {"amount": "0", "unit": "kg"},
        {"amount": "-1", "unit": "piece"},
    ],
)
def test_invalid_basis(candidate: dict[str, Any], basis: dict[str, str]) -> None:
    candidate["price_basis"] = basis
    with pytest.raises(ValidationError):
        Offer.model_validate(candidate)


@pytest.mark.parametrize(
    "patch",
    [
        {"current_price": "30"},
        {"regular_price": None},
        {"regular_price": "0"},
        {"current_price": "24.90"},
        {"promotion": {"kind": "price_cut", "advertised_discount_percent": "101"}},
        {"promotion": {"kind": "price_cut", "advertised_discount_percent": "-1"}},
        {"promotion": {"kind": "price_cut", "advertised_discount_percent": "80"}},
        {"promotion": {"kind": "loyalty", "requires_loyalty": False}},
        {"promotion": {"kind": "multibuy", "minimum_purchase": 1}},
        {"promotion": {"kind": "coupon"}},
    ],
)
def test_broken_promotions(candidate: dict[str, Any], patch: dict[str, Any]) -> None:
    candidate.update(patch)
    with pytest.raises(ValidationError):
        Offer.model_validate(candidate)


def test_invalid_validity_interval(candidate: dict[str, Any]) -> None:
    candidate.update(valid_from="2026-10-01", valid_until="2026-09-30")
    with pytest.raises(ValidationError):
        Offer.model_validate(candidate)


def test_single_day_and_unknown_validity(candidate: dict[str, Any]) -> None:
    candidate.update(valid_from="2026-10-01", valid_until="2026-10-01")
    assert Offer.model_validate(candidate).valid_from == Offer.model_validate(candidate).valid_until
    candidate.update(valid_from=None, valid_until=None)
    assert Offer.model_validate(candidate).valid_until is None


def test_no_promotion_can_represent_price_increase(candidate: dict[str, Any]) -> None:
    candidate.update(promotion=None, current_price="30")
    assert Offer.model_validate(candidate).discount_percent is None


def test_exact_money_and_derived_discount(candidate: dict[str, Any]) -> None:
    offer = Offer.model_validate(candidate)
    assert offer.current_price == Decimal("19.90")
    assert offer.discount_percent == Decimal("20.08")
    assert offer.product.gtin == "08594000000006"


@pytest.mark.parametrize(
    "quantity,expected,unit",
    [
        ({"amount": "500", "unit": "g"}, "39.8000", Unit.KG),
        ({"amount": "0.5", "unit": "kg"}, "39.8000", Unit.KG),
        ({"amount": "1000", "unit": "ml"}, "19.9000", Unit.L),
        ({"amount": "1", "unit": "l"}, "19.9000", Unit.L),
        (None, "19.9000", Unit.PIECE),
    ],
)
def test_unit_prices(
    candidate: dict[str, Any], quantity: dict[str, str] | None, expected: str, unit: Unit
) -> None:
    candidate["product"]["quantity"] = quantity
    price = Offer.model_validate(candidate).unit_price
    assert price.amount == Decimal(expected)
    assert price.unit == unit


def test_variable_weight(candidate: dict[str, Any]) -> None:
    candidate["product"].update(variable_weight=True, quantity=None)
    with pytest.raises(ValidationError, match="mass price basis"):
        Offer.model_validate(candidate)
    candidate["price_basis"] = {"amount": "100", "unit": "g"}
    assert Offer.model_validate(candidate).unit_price.amount == Decimal("199")


def test_multi_item_price_basis(candidate: dict[str, Any]) -> None:
    candidate["price_basis"] = {"amount": "2", "unit": "package"}
    assert Offer.model_validate(candidate).unit_price.amount == Decimal("9.95")


def test_package_with_multiple_pieces(candidate: dict[str, Any]) -> None:
    candidate["product"]["quantity"] = {"amount": "6", "unit": "piece"}
    candidate["price_basis"] = {"amount": "1", "unit": "package"}
    price = Offer.model_validate(candidate).unit_price
    assert price.unit == Unit.PIECE and price.amount == Decimal("3.3167")


def test_extra_source_fields_rejected(candidate: dict[str, Any]) -> None:
    candidate["retailer_internal_flag"] = "x"
    with pytest.raises(ValidationError, match="Extra inputs"):
        Offer.model_validate(candidate)


def test_naive_observation_timestamp_rejected(candidate: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        PriceObservation(
            offer=Offer.model_validate(candidate),
            observed_at=datetime(2026, 9, 30),
            snapshot_id="snapshot",
        )


def test_constructed_nested_models_revalidated(candidate: dict[str, Any]) -> None:
    candidate["product"] = StoreProduct.model_construct(store_id="mock", sku="", name="")
    with pytest.raises(ValidationError):
        Offer.model_validate(candidate)


def test_model_is_frozen(candidate: dict[str, Any]) -> None:
    offer = Offer.model_validate(candidate)
    with pytest.raises(ValidationError):
        offer.current_price = Decimal("0")
