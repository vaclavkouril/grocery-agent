from decimal import Decimal

import pytest

from grocery_agent.matching.base import ExactGTINResolver
from grocery_agent.models.normalization import canonical_quantity, normalize_gtin, normalize_name
from grocery_agent.models.product import StoreProduct


@pytest.mark.parametrize(
    "name,expected",
    [
        ("  Kuřecí  PRSNÍ řízky, 1 kg! ", "kuřecí prsní řízky 1 kg"),
        ("Ｍléko\u00a01 l", "mléko 1 l"),
        ("Čaj_černý", "čaj černý"),
        ("!!!", ""),
    ],
)
def test_normalized_names(name: str, expected: str) -> None:
    assert normalize_name(name) == expected
    assert normalize_name(normalize_name(name)) == expected


@pytest.mark.parametrize(
    "amount,unit,expected,target",
    [
        ("500", "g", "0.5", "kg"),
        ("250", "ml", "0.25", "l"),
        ("1", "piece", "1", "piece"),
        ("6", "package", "6", "package"),
    ],
)
def test_quantity_normalization(amount: str, unit: str, expected: str, target: str) -> None:
    assert canonical_quantity(Decimal(amount), unit) == (Decimal(expected), target)


@pytest.mark.parametrize("gtin", ["96385074", "036000291452", "8594000000006", "08594000000006"])
def test_gtin_forms(gtin: str) -> None:
    assert normalize_gtin(gtin) == gtin.zfill(14)


def test_gtin_exact_matching_across_stores() -> None:
    resolver = ExactGTINResolver()
    first = resolver.resolve(
        StoreProduct(store_id="mock", sku="a", name="Milk", gtin="8594000000006")
    )
    second = resolver.resolve(
        StoreProduct(store_id="another", sku="b", name="Mléko", gtin="08594000000006")
    )
    assert first is not None and second is not None and first.id == second.id
    assert resolver.resolve(StoreProduct(store_id="mock", sku="c", name="Milk")) is None
