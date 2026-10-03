"""Guard deterministic ordering and lazy evaluation without timing-sensitive assertions."""

from datetime import timedelta
from decimal import Decimal
from itertools import product

import pytest

from grocery_agent.meals.planner import IngredientPrice, _baskets, _cheaper, price_order
from grocery_agent.models.offer import Offer
from tests.test_combined_meals import NOW, quote


def price(offer: Offer | None = None, **updates: object) -> IngredientPrice:
    return IngredientPrice.model_validate(
        {
            "ingredient_id": "chicken",
            "label": "Chicken",
            "price_per_kg_czk": Decimal("100"),
            "offer": offer,
            "observed_at": NOW,
            "source_id": "kupi",
            "run_id": "run",
            **updates,
        }
    )


def legacy_order(value: IngredientPrice) -> tuple[object, ...]:
    return (
        value.price_per_kg_czk,
        value.offer.product.store_id if value.offer else "",
        value.offer.product.sku if value.offer else "",
        value.shopping_context,
        value.run_id or "",
        value.observed_at.isoformat() if value.observed_at else "",
        value.offer.model_dump_json(exclude_computed_fields=True) if value.offer else "",
    )


def test_lazy_comparison_preserves_every_legacy_tie_break() -> None:
    offer = quote("chicken", "100").offer
    values = [
        price(),
        price(offer),
        price(offer, price_per_kg_czk="99"),
        price(quote("chicken", "100", store="billa").offer),
        price(quote("chicken", "100", sku="other").offer),
        price(quote("chicken", "100", scope="kupi:brno").offer),
        price(offer, source_id="tesco"),
        price(offer, run_id="previous"),
        price(offer, observed_at=NOW - timedelta(seconds=1)),
        price(offer, observed_at=None, run_id=None),
        price(offer.model_copy(update={"source_url": "https://example.test/other"})),
    ]
    for candidate, previous in product(values, repeat=2):
        assert price_order(candidate) == legacy_order(candidate)
        assert _cheaper(candidate, previous) == (legacy_order(candidate) < legacy_order(previous))
    assert _cheaper(values[0], None)


def test_distinguishable_prices_do_not_serialize_offers(monkeypatch: pytest.MonkeyPatch) -> None:
    offer = quote("chicken", "100").offer
    candidate, previous = price(offer, price_per_kg_czk="99"), price(offer)

    def forbidden(*args: object, **kwargs: object) -> str:
        raise AssertionError("serialization is unnecessary before all other keys tie")

    monkeypatch.setattr(Offer, "model_dump_json", forbidden)
    assert _cheaper(candidate, None)
    assert _cheaper(candidate, previous)
    assert not _cheaper(previous, candidate)


def test_complete_baskets_are_streamed_in_stable_combination_order() -> None:
    first = price(quote("chicken", "100", store="a").offer)
    second = price(quote("chicken", "100", store="b").offer, price_per_kg_czk="99")
    baskets = _baskets(
        {},
        {"chicken": Decimal(100)},
        {("chicken", "a"): first, ("chicken", "b"): second},
        ["a", "b"],
        2,
    )
    assert iter(baskets) is baskets
    assert list(baskets) == [
        {"chicken": first},
        {"chicken": second},
        {"chicken": second},
    ]
    assert list(_baskets({}, {"chicken": Decimal(100)}, {}, [], 1)) == []
