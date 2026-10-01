import json
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import ValidationError

from grocery_agent.application.commands import (
    COMMAND_SCHEMA,
    MealCommand,
    ScrapeCommand,
    WorkflowCommand,
    parse_command,
)
from grocery_agent.application.parameters import (
    MealOverrides,
    MealParameters,
    PantryOverrides,
    PolicyOverrides,
    parse_owned_stock,
    resolve_parameters,
)
from grocery_agent.application.reports import FileMealReportStore
from grocery_agent.application.services import AcquisitionService, MealService, WorkflowService
from grocery_agent.catalogue.repository import SQLAlchemyCatalogueRepository
from grocery_agent.cli.main import main
from grocery_agent.matching.base import ExactGTINResolver
from grocery_agent.meals.catalog import MealCatalog
from grocery_agent.models.offer import Offer
from grocery_agent.persistence.reader import ObservedOffer, OfferBatch, SQLAlchemyCurrentOfferReader
from grocery_agent.persistence.repository import SQLAlchemyOfferRepository
from grocery_agent.persistence.snapshots import FileSnapshotStore
from grocery_agent.pipeline.results import ScrapeResult
from grocery_agent.pipeline.service import ScrapePipeline
from grocery_agent.stores.mock.adapter import MockStore
from tests.application_support import NOW, complete_batch, failed_batch

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def catalog() -> MealCatalog:
    return MealCatalog.load(ROOT / "config/meals.toml")


class EmptyBatchReader:
    def __init__(self) -> None:
        self.selected: list[str] = []

    def latest_batch(self, source_id: str) -> OfferBatch:
        return OfferBatch("latest", source_id, NOW)

    def get_batch(self, run_id: str, source_id: str) -> OfferBatch:
        self.selected.append(run_id)
        return OfferBatch(run_id, source_id, NOW)

    def iter_offers(self, batch: OfferBatch) -> Iterator[ObservedOffer]:
        return iter(())


def owned_all(catalog: MealCatalog) -> MealOverrides:
    return MealOverrides(
        pantry=PantryOverrides(
            items=parse_owned_stock([f"{key}=available" for key in catalog.ingredients]),
            seasonings_available=True,
        )
    )


def test_profile_then_request_parameters_are_isolated(catalog: MealCatalog) -> None:
    defaults = catalog.model_dump(mode="json")
    profile = resolve_parameters(
        catalog,
        MealOverrides(
            policy=PolicyOverrides(servings=2),
            pantry=PantryOverrides(
                items=parse_owned_stock(["chicken=2kg"]), use_first=("chicken",)
            ),
        ),
    )
    request = resolve_parameters(
        catalog,
        MealOverrides(
            policy=PolicyOverrides(max_cost_per_serving_czk="80"),
            pantry=PantryOverrides(items=parse_owned_stock(["chicken=500g", "rice=5kg"])),
        ),
        profile,
    )
    assert request.policy.servings == 2 and request.policy.max_cost_per_serving_czk == 80
    assert request.pantry.items["chicken"].grams == 500
    assert request.pantry.items["chicken"].use_first
    assert profile.pantry.items["chicken"].grams == 2000 and "rice" not in profile.pantry.items
    request.pantry.items.clear()
    assert profile.pantry.items and catalog.model_dump(mode="json") == defaults


@pytest.mark.parametrize(
    "entry", ["rice=0g", "rice=-1kg", "rice=NaNkg", "rice=1lb", "rice=1.0000001g"]
)
def test_stock_validation(entry: str) -> None:
    with pytest.raises(ValueError):
        parse_owned_stock([entry])


def test_duplicate_stock_and_unknown_ingredients(catalog: MealCatalog) -> None:
    with pytest.raises(ValueError, match="duplicate"):
        parse_owned_stock(["rice=1kg", "rice=2kg"])
    with pytest.raises(ValueError, match="unknown"):
        resolve_parameters(
            catalog,
            MealOverrides(pantry=PantryOverrides(items=parse_owned_stock(["unknown=available"]))),
        )


@pytest.mark.parametrize(
    "policy",
    [
        {"servings": True},
        {"servings": 1.5},
        {"servings": 0},
        {"max_stores": 4},
        {"max_cost_per_serving_czk": 1.5},
        {"max_cost_per_serving_czk": "1.00001"},
        {"min_protein_g": "NaN"},
        {"max_kcal": "Infinity"},
        {"ranking": None},
        {"extra": "value"},
    ],
)
def test_typed_policy_rejects_invalid_inputs(policy: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        PolicyOverrides.model_validate(policy)


def test_structured_command_uses_same_parameters(catalog: MealCatalog) -> None:
    command = parse_command(
        "meal have=rice=5kg,chicken=2kg use_first=chicken have_seasonings=true "
        "servings=2 min_protein_g=70 max_cost_per_serving_czk=80 lactose_free=true"
    )
    assert isinstance(command, MealCommand)
    parameters = resolve_parameters(catalog, command.overrides)
    assert parameters.pantry.items["rice"].grams == Decimal(5000)
    assert parameters.pantry.items["chicken"].use_first and parameters.policy.servings == 2
    assert COMMAND_SCHEMA.validate_json(command.model_dump_json()) == command
    assert isinstance(parse_command("scrape source=mock"), ScrapeCommand)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "meal budget=80",
        "meal servings=2 servings=3",
        "meal servings=1.5",
        "meal lactose_free=yes",
        "meal have=rice=5kg,rice=6kg",
        "scrape source=mock,mock",
        "workflow run_id=00000000-0000-0000-0000-000000000000",
        "meal " + "x" * 4096,
        "meal have=rice=$(touch /tmp/command-injection)",
    ],
)
def test_commands_are_bounded_and_allowlisted(text: str) -> None:
    with pytest.raises(ValueError):
        parse_command(text)


