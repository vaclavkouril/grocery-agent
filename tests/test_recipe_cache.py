"""Fixture-backed local-first acquisition, combined pinning and shared execution."""

from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest
from sqlalchemy import Engine

from grocery_agent.catalogue.profiles import AcquisitionProfile, Coverage
from grocery_agent.catalogue.repository import SQLAlchemyCatalogueRepository
from grocery_agent.config import Settings
from grocery_agent.meals.catalog import MealCatalog
from grocery_agent.models.offer import Offer
from grocery_agent.persistence.snapshots import FileSnapshotStore
from grocery_agent.pipeline.results import ScrapeResult
from grocery_agent.recipe_cache import RecipeCache
from grocery_agent.recipe_service import RecipeService
from grocery_agent.recipes import RecipeRequest
from grocery_agent.stores.base import AcquisitionAdapter, AcquisitionItem, AdapterContext
from grocery_agent.stores.registry import StoreRegistry
from tests.application_support import NOW, complete_batch
from tests.test_meals import groceries
from tests.test_profile_catalogue import ProfiledFixtureRepository

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class Options:
    source_scopes: dict[str, tuple[str, ...]] = field(default_factory=dict)
    acquisition_profiles: tuple[AcquisitionProfile, ...] = ()
    context_ingredients: int = 60
    provider_timeout_seconds: int = 120


class NativeFixture(AcquisitionAdapter):
    def __init__(self, source: str) -> None:
        self.source = source

    @property
    def source_id(self) -> str:
        return self.source

    def validate_offer(self, offer: Offer) -> None:
        pass

    async def fetch_offers(self, context: AdapterContext) -> AsyncIterator[AcquisitionItem]:
        raise AssertionError("tests must use the injected acquisition boundary")
        yield


@pytest.fixture
def setup_cache(
    engine: Engine, snapshots: FileSnapshotStore, candidate: dict[str, Any], tmp_path: Path
) -> Any:
    catalog = MealCatalog.load(ROOT / "config/meals.toml")
    profiles = tuple(AcquisitionProfile(source_id=source) for source in ("kupi", "tesco"))
    options = Options({"kupi": (catalog.policy.scope,), "tesco": ("online:fixture",)}, profiles)
    registry = StoreRegistry()
    for source in ("kupi", "tesco"):
        registry.register(source, lambda source=source: NativeFixture(source))
    settings = Settings(
        database_url=str(engine.url),
        meal_config=ROOT / "config/meals.toml",
        lock_path=tmp_path / "writer.lock",
        report_dir=tmp_path / "reports",
        _env_file=None,
    )
    calls: list[str] = []
    time = [NOW]

    def publish(
        profile: AcquisitionProfile, *, at: Any = None, chicken_only: bool = False
    ) -> ScrapeResult:
        rows = groceries(candidate)
        if chicken_only:
            rows = rows[:1]
        scope = options.source_scopes[profile.source_id][0]
        offers = [
            Offer.model_validate(
                {**row.offer.model_dump(exclude_computed_fields=True), "scope": scope}
            )
            for row in rows
        ]
        run = complete_batch(
            ProfiledFixtureRepository(engine, profile),
            snapshots,
            offers,
            at or time[0],
            source=profile.source_id,
        )
        cache = SQLAlchemyCatalogueRepository(engine)
        cache.record_profile(
            run.run_id,
            profile,
            Coverage(profile_fingerprint=profile.fingerprint, observed=run.accepted, complete=True),
        )
        cache.publish(run.run_id)
        return run

    def refresh(adapter: AcquisitionAdapter, profile: AcquisitionProfile) -> ScrapeResult:
        calls.append(profile.source_id)
        return publish(profile)

    cache = RecipeCache(settings, options, lambda: time[0], refresh=refresh, registry=registry)
    return cache, options, profiles, publish, calls, time, settings, catalog


def test_local_cache_hit_does_not_refresh_and_pins_effective_profile(setup_cache: Any) -> None:
    cache, _, profiles, publish, calls, _, _, catalog = setup_cache
    run = publish(profiles[0])
    inputs = cache.pin(RecipeRequest(), catalog)
    assert calls == []
    assert inputs["run_id"] == run.run_id
    assert inputs["coverage_complete"]
    assert inputs["profile_fingerprints"] == {"kupi": profiles[0].fingerprint}


@pytest.mark.parametrize("policy", ["local", "refresh", "no-cache"])
def test_missing_collection_refreshes_only_requested_profile(setup_cache: Any, policy: str) -> None:
    cache, _, profiles, _, calls, _, _, catalog = setup_cache
    inputs = cache.pin(RecipeRequest(cache_policy=policy), catalog)
    assert calls == ["kupi"]
    assert inputs["catalogue_snapshot"]["profile_fingerprints"] == [profiles[0].fingerprint]


def test_cache_only_prevents_refresh_even_when_missing(setup_cache: Any) -> None:
    cache, _, profiles, _, calls, _, _, catalog = setup_cache
    with pytest.raises(ValueError, match="missing"):
        cache.pin(
            RecipeRequest(cache_policy="cache-only", profile_fingerprint=profiles[0].fingerprint),
            catalog,
        )
    assert calls == []


def test_stale_local_refresh_and_force_policies(setup_cache: Any) -> None:
    cache, _, profiles, publish, calls, time, _, catalog = setup_cache
    old = publish(profiles[0], at=NOW - timedelta(hours=37))
    inputs = cache.pin(RecipeRequest(), catalog)
    assert inputs["run_id"] != old.run_id and calls == ["kupi"]
    time[0] += timedelta(minutes=1)
    forced = cache.pin(RecipeRequest(cache_policy="no-cache"), catalog)
    assert forced["run_id"] != inputs["run_id"] and calls == ["kupi", "kupi"]


