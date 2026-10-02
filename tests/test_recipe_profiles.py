"""Cache-only recipe selection and immutable profile provenance."""

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine

from grocery_agent.apps import recipes
from grocery_agent.backend.config import BackendSettings
from grocery_agent.backend.recipes import RecipeExecutor, effective_catalog
from grocery_agent.backend.repository import ControlRepository
from grocery_agent.backend.worker import Worker
from grocery_agent.catalogue.profiles import AcquisitionProfile, Coverage
from grocery_agent.catalogue.repository import SQLAlchemyCatalogueRepository
from grocery_agent.channel_commands import parse_channel_command
from grocery_agent.config import Settings
from grocery_agent.contracts import Capabilities
from grocery_agent.http_client import GroceryClient
from grocery_agent.meals.catalog import MealCatalog
from grocery_agent.models.offer import Offer
from grocery_agent.persistence.control.schema import JobRow
from grocery_agent.persistence.database import create_database_engine
from grocery_agent.persistence.migrations import upgrade_database
from grocery_agent.persistence.repository import SQLAlchemyOfferRepository
from grocery_agent.persistence.snapshots import FileSnapshotStore
from grocery_agent.recipes import RecipeRequest
from tests.application_support import NOW, complete_batch
from tests.test_backend_recipes import DraftProvider
from tests.test_meals import groceries
from tests.test_profile_catalogue import ProfiledFixtureRepository

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def profiled_recipes(
    engine: Engine,
    repository: SQLAlchemyOfferRepository,
    snapshots: FileSnapshotStore,
    candidate: dict[str, Any],
) -> Any:
    cache = SQLAlchemyCatalogueRepository(engine)

    def publish(p: AcquisitionProfile | None, price: str, minutes: int = 0) -> str:
        offers = [
            Offer.model_validate(
                {
                    **item.offer.model_dump(mode="json", exclude_computed_fields=True),
                    "current_price": price
                    if item.offer.product.sku == "chicken"
                    else item.offer.current_price,
                }
            )
            for item in groceries(candidate)
        ]
        run = complete_batch(
            ProfiledFixtureRepository(engine, p) if p else repository,
            snapshots,
            offers,
            NOW + timedelta(minutes=minutes),
            source="kupi",
        )
        if p:
            cache.record_profile(
                run.run_id,
                p,
                Coverage(profile_fingerprint=p.fingerprint, observed=run.accepted, complete=True),
            )
        cache.publish(run.run_id)
        return run.run_id

    first = AcquisitionProfile(source_id="kupi", name="full")
    second = AcquisitionProfile(source_id="kupi", name="produce", categories=("produce",))
    runs = {first.fingerprint: publish(first, "100"), second.fingerprint: publish(second, "200")}
    runs["legacy"] = publish(None, "150")
    settings = Settings(
        database_url=str(engine.url), meal_config=ROOT / "config/meals.toml", _env_file=None
    )
    executor = RecipeExecutor(settings, BackendSettings(), lambda: NOW, provider=DraftProvider())
    return executor, first, second, runs, publish


@pytest.mark.parametrize("selected", ["first", "second", "legacy", None])
def test_cache_only_selects_exact_profile_and_legacy_default(
    profiled_recipes: Any, selected: str | None
) -> None:
    executor, first, second, runs, _ = profiled_recipes
    fingerprint = {"first": first.fingerprint, "second": second.fingerprint}.get(selected, selected)
    request = RecipeRequest(profile_fingerprint=fingerprint, cache_policy="cache-only")
    inputs = executor.prepare(request)
    assert inputs["run_id"] == runs[fingerprint or "legacy"]
    assert inputs["profile_fingerprint"] == (fingerprint or "legacy")
    result, _ = executor.execute(request, inputs)
    assert result["run_id"] == result["report"]["run_id"] == inputs["run_id"]
    assert result["profile_fingerprint"] == inputs["profile_fingerprint"]
    assert result["coverage_complete"] is (fingerprint not in {None, "legacy"})
    if fingerprint in {None, "legacy"}:
        assert any("coverage" in warning.lower() for warning in result["warnings"])
    else:
        assert result["actual_scope"] == "kupi:locality:praha"
        assert result["actual_scopes"] == ["kupi:locality:praha"]
    line = result["report"]["meals"][0]["lines"][0]
    assert line["purchased_grams"] == "800"
    assert (
        result["report"]["meals"][0]["usage_cost_per_serving_czk"]
        == {"first": "43.00", "second": "83.00", "legacy": "63.00", None: "63.00"}[selected]
    )


