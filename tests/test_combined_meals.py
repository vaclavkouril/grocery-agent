"""Offline checks for combined-source shopping constraints and quote provenance."""

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import permutations
from typing import Any

import pytest
from sqlalchemy import Engine

from grocery_agent.meals.catalog import MealCatalog
from grocery_agent.meals.planner import IngredientPrice, MealReport, plan_meals, rejection_reason
from grocery_agent.meals.report import render_html
from grocery_agent.models.offer import Offer
from grocery_agent.persistence.reader import ObservedOffer, OfferBatch, SQLAlchemyCurrentOfferReader
from grocery_agent.persistence.repository import SQLAlchemyOfferRepository
from grocery_agent.persistence.snapshots import FileSnapshotStore
from tests.test_persistence import persist

NOW = datetime(2026, 10, 1, 8, tzinfo=UTC)
SCOPES = {"kupi": ("kupi:praha",), "tesco": ("tesco:praha",)}


class MemoryReader:
    def __init__(self, offers: tuple[ObservedOffer, ...]) -> None:
        self.offers = offers

    def latest_batch(self, source_id: str) -> OfferBatch:
        return OfferBatch("snapshot", source_id, NOW)

    def iter_offers(self, batch: OfferBatch) -> Iterator[ObservedOffer]:
        yield from self.offers


@pytest.fixture
def catalog() -> MealCatalog:
    return MealCatalog.model_validate(
        {
            "policy": {
                "source_id": "kupi",
                "scope": "kupi:praha",
                "min_protein_g": "0",
                "max_kcal": "1000",
                "max_cost_per_serving_czk": "100",
                "seasoning_allowance_czk": "0",
                "max_stores": 2,
            },
            "ingredients": {
                key: {
                    "label": key,
                    "name_pattern": key,
                    "lactose_free": True,
                    "nutrients_per_100g_edible": {
                        "kcal": "100",
                        "protein_g": "10",
                        "carbs_g": "5",
                        "fat_g": "1",
                    },
                    "nutrition_source": "https://example.test/nutrition",
                }
                for key in ("chicken", "rice")
            },
            "recipes": [
                {
                    "title": "Chicken and rice",
                    "minutes": 15,
                    "grams": {"chicken": "100", "rice": "100"},
                    "steps": ["Cook."],
                }
            ],
        }
    )


def configured(catalog: MealCatalog, **policy: Any) -> MealCatalog:
    payload = catalog.model_dump(mode="json")
    payload["policy"].update(policy)
    return MealCatalog.model_validate(payload)


def quote(
    ingredient: str,
    price: str,
    source: str = "kupi",
    *,
    scope: str | None = None,
    run: str | None = None,
    sku: str | None = None,
    store: str = "tesco",
) -> ObservedOffer:
    offer = Offer.model_validate(
        {
            "product": {"name": ingredient, "sku": sku or ingredient, "store_id": store},
            "scope": scope or f"{source}:praha",
            "current_price": price,
            "price_basis": {"amount": "1", "unit": "kg"},
            "source_url": f"https://example.test/{source}/{ingredient}",
        }
    )
    return ObservedOffer(offer, NOW, f"observation-{source}-{ingredient}", source, run or source)


def test_sql_reader_stamps_selected_batch_provenance(
    engine: Engine,
    repository: SQLAlchemyOfferRepository,
    snapshots: FileSnapshotStore,
    candidate: dict[str, Any],
) -> None:
    persist(repository, snapshots, candidate, at=NOW)
    reader = SQLAlchemyCurrentOfferReader(engine)
    batch = reader.latest_batch("mock")
    (observed,) = reader.iter_offers(batch)
    assert observed.source_id == "mock"
    assert observed.run_id == batch.run_id
    assert observed.observation_id is not None
    assert observed.observed_at == NOW


def test_legacy_defaults_and_retailer_count(catalog: MealCatalog) -> None:
    chicken = quote("chicken", "100")
    rice = quote("rice", "40", "tesco", scope="kupi:praha")
    legacy = ObservedOffer(chicken.offer, NOW)
    assert legacy.source_id is legacy.run_id is None
    price = IngredientPrice(ingredient_id="chicken", label="Chicken", price_per_kg_czk="100")
    assert price.source_id is price.run_id is None
    assert price.shopping_context == ""
    report = plan_meals(MemoryReader((chicken, rice)), configured(catalog, max_stores=1), NOW)
    assert report.meals[0].stores == ("tesco",)
    assert len(report.meals[0].shopping_contexts) == 2
    payload = report.model_dump(mode="json", exclude_computed_fields=True)
    del payload["meals"][0]["shopping_contexts"]
    assert MealReport.model_validate(payload).meals[0].shopping_contexts == ()