@pytest.mark.parametrize("policy", ["refresh", "no-cache", "local"])
def test_failed_refresh_fallback_requires_fresh_complete_same_profile(
    setup_cache: Any, policy: str
) -> None:
    cache, _, profiles, publish, _, time, _, catalog = setup_cache
    run = publish(profiles[0])
    calls = []

    def fail(*args: Any) -> ScrapeResult:
        calls.append(True)
        raise RuntimeError("private credentials must not surface")

    cache.refresh = fail
    request = RecipeRequest(cache_policy=policy)
    if policy == "no-cache":
        with pytest.raises(RuntimeError, match="fallback is disabled"):
            cache.pin(request, catalog)
    else:
        result = cache.pin(request, catalog)
        assert result["run_id"] == run.run_id
        assert bool(result["warnings"]) == (policy == "refresh")
    assert bool(calls) == (policy != "local")
    time[0] += timedelta(hours=37)
    with pytest.raises(RuntimeError):
        cache.pin(request, catalog)


def test_combined_pins_all_runs_scopes_and_quote_provenance(setup_cache: Any) -> None:
    cache, _, profiles, publish, calls, _, _, catalog = setup_cache
    runs = [publish(profile) for profile in profiles]
    request = RecipeRequest(
        cache_policy="cache-only",
        source_ids=("kupi", "tesco"),
        profile_fingerprints={p.source_id: p.fingerprint for p in profiles},
    )
    inputs = cache.pin(request, catalog)
    assert inputs["catalogue_snapshot"]["run_ids"] == [run.run_id for run in runs]
    assert inputs["coverage_complete"] and calls == []
    assert {item["source_id"] for item in inputs["offers"]} == {"kupi", "tesco"}
    assert {item["offer"]["scope"] for item in inputs["offers"]} == {
        catalog.policy.scope,
        "online:fixture",
    }
    publish(profiles[0])
    assert inputs["catalogue_snapshot"]["run_ids"][0] == runs[0].run_id


def test_shared_recipe_service_evaluates_pinned_combined_inputs(setup_cache: Any) -> None:
    cache, options, profiles, publish, _, time, settings, _ = setup_cache
    for profile in profiles:
        publish(profile)
    service = RecipeService(settings, options, lambda: time[0])
    service.cache = cache
    request = RecipeRequest(
        provider="template",
        cache_policy="cache-only",
        source_ids=("kupi", "tesco"),
        profile_fingerprints={p.source_id: p.fingerprint for p in profiles},
        max_stores=2,
    )
    inputs = service.prepare(request)
    result, html = service.execute(request, inputs)
    assert result["status"] == "ok" and "<html" in html
    assert len(result["catalogue_snapshot"]["run_ids"]) == 2
    assert result["effective_request"]["profile_fingerprints"] == request.profile_fingerprints
    assert all(
        line["price"]["source_id"] in request.source_ids
        for meal in result["report"]["meals"]
        for line in meal["lines"]
        if line["price"]["offer"]
    )


def test_unconfigured_source_and_ambiguous_profiles_fail_before_refresh(setup_cache: Any) -> None:
    cache, options, profiles, _, calls, _, _, catalog = setup_cache
    with pytest.raises(ValueError, match="compatible"):
        cache.pin(RecipeRequest(source_ids=("rohlik",)), catalog)
    options.acquisition_profiles = (profiles[0], profiles[0])
    with pytest.raises(ValueError, match="unique"):
        cache.pin(RecipeRequest(), catalog)
    assert calls == []


def test_concurrent_local_misses_share_one_refresh_under_writer_lock(setup_cache: Any) -> None:
    cache, _, _, _, calls, _, _, catalog = setup_cache
    barrier = Barrier(3)

    def prepare() -> dict[str, Any]:
        barrier.wait(timeout=5)
        return cache.pin(RecipeRequest(), catalog)

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(prepare) for _ in range(3)]
        results = [future.result(timeout=10) for future in futures]
    assert calls == ["kupi"]
    assert len({result["run_id"] for result in results}) == 1


def test_combined_refresh_only_missing_profile_and_keeps_other_pin(setup_cache: Any) -> None:
    cache, _, profiles, publish, calls, _, _, catalog = setup_cache
    first = publish(profiles[0])
    result = cache.pin(RecipeRequest(source_ids=("kupi", "tesco")), catalog)
    assert calls == ["tesco"]
    assert result["catalogue_snapshot"]["run_ids"][0] == first.run_id


def test_no_cache_does_not_accept_wrong_profile_or_incomplete_success(setup_cache: Any) -> None:
    cache, _, profiles, publish, _, _, _, catalog = setup_cache
    old = publish(profiles[0])
    cache.refresh = lambda *args: ScrapeResult(source_id="kupi", status="success")
    with pytest.raises(RuntimeError, match="fallback is disabled"):
        cache.pin(RecipeRequest(cache_policy="no-cache"), catalog)
    assert cache.pin(RecipeRequest(), catalog)["run_id"] == old.run_id


def test_unknown_legacy_heads_cannot_satisfy_combined_coverage(setup_cache: Any) -> None:
    cache, _, profiles, publish, _, _, _, catalog = setup_cache
    publish(profiles[0])
    # The first selected named profile cannot implicitly supply a legacy second head.
    with pytest.raises(ValueError, match="missing"):
        cache.pin(
            RecipeRequest(
                cache_policy="cache-only",
                source_ids=("kupi", "tesco"),
                profile_fingerprints={"kupi": profiles[0].fingerprint},
            ),
            catalog,
        )