def test_pinned_quotes_and_metadata_survive_later_publication_without_reads(
    profiled_recipes: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor, first, second, runs, publish = profiled_recipes
    request = RecipeRequest(profile_fingerprint=first.fingerprint, cache_policy="cache-only")
    inputs = json.loads(json.dumps(executor.prepare(request)))
    before, _ = executor.execute(request, inputs)
    publish(second, "250", 1)
    publish(first, "300", 2)
    executor.clock = lambda: NOW + timedelta(minutes=3)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("execution must use persisted inputs without database reads")

    monkeypatch.setattr("grocery_agent.recipe_cache.create_database_engine", forbidden)
    monkeypatch.setattr(SQLAlchemyCatalogueRepository, "state", forbidden)
    after, _ = executor.execute(request, inputs)
    assert after["run_id"] == runs[first.fingerprint]
    for key in (
        "profile_fingerprint",
        "actual_scope",
        "actual_scopes",
        "coverage_complete",
        "warnings",
    ):
        assert after[key] == before[key] == inputs[key]
    assert after["report"]["meals"] == before["report"]["meals"]


def test_unknown_profile_and_mismatched_snapshot_fail(profiled_recipes: Any) -> None:
    executor, first, second, _, _ = profiled_recipes
    with pytest.raises(ValueError, match="missing or stale"):
        executor.prepare(RecipeRequest(profile_fingerprint="0" * 24))
    inputs = executor.prepare(RecipeRequest(profile_fingerprint=first.fingerprint))
    with pytest.raises(ValueError, match="profile does not match"):
        executor.execute(RecipeRequest(profile_fingerprint=second.fingerprint), inputs)
    with pytest.raises(ValueError, match="compatible scopes"):
        executor.prepare(RecipeRequest(source_ids=("mock",), profile_fingerprint=first.fingerprint))


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown", [False, True])
async def test_worker_persists_selected_metadata_or_fails_unknown_profile_asynchronously(
    profiled_recipes: Any, tmp_path: Path, unknown: bool
) -> None:
    executor, first, _, runs, _ = profiled_recipes
    control = create_database_engine(f"sqlite:///{tmp_path / 'recipe-control.db'}")
    upgrade_database(control, "control")
    repo = ControlRepository(control)
    try:
        user = repo.authenticate(repo.bootstrap("admin", NOW), NOW).user_id
        request = RecipeRequest(
            profile_fingerprint="0" * 24 if unknown else first.fingerprint,
            cache_policy="cache-only",
        )
        receipt = repo.submit(user, request.model_dump(mode="json"), "recipe", NOW)
        assert receipt.status == "queued"
        worker = Worker(repo, executor, executor.settings, BackendSettings(), lambda: NOW)
        assert await worker.once()
        with repo.sessions() as session:
            job = session.get(JobRow, receipt.job_id)
            assert job is not None
            if unknown:
                assert job.state == "failed" and job.inputs is None
            else:
                assert job.state == "succeeded"
                assert job.inputs["run_id"] == runs[first.fingerprint]
                result = repo.result(user, receipt.job_id)[0]
                for key in (
                    "profile_fingerprint",
                    "actual_scope",
                    "actual_scopes",
                    "coverage_complete",
                    "warnings",
                ):
                    assert result[key] == job.inputs[key]
    finally:
        control.dispose()


def test_no_feasible_result_keeps_profile_metadata(profiled_recipes: Any) -> None:
    executor, first, _, _, _ = profiled_recipes
    request = RecipeRequest(profile_fingerprint=first.fingerprint)
    inputs = executor.prepare(request)
    inputs["catalog"]["policy"]["max_cost_per_serving_czk"] = "1"
    result, _ = executor.execute(request, inputs)
    assert result["status"] == "no-feasible-recipe"
    assert result["profile_fingerprint"] == first.fingerprint
    assert result["coverage_complete"] is True


@pytest.mark.parametrize("value", ["", "LEGACY", "A" * 24, "0" * 23, "0" * 25, "legacy-other"])
def test_profile_fingerprint_shape_is_strict(value: str) -> None:
    with pytest.raises(ValueError):
        RecipeRequest(profile_fingerprint=value)


@pytest.fixture
def capabilities() -> Capabilities:
    return Capabilities(
        providers=("template",),
        default_provider="template",
        models={},
        cache_policies=("cache-only",),
        default_cache_policy="cache-only",
        sources=("kupi",),
        scope="praha",
        ingredients=("rice",),
        meal_styles=("main",),
        refresh_allowed=False,
        email_enabled=False,
        simplex_enabled=False,
    )


def test_remote_cli_text_and_http_serialization_parity(
    capabilities: Capabilities, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    fingerprint = "abcdef0123456789abcdef01"
    bodies: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/capabilities":
            return httpx.Response(200, json=capabilities.model_dump(mode="json"))
        bodies.append(json.loads(request.content))
        return httpx.Response(
            202,
            json={
                "job_id": "job",
                "kind": "recipe",
                "status": "queued",
                "phase": "queued",
                "attempts": 0,
                "created_at": NOW.isoformat(),
                "updated_at": NOW.isoformat(),
                "error_code": None,
            },
        )

    def client(url: str, token: str) -> GroceryClient:
        return GroceryClient(url, token, transport=httpx.MockTransport(handle))

    monkeypatch.setattr(recipes, "GroceryClient", client)
    monkeypatch.setenv("GROCERY_API_TOKEN", "test-session")
    assert (
        recipes.main(
            [
                "--api-url",
                "http://backend.test",
                "--profile-fingerprint",
                fingerprint,
                "--have",
                "rice=500g",
                "--budget",
                "80.0000",
                "--no-wait",
            ]
        )
        == 0
    )
    text = parse_channel_command(
        f'recipe profile="{fingerprint}" have="rice=500g" budget=80.0000', capabilities
    )
    assert isinstance(text, RecipeRequest)
    body = RecipeRequest.model_validate(bodies[0]).model_dump(mode="json", exclude={"request_id"})
    assert body == text.model_dump(mode="json", exclude={"request_id"})
    assert json.loads(capsys.readouterr().out)["status"] == "queued"


@pytest.mark.parametrize(
    "parameters",
    [
        "profile=legacy profile_fingerprint=legacy",
        "profile=legacy profile=legacy",
        "profile=UNKNOWN",
        "profile=",
        "profile=legacy provider=ollama",
        "profile=legacy source=mock",
        "profile=legacy cache_policy=refresh",
    ],
)
def test_text_profile_preserves_strict_validation(
    parameters: str, capabilities: Capabilities
) -> None:
    with pytest.raises(ValueError):
        parse_channel_command("recipe " + parameters, capabilities)


def test_local_cli_accepts_profile_for_shared_service(
    monkeypatch: pytest.MonkeyPatch, capsys: Any, tmp_path: Path
) -> None:
    requests: list[RecipeRequest] = []

    class Service:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def prepare(self, request: RecipeRequest) -> dict[str, Any]:
            requests.append(request)
            return {"profile_fingerprint": request.profile_fingerprint}

        def execute(
            self, request: RecipeRequest, inputs: dict[str, Any]
        ) -> tuple[dict[str, Any], str]:
            return {"profile_fingerprint": request.profile_fingerprint}, "<html></html>"

    monkeypatch.setattr(recipes, "RecipeService", Service)
    monkeypatch.setenv("GROCERY_REPORT_DIR", str(tmp_path / "reports"))
    assert recipes.main(["--provider", "template", "--profile-fingerprint", "legacy"]) == 0
    assert requests[0].profile_fingerprint == "legacy"
    assert json.loads(capsys.readouterr().out)["profile_fingerprint"] == "legacy"


def test_effective_catalog_retains_single_source_boundary() -> None:
    catalog = MealCatalog.load(ROOT / "config/meals.toml")
    with pytest.raises(ValueError, match="explicitly permitted"):
        effective_catalog(catalog, RecipeRequest(source_ids=("kupi", "mock")))