def test_cross_source_same_retailer_requires_two_contexts(catalog: MealCatalog) -> None:
    rows = (quote("chicken", "100"), quote("rice", "40", "tesco"))
    with pytest.raises(ValueError, match="no complete recipe"):
        plan_meals(MemoryReader(rows), configured(catalog, max_stores=1), NOW, source_scopes=SCOPES)
    report = plan_meals(MemoryReader(rows), catalog, NOW, source_scopes=SCOPES)
    meal = report.meals[0]
    assert meal.stores == ("tesco",)
    assert meal.shopping_contexts == ("kupi|tesco|kupi:praha", "tesco|tesco|tesco:praha")
    assert meal.usage_cost_per_serving_czk == Decimal("14.00")


def test_same_source_different_scopes_require_two_contexts(catalog: MealCatalog) -> None:
    rows = (quote("chicken", "100"), quote("rice", "40", scope="kupi:brno"))
    scopes = {"kupi": ("kupi:praha", "kupi:brno")}
    with pytest.raises(ValueError, match="no complete recipe"):
        plan_meals(MemoryReader(rows), configured(catalog, max_stores=1), NOW, source_scopes=scopes)
    meal = plan_meals(MemoryReader(rows), catalog, NOW, source_scopes=scopes).meals[0]
    assert meal.shopping_contexts == ("kupi|tesco|kupi:brno", "kupi|tesco|kupi:praha")


def test_context_limit_and_budget_choose_feasible_basket(catalog: MealCatalog) -> None:
    rows = (quote("chicken", "100"), quote("rice", "80"), quote("rice", "10", "tesco"))
    one = plan_meals(
        MemoryReader(rows), configured(catalog, max_stores=1), NOW, source_scopes=SCOPES
    ).meals[0]
    assert one.usage_cost_per_serving_czk == Decimal("18.00")
    assert one.shopping_contexts == ("kupi|tesco|kupi:praha",)
    budget = configured(catalog, max_cost_per_serving_czk="12")
    two = plan_meals(MemoryReader(rows), budget, NOW, source_scopes=SCOPES).meals[0]
    assert two.usage_cost_per_serving_czk == Decimal("11.00")
    with pytest.raises(ValueError, match="no complete recipe"):
        plan_meals(MemoryReader(rows), configured(budget, max_stores=1), NOW, source_scopes=SCOPES)


@pytest.mark.parametrize("source", [None, "unknown"])
def test_missing_or_unconfigured_source_is_rejected(
    catalog: MealCatalog, source: str | None
) -> None:
    invalid = replace(quote("chicken", "1"), source_id=source)
    rows = (invalid, quote("chicken", "100"), quote("rice", "40"))
    report = plan_meals(MemoryReader(rows), catalog, NOW, source_scopes=SCOPES)
    assert report.rejected_offers == {"source": 1}
    assert report.meals[0].usage_cost_per_serving_czk == Decimal("14.00")


def test_scope_allowlist_is_source_specific_and_never_rewrites_scope(catalog: MealCatalog) -> None:
    bad = quote("chicken", "1", "tesco", scope="kupi:praha")
    rows = (bad, quote("chicken", "100"), quote("rice", "40", "tesco"))
    report = plan_meals(MemoryReader(rows), catalog, NOW, source_scopes=SCOPES)
    assert report.rejected_offers == {"scope": 1}
    rice = report.meals[0].lines[1].price
    assert rice.offer is not None and rice.offer.scope == "tesco:praha"
    assert rejection_reason(rice.offer, catalog.policy) == "scope"
    assert rejection_reason(rice.offer, catalog.policy, allowed_scopes=("tesco:praha",)) is None
    assert rejection_reason(rice.offer, catalog.policy, allowed_scopes=()) == "scope"
    with pytest.raises(ValueError, match="no complete recipe"):
        plan_meals(MemoryReader(rows), catalog, NOW, source_scopes={})