def test_pinned_planning_ignores_a_later_failed_run(
    catalog: MealCatalog,
    repository: SQLAlchemyOfferRepository,
    snapshots: FileSnapshotStore,
    candidate: dict[str, Any],
) -> None:
    from datetime import timedelta

    run = complete_batch(repository, snapshots, [Offer.model_validate(candidate)])
    failed_batch(repository, NOW + timedelta(minutes=1))
    payload = catalog.model_dump(mode="json")
    payload["policy"]["source_id"] = "mock"
    catalog = MealCatalog.model_validate(payload)
    service = MealService(
        SQLAlchemyCurrentOfferReader(repository.sessions.kw["bind"]), catalog, lambda: NOW
    )
    execution = service.plan(MealCommand(run_id=UUID(run.run_id), overrides=owned_all(catalog)))
    assert execution.report.run_id == run.run_id
    with pytest.raises(ValueError, match="unsuccessful"):
        service.plan(MealCommand(overrides=owned_all(catalog)))


@pytest.mark.asyncio
async def test_acquisition_stream_has_no_stdout_and_honors_run_id(
    repository: SQLAlchemyOfferRepository,
    snapshots: FileSnapshotStore,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pipeline = ScrapePipeline(repository, snapshots, ExactGTINResolver())
    cache = SQLAlchemyCatalogueRepository(repository.sessions.kw["bind"])
    selected = uuid4()
    async with httpx.AsyncClient() as http:
        result = await AcquisitionService(pipeline, http, cache).acquire(MockStore(), selected)
    assert result.run_id == str(selected) and result.accepted == 5
    assert cache.state("mock", result.finished_at).run_id == str(selected)
    assert capsys.readouterr().out == ""


@pytest.mark.asyncio
async def test_workflow_pins_its_own_run(catalog: MealCatalog) -> None:
    payload = catalog.model_dump(mode="json")
    payload["policy"]["source_id"] = "mock"
    catalog = MealCatalog.model_validate(payload)
    result = ScrapeResult(source_id="mock", status="success", finished_at=NOW)

    class StubAcquisition:
        async def acquire(self, adapter: MockStore) -> ScrapeResult:
            return result

    reader = EmptyBatchReader()
    # The service boundary is structural; this injected test acquisition has no network resources.
    service = WorkflowService(StubAcquisition(), MealService(reader, catalog, lambda: NOW))
    command = WorkflowCommand(overrides=owned_all(catalog))
    execution = await service.run(command, MockStore())
    assert reader.selected == [result.run_id] and execution.meal.request_id == command.request_id


def test_immutable_request_artifacts_never_overwrite_another_request(
    tmp_path: Path,
    catalog: MealCatalog,
) -> None:
    service = MealService(EmptyBatchReader(), catalog, lambda: NOW)
    first = service.plan(MealCommand(overrides=owned_all(catalog)))
    second = service.plan(MealCommand(overrides=owned_all(catalog)))
    store = FileMealReportStore(tmp_path)
    a, b = store.save(first), store.save(second)
    assert a.directory != b.directory and a.html_path.is_file() and b.json_path.is_file()
    assert not (tmp_path / "latest.html").exists()
    manifest = json.loads(a.request_path.read_text())
    assert manifest["version"] == 1 and manifest["run_id"] == "latest"
    assert manifest["parameters"]["pantry"]["items"] and manifest["catalog_sha256"]
    before = a.json_path.read_bytes()
    with pytest.raises(FileExistsError):
        store.save(first)
    assert a.json_path.read_bytes() == before and not list(
        (tmp_path / "requests").glob(".report-*")
    )


def test_cli_stays_account_independent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GROCERY_CONTROL_DATABASE_URL", "invalid user DB URL deliberately unused")
    monkeypatch.setattr("grocery_agent.cli.main.configure_logging", lambda level: None)
    assert main(["command", "scrape source=mock"]) == 0
    assert main(["catalogue", "mock"]) == 0
    assert not (tmp_path / "data/control.db").exists()


def test_parameter_schema_is_serializable(catalog: MealCatalog) -> None:
    parameters = MealParameters.from_catalog(catalog)
    assert MealParameters.model_validate_json(parameters.model_dump_json()) == parameters
    assert "user_id" not in json.dumps(MealParameters.model_json_schema())
