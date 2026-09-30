import copy
import json
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from grocery_agent.cli.main import main
from grocery_agent.meals.catalog import MealCatalog, Nutrients
from grocery_agent.meals.planner import MealReport, matches, plan_meals
from grocery_agent.meals.report import render_html, save_failure, save_report
from grocery_agent.models.offer import Offer
from grocery_agent.persistence.reader import ObservedOffer, OfferBatch, SQLAlchemyCurrentOfferReader
from grocery_agent.persistence.repository import SQLAlchemyOfferRepository
from grocery_agent.persistence.snapshots import FileSnapshotStore
from grocery_agent.pipeline.results import ScrapeResult
from grocery_agent.stores.base import (
    AcquisitionAdapter,
    AcquisitionItem,
    AdapterContext,
    SourceEvidence,
)
from grocery_agent.stores.registry import StoreRegistry
from grocery_agent.workflow import writer_lock
from tests.test_persistence import persist

NOW = datetime(2026, 10, 1, 8, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[1]


class MemoryReader:
    def __init__(self, offers: list[ObservedOffer], finished_at: datetime = NOW) -> None:
        self.offers = offers
        self.finished_at = finished_at

    def latest_batch(self, source_id: str) -> OfferBatch:
        return OfferBatch("run", source_id, self.finished_at)

    def iter_offers(self, batch: OfferBatch) -> Iterator[ObservedOffer]:
        yield from self.offers


@pytest.fixture
def catalog() -> MealCatalog:
    return MealCatalog.load(ROOT / "config/meals.toml")


def configured(catalog: MealCatalog, **policy: Any) -> MealCatalog:
    payload = catalog.model_dump(mode="json")
    payload["policy"].update(policy)
    return MealCatalog.model_validate(payload)


def groceries(candidate: dict[str, Any]) -> list[ObservedOffer]:
    rows = []
    for sku, name, price in (
        ("chicken", "Kuřecí prsní řízky", "100"),
        ("turkey", "Krůtí prsní řízky", "180"),
        ("lentils", "Čočka červená", "60"),
        ("rice", "Rýže dlouhozrnná", "40"),
        ("carrot", "Mrkev", "12"),
        ("onion", "Cibule kuchyňská žlutá", "20"),
    ):
        data = copy.deepcopy(candidate)
        data.update(
            current_price=price,
            regular_price=None,
            promotion=None,
            scope="kupi:locality:praha",
            valid_from="2026-10-01",
            valid_until="2026-10-04",
            price_basis={"amount": "1", "unit": "kg"},
        )
        data["product"].update(name=name, store_id="billa", sku=sku, quantity=None, gtin=None)
        rows.append(ObservedOffer(Offer.model_validate(data), NOW))
    return rows


def test_decimal_nutrient_arithmetic() -> None:
    n = Nutrients(kcal="102", protein_g="22.8", carbs_g="0", fat_g="1.2")
    assert n.scaled(Decimal(3)).protein_g == Decimal("68.4")
    assert n.plus(n).fat_g == Decimal("2.4")
    with pytest.raises(ValidationError):
        Nutrients(kcal=102.0, protein_g="22.8", carbs_g="0", fat_g="1.2")


@pytest.mark.parametrize(
    "field,value", [("timezone", "Unknown/Place"), ("ingredient_pattern", "[")]
)
def test_invalid_catalog_is_rejected(catalog: MealCatalog, field: str, value: str) -> None:
    payload = catalog.model_dump(mode="json")
    if field == "timezone":
        payload["policy"]["timezone"] = value
    else:
        payload["ingredients"]["chicken"]["name_pattern"] = value
    with pytest.raises(ValidationError):
        MealCatalog.model_validate(payload)


def test_processing_terms_are_checked(catalog: MealCatalog, candidate: dict[str, Any]) -> None:
    payload = groceries(candidate)[0].offer.model_dump(mode="json", exclude_computed_fields=True)
    payload["promotion"] = {"kind": "advertised", "conditions": "marinované"}
    assert not matches(catalog.ingredients["chicken"], Offer.model_validate(payload))


def test_loyalty_is_explicit_opt_in(catalog: MealCatalog, candidate: dict[str, Any]) -> None:
    rows = groceries(candidate)
    payload = rows[0].offer.model_dump(mode="json", exclude_computed_fields=True)
    payload["promotion"] = {"kind": "loyalty", "requires_loyalty": True}
    rows = [ObservedOffer(Offer.model_validate(payload), NOW), *rows[2:]]
    assert plan_meals(MemoryReader(rows), configured(catalog, allow_loyalty=True), NOW).meals


def test_plan_macros_prices_and_servings(catalog: MealCatalog, candidate: dict[str, Any]) -> None:
    report = plan_meals(MemoryReader(groceries(candidate)), catalog, NOW)
    meal = report.meals[0]
    assert len(report.meals) == 3 and meal.stores == ("billa",)
    assert meal.nutrients_per_serving.protein_g == Decimal("90.024")
    assert meal.nutrients_per_serving.kcal == Decimal("708.06")
    assert meal.nutrients_per_serving.fat_g == Decimal("15.002")
    assert meal.usage_cost_per_serving_czk == Decimal("43.20")
    doubled = plan_meals(MemoryReader(groceries(candidate)), configured(catalog, servings=2), NOW)
    assert doubled.meals[0].nutrients_per_serving == meal.nutrients_per_serving
    assert doubled.meals[0].usage_cost_per_serving_czk == meal.usage_cost_per_serving_czk
    assert doubled.meals[0].lines[0].purchased_grams == Decimal(600)


@pytest.mark.parametrize(
    "changes",
    [
        {"valid_from": "2026-09-01", "valid_until": "2026-09-30"},
        {"valid_from": "2026-10-02"},
        {"scope": "other-city"},
        {"currency": "EUR"},
        {"availability": "unavailable"},
        {"price_qualifier": "from"},
        {"current_price": "0"},
        {"promotion": {"kind": "loyalty", "requires_loyalty": True}},
        {"promotion": {"kind": "multibuy", "minimum_purchase": 2}},
        {"promotion": {"kind": "coupon", "conditions": "coupon required"}},
        {"promotion": {"kind": "advertised", "conditions": "akce 1+1 zdarma"}},
        {"price_basis": {"amount": "1", "unit": "package"}},
    ],
)
def test_unusable_offers_never_make_recipe(
    catalog: MealCatalog, candidate: dict[str, Any], changes: dict[str, Any]
) -> None:
    rows = groceries(candidate)
    payload = rows[0].offer.model_dump(mode="json", exclude_computed_fields=True)
    payload.update(changes)
    # Leave only the chicken recipe ingredients; turkey cannot hide the missing chicken.
    rows = [ObservedOffer(Offer.model_validate(payload), NOW), *rows[2:]]
    with pytest.raises(ValueError, match="no complete recipe"):
        plan_meals(MemoryReader(rows), catalog, NOW)


@pytest.mark.parametrize("age", [timedelta(hours=37), timedelta(seconds=-1)])
def test_stale_or_future_batch(
    catalog: MealCatalog, candidate: dict[str, Any], age: timedelta
) -> None:
    with pytest.raises(ValueError, match="stale or in the future"):
        plan_meals(MemoryReader(groceries(candidate), NOW - age), catalog, NOW)


def test_individual_stale_and_future_timestamps(
    catalog: MealCatalog, candidate: dict[str, Any]
) -> None:
    rows = groceries(candidate)
    for age in (timedelta(hours=37), timedelta(seconds=-1)):
        with pytest.raises(ValueError, match="no complete recipe"):
            plan_meals(
                MemoryReader([ObservedOffer(row.offer, NOW - age) for row in rows]), catalog, NOW
            )


def test_praha_date_and_inclusive_validity(catalog: MealCatalog, candidate: dict[str, Any]) -> None:
    # October 1 in Praha is still September 30 UTC.
    now = datetime(2026, 9, 30, 22, 1, tzinfo=UTC)
    rows = [ObservedOffer(row.offer, now) for row in groceries(candidate)]
    assert plan_meals(MemoryReader(rows, now), catalog, now).meals


@pytest.mark.parametrize(
    "name",
    [
        "Šunka kuřecí prsní",
        "Kuřecí prsní řízky obalované",
        "Kuřecí prsní řízky marinované",
        "Kuřecí prsa s kostí",
        "Rýže mléčná Riso",
        "Čočka červená vařená",
    ],
)
def test_processed_foods_do_not_inherit_raw_macros(
    catalog: MealCatalog, candidate: dict[str, Any], name: str
) -> None:
    payload = groceries(candidate)[0].offer.model_dump(mode="json", exclude_computed_fields=True)
    payload["product"]["name"] = name
    offer = Offer.model_validate(payload)
    assert not any(matches(ingredient, offer) for ingredient in catalog.ingredients.values())


def test_store_limit_changes_basket(catalog: MealCatalog, candidate: dict[str, Any]) -> None:
    rows = groceries(candidate)
    # A cheap carrot elsewhere must not add a second trip under a one-store constraint.
    cheap = rows[4].offer.model_dump(mode="json", exclude_computed_fields=True)
    cheap.update(current_price="1")
    cheap["product"]["store_id"] = "albert"
    rows.append(ObservedOffer(Offer.model_validate(cheap), NOW))
    one = plan_meals(MemoryReader(rows), configured(catalog, max_stores=1), NOW).meals[0]
    two = plan_meals(MemoryReader(rows), configured(catalog, max_stores=2), NOW).meals[0]
    assert one.stores == ("billa",) and two.stores == ("albert", "billa")
    assert two.usage_cost_per_serving_czk < one.usage_cost_per_serving_czk


def test_lactose_and_limits_are_enforced(catalog: MealCatalog, candidate: dict[str, Any]) -> None:
    data = catalog.model_dump(mode="json")
    data["ingredients"]["oil"]["lactose_free"] = False
    with pytest.raises(ValueError, match="no complete recipe"):
        plan_meals(MemoryReader(groceries(candidate)), MealCatalog.model_validate(data), NOW)
    for policy in (
        {"min_protein_g": "200"},
        {"max_kcal": "300"},
        {"max_cost_per_serving_czk": "1"},
        {"retailers": ["lidl"]},
    ):
        with pytest.raises(ValueError, match="no complete recipe"):
            plan_meals(MemoryReader(groceries(candidate)), configured(catalog, **policy), NOW)


def test_report_escape_and_failure(
    tmp_path: Path, catalog: MealCatalog, candidate: dict[str, Any]
) -> None:
    report = plan_meals(MemoryReader(groceries(candidate)), catalog, NOW)
    payload = report.model_dump(mode="json", exclude_computed_fields=True)
    payload["meals"][0]["title"] = '<img src=x onerror="alert(1)">'
    rendered = render_html(MealReport.model_validate(payload), catalog)
    assert "&lt;img" in rendered and "<img src=x" not in rendered
    assert "out of date" in rendered and "checkout total" in rendered
    save_report(tmp_path, report, catalog)
    assert json.loads((tmp_path / "latest.json").read_text())["run_id"] == "run"
    assert MealReport.model_validate_json((tmp_path / "latest.json").read_text()) == report
    assert not list(tmp_path.glob("tmp*"))
    save_failure(tmp_path, "broken <source>")
    assert json.loads((tmp_path / "latest.json").read_text())["status"] == "failed"
    assert "broken &lt;source&gt;" in (tmp_path / "latest.html").read_text()


def test_writer_lock_releases_after_failure(tmp_path: Path) -> None:
    path = tmp_path / "job.lock"
    with writer_lock(path):
        with pytest.raises(ValueError, match="writer lock"), writer_lock(path):
            pass
    with pytest.raises(RuntimeError), writer_lock(path):
        raise RuntimeError("crash")
    with writer_lock(path):
        pass


def test_reader_batch_membership_and_unchanged_data(
    repository: SQLAlchemyOfferRepository, snapshots: FileSnapshotStore, candidate: dict[str, Any]
) -> None:
    persist(repository, snapshots, candidate, at=NOW - timedelta(hours=1))
    reader = SQLAlchemyCurrentOfferReader(repository.sessions.kw["bind"])
    original = reader.latest_batch("mock")
    assert len(list(reader.iter_offers(original))) == 1
    assert not persist(repository, snapshots, candidate, at=NOW)
    current = reader.latest_batch("mock")
    rows = list(reader.iter_offers(current))
    assert rows[0].observed_at == NOW
    candidate["current_price"] = "19.80"
    persist(repository, snapshots, candidate, at=NOW + timedelta(minutes=1))
    assert list(reader.iter_offers(original))[0].offer.current_price == Decimal("19.90")
    # Empty completed batch represents disappearance, not an old offer carried forward.
    run = ScrapeResult(source_id="mock")
    repository.start_run(run)
    run.status = "success"
    run.finished_at = NOW
    repository.finish_run(run)
    assert not list(reader.iter_offers(reader.latest_batch("mock")))


@pytest.mark.parametrize("status", ["running", "failed", "partial", "empty", "cancelled"])
def test_reader_blocks_latest_unsuccessful_run(
    repository: SQLAlchemyOfferRepository,
    snapshots: FileSnapshotStore,
    candidate: dict[str, Any],
    status: str,
) -> None:
    persist(repository, snapshots, candidate, at=NOW)
    run = ScrapeResult(source_id="mock")
    repository.start_run(run)
    run.status = status  # runtime persistence may contain any recorded failure status
    repository.finish_run(run)
    reader = SQLAlchemyCurrentOfferReader(repository.sessions.kw["bind"])
    with pytest.raises(ValueError, match="unsuccessful"):
        reader.latest_batch("mock")


def test_cli_failed_workflow_is_observable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GROCERY_MEAL_CONFIG", str(ROOT / "config/meals.toml"))
    monkeypatch.setattr("grocery_agent.cli.main.configure_logging", lambda level: None)

    async def failed(*args: Any) -> int:
        return 1

    monkeypatch.setattr("grocery_agent.cli.main.scrape_stores", failed)
    assert main(["workflow"]) == 1
    assert json.loads((tmp_path / "data/reports/latest.json").read_text())["status"] == "failed"


def test_complete_one_off_workflow_offline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    candidate: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    rows = groceries(candidate)

    class FixtureSource(AcquisitionAdapter):
        source_id = "fixture"

        def validate_offer(self, offer: Offer) -> None:
            assert offer.product.store_id == "billa"

        async def fetch_offers(self, context: AdapterContext) -> AsyncIterator[AcquisitionItem]:
            for row in rows:
                payload = row.offer.model_dump(mode="json", exclude_computed_fields=True)
                evidence = SourceEvidence(
                    json.dumps(payload).encode(),
                    str(row.offer.source_url),
                    "application/json",
                    NOW,
                    row.offer.product.sku,
                )
                yield AcquisitionItem(evidence=evidence, candidate=payload)

    registry = StoreRegistry()
    registry.register("fixture", FixtureSource)
    config_path = tmp_path / "meals.toml"
    config_path.write_text(
        (ROOT / "config/meals.toml")
        .read_text()
        .replace(
            'source_id = "kupi"',
            'source_id = "fixture"',
        )
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GROCERY_MEAL_CONFIG", str(config_path))
    monkeypatch.setattr("grocery_agent.cli.main.default_registry", lambda: registry)
    monkeypatch.setattr("grocery_agent.cli.main.configure_logging", lambda level: None)
    monkeypatch.setattr("grocery_agent.workflow.utc_now", lambda: NOW)
    monkeypatch.setattr("grocery_agent.pipeline.results.utc_now", lambda: NOW)
    monkeypatch.setattr("grocery_agent.pipeline.service.utc_now", lambda: NOW)
    for expected_changed in (6, 0):
        assert main(["workflow"]) == 0
        lines = capsys.readouterr().out.splitlines()
        assert json.loads(lines[0])["changed"] == expected_changed
        report = MealReport.model_validate_json(lines[1])
        assert report.meals[0].nutrients_per_serving.protein_g == Decimal("90.024")
        assert (tmp_path / "data/reports/latest.html").is_file()