def test_pantry_shortfall_budget_and_context_count(catalog: MealCatalog) -> None:
    payload = configured(catalog, servings=2, max_stores=1).model_dump(mode="json")
    payload["pantry"] = {
        "items": {"chicken": {"grams": "200", "use_first": True}, "rice": {"grams": "50"}},
        "seasonings_available": True,
    }
    stock = MealCatalog.model_validate(payload)
    rows = (quote("chicken", "100"), quote("rice", "40", "tesco"))
    meal = plan_meals(MemoryReader(rows), stock, NOW, source_scopes=SCOPES).meals[0]
    assert meal.shopping_contexts == ("tesco|tesco|tesco:praha",)
    assert meal.usage_cost_per_serving_czk == Decimal("3.00")
    assert meal.use_first_grams == Decimal(200)
    chicken, rice = meal.lines
    assert chicken.price.offer is None and chicken.price.shopping_context == ""
    assert chicken.purchased_grams == chicken.usage_cost_czk == 0
    assert rice.owned_grams == Decimal(50) and rice.purchased_grams == Decimal(150)
    assert rice.price.run_id == rice.price.source_id == "tesco"
    with pytest.raises(ValueError, match="no complete recipe"):
        plan_meals(
            MemoryReader(rows),
            configured(stock, max_cost_per_serving_czk="2.99"),
            NOW,
            source_scopes=SCOPES,
        )
    payload["pantry"]["items"]["rice"] = {}
    owned = plan_meals(
        MemoryReader(()), MealCatalog.model_validate(payload), NOW, source_scopes={}
    ).meals[0]
    assert owned.shopping_contexts == owned.stores == ()
    assert owned.usage_cost_per_serving_czk == 0 and owned.protein_g_per_czk is None


def test_ties_are_independent_of_offer_and_mapping_order(catalog: MealCatalog) -> None:
    rows = (
        quote("chicken", "100", run="run-b"),
        quote("chicken", "100", run="run-a"),
        quote("chicken", "100", "tesco", run="run-a"),
        quote("rice", "40"),
    )
    expected = plan_meals(MemoryReader(rows), catalog, NOW, source_scopes=SCOPES)
    for order in permutations(rows):
        result = plan_meals(
            MemoryReader(order), catalog, NOW, source_scopes=dict(reversed(tuple(SCOPES.items())))
        )
        assert result == expected
    chicken = expected.meals[0].lines[0].price
    assert chicken.source_id == "kupi" and chicken.run_id == "run-a"


def test_provenance_round_trip_and_html_escaping(catalog: MealCatalog) -> None:
    rows = (quote("chicken", "100", run="<run>"), quote("rice", "40", "tesco"))
    report = plan_meals(MemoryReader(rows), catalog, NOW, source_scopes=SCOPES)
    encoded = report.model_dump_json(exclude_computed_fields=True)
    assert '"shopping_context":"kupi|tesco|kupi:praha"' in encoded
    assert MealReport.model_validate_json(encoded) == report
    assert {(p.source_id, p.run_id) for p in report.cheap_ingredients} == {
        ("kupi", "<run>"),
        ("tesco", "tesco"),
    }
    rendered = render_html(report, catalog)
    assert "source: kupi; run: &lt;run&gt;;" in rendered
    assert "shopping context: tesco|tesco|tesco:praha" in rendered
    assert "<run>" not in rendered


def test_pinned_combined_batch_uses_oldest_freshness_and_primary_source(
    catalog: MealCatalog,
) -> None:
    reader = MemoryReader((quote("chicken", "100"), quote("rice", "40", "tesco")))
    # The shared service supplies the pinned combined batch and owns the run snapshot.
    batch = OfferBatch("combined-snapshot", "kupi", NOW - timedelta(hours=2), ("cached",))
    report = plan_meals(reader, catalog, NOW, batch=batch, source_scopes=SCOPES)
    assert report.run_id == "combined-snapshot"
    assert report.batch_finished_at == batch.finished_at and report.warnings == ("cached",)
    with pytest.raises(ValueError, match="stale"):
        plan_meals(
            reader,
            catalog,
            NOW,
            batch=replace(batch, finished_at=NOW - timedelta(hours=37)),
            source_scopes=SCOPES,
        )
    with pytest.raises(ValueError, match="source does not match"):
        plan_meals(
            reader, catalog, NOW, batch=replace(batch, source_id="tesco"), source_scopes=SCOPES
        )
